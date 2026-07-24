from __future__ import annotations

import http.client
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("opc_dashboard", SCRIPTS / "opc_dashboard.py")
opc_dashboard = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(opc_dashboard)


STAMP = "2026-07-23T05:00:00Z"


class DashboardMarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.navigation = []
        self.views = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set(attributes.get("class", "").split())
        if tag == "a" and "nav-item" in classes:
            self.navigation.append(attributes)
        if tag == "section" and "dashboard-view" in classes:
            self.views.append(attributes)


def acceptance_table(statuses: list[str]) -> str:
    rows = [
        "# OPC Acceptance Contract",
        "",
        "| Criterion | Verification method | Required evidence | Status |",
        "|---|---|---|---|",
    ]
    rows.extend(
        f"| Criterion {index} | inspect | evidence | {status} |"
        for index, status in enumerate(statuses, 1)
    )
    return "\n".join(rows) + "\n"


class ProjectFixture:
    def __init__(self, root: Path, project_id: str = "project-alpha"):
        self.root = root
        opc = root / ".opc"
        opc.mkdir(parents=True)
        (opc / "project.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": project_id,
                    "name": "Project Alpha",
                    "created_at": STAMP,
                    "updated_at": STAMP,
                }
            ),
            encoding="utf-8",
        )
        (opc / "run.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "opc-secret-run-id",
                    "title": "Build dashboard",
                    "project_id": project_id,
                    "status": "validating",
                    "active": True,
                    "evidence": {},
                    "created_at": STAMP,
                    "updated_at": STAMP,
                }
            ),
            encoding="utf-8",
        )
        (opc / "acceptance.md").write_text(
            acceptance_table(["pass", "pending", "pass"]),
            encoding="utf-8",
        )


class FakeBackend:
    def __init__(self, root):
        self.root = Path(root)

    def doctor(self):
        return {
            "ok": True,
            "state": "READY",
            "provenance_ready": True,
            "git": {"authoritative_uncommitted": ["redacted-by-dashboard"]},
        }

    def governance_snapshot(self):
        return {
            "inventory": {
                "exp-candidate": {"status": "candidate"},
                "exp-published": {"status": "approved"},
                "exp-approved": {"status": "approved"},
                "exp-rejected": {"status": "rejected"},
                "exp-obsolete": {"status": "obsolete"},
            },
            "provenance": {
                "exp-candidate": {"source_commit": None},
                "exp-published": {"source_commit": "a" * 40},
                "exp-approved": {"source_commit": None},
                "exp-rejected": {"source_commit": "a" * 40},
                "exp-obsolete": {"source_commit": "a" * 40},
            },
        }


class FakeService:
    @classmethod
    def from_paths(cls, knowledge_root, data_root):
        return cls()

    def status(self):
        return {"mem0": {"health": "disabled"}}


