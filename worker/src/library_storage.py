"""Library object bytes; the catalog and queue remain in Supabase.

Kept in the worker artifact because Railway deploys that subtree. Repository
tools import this same module so the hash/MIME contract has one Python owner.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote, urlsplit
from urllib.request import urlopen


EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    "image/avif": "avif",
}
MAX_BYTES = 30 * 1024 * 1024
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


class LibraryStorageError(RuntimeError):
    pass


def storage_key(digest: str, mimetype: str) -> str:
    if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        raise LibraryStorageError("Library object requires a lowercase SHA-256 digest")
    extension = EXTENSIONS.get(mimetype)
    if not extension:
        raise LibraryStorageError("Unsupported Library media type")
    return f"{digest}.{extension}"


def _origin(value: str, *, secure: bool = True) -> str:
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError as error:
        raise LibraryStorageError("Invalid Library storage origin") from error
    local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parts.scheme not in {"https", "http"}
        or (secure and parts.scheme != "https" and not local)
        or not parts.hostname
        or (port is not None and not 0 < port <= 65535)
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise LibraryStorageError("Invalid Library storage origin")
    return f"{parts.scheme}://{parts.netloc}"


def _status(error: Exception) -> int | None:
    response = getattr(error, "response", {})
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return int(status) if status is not None else None


def _duplicate(error: Exception) -> bool:
    value = str(error)
    return (
        "409" in value
        or "duplicate" in value.lower()
        or "already exists" in value.lower()
    )


@dataclass(frozen=True)
class StoredObject:
    path: str
    sha256: str
    storage_id: str | None = None


class LibraryStorage:
    def __init__(
        self,
        *,
        provider: str = "supabase",
        supabase: Any = None,
        s3: Any = None,
        bucket: str | None = None,
        asset_origin: str = "https://library.grida.co",
    ):
        if provider not in {"supabase", "tigris"}:
            raise LibraryStorageError("Invalid Library storage provider")
        self.provider = provider
        self.supabase = supabase
        self._s3 = s3
        self._bucket = bucket
        self._asset_origin = _origin(asset_origin) if provider == "tigris" else None

    @classmethod
    def from_environment(cls, supabase: Any = None) -> LibraryStorage:
        # Readers need no Tigris credential. The uploader initializes S3 lazily.
        return cls(
            provider=os.getenv("GRIDA_LIBRARY_STORAGE_PROVIDER") or "supabase",
            supabase=supabase,
            bucket=os.getenv("GRIDA_LIBRARY_TIGRIS_BUCKET"),
            asset_origin=os.getenv("GRIDA_LIBRARY_ASSET_ORIGIN")
            or "https://library.grida.co",
        )

    def public_url(
        self, asset: Mapping[str, Any], *, supabase_url: str | None = None
    ) -> str:
        if self.provider == "tigris":
            key = storage_key(asset.get("sha256"), asset.get("mimetype"))
            return f"{self._asset_origin}/{key}"
        path = asset.get("path")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise LibraryStorageError("Library object has an invalid Storage path")
        origin = _origin(supabase_url or "", secure=False)
        return f"{origin}/storage/v1/object/public/library/{quote(path, safe='/')}"

    def upload(
        self, data: bytes, mimetype: str, *, fallback_suffix: str = ""
    ) -> StoredObject:
        digest = hashlib.sha256(data).hexdigest()
        if self.provider == "tigris":
            if not data or len(data) > MAX_BYTES:
                raise LibraryStorageError("Library uploads require 1 byte to 30 MiB")
            path = storage_key(digest, mimetype)
            self._upload_tigris(path, digest, data, mimetype)
            return StoredObject(path, digest)

        extension = EXTENSIONS.get(mimetype) or fallback_suffix.lstrip(".").lower()
        path = f"{digest}.{extension}" if extension else digest
        bucket = self.supabase.storage.from_("library")
        try:
            bucket.upload(path, data, {"content-type": mimetype})
        except Exception as error:
            if not _duplicate(error):
                raise
            # A conflict alone is not proof of bytes after an interrupted run.
            if bucket.download(path) != data:
                raise LibraryStorageError(
                    "Existing Library bytes do not match their content address"
                ) from error
        info = bucket.info(path)
        storage_id = info.id if hasattr(info, "id") else info["id"]
        return StoredObject(path, digest, storage_id)

    def _client(self) -> Any:
        if self._s3 is not None:
            return self._s3
        key = os.getenv("GRIDA_LIBRARY_TIGRIS_ACCESS_KEY_ID")
        secret = os.getenv("GRIDA_LIBRARY_TIGRIS_SECRET_ACCESS_KEY")
        if not self._bucket or not key or not secret:
            raise LibraryStorageError("Missing Library Tigris bucket or credentials")
        endpoint = _origin(
            os.getenv("GRIDA_LIBRARY_TIGRIS_ENDPOINT") or "https://t3.storage.dev"
        )
        try:
            import boto3
            from botocore.config import Config
        except ImportError as error:
            raise LibraryStorageError(
                "Install boto3>=1.40,<2 for Tigris uploads"
            ) from error
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name="auto",
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            config=Config(
                connect_timeout=10,
                read_timeout=60,
                retries={"mode": "standard", "max_attempts": 3},
                s3={
                    "addressing_style": "path"
                    if urlsplit(endpoint).hostname in {"localhost", "127.0.0.1", "::1"}
                    else "virtual"
                },
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        return self._s3

    def _upload_tigris(
        self, path: str, digest: str, data: bytes, mimetype: str
    ) -> None:
        client = self._client()
        if not self._bucket:
            raise LibraryStorageError("Missing Library Tigris bucket")
        checksum = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
        for attempt in range(3):
            try:
                client.put_object(
                    Bucket=self._bucket,
                    Key=path,
                    Body=data,
                    ContentLength=len(data),
                    ContentType=mimetype,
                    CacheControl=IMMUTABLE_CACHE,
                    ChecksumSHA256=checksum,
                    IfNoneMatch="*",
                )
            except Exception as error:
                status = _status(error)
                if status is not None and status not in {409, 412} and status < 500:
                    raise
                # A timeout or conditional conflict can follow a committed PUT.
            try:
                head = client.head_object(
                    Bucket=self._bucket, Key=path, ChecksumMode="ENABLED"
                )
            except Exception as error:
                if _status(error) == 404 and attempt < 2:
                    continue
                raise
            if (
                head.get("ContentLength") != len(data)
                or head.get("ContentType") != mimetype
                or ("ChecksumType" in head and head["ChecksumType"] != "FULL_OBJECT")
                or ("ChecksumSHA256" in head and head["ChecksumSHA256"] != checksum)
            ):
                raise LibraryStorageError(
                    "Library Tigris object does not match its content address"
                )
            # Manual uploads may omit checksum metadata; readback proves bytes.
            self._verify_stored_bytes(client, path, digest, len(data), mimetype)
            return

    def _verify_stored_bytes(
        self, client: Any, path: str, digest: str, expected_bytes: int, mimetype: str
    ) -> None:
        # Tigris may echo a supplied checksum in HEAD without validating the
        # body. Only an independent authenticated GET proves stored bytes.
        response = client.get_object(
            Bucket=self._bucket, Key=path, ChecksumMode="ENABLED"
        )
        body = response["Body"]
        try:
            if (
                response.get("ContentLength") != expected_bytes
                or response.get("ContentType") != mimetype
            ):
                raise LibraryStorageError("Library Tigris GET metadata differs")
            observed = 0
            checksum = hashlib.sha256()
            while True:
                chunk = body.read(min(64 * 1024, expected_bytes + 1 - observed))
                if not chunk:
                    break
                observed += len(chunk)
                if observed > expected_bytes:
                    raise LibraryStorageError(
                        "Library Tigris object exceeds its expected length"
                    )
                checksum.update(chunk)
            if observed != expected_bytes or checksum.hexdigest() != digest:
                raise LibraryStorageError(
                    "Library Tigris stored bytes do not match their content address"
                )
        finally:
            body.close()

    def download(
        self, asset: Mapping[str, Any], *, transform: Mapping[str, Any] | None = None
    ) -> bytes:
        if self.provider == "supabase":
            bucket = self.supabase.storage.from_("library")
            if transform is not None:
                return bucket.download(
                    asset["path"], options={"transform": dict(transform)}
                )
            return bucket.download(asset["path"])
        # Tigris stores the original immutable bytes. No implicit image transform.
        with urlopen(self.public_url(asset), timeout=60) as response:
            data = response.read(MAX_BYTES + 1)
        self.verify_download(asset, data)
        return data

    @staticmethod
    def verify_download(asset: Mapping[str, Any], data: bytes) -> None:
        if not data or len(data) > MAX_BYTES:
            raise LibraryStorageError("Invalid Library object byte length")
        if hashlib.sha256(data).hexdigest() != asset.get("sha256"):
            raise LibraryStorageError(
                "Downloaded Library bytes do not match their SHA-256"
            )
        if asset.get("bytes") is not None and len(data) != asset["bytes"]:
            raise LibraryStorageError(
                "Downloaded Library bytes do not match their catalog size"
            )
