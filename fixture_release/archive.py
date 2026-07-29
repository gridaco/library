from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
import zipfile
from collections import Counter
from collections.abc import Mapping, Sequence
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any

from PIL import Image, UnidentifiedImageError

from .model import FixtureExportError, JsonObject

ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
FORMAT = "grida-library-developer-corpus"
FORMAT_VERSION = 1
EMBEDDING_MODEL = "google/gemini-embedding-2"
EMBEDDING_DIMENSIONS = 1536
EMBEDDING_NORMALIZED = True
SUPPORTED_LICENSE_VALUES = ("CC0-1.0", "LicenseRef-GridaLibrary")
LICENSE_REFERENCES = {
    "CC0-1.0": "https://creativecommons.org/public-domain/cc0/",
}
LIBRARY_LICENSE_POLICY = "https://grida.co/library/license"
LICENSE_NOTICE = (
    "Source catalog values are preserved verbatim. LicenseRef-GridaLibrary "
    "has no standalone license text or SPDX mapping in this archive."
)
LIBRARY_MAX_ASSET_BYTES = 3 * 1024 * 1024
MAX_OBJECTS = 1000
MAX_ARCHIVE_ENTRIES = MAX_OBJECTS + 6
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
CONTROL_ENTRY_MAX_BYTES = {
    "README.md": 256 * 1024,
    "manifest.json": 4 * 1024 * 1024,
    "checksums.sha256": 256 * 1024,
    "objects.jsonl": 16 * 1024 * 1024,
    "embeddings.jsonl": 64 * 1024 * 1024,
    "categories.json": 1024 * 1024,
}
MIMETYPE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
EMAIL_RE = re.compile(r"\b[^@\s]+@[^@\s]+\.[^@\s]+\b")
OBJECT_METADATA_FIELDS = (
    "title",
    "alt",
    "description",
    "category",
    "categories",
    "objects",
    "keywords",
    "mimetype",
    "width",
    "height",
    "bytes",
    "license",
    "version",
    "fill",
    "color",
    "colors",
    "background",
    "score",
    "year",
    "entropy",
    "orientation",
    "gravity_x",
    "gravity_y",
    "lang",
    "transparency",
    "public_domain",
)


def json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    indent = 2 if pretty else None
    separators = None if pretty else (",", ":")
    text = json.dumps(
        value,
        ensure_ascii=False,
        indent=indent,
        separators=separators,
        sort_keys=True,
    )
    return (text + "\n").encode("utf-8")


def jsonl_bytes(rows: Sequence[JsonObject]) -> bytes:
    return b"".join(json_bytes(row) for row in rows)


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_vector(value: Any, *, name: str, required: bool) -> list[float] | None:
    if value is None:
        if required:
            raise FixtureExportError(f"{name} is missing")
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise FixtureExportError(f"{name} is not a valid vector") from error
    if not isinstance(value, list):
        raise FixtureExportError(f"{name} is not a vector")
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError) as error:
        raise FixtureExportError(f"{name} contains a non-number") from error
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise FixtureExportError(
            f"{name} has {len(vector)} dimensions; expected {EMBEDDING_DIMENSIONS}"
        )
    if not all(math.isfinite(item) for item in vector):
        raise FixtureExportError(f"{name} contains a non-finite number")
    norm = math.sqrt(sum(item * item for item in vector))
    if abs(norm - 1.0) > 0.001:
        raise FixtureExportError(f"{name} is not L2-normalized (norm={norm:.6f})")
    return vector


