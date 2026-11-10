from dotenv import load_dotenv
import os
import json
import hashlib
import click
import sys
from pathlib import Path
from tqdm import tqdm
from supabase import create_client, Client
from uuid import uuid4

# Direct execution (`python g/src/upload.py`) must see the repository package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from worker.src.library_storage import LibraryStorage, LibraryStorageError


def existing_object(library, digest: str, legacy_path: str | None = None):
    # Historical duplicates keep all their catalog IDs. Adoption is stable.
    query = library.table("object").select("id,path,sha256,mimetype,bytes")
    rows = (
        query.eq("sha256", digest)
        .order("created_at")
        .order("id")
        .limit(1)
        .execute()
        .data
    )
    if rows:
        return rows[0]
    if legacy_path:
        rows = (
            library.table("object")
            .select("id,path,sha256,mimetype,bytes")
            .eq("path", legacy_path)
            .limit(1)
            .execute()
            .data
        )
        if rows:
            return rows[0]
    return None


def register_object(
    library,
    storage: LibraryStorage,
    data: bytes,
    metadata: dict,
    legacy_path: str,
    suffix: str,
) -> tuple[str, str]:
    """Existing curation refresh; new catalog rows use immutable CAS bytes."""
    digest = hashlib.sha256(data).hexdigest()
    mimetype = metadata["mimetype"]
    if metadata["bytes"] != len(data):
        raise LibraryStorageError("Object metadata size does not match the local file")
    existing = existing_object(library, digest, legacy_path)
    if existing:
        if existing.get("sha256") is not None and existing["sha256"] != digest:
            raise LibraryStorageError(
                "Legacy object bytes differ; curate the existing asset separately"
            )
        if existing["mimetype"] != mimetype or existing["bytes"] != len(data):
            raise LibraryStorageError("Existing Library media type or size differs")
        if storage.provider == "tigris":
            if existing.get("sha256") is None:
                raise LibraryStorageError(
                    "Backfill the legacy object's verified SHA-256 before Tigris curation"
                )
            storage.upload(data, mimetype)
        # A deliberate curation rerun may refresh metadata, never UUID/path/SHA.
        library.table("object").update(metadata).eq("id", existing["id"]).execute()
        return existing["id"], "refreshed"

    stored = storage.upload(data, mimetype, fallback_suffix=suffix)
    object_id = stored.storage_id if storage.provider == "supabase" else str(uuid4())
    try:
        library.table("object").insert(
            {
                "id": object_id,
                "path": stored.path,
                "sha256": stored.sha256,
                **metadata,
            }
        ).execute()
        return object_id, "uploaded"
    except Exception as error:
        # Path uniqueness arbitrates simultaneous registrations, including the
        # web producer. Adopt only a proved matching winner; don't refresh it.
        if getattr(error, "code", None) == "23505" or "23505" in str(error):
            winner = existing_object(library, digest)
            if (
                winner
                and winner["mimetype"] == mimetype
                and winner["bytes"] == len(data)
            ):
                return winner["id"], "adopted"
        raise


@click.command()
@click.argument("input_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("category")
@click.option(
    "--folder",
    show_default=True,
    help="legacy folder in bucket (pre-CAS uploads; used only to detect already-uploaded legacy objects)",
)
@click.option(
    "--type",
    "file_type",
    type=click.Choice(["jpg", "png", "svg", "webp", "gif", "avif"]),
    default="jpg",
    show_default=True,
    help="File type to process",
)
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False),
    default=".env",
    show_default=True,
    help="Path to .env file",
)
def cli(input_dir, category, folder, file_type, env_file):
    load_dotenv(env_file)
    url: str = os.environ.get("SUPABASE_URL")
    key: str = os.environ.get("SUPABASE_KEY")
    supabase: Client = create_client(url, key)
    storage = LibraryStorage.from_environment(supabase)
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
        legacy_path = f"{folder}/{file.name}"

        try:
            obj_author = obj.get("author")
            if obj_author:
                author = (
                    library.table("author")
                    .upsert(obj_author, on_conflict="provider,username")
                    .execute()
                )
                author_id = author.data[0].get("id")

            annotated_fields = [
                k for k, v in obj.items() if v not in (None, [], "", {})
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

            object_id, status = register_object(
                library, storage, data, metadata, legacy_path, file.suffix
            )
            tqdm.write(f"[OK] {status} {file.name} ({object_id})")
        except Exception as e:
            tqdm.write(f"[ERROR] {file.name}: {e}")
            continue


if __name__ == "__main__":
    cli()
