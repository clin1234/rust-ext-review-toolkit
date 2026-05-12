"""Tests for run_external_tools.py — external static analysis tool integration."""

import shutil
import unittest
from helpers import import_script, TempExtension, MINIMAL_EXTENSION

ext_tools = import_script("run_external_tools")


class TestRunExternalTools(unittest.TestCase):
    """Test external tool wrapper script."""

    def test_analyze_returns_envelope(self):
        """analyze() returns standard envelope structure."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            result = ext_tools.analyze(str(root))
            self.assertIn("findings", result)
            self.assertIn("summary", result)
            self.assertIn("tools_available", result)
            self.assertIn("skipped_tools", result)
            self.assertIn("files_analyzed", result)
            self.assertIn("compile_commands", result)

    def test_tool_availability_detection(self):
        """Tool availability correctly detected."""
        self.assertIsInstance(ext_tools._tool_available("rust-analyzer"), bool)
        self.assertIsInstance(ext_tools._tool_available("cargo clippy"), bool)
        self.assertFalse(ext_tools._tool_available("nonexistent_tool_xyz"))

    def test_missing_tool_graceful(self):
        """Missing tool produces skip note, not error."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            result = ext_tools.analyze(
                str(root), skip_tools={"rust-analyzer", "cargo clippy"})
            self.assertIsInstance(result["findings"], list)
            self.assertEqual(len(result["findings"]), 0)
            self.assertEqual(len(result["skipped_tools"]), 2)

    def test_compile_commands_search(self):
        """compile_commands.json found in project root."""
        cc_json = '[{"directory":".","file":"myext.c","command":"cc -c myext.c"}]'
        with TempExtension({
            "myext.c": MINIMAL_EXTENSION,
            "compile_commands.json": cc_json,
        }) as root:
            cc = ext_tools._find_compile_commands(root, None)
            self.assertIsNotNone(cc)

    def test_compile_commands_not_found(self):
        """No compile_commands.json returns None."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            cc = ext_tools._find_compile_commands(root, None)
            self.assertIsNone(cc)

    def test_skip_tools_flag(self):
        """--skip flag prevents tool execution."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            result = ext_tools.analyze(
                str(root), skip_tools={"rust-analyzer", "cargo clippy"})
            skipped_names = [s["tool"] for s in result["skipped_tools"]]
            self.assertIn("rust-analyzer", skipped_names)
            self.assertIn("cargo clippy", skipped_names)

    @unittest.skipUnless(shutil.which("cargo clippy"), "cargo clippy not installed")
    def test_cargo clippy_runs_on_c_file(self):
        """cargo clippy runs and produces output on C file."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            result = ext_tools.analyze(
                str(root), skip_tools={"rust-analyzer"})
            self.assertTrue(result["tools_available"]["cargo clippy"])

    def test_output_summary_structure(self):
        """Summary has expected sub-fields."""
        with TempExtension({"myext.c": MINIMAL_EXTENSION}) as root:
            result = ext_tools.analyze(str(root))
            summary = result["summary"]
            self.assertIn("total_findings", summary)
            self.assertIn("by_tool", summary)
            self.assertIn("by_severity", summary)


if __name__ == "__main__":
    unittest.main()
