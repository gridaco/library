from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image

from fixture_release import FixtureExporter, FixtureExportError
from fixture_release.archive import (
    ZIP_EPOCH,
    FixtureArchive,
    digest_bytes,
    digest_file,
    json_bytes,
)

VECTOR = [1.0, *([0.0] * 1535)]


def png_payload(color: str = "blue") -> bytes:
    output = BytesIO()
    Image.new("RGB", (128, 128), color).save(output, format="PNG")
    return output.getvalue()


class FakeSource:
    def __init__(
        self,
        *,
        payload: bytes | None = None,
        license_name: str = "CC0-1.0",
        recorded_sha256: str | None = None,
        vector: list[float] | None = None,
        mimetype: str = "image/png",
    ):
        payload = payload if payload is not None else png_payload()
        digest = hashlib.sha256(payload).hexdigest()
        self.payload = payload
        self.last_limit: int | None = None
        self.category = {
            "id": "generated",
            "name": "Developer corpus",
            "description": "Representative local Library data.",
        }
        self.object = {
            "id": "production-object-id",
            "path": "private/production/path.png",
            "sha256": digest if recorded_sha256 is None else recorded_sha256,
            "title": "Blue square",
            "alt": "A blue square",
            "description": "A minimal blue square on a white background.",
            "category": "generated",
            "categories": ["generated"],
            "objects": ["square"],
            "keywords": ["blue", "minimal"],
            "mimetype": mimetype,
            "width": 128,
            "height": 128,
            "bytes": len(payload),
            "license": license_name,
            "version": 1,
            "fill": None,
            "color": "#0000ff",
            "colors": ["#0000ff"],
            "background": "#ffffff",
            "score": 0.8,
            "year": 2026,
            "entropy": 0.1,
            "orientation": "square",
            "gravity_x": 0.5,
            "gravity_y": 0.5,
            "lang": "en",
            "transparency": False,
            "public_domain": False,
            "priority": None,
            "author": {
                "name": "Fixture Author",
                "username": "fixture-author",
                "provider": "fixture",
                "blog": "https://example.com",
                "user_id": "must-not-leak",
            },
            "object_embedding": {
                "gemini_embedding_2__image": VECTOR if vector is None else vector,
                "gemini_embedding_2__text": VECTOR,
                "created_at": "2026-07-29T00:00:00+00:00",
            },
        }

    def resolve_category(self, identifier):
        if identifier not in (self.category["id"], self.category["name"]):
            raise FixtureExportError("not found")
        return self.category

    def list_category_object_ids(self, category_id):
        self.assert_category_id = category_id
        return [self.object["id"]]

    def iter_objects(self, object_ids):
        if self.object["id"] in object_ids:
            yield self.object

    def download(self, path):
        if path != self.object["path"]:
            raise AssertionError(path)
        return self.payload


class MultiSource(FakeSource):
    def __init__(self):
        super().__init__()
        self.objects = []
        self.payloads = {}
        for object_id, score, title, color in (
            ("production-low", 0.1, "Low score", "black"),
            ("production-high", 0.9, "High score", "white"),
        ):
            payload = png_payload(color)
            row = {
                **self.object,
                "id": object_id,
                "path": f"{object_id}.png",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
                "score": score,
                "title": title,
            }
            self.objects.append(row)
            self.payloads[row["path"]] = payload

    def list_category_object_ids(self, category_id):
        self.assert_category_id = category_id
        return [row["id"] for row in self.objects]

    def iter_objects(self, object_ids):
        for row in self.objects:
            if row["id"] in object_ids:
                yield row

    def download(self, path):
        return self.payloads[path]


