from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import opc_evolution  # noqa: E402


STAMP = "2026-07-19T01:00:00Z"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(shutil.which("git"), "Git is required")
class CapabilityEvolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.project = root / "private-project"
        self.repo = root / "capability-repo"
        (self.project / ".opc").mkdir(parents=True)
        (self.project / ".opc" / "project.json").write_text(
            json.dumps({"schema_version": 1, "project_id": "project-alpha"}),
            encoding="utf-8",
        )
        self._artifact("evaluation/source.json", {"kind": "synthetic-evaluation"})
        self._artifact("lineage/source.json", {"kind": "synthetic-lineage"})
        self._artifact("approvals/pilot-manager.json", {"decision": "pilot-approved"})
        self._artifact("qa/pilot.json", {"verdict": "pass", "independent": True})
        self._artifact("shadow/pilot.json", {"conclusion": "beneficial"})
        self._artifact("approvals/promotion-manager.json", {"decision": "promote-approved"})
        self._artifact("qa/promotion.json", {"verdict": "pass", "independent": True})
        self._artifact("shadow/promotion.json", {"conclusion": "beneficial"})
        self._artifact("decisions/rollback.json", {"decision": "rollback"})

        (self.repo / "skills" / "demo").mkdir(parents=True)
        self.target = self.repo / "skills" / "demo" / "SKILL.md"
        self.user_similar = self.repo / "skills" / "demo" / ".SKILL.md.opc-backup-user"
        self.target.write_text("---\nname: demo\n---\n\nCurrent behavior.\n", encoding="utf-8")
        self.user_similar.write_text("user-owned similar filename\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "--", "skills/demo/.SKILL.md.opc-backup-user"], check=True, capture_output=True)
        self.base = self._commit("base")
        subprocess.run(["git", "-C", str(self.repo), "switch", "-c", "candidate"], check=True, capture_output=True)
        self.target.write_text("---\nname: demo\n---\n\nCandidate behavior.\n", encoding="utf-8")
        self.candidate = self._commit("candidate")
        self.candidate_hash = hashlib.sha256(
            subprocess.run(
                ["git", "-C", str(self.repo), "show", f"{self.candidate}:skills/demo/SKILL.md"],
                check=True, capture_output=True,
            ).stdout
        ).hexdigest()
        subprocess.run(["git", "-C", str(self.repo), "switch", "main"], check=True, capture_output=True)
        self.current_hash = hashlib.sha256(
            subprocess.run(
                ["git", "-C", str(self.repo), "show", f"{self.base}:skills/demo/SKILL.md"],
                check=True, capture_output=True,
            ).stdout
        ).hexdigest()
        self.metric_hash = hashlib.sha256(
            (ROOT / "evaluation" / "contracts" / "baseline-contract.v1.json").read_bytes()
        ).hexdigest()
        self.proposal = self._proposal()

    def _artifact(self, relative: str, value: dict) -> dict:
        path = self.project / ".opc" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return {"ref": f".opc/{relative}", "sha256": digest(path)}

    def _windows_short_alias_or_same(self, path: Path) -> Path:
        if os.name != "nt":
            return path
        import ctypes

        get_short = ctypes.windll.kernel32.GetShortPathNameW
        source = str(path.resolve(strict=True))
        size = get_short(source, None, 0)
        if size == 0:
            return path
        buffer = ctypes.create_unicode_buffer(size + 1)
        if get_short(source, buffer, len(buffer)) == 0:
            return path
        return Path(buffer.value)

    def _evidence(self, kind: str, relative: str) -> dict:
        path = self.project / ".opc" / relative
        return {"kind": kind, "ref": f".opc/{relative}", "sha256": digest(path)}

    def _commit(self, message: str) -> str:
        subprocess.run(["git", "-C", str(self.repo), "add", "--", "skills/demo/SKILL.md"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=OPC Test", "-c",
             "user.email=opc-test@example.invalid", "commit", "-m", message],
            check=True, capture_output=True,
        )
        return subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True,
            text=True, capture_output=True,
        ).stdout.strip()

    def _proposal(self) -> dict:
        current = {
            "version": "unversioned-v0.1", "source_path": "skills/demo/SKILL.md",
            "source_commit": self.base, "content_sha256": self.current_hash,
        }
        return {
            "schema_version": opc_evolution.PROPOSAL_VERSION,
            "contract_version": opc_evolution.CONTRACT_VERSION,
            "proposal_id": "cap-demo-v1",
            "project_id": "project-alpha",
            "sources": {
                "candidate_refs": ["exp-demo-rule"],
                "feedback_refs": [],
                "evaluation_refs": [self._artifact_ref("evaluation/source.json")],
                "lineage_refs": [self._artifact_ref("lineage/source.json")],
            },
            "capability": {"kind": "skill", "target_path": "skills/demo/SKILL.md"},
            "current_version": current,
            "candidate_version": {
                "version": "v1.0.0-candidate.1", "source_path": "skills/demo/SKILL.md",
                "source_commit": self.candidate, "content_sha256": self.candidate_hash,
            },
            "rollback_target": copy.deepcopy(current),
            "scope": {"kind": "project", "project_id": "project-alpha"},
            "owner": "manager",
            "pilot": {"min_cases": 1, "max_cases": 5, "observation_cases": 1},
            "created_at": STAMP,
        }

    def _artifact_ref(self, relative: str) -> dict:
        path = self.project / ".opc" / relative
        return {"ref": f".opc/{relative}", "sha256": digest(path)}

    def _authorization(self, prefix: str) -> dict:
        return {
            "manager_approval": self._evidence("manager_approval", f"approvals/{prefix}-manager.json"),
            "independent_qa": self._evidence("independent_qa", f"qa/{prefix}.json"),
            "shadow": self._evidence("shadow", f"shadow/{prefix}.json"),
            "recorded_at": STAMP,
        }

    def _metrics(self, *, quality: int = 1, status: str = "completed") -> dict:
        del status
        return {
            "manager_intervention_rate": {"numerator": quality, "denominator": 10},
            "qa_catch_rate": {"numerator": 10 - quality, "denominator": 10},
            "rework_loops_per_task": {"numerator": quality, "denominator": 10},
            "valid_knowledge_reuse_rate": {"numerator": 10 - quality, "denominator": 10},
            "false_recall_rate": {"numerator": quality, "denominator": 10},
            "scope_leakage_acceptances": 0,
            "stale_obsolete_acceptances": 0,
            "privacy_failures": 0,
            "context_tokens_per_task": 100 + quality,
            "latency_ms": 100.0 + quality,
        }

    def _case(self, case_id: str = "case-one", *, control_quality: int = 4,
              candidate_quality: int = 1, candidate_status: str = "completed") -> dict:
        lineage = self._evidence("lineage", "lineage/source.json")
        common = {
            "evaluation_contract": {"version": "opc-evaluation-contract-v1", "sha256": self.metric_hash},
            "knowledge_versions": [{
                "record_id": "exp-approved", "source_path": "approved/exp-approved.json",
                "source_commit": self.base, "content_sha256": "a" * 64,
            }],
            "lineage": lineage,
        }
        return {
            "case_id": case_id,
            "control": {
                **copy.deepcopy(common), "run_id": f"opc-{case_id}-control",
                "execution_status": "completed",
                "capability_version": copy.deepcopy(self.proposal["current_version"]),
                "metrics": self._metrics(quality=control_quality),
            },
            "candidate": {
                **copy.deepcopy(common), "run_id": f"opc-{case_id}-candidate",
                "execution_status": candidate_status,
                "capability_version": copy.deepcopy(self.proposal["candidate_version"]),
                "metrics": self._metrics(quality=candidate_quality),
            },
        }

    def _apply_action(self, revision: int, action: str, payload: dict,
                      now: str = STAMP) -> dict:
        preview = opc_evolution.preview_action(
            self.project, "cap-demo-v1", expected_revision=revision,
            action=action, payload=payload, now=now,
        )
        return opc_evolution.apply_action(
            self.project, "cap-demo-v1", expected_revision=revision,
            action=action, payload=payload, now=now,
            plan_token=preview["plan_token"],
        )["record"]

    def _open(self) -> dict:
        preview = opc_evolution.preview_open(self.project, self.repo, self.proposal)
        self.assertFalse((self.project / ".opc" / "evolution").exists())
        return opc_evolution.open_proposal(
            self.project, self.repo, self.proposal, plan_token=preview["plan_token"]
        )["record"]

    def _evaluated(self, *, candidate_quality: int = 1,
                   candidate_status: str = "completed") -> dict:
        self._open()
        self._apply_action(1, "authorize_pilot", {"authorization": self._authorization("pilot")})
        self._apply_action(2, "record_pilot_case", {
            "case": self._case(candidate_quality=candidate_quality, candidate_status=candidate_status)
        })
        return self._apply_action(3, "evaluate", {"confounders": ["task-difficulty", "tool-variation"]})

    def test_preview_open_is_zero_write_and_open_is_idempotent(self) -> None:
        preview = opc_evolution.preview_open(self.project, self.repo, self.proposal)
        self.assertFalse((self.project / ".opc" / "evolution").exists())
        first = opc_evolution.open_proposal(
            self.project, self.repo, self.proposal, plan_token=preview["plan_token"]
        )
        self.assertFalse(first["idempotent"])
        second_preview = opc_evolution.preview_open(self.project, self.repo, self.proposal)
        second = opc_evolution.open_proposal(
            self.project, self.repo, self.proposal,
            plan_token=second_preview["plan_token"],
        )
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["record"], second["record"])

    def test_beneficial_neutral_harmful_and_unavailable_are_distinct(self) -> None:
        beneficial = opc_evolution.evaluate_cases(
            [self._case(candidate_quality=1)], self.proposal,
            ["task-difficulty"], now=STAMP,
        )
        neutral = opc_evolution.evaluate_cases(
            [self._case(candidate_quality=4)], self.proposal,
            ["task-difficulty"], now=STAMP,
        )
        harmful = opc_evolution.evaluate_cases(
            [self._case(candidate_quality=7)], self.proposal,
            ["task-difficulty"], now=STAMP,
        )
        unavailable = opc_evolution.evaluate_cases(
            [self._case(candidate_quality=1, candidate_status="provider_unavailable")],
            self.proposal, ["provider-variation"], now=STAMP,
        )
        self.assertEqual(beneficial["conclusion"], "beneficial")
        self.assertEqual(neutral["conclusion"], "neutral")
        self.assertEqual(harmful["conclusion"], "harmful")
        self.assertEqual(unavailable["conclusion"], "inconclusive")
        self.assertIn("provider_unavailable", unavailable["blocking_reasons"])
        self.assertEqual(beneficial["claim"], "association/evidence only")

    def test_scope_privacy_missing_evidence_and_failed_pilot_block(self) -> None:
        over_scoped = copy.deepcopy(self.proposal)
        over_scoped["scope"]["project_id"] = "project-other"
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.validate_proposal(over_scoped)
        leaked = self._case(candidate_quality=1)
        leaked["candidate"]["metrics"]["scope_leakage_acceptances"] = 1
        result = opc_evolution.evaluate_cases(
            [leaked], self.proposal, ["task-difficulty"], now=STAMP,
        )
        self.assertEqual(result["conclusion"], "inconclusive")
        self.assertIn("scope_leakage", result["blocking_reasons"])
        privacy = self._case(candidate_quality=1)
        privacy["candidate"]["metrics"]["privacy_failures"] = 1
        privacy_result = opc_evolution.evaluate_cases(
            [privacy], self.proposal, ["task-difficulty"], now=STAMP,
        )
        self.assertEqual(privacy_result["conclusion"], "inconclusive")
        self.assertIn("privacy_failure", privacy_result["blocking_reasons"])
        empty = opc_evolution.evaluate_cases([], self.proposal, ["task-difficulty"], now=STAMP)
        self.assertIn("missing_evidence", empty["blocking_reasons"])
        failed = self._case(candidate_quality=1, candidate_status="failed")
        self.assertEqual(
            opc_evolution.evaluate_cases([failed], self.proposal, ["task-difficulty"], now=STAMP)["conclusion"],
            "inconclusive",
        )
        timed_out = self._case(candidate_quality=1, candidate_status="timeout")
        timeout_result = opc_evolution.evaluate_cases(
            [timed_out], self.proposal, ["tool-variation"], now=STAMP,
        )
        self.assertEqual(timeout_result["conclusion"], "inconclusive")
        self.assertIn("timeout", timeout_result["blocking_reasons"])

    def test_pilot_requires_exact_contract_capability_knowledge_and_lineage(self) -> None:
        case = self._case()
        wrong_contract = copy.deepcopy(case)
        wrong_contract["candidate"]["evaluation_contract"]["sha256"] = "b" * 64
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.validate_pilot_case(wrong_contract, self.proposal)
        wrong_capability = copy.deepcopy(case)
        wrong_capability["candidate"]["capability_version"]["version"] = "v-other"
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.validate_pilot_case(wrong_capability, self.proposal)
        duplicate_knowledge = copy.deepcopy(case)
        duplicate_knowledge["control"]["knowledge_versions"] *= 2
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.validate_pilot_case(duplicate_knowledge, self.proposal)
        wrong_lineage = copy.deepcopy(case)
        wrong_lineage["candidate"]["lineage"] = self._evidence("evaluation", "lineage/source.json")
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.validate_pilot_case(wrong_lineage, self.proposal)
        mismatched = copy.deepcopy(case)
        mismatched["candidate"]["evaluation_contract"]["version"] = "opc-evaluation-contract-v2"
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.validate_pilot_case(mismatched, self.proposal)

    def test_approval_requires_all_three_existing_exact_private_refs(self) -> None:
        self._open()
        incomplete = self._authorization("pilot")
        incomplete.pop("independent_qa")
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.preview_action(
                self.project, "cap-demo-v1", expected_revision=1,
                action="authorize_pilot", payload={"authorization": incomplete}, now=STAMP,
            )
        missing = self._authorization("pilot")
        missing["independent_qa"]["ref"] = ".opc/qa/missing.json"
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.preview_action(
                self.project, "cap-demo-v1", expected_revision=1,
                action="authorize_pilot", payload={"authorization": missing}, now=STAMP,
            )
        stale = self._authorization("pilot")
        stale["shadow"]["sha256"] = "c" * 64
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.preview_action(
                self.project, "cap-demo-v1", expected_revision=1,
                action="authorize_pilot", payload={"authorization": stale}, now=STAMP,
            )

    def test_candidate_commit_must_be_narrow_and_target_allowlisted(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "switch", "candidate"], check=True, capture_output=True)
        (self.repo / "unrelated.txt").write_text("unrelated", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "unrelated.txt"], check=True, capture_output=True)
        unrelated_commit = self._commit("candidate with unrelated path")
        subprocess.run(["git", "-C", str(self.repo), "switch", "main"], check=True, capture_output=True)
        bad = copy.deepcopy(self.proposal)
        bad["candidate_version"]["source_commit"] = unrelated_commit
        bad["candidate_version"]["content_sha256"] = self.candidate_hash
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.preview_open(self.project, self.repo, bad)
        outside = copy.deepcopy(self.proposal)
        outside["capability"] = {"kind": "skill", "target_path": "config.toml"}
        for key in ("current_version", "candidate_version", "rollback_target"):
            outside[key]["source_path"] = "config.toml"
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.validate_proposal(outside)

    def test_promotion_confirm_observe_and_rollback_preserve_history(self) -> None:
        record = self._evaluated()
        self.assertEqual(record["state"], "evaluated")
        auth = self._authorization("promotion")
        preview = opc_evolution.preview_transition(
            self.project, self.repo, "cap-demo-v1", expected_revision=4,
            kind="promotion", now=STAMP, authorization=auth,
        )
        self.assertEqual(self.target.read_text(encoding="utf-8"), "---\nname: demo\n---\n\nCurrent behavior.\n")
        applied = opc_evolution.apply_transition(
            self.project, self.repo, "cap-demo-v1", expected_revision=4,
            kind="promotion", now=STAMP, authorization=auth,
            plan_token=preview["plan_token"],
        )
        self.assertFalse(applied["staged"])
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            check=True, text=True, capture_output=True,
        ).stdout.strip()
        self.assertEqual(status.split(maxsplit=1)[1].replace("\\", "/"), "skills/demo/SKILL.md")
        promoted_commit = self._commit("promote candidate")
        confirm = opc_evolution.preview_confirm(
            self.project, self.repo, "cap-demo-v1", expected_revision=5, now=STAMP,
        )
        promoted = opc_evolution.confirm_transition(
            self.project, self.repo, "cap-demo-v1", expected_revision=5,
            now=STAMP, plan_token=confirm["plan_token"],
        )["record"]
        self.assertEqual(promoted["state"], "promoted")
        self.assertEqual(promoted["active_version"]["source_commit"], promoted_commit)

        observed = self._apply_action(6, "observe", {
            "evidence": self._evidence("outcome", "evaluation/source.json")
        })
        self.assertEqual(observed["state"], "observing")
        rollback_evidence = self._evidence("rollback_decision", "decisions/rollback.json")
        rollback_preview = opc_evolution.preview_transition(
            self.project, self.repo, "cap-demo-v1", expected_revision=7,
            kind="rollback", now=STAMP, rollback_evidence=rollback_evidence,
        )
        opc_evolution.apply_transition(
            self.project, self.repo, "cap-demo-v1", expected_revision=7,
            kind="rollback", now=STAMP, rollback_evidence=rollback_evidence,
            plan_token=rollback_preview["plan_token"],
        )
        rollback_commit = self._commit("rollback candidate")
        rollback_confirm = opc_evolution.preview_confirm(
            self.project, self.repo, "cap-demo-v1", expected_revision=8, now=STAMP,
        )
        rolled_back = opc_evolution.confirm_transition(
            self.project, self.repo, "cap-demo-v1", expected_revision=8,
            now=STAMP, plan_token=rollback_confirm["plan_token"],
        )["record"]
        self.assertEqual(rolled_back["state"], "rolled_back")
        self.assertEqual(rolled_back["active_version"]["source_commit"], rollback_commit)
        self.assertEqual(rolled_back["active_version"]["content_sha256"], self.current_hash)
        self.assertEqual(len(rolled_back["pilot_cases"]), 1)
        self.assertEqual(rolled_back["evaluation"]["conclusion"], "beneficial")
        self.assertTrue(any(item["action"] == "promotion_confirmed" for item in rolled_back["history"]))
        self.assertEqual(self.user_similar.read_text(encoding="utf-8"), "user-owned similar filename\n")

    def test_transition_failure_restores_target_and_private_record(self) -> None:
        self._evaluated()
        auth = self._authorization("promotion")
        preview = opc_evolution.preview_transition(
            self.project, self.repo, "cap-demo-v1", expected_revision=4,
            kind="promotion", now=STAMP, authorization=auth,
        )
        before = self.target.read_bytes()
        original = opc_evolution._atomic_private
        with patch.object(opc_evolution, "_atomic_private", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                opc_evolution.apply_transition(
                    self.project, self.repo, "cap-demo-v1", expected_revision=4,
                    kind="promotion", now=STAMP, authorization=auth,
                    plan_token=preview["plan_token"],
                )
        self.assertEqual(self.target.read_bytes(), before)
        record, _ = opc_evolution._read_record(self.project, "cap-demo-v1")
        self.assertEqual(record["revision"], 4)
        self.assertEqual(record["state"], "evaluated")
        self.assertFalse(list(self.target.parent.glob(".SKILL.md.opc-pending-*")))
        self.assertFalse(list(self.target.parent.glob(".SKILL.md.opc-restore-*")))
        self.assertTrue(self.user_similar.is_file())
        self.assertIsNotNone(original)

    def test_system_exit_restores_without_deleting_similar_user_file(self) -> None:
        self._evaluated()
        auth = self._authorization("promotion")
        preview = opc_evolution.preview_transition(
            self.project, self.repo, "cap-demo-v1", expected_revision=4,
            kind="promotion", now=STAMP, authorization=auth,
        )
        before = self.target.read_bytes()
        with patch.object(opc_evolution, "_atomic_private", side_effect=SystemExit(9)):
            with self.assertRaises(SystemExit):
                opc_evolution.apply_transition(
                    self.project, self.repo, "cap-demo-v1", expected_revision=4,
                    kind="promotion", now=STAMP, authorization=auth,
                    plan_token=preview["plan_token"],
                )
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.user_similar.read_text(encoding="utf-8"), "user-owned similar filename\n")
        self.assertFalse(list(self.target.parent.glob(".SKILL.md.opc-pending-*")))
        self.assertFalse(list(self.target.parent.glob(".SKILL.md.opc-restore-*")))

    def test_restore_failure_is_explicit_and_never_claims_private_transition(self) -> None:
        self._evaluated()
        auth = self._authorization("promotion")
        preview = opc_evolution.preview_transition(
            self.project, self.repo, "cap-demo-v1", expected_revision=4,
            kind="promotion", now=STAMP, authorization=auth,
        )
        original_replace = opc_evolution._ManagedDirectory.replace

        def fail_restore(bound: opc_evolution._ManagedDirectory, source: str, destination: str) -> None:
            if ".opc-restore-" in source:
                raise OSError("synthetic restore failure")
            original_replace(bound, source, destination)

        captured: BaseException | None = None
        with patch.object(opc_evolution, "_atomic_private", side_effect=SystemExit(9)), patch.object(
            opc_evolution._ManagedDirectory, "replace", new=fail_restore
        ):
            with self.assertRaises(OSError) as raised:
                opc_evolution.apply_transition(
                    self.project, self.repo, "cap-demo-v1", expected_revision=4,
                    kind="promotion", now=STAMP, authorization=auth,
                    plan_token=preview["plan_token"],
                )
            captured = raised.exception
        # Restoration failure is visible: private state remains authoritative
        # at evaluated, and the user must resolve the one exact dirty path.
        record, _ = opc_evolution._read_record(self.project, "cap-demo-v1")
        self.assertEqual((record["revision"], record["state"]), (4, "evaluated"))
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            check=True, text=True, capture_output=True,
        ).stdout
        self.assertIn("skills/demo/SKILL.md", status.replace("\\", "/"))
        self.assertTrue(self.user_similar.is_file())
        self.assertFalse(list(self.target.parent.glob(".SKILL.md.opc-restore-*")))
        self.assertNotIn(str(self.project), str(captured))
        self.assertNotIn("Candidate behavior", str(captured))

    def test_preexisting_exact_transaction_names_are_never_deleted(self) -> None:
        before = self.target.read_bytes()
        after = b"---\nname: demo\n---\n\nSynthetic alternate.\n"
        pending_name = ".SKILL.md.opc-pending-" + "a" * 48
        restore_name = ".SKILL.md.opc-restore-" + "b" * 48
        pending = self.target.parent / pending_name
        restore = self.target.parent / restore_name
        pending.write_text("user pending", encoding="utf-8")
        restore.write_text("user restore", encoding="utf-8")
        with opc_evolution._ManagedDirectory(self.target.parent, self.repo) as bound:
            with patch.object(opc_evolution.secrets, "token_hex", return_value="a" * 48):
                with self.assertRaises(FileExistsError):
                    opc_evolution._atomic_target(bound, self.target.name, before, after, bound.verify)
            self.assertEqual(pending.read_text(encoding="utf-8"), "user pending")
            self.assertEqual(self.target.read_bytes(), before)

            with patch.object(opc_evolution.secrets, "token_hex", side_effect=["c" * 48, "b" * 48]):
                rollback, cleanup = opc_evolution._atomic_target(
                    bound, self.target.name, before, after, bound.verify
                )
                with self.assertRaises(FileExistsError):
                    rollback()
                cleanup()
            self.assertEqual(restore.read_text(encoding="utf-8"), "user restore")
            self.assertEqual(self.target.read_bytes(), after)
        # Restore the fixture explicitly; runtime correctly refused to take
        # ownership of the pre-existing restore name.
        self.target.write_bytes(before)

    def test_state_machine_rejects_illegal_jumps_and_neutral_promotion(self) -> None:
        self._open()
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.preview_action(
                self.project, "cap-demo-v1", expected_revision=1,
                action="record_pilot_case", payload={"case": self._case()}, now=STAMP,
            )
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.preview_action(
                self.project, "cap-demo-v1", expected_revision=1,
                action="evaluate", payload={"confounders": ["task-difficulty"]}, now=STAMP,
            )
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.preview_transition(
                self.project, self.repo, "cap-demo-v1", expected_revision=1,
                kind="promotion", now=STAMP, authorization=self._authorization("promotion"),
            )
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.preview_confirm(
                self.project, self.repo, "cap-demo-v1", expected_revision=1, now=STAMP,
            )
        self._apply_action(1, "authorize_pilot", {"authorization": self._authorization("pilot")})
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.preview_action(
                self.project, "cap-demo-v1", expected_revision=2,
                action="authorize_pilot", payload={"authorization": self._authorization("pilot")}, now=STAMP,
            )
        self._apply_action(2, "record_pilot_case", {"case": self._case(candidate_quality=4)})
        neutral = self._apply_action(3, "evaluate", {"confounders": ["task-difficulty"]})
        self.assertEqual(neutral["evaluation"]["conclusion"], "neutral")
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.preview_transition(
                self.project, self.repo, "cap-demo-v1", expected_revision=4,
                kind="promotion", now=STAMP, authorization=self._authorization("promotion"),
            )
        corrupted = copy.deepcopy(neutral)
        corrupted["state"] = "promoted"
        corrupted["history"][-1]["state"] = "promoted"
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.validate_record(corrupted)

    def test_dirty_tree_head_drift_and_stale_cas_fail_closed(self) -> None:
        self._evaluated()
        (self.repo / "unrelated.tmp").write_text("user change", encoding="utf-8")
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.preview_transition(
                self.project, self.repo, "cap-demo-v1", expected_revision=4,
                kind="promotion", now=STAMP, authorization=self._authorization("promotion"),
            )
        (self.repo / "unrelated.tmp").unlink()
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.preview_action(
                self.project, "cap-demo-v1", expected_revision=3,
                action="evaluate", payload={"confounders": ["task-difficulty"]}, now=STAMP,
            )

    def test_v01_migration_preview_is_zero_write_and_deterministic(self) -> None:
        kwargs = dict(
            kind="skill", target_path="skills/demo/SKILL.md",
            project_id="project-alpha", owner="manager", proposal_id="cap-migrate-v1",
            candidate_commit=self.candidate, candidate_version="v1.0.0-candidate.1",
            created_at=STAMP,
        )
        before = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            check=True, text=True, capture_output=True,
        ).stdout
        first = opc_evolution.migration_preview(self.repo, **kwargs)
        second = opc_evolution.migration_preview(self.repo, **kwargs)
        after = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            check=True, text=True, capture_output=True,
        ).stdout
        self.assertEqual(first, second)
        self.assertFalse(first["writes"])
        self.assertEqual(first["proposal"]["current_version"]["version"], "unversioned-v0.1")
        self.assertEqual(before, after)

    def test_contract_schema_runtime_and_renderer_parity(self) -> None:
        contract = json.loads(opc_evolution.CONTRACT_PATH.read_text(encoding="utf-8"))
        proposal_schema = json.loads(opc_evolution.PROPOSAL_SCHEMA_PATH.read_text(encoding="utf-8"))
        record_schema = json.loads(opc_evolution.RECORD_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(contract["contract_version"], opc_evolution.CONTRACT_VERSION)
        self.assertEqual(set(proposal_schema["required"]), opc_evolution.PROPOSAL_KEYS)
        self.assertFalse(proposal_schema["additionalProperties"])
        self.assertFalse(record_schema["additionalProperties"])
        self.assertEqual(set(contract["lifecycle_states"]), opc_evolution.STATES)
        record = self._open()
        report = opc_evolution.render_report(record)
        self.assertIn("association/evidence only", report)
        corrupted = copy.deepcopy(record)
        corrupted["active_version"]["content_sha256"] = "bad"
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.render_report(corrupted)

    def test_git_project_requires_directory_ignore_and_rejects_tracked_content(self) -> None:
        subprocess.run(["git", "init", "-b", "main", str(self.project)], check=True, capture_output=True)
        with self.assertRaises(opc_evolution.EvolutionError):
            opc_evolution.preview_open(self.project, self.repo, self.proposal)
        (self.project / ".gitignore").write_text("/.opc/evolution/\n", encoding="utf-8")
        preview = opc_evolution.preview_open(self.project, self.repo, self.proposal)
        self.assertFalse(preview["idempotent"])

    @unittest.skipUnless(os.name == "nt", "Windows short-path identity test")
    def test_windows_short_directory_aliases_preserve_filesystem_identity(self) -> None:
        project_alias = self._windows_short_alias_or_same(self.project)
        repo_alias = self._windows_short_alias_or_same(self.repo)
        preview = opc_evolution.preview_open(project_alias, repo_alias, self.proposal)
        opened = opc_evolution.open_proposal(
            project_alias, repo_alias, self.proposal, plan_token=preview["plan_token"]
        )
        self.assertEqual(opened["record"]["state"], "candidate")

    def test_bound_evolution_directory_replacement_is_blocked_or_detected(self) -> None:
        self._open()
        directory = self.project / ".opc" / "evolution"
        moved = self.project / ".opc" / "evolution-moved"
        with opc_evolution._EvolutionBinding(self.project) as binding:
            try:
                os.replace(directory, moved)
            except OSError:
                # Windows may deny rename while the directory object handle is open.
                binding.verify()
            else:
                directory.mkdir()
                try:
                    with self.assertRaises(opc_evolution.EvolutionError):
                        binding.verify()
                finally:
                    directory.rmdir()
                    os.replace(moved, directory)
        record, _ = opc_evolution._read_record(self.project, "cap-demo-v1")
        self.assertEqual((record["revision"], record["state"]), (1, "candidate"))

    def test_cli_errors_are_redacted_without_traceback(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "opc_evolution.py"), "show",
             "--project-root", str(self.project), "--proposal-id", "cap-secret"],
            check=False, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("unversioned-v0.1", result.stdout)


if __name__ == "__main__":
    unittest.main()
