#!/usr/bin/env python3
"""Smoke-test the frozen OPC sidecar with isolated synthetic state."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


STARTUP = re.compile(r"^OPC App: (http://127[.]0[.]0[.]1:[1-9][0-9]{0,4}/)$")


def target_triple() -> str:
    result = subprocess.run(
        ["rustc", "-vV"],
        check=True,
        text=True,
        capture_output=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("host: "):
            value = line.removeprefix("host: ").strip()
            if re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                return value
    raise RuntimeError("Unable to resolve the Rust target triple.")


def get_json(url: str) -> tuple[int, object]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    desktop = Path(__file__).resolve().parents[1]
    extension = ".exe" if sys.platform == "win32" else ""
    parser.add_argument(
        "--binary",
        type=Path,
        default=desktop
        / "src-tauri"
        / "binaries"
        / f"opc-sidecar-{target_triple()}{extension}",
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=desktop / ".sidecar-build" / "smoke",
    )
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    evidence_root = args.evidence_root.resolve()
    state_root = evidence_root / "state"
    knowledge_root = evidence_root / "knowledge"
    data_root = evidence_root / "data"
    for path in (state_root, knowledge_root, data_root):
        path.mkdir(parents=True, exist_ok=True)

    process = subprocess.Popen(
        [
            str(binary),
            "--no-open",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--state-root",
            str(state_root),
            "--knowledge-root",
            str(knowledge_root),
            "--data-root",
            str(data_root),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        if process.stdout is None:
            raise RuntimeError("Sidecar stdout was not captured.")
        deadline = time.monotonic() + 20
        line = b""
        while time.monotonic() < deadline and process.poll() is None:
            line = process.stdout.readline(513)
            if line:
                break
        if len(line) > 512:
            raise RuntimeError("Sidecar startup output exceeded the safety limit.")
        text = line.rstrip(b"\r\n").decode("utf-8", errors="strict")
        match = STARTUP.fullmatch(text)
        if not match:
            raise RuntimeError("Sidecar returned an untrusted startup line.")
        url = match.group(1)
        with urllib.request.urlopen(url, timeout=5) as response:
            root_status = response.status
            response.read()
        context_status, context = get_json(url + "api/app-context")
        if root_status != 200 or context_status != 200:
            raise RuntimeError("Sidecar HTTP contract did not return 200.")
        if not isinstance(context, dict) or context.get("schema_version") != "opc-app.context.v1":
            raise RuntimeError("Sidecar returned an unexpected App context schema.")
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        print(
            json.dumps(
                {
                    "sidecar_sha256": digest,
                    "startup_url": url,
                    "root_status": root_status,
                    "context_status": context_status,
                    "context_schema": context["schema_version"],
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        if process.poll() is None:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.poll() is None:
            raise RuntimeError("The owned sidecar did not exit.")


if __name__ == "__main__":
    raise SystemExit(main())
