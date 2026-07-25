#!/usr/bin/env python3
"""Serve the legacy local, read-only OPC Dashboard presentation."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path
from typing import Any, Callable, Sequence

from opc_memory import resolve_data_root, resolve_knowledge_root
from opc_snapshot_service import (
    ASSET_ROUTES,
    MAX_ACCEPTANCE_BYTES,
    MAX_DEMO_BYTES,
    MAX_JSON_BYTES,
    MAX_PROJECTS,
    PORTABLE_ID,
    SCHEMA_VERSION,
    SECURITY_HEADERS,
    DashboardError,
    DashboardHTTPServer,
    DashboardRequestHandler,
    IPv6DashboardHTTPServer,
    SnapshotError,
    SnapshotService,
    _assert_redacted,
    _authority,
    _identity,
    _is_link,
    _looks_like_absolute_path,
    _manager_queue,
    _parse_acceptance,
    _project_warning,
    _read_checkpoint,
    _read_json,
    _read_project,
    _read_stable_bytes,
    _safe_text,
    _safe_time,
    _unavailable_acceptance,
    _validate_demo_snapshot,
    aggregate_snapshot,
    create_server,
    load_demo_snapshot,
    utc_now,
    validate_bind_host,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", action="append", default=[])
    parser.add_argument("--knowledge-root")
    parser.add_argument("--data-root")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--host", "--bind", dest="host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8569)
    parser.add_argument("--no-open", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_bind_host(args.host)
        if args.demo:
            if args.project_root or args.knowledge_root or args.data_root:
                parser.error("--demo 不能与真实数据根参数同时使用")
            service = SnapshotService(
                project_roots_provider=tuple,
                demo=True,
            )
        else:
            if not args.project_root:
                parser.error("真实模式需要至少一个 --project-root")
            knowledge_root = resolve_knowledge_root(args.knowledge_root)
            data_root = resolve_data_root(args.data_root)
            service = SnapshotService(
                project_roots_provider=lambda: args.project_root,
                knowledge_root=knowledge_root,
                data_root=data_root,
            )

        server = create_server(
            host=args.host,
            port=args.port,
            snapshot_provider=service.snapshot,
        )
    except SnapshotError as exc:
        print(f"OPC_DASHBOARD_ERROR: {exc.code}", file=sys.stderr)
        return 2
    url = f"http://{_authority(args.host, int(server.server_address[1]))}/"
    print(f"OPC Dashboard: {url}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
