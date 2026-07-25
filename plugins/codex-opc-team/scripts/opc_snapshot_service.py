#!/usr/bin/env python3
"""Shared, read-only snapshot application service for OPC visual surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


Snapshot = dict[str, Any]
ProjectRootsProvider = Callable[[], Sequence[Path | str]]
SnapshotBuilder = Callable[..., Snapshot]
DemoLoader = Callable[[], Snapshot]
SnapshotValidator = Callable[[Any], None]


class SnapshotService:
    """Keep Dashboard and OPC App on one aggregation/redaction contract."""

    def __init__(
        self,
        *,
        project_roots_provider: ProjectRootsProvider,
        snapshot_builder: SnapshotBuilder,
        demo_loader: DemoLoader,
        snapshot_validator: SnapshotValidator,
        knowledge_root: Path | str | None = None,
        data_root: Path | str | None = None,
        demo: bool = False,
        allow_empty: bool = False,
    ) -> None:
        self._project_roots_provider = project_roots_provider
        self._snapshot_builder = snapshot_builder
        self._demo_loader = demo_loader
        self._snapshot_validator = snapshot_validator
        self._knowledge_root = knowledge_root
        self._data_root = data_root
        self._demo = demo
        self._allow_empty = allow_empty

    def snapshot(self) -> Snapshot:
        if self._demo:
            snapshot = self._demo_loader()
        else:
            if self._knowledge_root is None or self._data_root is None:
                raise RuntimeError("live snapshot roots are required")
            arguments: dict[str, Any] = {
                "knowledge_root": self._knowledge_root,
                "data_root": self._data_root,
            }
            if self._allow_empty:
                arguments["allow_empty"] = True
            snapshot = self._snapshot_builder(
                self._project_roots_provider(),
                **arguments,
            )
        if not isinstance(snapshot, Mapping):
            raise RuntimeError("snapshot builder returned an invalid value")
        self._snapshot_validator(snapshot)
        return dict(snapshot)
