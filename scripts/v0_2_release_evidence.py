#!/usr/bin/env python3
"""Build public v0.2 evidence and fail closed on the private release gate.

Public synthetic evidence is reproducible and safe to commit. A real 3-5 task
pilot and exact-release-commit attestations stay in an approved private project
boundary. Synthetic fixtures and repository templates can never satisfy those
private gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Sequence


ROOT = Path(__file__).resolve().parents[1]
EVALUATION = ROOT / "evaluation"
CONTRACT_PATH = EVALUATION / "contracts" / "v0.2-release-contract.v1.json"
PRIVATE_SCHEMA_PATH = EVALUATION / "schemas" / "v0.2-private-pilot-aggregate.v1.schema.json"
GATES_SCHEMA_PATH = EVALUATION / "schemas" / "v0.2-release-gates.v1.schema.json"
PUBLIC_RESULT_PATH = EVALUATION / "baselines" / "v0.2-public-synthetic-evidence.v1.json"
PUBLIC_REPORT_PATH = EVALUATION / "baselines" / "v0.2-public-synthetic-evidence.v1.md"
BASELINE_RESULT_PATH = EVALUATION / "baselines" / "file-git-no-enhancement.v1.json"
HIERARCHICAL_RESULT_PATH = EVALUATION / "baselines" / "hierarchical-recall-comparison.v1.json"
MAX_JSON_BYTES = 1024 * 1024
CONTRACT_VERSION = "opc-v0.2-release-contract-v1"
PRIVATE_VERSION = "opc-v0.2-private-pilot-aggregate-v1"
GATES_VERSION = "opc-v0.2-release-gates-v1"
PILOT_EVIDENCE_VERSION = "opc-v0.2-private-evidence-envelope-v1"
RELEASE_CHECK_VERSION = "opc-v0.2-release-check-envelope-v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
MAX_PRIVATE_REF_LENGTH = 240

TARGETED_TESTS = {
    "context_packet": (
        "tests.unit.test_opc_hierarchical.HierarchicalRecallTests."
        "test_hierarchical_packet_has_canonical_l2_and_trace_has_no_body"
    ),
    "structured_feedback": (
        "tests.unit.test_opc_feedback.StructuredFeedbackTests."
        "test_synthetic_pass_fail_partial_and_unknown_end_to_end"
    ),
    "shadow_evaluation": (
        "tests.unit.test_opc_shadow.ShadowEvaluationTests."
        "test_beneficial_candidate_is_only_recommended_for_separate_curation"
    ),
    "conflict_governance": (
        "tests.unit.test_opc_governance.KnowledgeGovernanceTests."
        "test_unresolved_conflict_withholds_both_bodies_and_preserves_citations"
    ),
    "knowledge_lineage": (
        "tests.unit.test_opc_lineage.KnowledgeLineageTests."
        "test_recalled_but_unused_and_all_terminal_states_across_roles_steps"
    ),
    "capability_evolution": (
        "tests.unit.test_opc_evolution.CapabilityEvolutionTests."
        "test_promotion_confirm_observe_and_rollback_preserve_history"
    ),
    "provider_degradation": (
        "tests.unit.test_opc_hierarchical.HierarchicalRecallTests."
        "test_provider_failure_timeout_and_disagreement_do_not_block"
    ),
    "provider_delete_rebuild": (
        "tests.unit.test_opc_hierarchical.HierarchicalRecallTests."
        "test_delete_and_rebuild_require_exact_tokens"
    ),
}

DELIVERY_ARTIFACTS = {
    "2": "docs/installation-and-distribution.md",
    "3": "docs/installed-lifecycle-acceptance.md",
    "4": "evaluation/contracts/baseline-contract.v1.json",
    "5": "plugins/codex-opc-team/assets/feedback/structured-feedback-contract.v1.json",
    "6": "plugins/codex-opc-team/assets/evaluation/shadow-evaluation-contract.v1.json",
    "7": "plugins/codex-opc-team/assets/knowledge/knowledge-governance-contract.v1.json",
    "1": "plugins/codex-opc-team/assets/context/hierarchical-context-contract.v1.json",
    "8": "plugins/codex-opc-team/assets/lineage/knowledge-lineage-contract.v1.json",
    "9": "plugins/codex-opc-team/assets/evolution/capability-evolution-contract.v1.json",
}


class ReleaseEvidenceError(ValueError):
    pass


class _PrivateReadToken(NamedTuple):
    root: Path
    relative: str
    prefix: str
    sha256: str
    snapshot: tuple[int, int, int, int, int, int]
    parent_tokens: tuple[tuple[int, int], ...]


def _pairs_no_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ReleaseEvidenceError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ReleaseEvidenceError(f"non-finite JSON number is forbidden: {value}")


def _read_json(path: Path, *, limit: int = MAX_JSON_BYTES) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReleaseEvidenceError(f"cannot read required JSON artifact: {path.name}") from exc
    if not raw or len(raw) > limit:
        raise ReleaseEvidenceError(f"JSON artifact size is invalid: {path.name}")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseEvidenceError(f"invalid strict JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise ReleaseEvidenceError(f"JSON artifact must be an object: {path.name}")
    return value, raw


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReleaseEvidenceError("result is not strict JSON") from exc


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_sha(relative: str) -> str:
    path = ROOT / relative
    if not path.is_file():
        raise ReleaseEvidenceError(f"missing delivery artifact: {relative}")
    return _sha(path.read_bytes())


def _keys(value: Any, required: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseEvidenceError(f"{label} must be an object")
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing:
        raise ReleaseEvidenceError(f"{label} missing fields: {', '.join(missing)}")
    if extra:
        raise ReleaseEvidenceError(f"{label} has unsupported fields: {', '.join(extra)}")
    return value


def _integer(value: Any, label: str, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReleaseEvidenceError(f"{label} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ReleaseEvidenceError(f"{label} must be <= {maximum}")
    return value


def _run(command: Sequence[str]) -> None:
    completed = subprocess.run(
        list(command), cwd=ROOT, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False,
    )
    if completed.returncode:
        tail = "\n".join(completed.stdout.splitlines()[-12:])
        raise ReleaseEvidenceError(f"public evidence command failed:\n{tail}")


def _load_contract() -> tuple[dict[str, Any], bytes]:
    contract, raw = _read_json(CONTRACT_PATH)
    if contract.get("contract_version") != CONTRACT_VERSION:
        raise ReleaseEvidenceError("v0.2 release contract version drifted")
    if contract.get("authority") != "file-git-only":
        raise ReleaseEvidenceError("File/Git authority drifted")
    status = contract.get("status_policy") or {}
    if status.get("missing_private_pilot") != "blocked" or not status.get(
        "synthetic_cannot_substitute_for_private"
    ):
        raise ReleaseEvidenceError("private-pilot fail-closed policy drifted")
    claims = contract.get("claims") or {}
    if any(
        claims.get(key) is not False
        for key in (
            "causal_claim_allowed",
            "generalization_claim_allowed",
            "autonomous_self_improvement_claim_allowed",
        )
    ) or claims.get("required_qualifier") != "association/evidence only":
        raise ReleaseEvidenceError("release non-claim boundary drifted")
    return contract, raw


def build_public_evidence(*, execute: bool = True) -> dict[str, Any]:
    contract, contract_raw = _load_contract()
    if execute:
        _run([sys.executable, "scripts/evaluation_baseline.py", "verify"])
        _run([sys.executable, "scripts/hierarchical_evaluation.py", "verify"])
        _run([sys.executable, "-m", "unittest", *TARGETED_TESTS.values(), "-v"])
    baseline, baseline_raw = _read_json(BASELINE_RESULT_PATH)
    hierarchical, hierarchical_raw = _read_json(HIERARCHICAL_RESULT_PATH)
    if baseline.get("overall_safety_status") != "pass":
        raise ReleaseEvidenceError("File/Git synthetic safety baseline is not PASS")
    safety = (hierarchical.get("aggregate") or {}).get("safety") or {}
    if safety != {"scope_leakage_acceptances": 0, "stale_obsolete_acceptances": 0}:
        raise ReleaseEvidenceError("hierarchical synthetic safety gates are not zero")
    if hierarchical.get("comparison_status") != "superior":
        raise ReleaseEvidenceError("hierarchical synthetic comparison is not superior")

    aggregate = hierarchical["aggregate"]
    return {
        "schema_version": "opc-v0.2-public-release-evidence-v1",
        "contract_version": CONTRACT_VERSION,
        "contract_sha256": _sha(contract_raw),
        "mode": "public-synthetic",
        "delivery_artifacts": [
            {"issue": int(issue), "path": path, "sha256": _file_sha(path)}
            for issue, path in DELIVERY_ARTIFACTS.items()
        ],
        "executed_scenarios": [
            {"scenario": name, "test_id": test_id, "status": "pass"}
            for name, test_id in TARGETED_TESTS.items()
        ],
        "versioned_results": {
            "file_git_baseline": {
                "sha256": _sha(baseline_raw),
                "task_count": baseline["task_count"],
                "scope_leakage_acceptances": baseline["safety_gates"]["scope_leakage_acceptances"]["value"],
                "stale_obsolete_acceptances": baseline["safety_gates"]["stale_obsolete_acceptances"]["value"],
            },
            "hierarchical_context": {
                "sha256": _sha(hierarchical_raw),
                "query_count": len(hierarchical["cases"]),
                "flat_precision_at_5": aggregate["flat"]["support_precision_at_5"],
                "treatment_precision_at_5": aggregate["hierarchical"]["support_precision_at_5"],
                "flat_canonical_recall_at_5": aggregate["flat"]["canonical_leaf_recall_at_5"],
                "treatment_canonical_recall_at_5": aggregate["hierarchical"]["canonical_leaf_recall_at_5"],
                "flat_median_tokens": aggregate["flat"]["injected_tokens_median"],
                "treatment_median_tokens": aggregate["hierarchical"]["injected_tokens_median"],
                "latency_artifact_sha256": hierarchical["latency_sha256"],
                "scope_leakage_acceptances": safety["scope_leakage_acceptances"],
                "stale_obsolete_acceptances": safety["stale_obsolete_acceptances"],
            },
        },
        "authority_and_fallback": {
            "file_git_authoritative": True,
            "optional_provider_disabled_core_pass": True,
            "derived_index_delete_rebuild_pass": True,
        },
        "public_evidence_status": "pass",
        "release_status": "blocked",
        "release_blockers": [
            "representative_private_3_to_5_task_pilot_required",
            "exact_release_commit_gate_attestation_required",
        ],
        "measured_scope": "public synthetic fixtures only",
        "inference": "the implemented contracts interoperate under the named synthetic tests",
        "known_limitations": [
            "no representative private pilot result is committed or implied",
            "wall-clock latency is environment-dependent",
            "one synthetic suite is not statistically general",
        ],
        "non_claims": [
            "no causal attribution",
            "no autonomous self-improvement",
            "no generalization beyond the fixtures",
            "association/evidence only",
        ],
    }


def _public_report(result: Mapping[str, Any]) -> bytes:
    ctx = result["versioned_results"]["hierarchical_context"]
    base = result["versioned_results"]["file_git_baseline"]
    lines = [
        "# v0.2 public synthetic release evidence",
        "",
        f"- Public evidence: **{str(result['public_evidence_status']).upper()}**",
        f"- Release status: **{str(result['release_status']).upper()}**",
        f"- Contract: `{result['contract_version']}`",
        f"- Contract SHA-256: `{result['contract_sha256']}`",
        "",
        "## Executed synthetic scenarios",
        "",
        "| Scenario | Result |",
        "|---|---|",
    ]
    lines.extend(f"| `{item['scenario']}` | {item['status']} |" for item in result["executed_scenarios"])
    lines.extend(
        [
            "",
            "## Measured public fixture results",
            "",
            "| Measure | Control/flat | Treatment |",
            "|---|---:|---:|",
            f"| File/Git fixture tasks | {base['task_count']} | n/a |",
            f"| Context support precision@5 | {ctx['flat_precision_at_5']} | {ctx['treatment_precision_at_5']} |",
            f"| Canonical leaf recall@5 | {ctx['flat_canonical_recall_at_5']} | {ctx['treatment_canonical_recall_at_5']} |",
            f"| Median injected tokens | {ctx['flat_median_tokens']} | {ctx['treatment_median_tokens']} |",
            f"| Scope leakage acceptance | {base['scope_leakage_acceptances']} | {ctx['scope_leakage_acceptances']} |",
            f"| Stale/obsolete acceptance | {base['stale_obsolete_acceptances']} | {ctx['stale_obsolete_acceptances']} |",
            "",
            "## Release blockers",
            "",
        ]
    )
    lines.extend(f"- `{item}`" for item in result["release_blockers"])
    lines.extend(
        [
            "",
            "> These are public synthetic measurements only. They do not replace the real private 3–5 task pilot, do not prove causality, and do not establish statistical generality. association/evidence only.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def write_public(output: Path, report: Path) -> None:
    result = build_public_evidence(execute=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_json_bytes(result))
    report.write_bytes(_public_report(result))


def verify_public() -> None:
    result = build_public_evidence(execute=True)
    expected_json = _json_bytes(result)
    expected_report = _public_report(result)
    if not PUBLIC_RESULT_PATH.is_file() or PUBLIC_RESULT_PATH.read_bytes() != expected_json:
        raise ReleaseEvidenceError("committed v0.2 public evidence JSON drifted; regenerate it")
    if not PUBLIC_REPORT_PATH.is_file() or PUBLIC_REPORT_PATH.read_bytes() != expected_report:
        raise ReleaseEvidenceError("committed v0.2 public evidence report drifted; regenerate it")


def _canonical_private_root(root: Path) -> tuple[Path, Path]:
    root_absolute = Path(os.path.abspath(root))
    try:
        before = root_absolute.lstat()
        resolved = root_absolute.resolve(strict=True)
        canonical = resolved.lstat()
    except OSError as exc:
        raise ReleaseEvidenceError("private root is missing") from exc
    same_identity = _file_identity(before) == _file_identity(canonical)
    if (
        not stat.S_ISDIR(before.st_mode)
        or _is_reparse(before)
        or not same_identity
        or not os.path.samefile(root_absolute, resolved)
    ):
        raise ReleaseEvidenceError("private root must be a normal directory")
    # Windows path equality already ignores case, but the explicit normcase
    # comparison documents the only lexical normalization accepted here.
    # Short-name, junction, symlink, and other alias spellings resolve to a
    # different normalized path and therefore remain fail-closed.
    lexical = os.path.normcase(os.path.normpath(str(root_absolute)))
    canonical_lexical = os.path.normcase(os.path.normpath(str(resolved)))
    if lexical != canonical_lexical:
        raise ReleaseEvidenceError("private root must not be a link or alias")
    repository = ROOT.resolve(strict=True)
    try:
        private_in_repository = resolved == repository or resolved.is_relative_to(repository)
        repository_in_private = repository.is_relative_to(resolved)
    except (OSError, ValueError):
        private_in_repository = repository_in_private = True
    if private_in_repository or repository_in_private or os.path.samefile(resolved, repository):
        raise ReleaseEvidenceError(
            "private root must be filesystem-separated from the public repository"
        )
    return root_absolute, resolved


def _regular_private_file(
    root: Path,
    relative: str,
    expected_sha: str | None,
    *,
    prefix: str,
    ledger: list[_PrivateReadToken] | None = None,
) -> bytes:
    if expected_sha is not None and not HEX64.fullmatch(expected_sha):
        raise ReleaseEvidenceError("private evidence SHA-256 is invalid")
    if (
        not isinstance(relative, str)
        or not relative.startswith(prefix)
        or "\\" in relative
        or "//" in relative
        or len(relative) > MAX_PRIVATE_REF_LENGTH
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", relative)
    ):
        raise ReleaseEvidenceError("private evidence ref is not portable")
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ReleaseEvidenceError("private evidence ref is not portable")
    root_absolute, resolved_root = _canonical_private_root(root)
    candidate = root_absolute.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
        parent_tokens = _directory_chain_tokens(root_absolute, parts[:-1])
        before = candidate.lstat()
    except (OSError, ValueError) as exc:
        raise ReleaseEvidenceError("private evidence ref is missing or escapes its root") from exc
    canonical_candidate = resolved_root.joinpath(*parts)
    if resolved != canonical_candidate or not stat.S_ISREG(before.st_mode) or _is_reparse(before):
        raise ReleaseEvidenceError("private evidence ref must be a regular non-link file")
    if getattr(before, "st_nlink", 1) != 1:
        raise ReleaseEvidenceError("hard-linked private evidence is rejected")
    if before.st_size <= 0 or before.st_size > MAX_JSON_BYTES:
        raise ReleaseEvidenceError("private evidence file size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ReleaseEvidenceError("private evidence could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(before):
            raise ReleaseEvidenceError("private evidence changed before open")
        chunks: list[bytes] = []
        remaining = MAX_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after_fd = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = candidate.lstat()
        after_tokens = _directory_chain_tokens(root_absolute, parts[:-1])
    except OSError as exc:
        raise ReleaseEvidenceError("private evidence changed during read") from exc
    if (
        not raw
        or len(raw) > MAX_JSON_BYTES
        or _file_snapshot(opened) != _file_snapshot(after_fd)
        or _file_identity(before) != _file_identity(after_path)
        or parent_tokens != after_tokens
        or candidate.resolve(strict=True) != canonical_candidate
    ):
        raise ReleaseEvidenceError("private evidence changed during bound read")
    if expected_sha is not None and _sha(raw) != expected_sha:
        raise ReleaseEvidenceError("private evidence SHA-256 mismatch")
    if ledger is not None:
        ledger.append(
            _PrivateReadToken(
                root=root_absolute,
                relative=relative,
                prefix=prefix,
                sha256=_sha(raw),
                snapshot=_file_snapshot(after_fd),
                parent_tokens=after_tokens,
            )
        )
    return raw


def _revalidate_private_reads(tokens: Sequence[_PrivateReadToken]) -> None:
    for token in tokens:
        replay: list[_PrivateReadToken] = []
        _regular_private_file(
            token.root,
            token.relative,
            token.sha256,
            prefix=token.prefix,
            ledger=replay,
        )
        if len(replay) != 1 or (
            replay[0].snapshot != token.snapshot
            or replay[0].parent_tokens != token.parent_tokens
        ):
            raise ReleaseEvidenceError("private evidence changed after validation")


def _is_reparse(info: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & flag)


def _file_identity(info: os.stat_result) -> tuple[int, int]:
    return int(info.st_dev), int(info.st_ino)


def _file_snapshot(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(info.st_dev), int(info.st_ino), int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
        int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))),
        int(getattr(info, "st_nlink", 1)),
    )


def _directory_chain_tokens(root: Path, parent_parts: Sequence[str]) -> tuple[tuple[int, int], ...]:
    tokens: list[tuple[int, int]] = []
    current = root
    for part in (None, *parent_parts):
        if part is not None:
            current = current / part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
            raise ReleaseEvidenceError("private evidence parent is not a normal directory")
        tokens.append(_file_identity(info))
    return tuple(tokens)


def _private_input_bytes(
    root: Path,
    path: Path,
    label: str,
    *,
    ledger: list[_PrivateReadToken] | None = None,
) -> bytes:
    try:
        root_absolute = Path(os.path.abspath(root))
        absolute = Path(os.path.abspath(path))
        relative = absolute.relative_to(root_absolute).as_posix()
    except (OSError, ValueError) as exc:
        raise ReleaseEvidenceError(f"{label} must be inside the private root") from exc
    return _regular_private_file(root_absolute, relative, None, prefix="", ledger=ledger)


def _write_all_and_verify(descriptor: int, raw: bytes) -> os.stat_result:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise ReleaseEvidenceError("private verdict output write failed")
        view = view[written:]
    os.fsync(descriptor)
    info = os.fstat(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)
    verified = b""
    while len(verified) < len(raw):
        chunk = os.read(descriptor, len(raw) - len(verified))
        if not chunk:
            break
        verified += chunk
    if (
        not stat.S_ISREG(info.st_mode)
        or getattr(info, "st_nlink", 1) != 1
        or info.st_size != len(raw)
        or verified != raw
    ):
        raise ReleaseEvidenceError("private verdict output verification failed")
    return info


def _write_private_output_posix(
    root: Path,
    relative: Path,
    raw: bytes,
    parent_tokens: tuple[tuple[int, int], ...],
    pre_publish: Callable[[], None] | None,
) -> None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    parent_fd = os.open(root, directory_flags)
    descriptor = -1
    created = False
    try:
        if _file_identity(os.fstat(parent_fd)) != parent_tokens[0]:
            raise ReleaseEvidenceError("private verdict root changed before binding")
        for index, part in enumerate(relative.parts[:-1], start=1):
            child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
            if _file_identity(os.fstat(parent_fd)) != parent_tokens[index]:
                raise ReleaseEvidenceError("private verdict parent changed before binding")
        if pre_publish is not None:
            pre_publish()
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(relative.name, flags, 0o600, dir_fd=parent_fd)
        created = True
        created_identity = _file_identity(os.fstat(descriptor))
        _write_all_and_verify(descriptor, raw)
        after = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _file_identity(after) != created_identity
            or _file_identity(os.fstat(parent_fd)) != parent_tokens[-1]
        ):
            raise ReleaseEvidenceError("private verdict output boundary changed during write")
        os.close(descriptor)
        descriptor = -1
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        if created:
            try:
                os.unlink(relative.name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _write_private_output_windows(
    root: Path,
    absolute: Path,
    relative: Path,
    raw: bytes,
    pre_publish: Callable[[], None] | None,
) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    final_path = kernel32.GetFinalPathNameByHandleW
    final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_path.restype = wintypes.DWORD
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_info.restype = wintypes.BOOL

    invalid = wintypes.HANDLE(-1).value
    read_attributes = 0x0080
    generic_read = 0x80000000
    generic_write = 0x40000000
    delete = 0x00010000
    share_read_write = 0x00000001 | 0x00000002  # deliberately excludes FILE_SHARE_DELETE
    open_existing = 3
    create_new = 1
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    file_attribute_normal = 0x00000080

    def open_directory(path: Path) -> int:
        handle = create_file(
            str(path), read_attributes, share_read_write, None, open_existing,
            backup_semantics | open_reparse_point, None,
        )
        if handle == invalid:
            raise OSError(ctypes.get_last_error(), "cannot bind private verdict directory")
        buffer = ctypes.create_unicode_buffer(32768)
        length = final_path(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            close_handle(handle)
            raise OSError(ctypes.get_last_error(), "cannot resolve bound private verdict directory")
        bound = buffer.value
        if bound.startswith("\\\\?\\UNC\\"):
            bound = "\\\\" + bound[8:]
        elif bound.startswith("\\\\?\\"):
            bound = bound[4:]
        expected = str(path.resolve(strict=True))
        if os.path.normcase(os.path.normpath(bound)) != os.path.normcase(os.path.normpath(expected)):
            close_handle(handle)
            raise ReleaseEvidenceError("private verdict directory is a link or alias")
        return int(handle)

    directory_handles: list[int] = []
    descriptor = -1
    file_handle: int | None = None
    try:
        current = root
        directory_handles.append(open_directory(current))
        for part in relative.parts[:-1]:
            current = current / part
            directory_handles.append(open_directory(current))
        if pre_publish is not None:
            pre_publish()
        handle = create_file(
            str(absolute), generic_read | generic_write | delete, 0, None, create_new,
            file_attribute_normal | open_reparse_point, None,
        )
        if handle == invalid:
            error = ctypes.get_last_error()
            if error in {80, 183}:
                raise ReleaseEvidenceError("private verdict output already exists; choose a new path")
            raise OSError(error, "cannot create private verdict output")
        file_handle = int(handle)
        descriptor = msvcrt.open_osfhandle(file_handle, os.O_RDWR | getattr(os, "O_BINARY", 0))
        _write_all_and_verify(descriptor, raw)
        os.close(descriptor)
        descriptor = -1
        file_handle = None
    except BaseException:
        if file_handle is not None:
            class FileDispositionInfo(ctypes.Structure):
                _fields_ = [("DeleteFile", wintypes.BOOL)]

            disposition = FileDispositionInfo(True)
            set_info(file_handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition))
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
            file_handle = None
        elif file_handle is not None:
            close_handle(file_handle)
            file_handle = None
        raise
    finally:
        for handle in reversed(directory_handles):
            close_handle(handle)


def _write_private_output(
    root: Path,
    path: Path,
    value: Mapping[str, Any],
    *,
    pre_publish: Callable[[], None] | None = None,
) -> None:
    root_absolute, _ = _canonical_private_root(root)
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise ReleaseEvidenceError("private verdict output must stay inside the private root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ReleaseEvidenceError("private verdict output path is invalid")
    if absolute.exists() or absolute.is_symlink():
        raise ReleaseEvidenceError("private verdict output already exists; choose a new path")
    try:
        parent_tokens = _directory_chain_tokens(root_absolute, relative.parts[:-1])
    except OSError as exc:
        raise ReleaseEvidenceError("private verdict parent must already exist") from exc
    raw = _json_bytes(value)
    try:
        if os.name == "nt":
            _write_private_output_windows(
                root_absolute, absolute, relative, raw, pre_publish
            )
        else:
            _write_private_output_posix(
                root_absolute, relative, raw, parent_tokens, pre_publish
            )
    except BaseException as exc:
        if isinstance(exc, ReleaseEvidenceError):
            raise
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise ReleaseEvidenceError("private verdict output could not be published safely") from exc


def _core_sha256(summary: Mapping[str, Any]) -> str:
    core = {key: value for key, value in summary.items() if key != "attestations"}
    return _sha(_json_bytes(core))


def _evidence_envelope(
    raw: bytes,
    *,
    kind: str,
    pilot_id: str,
    pilot_core_sha256: str,
    task_count: int,
    decision: str,
    safety: str,
    independent: bool,
) -> tuple[str, str]:
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseEvidenceError(f"{kind} evidence is not a strict JSON envelope") from exc
    expected = {
        "schema_version": PILOT_EVIDENCE_VERSION,
        "contract_version": CONTRACT_VERSION,
        "evidence_kind": kind,
        "pilot_id": pilot_id,
        "pilot_core_sha256": pilot_core_sha256,
        "task_count": task_count,
        "decision": decision,
        "safety": safety,
        "independent_from_implementer": independent,
    }
    value = _keys(
        value, set(expected) | {"source_ref", "source_sha256"}, f"{kind} evidence envelope"
    )
    if any(value[key] != expected_value for key, expected_value in expected.items()):
        raise ReleaseEvidenceError(f"{kind} evidence envelope subject or semantics mismatch")
    source_ref = value["source_ref"]
    source_sha256 = value["source_sha256"]
    if not isinstance(source_ref, str) or not source_ref.startswith(".opc/"):
        raise ReleaseEvidenceError(f"{kind} source evidence must stay in .opc")
    if not isinstance(source_sha256, str) or not HEX64.fullmatch(source_sha256):
        raise ReleaseEvidenceError(f"{kind} source evidence SHA-256 is invalid")
    return source_ref, source_sha256


def _validate_arm(arm: Any, *, pilot_id: str, task_count: int, label: str) -> dict[str, Any]:
    arm = _keys(arm, {"counts", "context_tokens", "latency_ms"}, label)
    counts = _keys(
        arm["counts"],
        {
            "manager_interventions", "eligible_manager_decisions", "known_defects",
            "qa_caught_defects", "rework_loops", "valid_reuse_opportunities",
            "valid_reuses", "accepted_recalls", "false_recall_acceptances",
            "scope_leakage_acceptances", "stale_obsolete_acceptances", "privacy_failures",
        },
        f"{label}.counts",
    )
    if any(counts[key] != 0 for key in ("scope_leakage_acceptances", "stale_obsolete_acceptances", "privacy_failures")):
        raise ReleaseEvidenceError(f"{label} safety counts must all be zero")
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import evaluation_baseline as baseline

    summary_counts = dict(counts)
    summary_counts.pop("privacy_failures")
    try:
        result = baseline._score_private_summary(
            {
                "schema_version": "opc-private-pilot-summary-v1",
                "contract_version": "opc-evaluation-contract-v1",
                "pilot_id": pilot_id,
                "mode": "private-aggregate",
                "task_count": task_count,
                "counts": summary_counts,
                "context_tokens": arm["context_tokens"],
                "latency_ms": arm["latency_ms"],
            }
        )
    except baseline.EvaluationError as exc:
        raise ReleaseEvidenceError(f"{label} aggregate is invalid: {exc}") from exc
    return result


def validate_private_pilot(
    private_root: Path,
    summary_path: Path,
    *,
    _ledger_out: list[_PrivateReadToken] | None = None,
) -> dict[str, Any]:
    contract, contract_raw = _load_contract()
    del contract
    ledger: list[_PrivateReadToken] = []
    raw = _private_input_bytes(
        private_root, summary_path, "private pilot summary", ledger=ledger
    )
    try:
        summary = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseEvidenceError("private pilot summary is not strict JSON") from exc
    if isinstance(summary, dict) and (
        "$instructions" in summary or summary.get("schema_version") == "TEMPLATE-NOT-EVIDENCE"
    ):
        raise ReleaseEvidenceError("public template cannot satisfy the private pilot gate")
    required = {
        "schema_version", "contract_version", "pilot_id", "evidence_class", "task_count",
        "task_selection", "arms", "capability_coverage", "provider_fallback",
        "attestations", "confounders",
    }
    summary = _keys(summary, required, "private pilot")
    if summary["schema_version"] != PRIVATE_VERSION:
        raise ReleaseEvidenceError("template or unsupported private pilot schema cannot satisfy the gate")
    if summary["contract_version"] != CONTRACT_VERSION:
        raise ReleaseEvidenceError("private pilot contract version drifted")
    if summary["evidence_class"] != "representative-private-pilot":
        raise ReleaseEvidenceError("synthetic/example evidence cannot satisfy the private gate")
    pilot_id = summary["pilot_id"]
    if not isinstance(pilot_id, str) or not re.fullmatch(r"pilot-[0-9a-f]{12}", pilot_id):
        raise ReleaseEvidenceError("private pilot id must be opaque")
    task_count = _integer(summary["task_count"], "task_count", 3, 5)
    selection = _keys(summary["task_selection"], {"fixed_before_execution", "risk_class_count", "work_type_count"}, "task_selection")
    if selection["fixed_before_execution"] is not True:
        raise ReleaseEvidenceError("pilot task selection was not fixed before execution")
    _integer(selection["risk_class_count"], "risk_class_count", 2, task_count)
    _integer(selection["work_type_count"], "work_type_count", 2, task_count)

    arms = _keys(summary["arms"], {"same_evaluation_contract", "control", "treatment"}, "arms")
    if arms["same_evaluation_contract"] != "opc-evaluation-contract-v1":
        raise ReleaseEvidenceError("control and treatment must use the exact v1 evaluation contract")
    control = _validate_arm(arms["control"], pilot_id=pilot_id, task_count=task_count, label="control")
    treatment = _validate_arm(arms["treatment"], pilot_id=pilot_id, task_count=task_count, label="treatment")
    directions = {
        "manager_intervention_rate": "lower",
        "qa_catch_rate": "higher",
        "rework_loops_per_task": "lower",
        "valid_knowledge_reuse_rate": "higher",
        "false_recall_rate": "lower",
    }
    comparisons: dict[str, str] = {}
    improved = 0
    for metric, direction in directions.items():
        left = control["metrics"][metric]["value"]
        right = treatment["metrics"][metric]["value"]
        delta = right - left
        status = "equal"
        if delta:
            better = delta < 0 if direction == "lower" else delta > 0
            status = "improved" if better else "regressed"
        comparisons[metric] = status
        improved += status == "improved"
    if "regressed" in comparisons.values() or improved == 0:
        raise ReleaseEvidenceError("private treatment must improve at least one quality metric without regression")

    coverage_keys = {
        "context_packets", "structured_feedback_records", "lineage_records", "shadow_pairs",
        "conflicts_seeded", "conflicts_rejected", "evolution_pilot_cases", "rollback_drills",
        "exact_rollback_restores",
    }
    coverage = _keys(summary["capability_coverage"], coverage_keys, "capability_coverage")
    for key in ("context_packets", "structured_feedback_records", "lineage_records", "shadow_pairs", "evolution_pilot_cases"):
        if _integer(coverage[key], key, 0, task_count) != task_count:
            raise ReleaseEvidenceError(f"{key} must cover every pilot task")
    conflicts = _integer(coverage["conflicts_seeded"], "conflicts_seeded", 1, task_count)
    if _integer(coverage["conflicts_rejected"], "conflicts_rejected", 1, task_count) != conflicts:
        raise ReleaseEvidenceError("every seeded conflict must be rejected")
    drills = _integer(coverage["rollback_drills"], "rollback_drills", 1, task_count)
    if _integer(coverage["exact_rollback_restores"], "exact_rollback_restores", 1, task_count) != drills:
        raise ReleaseEvidenceError("every rollback drill must restore exact bytes")
    fallback = _keys(summary["provider_fallback"], {"disabled_core_pass", "delete_rebuild_pass", "canonical_digest_unchanged"}, "provider_fallback")
    if any(value is not True for value in fallback.values()):
        raise ReleaseEvidenceError("File/Git disable/rebuild fallback is not fully proven")

    attestations = _keys(summary["attestations"], {"manager_approval", "independent_qa", "shadow_evaluation", "capability_evolution"}, "attestations")
    pilot_core_sha256 = _core_sha256(summary)
    decisions = {
        "manager_approval": ("approved", "not_applicable", False),
        "independent_qa": ("pass", "safe", True),
        "shadow_evaluation": ("beneficial", "safe", False),
        "capability_evolution": ("beneficial", "safe", False),
    }
    for name, (decision, safety, independent) in decisions.items():
        required_fields = {"decision", "evidence"}
        if name == "independent_qa":
            required_fields.add("independent_from_implementer")
        if name in {"shadow_evaluation", "capability_evolution"}:
            required_fields.add("safety")
        attestation = _keys(attestations[name], required_fields, f"attestations.{name}")
        if attestation["decision"] != decision or (
            name in {"shadow_evaluation", "capability_evolution"}
            and attestation["safety"] != safety
        ):
            raise ReleaseEvidenceError(f"{name} attestation does not pass")
        if name == "independent_qa" and attestation["independent_from_implementer"] is not True:
            raise ReleaseEvidenceError("release QA is not independent")
        evidence = _keys(attestation["evidence"], {"ref", "sha256"}, f"attestations.{name}.evidence")
        if not isinstance(evidence["ref"], str) or not evidence["ref"].startswith(".opc/"):
            raise ReleaseEvidenceError("pilot evidence refs must stay in the project .opc boundary")
        evidence_raw = _regular_private_file(
            private_root,
            evidence["ref"],
            evidence["sha256"],
            prefix=".opc/",
            ledger=ledger,
        )
        source_ref, source_sha256 = _evidence_envelope(
            evidence_raw,
            kind=name,
            pilot_id=pilot_id,
            pilot_core_sha256=pilot_core_sha256,
            task_count=task_count,
            decision=decision,
            safety=safety,
            independent=independent,
        )
        if source_ref == evidence["ref"]:
            raise ReleaseEvidenceError(f"{name} envelope cannot be its own source evidence")
        _regular_private_file(
            private_root,
            source_ref,
            source_sha256,
            prefix=".opc/",
            ledger=ledger,
        )

    confounders = summary["confounders"]
    if not isinstance(confounders, list) or not 1 <= len(confounders) <= 10 or len(set(confounders)) != len(confounders):
        raise ReleaseEvidenceError("confounders must be a non-empty unique bounded list")
    if any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", item) for item in confounders):
        raise ReleaseEvidenceError("confounders must use portable category identifiers")

    verdict = {
        "schema_version": "opc-v0.2-private-pilot-verdict-v1",
        "contract_version": CONTRACT_VERSION,
        "contract_sha256": _sha(contract_raw),
        "private_summary_sha256": _sha(raw),
        "task_count": task_count,
        "quality_comparison": comparisons,
        "safety": {
            "scope_leakage_acceptances": 0,
            "stale_obsolete_acceptances": 0,
            "privacy_failures": 0,
        },
        "control_metrics": control["metrics"],
        "treatment_metrics": treatment["metrics"],
        "capability_coverage": dict(coverage),
        "private_pilot_status": "pass",
        "claim": "association/evidence only",
    }
    _revalidate_private_reads(ledger)
    if _ledger_out is not None:
        _ledger_out.extend(ledger)
    return verdict


def _require_exact_clean_head(release_commit: str) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=False
    )
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if head.returncode or dirty.returncode:
        raise ReleaseEvidenceError("release gate could not establish repository state")
    if head.stdout.strip() != release_commit:
        raise ReleaseEvidenceError("release gate is not bound to the exact HEAD")
    if dirty.stdout:
        raise ReleaseEvidenceError("release gate requires a clean working tree")


def validate_release_gates(
    private_root: Path,
    gates_path: Path,
    private_sha: str,
    *,
    _ledger_out: list[_PrivateReadToken] | None = None,
) -> dict[str, Any]:
    ledger: list[_PrivateReadToken] = []
    raw = _private_input_bytes(
        private_root, gates_path, "release gates", ledger=ledger
    )
    try:
        gates = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseEvidenceError("release gates are not strict JSON") from exc
    gates = _keys(gates, {"schema_version", "contract_version", "release_commit", "private_pilot_sha256", "checks"}, "release gates")
    if gates["schema_version"] != GATES_VERSION:
        raise ReleaseEvidenceError("template or unsupported release gates cannot satisfy the gate")
    if gates["contract_version"] != CONTRACT_VERSION or gates["private_pilot_sha256"] != private_sha:
        raise ReleaseEvidenceError("release gates are not bound to the exact private summary")
    release_commit = gates["release_commit"]
    if not isinstance(release_commit, str) or not HEX40.fullmatch(release_commit):
        raise ReleaseEvidenceError("release commit is invalid")
    _require_exact_clean_head(release_commit)
    check_names = {
        "windows_ci", "linux_ci", "repository_validation", "privacy_current_and_history",
        "official_plugin_validator", "all_skill_quick_validators", "independent_release_qa",
        "rollback_evidence",
    }
    checks = _keys(gates["checks"], check_names, "release checks")
    for name in sorted(check_names):
        required = {"status", "evidence"}
        if name == "independent_release_qa":
            required.add("independent_from_implementer")
        check = _keys(checks[name], required, f"checks.{name}")
        if check["status"] != "pass":
            raise ReleaseEvidenceError(f"release check did not pass: {name}")
        if name == "independent_release_qa" and check["independent_from_implementer"] is not True:
            raise ReleaseEvidenceError("exact-release QA is not independent")
        evidence = _keys(check["evidence"], {"ref", "sha256"}, f"checks.{name}.evidence")
        if not isinstance(evidence["ref"], str) or not evidence["ref"].startswith("evidence/"):
            raise ReleaseEvidenceError("release check evidence must use the private evidence directory")
        evidence_raw = _regular_private_file(
            private_root,
            evidence["ref"],
            evidence["sha256"],
            prefix="evidence/",
            ledger=ledger,
        )
        try:
            envelope = json.loads(
                evidence_raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseEvidenceError(f"release check evidence is not strict JSON: {name}") from exc
        expected_envelope = {
            "schema_version": RELEASE_CHECK_VERSION,
            "contract_version": CONTRACT_VERSION,
            "evidence_kind": name,
            "release_commit": release_commit,
            "private_pilot_sha256": private_sha,
            "status": "pass",
            "independent_from_implementer": name == "independent_release_qa",
        }
        envelope = _keys(
            envelope,
            set(expected_envelope) | {"source_ref", "source_sha256"},
            f"release check envelope {name}",
        )
        if any(
            envelope[key] != expected_value
            for key, expected_value in expected_envelope.items()
        ):
            raise ReleaseEvidenceError(f"release check evidence subject or semantics mismatch: {name}")
        source_ref = envelope["source_ref"]
        source_sha256 = envelope["source_sha256"]
        if (
            not isinstance(source_ref, str)
            or not source_ref.startswith("evidence/")
            or source_ref == evidence["ref"]
            or not isinstance(source_sha256, str)
            or not HEX64.fullmatch(source_sha256)
        ):
            raise ReleaseEvidenceError(f"release check source evidence is invalid: {name}")
        _regular_private_file(
            private_root,
            source_ref,
            source_sha256,
            prefix="evidence/",
            ledger=ledger,
        )
    _revalidate_private_reads(ledger)
    _require_exact_clean_head(release_commit)
    if _ledger_out is not None:
        _ledger_out.extend(ledger)
    return {"release_commit": release_commit, "checks": {name: "pass" for name in sorted(check_names)}}


def build_release_verdict(
    private_root: Path,
    summary: Path,
    gates: Path,
    *,
    _ledger_out: list[_PrivateReadToken] | None = None,
) -> dict[str, Any]:
    verify_public()
    private_ledger: list[_PrivateReadToken] = []
    private = validate_private_pilot(private_root, summary, _ledger_out=private_ledger)
    release_ledger: list[_PrivateReadToken] = []
    exact = validate_release_gates(
        private_root,
        gates,
        private["private_summary_sha256"],
        _ledger_out=release_ledger,
    )
    verdict = {
        "schema_version": "opc-v0.2-release-verdict-v1",
        "contract_version": CONTRACT_VERSION,
        "release": "v0.2.0",
        "release_commit": exact["release_commit"],
        "public_evidence_sha256": _sha(PUBLIC_RESULT_PATH.read_bytes()),
        "private_summary_sha256": private["private_summary_sha256"],
        "task_count": private["task_count"],
        "quality_comparison": private["quality_comparison"],
        "safety": private["safety"],
        "checks": exact["checks"],
        "release_status": "ready",
        "claim": "association/evidence only",
        "non_claims": ["no causal attribution", "no statistical generality", "no autonomous self-improvement"],
    }
    _revalidate_private_reads(private_ledger)
    _revalidate_private_reads(release_ledger)
    _require_exact_clean_head(exact["release_commit"])
    if _ledger_out is not None:
        _ledger_out.extend(private_ledger)
        _ledger_out.extend(release_ledger)
    return verdict


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    public = commands.add_parser("public")
    public.add_argument("--output", type=Path, default=PUBLIC_RESULT_PATH)
    public.add_argument("--report", type=Path, default=PUBLIC_REPORT_PATH)
    commands.add_parser("verify-public")
    private = commands.add_parser("private-pilot")
    private.add_argument("--private-root", type=Path, required=True)
    private.add_argument("--summary", type=Path, required=True)
    private.add_argument("--output", type=Path)
    release = commands.add_parser("release")
    release.add_argument("--private-root", type=Path, required=True)
    release.add_argument("--summary", type=Path, required=True)
    release.add_argument("--gates", type=Path, required=True)
    release.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "public":
            write_public(args.output, args.report)
            result: Mapping[str, Any] = {"public_evidence_status": "pass", "release_status": "blocked"}
        elif args.command == "verify-public":
            verify_public()
            result = {"public_evidence_status": "pass", "release_status": "blocked"}
        elif args.command == "private-pilot":
            final_ledger: list[_PrivateReadToken] = []
            result = validate_private_pilot(
                args.private_root, args.summary, _ledger_out=final_ledger
            )
            if args.output:
                _write_private_output(
                    args.private_root,
                    args.output,
                    result,
                    pre_publish=lambda: _revalidate_private_reads(final_ledger),
                )
        else:
            final_ledger = []
            result = build_release_verdict(
                args.private_root,
                args.summary,
                args.gates,
                _ledger_out=final_ledger,
            )
            if args.output:
                _write_private_output(
                    args.private_root,
                    args.output,
                    result,
                    pre_publish=lambda: (
                        _revalidate_private_reads(final_ledger),
                        _require_exact_clean_head(result["release_commit"]),
                    ),
                )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ReleaseEvidenceError) as exc:
        print(f"V0_2_RELEASE_GATE_FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
