from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import click
from tqdm import tqdm

from .archive import MAX_OBJECTS, FixtureArchive
from .exporter import FixtureExporter
from .model import FixtureExportError
from .source import SupabaseLibrarySource


class TqdmProgress:
    def __init__(self):
        self._bar: Any | None = None

    def start(self, label: str, total: int) -> None:
        self.finish()
        self._bar = tqdm(total=total, desc=label, unit="item")

    def advance(self) -> None:
        if self._bar is not None:
            self._bar.update(1)

    def finish(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None


def _filename_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return token or "category"


@click.group()
def cli() -> None:
    """Build and verify portable Grida Library developer corpora."""


@cli.command("export")
@click.argument("category")
@click.option(
    "--limit",
    type=click.IntRange(min=1, max=MAX_OBJECTS),
    default=None,
    help="Maximum category members to export. Defaults to all.",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help=(
        "Archive path. Defaults to dist/grida-library-<category>-developer-corpus.zip."
    ),
)
@click.option(
    "--env-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path(".env"),
    show_default=True,
    help="Environment file containing the Supabase URL and public key.",
)
@click.option(
    "--page-size",
    type=click.IntRange(min=1, max=1000),
    default=500,
    show_default=True,
    help="Rows fetched per category-membership request.",
)
def export_category(
    category: str,
    limit: int | None,
    output: Path | None,
    env_file: Path,
    page_size: int,
) -> None:
    """Export CATEGORY (exact ID or name) as a developer-corpus archive."""

    output = output or (
        Path("dist") / f"grida-library-{_filename_token(category)}-developer-corpus.zip"
    )
    progress = TqdmProgress()
    try:
        click.echo(f'Resolving Library category "{category}"…')
        source = SupabaseLibrarySource.from_environment(env_file, page_size=page_size)
        summary = FixtureExporter(source, progress=progress).export(
            category, output, limit=limit
        )
    except FixtureExportError as error:
        progress.finish()
        raise click.ClickException(str(error)) from error

    click.echo()
    click.echo(f"Archive: {summary.archive}")
    click.echo(f"Checksum: {summary.checksum_file}")
    click.echo(f"Objects: {summary.object_count}")
    click.echo(f"Asset bytes: {summary.total_bytes:,}")
    click.echo(f"SHA-256: {summary.archive_sha256}")


@cli.command("verify")
@click.argument(
    "archive",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
def verify_archive(archive: Path) -> None:
    """Verify ARCHIVE and its adjacent external checksum sidecar."""

    try:
        manifest = FixtureArchive().verify_with_sidecar(archive)
    except FixtureExportError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"OK: {manifest['category']['id']} ({manifest['object_count']} objects)")


@cli.command("release")
@click.argument(
    "archive",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
@click.option("--tag", required=True, help="Immutable GitHub release tag.")
@click.option(
    "--title",
    default=None,
    help="Release title. Defaults to the tag.",
)
@click.option(
    "--target",
    default="main",
    show_default=True,
    help="Branch or commit from which GitHub creates the tag.",
)
@click.option(
    "--repo",
    default="gridaco/library",
    show_default=True,
    help="GitHub repository receiving the prerelease.",
)
def release_archive(
    archive: Path,
    tag: str,
    title: str | None,
    target: str,
    repo: str,
) -> None:
    """Verify and publish ARCHIVE plus its checksum as a GitHub prerelease."""

    checksum_file = Path(f"{archive}.sha256")
    try:
        manifest = FixtureArchive().verify_with_sidecar(archive)
    except FixtureExportError as error:
        raise click.ClickException(str(error)) from error

    category = manifest["category"]
    notes = (
        f"Developer corpus for the Grida Library `{category['id']}` "
        f"category ({manifest['object_count']} objects).\n\n"
        "This prerelease contains source images, sanitized catalog metadata, "
        "and matching image/text embeddings for local development. Author/user "
        "data and production identifiers and paths are excluded.\n\n"
        "The source catalog license value is preserved verbatim; this artifact "
        "does not map `LicenseRef-GridaLibrary` to separate license text. "
        "Verify the ZIP against the attached `.sha256` sidecar before importing."
    )
    command = [
        "gh",
        "release",
        "create",
        tag,
        str(archive),
        str(checksum_file),
        "--repo",
        repo,
        "--target",
        target,
        "--title",
        title or tag,
        "--notes",
        notes,
        "--prerelease",
        "--latest=false",
    ]
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as error:
        raise click.ClickException("GitHub CLI (gh) is not installed") from error
    except subprocess.CalledProcessError as error:
        raise click.ClickException("GitHub prerelease creation failed") from error


@cli.command("categories")
@click.option(
    "--env-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path(".env"),
    show_default=True,
    help="Environment file containing the Supabase URL and public key.",
)
def list_categories(env_file: Path) -> None:
    """List category IDs and names available to the exporter."""

    try:
        source = SupabaseLibrarySource.from_environment(env_file)
        rows = source.list_categories()
    except FixtureExportError as error:
        raise click.ClickException(str(error)) from error
    if not rows:
        click.echo("No Library categories found.")
        return
    for row in rows:
        click.echo(f"{row['id']}\t{row['name']}")