def validate_media(
    payload: bytes,
    mimetype: str,
    name: str,
    *,
    expected_dimensions: tuple[int, int] | None = None,
) -> None:
    valid = {
        "image/png": payload.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": payload.startswith(b"\xff\xd8\xff"),
        "image/gif": payload.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": (
            len(payload) >= 12
            and payload.startswith(b"RIFF")
            and payload[8:12] == b"WEBP"
        ),
        "image/avif": (
            len(payload) >= 16
            and payload[4:8] == b"ftyp"
            and any(
                brand in payload[8 : min(len(payload), 32)]
                for brand in (b"avif", b"avis")
            )
        ),
    }
    if not valid.get(mimetype, False):
        raise FixtureExportError(
            f'object "{name}" bytes do not match MIME type "{mimetype}"'
        )
    if mimetype == "image/webp":
        webp_dimensions = _validate_webp(payload, name)
        if expected_dimensions is not None and webp_dimensions != expected_dimensions:
            raise FixtureExportError(
                f'object "{name}" dimensions do not match its WebP bytes'
            )
    try:
        with Image.open(BytesIO(payload)) as image:
            image.load()
            decoded_dimensions = image.size
            decoded_mimetype = Image.MIME.get(image.format)
            image_info = image.info
            image_text = getattr(image, "text", {})
            has_exif = bool(image.getexif())
    except (OSError, SyntaxError, UnidentifiedImageError) as error:
        raise FixtureExportError(f'object "{name}" cannot be decoded') from error
    if decoded_mimetype != mimetype:
        raise FixtureExportError(
            f'object "{name}" decoded MIME type does not match "{mimetype}"'
        )
    if expected_dimensions is not None and decoded_dimensions != expected_dimensions:
        raise FixtureExportError(
            f'object "{name}" decoded dimensions do not match its metadata'
        )
    if (
        has_exif
        or image_text
        or any(
            key.lower() in {"comment", "exif", "iptc", "photoshop", "xmp"}
            or "xmp" in key.lower()
            for key in image_info
        )
    ):
        raise FixtureExportError(
            f'object "{name}" contains embedded descriptive or author metadata'
        )


def _validate_webp(payload: bytes, name: str) -> tuple[int, int]:
    """Validate WebP framing, dimensions, and privacy-sensitive chunks."""

    if int.from_bytes(payload[4:8], "little") + 8 != len(payload):
        raise FixtureExportError(
            f'object "{name}" has invalid WebP framing or trailing data'
        )
    offset = 12
    dimensions: tuple[int, int] | None = None
    while offset + 8 <= len(payload):
        chunk = payload[offset : offset + 4]
        chunk_size = int.from_bytes(payload[offset + 4 : offset + 8], "little")
        chunk_end = offset + 8 + chunk_size
        if chunk_end > len(payload):
            raise FixtureExportError(f'object "{name}" has a malformed WebP chunk')
        if chunk in {b"EXIF", b"XMP "}:
            raise FixtureExportError(
                f'object "{name}" contains embedded EXIF or XMP metadata'
            )
        data = payload[offset + 8 : chunk_end]
        if chunk == b"VP8X":
            if len(data) < 10:
                raise FixtureExportError(
                    f'object "{name}" has malformed WebP dimensions'
                )
            dimensions = (
                int.from_bytes(data[4:7], "little") + 1,
                int.from_bytes(data[7:10], "little") + 1,
            )
        elif chunk == b"VP8L":
            if len(data) < 5 or data[0] != 0x2F:
                raise FixtureExportError(
                    f'object "{name}" has malformed WebP dimensions'
                )
            packed = int.from_bytes(data[1:5], "little")
            dimensions = ((packed & 0x3FFF) + 1, ((packed >> 14) & 0x3FFF) + 1)
        elif chunk == b"VP8 ":
            if len(data) < 10 or data[3:6] != b"\x9d\x01\x2a":
                raise FixtureExportError(
                    f'object "{name}" has malformed WebP dimensions'
                )
            dimensions = (
                int.from_bytes(data[6:8], "little") & 0x3FFF,
                int.from_bytes(data[8:10], "little") & 0x3FFF,
            )
        offset = chunk_end + (chunk_size % 2)
    if offset != len(payload) or dimensions is None or 0 in dimensions:
        raise FixtureExportError(f'object "{name}" has malformed WebP contents')
    return dimensions


def _reject_sensitive_metadata(
    objects: Sequence[JsonObject], categories: Sequence[JsonObject]
) -> None:
    serialized = json.dumps(
        {"categories": categories, "objects": objects},
        ensure_ascii=False,
        sort_keys=True,
    )
    if (
        UUID_RE.search(serialized)
        or EMAIL_RE.search(serialized)
        or "http://" in serialized
        or "https://" in serialized
        or "sb_secret_" in serialized
        or "/storage/v1/object/" in serialized
        or "eyJhbGciOi" in serialized
    ):
        raise FixtureExportError(
            "archive metadata contains a production identifier, URL, or credential"
        )