class SnapshotTests(unittest.TestCase):
    def test_aggregation_is_redacted_and_counts_publication_by_git_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = ProjectFixture(base / "project")
            knowledge = base / "knowledge"
            data = base / "data"
            knowledge.mkdir()
            data.mkdir()
            with (
                mock.patch.object(opc_dashboard, "FileGitBackend", FakeBackend),
                mock.patch.object(opc_dashboard, "MemoryService", FakeService),
                mock.patch.object(
                    opc_dashboard,
                    "read_feedback",
                    return_value={"structured_feedback": {"events": []}},
                ),
                mock.patch.object(
                    opc_dashboard,
                    "build_view",
                    return_value={"lineage_status": "available"},
                ),
            ):
                snapshot = opc_dashboard.aggregate_snapshot(
                    [project.root],
                    knowledge_root=knowledge,
                    data_root=data,
                    now=lambda: STAMP,
                )
        self.assertEqual(snapshot["schema_version"], "opc-dashboard.snapshot.v1")
        self.assertEqual(snapshot["mode"], "live")
        self.assertEqual(snapshot["summary"], {
            "active_projects": 1,
            "pending_acceptance": 1,
            "candidates": 1,
            "published": 1,
        })
        self.assertEqual(snapshot["knowledge"]["published"], 1)
        self.assertEqual(snapshot["knowledge"]["approved_uncommitted"], 1)
        self.assertEqual(snapshot["projects"][0]["acceptance"]["passed"], 2)
        self.assertEqual(snapshot["projects"][0]["feedback_status"], "available")
        serialized = json.dumps(snapshot)
        self.assertNotIn("opc-secret-run-id", serialized)
        self.assertNotIn(str(project.root), serialized)
        self.assertNotIn("run_id", serialized)

    def test_missing_live_knowledge_never_falls_back_to_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = ProjectFixture(base / "project")
            with (
                mock.patch.object(
                    opc_dashboard,
                    "read_feedback",
                    return_value={"structured_feedback": None},
                ),
                mock.patch.object(
                    opc_dashboard,
                    "build_view",
                    return_value={"lineage_status": "unavailable"},
                ),
            ):
                snapshot = opc_dashboard.aggregate_snapshot(
                    [project.root],
                    knowledge_root=base / "missing-knowledge",
                    data_root=base / "data",
                )
        self.assertEqual(snapshot["mode"], "live")
        self.assertEqual(snapshot["knowledge"]["state"], "unavailable")
        self.assertEqual(snapshot["summary"]["published"], 0)
        self.assertTrue(
            any(
                item["code"] in {"KNOWLEDGE_NOT_INITIALIZED", "KNOWLEDGE_SOURCE_UNAVAILABLE"}
                for item in snapshot["warnings"]
            )
        )

    def test_invalid_project_is_visible_as_invalid_without_path_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "bad-project"
            (project / ".opc").mkdir(parents=True)
            (project / ".opc" / "project.json").write_text("{bad", encoding="utf-8")
            with (
                mock.patch.object(opc_dashboard, "FileGitBackend", FakeBackend),
                mock.patch.object(opc_dashboard, "MemoryService", FakeService),
            ):
                snapshot = opc_dashboard.aggregate_snapshot(
                    [project],
                    knowledge_root=base / "knowledge",
                    data_root=base / "data",
                )
        self.assertEqual(snapshot["projects"][0]["source_state"], "invalid")
        self.assertNotIn(str(project), json.dumps(snapshot))

    def test_demo_is_explicit_synthetic_fixture(self):
        snapshot = opc_dashboard.load_demo_snapshot()
        self.assertEqual(snapshot["mode"], "demo")
        self.assertEqual(snapshot["schema_version"], "opc-dashboard.snapshot.v1")
        self.assertTrue(any(item["code"] == "SYNTHETIC_DATA" for item in snapshot["warnings"]))

    def test_demo_loader_rejects_forbidden_identifier_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = opc_dashboard.load_demo_snapshot()
            snapshot["run_id"] = "secret"
            (root / "synthetic-dashboard.v1.json").write_text(
                json.dumps(snapshot),
                encoding="utf-8",
            )
            with self.assertRaises(opc_dashboard.DashboardError) as caught:
                opc_dashboard.load_demo_snapshot(root)
        self.assertEqual(caught.exception.code, "INVALID_DEMO")

    def test_inactive_run_is_not_counted_as_active(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = ProjectFixture(base / "project")
            run_path = project.root / ".opc" / "run.json"
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["active"] = False
            run_path.write_text(json.dumps(run), encoding="utf-8")
            with (
                mock.patch.object(opc_dashboard, "FileGitBackend", FakeBackend),
                mock.patch.object(opc_dashboard, "MemoryService", FakeService),
                mock.patch.object(
                    opc_dashboard,
                    "read_feedback",
                    return_value={"structured_feedback": None},
                ),
                mock.patch.object(
                    opc_dashboard,
                    "build_view",
                    return_value={"lineage_status": "unavailable"},
                ),
            ):
                snapshot = opc_dashboard.aggregate_snapshot(
                    [project.root],
                    knowledge_root=base / "knowledge",
                    data_root=base / "data",
                )
        self.assertFalse(snapshot["projects"][0]["run"]["active"])
        self.assertEqual(snapshot["summary"]["active_projects"], 0)

    def test_redaction_rejects_posix_absolute_paths(self):
        for value in ("/opt/private", "diagnostic: /etc/passwd"):
            with self.subTest(value=value):
                with self.assertRaises(opc_dashboard.DashboardError) as caught:
                    opc_dashboard._assert_redacted({"name": value})
                self.assertEqual(caught.exception.code, "ABSOLUTE_PATH_REDACTED")


class DashboardAssetContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.asset_root = ROOT / "plugins" / "codex-opc-team" / "assets" / "dashboard"
        cls.html = (cls.asset_root / "index.html").read_text(encoding="utf-8")
        cls.script = (cls.asset_root / "dashboard.js").read_text(encoding="utf-8")

    def test_navigation_targets_six_distinct_views(self):
        parser = DashboardMarkupParser()
        parser.feed(self.html)
        expected = ["overview", "projects", "runs", "knowledge", "lineage", "health"]
        self.assertEqual([item["data-nav"] for item in parser.navigation], expected)
        self.assertEqual([item["href"] for item in parser.navigation], [f"#{name}" for name in expected])
        self.assertEqual([item["aria-controls"] for item in parser.navigation], expected)
        self.assertEqual([item["id"] for item in parser.views], expected)
        self.assertEqual(
            [item["id"] for item in parser.views if "hidden" not in item],
            ["overview"],
        )

    def test_navigation_restores_hash_and_only_shows_active_view(self):
        self.assertIn('window.addEventListener("hashchange"', self.script)
        self.assertIn("view.hidden = !selected", self.script)
        self.assertIn('window.history.replaceState(null, "", `#${initialView}`)', self.script)
        self.assertIn('renderRuns(snapshot)', self.script)
        self.assertIn('text("run-count"', self.script)


class StableReadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "acceptance.md"
        self.path.write_text(acceptance_table(["pass", "pending"]), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_acceptance_is_bounded_and_counted(self):
        result = opc_dashboard._parse_acceptance(self.path, self.root)
        self.assertEqual(result, {"passed": 1, "total": 2, "state": "available"})
        self.path.write_bytes(b"x" * (opc_dashboard.MAX_ACCEPTANCE_BYTES + 1))
        with self.assertRaises(opc_dashboard.DashboardError) as caught:
            opc_dashboard._parse_acceptance(self.path, self.root)
        self.assertEqual(caught.exception.code, "SOURCE_TOO_LARGE")

    def test_path_escape_is_rejected(self):
        outside = self.root.parent / f"{self.root.name}-outside.md"
        outside.write_text("outside", encoding="utf-8")
        try:
            with self.assertRaises(opc_dashboard.DashboardError) as caught:
                opc_dashboard._read_stable_bytes(
                    outside,
                    root=self.root,
                    maximum=100,
                    label="outside",
                )
            self.assertEqual(caught.exception.code, "PATH_ESCAPE")
        finally:
            outside.unlink()

    def test_symlink_is_rejected_when_platform_can_create_it(self):
        target = self.root / "target.md"
        target.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
        linked = self.root / "linked.md"
        try:
            linked.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(opc_dashboard.DashboardError) as caught:
            opc_dashboard._read_stable_bytes(
                linked,
                root=self.root,
                maximum=1024,
                label="linked",
            )
        self.assertEqual(caught.exception.code, "LINKED_SOURCE")

    def test_hardlink_is_rejected(self):
        linked = self.root / "hardlinked.md"
        try:
            os.link(self.path, linked)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        with self.assertRaises(opc_dashboard.DashboardError) as caught:
            opc_dashboard._read_stable_bytes(
                linked,
                root=self.root,
                maximum=1024,
                label="hardlinked",
            )
        self.assertEqual(caught.exception.code, "HARDLINKED_SOURCE")

    def test_toctou_change_is_rejected(self):
        def mutate(label, path):
            if label.endswith("before_verify"):
                os.utime(path, ns=(1_000_000_000, 1_000_000_000))

        with (
            mock.patch.object(opc_dashboard, "_read_checkpoint", side_effect=mutate),
            self.assertRaises(opc_dashboard.DashboardError) as caught,
        ):
            opc_dashboard._read_stable_bytes(
                self.path,
                root=self.root,
                maximum=1024,
                label="acceptance",
            )
        self.assertEqual(caught.exception.code, "SOURCE_CHANGED")


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.asset_root = Path(self.temporary.name)
        (self.asset_root / "index.html").write_text("<!doctype html><title>OPC</title>", encoding="utf-8")
        (self.asset_root / "dashboard.css").write_text("body {}", encoding="utf-8")
        (self.asset_root / "dashboard.js").write_text("void 0;", encoding="utf-8")
        self.snapshot = opc_dashboard.load_demo_snapshot()
        self.server = opc_dashboard.create_server(
            host="127.0.0.1",
            port=0,
            snapshot_provider=lambda: self.snapshot,
            asset_root=self.asset_root,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        host: str | None = None,
        origin: str | None = None,
    ):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host or f"127.0.0.1:{self.port}")
        if origin is not None:
            connection.putheader("Origin", origin)
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    def test_get_snapshot_has_security_headers_and_redacted_json(self):
        status, headers, body = self.request("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("camera=()", headers["Permissions-Policy"])
        self.assertEqual(json.loads(body)["mode"], "demo")

    def test_head_has_get_length_but_no_body(self):
        get_status, get_headers, get_body = self.request("GET", "/dashboard.css")
        head_status, head_headers, head_body = self.request("HEAD", "/dashboard.css")
        self.assertEqual((get_status, head_status), (200, 200))
        self.assertEqual(head_body, b"")
        self.assertEqual(head_headers["Content-Length"], str(len(get_body)))
        self.assertEqual(head_headers["Content-Length"], get_headers["Content-Length"])

    def test_host_must_be_exact_loopback_authority(self):
        for host in ("localhost", "127.0.0.1", f"localhost:{self.port}", "evil.example"):
            with self.subTest(host=host):
                status, headers, body = self.request("GET", "/", host=host)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"], "INVALID_HOST")
                self.assertIn("Content-Security-Policy", headers)

    def test_origin_must_be_absent_or_exact_same_origin(self):
        expected = f"http://127.0.0.1:{self.port}"
        status, headers, _ = self.request("GET", "/api/snapshot", origin=expected)
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        for origin in ("https://evil.example", "null", f"http://localhost:{self.port}"):
            with self.subTest(origin=origin):
                status, headers, body = self.request(
                    "GET",
                    "/api/snapshot",
                    origin=origin,
                )
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body)["error"], "ORIGIN_FORBIDDEN")
                self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_only_allowlisted_static_paths_are_served(self):
        status, _, _ = self.request("GET", "/../opc_dashboard.py")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/unknown.js")
        self.assertEqual(status, 404)

    def test_mutating_methods_are_rejected(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
            with self.subTest(method=method):
                status, headers, body = self.request(method, "/api/snapshot")
                self.assertEqual(status, 405)
                self.assertEqual(json.loads(body)["error"], "METHOD_NOT_ALLOWED")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["Allow"], "GET, HEAD")

    def test_asset_symlink_is_not_followed(self):
        target = self.asset_root / "target.html"
        target.write_text("secret", encoding="utf-8")
        index = self.asset_root / "index.html"
        index.unlink()
        try:
            index.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 404)
        self.assertNotIn(b"secret", body)