class FixtureExporterTest(unittest.TestCase):
    def export(self, source: FakeSource, *, limit=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        output = Path(temp.name) / "fixture.zip"
        summary = FixtureExporter(source).export(
            "Developer corpus", output, limit=limit
        )
        return output, summary

    def rewrite_valid_archive(self, output, mutate):
        with zipfile.ZipFile(output) as archive:
            files = {
                name: archive.read(name)
                for name in archive.namelist()
                if name not in {"manifest.json", "checksums.sha256"}
            }
            manifest = json.loads(archive.read("manifest.json"))

        mutate(manifest, files)
        checksums = "".join(
            f"{digest_bytes(payload)}  {name}\n"
            for name, payload in sorted(files.items())
        ).encode()
        files["checksums.sha256"] = checksums
        manifest["files"] = {
            name: {"bytes": len(payload), "sha256": digest_bytes(payload)}
            for name, payload in sorted(files.items())
        }

        with zipfile.ZipFile(
            output,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, payload in sorted(
                {**files, "manifest.json": json_bytes(manifest, pretty=True)}.items()
            ):
                info = zipfile.ZipInfo(name, ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, payload)
        Path(f"{output}.sha256").write_text(
            f"{digest_file(output)}  {output.name}\n",
            encoding="utf-8",
        )

    def test_writes_self_describing_archive_without_production_ids(self):
        source = FakeSource()
        output, summary = self.export(source, limit=1)
        digest = hashlib.sha256(source.payload).hexdigest()

        self.assertEqual(summary.object_count, 1)
        self.assertEqual(summary.category_id, "generated")
        self.assertTrue(summary.checksum_file.exists())
        verified = FixtureArchive().verify_with_sidecar(output)
        self.assertEqual(verified["category"]["id"], "generated")
        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
            self.assertIn("README.md", names)
            self.assertIn("manifest.json", names)
            self.assertIn("checksums.sha256", names)
            self.assertIn("objects.jsonl", names)
            self.assertIn("embeddings.jsonl", names)
            self.assertIn("categories.json", names)
            self.assertIn(f"assets/{digest}.png", names)

            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["format_version"], 1)
            self.assertEqual(manifest["object_count"], 1)
            self.assertEqual(manifest["category"]["ref"], "category:generated")

            object_row = json.loads(archive.read("objects.jsonl"))
            embedding_row = json.loads(archive.read("embeddings.jsonl"))
            serialized = json.dumps(object_row)
            self.assertNotIn("production-object-id", serialized)
            self.assertNotIn("production/path", serialized)
            self.assertNotIn("must-not-leak", serialized)
            self.assertEqual(object_row["ref"], f"sha256:{digest}")
            self.assertNotIn("author", object_row)
            self.assertNotIn("priority", object_row["metadata"])
            self.assertEqual(embedding_row["text"], VECTOR)

        checksum, filename = summary.checksum_file.read_text().strip().split("  ", 1)
        self.assertEqual(checksum, summary.archive_sha256)
        self.assertEqual(filename, output.name)

    def test_rejects_downloaded_bytes_that_disagree_with_recorded_sha(self):
        source = FakeSource(recorded_sha256="0" * 64)
        with self.assertRaisesRegex(FixtureExportError, "recorded SHA-256"):
            self.export(source)

    def test_rejects_an_unsupported_source_license_value(self):
        source = FakeSource(license_name="Unsplash License")
        with self.assertRaisesRegex(
            FixtureExportError, "unsupported source license value"
        ):
            self.export(source)

    def test_rejects_a_normalized_alias_of_cc0(self):
        source = FakeSource(license_name="cc0-1.0")
        with self.assertRaisesRegex(
            FixtureExportError, "unsupported source license value"
        ):
            self.export(source)

    def test_accepts_the_grida_library_license_reference(self):
        output, summary = self.export(
            FakeSource(license_name="LicenseRef-GridaLibrary")
        )

        self.assertEqual(summary.object_count, 1)
        manifest = FixtureArchive().verify_with_sidecar(output)
        self.assertEqual(
            manifest["licenses"],
            {"LicenseRef-GridaLibrary": 1},
        )

    def test_rejects_svg_from_the_raster_only_v1_contract(self):
        source = FakeSource(
            payload=b'<svg xmlns="http://www.w3.org/2000/svg"/>',
            mimetype="image/svg+xml",
        )
        with self.assertRaisesRegex(FixtureExportError, "unsupported MIME"):
            self.export(source)

    def test_rejects_webp_with_embedded_user_metadata(self):
        chunk = b"EXIF" + (4).to_bytes(4, "little") + b"user"
        body = b"WEBP" + chunk
        payload = b"RIFF" + len(body).to_bytes(4, "little") + body
        source = FakeSource(payload=payload, mimetype="image/webp")

        with self.assertRaisesRegex(FixtureExportError, "embedded EXIF or XMP"):
            self.export(source)

    def test_rejects_incompatible_embedding_dimension(self):
        source = FakeSource(vector=[1.0, 0.0])
        with self.assertRaisesRegex(FixtureExportError, "2 dimensions"):
            self.export(source)

    def test_archive_is_byte_deterministic(self):
        source = FakeSource()
        first, first_summary = self.export(source)
        first_bytes = first.read_bytes()
        second, second_summary = self.export(FakeSource())

        self.assertEqual(first_bytes, second.read_bytes())
        self.assertEqual(first_summary.archive_sha256, second_summary.archive_sha256)

    def test_verify_rejects_archive_tampering_against_the_external_checksum(self):
        source = FakeSource()
        output, _ = self.export(source)
        with zipfile.ZipFile(output, "a") as archive:
            archive.writestr("unexpected.txt", "tampered")

        with self.assertRaisesRegex(FixtureExportError, "external SHA-256"):
            FixtureArchive().verify_with_sidecar(output)

    def test_verify_requires_the_external_checksum_sidecar(self):
        output, summary = self.export(FakeSource())
        summary.checksum_file.unlink()

        with self.assertRaisesRegex(FixtureExportError, "sidecar is missing"):
            FixtureArchive().verify_with_sidecar(output)

    def test_verify_rejects_a_fully_rehashed_extra_entry(self):
        output, _ = self.export(FakeSource())
        self.rewrite_valid_archive(
            output,
            lambda _manifest, files: files.update({"unexpected.txt": b"extra"}),
        )

        with self.assertRaisesRegex(FixtureExportError, "unsupported entries"):
            FixtureArchive().verify_with_sidecar(output)

    def test_verify_rejects_a_fully_rehashed_license_metadata_change(self):
        output, _ = self.export(FakeSource())

        def mutate(manifest, _files):
            manifest["license_metadata"] = {"supported_source_values": ["Other"]}

        self.rewrite_valid_archive(output, mutate)
        with self.assertRaisesRegex(FixtureExportError, "license metadata"):
            FixtureArchive().verify_with_sidecar(output)

    def test_verify_rejects_category_record_that_differs_from_manifest(self):
        output, _ = self.export(FakeSource())

        def mutate(_manifest, files):
            categories = json.loads(files["categories.json"])
            categories[0]["name"] = "Changed category"
            files["categories.json"] = json_bytes(categories, pretty=True)

        self.rewrite_valid_archive(output, mutate)
        with self.assertRaisesRegex(
            FixtureExportError, "category manifest and record do not match"
        ):
            FixtureArchive().verify_with_sidecar(output)

    def test_verify_rejects_a_fully_rehashed_uuid_in_metadata(self):
        output, _ = self.export(FakeSource())

        def mutate(_manifest, files):
            row = json.loads(files["objects.jsonl"])
            row["metadata"]["description"] = (
                "production row 12345678-1234-1234-1234-123456789abc"
            )
            files["objects.jsonl"] = json_bytes(row)

        self.rewrite_valid_archive(output, mutate)
        with self.assertRaisesRegex(
            FixtureExportError, "production identifier, URL, or credential"
        ):
            FixtureArchive().verify_with_sidecar(output)

    def test_sanitizer_rejects_a_known_production_identifier_in_metadata(self):
        source = FakeSource()
        source.object["description"] = source.object["id"]

        with self.assertRaisesRegex(FixtureExportError, "production identifier"):
            self.export(source)

    def test_content_addressed_source_path_is_not_a_false_positive(self):
        source = FakeSource()
        digest = hashlib.sha256(source.payload).hexdigest()
        source.object["path"] = f"{digest}.png"

        output, summary = self.export(source)

        self.assertEqual(summary.object_count, 1)
        FixtureArchive().verify_with_sidecar(output)

    def test_rejects_object_that_moved_outside_the_requested_category(self):
        source = FakeSource()
        source.object["category"] = "other"

        with self.assertRaisesRegex(
            FixtureExportError, "does not match the requested category"
        ):
            self.export(source)

    def test_default_limit_exports_every_category_member(self):
        source = MultiSource()
        output, summary = self.export(source)

        with zipfile.ZipFile(output) as archive:
            rows = [
                json.loads(line) for line in archive.read("objects.jsonl").splitlines()
            ]

        self.assertEqual(summary.object_count, 2)
        self.assertEqual(
            {row["metadata"]["title"] for row in rows},
            {"High score", "Low score"},
        )

    def test_limit_selects_highest_score(self):
        source = MultiSource()
        output, summary = self.export(source, limit=1)

        with zipfile.ZipFile(output) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            object_row = json.loads(archive.read("objects.jsonl"))

        self.assertEqual(summary.object_count, 1)
        self.assertEqual(object_row["metadata"]["title"], "High score")
        self.assertEqual(manifest["category"]["id"], "generated")


if __name__ == "__main__":
    unittest.main()
