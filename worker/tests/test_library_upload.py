from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
from click.testing import CliRunner

from g.src import upload
from worker.src.library_storage import LibraryStorage, LibraryStorageError
from worker.tests.test_library_storage import png_bytes


class UniqueViolation(RuntimeError):
    code = "23505"


class Catalog:
    """Small SQL-backed catalog adapter with real unique-path arbitration."""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.execute(
            "CREATE TABLE objects (id TEXT PRIMARY KEY, path TEXT UNIQUE, sha256 TEXT, created_at TEXT, payload TEXT)"
        )
        self.lock = threading.RLock()
        self.inserts = 0
        self.updates = 0

    def table(self, name):
        assert name == "object"
        return Query(self)

    def add(self, row):
        with self.lock:
            try:
                self.connection.execute(
                    "INSERT INTO objects VALUES (?, ?, ?, ?, ?)",
                    (
                        row["id"],
                        row["path"],
                        row["sha256"],
                        row.get("created_at", "2026-01-01"),
                        json.dumps(row),
                    ),
                )
                self.connection.commit()
                self.inserts += 1
            except sqlite3.IntegrityError as error:
                raise UniqueViolation("23505 unique catalog path") from error

    def rows(self):
        with self.lock:
            return [
                json.loads(value[0])
                for value in self.connection.execute(
                    "SELECT payload FROM objects ORDER BY id"
                )
            ]


class Query:
    def __init__(self, catalog):
        self.catalog = catalog
        self.filters = []
        self.orders = []
        self.limit_value = None
        self.operation = "select"

    def select(self, fields):
        return self

    def eq(self, field, value):
        assert field in {"id", "path", "sha256"}
        self.filters.append((field, value))
        return self

    def order(self, field):
        assert field in {"created_at", "id"}
        self.orders.append(field)
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def insert(self, row):
        self.operation, self.payload = "insert", row
        return self

    def update(self, row):
        self.operation, self.payload = "update", row
        return self

    def execute(self):
        if self.operation == "insert":
            self.catalog.add(self.payload)
            return SimpleNamespace(data=[self.payload])
        with self.catalog.lock:
            rows = self.catalog.rows()
            rows = [r for r in rows if all(r.get(k) == v for k, v in self.filters)]
            for field in reversed(self.orders):
                rows.sort(key=lambda row: row.get(field, "2026-01-01"))
            if self.limit_value is not None:
                rows = rows[: self.limit_value]
            if self.operation == "update":
                for row in rows:
                    self.catalog.updates += 1
                    row.update(self.payload)
                    self.catalog.connection.execute(
                        "UPDATE objects SET payload=? WHERE id=?",
                        (json.dumps(row), row["id"]),
                    )
                self.catalog.connection.commit()
            return SimpleNamespace(data=rows)


class ConditionalStore:
    def __init__(self, barrier=None):
        self.lock = threading.Lock()
        self.barrier = barrier
        self.objects = {}
        self.puts = []

    def put_object(self, **request):
        if self.barrier:
            self.barrier.wait(timeout=5)
        assert request["IfNoneMatch"] == "*"
        assert (
            base64.b64encode(hashlib.sha256(request["Body"]).digest()).decode()
            == request["ChecksumSHA256"]
        )
        with self.lock:
            self.puts.append(request)
            if request["Key"] in self.objects:
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            self.objects[request["Key"]] = {
                "ContentLength": len(request["Body"]),
                "ContentType": request["ContentType"],
                "ChecksumSHA256": request["ChecksumSHA256"],
                "ChecksumType": "FULL_OBJECT",
                "_bytes": request["Body"],
            }

    def head_object(self, **request):
        with self.lock:
            return dict(self.objects[request["Key"]])

    def get_object(self, **request):
        with self.lock:
            stored = self.objects[request["Key"]]
            return {
                "Body": BytesIO(stored["_bytes"]),
                "ContentLength": stored["ContentLength"],
                "ContentType": stored["ContentType"],
            }


