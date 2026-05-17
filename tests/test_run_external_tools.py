"""Tests for run_external_tools — the external Rust-tooling baseline.

These tests are environment-tolerant: they assert the report *shape* and the
clippy opt-in policy, not the output of `cargo` (which may or may not be
installed).
"""

import unittest

from fixtures import MINIMAL_PYO3_CRATE
from helpers import TempRustExtension, import_script

ret = import_script("run_external_tools")


def run(code: str = MINIMAL_PYO3_CRATE, **kw) -> dict:
    with TempRustExtension({"src/lib.rs": code}) as ext:
        return ret.analyze(str(ext.root), **kw)


class TestEnvelope(unittest.TestCase):
    def test_report_shape(self):
        result = run()
        for key in (
            "project_root",
            "tools_available",
            "cargo_metadata",
            "findings",
            "summary",
            "skipped_tools",
        ):
            self.assertIn(key, result)
        self.assertIsInstance(result["tools_available"], dict)
        self.assertIn("cargo", result["tools_available"])
        self.assertIsInstance(result["findings"], list)

    def test_clippy_is_opt_in(self):
        # Clippy compiles the crate (executes build scripts); it must never
        # run unless --run-clippy / run_clippy=True is given.
        result = run(run_clippy=False)
        clippy_skips = [s for s in result["skipped_tools"] if s["tool"] == "clippy"]
        self.assertEqual(len(clippy_skips), 1)
        self.assertEqual(
            [f for f in result["findings"] if f.get("tool") == "clippy"], []
        )

    def test_detection_only_tools_listed(self):
        result = run()
        skipped_tools = {s["tool"] for s in result["skipped_tools"]}
        for tool in ("miri", "cargo-geiger", "cargo-expand"):
            self.assertIn(tool, skipped_tools)


if __name__ == "__main__":
    unittest.main()
