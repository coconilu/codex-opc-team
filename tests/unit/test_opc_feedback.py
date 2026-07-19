from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("opc_feedback", SCRIPTS / "opc_feedback.py")
opc_feedback = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(opc_feedback)


STAMP = "2026-07-19T00:00:00Z"
LATER = "2026-07-19T00:01:00Z"
SHA = "a" * 64


class FeedbackFixture:
    def __init__(self, root: Path):
        self.root = root
        opc = root / ".opc"
        opc.mkdir(parents=True)
        self.project = {
            "schema_version": 1,
            "project_id": "project-synthetic",
            "name": "Synthetic",
            "created_at": STAMP,
            "updated_at": STAMP,
        }
        self.run = {
            "schema_version": 1,
            "run_id": "opc-run-synthetic",
            "title": "Synthetic run",
            "project_id": "project-synthetic",
            "status": "completed",
            "active": False,
            "evidence": {},
            "created_at": STAMP,
            "updated_at": STAMP,
        }
        (opc / "project.json").write_text(json.dumps(self.project), encoding="utf-8")
        (opc / "run.json").write_text(json.dumps(self.run), encoding="utf-8")

    def event(
        self,
        event_id: str,
        category: str,
        *,
        recorded_at: str = STAMP,
        outcome_status: str = "not_applicable",
        manager_judgment: str = "not_applicable",
        qa_status: str = "not_applicable",
        summary: str = "Synthetic bounded evidence.",
        candidate_ids: list[str] | None = None,
        metric_refs: list[dict] | None = None,
        artifact_refs: list[str] | None = None,
    ) -> dict:
        return {
            "event_id": event_id,
            "recorded_at": recorded_at,
            "category": category,
            "epistemic_status": category,
            "summary": summary,
            "outcome_status": outcome_status,
            "manager_judgment": manager_judgment,
            "qa_status": qa_status,
            "references": {
                "project_id": self.project["project_id"],
                "run_id": self.run["run_id"],
                "candidate_ids": candidate_ids or [],
                "metric_refs": metric_refs or [],
                "artifact_refs": artifact_refs or [],
            },
        }