class LibraryUploadTest(unittest.TestCase):
    def setUp(self):
        self.data = png_bytes()
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.metadata = {
            "mimetype": "image/png",
            "bytes": len(self.data),
            "title": "Curated blue",
            "width": 32,
            "height": 32,
            "transparency": False,
            "orientation": "square",
        }
        self.catalog = Catalog()
        self.addCleanup(self.catalog.connection.close)

    def storage(self, store=None):
        return LibraryStorage(
            provider="tigris", s3=store or ConditionalStore(), bucket="local-test"
        )

    def test_new_tigris_catalog_id_is_independent_uuid_and_key_is_full_hash(self):
        object_id, status = upload.register_object(
            self.catalog,
            self.storage(),
            self.data,
            self.metadata,
            "legacy/blue.png",
            ".png",
        )
        from uuid import UUID

        self.assertEqual(str(UUID(object_id)), object_id)
        self.assertEqual(status, "uploaded")
        self.assertEqual(
            self.catalog.rows(),
            [
                {
                    "id": object_id,
                    "path": f"{self.digest}.png",
                    "sha256": self.digest,
                    **self.metadata,
                }
            ],
        )

    def test_supabase_default_retains_storage_response_id(self):
        bucket = Mock()
        bucket.info.return_value = {"id": "existing-storage-uuid"}
        sb = Mock()
        sb.storage.from_.return_value = bucket
        object_id, status = upload.register_object(
            self.catalog,
            LibraryStorage(supabase=sb),
            self.data,
            self.metadata,
            "legacy/blue.png",
            ".png",
        )
        self.assertEqual((object_id, status), ("existing-storage-uuid", "uploaded"))

    def test_historical_duplicate_adoption_is_oldest_then_uuid_and_preserves_other_rows(
        self,
    ):
        for object_id, date in (
            ("z", "2026-01-01"),
            ("b", "2025-01-01"),
            ("a", "2025-01-01"),
        ):
            self.catalog.add(
                {
                    "id": object_id,
                    "path": f"legacy/{object_id}.png",
                    "sha256": self.digest,
                    "created_at": date,
                    **self.metadata,
                    "title": object_id,
                }
            )
        before = self.catalog.rows()
        object_id, status = upload.register_object(
            self.catalog,
            self.storage(),
            self.data,
            self.metadata,
            "legacy/z.png",
            ".png",
        )
        self.assertEqual((object_id, status), ("a", "refreshed"))
        after = self.catalog.rows()
        self.assertEqual(after[1:], before[1:])
        self.assertEqual(after[0]["path"], "legacy/a.png")
        self.assertEqual(after[0]["sha256"], self.digest)
        self.assertEqual(after[0]["title"], "Curated blue")
        self.assertEqual(self.catalog.inserts, 3)
        self.assertEqual(self.catalog.updates, 1)

    def test_legacy_null_sha_can_refresh_only_in_supabase_without_rekeying(self):
        row = {
            "id": "legacy-id",
            "path": "legacy/blue.png",
            "sha256": None,
            **self.metadata,
        }
        self.catalog.add(row)
        storage = LibraryStorage(supabase=Mock())
        self.assertEqual(
            upload.register_object(
                self.catalog, storage, self.data, self.metadata, row["path"], ".png"
            ),
            ("legacy-id", "refreshed"),
        )
        self.assertEqual(self.catalog.rows()[0]["sha256"], None)
        storage.supabase.storage.from_.assert_not_called()
        with self.assertRaisesRegex(LibraryStorageError, "Backfill"):
            upload.register_object(
                self.catalog,
                self.storage(),
                self.data,
                self.metadata,
                row["path"],
                ".png",
            )

    def test_existing_blob_metadata_or_local_size_disagreement_is_rejected(self):
        row = {
            "id": "legacy-id",
            "path": "legacy/blue.png",
            "sha256": "b" * 64,
            **self.metadata,
        }
        self.catalog.add(row)
        with self.assertRaises(LibraryStorageError):
            upload.register_object(
                self.catalog,
                self.storage(),
                self.data,
                self.metadata,
                row["path"],
                ".png",
            )
        self.assertEqual(self.catalog.rows(), [row])
        with self.assertRaisesRegex(LibraryStorageError, "size"):
            upload.register_object(
                self.catalog,
                self.storage(),
                self.data,
                {**self.metadata, "bytes": 1},
                "unused.png",
                ".png",
            )

    def test_two_clients_racing_share_one_immutable_blob_and_one_catalog_winner(self):
        store = ConditionalStore(threading.Barrier(2))

        def register(title):
            return upload.register_object(
                self.catalog,
                self.storage(store),
                self.data,
                {**self.metadata, "title": title},
                "legacy/blue.png",
                ".png",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(register, ("first contender", "second contender")))
        self.assertEqual(len({object_id for object_id, status in results}), 1)
        self.assertEqual(
            {status for object_id, status in results}, {"uploaded", "adopted"}
        )
        self.assertEqual(len(self.catalog.rows()), 1)
        self.assertEqual(self.catalog.inserts, 1)
        self.assertEqual(self.catalog.updates, 0)
        self.assertEqual(len(store.objects), 1)
        self.assertEqual(len(store.puts), 2)

    def test_unrelated_unique_violation_is_not_silently_reported_as_success(self):
        with patch.object(
            self.catalog, "add", side_effect=UniqueViolation("23505 unrelated failure")
        ):
            with self.assertRaises(UniqueViolation):
                upload.register_object(
                    self.catalog,
                    self.storage(),
                    self.data,
                    self.metadata,
                    "legacy/blue.png",
                    ".png",
                )

    def test_matching_reported_checksum_with_wrong_actual_body_cannot_publish(self):
        store = ConditionalStore()
        wrong_body = {
            "Body": BytesIO(b"x" * len(self.data)),
            "ContentLength": len(self.data),
            "ContentType": "image/png",
        }
        with patch.object(store, "get_object", return_value=wrong_body):
            with self.assertRaises(LibraryStorageError):
                upload.register_object(
                    self.catalog,
                    self.storage(store),
                    self.data,
                    self.metadata,
                    "legacy/blue.png",
                    ".png",
                )
        self.assertEqual(self.catalog.rows(), [])
        self.assertEqual(self.catalog.inserts, 0)
        self.assertTrue(wrong_body["Body"].closed)

    def test_failed_catalog_registration_can_retry_the_already_verified_blob(self):
        store = ConditionalStore()
        with patch.object(
            self.catalog,
            "add",
            side_effect=RuntimeError("catalog temporarily unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                upload.register_object(
                    self.catalog,
                    self.storage(store),
                    self.data,
                    self.metadata,
                    "legacy/blue.png",
                    ".png",
                )
        self.assertEqual(self.catalog.rows(), [])
        self.assertEqual(len(store.objects), 1)
        object_id, status = upload.register_object(
            self.catalog,
            self.storage(store),
            self.data,
            self.metadata,
            "legacy/blue.png",
            ".png",
        )
        self.assertEqual((status, self.catalog.inserts), ("uploaded", 1))
        self.assertEqual(self.catalog.rows()[0]["id"], object_id)
        self.assertEqual(len(store.objects), 1)

    def test_cli_reads_existing_metadata_format_and_uses_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "blue.png").write_bytes(self.data)
            (root / "blue.object.json").write_text(json.dumps(self.metadata))
            env = root / "empty.env"
            env.write_text("")
            sb = Mock()
            sb.schema.return_value = self.catalog
            with (
                patch.object(upload, "create_client", return_value=sb),
                patch.object(upload, "load_dotenv"),
                patch.object(
                    upload.LibraryStorage,
                    "from_environment",
                    return_value=self.storage(),
                ),
            ):
                result = CliRunner().invoke(
                    upload.cli,
                    [directory, "fixture", "--type", "png", "--env-file", str(env)],
                )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("uploaded blue.png", result.output)
            self.assertEqual(self.catalog.rows()[0]["category"], "fixture")


if __name__ == "__main__":
    unittest.main()
