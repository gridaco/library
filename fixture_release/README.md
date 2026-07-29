# Developer corpus release

This module is for a Grida Library maintainer preparing a bounded, realistic,
versioned corpus for local development in
[gridaco/grida](https://github.com/gridaco/grida). It reads one public Library
category, downloads its asset bytes and existing Gemini embeddings, removes
production identities, validates the bounded source values and compatibility,
then writes a self-describing ZIP archive.

It is deliberately not a database dump, a backup tool, or a general corpus
exporter.

## Safety boundary

- The exporter performs read-only Library and public Storage calls.
- It loads `SUPABASE_URL` plus `SUPABASE_PUBLISHABLE_KEY`,
  `SUPABASE_ANON_KEY`, or a public `SUPABASE_KEY` from an environment file but
  never writes or prints either value. Privileged keys are rejected.
- Asset bytes are downloaded from the unauthenticated public Storage URL. The
  database client is used only for public, read-only Library rows.
- Production object and Storage UUIDs are not written to
  the archive. Objects are keyed by the SHA-256 of their downloaded bytes.
- Author data, authentication data, original Storage paths, prompts, and
  unrelated catalog rows are not selected.
- Archive format v1 accepts the explicitly reviewed `CC0-1.0` and
  `LicenseRef-GridaLibrary` identifiers. Any other source value makes the
  export fail. The archive preserves each value and does not rewrite license
  terms. `LicenseRef-GridaLibrary` is project-specific and does not currently
  map to standalone license text; the archive links the public Library
  licensing policy as context, not as a replacement definition.
- Archive format v1 supports raster JPEG, PNG, WebP, GIF, and AVIF assets.
  SVG is deliberately excluded rather than weakly sanitized. WebP assets with
  embedded EXIF or XMP metadata are also rejected. Every asset is decoded,
  checked against its catalog dimensions, and rejected if Pillow exposes
  descriptive, author, EXIF, XMP, IPTC, or comment metadata.
- Existing image embeddings are required to be 1536-dimensional and
  L2-normalized. Matching text embeddings are exported when present so the
  local corpus exercises the same primary text-ranking and image-fallback
  search tiers as production.

The category itself is the maintainer-managed selection. Keep it bounded and
include only assets that a maintainer has authorized for this public GitHub
release.

## Setup

From the repository root:

```sh
uv venv
uv pip install -r fixture_release/requirements.txt
```

The repository's existing `.env` may provide `SUPABASE_URL` and a publishable
or legacy anon key. A privileged service-role or secret key is intentionally
rejected.

## Export

List the available public category IDs without printing connection
configuration:

```sh
uv run python -m fixture_release categories
```

Pass an exact category ID or name:

```sh
uv run python -m fixture_release export home
```

All category members are exported by default. For a smaller inspection
archive:

```sh
uv run python -m fixture_release export home --limit 12
```

The default output is
`dist/grida-library-<category>-developer-corpus.zip`. Use `--output` to
choose another path.

The CLI shows progress for metadata retrieval, asset download and validation,
and archive creation. `--limit` is applied after category members are ordered
by Library score, then catalog ID as a stable tie-breaker. Archives contain at
most 1,000 objects; curate the category or pass `--limit 1000` when a category
is larger. A successful run writes the ZIP and a sibling `.zip.sha256` file.

Verify an archive without accessing Supabase:

```sh
uv run python -m fixture_release verify dist/<archive>.zip
```

Verification requires the adjacent `.zip.sha256` sidecar and then checks the
archive inventory, internal checksums, relationships, media signatures,
licenses, and embedding contract.

After inspection, publish both files as an immutable GitHub prerelease:

```sh
uv run python -m fixture_release release dist/<archive>.zip \
  --tag developer-corpus-home-v1-rc.1 \
  --target feat/dev-corpus-release
```

The release command verifies the artifact again before invoking GitHub CLI. It
does not replace an existing tag or release asset.

## Archive contract

Every ZIP contains:

```text
README.md
manifest.json
checksums.sha256
objects.jsonl
embeddings.jsonl
categories.json
assets/
```

`manifest.json` carries `format_version`, the category metadata, the
embedding model contract, per-license counts, and SHA-256/byte-size entries for
every other archive file. `checksums.sha256` independently covers every content
entry other than itself and the manifest. The README inside the archive
explains the artifact to someone who downloads it without this repository.

Release assets are immutable. Publish a new versioned tag and archive when the
category or embedding contract changes; never replace an existing release
asset in place.

## Test

The core exporter and archive writer are tested without Supabase:

```sh
python -m unittest discover -s fixture_release/tests
```

## Review workflow

1. Export the maintainer-managed category.
2. Inspect the ZIP and its generated README and manifest.
3. Scan the archive for production UUIDs, credentials, unsupported licenses,
   missing bytes, and incompatible embeddings.
4. Commit and push the exporter branch.
5. Publish the archive as a GitHub prerelease for product review with the
   `release` command.
6. Open the implementation PR only after the artifact is approved.

The consuming downloader/importer in `gridaco/grida` is intentionally a
separate change.