class FixtureArchive:
    """Writes and validates the portable fixture archive."""

    def write(
        self,
        output: Path,
        *,
        manifest: JsonObject,
        readme: str,
        objects: Sequence[JsonObject],
        embeddings: Sequence[JsonObject],
        categories: Sequence[JsonObject],
        assets: Mapping[str, bytes],
    ) -> str:
        if output.suffix.lower() != ".zip":
            raise FixtureExportError("fixture archive output must end in .zip")
        output.parent.mkdir(parents=True, exist_ok=True)

        checksum_file = Path(f"{output}.sha256")
        temporary_archive: Path | None = None
        temporary_checksum: Path | None = None
        with TemporaryDirectory(prefix="grida-library-fixture-") as temp:
            root = Path(temp)
            files: dict[str, bytes] = {
                "README.md": readme.encode("utf-8"),
                "objects.jsonl": jsonl_bytes(objects),
                "embeddings.jsonl": jsonl_bytes(embeddings),
                "categories.json": json_bytes(categories, pretty=True),
                **dict(assets),
            }
            checksums = "".join(
                f"{digest_bytes(payload)}  {relative_path}\n"
                for relative_path, payload in sorted(files.items())
            ).encode("utf-8")
            files["checksums.sha256"] = checksums

            inventory: dict[str, JsonObject] = {}
            for relative_path, payload in sorted(files.items()):
                self._validate_relative_path(relative_path)
                destination = root / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
                inventory[relative_path] = {
                    "bytes": len(payload),
                    "sha256": digest_bytes(payload),
                }

            complete_manifest = {**manifest, "files": inventory}
            manifest_payload = json_bytes(complete_manifest, pretty=True)
            (root / "manifest.json").write_bytes(manifest_payload)

            with NamedTemporaryFile(
                prefix=f".{output.name}.",
                suffix=".tmp",
                dir=output.parent,
                delete=False,
            ) as temporary:
                temporary_archive = Path(temporary.name)
            try:
                with zipfile.ZipFile(
                    temporary_archive,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                ) as archive:
                    for path in sorted(root.rglob("*")):
                        if not path.is_file():
                            continue
                        relative = path.relative_to(root).as_posix()
                        info = zipfile.ZipInfo(relative, ZIP_EPOCH)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = 0o100644 << 16
                        archive.writestr(info, path.read_bytes())
                archive_sha256 = digest_file(temporary_archive)
                self.verify(temporary_archive, expected_sha256=archive_sha256)

                with NamedTemporaryFile(
                    mode="w",
                    prefix=f".{checksum_file.name}.",
                    suffix=".tmp",
                    dir=checksum_file.parent,
                    delete=False,
                    encoding="utf-8",
                ) as temporary:
                    temporary.write(f"{archive_sha256}  {output.name}\n")
                    temporary_checksum = Path(temporary.name)

                # A crash between the two replaces leaves no sidecar, never a
                # stale checksum that appears to authenticate a new archive.
                checksum_file.unlink(missing_ok=True)
                os.replace(temporary_archive, output)
                temporary_archive = None
                os.replace(temporary_checksum, checksum_file)
                temporary_checksum = None
                self.verify_with_sidecar(output)
            finally:
                if temporary_archive is not None:
                    temporary_archive.unlink(missing_ok=True)
                if temporary_checksum is not None:
                    temporary_checksum.unlink(missing_ok=True)

        return archive_sha256

    def verify_with_sidecar(
        self, archive_path: Path, checksum_file: Path | None = None
    ) -> JsonObject:
        checksum_file = checksum_file or Path(f"{archive_path}.sha256")
        expected = self.read_checksum_file(
            checksum_file, expected_filename=archive_path.name
        )
        return self.verify(archive_path, expected_sha256=expected)

    def verify(self, archive_path: Path, *, expected_sha256: str) -> JsonObject:
        if not SHA256_RE.fullmatch(expected_sha256):
            raise FixtureExportError("expected archive SHA-256 is invalid")
        try:
            actual_sha256 = digest_file(archive_path)
        except OSError as error:
            raise FixtureExportError("fixture archive could not be read") from error
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise FixtureExportError("archive does not match its external SHA-256")
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ARCHIVE_ENTRIES:
                    raise FixtureExportError("archive has too many entries")
                if (
                    sum(info.file_size for info in infos)
                    > MAX_ARCHIVE_UNCOMPRESSED_BYTES
                ):
                    raise FixtureExportError("archive is too large when expanded")
                names = [info.filename for info in infos]
                if len(names) != len(set(names)):
                    raise FixtureExportError("archive contains duplicate paths")
                for info in infos:
                    self._validate_entry(info)
                    maximum = CONTROL_ENTRY_MAX_BYTES.get(info.filename)
                    if maximum is not None and info.file_size > maximum:
                        raise FixtureExportError(
                            f'archive entry "{info.filename}" is too large'
                        )
                if "manifest.json" not in names:
                    raise FixtureExportError("archive manifest.json is missing")
                manifest = json.loads(archive.read("manifest.json"))
                if not isinstance(manifest, dict):
                    raise FixtureExportError("archive manifest.json is invalid")
                if manifest.get("format") != FORMAT:
                    raise FixtureExportError("archive format is unsupported")
                if manifest.get("format_version") != FORMAT_VERSION:
                    raise FixtureExportError("archive format version is unsupported")

                inventory = manifest.get("files")
                if not isinstance(inventory, dict):
                    raise FixtureExportError("archive file inventory is missing")
                expected = set(inventory) | {"manifest.json"}
                if set(names) != expected:
                    raise FixtureExportError(
                        "archive entries do not match the manifest"
                    )
                for name, expected_file in inventory.items():
                    if not isinstance(expected_file, dict):
                        raise FixtureExportError(
                            f'archive inventory entry "{name}" is invalid'
                        )
                    if set(expected_file) != {"bytes", "sha256"}:
                        raise FixtureExportError(
                            f'archive inventory entry "{name}" is invalid'
                        )
                    if (
                        not isinstance(expected_file.get("bytes"), int)
                        or isinstance(expected_file.get("bytes"), bool)
                        or expected_file["bytes"] < 0
                        or not isinstance(expected_file.get("sha256"), str)
                        or not SHA256_RE.fullmatch(expected_file["sha256"])
                    ):
                        raise FixtureExportError(
                            f'archive inventory entry "{name}" is invalid'
                        )
                    payload = archive.read(name)
                    if len(payload) != expected_file.get("bytes"):
                        raise FixtureExportError(
                            f'archive entry "{name}" has the wrong byte size'
                        )
                    if digest_bytes(payload) != expected_file.get("sha256"):
                        raise FixtureExportError(
                            f'archive entry "{name}" failed SHA-256 verification'
                        )
                self._verify_checksum_list(archive)
                self._verify_contract(archive, manifest)
                return manifest
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as error:
            raise FixtureExportError("fixture archive is not valid") from error

    @staticmethod
    def read_checksum_file(checksum_file: Path, *, expected_filename: str) -> str:
        try:
            lines = checksum_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as error:
            raise FixtureExportError("archive checksum sidecar is missing") from error
        if len(lines) != 1:
            raise FixtureExportError("archive checksum sidecar is invalid")
        digest, separator, filename = lines[0].partition("  ")
        if (
            not separator
            or not SHA256_RE.fullmatch(digest)
            or filename != expected_filename
        ):
            raise FixtureExportError("archive checksum sidecar is invalid")
        return digest

    @staticmethod
    def _verify_checksum_list(archive: zipfile.ZipFile) -> None:
        try:
            lines = archive.read("checksums.sha256").decode("utf-8").splitlines()
        except (KeyError, UnicodeDecodeError) as error:
            raise FixtureExportError("archive checksums.sha256 is invalid") from error
        checksums: dict[str, str] = {}
        for line in lines:
            digest, separator, name = line.partition("  ")
            if not separator or len(digest) != 64 or name in checksums or not name:
                raise FixtureExportError("archive checksums.sha256 is invalid")
            checksums[name] = digest
        expected_names = set(archive.namelist()) - {
            "manifest.json",
            "checksums.sha256",
        }
        if set(checksums) != expected_names:
            raise FixtureExportError(
                "archive checksum list does not cover every content entry"
            )
        for name, expected in checksums.items():
            if digest_bytes(archive.read(name)) != expected:
                raise FixtureExportError(
                    f'archive entry "{name}" failed checksum-list verification'
                )

    @staticmethod
    def _verify_contract(archive: zipfile.ZipFile, manifest: JsonObject) -> None:
        required = {
            "README.md",
            "manifest.json",
            "checksums.sha256",
            "objects.jsonl",
            "embeddings.jsonl",
            "categories.json",
        }
        if not required.issubset(archive.namelist()):
            raise FixtureExportError("archive is missing required corpus entries")

        readme = archive.read("README.md")
        if not readme.startswith(b"# Grida Library developer corpus\n"):
            raise FixtureExportError("archive README.md is invalid")
        objects = FixtureArchive._read_jsonl(archive, "objects.jsonl")
        embeddings = FixtureArchive._read_jsonl(archive, "embeddings.jsonl")
        try:
            categories = json.loads(archive.read("categories.json"))
        except json.JSONDecodeError as error:
            raise FixtureExportError("archive categories.json is invalid") from error
        if not isinstance(categories, list):
            raise FixtureExportError("archive categories.json is invalid")
        _reject_sensitive_metadata(objects, categories)

        expected_manifest_fields = {
            "asset_addressing",
            "category",
            "embedding",
            "files",
            "format",
            "format_version",
            "license_metadata",
            "licenses",
            "object_count",
        }
        if set(manifest) != expected_manifest_fields:
            raise FixtureExportError("archive manifest fields are invalid")
        if manifest.get("asset_addressing") != {
            "algorithm": "sha256",
            "path_template": "assets/{sha256}.{ext}",
        }:
            raise FixtureExportError("archive asset-addressing contract is invalid")

        object_count = manifest.get("object_count")
        if (
            not isinstance(object_count, int)
            or object_count < 1
            or object_count > MAX_OBJECTS
            or object_count != len(objects)
        ):
            raise FixtureExportError("archive object count is invalid")
        if len(embeddings) != object_count:
            raise FixtureExportError("archive embedding count is invalid")

        embedding_contract = manifest.get("embedding")
        if not isinstance(embedding_contract, dict) or embedding_contract != {
            "dimensions": EMBEDDING_DIMENSIONS,
            "image_column": "gemini_embedding_2__image",
            "image_required": True,
            "metric": "cosine",
            "model": EMBEDDING_MODEL,
            "normalized": EMBEDDING_NORMALIZED,
            "text_column": "gemini_embedding_2__text",
            "text_optional": True,
        }:
            raise FixtureExportError("archive embedding contract is invalid")
        license_metadata = manifest.get("license_metadata")
        if license_metadata != {
            "notice": LICENSE_NOTICE,
            "project_policy": LIBRARY_LICENSE_POLICY,
            "references": LICENSE_REFERENCES,
            "supported_source_values": list(SUPPORTED_LICENSE_VALUES),
        }:
            raise FixtureExportError("archive license metadata is invalid")

        category_ids: set[str] = set()
        for category in categories:
            if not isinstance(category, dict) or set(category) != {
                "id",
                "name",
                "description",
            }:
                raise FixtureExportError("archive category record is invalid")
            category_id = category.get("id")
            category_name = category.get("name")
            category_description = category.get("description")
            if (
                not isinstance(category_id, str)
                or not category_id
                or not isinstance(category_name, str)
                or not category_name
                or (
                    category_description is not None
                    and not isinstance(category_description, str)
                )
            ):
                raise FixtureExportError("archive category record is invalid")
            if category_id in category_ids:
                raise FixtureExportError("archive category IDs are not unique")
            category_ids.add(category_id)

        object_refs: set[str] = set()
        asset_paths: set[str] = set()
        referenced_category_ids: set[str] = set()
        license_counts: Counter[str] = Counter()
        for row in objects:
            ref = row.get("ref")
            sha256 = row.get("sha256")
            asset_path = row.get("asset_path")
            metadata = row.get("metadata")
            if (
                set(row) != {"asset_path", "metadata", "ref", "sha256"}
                or not isinstance(ref, str)
                or not isinstance(sha256, str)
                or not SHA256_RE.fullmatch(sha256)
                or ref != f"sha256:{sha256}"
                or not isinstance(asset_path, str)
                or not isinstance(metadata, dict)
            ):
                raise FixtureExportError("archive object record is invalid")
            if set(metadata) != set(OBJECT_METADATA_FIELDS):
                raise FixtureExportError("archive object metadata fields are invalid")
            if ref in object_refs or asset_path in asset_paths:
                raise FixtureExportError("archive object references are not unique")
            mimetype = metadata.get("mimetype")
            extension = MIMETYPE_EXTENSIONS.get(mimetype)
            if extension is None or asset_path != f"assets/{sha256}.{extension}":
                raise FixtureExportError("archive object asset path is invalid")
            license_name = metadata.get("license")
            if license_name not in SUPPORTED_LICENSE_VALUES:
                raise FixtureExportError("archive object license is invalid")
            license_counts[license_name] += 1
            if metadata.get("category") not in category_ids:
                raise FixtureExportError("archive object category is invalid")
            referenced_category_ids.add(metadata["category"])
            for dimension in ("width", "height"):
                value = metadata.get(dimension)
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise FixtureExportError("archive object dimensions are invalid")
            try:
                info = archive.getinfo(asset_path)
            except KeyError as error:
                raise FixtureExportError("archive object asset is missing") from error
            metadata_bytes = metadata.get("bytes")
            if (
                info.file_size > LIBRARY_MAX_ASSET_BYTES
                or not isinstance(metadata_bytes, int)
                or isinstance(metadata_bytes, bool)
                or metadata_bytes != info.file_size
            ):
                raise FixtureExportError("archive object byte size is invalid")
            asset_payload = archive.read(asset_path)
            if digest_bytes(asset_payload) != sha256:
                raise FixtureExportError("archive asset content address is invalid")
            validate_media(
                asset_payload,
                mimetype,
                asset_path,
                expected_dimensions=(
                    metadata["width"],
                    metadata["height"],
                ),
            )
            object_refs.add(ref)
            asset_paths.add(asset_path)
        if referenced_category_ids != category_ids:
            raise FixtureExportError("archive category set is invalid")
        if manifest.get("licenses") != dict(sorted(license_counts.items())):
            raise FixtureExportError("archive license counts are invalid")

        archive_assets = {
            name for name in archive.namelist() if name.startswith("assets/")
        }
        if archive_assets != asset_paths:
            raise FixtureExportError("archive asset set is invalid")
        if set(archive.namelist()) != required | asset_paths:
            raise FixtureExportError("archive contains unsupported entries")

        embedding_refs: set[str] = set()
        for row in embeddings:
            if set(row) != {"image", "object_ref", "text"}:
                raise FixtureExportError("archive embedding record is invalid")
            ref = row.get("object_ref")
            if not isinstance(ref, str) or ref in embedding_refs:
                raise FixtureExportError("archive embedding reference is invalid")
            FixtureArchive._verify_archived_vector(
                row.get("image"),
                name=f"{ref} image embedding",
                required=True,
            )
            FixtureArchive._verify_archived_vector(
                row.get("text"),
                name=f"{ref} text embedding",
                required=False,
            )
            embedding_refs.add(ref)
        if embedding_refs != object_refs:
            raise FixtureExportError("archive embeddings do not match its objects")

        category = manifest.get("category")
        if not isinstance(category, dict) or set(category) != {
            "description",
            "id",
            "name",
            "ref",
        }:
            raise FixtureExportError("archive category metadata is invalid")
        category_id = category.get("id")
        name = category.get("name")
        description = category.get("description")
        if (
            not isinstance(category_id, str)
            or not category_id
            or not isinstance(name, str)
            or not name
            or (description is not None and not isinstance(description, str))
            or category.get("ref") != f"category:{category_id}"
            or category_ids != {category_id}
        ):
            raise FixtureExportError("archive category metadata is invalid")
        if categories != [
            {
                "description": description,
                "id": category_id,
                "name": name,
            }
        ]:
            raise FixtureExportError(
                "archive category manifest and record do not match"
            )

    @staticmethod
    def _verify_archived_vector(value: Any, *, name: str, required: bool) -> None:
        if value is None:
            if required:
                raise FixtureExportError(f"{name} is missing")
            return
        if not isinstance(value, list) or any(
            isinstance(item, bool) or not isinstance(item, (int, float))
            for item in value
        ):
            raise FixtureExportError(f"{name} is not a canonical numeric vector")
        normalize_vector(value, name=name, required=required)

    @staticmethod
    def _read_jsonl(archive: zipfile.ZipFile, name: str) -> list[JsonObject]:
        try:
            text = archive.read(name).decode("utf-8")
            rows = [json.loads(line) for line in text.splitlines() if line]
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FixtureExportError(f'archive entry "{name}" is invalid') from error
        if not all(isinstance(row, dict) for row in rows):
            raise FixtureExportError(f'archive entry "{name}" is invalid')
        return rows

    @staticmethod
    def _validate_entry(info: zipfile.ZipInfo) -> None:
        value = info.filename
        path = PurePosixPath(value)
        mode = info.external_attr >> 16
        if (
            not value
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or path.is_absolute()
            or "." in path.parts
            or ".." in path.parts
            or path.as_posix() != value
            or ":" in path.parts[0]
            or info.is_dir()
            or info.flag_bits & 0x1
            or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or (mode and not stat.S_ISREG(mode))
        ):
            raise FixtureExportError(f'unsafe archive path "{value}"')

    @staticmethod
    def _validate_relative_path(value: str) -> None:
        pseudo_info = zipfile.ZipInfo(value, ZIP_EPOCH)
        pseudo_info.external_attr = 0o100644 << 16
        FixtureArchive._validate_entry(pseudo_info)
