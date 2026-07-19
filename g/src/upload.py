from dotenv import load_dotenv
import os
import json
import hashlib
import click
from pathlib import Path
from tqdm import tqdm
from supabase import create_client, Client

BUCKET_NAME = "library"

# Content addressing (#929, grida/docs/wg/platform/library.md §3):
# identity = sha256 of the stored bytes (lowercase hex, required on INSERT);
# storage path = flat `<sha256>.<ext>`. The extension map is pinned and
# mirrored verbatim from the editor producer (LibraryCAS) so every producer
# derives the same CAS path for the same media type.
EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    "image/avif": "avif",
}


def cas_path(digest: str, mimetype: str, fallback_suffix: str) -> str:
    ext = EXT.get(mimetype) or fallback_suffix.lstrip(".").lower()
    return f"{digest}.{ext}" if ext else digest


def is_duplicate_error(e: Exception) -> bool:
    # storage-api duplicate shapes: HTTP 409 / statusCode "409" / "Duplicate";
    # historic servers used HTTP 400 with a 409 body code.
    s = str(e)
    return "409" in s or "Duplicate" in s or "already exists" in s.lower()


@click.command()
@click.argument('input_dir', type=click.Path(exists=True, file_okay=False))
@click.argument('category')
@click.option('--folder', show_default=True, help="legacy folder in bucket (pre-CAS uploads; used only to detect already-uploaded legacy objects)")
@click.option('--type', 'file_type', type=click.Choice(['jpg', 'png', 'svg', 'webp']), default='jpg', show_default=True, help="File type to process")
@click.option('--env-file', type=click.Path(exists=True, dir_okay=False), default=".env", show_default=True, help="Path to .env file")
def cli(input_dir, category, folder, file_type, env_file):
    load_dotenv(env_file)
    url: str = os.environ.get("SUPABASE_URL")
    key: str = os.environ.get("SUPABASE_KEY")
    supabase: Client = create_client(url, key)
    input_path = Path(input_dir)
    folder = folder or category
    library = supabase.schema("grida_library")

    for file in tqdm(list(input_path.glob(f"*.{file_type}")), desc="Uploading objects"):
        object_path = file.with_name(file.stem + ".object.json")
        if not object_path.exists():
            tqdm.write(f"[SKIP] {file.name}: missing object.json")
            continue

        with open(object_path) as f:
            obj = json.load(f)

        data = file.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        mimetype = obj.get("mimetype") or "application/octet-stream"
        path = cas_path(digest, mimetype, file.suffix)
        legacy_path = f"{folder}/{file.name}"

        try:
            obj_author = obj.get("author")
            if obj_author:
                author = library.table("author").upsert(
                    obj_author, on_conflict="provider,username").execute()
                author_id = author.data[0].get("id")

            annotated_fields = [
                k for k, v in obj.items()
                if v not in (None, [], "", {})
            ]

            metadata = {
                "title": obj.get("title"),
                "alt": obj.get("alt"),
                "description": obj.get("description"),
                "author_id": author_id if obj_author else None,
                "category": category,
                "objects": obj.get("objects", []),
                "keywords": obj.get("keywords", []),
                "mimetype": obj["mimetype"],
                "width": obj["width"],
                "height": obj["height"],
                "bytes": obj["bytes"],
                "license": obj.get("license"),
                "version": obj.get("version", 1),
                "fill": obj.get("fill"),
                "color": obj.get("color"),
                "colors": obj.get("colors", []),
                "background": obj.get("background"),
                "transparency": obj["transparency"],
                "score": obj.get("score"),
                "year": obj.get("year"),
                "entropy": obj.get("entropy"),
                "orientation": obj["orientation"],
                "gravity_x": obj.get("gravity_x"),
                "gravity_y": obj.get("gravity_y"),
                "lang": obj.get("lang"),
                "generator": obj.get("generator"),
                "prompt": obj.get("prompt"),
                "public_domain": obj.get("public_domain", False),
                "sys_annotations": annotated_fields,
            }

            # Pre-check: the object may already be registered under the CAS
            # regime (by content address) or the legacy regime (by the old
            # folder path — legacy rows have NULL sha256 until backfill, so
            # the unique index alone cannot catch this re-run).
            existing = library.table("object").select("id, sha256").or_(
                f"sha256.eq.{digest},path.eq.{legacy_path}"
            ).limit(1).execute()
            if existing.data:
                # Curation lane: a re-run over an existing object is a
                # deliberate metadata refresh. Never touches sha256/path —
                # local bytes are not proof of stored bytes (x-upsert
                # history), and identity is not a correction channel.
                library.table("object").update(metadata).eq(
                    "id", existing.data[0]["id"]).execute()
                tqdm.write(f"[OK] refreshed metadata for {file.name}")
                continue

            # Store at the CAS path. No x-upsert: blobs are immutable under
            # content addressing; "already exists" = the same bytes are
            # already stored (success signal, e.g. a crashed prior run).
            try:
                supabase.storage.from_(BUCKET_NAME).upload(
                    path, data, {"content-type": mimetype})
            except Exception as e:
                if not is_duplicate_error(e):
                    raise

            # supabase-py's upload response has no object id
            # (supabase/supabase-py#1111) — recover it via info(). (The old
            # list(search=...) hack hangs on bucket-root listings.)
            info = supabase.storage.from_(BUCKET_NAME).info(path)
            uploaded_obj_id = info.id if hasattr(info, "id") else info["id"]

            try:
                library.table("object").insert({
                    "id": uploaded_obj_id,
                    "path": path,
                    "sha256": digest,
                    **metadata,
                }).execute()
                tqdm.write(f"[OK] uploaded {file.name} as {path}")
            except Exception as e:
                # unique_violation: same bytes registered concurrently —
                # first-writer-wins, adopt the existing row.
                if "23505" in str(e):
                    tqdm.write(f"[SKIP] {file.name}: already registered ({digest[:12]}…)")
                    continue
                raise
        except Exception as e:
            tqdm.write(f"[ERROR] {file.name}: {e}")
            continue


if __name__ == "__main__":
    cli()
