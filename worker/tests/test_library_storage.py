from __future__ import annotations

import base64
import hashlib
import os
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import boto3
from botocore.config import Config
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber
from botocore.response import StreamingBody
from PIL import Image

from worker.src.library_storage import (
    EXTENSIONS,
    IMMUTABLE_CACHE,
    MAX_BYTES,
    LibraryStorage,
    LibraryStorageError,
    storage_key,
)


def png_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (32, 32), "blue").save(stream, format="PNG")
    return stream.getvalue()


class DownloadResponse(BytesIO):
    pass


class LibraryStorageTest(unittest.TestCase):
    def setUp(self):
        self.data = png_bytes()
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.checksum = base64.b64encode(bytes.fromhex(self.digest)).decode()
        self.path = f"{self.digest}.png"
        self.asset = {
            "path": "legacy/folder.png",
            "sha256": self.digest,
            "mimetype": "image/png",
            "bytes": len(self.data),
        }
        # Explicit synthetic credentials ensure no discovery or cloud call.
        self.client = boto3.client(
            "s3",
            endpoint_url="https://unused.invalid",
            region_name="auto",
            aws_access_key_id="local-test-key",
            aws_secret_access_key="local-test-secret",
            config=Config(
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        self.stub = Stubber(self.client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)
        self.storage = LibraryStorage(
            provider="tigris", s3=self.client, bucket="test-library"
        )
        self.put = {
            "Bucket": "test-library",
            "Key": self.path,
            "Body": self.data,
            "ContentLength": len(self.data),
            "ContentType": "image/png",
            "CacheControl": IMMUTABLE_CACHE,
            "ChecksumSHA256": self.checksum,
            "IfNoneMatch": "*",
        }
        self.head_request = {
            "Bucket": "test-library",
            "Key": self.path,
            "ChecksumMode": "ENABLED",
        }
        self.head = {
            "ContentLength": len(self.data),
            "ContentType": "image/png",
            "ChecksumSHA256": self.checksum,
            "ChecksumType": "FULL_OBJECT",
        }

    def add_get(self, data=None, **overrides):
        data = self.data if data is None else data
        stream = BytesIO(data)
        self.stub.add_response(
            "get_object",
            {
                "Body": StreamingBody(stream, len(data)),
                "ContentLength": len(self.data),
                "ContentType": "image/png",
                **overrides,
            },
            self.head_request,
        )
        return stream

    def test_pinned_keys_and_input_validation(self):
        for mimetype, extension in EXTENSIONS.items():
            self.assertEqual(
                storage_key(self.digest, mimetype), f"{self.digest}.{extension}"
            )
        for value in (None, "", self.digest.upper(), "../bad", "a" * 63):
            with self.subTest(value=value), self.assertRaises(LibraryStorageError):
                storage_key(value, "image/png")
        with self.assertRaises(LibraryStorageError):
            storage_key(self.digest, "image/jpeg; charset=utf-8")

    def test_default_and_credential_free_reader_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(LibraryStorage.from_environment().provider, "supabase")
        with patch.dict(
            os.environ, {"GRIDA_LIBRARY_STORAGE_PROVIDER": "tigris"}, clear=True
        ):
            reader = LibraryStorage.from_environment()
            self.assertEqual(
                reader.public_url(self.asset), f"https://library.grida.co/{self.path}"
            )
        with self.assertRaises(LibraryStorageError):
            LibraryStorage(provider="typo")

    def test_origins_cannot_smuggle_paths_credentials_or_queries(self):
        for origin in (
            "https://user:secret@example.com",
            "http://example.com",
            "https://example.com/prefix",
            "https://example.com/?secret=x",
            "https://example.com/#fragment",
            "ftp://example.com",
            "https://example.com:bad",
            "https://:443",
            "https://example.com:0",
            "https://example.com\n",
        ):
            with self.subTest(origin=origin), self.assertRaises(LibraryStorageError):
                LibraryStorage(provider="tigris", asset_origin=origin)
        local = LibraryStorage(provider="tigris", asset_origin="http://127.0.0.1:8080")
        self.assertEqual(
            local.public_url(self.asset), f"http://127.0.0.1:8080/{self.path}"
        )

    def test_uploader_client_uses_only_explicit_credentials_and_loopback_path_style(
        self,
    ):
        environment = {
            "GRIDA_LIBRARY_STORAGE_PROVIDER": "tigris",
            "GRIDA_LIBRARY_TIGRIS_BUCKET": "local-test",
            "GRIDA_LIBRARY_TIGRIS_ENDPOINT": "http://127.0.0.1:8080",
            "GRIDA_LIBRARY_TIGRIS_ACCESS_KEY_ID": "local-test-key",
            "GRIDA_LIBRARY_TIGRIS_SECRET_ACCESS_KEY": "local-test-secret",
        }
        with patch.dict(os.environ, environment, clear=True):
            storage = LibraryStorage.from_environment()
            client = storage._client()
        self.assertEqual(client.meta.endpoint_url, "http://127.0.0.1:8080")
        self.assertEqual(client.meta.region_name, "auto")
        self.assertEqual(client.meta.config.s3["addressing_style"], "path")
        self.assertEqual(
            client.meta.config.request_checksum_calculation, "when_required"
        )

    def test_conditional_checksum_put_and_full_object_head(self):
        self.stub.add_response("put_object", {}, self.put)
        self.stub.add_response("head_object", self.head, self.head_request)
        stream = self.add_get()
        stored = self.storage.upload(self.data, "image/png")
        self.assertTrue(stream.closed)
        self.assertEqual(
            (stored.path, stored.sha256, stored.storage_id),
            (self.path, self.digest, None),
        )
        self.stub.assert_no_pending_responses()

    def test_conflict_recovers_only_after_verified_head(self):
        for status in (409, 412):
            with self.subTest(status=status):
                self.stub.add_client_error(
                    "put_object",
                    "PreconditionFailed",
                    http_status_code=status,
                    expected_params=self.put,
                )
                self.stub.add_response("head_object", self.head, self.head_request)
                self.add_get()
                self.assertEqual(
                    self.storage.upload(self.data, "image/png").sha256, self.digest
                )
        self.stub.assert_no_pending_responses()

    def test_manual_upload_without_checksum_metadata_requires_actual_byte_verification(self):
        for omitted in (
            ("ChecksumSHA256", "ChecksumType"),
            ("ChecksumSHA256",),
            ("ChecksumType",),
        ):
            with self.subTest(omitted=omitted):
                self.stub.add_client_error(
                    "put_object",
                    "PreconditionFailed",
                    http_status_code=412,
                    expected_params=self.put,
                )
                head = {
                    key: value for key, value in self.head.items() if key not in omitted
                }
                self.stub.add_response("head_object", head, self.head_request)
                stream = self.add_get()
                self.assertEqual(
                    self.storage.upload(self.data, "image/png").sha256, self.digest
                )
                self.assertTrue(stream.closed)
        self.stub.assert_no_pending_responses()

    def test_manual_upload_without_checksum_metadata_cannot_adopt_wrong_body(self):
        self.stub.add_client_error(
            "put_object",
            "PreconditionFailed",
            http_status_code=412,
            expected_params=self.put,
        )
        self.stub.add_response(
            "head_object",
            {"ContentLength": len(self.data), "ContentType": "image/png"},
            self.head_request,
        )
        stream = self.add_get(b"x" * len(self.data))
        with self.assertRaisesRegex(LibraryStorageError, "stored bytes"):
            self.storage.upload(self.data, "image/png")
        self.assertTrue(stream.closed)
        self.stub.assert_no_pending_responses()

    def test_timeout_after_commit_recovers_without_overwrite(self):
        client = Mock()
        client.put_object.side_effect = EndpointConnectionError(
            endpoint_url="https://unused.invalid"
        )
        client.head_object.return_value = self.head
        stream = BytesIO(self.data)
        client.get_object.return_value = {
            "Body": stream,
            "ContentLength": len(self.data),
            "ContentType": "image/png",
        }
        storage = LibraryStorage(provider="tigris", s3=client, bucket="test-library")
        self.assertEqual(storage.upload(self.data, "image/png").sha256, self.digest)
        client.put_object.assert_called_once_with(**self.put)
        client.head_object.assert_called_once_with(**self.head_request)
        client.get_object.assert_called_once_with(**self.head_request)
        self.assertTrue(stream.closed)

    def test_racing_put_not_visible_yet_retries_conditional_create(self):
        self.stub.add_client_error(
            "put_object",
            "ConditionalRequestConflict",
            http_status_code=409,
            expected_params=self.put,
        )
        self.stub.add_client_error(
            "head_object",
            "NotFound",
            http_status_code=404,
            expected_params=self.head_request,
        )
        self.stub.add_response("put_object", {}, self.put)
        self.stub.add_response("head_object", self.head, self.head_request)
        self.add_get()
        self.storage.upload(self.data, "image/png")
        self.stub.assert_no_pending_responses()

    def test_echoed_matching_head_checksum_cannot_adopt_wrong_same_length_body(self):
        self.stub.add_client_error(
            "put_object",
            "PreconditionFailed",
            http_status_code=412,
            expected_params=self.put,
        )
        self.stub.add_response("head_object", self.head, self.head_request)
        stream = self.add_get(b"x" * len(self.data))
        with self.assertRaisesRegex(LibraryStorageError, "stored bytes"):
            self.storage.upload(self.data, "image/png")
        self.assertTrue(stream.closed)
        self.stub.assert_no_pending_responses()

    def test_get_overflow_truncation_or_metadata_mismatch_fails_and_closes_body(self):
        for body, metadata in (
            (self.data + b"x", {}),
            (self.data[:-1], {}),
            (self.data, {"ContentType": "image/jpeg"}),
            (self.data, {"ContentLength": 1}),
        ):
            with self.subTest(metadata=metadata, length=len(body)):
                self.stub.add_response("put_object", {}, self.put)
                self.stub.add_response("head_object", self.head, self.head_request)
                stream = self.add_get(body, **metadata)
                with self.assertRaises(LibraryStorageError):
                    self.storage.upload(self.data, "image/png")
                self.assertTrue(stream.closed)
        self.stub.assert_no_pending_responses()

    def test_get_read_exception_still_closes_response(self):
        self.stub.add_response("put_object", {}, self.put)
        self.stub.add_response("head_object", self.head, self.head_request)
        body = Mock()
        body.read.side_effect = RuntimeError("interrupted stream")
        self.stub.add_response(
            "get_object",
            {"Body": body, "ContentLength": len(self.data), "ContentType": "image/png"},
            self.head_request,
        )
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.storage.upload(self.data, "image/png")
        body.close.assert_called_once_with()
        self.stub.assert_no_pending_responses()

    def test_unconfirmed_put_is_bounded_and_authorization_errors_do_not_adopt(self):
        for _ in range(3):
            self.stub.add_client_error(
                "put_object",
                "InternalError",
                http_status_code=500,
                expected_params=self.put,
            )
            self.stub.add_client_error(
                "head_object",
                "NotFound",
                http_status_code=404,
                expected_params=self.head_request,
            )
        with self.assertRaises(Exception):
            self.storage.upload(self.data, "image/png")
        self.stub.add_client_error(
            "put_object", "AccessDenied", http_status_code=403, expected_params=self.put
        )
        with self.assertRaises(Exception):
            self.storage.upload(self.data, "image/png")
        self.stub.assert_no_pending_responses()

    def test_conflicting_bytes_type_length_and_multipart_checksums_fail_closed(self):
        for field, bad in (
            ("ContentLength", 1),
            ("ContentType", "image/jpeg"),
            ("ChecksumSHA256", "wrong"),
            ("ChecksumType", "COMPOSITE"),
        ):
            with self.subTest(field=field, value=bad):
                head = {**self.head, field: bad}
                self.stub.add_response("put_object", {}, self.put)
                self.stub.add_response("head_object", head, self.head_request)
                with self.assertRaises(LibraryStorageError):
                    self.storage.upload(self.data, "image/png")
        self.stub.assert_no_pending_responses()

    def test_empty_oversized_and_unsupported_uploads_fail_before_s3(self):
        for data, mimetype in (
            (b"", "image/png"),
            (b"x" * (MAX_BYTES + 1), "image/png"),
            (self.data, "application/octet-stream"),
        ):
            with (
                self.subTest(mimetype=mimetype, length=len(data)),
                self.assertRaises(LibraryStorageError),
            ):
                self.storage.upload(data, mimetype)

    @patch("worker.src.library_storage.urlopen")
    def test_public_hash_download_verifies_exact_bytes(self, open_url):
        open_url.return_value = DownloadResponse(self.data)
        self.assertEqual(self.storage.download(self.asset), self.data)
        open_url.assert_called_once_with(
            f"https://library.grida.co/{self.path}", timeout=60
        )
        for value in (b"corrupt", b"", self.data + b"x"):
            open_url.return_value = DownloadResponse(value)
            with self.assertRaises(LibraryStorageError):
                self.storage.download(self.asset)
        with self.assertRaises(LibraryStorageError):
            LibraryStorage.verify_download({**self.asset, "bytes": 1}, self.data)

    def test_supabase_preserves_storage_uuid_and_transform_and_checks_conflict(self):
        bucket = Mock()
        bucket.info.return_value = SimpleNamespace(id="storage-uuid")
        client = Mock()
        client.storage.from_.return_value = bucket
        storage = LibraryStorage(supabase=client)
        self.assertEqual(
            storage.upload(self.data, "image/png").storage_id, "storage-uuid"
        )
        bucket.upload.assert_called_once_with(
            self.path, self.data, {"content-type": "image/png"}
        )
        bucket.download.return_value = self.data
        storage.download(self.asset, transform={"quality": 80})
        bucket.download.assert_called_with(
            self.asset["path"], options={"transform": {"quality": 80}}
        )
        bucket.upload.side_effect = RuntimeError("409 Duplicate")
        storage.upload(self.data, "image/png")
        bucket.download.assert_called_with(self.path)
        bucket.download.return_value = b"wrong"
        with self.assertRaises(LibraryStorageError):
            storage.upload(self.data, "image/png")


if __name__ == "__main__":
    unittest.main()
