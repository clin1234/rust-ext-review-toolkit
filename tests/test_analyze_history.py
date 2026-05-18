"""Tests for analyze_history — git churn and Rust/PyO3 commit classification."""

import contextlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import import_script

ah = import_script("analyze_history")


@contextlib.contextmanager
def _git_repo(commits: list[tuple[str, str]]):
    """Create a temp git repo; ``commits`` is a list of (message, lib.rs body)."""
    tmpdir = tempfile.mkdtemp(prefix="rustext_hist_")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmpdir, check=True, capture_output=True)

    try:
        git("init")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        git("config", "commit.gpgsign", "false")
        src = Path(tmpdir) / "src"
        src.mkdir()
        for message, body in commits:
            (src / "lib.rs").write_text(body, encoding="utf-8")
            git("add", ".")
            git("commit", "-m", message)
        yield tmpdir
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


class TestClassifyCommit(unittest.TestCase):
    def test_safety(self):
        self.assertEqual(ah.classify_commit("Fix an unsafe soundness bug"), "safety")

    def test_panic(self):
        self.assertEqual(ah.classify_commit("Remove an unwrap in the parser"), "panic")

    def test_concurrency(self):
        self.assertEqual(
            ah.classify_commit("Make Counter frozen for free-threading"), "concurrency"
        )

    def test_migration(self):
        self.assertEqual(
            ah.classify_commit("Migrate getters to the Bound API"), "migration"
        )

    def test_bugfix(self):
        self.assertEqual(ah.classify_commit("Fix a crash on empty input"), "bugfix")

    def test_unknown(self):
        self.assertEqual(ah.classify_commit("Tweak the widget"), "unknown")


class TestAnalyzeHistory(unittest.TestCase):
    def test_history_envelope_and_classification(self):
        with _git_repo(
            [
                ("Initial layout", "fn a() {}\n"),
                ("Fix unsafe soundness issue", "fn a() {}\nfn b() {}\n"),
                ("Add a new feature", "fn a() {}\nfn b() {}\nfn c() {}\n"),
            ]
        ) as repo:
            result = ah.analyze([repo, "--last", "10"])
        self.assertNotIn("error", result)
        self.assertEqual(result["summary"]["total_commits"], 3)
        self.assertIn("safety", result["summary"]["commits_by_type"])
        self.assertIn("file_churn", result)
        self.assertTrue(
            any(c["type"] == "safety" for c in result["recent_fixes"]),
            "the safety commit should appear in recent_fixes",
        )

    def test_not_a_git_repo(self):
        tmpdir = tempfile.mkdtemp(prefix="rustext_nogit_")
        try:
            result = ah.analyze([tmpdir])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
