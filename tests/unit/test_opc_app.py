from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "scripts"))

import opc_app  # noqa: E402
import opc_app_admin  # noqa: E402
import opc_dashboard  # noqa: E402
import opc_snapshot_service  # noqa: E402
from opc_snapshot_service import SnapshotService  # noqa: E402


class AppMarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.navigation: list[dict[str, str | None]] = []
        self.views: list[dict[str, str | None]] = []
        self.remote_resources: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "a" and "nav-item" in classes:
            self.navigation.append(attributes)
        if tag == "section" and "app-view" in classes:
            self.views.append(attributes)
        if tag in {"link", "script", "img"}:
            value = attributes.get("href") or attributes.get("src")
            if value and value.startswith(("http://", "https://", "//")):
                self.remote_resources.append(value)


def project_fixture(root: Path, project_id: str = "sample-project") -> Path:
    opc = root / ".opc"
    opc.mkdir(parents=True)
    (opc / "project.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": project_id,
                "name": "Synthetic Project",
                "created_at": "2026-07-25T00:00:00Z",
                "updated_at": "2026-07-25T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    marker = root / "must-survive.txt"
    marker.write_text("preserve", encoding="utf-8")
    return root


@contextlib.contextmanager
def running_server(*, store, demo=False, provider=None, adapter_manager=None):
    server = opc_app.create_app_server(
        host="127.0.0.1",
        port=0,
        snapshot_provider=provider or opc_dashboard.load_demo_snapshot,
        settings_store=store,
        adapter_manager=adapter_manager,
        demo=demo,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(
    server,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
):
    host, port = server.server_address[:2]
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = dict(headers or {})
    if body is not None:
        request_headers.setdefault("Content-Type", "application/json")
        request_headers["Content-Length"] = str(len(body))
    connection = http.client.HTTPConnection(host, port, timeout=5)
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    content = response.read()
    response_headers = dict(response.getheaders())
    connection.close()
    decoded = json.loads(content) if content else None
    return response.status, decoded, response_headers, content


class SettingsStoreTests(unittest.TestCase):
    def test_relative_app_state_root_is_rejected_by_runtime_and_installer(self):
        with self.assertRaises(opc_app.AppSettingsError) as runtime:
            opc_app.resolve_app_state_root("relative/app-state")
        self.assertEqual(
            runtime.exception.code,
            "ABSOLUTE_APP_STATE_ROOT_REQUIRED",
        )
        previous = os.environ.get("OPC_APP_HOME")
        os.environ["OPC_APP_HOME"] = "relative/app-state"
        try:
            with self.assertRaises(opc_app_admin.AppInstallError):
                opc_app_admin.default_state_root()
        finally:
            if previous is None:
                os.environ.pop("OPC_APP_HOME", None)
            else:
                os.environ["OPC_APP_HOME"] = previous

    def test_state_root_rejects_checkout_before_directory_creation(self):
        candidate = ROOT / f".opc-app-state-test-{os.getpid()}"
        self.assertFalse(candidate.exists())
        with self.assertRaises(opc_app.AppSettingsError) as caught:
            opc_app.AppSettingsStore(candidate)
        self.assertEqual(caught.exception.code, "STATE_ROOT_OVERLAP")
        self.assertFalse(candidate.exists())

    def test_state_root_rejects_project_and_dot_opc_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            project = project_fixture(Path(directory) / "project")
            for candidate in (project / "app-state", project / ".opc" / "app-state"):
                store = opc_app.AppSettingsStore(candidate)
                with self.assertRaises(opc_app.AppSettingsError) as caught:
                    store.add_project(str(project))
                self.assertEqual(caught.exception.code, "STATE_ROOT_OVERLAP")
                self.assertFalse(candidate.exists())

    def test_state_root_rejects_knowledge_data_and_parent_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            knowledge = base / "knowledge"
            data = base / "data"
            knowledge.mkdir()
            data.mkdir()
            cases = (
                knowledge / "state",
                data / "state",
                base,
            )
            for candidate in cases:
                with self.subTest(candidate=candidate):
                    with self.assertRaises(opc_app.AppSettingsError) as caught:
                        opc_app.AppSettingsStore(
                            candidate,
                            forbidden_roots=(knowledge, data),
                        )
                    self.assertEqual(caught.exception.code, "STATE_ROOT_OVERLAP")
                    if candidate != base:
                        self.assertFalse(candidate.exists())

    def test_linked_parent_is_rejected_before_target_creation(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlink is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "target"
            target.mkdir()
            alias = base / "state-alias"
            try:
                os.symlink(target, alias, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlink creation is not permitted")
            candidate = alias / "nested"
            with self.assertRaises(opc_app.AppSettingsError) as caught:
                opc_app.AppSettingsStore(candidate)
            self.assertEqual(caught.exception.code, "UNSAFE_STATE_ROOT")
            self.assertFalse((target / "nested").exists())

    def test_existing_registry_cannot_hide_state_project_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            project = project_fixture(Path(directory) / "project")
            state = project / ".opc" / "app-state"
            state.mkdir()
            settings = {
                "schema_version": opc_app.APP_SETTINGS_SCHEMA,
                "projects": [
                    {
                        "id": "project-malicious",
                        "path": str(project),
                        "added_at": "2026-07-25T00:00:00Z",
                    }
                ],
                "selected_project_id": "project-malicious",
            }
            settings_path = state / "settings.json"
            original = json.dumps(settings)
            settings_path.write_text(original, encoding="utf-8")
            store = opc_app.AppSettingsStore(state)

            self.assertEqual(store.context()["settings_state"], "invalid")
            with self.assertRaises(opc_app.AppSettingsError) as caught:
                store.add_project(str(project))
            self.assertEqual(caught.exception.code, "STATE_ROOT_OVERLAP")
            self.assertEqual(settings_path.read_text(encoding="utf-8"), original)

    def test_registry_is_app_owned_redacted_and_removal_preserves_project(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state_root = base / "state"
            project = project_fixture(base / "project")
            store = opc_app.AppSettingsStore(state_root)

            store.add_project(str(project))
            context = store.context()
            serialized = json.dumps(context)
            settings = json.loads((state_root / "settings.json").read_text(encoding="utf-8"))

            self.assertEqual(context["settings_state"], "ready")
            self.assertEqual(context["projects"][0]["name"], "Synthetic Project")
            self.assertEqual(context["projects"][0]["root_label"], "显式目录 1")
            self.assertNotIn(str(project), serialized)
            self.assertEqual(settings["projects"][0]["path"], str(project))

            item_id = context["projects"][0]["id"]
            store.remove_project(item_id)
            self.assertEqual(store.project_roots(), ())
            self.assertTrue((project / ".opc" / "project.json").is_file())
            self.assertEqual((project / "must-survive.txt").read_text(encoding="utf-8"), "preserve")

    def test_corrupt_registry_degrades_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "state"
            state_root.mkdir()
            settings = state_root / "settings.json"
            settings.write_text("{invalid", encoding="utf-8")
            store = opc_app.AppSettingsStore(state_root)

            self.assertEqual(store.project_roots(), ())
            self.assertEqual(store.context()["settings_state"], "invalid")
            with self.assertRaises(opc_app.AppSettingsError) as caught:
                store.add_project(str(Path(directory) / "missing"))
            self.assertEqual(caught.exception.code, "INVALID_SETTINGS")
            self.assertEqual(settings.read_text(encoding="utf-8"), "{invalid")

    def test_project_must_be_absolute_valid_and_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = project_fixture(base / "project")
            store = opc_app.AppSettingsStore(base / "state")
            with self.assertRaises(opc_app.AppSettingsError) as relative:
                store.add_project("relative/project")
            self.assertEqual(relative.exception.code, "ABSOLUTE_PROJECT_ROOT_REQUIRED")
            store.add_project(str(project))
            with self.assertRaises(opc_app.AppSettingsError) as duplicate:
                store.add_project(str(project))
            self.assertEqual(duplicate.exception.code, "PROJECT_ALREADY_REGISTERED")

    def test_linked_state_file_is_never_followed(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlink is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state_root = base / "state"
            state_root.mkdir()
            outside = base / "outside.json"
            outside.write_text(json.dumps(opc_app._default_settings()), encoding="utf-8")
            try:
                os.symlink(outside, state_root / "settings.json")
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is not permitted")
            store = opc_app.AppSettingsStore(state_root)
            self.assertEqual(store.context()["settings_state"], "invalid")
            self.assertEqual(outside.read_text(encoding="utf-8"), json.dumps(opc_app._default_settings()))


class SnapshotServiceTests(unittest.TestCase):
    def test_app_allows_empty_registry_without_changing_dashboard_default(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with self.assertRaises(opc_dashboard.DashboardError):
                opc_dashboard.aggregate_snapshot(
                    [],
                    knowledge_root=base / "knowledge",
                    data_root=base / "data",
                )
            snapshot = opc_dashboard.aggregate_snapshot(
                [],
                knowledge_root=base / "knowledge",
                data_root=base / "data",
                allow_empty=True,
            )
            self.assertEqual(snapshot["projects"], [])
            self.assertEqual(snapshot["summary"]["active_projects"], 0)
            self.assertNotEqual(snapshot["mode"], "demo")

    def test_shared_service_validates_the_same_snapshot_contract(self):
        expected = opc_dashboard.load_demo_snapshot()
        service = SnapshotService(
            project_roots_provider=tuple,
            demo=True,
        )
        self.assertEqual(service.snapshot(), expected)


class HTTPTests(unittest.TestCase):
    def test_adapter_routes_keep_preview_and_apply_separate(self):
        class FakeManager:
            def __init__(self):
                self.applied = False
                self.fail_with_recovery = False
                self.rolled_back = False

            def inventory(self):
                return {
                    "schema_version": "opc-adapters.api.v1",
                    "hosts": [{"host_id": "codex", "state": "available"}],
                }

            def create_plan(self, host_id, operation):
                return {
                    "schema_version": "opc-adapters.plan.v1",
                    "host_id": host_id,
                    "operation": operation,
                    "plan_id": "plan-safe",
                    "confirmation_token": "confirm-safe",
                    "changes": [{"action": "add", "target": "codex:plugin/opc"}],
                }

            def apply(self, *, plan_id, confirmation_token):
                if confirmation_token != "confirm-safe":
                    raise opc_app.AdapterError("CONFIRMATION_REQUIRED")
                if self.fail_with_recovery:
                    raise opc_app.AdapterError(
                        "ROLLBACK_FAILED",
                        rollback_id="op-recover-safe",
                    )
                self.applied = True
                return {
                    "schema_version": "opc-adapters.api.v1",
                    "host_id": "codex",
                    "state": "completed",
                    "rollback_id": "op-complete-safe",
                }

            def rollback(self, host_id, rollback_id):
                self.rolled_back = True
                return {
                    "schema_version": "opc-adapters.api.v1",
                    "host_id": host_id,
                    "state": "rolled_back",
                    "rollback_id": rollback_id,
                }

        with tempfile.TemporaryDirectory() as directory:
            store = opc_app.AppSettingsStore(Path(directory) / "state")
            adapters = FakeManager()
            with running_server(store=store, adapter_manager=adapters) as server:
                authority = f"http://127.0.0.1:{server.server_address[1]}"
                _, context, _, _ = request(server, "GET", "/api/app-context")
                headers = {"Origin": authority, "X-OPC-CSRF": context["csrf_token"]}
                status, inventory, _, raw = request(server, "GET", "/api/adapters")
                self.assertEqual(status, 200)
                self.assertEqual(inventory["hosts"][0]["state"], "available")
                self.assertNotRegex(raw.decode(), r"[A-Za-z]:[\\/]")

                status, plan, _, _ = request(
                    server,
                    "POST",
                    "/api/adapters/plan",
                    headers=headers,
                    payload={"host_id": "codex", "operation": "install"},
                )
                self.assertEqual(status, 200)
                self.assertFalse(adapters.applied)
                status, error, _, _ = request(
                    server,
                    "POST",
                    "/api/adapters/apply",
                    headers=headers,
                    payload={
                        "plan_id": plan["plan_id"],
                        "confirmation_token": "wrong",
                    },
                )
                self.assertEqual(status, 409)
                self.assertEqual(error["error"], "CONFIRMATION_REQUIRED")
                self.assertFalse(adapters.applied)
                adapters.fail_with_recovery = True
                status, error, _, _ = request(
                    server,
                    "POST",
                    "/api/adapters/apply",
                    headers=headers,
                    payload={
                        "plan_id": plan["plan_id"],
                        "confirmation_token": plan["confirmation_token"],
                    },
                )
                self.assertEqual(status, 409)
                self.assertEqual(error["error"], "ROLLBACK_FAILED")
                self.assertEqual(error["rollback_id"], "op-recover-safe")

                adapters.fail_with_recovery = False
                status, result, _, _ = request(
                    server,
                    "POST",
                    "/api/adapters/apply",
                    headers=headers,
                    payload={
                        "plan_id": plan["plan_id"],
                        "confirmation_token": plan["confirmation_token"],
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(result["rollback_id"], "op-complete-safe")
                status, result, _, _ = request(
                    server,
                    "POST",
                    "/api/adapters/rollback",
                    headers=headers,
                    payload={
                        "host_id": "codex",
                        "rollback_id": result["rollback_id"],
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(result["state"], "rolled_back")
                self.assertTrue(adapters.rolled_back)

    def test_context_is_redacted_and_security_boundaries_are_enforced(self):
        store = opc_app.DemoSettingsStore()
        with running_server(store=store, demo=True) as server:
            authority = f"http://127.0.0.1:{server.server_address[1]}"
            status, context, headers, _ = request(server, "GET", "/api/app-context")
            token = context["csrf_token"]
            self.assertEqual(status, 200)
            self.assertEqual(context["schema_version"], "opc-app.context.v1")
            serialized = json.dumps(context, ensure_ascii=False)
            self.assertNotRegex(serialized, r"[A-Za-z]:[\\/]")
            self.assertNotIn("/home/", serialized)
            self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
            self.assertEqual(headers["Cache-Control"], "no-store")

            status, _, _, _ = request(
                server,
                "GET",
                "/api/app-context",
                headers={"Host": "remote.example"},
            )
            self.assertEqual(status, 400)
            status, _, _, _ = request(
                server,
                "GET",
                "/api/app-context",
                headers={"Origin": "http://remote.example"},
            )
            self.assertEqual(status, 403)
            status, _, _, _ = request(
                server,
                "POST",
                "/api/selection",
                payload={"project_id": "project-demo-primary"},
            )
            self.assertEqual(status, 403)
            status, error, _, _ = request(
                server,
                "POST",
                "/api/selection",
                headers={"Origin": authority, "X-OPC-CSRF": token},
                payload={"project_id": "project-demo-primary"},
            )
            self.assertEqual(status, 409)
            self.assertEqual(error["error"], "DEMO_SETTINGS_READ_ONLY")
            self.assertEqual(request(server, "PUT", "/api/snapshot")[0], 405)
            self.assertEqual(request(server, "GET", "/api/app-context?raw=1")[0], 404)

    def test_only_app_settings_routes_write_and_never_return_the_input_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = project_fixture(base / "project")
            store = opc_app.AppSettingsStore(base / "state")
            with running_server(store=store) as server:
                authority = f"http://127.0.0.1:{server.server_address[1]}"
                _, context, _, _ = request(server, "GET", "/api/app-context")
                headers = {"Origin": authority, "X-OPC-CSRF": context["csrf_token"]}
                status, context, _, raw = request(
                    server,
                    "POST",
                    "/api/projects",
                    headers=headers,
                    payload={"path": str(project)},
                )
                self.assertEqual(status, 201)
                self.assertNotIn(str(project).encode("utf-8"), raw)
                self.assertTrue((project / "must-survive.txt").is_file())

                item_id = context["projects"][0]["id"]
                status, error, _, _ = request(
                    server,
                    "DELETE",
                    f"/api/projects/{item_id}",
                    headers=headers,
                    payload={"unexpected": True},
                )
                self.assertEqual(status, 400)
                self.assertEqual(error["error"], "INVALID_REQUEST")
                self.assertTrue((project / "must-survive.txt").is_file())

                status, context, _, raw = request(
                    server,
                    "DELETE",
                    f"/api/projects/{item_id}",
                    headers=headers,
                )
                self.assertEqual(status, 200)
                self.assertEqual(context["projects"], [])
                self.assertNotIn(str(project).encode("utf-8"), raw)
                self.assertTrue((project / "must-survive.txt").is_file())

                status, _, _, _ = request(
                    server,
                    "POST",
                    "/api/snapshot",
                    headers=headers,
                    payload={"path": str(project)},
                )
                self.assertEqual(status, 404)


class AssetContractTests(unittest.TestCase):
    def test_complete_app_surface_is_local_and_has_distinct_views(self):
        asset_root = ROOT / "plugins" / "codex-opc-team" / "assets" / "app"
        parser = AppMarkupParser()
        parser.feed((asset_root / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(
            [item["data-nav"] for item in parser.navigation],
            ["overview", "projects", "runs", "knowledge", "lineage", "health", "adapters", "settings"],
        )
        self.assertEqual(
            [item["data-view"] for item in parser.views],
            ["overview", "projects", "runs", "knowledge", "lineage", "health", "adapters", "settings"],
        )
        self.assertEqual(parser.remote_resources, [])
        javascript = (asset_root / "dashboard.js").read_text(encoding="utf-8")
        stylesheet = (asset_root / "dashboard.css").read_text(encoding="utf-8")
        self.assertIn('fetchJSON("/api/app-context")', javascript)
        self.assertIn('fetchJSON("/api/snapshot")', javascript)
        self.assertIn('fetchJSON("/api/adapters")', javascript)
        self.assertIn('"/api/adapters/rollback"', javascript)
        self.assertIn("adapter-rollback-confirm", javascript)
        self.assertIn("dataset.adapterRollbackId", javascript)
        self.assertNotIn("innerHTML", javascript)
        markup = (asset_root / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="adapter-rollback-dialog"', markup)
        self.assertIn('id="confirm-adapter-rollback"', markup)
        self.assertIn(".filterable[hidden]", stylesheet)
        self.assertIn("display: none !important", stylesheet)


if __name__ == "__main__":
    unittest.main()
