"""Real SDK serialization against an ephemeral loopback S3-shaped fixture."""

from __future__ import annotations

import base64
import hashlib
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from botocore.config import Config

from worker.src.library_storage import IMMUTABLE_CACHE, LibraryStorage
from worker.tests.test_library_storage import png_bytes


class LocalStorageHttpTest(unittest.TestCase):
    def setUp(self):
        self.data = png_bytes()
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.key = f"{self.digest}.png"
        self.objects = {}
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def record(self):
                owner.requests.append((self.command, self.path, dict(self.headers)))

            def do_PUT(self):
                self.record()
                data = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path in owner.objects:
                    self.send_response(412)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                owner.objects[self.path] = (
                    data,
                    self.headers["Content-Type"],
                    self.headers["x-amz-checksum-sha256"],
                )
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_HEAD(self):
                self.respond(False)

            def do_GET(self):
                self.respond(True)

            def respond(self, include_body):
                self.record()
                data, mimetype, checksum = owner.objects[self.path]
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type", mimetype)
                self.send_header("x-amz-checksum-sha256", checksum)
                self.send_header("x-amz-checksum-type", "FULL_OBJECT")
                self.end_headers()
                if include_body:
                    self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.client = boto3.client(
            "s3",
            endpoint_url=f"http://127.0.0.1:{self.server.server_port}",
            region_name="auto",
            aws_access_key_id="local-test-key",
            aws_secret_access_key="local-test-secret",
            config=Config(
                s3={"addressing_style": "path"},
                retries={"max_attempts": 0},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        self.addCleanup(self.client.close)
        self.storage = LibraryStorage(
            provider="tigris", s3=self.client, bucket="local-test"
        )

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_sdk_sends_conditional_hash_headers_and_rechecks_actual_body_on_adoption(
        self,
    ):
        self.storage.upload(self.data, "image/png")
        self.storage.upload(self.data, "image/png")
        path = f"/local-test/{self.key}"
        self.assertEqual(self.objects[path][0], self.data)
        self.assertEqual(
            [(method, location) for method, location, headers in self.requests],
            [(method, path) for method in ("PUT", "HEAD", "GET", "PUT", "HEAD", "GET")],
        )
        for method, location, headers in self.requests:
            self.assertIn("Authorization", headers)
            if method == "PUT":
                self.assertEqual(headers["If-None-Match"], "*")
                self.assertEqual(headers["Content-Length"], str(len(self.data)))
                self.assertEqual(headers["Content-Type"], "image/png")
                self.assertEqual(headers["Cache-Control"], IMMUTABLE_CACHE)
                self.assertEqual(
                    headers["x-amz-checksum-sha256"],
                    base64.b64encode(bytes.fromhex(self.digest)).decode(),
                )

    def test_matching_echoed_head_with_wrong_body_is_rejected_over_real_http(self):
        path = f"/local-test/{self.key}"
        checksum = base64.b64encode(bytes.fromhex(self.digest)).decode()
        self.objects[path] = (b"x" * len(self.data), "image/png", checksum)
        with self.assertRaises(Exception):
            self.storage.upload(self.data, "image/png")
        self.assertEqual(
            [method for method, path, headers in self.requests], ["PUT", "HEAD", "GET"]
        )
        self.assertEqual(self.objects[path][0], b"x" * len(self.data))


if __name__ == "__main__":
    unittest.main()
