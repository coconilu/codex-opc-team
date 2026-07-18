#!/usr/bin/env python3
"""Privacy-minimal Codex hook for active OPC project runs.

The hook performs no write until a valid ``.opc/run.json`` has been found.
Runtime events use a strict allowlist and never copy raw hook payloads, local
paths, session identifiers, turn identifiers, or model names.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from opc_knowledge import find_run_path, load_json, utc_now


ALLOWED_EVENTS = {"Stop", "SubagentStop"}
STOP_ALLOWED_STATUSES = {"ready_for_manager", "completed", "paused", "failed"}
SAFE_RUN_ID = re.compile(r"^opc-[A-Za-z0-9._-]+$")
SAFE_PROJECT_ID = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_EVENT_LOG_BYTES = 256 * 1024
MAX_ROTATED_EVENT_LOGS = 3
EVENT_LOG_RETENTION_DAYS = 14


def _allow(reason: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"continue": True}
    if reason:
        result["systemMessage"] = reason
    return result


def _block(reason: str) -> dict[str, Any]:
    return {"continue": False, "stopReason": reason}


def _read_payload() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _valid_run(run: Mapping[str, Any], run_path: Path, start: Path) -> bool:
    run_id = run.get("run_id")
    status = run.get("status")
    project_id = run.get("project_id")
    expires_at = _parse_utc(run.get("expires_at"))
    basic = (
        run.get("schema_version") == 1
        and isinstance(run_id, str)
        and SAFE_RUN_ID.fullmatch(run_id) is not None
        and isinstance(status, str)
        and status
        and isinstance(project_id, str)
        and SAFE_PROJECT_ID.fullmatch(project_id) is not None
        and run.get("active") is True
        and expires_at is not None
        and expires_at > datetime.now(timezone.utc)
    )
    if not basic or run_path.is_symlink() or run_path.parent.is_symlink():
        return False
    try:
        project_root = run_path.parent.parent.resolve()
        start.relative_to(project_root)
    except (OSError, RuntimeError, ValueError):
        return False
    project_path = run_path.parent / "project.json"
    if not project_path.is_file() or project_path.is_symlink():
        return False
    try:
        project = load_json(project_path)
    except Exception:
        return False
    return (
        project.get("schema_version") == 1
        and project.get("project_id") == project_id
    )


def _event_path(run_path: Path, run_id: str) -> Path:
    project_fallback = run_path.parent / "events.jsonl"
    plugin_data = os.environ.get("PLUGIN_DATA")
    if not plugin_data:
        return project_fallback
    try:
        configured_data = Path(plugin_data).expanduser()
        if not configured_data.is_absolute():
            return project_fallback
        data_root = configured_data.resolve()
        knowledge_value = os.environ.get("OPC_KNOWLEDGE_HOME")
        knowledge_root = (
            Path(knowledge_value).expanduser().resolve()
            if knowledge_value
            else (Path.home() / "opc-knowledge").resolve()
        )
        data_root.relative_to(knowledge_root)
    except ValueError:
        return data_root / "run-events" / f"{run_id}.jsonl"
    except (OSError, RuntimeError):
        return project_fallback
    # A misconfigured PLUGIN_DATA inside canonical knowledge must never turn
    # runtime telemetry into organizational knowledge. Keep the event local to
    # the already validated project run instead.
    return project_fallback


def _rotate_event_log(path: Path, incoming_bytes: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=EVENT_LOG_RETENTION_DAYS)
    for rotated in path.parent.glob(f"{path.name}.*"):
        try:
            modified = datetime.fromtimestamp(rotated.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                rotated.unlink()
        except OSError:
            continue
    try:
        current_size = path.stat().st_size
    except FileNotFoundError:
        return
    if current_size + incoming_bytes <= MAX_EVENT_LOG_BYTES:
        return
    oldest = path.with_name(f"{path.name}.{MAX_ROTATED_EVENT_LOGS}")
    try:
        oldest.unlink()
    except FileNotFoundError:
        pass
    for index in range(MAX_ROTATED_EVENT_LOGS - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        destination = path.with_name(f"{path.name}.{index + 1}")
        if source.exists():
            os.replace(source, destination)
    os.replace(path, path.with_name(f"{path.name}.1"))


@contextmanager
def _event_file_lock(path: Path):
    """Serialize rotation and append across hook processes."""
    lock_path = path.parent / ".opc-hook.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            # A full test run can start many short-lived hook processes at once.
            # Keep retrying long enough for Windows scheduler/process-start jitter
            # instead of silently dropping an otherwise valid event.
            for attempt in range(200):
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if attempt == 199:
                        raise
                    time.sleep(0.025)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _append_event(path: Path, event: Mapping[str, Any]) -> None:
    for attempt in range(20):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            break
        except OSError:
            if path.parent.is_dir():
                break
            if attempt == 19:
                raise
            time.sleep(0.025)
    encoded = (json.dumps(dict(event), ensure_ascii=False) + "\n").encode("utf-8")
    with _event_file_lock(path):
        _rotate_event_log(path, len(encoded))
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("Could not append the complete OPC hook event")
                remaining = remaining[written:]
        finally:
            os.close(descriptor)


def evaluate(payload: Mapping[str, Any]) -> tuple[dict[str, Any], Path | None, dict[str, Any] | None]:
    """Return hook response, validated run path, and allowlisted event.

    A missing or invalid project run always returns before an event is built.
    This ordering is the central privacy invariant of the hook.
    """
    cwd_value = payload.get("cwd")
    if not isinstance(cwd_value, str) or not cwd_value:
        return _allow(), None, None
    try:
        start = Path(cwd_value).expanduser().resolve()
    except (OSError, RuntimeError):
        return _allow(), None, None
    if not start.is_dir():
        return _allow(), None, None
    located = find_run_path(start)
    if located is None:
        return _allow(), None, None
    try:
        run = load_json(located)
    except Exception:
        return _allow(), None, None
    if not _valid_run(run, located, start):
        return _allow(), None, None

    raw_event = payload.get("hook_event_name")
    event_name = raw_event if raw_event in ALLOWED_EVENTS else "Unknown"
    status = str(run["status"])
    if event_name == "Stop":
        can_stop = bool(run.get("allow_stop")) or status in STOP_ALLOWED_STATUSES
        response = (
            _allow("OPC acceptance gate passed.")
            if can_stop
            else _block(
                "OPC run is not ready for manager handoff. Record implementation, verification, and independent QA evidence first."
            )
        )
    else:
        response = _allow()
    event = {
        "event": event_name,
        "at": utc_now(),
        "run_id": run["run_id"],
        "status": status,
        "continue": bool(response["continue"]),
    }
    return response, located, event


def main() -> int:
    payload = _read_payload()
    response, located, event = evaluate(payload)
    if located is not None and event is not None:
        event_path = _event_path(located, str(event["run_id"]))
        for attempt in range(5):
            try:
                _append_event(event_path, event)
                break
            except OSError:
                # Event telemetry is best-effort; retry transient Windows sharing
                # failures without ever weakening acceptance from run.json.
                if attempt == 4:
                    break
                time.sleep(0.05 * (attempt + 1))
    print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
