#!/usr/bin/env python3
"""Dry-run inventory for migration from the pre-open-source local prototype."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


PUBLIC_PLUGIN_PARTS = ("skills", "scripts", "hooks", "assets", ".codex-plugin")
PRIVATE_NAMES = {"hook-events.jsonl", "manager-profile.md", ".env"}
ABSOLUTE_HOME = re.compile(
    r"(?i)(?:[a-z]:\\Users\\[^\\\s]+|/" r"Users/[^/\s]+|/" r"home/[^/\s]+)"
)


def inspect_plugin(root: Path) -> tuple[list[str], list[dict[str, str]]]:
    candidates: list[str] = []
    excluded: list[dict[str, str]] = []
    if not root.is_dir():
        return candidates, [{"path": str(root), "reason": "legacy plugin directory not found"}]
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if ".git" in relative.parts or "__pycache__" in relative.parts or path.suffix == ".pyc":
            excluded.append({"path": str(relative), "reason": "repository/runtime metadata"})
            continue
        if relative.parts[0] not in PUBLIC_PLUGIN_PARTS:
            excluded.append({"path": str(relative), "reason": "not a reusable plugin component"})
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            excluded.append({"path": str(relative), "reason": "binary or non-UTF-8"})
            continue
        if ABSOLUTE_HOME.search(text):
            excluded.append({"path": str(relative), "reason": "contains an absolute user-home path"})
            continue
        candidates.append(str(relative))
    return candidates, excluded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-plugin", type=Path, required=True)
    parser.add_argument("--knowledge-home", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    candidates, excluded = inspect_plugin(args.old_plugin.expanduser().resolve())
    knowledge = args.knowledge_home.expanduser().resolve() if args.knowledge_home else None
    report = {
        "mode": "dry-run",
        "public_candidates": candidates,
        "excluded": excluded,
        "knowledge": {
            "path": str(knowledge) if knowledge else None,
            "policy": "keep private and reuse through OPC_KNOWLEDGE_HOME; never copy into the public repository",
            "forbidden_names": sorted(PRIVATE_NAMES),
        },
        "next_step": "review the report; this command intentionally performs no writes",
    }
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"Public candidates: {len(candidates)}")
        print(f"Excluded: {len(excluded)}")
        print(report["knowledge"]["policy"])
        print(report["next_step"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
