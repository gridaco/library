from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from click.testing import CliRunner

from worker.src.library_storage import LibraryStorage, LibraryStorageError
from worker.tests.test_library_storage import png_bytes


def load_script(name, filename):
    # Model HTTP and image-analysis dependencies are outside this byte-storage
    # test; keep their existing interfaces, with deterministic local outputs.
    dependencies = {
        "embedding": SimpleNamespace(embed_image=Mock(), embed_text=Mock()),
        "metadata": SimpleNamespace(object_metadata=Mock()),
        "embedding_transform": SimpleNamespace(
            b64=lambda data, mime: base64.b64encode(data).decode()
        ),
        "library_storage": sys.modules[LibraryStorage.__module__],
    }
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[1] / "src" / filename
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, dependencies):
        spec.loader.exec_module(module)
    return module


worker_module = load_script("storage_test_worker", "worker.py")
cli_module = load_script("storage_test_cli", "cli.py")


class StorageConsumersTest(unittest.TestCase):
    def setUp(self):
        self.data = png_bytes()
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.asset = {
            "path": "legacy/blue.png",
            "sha256": self.digest,
            "mimetype": "image/png",
            "bytes": len(self.data),
        }
        self.payload = {
            "object_id": "catalog-uuid",
            "path": "legacy/blue.png",
            "mimetype": "image/jpeg",
        }
        self.worker = worker_module.EmbeddingWorker.__new__(
            worker_module.EmbeddingWorker
        )
        self.worker.storage = LibraryStorage(provider="tigris")
        self.worker.library_client = Mock()
        self.query = self.worker.library_client.table.return_value
        self.query.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = SimpleNamespace(
            data=self.asset
        )
        worker_module.running = True
        self.addCleanup(setattr, worker_module, "running", True)

    @patch("worker.src.library_storage.urlopen")
    def test_existing_queue_payload_resolves_catalog_hash_and_original_mime(
        self, open_url
    ):
        open_url.return_value = BytesIO(self.data)
        self.assertEqual(
            self.worker.load_object(self.payload), (self.data, "image/png")
        )
        self.query.select.assert_called_once_with("path,sha256,mimetype,bytes")
        self.query.select.return_value.eq.assert_called_once_with("id", "catalog-uuid")
        open_url.assert_called_once_with(
            f"https://library.grida.co/{self.digest}.png", timeout=60
        )
        self.assertEqual(self.payload["path"], "legacy/blue.png")

    def test_default_worker_keeps_existing_quality_transform(self):
        sb = Mock()
        sb.storage.from_.return_value.download.return_value = self.data
        self.worker.storage = LibraryStorage(supabase=sb)
        self.assertEqual(
            self.worker.load_object(self.payload), (self.data, "image/jpeg")
        )
        sb.storage.from_.return_value.download.assert_called_once_with(
            "legacy/blue.png", options={"transform": {"quality": 80}}
        )
        self.worker.library_client.table.assert_not_called()

    @patch("worker.src.library_storage.urlopen")
    def test_missing_hash_fails_without_storage_fallback_or_fetch(self, open_url):
        self.query.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = SimpleNamespace(
            data={**self.asset, "sha256": None}
        )
        with self.assertRaises(LibraryStorageError):
            self.worker.load_object(self.payload)
        open_url.assert_not_called()

    @patch("worker.src.library_storage.urlopen")
    def test_worker_preserves_embedding_metadata_and_ack_contract(self, open_url):
        open_url.return_value = BytesIO(self.data)
        self.worker.q_read = Mock(
            return_value=[{"msg_id": 7, "message": dict(self.payload)}]
        )
        self.worker.has_embedding = Mock(return_value=False)
        self.worker.get_object_text = Mock(
            return_value={
                "title": "Blue",
                "description": "Square",
                "keywords": ["blue"],
            }
        )
        self.worker.upsert_embedding = Mock()
        self.worker.update_metadata = Mock()

        def acknowledge(message_id):
            worker_module.running = False

        self.worker.q_ack = Mock(side_effect=acknowledge)
        with (
            patch.object(
                worker_module, "embed_image", return_value=[1.0, 0.0]
            ) as image,
            patch.object(worker_module, "embed_text", return_value=[0.0, 1.0]) as text,
            patch.object(
                worker_module, "object_metadata", return_value={"color": "#0000ff"}
            ) as metadata,
        ):
            self.worker.run()
        image.assert_called_once_with(self.data, "image/png")
        text.assert_called_once_with("Blue. Square. blue")
        metadata.assert_called_once_with(self.data, "image/png")
        self.worker.upsert_embedding.assert_called_once_with(
            "catalog-uuid", [1.0, 0.0], [0.0, 1.0]
        )
        self.worker.update_metadata.assert_called_once_with(
            "catalog-uuid", {"color": "#0000ff"}
        )
        self.worker.q_ack.assert_called_once_with(7)

    @patch("worker.src.library_storage.urlopen")
    def test_bad_download_is_not_acknowledged_or_sent_to_models(self, open_url):
        open_url.return_value = BytesIO(b"wrong")

        def read_once():
            worker_module.running = False
            return [{"msg_id": 7, "message": self.payload}]

        self.worker.q_read = read_once
        self.worker.q_ack = Mock()
        with (
            patch.object(worker_module, "embed_image") as image,
            patch.object(worker_module, "object_metadata") as metadata,
        ):
            self.worker.run()
        self.worker.q_ack.assert_not_called()
        image.assert_not_called()
        metadata.assert_not_called()

    def test_missing_catalog_row_preserves_failed_message_for_retry(self):
        self.query.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = SimpleNamespace(
            data=None
        )

        def read_once():
            worker_module.running = False
            return [{"msg_id": 7, "message": self.payload}]

        self.worker.q_read = read_once
        self.worker.q_ack = Mock()
        self.worker.run()
        self.worker.q_ack.assert_not_called()

    @patch("worker.src.library_storage.urlopen")
    def test_batch_dump_uses_hash_download_and_preserves_record_uuid_and_shape(
        self, open_url
    ):
        open_url.return_value = BytesIO(self.data)
        sb = Mock()
        library = sb.schema.return_value
        table = library.table.return_value
        table.select.return_value.limit.return_value.execute.return_value = (
            SimpleNamespace(count=1)
        )
        pages = table.select.return_value.order.return_value.range.return_value.execute
        pages.side_effect = [
            SimpleNamespace(data=[{"id": "catalog-uuid", **self.asset}]),
            SimpleNamespace(data=[]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "batch.jsonl"
            with (
                patch.object(cli_module, "create_client", return_value=sb),
                patch.object(
                    cli_module.LibraryStorage,
                    "from_environment",
                    return_value=LibraryStorage(provider="tigris"),
                ),
            ):
                result = CliRunner().invoke(
                    cli_module.cli,
                    [
                        "prepare",
                        "dump",
                        "--supabase-url",
                        "http://127.0.0.1:54321",
                        "--supabase-key",
                        "local-test-key",
                        "--output",
                        str(output),
                    ],
                )
            self.assertEqual(result.exit_code, 0, result.output)
            row = json.loads(output.read_text())
        self.assertEqual(
            row,
            {
                "recordId": "catalog-uuid",
                "modelInput": {
                    "inputImage": base64.b64encode(self.data).decode(),
                    "embeddingConfig": {"outputEmbeddingLength": 1024},
                },
            },
        )
        sb.storage.from_.assert_not_called()
        open_url.assert_called_once_with(
            f"https://library.grida.co/{self.digest}.png", timeout=60
        )


if __name__ == "__main__":
    unittest.main()
