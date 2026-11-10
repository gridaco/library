# Library object storage

For Library maintainers running the existing uploader, enrichment worker,
batch embedding exporter, or developer corpus exporter. The catalog,
metadata, embeddings, and queue stay in Supabase; only object bytes move.

The default provider is `supabase`. Deploying this code does not change that.
An invalid provider fails instead of choosing a backend silently. Configuration
examples are in [`.env.storage.example`](./.env.storage.example).

| Variable | Purpose |
| --- | --- |
| `GRIDA_LIBRARY_STORAGE_PROVIDER` | `supabase` (default) or `tigris` |
| `GRIDA_LIBRARY_ASSET_ORIGIN` | Public Tigris origin; default `https://library.grida.co` |
| `GRIDA_LIBRARY_TIGRIS_BUCKET` | Uploader destination bucket |
| `GRIDA_LIBRARY_TIGRIS_ENDPOINT` | Uploader S3 endpoint; default `https://t3.storage.dev` |
| `GRIDA_LIBRARY_TIGRIS_ACCESS_KEY_ID` | Uploader's bucket-scoped credential |
| `GRIDA_LIBRARY_TIGRIS_SECRET_ACCESS_KEY` | Uploader's secret credential |

Supabase connection configuration remains unchanged. Tigris readers need no
Tigris key. The fixture exporter still rejects privileged Supabase keys.
Tigris origins and endpoints require HTTPS; HTTP is permitted only on exact
loopback hosts for local tests. Origins cannot contain credentials, a path
prefix, a query, or a fragment.

## Bytes and catalog identities

The shared Python implementation is [`src/library_storage.py`](./src/library_storage.py).
It lives in the worker subtree so the existing Railway artifact contains it;
the repository's uploader and fixture exporter import the same implementation.

Tigris keys are the lowercase SHA-256 of the exact original file bytes plus
one pinned extension:

| MIME type | Extension |
| --- | --- |
| `image/jpeg` | `jpg` |
| `image/png` | `png` |
| `image/webp` | `webp` |
| `image/gif` | `gif` |
| `image/svg+xml` | `svg` |
| `image/avif` | `avif` |

The Tigris uploader accepts 1 byte through 30 MiB and rejects other MIME
types. It uses create-only conditional PUTs. A conditional create sends the full
SHA-256 checksum. HEAD checks MIME and size; reported SHA-256 and checksum type
must match when present. Trusted manual or custom CLI uploads may omit that
checksum metadata. Reported checksums alone do not prove the body: an
authenticated SDK GET then streams and independently hashes the actual stored
bytes, checking exact length and MIME and closing the response on success or
failure. This read is required for fresh uploads and adoption, including
uncertain PUTs and already-present keys, before a catalog row can be inserted.
The SDK can also reject a mismatched response checksum while reading; that
fails registration.
A failed registration may leave a reusable, content-addressed object;
rerunning uses the same key.

With Global bucket placement, readback confirms the serving region;
replication elsewhere can lag. Conditional object creation is not a global
catalog lock. Trusted producers derive identical bytes and MIME metadata for
a content address; the database's unique path arbitrates new catalog rows.
Retry an unconfirmed upload with the same bytes.

New Tigris catalog IDs are independent UUIDs. Existing UUIDs, legacy paths,
metadata, collection links, and embeddings are not rekeyed by these tools.
Historical duplicate hashes remain separate records. An upload adopts the
oldest matching `created_at`, then the lowest UUID. The existing uploader's
explicit curation rerun refreshes that record's metadata, preserving its
UUID/path/hash and requiring matching bytes/MIME/size. Legacy rows without a
hash can still be curated in Supabase mode; Tigris mode requires the reviewed
hash backfill first. Simultaneous new registrations are arbitrated by the
catalog's unique path; a losing insert adopts a matching winner without
refreshing the winner's metadata. Unrelated conflicts fail.

## Worker and exports

Queue payloads are unchanged. In Tigris mode the worker looks up the queued
catalog UUID to get SHA-256/MIME/size, downloads its public hash URL, and
verifies the original bytes before processing. Missing catalog rows or hashes,
unavailable assets, and verification failures follow the existing failure/retry
path: they are not acknowledged or sent to the models.

Supabase mode retains its existing quality-80 download transform. Tigris has
no equivalent implicit transform: the same embedding and metadata routines
receive original bytes. Existing vectors remain untouched; switching storage
does not enqueue work or re-embed the corpus. Future newly processed vectors
may differ from vectors computed from the prior transformed image.

The worker CLI's `prepare dump` and [developer corpus exporter](../fixture_release/README.md)
resolve the same hash URL. Batch record IDs, model configuration and vector
sync behavior are unchanged. The developer corpus keeps its existing raster,
3 MiB, licensing, sanitization, and embedding restrictions.

## Deploy and verify

Install the relevant existing requirements; `boto3>=1.40,<2` is needed only
for uploads (and the SDK contract tests). Do not enable Tigris until the
reviewed Grida schema change, verified object copy, and catalog SHA backfill
are complete. Apply the same provider/origin decision to the uploader,
worker, batch export and fixture-export environments. This code does not
copy production objects, backfill the database, or perform cutover.

From the repository root, run the offline tests:

```sh
python -m unittest discover -s worker/tests
python -m unittest discover -s fixture_release/tests
```

The tests use synthetic images, local SQL catalog constraints, deterministic
model stubs, and the real boto3 request/response model. No production keys or
cloud objects are needed.
