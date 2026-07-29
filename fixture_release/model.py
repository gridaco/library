from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

JsonObject = dict[str, Any]


class FixtureExportError(RuntimeError):
    """A safe, user-actionable fixture export failure."""


class LibrarySource(Protocol):
    """Read-only source contract consumed by the fixture exporter."""

    def resolve_category(self, identifier: str) -> JsonObject: ...

    def list_category_object_ids(self, category_id: str) -> list[str]: ...

    def iter_objects(self, object_ids: Sequence[str]) -> Iterable[JsonObject]: ...

    def download(self, path: str) -> bytes: ...


class ProgressReporter(Protocol):
    def start(self, label: str, total: int) -> None: ...

    def advance(self) -> None: ...

    def finish(self) -> None: ...


class SilentProgress:
    def start(self, label: str, total: int) -> None:
        del label, total

    def advance(self) -> None:
        pass

    def finish(self) -> None:
        pass


@dataclass(frozen=True)
class ExportSummary:
    archive: Path
    checksum_file: Path
    archive_sha256: str
    category_id: str
    object_count: int
    total_bytes: int
