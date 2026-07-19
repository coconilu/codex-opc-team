#!/usr/bin/env python3
"""Fail when public repository content looks like private runtime data or a secret."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SKIP_DIRS = {".git", ".pytest_cache", ".venv", "__pycache__", "build", "dist", "venv"}
FORBIDDEN_NAMES = {"hook-events.jsonl", "manager-profile.md"}
SAFE_ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template"}
PRIVATE_KEY_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}
PRIVATE_KEY_NAMES = {"id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}
PATTERNS = {
    "Windows user home": re.compile(r"(?i)[a-z]:\\Users\\[^\\\s\"'`]+"),
    "macOS user home": re.compile(r"/" r"Users/[^/\s\"'`]+"),
    "Linux user home": re.compile(r"/" r"home/[^/\s\"'`]+"),
    "OpenAI-style secret": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "private key material": re.compile(
        r"-----BEGIN (?:[A-Z0-9]+(?: [A-Z0-9]+)* )?PRIVATE KEY-----"
    ),
    "credential assignment": re.compile(
        r"(?i)(?:api[_-]?key|token|secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9+/=_-]{24,}"
    ),
    "captured session id": re.compile(
        r"[\"'](?:session_id|turn_id)[\"']\s*:\s*[\"'][0-9a-f]{8}-[0-9a-f-]{27,}[\"']",
        re.IGNORECASE,
    ),
}


def forbidden_filename(path: Path) -> bool:
    name = path.name.lower()
    if name in FORBIDDEN_NAMES or name in PRIVATE_KEY_NAMES:
        return True
    if name.startswith(".env") and name not in SAFE_ENV_EXAMPLES:
        return True
    return path.suffix.lower() in PRIVATE_KEY_SUFFIXES


def iter_files(root: Path):
    first_directory = True
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        symlink_dirs = sorted(
            current_path / name
            for name in dirs
            if name not in SKIP_DIRS and (current_path / name).is_symlink()
        )
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in SKIP_DIRS and not (current_path / name).is_symlink()
        )
        yield from symlink_dirs
        for name in sorted(files):
            # A linked Git worktree stores a machine-local ``gitdir`` pointer
            # in a root .git control file. It is Git metadata, not publishable
            # repository content; history is scanned separately below.
            if first_directory and name == ".git":
                continue
            yield current_path / name
        first_directory = False


def scan_text(relative: Path, text: str, *, prefix: str = "") -> list[str]:
    findings: list[str] = []
    label_path = f"{prefix}{relative}"
    for label, pattern in PATTERNS.items():
        match = pattern.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{label_path}:{line}: {label}")
    return findings


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    try:
        canonical_root = root.expanduser().resolve(strict=True)
    except OSError as exc:
        return [f"scan-root: SCAN_ROOT_UNAVAILABLE ({type(exc).__name__})"]
    if not canonical_root.is_dir():
        return ["scan-root: SCAN_ROOT_UNAVAILABLE (NotDirectory)"]
    for path in iter_files(canonical_root):
        try:
            relative = path.relative_to(canonical_root)
        except ValueError:
            findings.append(f"{path.name}: SCAN_PATH_ESCAPED")
            continue
        if forbidden_filename(relative):
            findings.append(f"{relative}: forbidden private/runtime filename")
        if path.is_symlink():
            try:
                target = os.readlink(path)
                resolved_target = (path.parent / target).resolve(strict=False)
                resolved_target.relative_to(canonical_root)
            except (OSError, ValueError):
                findings.append(f"{relative}: symbolic link escapes scan root")
            else:
                findings.extend(scan_text(relative, target))
            continue
        is_safe_env_example = path.name.lower() in SAFE_ENV_EXAMPLES
        if (
            not is_safe_env_example
            and path.suffix.lower() not in TEXT_SUFFIXES
        ) or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_text(relative, text))
    return findings


def scan_git_history(root: Path) -> list[str]:
    try:
        revisions = subprocess.run(
            ["git", "-C", str(root), "rev-list", "--all"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        return [f"git-history: HISTORY_SCAN_UNAVAILABLE ({type(exc).__name__})"]
    findings: set[str] = set()
    for revision in revisions:
        try:
            listing = subprocess.run(
                ["git", "-C", str(root), "ls-tree", "-r", "--name-only", revision],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.splitlines()
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            findings.add(
                f"git:{revision[:12]}: HISTORY_SCAN_UNAVAILABLE ({type(exc).__name__})"
            )
            continue
        for item in listing:
            relative = Path(item)
            if forbidden_filename(relative):
                findings.add(f"git:{revision[:12]}:{relative}: forbidden private/runtime filename")
            if (
                relative.name.lower() not in SAFE_ENV_EXAMPLES
                and relative.suffix.lower() not in TEXT_SUFFIXES
            ):
                continue
            blob = subprocess.run(
                ["git", "-C", str(root), "show", f"{revision}:{item}"],
                check=False,
                capture_output=True,
            )
            if blob.returncode != 0:
                findings.add(f"git:{revision[:12]}:{relative}: HISTORY_BLOB_SCAN_UNAVAILABLE")
                continue
            if len(blob.stdout) > 2_000_000:
                continue
            try:
                text = blob.stdout.decode("utf-8")
            except UnicodeDecodeError:
                continue
            findings.update(scan_text(relative, text, prefix=f"git:{revision[:12]}:"))
    return sorted(findings)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--git-history", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    findings = scan(root)
    if args.git_history:
        findings.extend(scan_git_history(root))
    if findings:
        print("PRIVACY_SCAN_FAILED")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print(f"PRIVACY_SCAN_OK root={root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
