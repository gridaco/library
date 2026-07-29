from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from fixture_release.cli import cli
from fixture_release.model import FixtureExportError


class FixtureCliTest(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Path(self.temp.name) / "corpus.zip"
        self.archive.write_bytes(b"fixture")
        Path(f"{self.archive}.sha256").write_text(
            f"{'0' * 64}  {self.archive.name}\n",
            encoding="utf-8",
        )
        self.manifest = {
            "category": {"id": "home"},
            "object_count": 12,
        }

    @patch("fixture_release.cli.FixtureArchive.verify_with_sidecar")
    def test_verify_uses_the_external_checksum_sidecar(self, verify):
        verify.return_value = self.manifest

        result = self.runner.invoke(cli, ["verify", str(self.archive)])

        self.assertEqual(result.exit_code, 0, result.output)
        verify.assert_called_once_with(self.archive)

    @patch("fixture_release.cli.FixtureExporter")
    @patch("fixture_release.cli.SupabaseLibrarySource.from_environment")
    def test_export_accepts_the_home_category_size(self, source, exporter):
        exporter.return_value.export.return_value = SimpleNamespace(
            archive=self.archive,
            checksum_file=Path(f"{self.archive}.sha256"),
            object_count=631,
            total_bytes=123,
            archive_sha256="a" * 64,
        )

        result = self.runner.invoke(
            cli,
            [
                "export",
                "home",
                "--limit",
                "631",
                "--output",
                str(self.archive),
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        exporter.return_value.export.assert_called_once_with(
            "home", self.archive, limit=631
        )
        source.assert_called_once()

    def test_export_rejects_a_limit_above_the_archive_contract(self):
        result = self.runner.invoke(cli, ["export", "home", "--limit", "1001"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not in the range 1<=x<=1000", result.output)

    @patch("fixture_release.cli.subprocess.run")
    @patch("fixture_release.cli.FixtureArchive.verify_with_sidecar")
    def test_release_verifies_then_invokes_gh_without_a_shell(self, verify, run):
        verify.return_value = self.manifest

        result = self.runner.invoke(
            cli,
            [
                "release",
                str(self.archive),
                "--tag",
                "developer-corpus-home-v1-rc.1",
                "--target",
                "feat/dev-corpus-release",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        verify.assert_called_once_with(self.archive)
        command = run.call_args.args[0]
        self.assertEqual(
            command[:5],
            [
                "gh",
                "release",
                "create",
                "developer-corpus-home-v1-rc.1",
                str(self.archive),
            ],
        )
        self.assertIn(str(Path(f"{self.archive}.sha256")), command)
        self.assertIn("--prerelease", command)
        self.assertIn("--latest=false", command)
        self.assertEqual(run.call_args.kwargs, {"check": True})

    @patch("fixture_release.cli.subprocess.run")
    @patch("fixture_release.cli.FixtureArchive.verify_with_sidecar")
    def test_release_does_not_call_github_when_verification_fails(self, verify, run):
        verify.side_effect = FixtureExportError("invalid fixture")

        result = self.runner.invoke(
            cli,
            ["release", str(self.archive), "--tag", "invalid"],
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("invalid fixture", result.output)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
