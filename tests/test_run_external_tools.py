"""Tests for run_external_tools — the external Rust-tooling baseline.

These tests are environment-tolerant: they assert the report *shape* and the
clippy opt-in policy, not the output of `cargo` (which may or may not be
installed).
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_unrequested_tools_listed(self):
        result = run()
        skipped_tools = {s["tool"] for s in result["skipped_tools"]}
        for tool in ("miri", "cargo-geiger", "cargo-expand"):
            self.assertIn(tool, skipped_tools)


class TestOptionalEvidence(unittest.TestCase):
    def setUp(self):
        self.available = patch.object(ret, "_tool_available", return_value=True)
        self.available.start()
        self.addCleanup(self.available.stop)
        self.metadata = patch.object(ret, "_run_cargo_metadata", return_value={})
        self.metadata.start()
        self.addCleanup(self.metadata.stop)

    def test_installed_tools_do_not_execute_by_default(self):
        with patch.object(ret.subprocess, "run") as execute:
            result = run()
        execute.assert_not_called()
        self.assertEqual(result["tool_runs"], {})

    def test_requested_missing_tool_does_not_execute(self):
        for missing in ("cargo", "cargo-expand", "cargo-geiger", "cargo-miri"):
            with (
                self.subTest(missing=missing),
                patch.object(
                    ret,
                    "_tool_available",
                    side_effect=lambda name, missing=missing: name != missing,
                ),
                patch.object(ret.subprocess, "run") as execute,
            ):
                flags = {
                    "cargo": "run_expand",
                    "cargo-expand": "run_expand",
                    "cargo-geiger": "run_geiger",
                    "cargo-miri": "run_miri",
                }
                result = run(
                    **{flags[missing]: True},
                    output_dir="unused",
                    miri_test="tests::pure",
                )
                execute.assert_not_called()
                self.assertEqual(result["tool_runs"], {})

    def test_missing_output_directory_is_nonblocking_skip(self):
        with patch.object(ret.subprocess, "run") as execute:
            result = run(run_expand=True)
        execute.assert_not_called()
        self.assertTrue(
            any("--output-dir" in item["reason"] for item in result["skipped_tools"])
        )

    def test_miri_requires_nonempty_exact_test(self):
        for name in (None, "", " "):
            with (
                self.subTest(name=name),
                patch.object(ret.subprocess, "run") as execute,
            ):
                result = run(run_miri=True, miri_test=name, output_dir="unused")
                execute.assert_not_called()
                self.assertNotIn("miri", result["tool_runs"])

    def test_opted_in_commands_preserve_full_evidence_and_selection(self):
        def execute(command, **kwargs):
            kwargs["stdout"].write("expanded or inventory output\n" * 10000)
            kwargs["stderr"].write("diagnostic")
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["env"]["RUSTUP_AUTO_INSTALL"], "0")
            self.assertEqual(kwargs["timeout"], 600)
            return subprocess.CompletedProcess(command, 0)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(ret.subprocess, "run", side_effect=execute) as execute_mock,
        ):
            result = run(
                run_expand=True,
                run_geiger=True,
                run_miri=True,
                miri_test="tests::pure",
                output_dir=directory,
                package="core",
                features="foo bar",
                no_default_features=True,
            )
            self.assertEqual(execute_mock.call_count, 3)
            for tool, record in result["tool_runs"].items():
                self.assertEqual(record["status"], "completed")
                self.assertEqual(record["returncode"], 0)
                self.assertEqual(
                    Path(record["stdout_path"]).read_text(),
                    "expanded or inventory output\n" * 10000,
                )
                self.assertEqual(Path(record["stderr_path"]).read_text(), "diagnostic")
                selection = [
                    "--package",
                    "core",
                    "--features",
                    "foo bar",
                    "--no-default-features",
                ]
                commands = {
                    "cargo-expand": [
                        "cargo",
                        "expand",
                        "--lib",
                        "--color",
                        "never",
                        *selection,
                    ],
                    "cargo-geiger": ["cargo", "geiger", *selection],
                    "miri": [
                        "cargo",
                        "miri",
                        "test",
                        "--lib",
                        *selection,
                        "--",
                        "--exact",
                        "tests::pure",
                    ],
                }
                self.assertEqual(record["command"], commands[tool])
            self.assertEqual(result["findings"], [])
            self.assertEqual(result["summary"]["total_findings"], 0)

    def test_failures_are_not_clean_results_and_other_tools_continue(self):
        for failure, status in (
            (1, "failed"),
            (subprocess.TimeoutExpired("cargo", 600), "timeout"),
            (OSError("unavailable"), "error"),
        ):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as directory,
            ):

                def execute(command, failure=failure, **kwargs):
                    kwargs["stderr"].write("partial diagnostics")
                    if command[1] == "expand":
                        if isinstance(failure, Exception):
                            raise failure
                        return subprocess.CompletedProcess(command, failure)
                    return subprocess.CompletedProcess(command, 0)

                with patch.object(ret.subprocess, "run", side_effect=execute):
                    result = run(run_expand=True, run_geiger=True, output_dir=directory)
                self.assertEqual(result["tool_runs"]["cargo-expand"]["status"], status)
                self.assertEqual(
                    result["tool_runs"]["cargo-geiger"]["status"], "completed"
                )
                self.assertEqual(
                    Path(
                        result["tool_runs"]["cargo-expand"]["stderr_path"]
                    ).read_text(),
                    "partial diagnostics",
                )
                self.assertTrue(
                    any(
                        item["tool"] == "cargo-expand"
                        for item in result["skipped_tools"]
                    )
                )

    def test_output_directory_error_does_not_abort_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "file"
            blocker.write_text("not a directory")
            with patch.object(ret.subprocess, "run") as execute:
                result = run(run_geiger=True, output_dir=str(blocker))
            execute.assert_not_called()
            self.assertEqual(result["tool_runs"]["cargo-geiger"]["status"], "error")

    def test_repeat_runs_do_not_overwrite_artifacts(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                ret.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ),
        ):
            first = run(run_expand=True, output_dir=directory)
            second = run(run_expand=True, output_dir=directory)
            a = first["tool_runs"]["cargo-expand"]["stdout_path"]
            b = second["tool_runs"]["cargo-expand"]["stdout_path"]
            self.assertNotEqual(a, b)
            self.assertTrue(Path(a).is_file())
            self.assertTrue(Path(b).is_file())

    def test_cli_forwards_explicit_flags(self):
        import io

        with (
            patch.object(
                ret.sys,
                "argv",
                [
                    "run_external_tools.py",
                    ".",
                    "--run-miri",
                    "--miri-test",
                    "tests::pure",
                    "--output-dir",
                    "logs",
                ],
            ),
            patch.object(ret, "analyze", return_value={}) as analyze,
            patch.object(ret.sys, "stdout", io.StringIO()),
        ):
            ret.main()
        self.assertTrue(analyze.call_args.kwargs["run_miri"])
        self.assertEqual(analyze.call_args.kwargs["miri_test"], "tests::pure")
        self.assertFalse(analyze.call_args.kwargs["run_expand"])


if __name__ == "__main__":
    unittest.main()
