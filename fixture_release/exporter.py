from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .archive import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EMBEDDING_NORMALIZED,
    FORMAT,
    FORMAT_VERSION,
    LIBRARY_LICENSE_POLICY,
    LIBRARY_MAX_ASSET_BYTES,
    LICENSE_NOTICE,
    LICENSE_REFERENCES,
    MAX_OBJECTS,
    MIMETYPE_EXTENSIONS,
    OBJECT_METADATA_FIELDS,
    SUPPORTED_LICENSE_VALUES,
    FixtureArchive,
    normalize_vector,
    validate_media,
)
from .model import (
    ExportSummary,
    FixtureExportError,
    JsonObject,
    LibrarySource,
    ProgressReporter,
    SilentProgress,
)

SENSITIVE_MARKERS = ("sb_secret_", "/storage/v1/object/", "eyJhbGciOi")


def _embedding_row(value: Any) -> JsonObject:
    if isinstance(value, list):
        if len(value) > 1:
            raise FixtureExportError("object has multiple embedding rows")
        value = value[0] if value else None
    if not isinstance(value, dict):
        raise FixtureExportError("object image embedding is missing")
    return value


class FixtureExporter:
    """Exports one curated category into a portable developer fixture."""

    def __init__(
        self,
        source: LibrarySource,
        *,
        archive: FixtureArchive | None = None,
        progress: ProgressReporter | None = None,
    ):
        self._source = source
        self._archive = archive or FixtureArchive()
        self._progress = progress or SilentProgress()

    def export(
        self,
        category_identifier: str,
        output: Path,
        *,
        limit: int | None = None,
    ) -> ExportSummary:
        identifier = category_identifier.strip()
        if not identifier:
            raise FixtureExportError("category name must not be empty")
        if limit is not None and limit < 1:
            raise FixtureExportError("limit must be at least 1")

        category = self._source.resolve_category(identifier)
        production_ids = self._source.list_category_object_ids(str(category["id"]))
        if not production_ids:
            raise FixtureExportError(f'Library category "{category["name"]}" is empty')
        if len(production_ids) != len(set(production_ids)):
            raise FixtureExportError("Library category membership is duplicated")

        self._progress.start("Fetching object metadata", len(production_ids))
        raw_by_id: dict[str, JsonObject] = {}
        for row in self._source.iter_objects(production_ids):
            object_id = str(row.get("id") or "")
            if not object_id or object_id in raw_by_id:
                raise FixtureExportError(
                    "production object query returned an invalid duplicate"
                )
            raw_by_id[object_id] = row
            self._progress.advance()
        self._progress.finish()

        missing = [value for value in production_ids if value not in raw_by_id]
        if missing:
            raise FixtureExportError(
                f"{len(missing)} category objects could not be read"
            )

        ordered_ids = sorted(
            production_ids,
            key=lambda object_id: self._selection_key(object_id, raw_by_id[object_id]),
        )
        selected_ids = ordered_ids if limit is None else ordered_ids[:limit]
        if len(selected_ids) > MAX_OBJECTS:
            raise FixtureExportError(
                f"category has {len(selected_ids)} objects; release archives "
                f"support at most {MAX_OBJECTS}"
            )

        objects: list[JsonObject] = []
        embeddings: list[JsonObject] = []
        assets: dict[str, bytes] = {}
        licenses: Counter[str] = Counter()
        total_bytes = 0

        self._progress.start("Downloading and validating assets", len(selected_ids))
        for production_id in selected_ids:
            raw = raw_by_id[production_id]
            path = str(raw.get("path") or "")
            if not path:
                raise FixtureExportError("production object path is missing")
            payload = self._source.download(path)
            digest = hashlib.sha256(payload).hexdigest()
            recorded_digest = raw.get("sha256")
            if recorded_digest is not None and recorded_digest != digest:
                raise FixtureExportError(
                    f'object "{path}" does not match its recorded SHA-256'
                )
            if digest in assets:
                raise FixtureExportError(
                    "category contains duplicate bytes with conflicting "
                    f"catalog identities ({digest})"
                )

            license_name = str(raw.get("license") or "").strip()
            if license_name not in SUPPORTED_LICENSE_VALUES:
                raise FixtureExportError(
                    f'object "{path}" has an unsupported source license value '
                    f'"{license_name or "unknown"}"'
                )

            declared_bytes = raw.get("bytes")
            if declared_bytes is not None:
                try:
                    declared_byte_count = int(declared_bytes)
                except (TypeError, ValueError) as error:
                    raise FixtureExportError(
                        f'object "{path}" has invalid byte-size metadata'
                    ) from error
                if declared_byte_count != len(payload):
                    raise FixtureExportError(
                        f'object "{path}" has stale byte-size metadata'
                    )
            if len(payload) > LIBRARY_MAX_ASSET_BYTES:
                raise FixtureExportError(
                    f'object "{path}" exceeds the 3 MiB Library asset limit'
                )

            mimetype = str(raw.get("mimetype") or "")
            extension = MIMETYPE_EXTENSIONS.get(mimetype)
            if not extension:
                raise FixtureExportError(
                    f'object "{path}" has unsupported MIME type "{mimetype}"'
                )
            dimensions: dict[str, int] = {}
            for dimension in ("width", "height"):
                try:
                    dimension_value = int(raw.get(dimension) or 0)
                except (TypeError, ValueError) as error:
                    raise FixtureExportError(
                        f'object "{path}" has invalid {dimension} metadata'
                    ) from error
                if dimension_value < 1:
                    raise FixtureExportError(
                        f'object "{path}" has invalid {dimension} metadata'
                    )
                dimensions[dimension] = dimension_value
            validate_media(
                payload,
                mimetype,
                path,
                expected_dimensions=(dimensions["width"], dimensions["height"]),
            )
            asset_path = f"assets/{digest}.{extension}"
            assets[asset_path] = payload
            object_ref = f"sha256:{digest}"
            total_bytes += len(payload)
            licenses[license_name] += 1

            metadata = {field: raw.get(field) for field in OBJECT_METADATA_FIELDS}
            metadata["bytes"] = len(payload)
            objects.append(
                {
                    "asset_path": asset_path,
                    "metadata": metadata,
                    "ref": object_ref,
                    "sha256": digest,
                }
            )

            embedding = _embedding_row(raw.get("object_embedding"))
            embeddings.append(
                {
                    "image": normalize_vector(
                        embedding.get("gemini_embedding_2__image"),
                        name=f"{digest} image embedding",
                        required=True,
                    ),
                    "object_ref": object_ref,
                    "text": normalize_vector(
                        embedding.get("gemini_embedding_2__text"),
                        name=f"{digest} text embedding",
                        required=False,
                    ),
                }
            )
            self._progress.advance()
        self._progress.finish()

        category_id = str(category["id"])
        if any(row["metadata"]["category"] != category_id for row in objects):
            raise FixtureExportError(
                "exported object does not match the requested category"
            )
        categories = [
            {
                "description": category.get("description"),
                "id": category_id,
                "name": category["name"],
            }
        ]

        objects.sort(key=lambda row: str(row["ref"]))
        embeddings.sort(key=lambda row: str(row["object_ref"]))

        category_manifest = {
            "description": category.get("description"),
            "id": category["id"],
            "name": category["name"],
            "ref": f"category:{category['id']}",
        }
        manifest: JsonObject = {
            "asset_addressing": {
                "algorithm": "sha256",
                "path_template": "assets/{sha256}.{ext}",
            },
            "category": category_manifest,
            "embedding": {
                "dimensions": EMBEDDING_DIMENSIONS,
                "image_column": "gemini_embedding_2__image",
                "image_required": True,
                "metric": "cosine",
                "model": EMBEDDING_MODEL,
                "normalized": EMBEDDING_NORMALIZED,
                "text_column": "gemini_embedding_2__text",
                "text_optional": True,
            },
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "license_metadata": {
                "notice": LICENSE_NOTICE,
                "project_policy": LIBRARY_LICENSE_POLICY,
                "references": LICENSE_REFERENCES,
                "supported_source_values": list(SUPPORTED_LICENSE_VALUES),
            },
            "licenses": dict(sorted(licenses.items())),
            "object_count": len(objects),
        }
        readme = self._archive_readme(
            category=category_manifest,
            licenses=licenses,
            object_count=len(objects),
            text_embedding_count=sum(row["text"] is not None for row in embeddings),
        )
        self._assert_no_production_references(
            manifest=manifest,
            readme=readme,
            objects=objects,
            embeddings=embeddings,
            categories=categories,
            production_values=[
                *production_ids,
                *(
                    str(raw_by_id[object_id].get("path") or "")
                    for object_id in production_ids
                    if not self._has_canonical_source_path(raw_by_id[object_id])
                ),
            ],
        )

        self._progress.start("Writing archive", 1)
        archive_sha256 = self._archive.write(
            output,
            manifest=manifest,
            readme=readme,
            objects=objects,
            embeddings=embeddings,
            categories=categories,
            assets=assets,
        )
        self._progress.advance()
        self._progress.finish()
        return ExportSummary(
            archive=output,
            checksum_file=Path(f"{output}.sha256"),
            archive_sha256=archive_sha256,
            category_id=str(category["id"]),
            object_count=len(objects),
            total_bytes=total_bytes,
        )

    @staticmethod
    def _archive_readme(
        *,
        category: JsonObject,
        licenses: Counter[str],
        object_count: int,
        text_embedding_count: int,
    ) -> str:
        license_lines = "\n".join(
            f"- {name}: {count}" for name, count in sorted(licenses.items())
        )
        return f"""# Grida Library developer corpus

This archive is a versioned local-development fixture exported from the
public Grida Library category **{category["name"]}**
(`{category["id"]}`). It is not a database backup and must not be edited
in place.

## Contents

- `manifest.json` — archive contract, embedding model, counts, and checksums
- `objects.jsonl` — sanitized object metadata keyed by content SHA-256
- `embeddings.jsonl` — matching image and optional text embeddings
- `categories.json` — referenced category metadata
- `assets/` — the exact source asset bytes

Production database identifiers, storage paths, authentication data, all
author data, prompts, and production timestamps are intentionally excluded.
Consumers must verify the manifest and file checksums before importing.

## Snapshot

- Objects: {object_count}
- Image embeddings: {object_count}
- Text embeddings: {text_embedding_count}
- Embedding model: `{EMBEDDING_MODEL}`
- Embedding dimensions: {EMBEDDING_DIMENSIONS}, L2-normalized

## Licenses

The following per-object license values were present when this fixture was
exported:

{license_lines}

Each asset retains the license recorded in `objects.jsonl`. Inclusion in this
archive does not replace or broaden that license.

- `CC0-1.0`: {LICENSE_REFERENCES["CC0-1.0"]}
- `LicenseRef-GridaLibrary` is a project-specific source identifier. No
  standalone license text is embedded or mapped to it by this archive.
- Grida Library licensing policy: {LIBRARY_LICENSE_POLICY}
"""

    @staticmethod
    def _selection_key(object_id: str, row: JsonObject) -> tuple[float, str]:
        value = row.get("score")
        try:
            score = float(value) if value is not None else float("-inf")
        except (TypeError, ValueError):
            score = float("-inf")
        return (-score, object_id)

    @staticmethod
    def _has_canonical_source_path(row: JsonObject) -> bool:
        path = row.get("path")
        digest = row.get("sha256")
        extension = MIMETYPE_EXTENSIONS.get(row.get("mimetype"))
        return (
            isinstance(path, str)
            and isinstance(digest, str)
            and extension is not None
            and path == f"{digest}.{extension}"
        )

    @staticmethod
    def _assert_no_production_references(
        *,
        manifest: JsonObject,
        readme: str,
        objects: list[JsonObject],
        embeddings: list[JsonObject],
        categories: list[JsonObject],
        production_values: list[str],
    ) -> None:
        serialized = json.dumps(
            {
                "manifest": manifest,
                "readme": readme,
                "objects": objects,
                "embeddings": embeddings,
                "categories": categories,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if any(value and value in serialized for value in production_values):
            raise FixtureExportError(
                "sanitized corpus still contains a production identifier or path"
            )
        if any(marker in serialized for marker in SENSITIVE_MARKERS):
            raise FixtureExportError(
                "sanitized corpus contains a credential or Storage URL marker"
            )
