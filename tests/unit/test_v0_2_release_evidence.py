from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "v0_2_release_evidence", ROOT / "scripts" / "v0_2_release_evidence.py"
)
release = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(release)


def strict_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


class V02ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.temporary_alias = Path(self.temporary.name)
        self.temporary_canonical = self.temporary_alias.resolve(strict=True)
        self.private = self.temporary_canonical / "private-project"
        (self.private / ".opc" / "release-evidence").mkdir(parents=True)
        (self.private / "evidence").mkdir()

    @staticmethod
    def arm(*, treatment: bool = False) -> dict:
        if treatment:
            counts = {
                "manager_interventions": 1,
                "eligible_manager_decisions": 6,
                "known_defects": 4,
                "qa_caught_defects": 3,
                "rework_loops": 1,
                "valid_reuse_opportunities": 6,
                "valid_reuses": 4,
                "accepted_recalls": 5,
                "false_recall_acceptances": 1,
                "scope_leakage_acceptances": 0,
                "stale_obsolete_acceptances": 0,
                "privacy_failures": 0,
            }
            context = {"total": 240, "median": 80, "p95_nearest_rank": 90}
            latency = {"total": 24, "median": 8, "p95_nearest_rank": 9}
        else:
            counts = {
                "manager_interventions": 2,
                "eligible_manager_decisions": 6,
                "known_defects": 4,
                "qa_caught_defects": 2,
                "rework_loops": 2,
                "valid_reuse_opportunities": 6,
                "valid_reuses": 3,
                "accepted_recalls": 5,
                "false_recall_acceptances": 2,
                "scope_leakage_acceptances": 0,
                "stale_obsolete_acceptances": 0,
                "privacy_failures": 0,
            }
            context = {"total": 300, "median": 100, "p95_nearest_rank": 120}
            latency = {"total": 30, "median": 10, "p95_nearest_rank": 12}
        return {"counts": counts, "context_tokens": context, "latency_ms": latency}

    def summary(self) -> dict:
        return {
            "schema_version": release.PRIVATE_VERSION,
            "contract_version": release.CONTRACT_VERSION,
            "pilot_id": "pilot-0123456789ab",
            "evidence_class": "representative-private-pilot",
            "task_count": 3,
            "task_selection": {
                "fixed_before_execution": True,
                "risk_class_count": 2,
                "work_type_count": 3,
            },
            "arms": {
                "same_evaluation_contract": "opc-evaluation-contract-v1",
                "control": self.arm(),
                "treatment": self.arm(treatment=True),
            },
            "capability_coverage": {
                "context_packets": 3,
                "structured_feedback_records": 3,
                "lineage_records": 3,
                "shadow_pairs": 3,
                "conflicts_seeded": 1,
                "conflicts_rejected": 1,
                "evolution_pilot_cases": 3,
                "rollback_drills": 1,
                "exact_rollback_restores": 1,
            },
            "provider_fallback": {
                "disabled_core_pass": True,
                "delete_rebuild_pass": True,
                "canonical_digest_unchanged": True,
            },
            "attestations": {},
            "confounders": ["task-difficulty", "warm-cache"],
        }

    def write_pilot(self, value: dict | None = None) -> tuple[Path, dict]:
        value = value or self.summary()
        core = release._core_sha256(value)
        semantics = {
            "manager_approval": ("approved", "not_applicable", False),
            "independent_qa": ("pass", "safe", True),
            "shadow_evaluation": ("beneficial", "safe", False),
            "capability_evolution": ("beneficial", "safe", False),
        }
        (self.private / ".opc" / "release-evidence" / "sources").mkdir(exist_ok=True)
        for kind, (decision, safety, independent) in semantics.items():
            source_relative = f".opc/release-evidence/sources/{kind}.json"
            source_path = self.private.joinpath(*Path(source_relative).parts)
            source_path.write_bytes(strict_bytes({"synthetic_private_source_kind": kind}))
            envelope = {
                "schema_version": release.PILOT_EVIDENCE_VERSION,
                "contract_version": release.CONTRACT_VERSION,
                "evidence_kind": kind,
                "pilot_id": value["pilot_id"],
                "pilot_core_sha256": core,
                "task_count": value["task_count"],
                "decision": decision,
                "safety": safety,
                "independent_from_implementer": independent,
                "source_ref": source_relative,
                "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            }
            relative = f".opc/release-evidence/{kind}.json"
            path = self.private.joinpath(*Path(relative).parts)
            path.write_bytes(strict_bytes(envelope))
            item = {"decision": decision, "evidence": {"ref": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}
            if kind == "independent_qa":
                item["independent_from_implementer"] = True
            if kind in {"shadow_evaluation", "capability_evolution"}:
                item["safety"] = safety
            value["attestations"][kind] = item
        summary = self.private / ".opc" / "release-evidence" / "pilot.json"
        summary.write_bytes(strict_bytes(value))
        return summary, value

    def test_committed_public_evidence_is_deterministic_and_explicitly_blocked(self) -> None:
        result = release.build_public_evidence(execute=False)
        self.assertEqual("pass", result["public_evidence_status"])
        self.assertEqual("blocked", result["release_status"])
        self.assertIn("representative_private_3_to_5_task_pilot_required", result["release_blockers"])
        self.assertEqual(
            release.PUBLIC_RESULT_PATH.read_bytes(), release._json_bytes(result)
        )
        self.assertEqual(
            release.PUBLIC_REPORT_PATH.read_bytes(), release._public_report(result)
        )

    def test_real_private_aggregate_with_bound_semantic_attestations_passes(self) -> None:
        path, _ = self.write_pilot()
        verdict = release.validate_private_pilot(self.private, path)
        self.assertEqual("pass", verdict["private_pilot_status"])
        self.assertEqual(0, verdict["safety"]["scope_leakage_acceptances"])
        self.assertTrue(all(value in {"improved", "equal"} for value in verdict["quality_comparison"].values()))
        self.assertIn("improved", verdict["quality_comparison"].values())

    def test_public_template_and_quality_regression_fail_closed(self) -> None:
        private_template = self.private / ".opc" / "release-evidence" / "template.json"
        private_template.write_bytes(
            (ROOT / "evaluation" / "private-pilot-v0.2.template.json").read_bytes()
        )
        with self.assertRaisesRegex(release.ReleaseEvidenceError, "template"):
            release.validate_private_pilot(self.private, private_template)
        value = self.summary()
        value["arms"]["treatment"]["counts"]["manager_interventions"] = 3
        path, _ = self.write_pilot(value)
        with self.assertRaisesRegex(release.ReleaseEvidenceError, "regression"):
            release.validate_private_pilot(self.private, path)

    def test_impossible_distribution_and_coverage_mismatch_fail_closed(self) -> None:
        value = self.summary()
        value["arms"]["treatment"]["context_tokens"] = {
            "total": 5, "median": 80, "p95_nearest_rank": 90
        }
        path, _ = self.write_pilot(value)
        with self.assertRaisesRegex(release.ReleaseEvidenceError, "cannot describe"):
            release.validate_private_pilot(self.private, path)
        value = self.summary()
        value["capability_coverage"]["context_packets"] = 2
        path, _ = self.write_pilot(value)
        with self.assertRaisesRegex(release.ReleaseEvidenceError, "every pilot task"):
            release.validate_private_pilot(self.private, path)

    def test_attestation_subject_mismatch_traversal_and_hardlink_are_rejected(self) -> None:
        path, value = self.write_pilot()
        evidence = self.private / ".opc" / "release-evidence" / "manager_approval.json"
        envelope = json.loads(evidence.read_text(encoding="utf-8"))
        envelope["pilot_id"] = "pilot-ffffffffffff"
        evidence.write_bytes(strict_bytes(envelope))
        value["attestations"]["manager_approval"]["evidence"]["sha256"] = hashlib.sha256(evidence.read_bytes()).hexdigest()
        path.write_bytes(strict_bytes(value))
        with self.assertRaisesRegex(release.ReleaseEvidenceError, "subject or semantics mismatch"):
            release.validate_private_pilot(self.private, path)

        path, value = self.write_pilot(self.summary())
        item = value["attestations"]["manager_approval"]["evidence"]
        item["ref"] = ".opc/release-evidence/../release-evidence/manager_approval.json"
        path.write_bytes(strict_bytes(value))
        with self.assertRaisesRegex(release.ReleaseEvidenceError, "not portable"):
            release.validate_private_pilot(self.private, path)

        path, value = self.write_pilot(self.summary())
        source = self.private / ".opc" / "release-evidence" / "manager_approval.json"
        linked = self.private / ".opc" / "release-evidence" / "manager-hardlink.json"
        try:
            os.link(source, linked)
        except OSError:
            self.skipTest("hard links unavailable")
        value["attestations"]["manager_approval"]["evidence"] = {
            "ref": ".opc/release-evidence/manager-hardlink.json",
            "sha256": hashlib.sha256(linked.read_bytes()).hexdigest(),
        }
        path.write_bytes(strict_bytes(value))
        with self.assertRaisesRegex(release.ReleaseEvidenceError, "Hard-linked|hard-linked"):
            release.validate_private_pilot(self.private, path)
        linked.unlink()

        path, _ = self.write_pilot(self.summary())
        source = self.private / ".opc" / "release-evidence" / "sources" / "manager_approval.json"
        source.write_bytes(strict_bytes({"synthetic_private_source_kind": "replaced"}))
        with self.assertRaisesRegex(release.ReleaseEvidenceError, "SHA-256 mismatch"):
            release.validate_private_pilot(self.private, path)

    def test_private_verdict_output_cannot_escape_or_overwrite(self) -> None:
        value = {"private_pilot_status": "pass"}
        with self.assertRaisesRegex(release.ReleaseEvidenceError, "inside the private root"):
            release._write_private_output(
                self.private, Path(self.temporary.name) / "outside.json", value
            )
        output = self.private / "evidence" / "verdict.json"
        release._write_private_output(self.private, output, value)
        self.assertEqual(value, json.loads(output.read_text(encoding="utf-8")))
        with self.assertRaisesRegex(release.ReleaseEvidenceError, "already exists"):
            release._write_private_output(self.private, output, value)
        interrupted = self.private / "evidence" / "interrupted.json"
        with mock.patch.object(release.os, "write", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                release._write_private_output(self.private, interrupted, value)
        self.assertFalse(interrupted.exists())

    def test_private_root_cannot_overlap_or_alias_the_public_repository(self) -> None:
        candidates = (ROOT, ROOT.parent, ROOT / "evaluation")
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(
                    release.ReleaseEvidenceError, "filesystem-separated"
                ):
                    release._canonical_private_root(candidate)

        alias = self.temporary_canonical / "repository-alias"
        try:
            alias.symlink_to(ROOT, target_is_directory=True)
        except OSError:
            return
        with self.assertRaisesRegex(
            release.ReleaseEvidenceError, "normal directory|link or alias"
        ):
            release._canonical_private_root(alias)

    def test_private_sources_are_cumulatively_revalidated_before_pass(self) -> None:
        path, _ = self.write_pilot()
        source = (
            self.private
            / ".opc"
            / "release-evidence"
            / "sources"
            / "manager_approval.json"
        )
        original = release._regular_private_file
        replaced = False

        def race(*args, **kwargs):
            nonlocal replaced
            raw = original(*args, **kwargs)
            relative = args[1] if len(args) > 1 else kwargs["relative"]
            if relative.endswith("sources/manager_approval.json") and not replaced:
                replaced = True
                source.write_bytes(strict_bytes({"synthetic_private_source_kind": "replaced"}))
            return raw

        with mock.patch.object(release, "_regular_private_file", side_effect=race):
            with self.assertRaisesRegex(
                release.ReleaseEvidenceError, "SHA-256 mismatch|changed after validation"
            ):
                release.validate_private_pilot(self.private, path)

    @unittest.skipUnless(os.name == "nt", "Windows path semantics only")
    def test_windows_case_and_short_aliases_pass_but_reparse_roots_fail(self) -> None:
        path, _ = self.write_pilot()
        case_root = Path(str(self.private).swapcase())
        case_path = Path(str(path).swapcase())
        self.assertTrue(os.path.samefile(case_root, self.private))
        verdict = release.validate_private_pilot(case_root, case_path)
        self.assertEqual("pass", verdict["private_pilot_status"])

        if self.temporary_alias != self.temporary_canonical:
            alias_root = self.temporary_alias / "private-project"
            alias_path = alias_root / ".opc" / "release-evidence" / "pilot.json"
            self.assertTrue(os.path.samefile(alias_root, self.private))
            alias_verdict = release.validate_private_pilot(alias_root, alias_path)
            self.assertEqual("pass", alias_verdict["private_pilot_status"])

        reparse_root = self.temporary_canonical / "private-reparse"
        try:
            reparse_root.symlink_to(self.private, target_is_directory=True)
        except OSError:
            if self.temporary_alias == self.temporary_canonical:
                self.skipTest("neither a short-name alias nor directory symlink is available")
        else:
            reparse_path = reparse_root / ".opc" / "release-evidence" / "pilot.json"
            with self.assertRaisesRegex(release.ReleaseEvidenceError, "normal directory|link or alias"):
                release.validate_private_pilot(reparse_root, reparse_path)

    @unittest.skipIf(os.name == "nt", "POSIX directory-fd transaction only")
    def test_posix_output_removes_owned_file_if_bound_parent_moves_outside(self) -> None:
        output = self.private / "evidence" / "race-verdict.json"
        escaped_parent = self.temporary_canonical / "escaped-evidence"
        original = release._write_all_and_verify
        fd_root = Path("/proc/self/fd")
        before_fds = len(tuple(fd_root.iterdir())) if fd_root.is_dir() else None

        def race(descriptor, raw):
            (self.private / "evidence").rename(escaped_parent)
            return original(descriptor, raw)

        with mock.patch.object(release, "_write_all_and_verify", side_effect=race):
            with self.assertRaisesRegex(
                release.ReleaseEvidenceError, "boundary changed|missing"
            ):
                release._write_private_output(
                    self.private, output, {"private_pilot_status": "pass"}
                )
        self.assertFalse(output.exists())
        self.assertFalse((escaped_parent / output.name).exists())
        escaped_parent.rename(self.private / "evidence")
        if before_fds is not None:
            self.assertEqual(before_fds, len(tuple(fd_root.iterdir())))

    @unittest.skipIf(os.name == "nt", "POSIX directory-fd transaction only")
    def test_posix_output_rejects_whole_root_move_and_removes_escaped_owned_file(self) -> None:
        output = self.private / "evidence" / "root-race-verdict.json"
        escaped_root = self.temporary_canonical / "escaped-private"
        original = release._write_all_and_verify
        fd_root = Path("/proc/self/fd")
        before_fds = len(tuple(fd_root.iterdir())) if fd_root.is_dir() else None

        def race(descriptor, raw):
            self.private.rename(escaped_root)
            return original(descriptor, raw)

        with mock.patch.object(release, "_write_all_and_verify", side_effect=race):
            with self.assertRaisesRegex(
                release.ReleaseEvidenceError, "boundary changed"
            ):
                release._write_private_output(
                    self.private, output, {"private_pilot_status": "pass"}
                )
        self.assertFalse(output.exists())
        self.assertFalse((escaped_root / "evidence" / output.name).exists())
        escaped_root.rename(self.private)
        if before_fds is not None:
            self.assertEqual(before_fds, len(tuple(fd_root.iterdir())))

    @unittest.skipIf(os.name == "nt", "POSIX directory-fd transaction only")
    def test_posix_cleanup_preserves_competitor_that_replaces_owned_inode(self) -> None:
        output = self.private / "evidence" / "competitor-verdict.json"
        competitor = b"competitor-owned\n"
        original = release._write_all_and_verify
        fd_root = Path("/proc/self/fd")
        before_fds = len(tuple(fd_root.iterdir())) if fd_root.is_dir() else None

        def race(descriptor, raw):
            output.unlink()
            output.write_bytes(competitor)
            return original(descriptor, raw)

        with mock.patch.object(release, "_write_all_and_verify", side_effect=race):
            with self.assertRaisesRegex(
                release.ReleaseEvidenceError, "boundary changed|verification failed"
            ):
                release._write_private_output(
                    self.private, output, {"private_pilot_status": "pass"}
                )
        self.assertEqual(competitor, output.read_bytes())
        if before_fds is not None:
            self.assertEqual(before_fds, len(tuple(fd_root.iterdir())))

    @unittest.skipIf(os.name == "nt", "POSIX directory-fd transaction only")
    def test_posix_output_closes_anchor_if_root_open_fails(self) -> None:
        output = self.private / "evidence" / "open-failure-verdict.json"
        original = release.os.open
        calls = 0
        fd_root = Path("/proc/self/fd")
        before_fds = len(tuple(fd_root.iterdir())) if fd_root.is_dir() else None

        def fail_root_open(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated root open failure")
            return original(*args, **kwargs)

        with mock.patch.object(release.os, "open", side_effect=fail_root_open):
            with self.assertRaisesRegex(
                release.ReleaseEvidenceError, "could not be published safely"
            ):
                release._write_private_output(
                    self.private, output, {"private_pilot_status": "pass"}
                )
        self.assertFalse(output.exists())
        if before_fds is not None:
            self.assertEqual(before_fds, len(tuple(fd_root.iterdir())))

    @unittest.skipUnless(os.name == "nt", "Windows directory handle locking only")
    def test_windows_output_holds_parent_chain_and_releases_every_handle(self) -> None:
        output = self.private / "evidence" / "locked-verdict.json"
        displaced = self.private / "evidence-displaced"
        attempted = False
        blocked = False
        original = release._write_all_and_verify

        def race(descriptor, raw):
            nonlocal attempted, blocked
            attempted = True
            try:
                (self.private / "evidence").rename(displaced)
            except OSError:
                blocked = True
            return original(descriptor, raw)

        with mock.patch.object(release, "_write_all_and_verify", side_effect=race):
            release._write_private_output(
                self.private, output, {"private_pilot_status": "pass"}
            )
        self.assertTrue(attempted)
        self.assertTrue(blocked)
        self.assertTrue(output.is_file())
        self.assertFalse(displaced.exists())

        # A successful rename after return proves all native directory handles closed.
        (self.private / "evidence").rename(displaced)
        displaced.rename(self.private / "evidence")

    def test_release_checks_bind_exact_clean_head_private_summary_and_semantics(self) -> None:
        summary_path, _ = self.write_pilot()
        private = release.validate_private_pilot(self.private, summary_path)
        commit = "a" * 40
        checks = {}
        names = {
            "windows_ci", "linux_ci", "repository_validation", "privacy_current_and_history",
            "official_plugin_validator", "all_skill_quick_validators", "independent_release_qa",
            "rollback_evidence",
        }
        for name in names:
            logs = self.private / "evidence" / "logs"
            logs.mkdir(exist_ok=True)
            source_ref = f"evidence/logs/{name}.txt"
            source_path = self.private.joinpath(*Path(source_ref).parts)
            source_path.write_text(f"synthetic {name} gate log\n", encoding="utf-8")
            envelope = {
                "schema_version": release.RELEASE_CHECK_VERSION,
                "contract_version": release.CONTRACT_VERSION,
                "evidence_kind": name,
                "release_commit": commit,
                "private_pilot_sha256": private["private_summary_sha256"],
                "status": "pass",
                "independent_from_implementer": name == "independent_release_qa",
                "source_ref": source_ref,
                "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            }
            evidence = self.private / "evidence" / f"{name}.json"
            evidence.write_bytes(strict_bytes(envelope))
            check = {
                "status": "pass",
                "evidence": {
                    "ref": f"evidence/{name}.json",
                    "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                },
            }
            if name == "independent_release_qa":
                check["independent_from_implementer"] = True
            checks[name] = check
        gates = {
            "schema_version": release.GATES_VERSION,
            "contract_version": release.CONTRACT_VERSION,
            "release_commit": commit,
            "private_pilot_sha256": private["private_summary_sha256"],
            "checks": checks,
        }
        gates_path = self.private / "evidence" / "gates.json"
        gates_path.write_bytes(strict_bytes(gates))
        runs = [
            SimpleNamespace(returncode=0, stdout=commit + "\n"),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=commit + "\n"),
            SimpleNamespace(returncode=0, stdout=""),
        ]
        with mock.patch.object(release.subprocess, "run", side_effect=runs):
            result = release.validate_release_gates(
                self.private, gates_path, private["private_summary_sha256"]
            )
        self.assertEqual(commit, result["release_commit"])

        source_path = self.private / "evidence" / "logs" / "windows_ci.txt"
        source_original = source_path.read_bytes()
        regular = release._regular_private_file
        replaced = False

        def race(*args, **kwargs):
            nonlocal replaced
            raw = regular(*args, **kwargs)
            relative = args[1] if len(args) > 1 else kwargs["relative"]
            if relative == "evidence/logs/windows_ci.txt" and not replaced:
                replaced = True
                source_path.write_text("replaced gate log\n", encoding="utf-8")
            return raw

        runs = [
            SimpleNamespace(returncode=0, stdout=commit + "\n"),
            SimpleNamespace(returncode=0, stdout=""),
        ]
        with mock.patch.object(release.subprocess, "run", side_effect=runs), mock.patch.object(
            release, "_regular_private_file", side_effect=race
        ):
            with self.assertRaisesRegex(
                release.ReleaseEvidenceError, "SHA-256 mismatch|changed after validation"
            ):
                release.validate_release_gates(
                    self.private, gates_path, private["private_summary_sha256"]
                )
        source_path.write_bytes(source_original)

        envelope_path = self.private / "evidence" / "windows_ci.json"
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        envelope["release_commit"] = "b" * 40
        envelope_path.write_bytes(strict_bytes(envelope))
        gates["checks"]["windows_ci"]["evidence"]["sha256"] = hashlib.sha256(envelope_path.read_bytes()).hexdigest()
        gates_path.write_bytes(strict_bytes(gates))
        runs = [
            SimpleNamespace(returncode=0, stdout=commit + "\n"),
            SimpleNamespace(returncode=0, stdout=""),
        ]
        with mock.patch.object(release.subprocess, "run", side_effect=runs):
            with self.assertRaisesRegex(release.ReleaseEvidenceError, "subject or semantics mismatch"):
                release.validate_release_gates(
                    self.private, gates_path, private["private_summary_sha256"]
                )

    def test_published_schemas_are_strict_and_bound_to_runtime_versions(self) -> None:
        private_schema = json.loads(release.PRIVATE_SCHEMA_PATH.read_text(encoding="utf-8"))
        gates_schema = json.loads(release.GATES_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertFalse(private_schema["additionalProperties"])
        self.assertFalse(gates_schema["additionalProperties"])
        self.assertEqual(release.PRIVATE_VERSION, private_schema["properties"]["schema_version"]["const"])
        self.assertEqual(release.GATES_VERSION, gates_schema["properties"]["schema_version"]["const"])
        for definition in private_schema["$defs"].values():
            if definition.get("type") == "object":
                self.assertFalse(definition["additionalProperties"])
        for definition in gates_schema["$defs"].values():
            if definition.get("type") == "object":
                self.assertFalse(definition["additionalProperties"])
        for name in (
            "v0.2-private-evidence-envelope.v1.schema.json",
            "v0.2-release-check-envelope.v1.schema.json",
        ):
            schema = json.loads(
                (ROOT / "evaluation" / "schemas" / name).read_text(encoding="utf-8")
            )
            self.assertFalse(schema["additionalProperties"])
            self.assertIn("source_ref", schema["required"])
            self.assertIn("source_sha256", schema["required"])

    def test_private_ref_schema_and_runtime_syntax_have_bidirectional_parity(self) -> None:
        fields = []
        private_schema = json.loads(release.PRIVATE_SCHEMA_PATH.read_text(encoding="utf-8"))
        gates_schema = json.loads(release.GATES_SCHEMA_PATH.read_text(encoding="utf-8"))
        fields.append((".opc/", private_schema["$defs"]["evidenceRef"]["properties"]["ref"]))
        fields.append(("evidence/", gates_schema["$defs"]["evidenceRef"]["properties"]["ref"]))
        for name, prefix in (
            ("v0.2-private-evidence-envelope.v1.schema.json", ".opc/"),
            ("v0.2-release-check-envelope.v1.schema.json", "evidence/"),
        ):
            schema = json.loads(
                (ROOT / "evaluation" / "schemas" / name).read_text(encoding="utf-8")
            )
            fields.append((prefix, schema["properties"]["source_ref"]))

        for prefix, field in fields:
            def schema_accepts(ref: str) -> bool:
                return len(ref) <= field["maxLength"] and re.fullmatch(field["pattern"], ref) is not None

            def runtime_accepts(ref: str) -> bool:
                return (
                    ref.startswith(prefix)
                    and "\\" not in ref
                    and "//" not in ref
                    and len(ref) <= release.MAX_PRIVATE_REF_LENGTH
                    and re.fullmatch(r"[A-Za-z0-9._/-]+", ref) is not None
                    and all(part not in {"", ".", ".."} for part in Path(ref).parts)
                )

            valid = prefix + "a" * (release.MAX_PRIVATE_REF_LENGTH - len(prefix))
            cases = (
                valid,
                valid + "a",
                prefix + "a/b.json",
                prefix + "../escape.json",
                prefix + "a//b.json",
                prefix + "a\\b.json",
            )
            for ref in cases:
                with self.subTest(prefix=prefix, length=len(ref), ref=ref[-24:]):
                    self.assertEqual(runtime_accepts(ref), schema_accepts(ref))


if __name__ == "__main__":
    unittest.main()