class StructuredFeedbackTests(unittest.TestCase):
    def make_fixture(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name) / "private-project"
        root.mkdir()
        return temporary, FeedbackFixture(root)

    def test_existing_run_without_feedback_remains_readable_without_default_fabrication(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        before = (fixture.root / ".opc" / "run.json").read_bytes()

        view = opc_feedback.read_feedback(fixture.root)

        self.assertIsNone(view["structured_feedback"])
        self.assertEqual(before, (fixture.root / ".opc" / "run.json").read_bytes())
        self.assertFalse((fixture.root / ".opc" / "feedback").exists())
        self.assertIn("No structured feedback recorded", opc_feedback.render_report(view))

    def test_cli_show_record_and_report_use_the_same_machine_record(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        event_path = Path(temporary.name) / "synthetic-event.json"
        event = fixture.event(
            "feedback-cli", "manager_judgment", manager_judgment="neutral"
        )
        event_path.write_text(json.dumps(event), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                0,
                opc_feedback.main(
                    [
                        "record",
                        "--project-root",
                        str(fixture.root),
                        "--event-file",
                        str(event_path),
                        "--expected-revision",
                        "0",
                    ]
                ),
            )
        machine = json.loads(output.getvalue())
        self.assertEqual(1, machine["record"]["revision"])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                0,
                opc_feedback.main(["report", "--project-root", str(fixture.root)]),
            )
        self.assertIn("feedback is evaluation input only", output.getvalue().lower())
        self.assertIn("Synthetic bounded evidence.", output.getvalue())

    def test_synthetic_pass_fail_partial_and_unknown_end_to_end(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        cases = [
            ("pass", "confirmed_outcome", "pass"),
            ("fail", "confirmed_outcome", "fail"),
            ("partial", "confirmed_outcome", "partial"),
            ("unknown", "unverified", "unknown"),
        ]
        for revision, (suffix, category, outcome) in enumerate(cases):
            event = fixture.event(
                f"feedback-{suffix}",
                category,
                recorded_at=f"2026-07-19T00:0{revision}:00Z",
                outcome_status=outcome,
                summary=f"Synthetic {suffix} outcome.",
                metric_refs=[
                    {
                        "metric_id": "manager_intervention_rate",
                        "aggregate_ref": "aggregates/pilot-synthetic.json",
                        "aggregate_sha256": SHA,
                        "interpretation": "unknown" if outcome == "unknown" else "supporting",
                    }
                ],
            )
            result = opc_feedback.record_feedback(
                fixture.root,
                event,
                expected_revision=revision,
                now=f"2026-07-19T00:0{revision}:30Z",
            )
            self.assertFalse(result["idempotent"])
        view = opc_feedback.read_feedback(fixture.root)
        report = opc_feedback.render_report(view)
        self.assertEqual(4, view["structured_feedback"]["revision"])
        for status in ("pass", "fail", "partial", "unknown"):
            self.assertIn(f"`{status}`", report)
        self.assertEqual(report, opc_feedback.render_report(view))

    def test_late_outcome_updates_established_historical_run_sidecar(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        unknown = fixture.event(
            "feedback-awaiting-outcome",
            "unverified",
            outcome_status="unknown",
        )
        opc_feedback.record_feedback(fixture.root, unknown, expected_revision=0, now=STAMP)
        newer = dict(fixture.run)
        newer["run_id"] = "opc-run-newer"
        (fixture.root / ".opc" / "run.json").write_text(json.dumps(newer), encoding="utf-8")

        confirmed = fixture.event(
            "feedback-late-pass",
            "confirmed_outcome",
            recorded_at=LATER,
            outcome_status="pass",
        )
        result = opc_feedback.record_feedback(
            fixture.root,
            confirmed,
            expected_revision=1,
            run_id=fixture.run["run_id"],
            now=LATER,
        )
        self.assertEqual(2, result["record"]["revision"])
        self.assertEqual(
            2,
            opc_feedback.read_feedback(fixture.root, fixture.run["run_id"])["structured_feedback"]["revision"],
        )

    def test_arbitrary_historical_run_without_sidecar_fails_closed(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "historical run"):
            opc_feedback.read_feedback(fixture.root, "opc-run-unverifiable")

    def test_all_evidence_classes_remain_distinct_and_non_binary(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        events = [
            fixture.event(
                "feedback-manager",
                "manager_judgment",
                manager_judgment="mixed",
            ),
            fixture.event(
                "feedback-qa",
                "independent_qa_evidence",
                qa_status="partial",
                artifact_refs=["qa/report.json"],
            ),
            fixture.event("feedback-hypothesis", "hypothesis"),
        ]
        for event in events:
            opc_feedback.validate_event(
                event,
                project_id=fixture.project["project_id"],
                run_id=fixture.run["run_id"],
            )

    def test_strict_additional_fields_and_cross_field_contradictions_are_rejected(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        event = fixture.event(
            "feedback-pass", "confirmed_outcome", outcome_status="pass"
        )
        event["unexpected"] = True
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "extra"):
            opc_feedback.validate_event(
                event,
                project_id=fixture.project["project_id"],
                run_id=fixture.run["run_id"],
            )
        event.pop("unexpected")
        event["manager_judgment"] = "accepted"
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "only manager"):
            opc_feedback.validate_event(
                event,
                project_id=fixture.project["project_id"],
                run_id=fixture.run["run_id"],
            )
        qa = fixture.event("feedback-qa", "independent_qa_evidence", qa_status="pass")
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "contradictory"):
            opc_feedback.validate_event(
                qa,
                project_id=fixture.project["project_id"],
                run_id=fixture.run["run_id"],
            )
        refs_extra = fixture.event("feedback-refs-extra", "hypothesis")
        refs_extra["references"]["unexpected"] = []
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "extra"):
            opc_feedback.validate_event(
                refs_extra,
                project_id=fixture.project["project_id"],
                run_id=fixture.run["run_id"],
            )

    def test_text_and_reference_counts_are_bounded(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        too_long = fixture.event(
            "feedback-long", "hypothesis", summary="x" * (opc_feedback.MAX_SUMMARY_LENGTH + 1)
        )
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "1..500"):
            opc_feedback.validate_event(
                too_long,
                project_id=fixture.project["project_id"],
                run_id=fixture.run["run_id"],
            )
        too_many = fixture.event(
            "feedback-many-refs",
            "hypothesis",
            candidate_ids=[f"exp-synthetic-{index}" for index in range(opc_feedback.MAX_REFS + 1)],
        )
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "at most 20"):
            opc_feedback.validate_event(
                too_many,
                project_id=fixture.project["project_id"],
                run_id=fixture.run["run_id"],
            )

    def test_schema_declares_strict_objects_and_non_binary_enums(self):
        schema_path = ROOT / "plugins" / "codex-opc-team" / "assets" / "feedback" / "structured-feedback.v1.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        for definition in ("metricRef", "references", "event"):
            self.assertFalse(schema["$defs"][definition]["additionalProperties"])
        self.assertIn("partial", schema["$defs"]["observedOutcome"]["enum"])
        self.assertIn("unknown", schema["$defs"]["observedOutcome"]["enum"])
        self.assertIn("mixed", schema["$defs"]["managerAcceptance"]["enum"])
        self.assertIn("neutral", schema["$defs"]["managerAcceptance"]["enum"])

    def test_portable_reference_consistency_rejects_paths_urls_runtime_ids_and_uuid(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        base = fixture.event("feedback-hypothesis", "hypothesis")
        forbidden_refs = [
            "../escape.json",
            "https:" + "//example.invalid/value",
            "C:" + "\\private\\value.json",
        ]
        for ref in forbidden_refs:
            with self.subTest(ref=ref):
                event = json.loads(json.dumps(base))
                event["references"]["artifact_refs"] = [ref]
                with self.assertRaises(opc_feedback.FeedbackError):
                    opc_feedback.validate_event(
                        event,
                        project_id=fixture.project["project_id"],
                        run_id=fixture.run["run_id"],
                    )
        event = json.loads(json.dumps(base))
        event["references"]["project_id"] = "other-project"
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "do not match"):
            opc_feedback.validate_event(
                event,
                project_id=fixture.project["project_id"],
                run_id=fixture.run["run_id"],
            )
        runtime_uuid = "12345678" + "-1234-4123-8123-123456789abc"
        event = json.loads(json.dumps(base))
        event["event_id"] = "feedback-" + runtime_uuid
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "UUID"):
            opc_feedback.validate_event(
                event,
                project_id=fixture.project["project_id"],
                run_id=fixture.run["run_id"],
            )

    def test_sensitive_summary_content_is_rejected(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        forbidden = [
            "session" + "_id=value",
            "api" + "_key=value",
            "raw " + "chat payload",
            "hook " + "payload body",
            "C:" + "\\private\\artifact.txt",
            "/" + "home/example/artifact.txt",
            "https:" + "//example.invalid/report",
        ]
        for index, summary in enumerate(forbidden):
            with self.subTest(summary=summary):
                event = fixture.event(f"feedback-sensitive-{index}", "hypothesis", summary=summary)
                with self.assertRaises(opc_feedback.FeedbackError):
                    opc_feedback.validate_event(
                        event,
                        project_id=fixture.project["project_id"],
                        run_id=fixture.run["run_id"],
                    )

    def test_idempotency_stale_revision_and_event_collision_fail_closed(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        event = fixture.event(
            "feedback-pass", "confirmed_outcome", outcome_status="pass"
        )
        first = opc_feedback.record_feedback(fixture.root, event, expected_revision=0, now=STAMP)
        retry = opc_feedback.record_feedback(fixture.root, event, expected_revision=0, now=LATER)
        self.assertFalse(first["idempotent"])
        self.assertTrue(retry["idempotent"])
        self.assertEqual(1, retry["record"]["revision"])
        stale = fixture.event("feedback-hypothesis", "hypothesis", recorded_at=LATER)
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "stale"):
            opc_feedback.record_feedback(fixture.root, stale, expected_revision=0, now=LATER)
        collision = json.loads(json.dumps(event))
        collision["summary"] = "Different bounded evidence."
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "different content"):
            opc_feedback.record_feedback(fixture.root, collision, expected_revision=1, now=LATER)

    def test_preexisting_concurrent_lock_is_preserved_and_writer_fails_closed(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        feedback = fixture.root / ".opc" / "feedback"
        feedback.mkdir()
        lock = feedback / "opc-run-synthetic.json.lock"
        lock.write_text("other-writer", encoding="utf-8")
        event = fixture.event("feedback-hypothesis", "hypothesis")
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "locked"):
            opc_feedback.record_feedback(fixture.root, event, expected_revision=0, now=STAMP)
        self.assertEqual("other-writer", lock.read_text(encoding="utf-8"))

    def test_parent_identity_change_fails_before_feedback_write(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        feedback = fixture.root / ".opc" / "feedback"
        feedback.mkdir()
        token = opc_feedback._directory_token(feedback)
        changed = (token[0], token[1] + 1, token[2], token[3])
        event = fixture.event("feedback-hypothesis", "hypothesis")
        with mock.patch.object(opc_feedback, "_directory_token", side_effect=[token, changed]):
            with self.assertRaisesRegex(opc_feedback.FeedbackError, "TOCTOU"):
                opc_feedback.record_feedback(fixture.root, event, expected_revision=0, now=STAMP)
        self.assertFalse((feedback / "opc-run-synthetic.json").exists())
        self.assertFalse((feedback / "opc-run-synthetic.json.lock").exists())

    def test_atomic_write_failure_leaves_no_partial_record_and_releases_owned_lock(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        event = fixture.event("feedback-hypothesis", "hypothesis")
        with mock.patch.object(opc_feedback, "_atomic_write_feedback", side_effect=OSError("synthetic write failure")):
            with self.assertRaisesRegex(OSError, "synthetic write failure"):
                opc_feedback.record_feedback(fixture.root, event, expected_revision=0, now=STAMP)
        target = fixture.root / ".opc" / "feedback" / "opc-run-synthetic.json"
        self.assertFalse(target.exists())
        self.assertFalse(target.with_suffix(".json.lock").exists())

    def test_preexisting_pending_file_is_preserved_and_write_fails_closed(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        feedback = fixture.root / ".opc" / "feedback"
        feedback.mkdir()
        pending = feedback / "opc-run-synthetic.json.pending"
        pending.write_text("other-operation", encoding="utf-8")
        event = fixture.event("feedback-hypothesis", "hypothesis")
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "pending"):
            opc_feedback.record_feedback(fixture.root, event, expected_revision=0, now=STAMP)
        self.assertEqual("other-operation", pending.read_text(encoding="utf-8"))

    def test_replace_failure_preserves_previous_revision_and_cleans_pending(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        first = fixture.event("feedback-first", "hypothesis")
        opc_feedback.record_feedback(fixture.root, first, expected_revision=0, now=STAMP)
        target = fixture.root / ".opc" / "feedback" / "opc-run-synthetic.json"
        before = target.read_bytes()
        second = fixture.event("feedback-second", "hypothesis", recorded_at=LATER)
        with mock.patch.object(opc_feedback.os, "replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                opc_feedback.record_feedback(
                    fixture.root, second, expected_revision=1, now=LATER
                )
        self.assertEqual(before, target.read_bytes())
        self.assertFalse(target.with_suffix(".json.pending").exists())

    def test_two_concurrent_writers_cannot_lose_an_update(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        barrier = threading.Barrier(3)
        results: list[str] = []

        def write(suffix: str) -> None:
            event = fixture.event(f"feedback-{suffix}", "hypothesis")
            barrier.wait()
            try:
                opc_feedback.record_feedback(fixture.root, event, expected_revision=0, now=STAMP)
                results.append("written")
            except opc_feedback.FeedbackError as exc:
                results.append(str(exc))

        threads = [threading.Thread(target=write, args=(suffix,)) for suffix in ("one", "two")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(1, results.count("written"))
        self.assertEqual(2, len(results))
        self.assertEqual(1, opc_feedback.read_feedback(fixture.root)["structured_feedback"]["revision"])
        self.assertTrue(any(result != "written" for result in results))

    def test_recording_only_mutates_private_feedback_sidecar(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        run_before = (fixture.root / ".opc" / "run.json").read_bytes()
        project_before = (fixture.root / ".opc" / "project.json").read_bytes()
        event = fixture.event(
            "feedback-manager", "manager_judgment", manager_judgment="neutral"
        )
        opc_feedback.record_feedback(fixture.root, event, expected_revision=0, now=STAMP)
        self.assertEqual(run_before, (fixture.root / ".opc" / "run.json").read_bytes())
        self.assertEqual(project_before, (fixture.root / ".opc" / "project.json").read_bytes())
        files = sorted(
            path.relative_to(fixture.root).as_posix()
            for path in fixture.root.rglob("*")
            if path.is_file()
        )
        self.assertEqual(
            [
                ".opc/feedback/opc-run-synthetic.json",
                ".opc/project.json",
                ".opc/run.json",
            ],
            files,
        )

    def test_unsupported_feedback_version_requires_explicit_migration(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        feedback = fixture.root / ".opc" / "feedback"
        feedback.mkdir()
        record = {
            "schema_version": "opc-structured-feedback-v0",
            "contract_version": "opc-structured-feedback-contract-v0",
            "project_ref": fixture.project["project_id"],
            "run_ref": fixture.run["run_id"],
            "revision": 0,
            "created_at": STAMP,
            "updated_at": STAMP,
            "events": [],
        }
        (feedback / "opc-run-synthetic.json").write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "migrate explicitly"):
            opc_feedback.read_feedback(fixture.root)

    def test_strict_json_rejects_nonfinite_numbers(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "event.json"
        path.write_text('{"value": NaN}', encoding="utf-8")
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "non-finite"):
            opc_feedback._strict_json(path)

    def test_timestamp_shape_and_update_time_cannot_move_backward(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        malformed = fixture.event("feedback-malformed-time", "hypothesis", recorded_at="2026-07-19Z")
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "RFC 3339"):
            opc_feedback.validate_event(
                malformed,
                project_id=fixture.project["project_id"],
                run_id=fixture.run["run_id"],
            )
        first = fixture.event("feedback-first", "hypothesis")
        opc_feedback.record_feedback(fixture.root, first, expected_revision=0, now=LATER)
        second = fixture.event("feedback-second", "hypothesis", recorded_at=LATER)
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "move backward"):
            opc_feedback.record_feedback(
                fixture.root,
                second,
                expected_revision=1,
                now="2026-07-19T00:00:30Z",
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support unavailable")
    def test_feedback_symlink_escape_is_rejected(self):
        temporary, fixture = self.make_fixture()
        self.addCleanup(temporary.cleanup)
        outside = Path(temporary.name) / "outside"
        outside.mkdir()
        opc = fixture.root / ".opc"
        try:
            (opc / "feedback").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        event = fixture.event("feedback-hypothesis", "hypothesis")
        with self.assertRaisesRegex(opc_feedback.FeedbackError, "escapes|symlink"):
            opc_feedback.record_feedback(fixture.root, event, expected_revision=0, now=STAMP)


if __name__ == "__main__":
    unittest.main()
