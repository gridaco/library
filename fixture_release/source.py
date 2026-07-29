from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

from .archive import LIBRARY_MAX_ASSET_BYTES
from .model import FixtureExportError, JsonObject

OBJECT_SELECT = (
    "id,path,sha256,title,alt,description,category,categories,"
    "objects,keywords,mimetype,width,height,bytes,license,version,fill,color,"
    "colors,background,score,year,entropy,orientation,gravity_x,gravity_y,"
    "lang,transparency,public_domain,"
    "object_embedding(gemini_embedding_2__image,gemini_embedding_2__text)"
)
CATEGORY_SELECT = "id,name,description"
PUBLIC_KEY_ENV_NAMES = (
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_ANON_KEY",
    "SUPABASE_KEY",
)


def _assert_public_key(key: str) -> None:
    """Reject credentials that can bypass the Library's public read policy."""

    if key.startswith("sb_publishable_"):
        return
    if key.startswith("sb_secret_"):
        raise FixtureExportError(
            "the configured Supabase key is privileged; configure a "
            "publishable or anon key for fixture exports"
        )

    parts = key.split(".")
    if len(parts) != 3:
        raise FixtureExportError(
            "the configured Supabase key is not a publishable or anon key"
        )
    try:
        encoded = parts[1] + ("=" * (-len(parts[1]) % 4))
        claims = json.loads(base64.urlsafe_b64decode(encoded))
    except (ValueError, json.JSONDecodeError) as error:
        raise FixtureExportError(
            "the configured Supabase key is not a publishable or anon key"
        ) from error
    if not isinstance(claims, dict) or claims.get("role") != "anon":
        raise FixtureExportError(
            "the configured Supabase key is privileged; configure a "
            "publishable or anon key for fixture exports"
        )


def _public_asset_url(supabase_url: str, path: str) -> str:
    parts = urlsplit(supabase_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise FixtureExportError("SUPABASE_URL is not a valid HTTP origin")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise FixtureExportError("Library object has an invalid Storage path")
    origin = f"{parts.scheme}://{parts.netloc}"
    encoded_path = quote(path, safe="/")
    return f"{origin}/storage/v1/object/public/library/{encoded_path}"


class SupabaseLibrarySource:
    """Read-only production Library adapter.

    It deliberately exposes only the calls needed to materialize a curated
    category. The export layer never receives auth rows, author rows, or
    unrelated catalog data.
    """

    def __init__(
        self,
        client: Any,
        *,
        supabase_url: str | None = None,
        page_size: int = 500,
    ):
        if page_size < 1 or page_size > 1000:
            raise FixtureExportError("page size must be between 1 and 1000")
        self._client = client
        self._library = client.schema("grida_library")
        self._supabase_url = supabase_url
        self._page_size = page_size

    @classmethod
    def from_environment(
        cls, env_file: Path, *, page_size: int = 500
    ) -> SupabaseLibrarySource:
        try:
            from dotenv import load_dotenv
            from supabase import create_client
        except ImportError as error:
            raise FixtureExportError(
                "fixture_release dependencies are missing; install "
                "fixture_release/requirements.txt"
            ) from error

        load_dotenv(env_file, override=True)
        url = os.environ.get("SUPABASE_URL")
        key = next(
            (value for name in PUBLIC_KEY_ENV_NAMES if (value := os.environ.get(name))),
            None,
        )
        if not url or not key:
            raise FixtureExportError(
                "SUPABASE_URL and a Supabase publishable or anon key must be configured"
            )
        _assert_public_key(key)
        try:
            client = create_client(url, key)
        except Exception as error:
            raise FixtureExportError(
                "could not initialize the configured Supabase client"
            ) from error
        return cls(client, supabase_url=url, page_size=page_size)

    def resolve_category(self, identifier: str) -> JsonObject:
        by_id = self._execute(
            self._library.table("category")
            .select(CATEGORY_SELECT)
            .eq("id", identifier)
            .limit(2),
            "resolve the Library category",
        )
        if len(by_id.data or []) == 1:
            return dict(by_id.data[0])

        by_name = self._execute(
            self._library.table("category")
            .select(CATEGORY_SELECT)
            .eq("name", identifier)
            .limit(2),
            "resolve the Library category",
        )
        matches = by_name.data or []
        if not matches:
            raise FixtureExportError(f'Library category "{identifier}" was not found')
        if len(matches) > 1:
            raise FixtureExportError(
                f'Library category name "{identifier}" is ambiguous; '
                "pass its unique ID instead"
            )
        return dict(matches[0])

    def list_categories(self) -> list[JsonObject]:
        categories: list[JsonObject] = []
        offset = 0
        while True:
            response = self._execute(
                self._library.table("category")
                .select(CATEGORY_SELECT)
                .order("id")
                .range(offset, offset + self._page_size - 1),
                "list Library categories",
            )
            rows = response.data or []
            categories.extend(dict(row) for row in rows)
            offset += len(rows)
            if len(rows) < self._page_size:
                break
        return categories

    def list_category_object_ids(self, category_id: str) -> list[str]:
        object_ids: list[str] = []
        offset = 0
        while True:
            response = self._execute(
                self._library.table("object")
                .select("id")
                .eq("category", category_id)
                .order("id")
                .range(offset, offset + self._page_size - 1),
                "read the Library category membership",
            )
            rows = response.data or []
            if not rows:
                break
            object_ids.extend(str(row["id"]) for row in rows)
            offset += len(rows)
            if len(rows) < self._page_size:
                break
        return object_ids

    def iter_objects(self, object_ids: Sequence[str]) -> Iterable[JsonObject]:
        batch_size = 40
        for start in range(0, len(object_ids), batch_size):
            batch = list(object_ids[start : start + batch_size])
            response = self._execute(
                self._library.table("object").select(OBJECT_SELECT).in_("id", batch),
                "read Library object metadata",
            )
            for row in response.data or []:
                yield dict(row)

    def download(self, path: str) -> bytes:
        if self._supabase_url is None:
            raise FixtureExportError(
                "the Library source has no public Storage origin configured"
            )
        url = _public_asset_url(self._supabase_url, path)
        try:
            with urlopen(url, timeout=60) as response:
                payload = response.read(LIBRARY_MAX_ASSET_BYTES + 1)
        except (OSError, ValueError) as error:
            raise FixtureExportError(
                f'could not download Library object "{path}"'
            ) from error
        if not isinstance(payload, bytes):
            raise FixtureExportError(f'Library object "{path}" returned no bytes')
        if len(payload) > LIBRARY_MAX_ASSET_BYTES:
            raise FixtureExportError(
                f'Library object "{path}" exceeds the 3 MiB asset limit'
            )
        return payload

    @staticmethod
    def _execute(query: Any, action: str) -> Any:
        try:
            return query.execute()
        except Exception as error:
            raise FixtureExportError(f"could not {action}") from error
