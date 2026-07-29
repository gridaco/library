from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import patch

from fixture_release import FixtureExportError
from fixture_release.source import (
    OBJECT_SELECT,
    SupabaseLibrarySource,
    _assert_public_key,
    _public_asset_url,
)


class Response:
    def __init__(self, data):
        self.data = data


class Query:
    def __init__(self, rows):
        self._rows = list(rows)
        self._filters = []
        self._limit = None
        self._order = None
        self._range = None

    def select(self, fields):
        self._fields = fields
        return self

    def eq(self, field, value):
        self._filters.append((field, value))
        return self

    def in_(self, field, values):
        self._filters.append((field, set(values)))
        return self

    def limit(self, value):
        self._limit = value
        return self

    def order(self, field):
        self._order = field
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        rows = [
            row
            for row in self._rows
            if all(
                row.get(field) in value
                if isinstance(value, set)
                else row.get(field) == value
                for field, value in self._filters
            )
        ]
        if self._order:
            rows.sort(key=lambda row: row[self._order])
        if self._range:
            start, end = self._range
            rows = rows[start : end + 1]
        if self._limit is not None:
            rows = rows[: self._limit]
        return Response(rows)


class Schema:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return Query(self._tables[name])


class Client:
    def __init__(self, tables):
        self._schema = Schema(tables)

    def schema(self, name):
        if name != "grida_library":
            raise AssertionError(name)
        return self._schema


class DownloadResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, maximum):
        return self.payload[:maximum]


class SupabaseLibrarySourceTest(unittest.TestCase):
    def source(self, *, categories=None, objects=None, page_size=2):
        return SupabaseLibrarySource(
            Client(
                {
                    "category": categories or [],
                    "object": objects or [],
                }
            ),
            page_size=page_size,
        )

    def test_resolves_unique_id_before_a_matching_name(self):
        source = self.source(
            categories=[
                {"id": "fixture", "name": "Other", "description": None},
                {
                    "id": "by-name",
                    "name": "fixture",
                    "description": "Name match",
                },
            ]
        )

        self.assertEqual(source.resolve_category("fixture")["id"], "fixture")

    def test_rejects_ambiguous_category_name(self):
        source = self.source(
            categories=[
                {"id": "a", "name": "Fixture", "description": None},
                {"id": "b", "name": "Fixture", "description": None},
            ]
        )

        with self.assertRaisesRegex(FixtureExportError, "ambiguous"):
            source.resolve_category("Fixture")

    def test_paginates_category_objects_in_stable_id_order(self):
        source = self.source(
            objects=[
                {"id": "e", "category": "home"},
                {"id": "a", "category": "home"},
                {"id": "c", "category": "home"},
                {"id": "b", "category": "home"},
                {"id": "d", "category": "home"},
                {"id": "ignored", "category": "other"},
            ],
            page_size=2,
        )

        self.assertEqual(
            source.list_category_object_ids("home"), ["a", "b", "c", "d", "e"]
        )

    def test_lists_categories_in_id_order_across_pages(self):
        source = self.source(
            categories=[
                {"id": "z", "name": "Z", "description": None},
                {"id": "a", "name": "A", "description": None},
                {"id": "m", "name": "M", "description": None},
            ],
            page_size=2,
        )

        self.assertEqual(
            [row["id"] for row in source.list_categories()],
            ["a", "m", "z"],
        )

    def test_object_query_does_not_request_author_or_user_data(self):
        self.assertNotIn("author", OBJECT_SELECT)
        self.assertNotIn("user_id", OBJECT_SELECT)

    def test_rejects_invalid_page_size_outside_the_cli(self):
        with self.assertRaisesRegex(FixtureExportError, "page size"):
            self.source(page_size=0)

    def test_public_asset_url_is_encoded_and_uses_the_public_bucket(self):
        self.assertEqual(
            _public_asset_url(
                "https://example.supabase.co",
                "objects/hello world.png",
            ),
            "https://example.supabase.co/storage/v1/object/public/library/"
            "objects/hello%20world.png",
        )

    @patch("fixture_release.source.urlopen")
    def test_download_uses_an_unauthenticated_public_url(self, open_url):
        open_url.return_value = DownloadResponse(b"asset")
        source = SupabaseLibrarySource(
            Client(
                {
                    "object": [],
                    "category": [],
                }
            ),
            supabase_url="https://example.supabase.co",
        )

        self.assertEqual(source.download("objects/a.png"), b"asset")
        open_url.assert_called_once_with(
            "https://example.supabase.co/storage/v1/object/public/library/"
            "objects/a.png",
            timeout=60,
        )

    def test_rejects_a_secret_supabase_key(self):
        with self.assertRaisesRegex(FixtureExportError, "privileged"):
            _assert_public_key("sb_secret_example")

    def test_accepts_publishable_and_legacy_anon_keys(self):
        _assert_public_key("sb_publishable_example")
        payload = base64.urlsafe_b64encode(
            json.dumps({"role": "anon"}).encode()
        ).rstrip(b"=")
        _assert_public_key(f"header.{payload.decode()}.signature")

    def test_rejects_a_legacy_service_role_key(self):
        payload = base64.urlsafe_b64encode(
            json.dumps({"role": "service_role"}).encode()
        ).rstrip(b"=")
        with self.assertRaisesRegex(FixtureExportError, "privileged"):
            _assert_public_key(f"header.{payload.decode()}.signature")


if __name__ == "__main__":
    unittest.main()