class ServerConfigurationTests(unittest.TestCase):
    def test_non_loopback_bind_is_rejected(self):
        for host in ("0.0.0.0", "::", "localhost", "192.168.1.2"):
            with self.subTest(host=host):
                with self.assertRaises(opc_dashboard.DashboardError) as caught:
                    opc_dashboard.create_server(
                        host=host,
                        port=0,
                        snapshot_provider=dict,
                    )
                self.assertEqual(caught.exception.code, "LOOPBACK_REQUIRED")

    def test_default_cli_port_is_fixed_and_no_open_is_supported(self):
        args = opc_dashboard.build_parser().parse_args(["--demo", "--no-open"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8569)
        self.assertTrue(args.no_open)

    def test_live_cli_resolves_default_private_roots(self):
        captured = {}

        class FakeServer:
            server_address = ("127.0.0.1", 45678)

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                captured["closed"] = True

        def create_server(**kwargs):
            captured.update(kwargs)
            kwargs["snapshot_provider"]()
            return FakeServer()

        with (
            mock.patch.object(
                opc_dashboard,
                "resolve_knowledge_root",
                return_value=Path("private-knowledge"),
            ) as knowledge_root,
            mock.patch.object(
                opc_dashboard,
                "resolve_data_root",
                return_value=Path("private-data"),
            ) as data_root,
            mock.patch.object(
                opc_dashboard,
                "aggregate_snapshot",
                return_value=opc_dashboard.load_demo_snapshot(),
            ) as aggregate,
            mock.patch.object(opc_dashboard, "create_server", side_effect=create_server),
        ):
            result = opc_dashboard.main(["--project-root", ".", "--no-open"])

        self.assertEqual(result, 0)
        knowledge_root.assert_called_once_with(None)
        data_root.assert_called_once_with(None)
        aggregate.assert_called_once_with(
            ["."],
            knowledge_root=Path("private-knowledge"),
            data_root=Path("private-data"),
        )
        self.assertTrue(captured["closed"])

    def test_occupied_port_fails_clearly(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            with self.assertRaises(opc_dashboard.DashboardError) as caught:
                opc_dashboard.create_server(
                    host="127.0.0.1",
                    port=port,
                    snapshot_provider=dict,
                )
            self.assertEqual(caught.exception.code, "PORT_UNAVAILABLE")
        finally:
            listener.close()


if __name__ == "__main__":
    unittest.main()
