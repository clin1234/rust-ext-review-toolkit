"""Tests for scan_common — shared scanner utilities."""

import unittest
from pathlib import Path

from helpers import TempRustExtension, import_script

sc = import_script("scan_common")
rtu = import_script("rust_ts_utils")


class TestProjectRoot(unittest.TestCase):
    def test_finds_cargo_toml_root(self):
        with TempRustExtension({"src/lib.rs": "fn x() {}"}) as ext:
            found = sc.find_project_root(ext.root / "src" / "lib.rs")
            self.assertEqual(found.resolve(), ext.root.resolve())


class TestDiscoverRustFiles(unittest.TestCase):
    def test_finds_rs_skips_target(self):
        with TempRustExtension(
            {
                "src/lib.rs": "fn a() {}",
                "src/util.rs": "fn b() {}",
                "target/debug/junk.rs": "fn junk() {}",
            }
        ) as ext:
            files = sc.discover_rust_files(ext.root)
            rel = sorted(str(p.relative_to(ext.root)) for p in files)
        self.assertEqual(rel, ["src/lib.rs", "src/util.rs"])

    def test_max_files_cap(self):
        with TempRustExtension({"src/a.rs": "", "src/b.rs": "", "src/c.rs": ""}) as ext:
            self.assertEqual(len(sc.discover_rust_files(ext.root, max_files=2)), 2)


class TestLoadDataFile(unittest.TestCase):
    def test_loads_pyo3_api_tables(self):
        data = sc.load_data_file("pyo3_api_tables.json")
        self.assertIn("apis", data)
        self.assertIn("python_with_gil", data["apis"])

    def test_loads_non_send_sync_types(self):
        data = sc.load_data_file("non_send_sync_types.json")
        self.assertTrue(any(t["path"] == "std::rc::Rc" for t in data["types"]))

    def test_missing_file_returns_empty(self):
        self.assertEqual(sc.load_data_file("does_not_exist.json"), {})


class TestMakeFinding(unittest.TestCase):
    def test_shape(self):
        f = sc.make_finding(
            "unsafe_ffi_call",
            classification="CONSIDER",
            description="raw ffi call",
            file="src/lib.rs",
            line=12,
            category="soundness",
        )
        self.assertEqual(f["type"], "unsafe_ffi_call")
        self.assertEqual(f["classification"], "CONSIDER")
        self.assertEqual(f["confidence"], "HIGH")
        self.assertEqual(f["line"], 12)
        self.assertEqual(f["category"], "soundness")
        self.assertEqual(f["details"], {})


class TestDeduplicate(unittest.TestCase):
    def test_collapses_similar(self):
        findings = [
            sc.make_finding(
                "t", classification="FIX", description="bug at line 10", file="a.rs"
            ),
            sc.make_finding(
                "t", classification="FIX", description="bug at line 22", file="a.rs"
            ),
        ]
        deduped = sc.deduplicate_findings(findings)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["duplicate_count"], 1)

    def test_keeps_distinct(self):
        findings = [
            sc.make_finding("t1", classification="FIX", description="x", file="a.rs"),
            sc.make_finding("t2", classification="FIX", description="y", file="a.rs"),
        ]
        self.assertEqual(len(sc.deduplicate_findings(findings)), 2)

    def test_keeps_distinct_targets_in_same_file(self):
        # v0.2 regression: pre-v0.2 the description normaliser back-ticked out
        # identifier text, so two findings of the same TYPE on two different
        # TARGETS in one file collapsed into one. The cryptography review
        # missed `declarative_asn1::Type` this way. Including `function` in
        # the dedup key restores per-target distinction.
        findings = [
            sc.make_finding(
                "missing_traverse",
                classification="CONSIDER",
                description="`#[pyclass] Foo` holds `Py<PyAny>` but lacks `__traverse__`",
                file="lib.rs",
                function="Foo",
            ),
            sc.make_finding(
                "missing_traverse",
                classification="CONSIDER",
                description="`#[pyclass] Bar` holds `Py<PyAny>` but lacks `__traverse__`",
                file="lib.rs",
                function="Bar",
            ),
        ]
        deduped = sc.deduplicate_findings(findings)
        self.assertEqual(len(deduped), 2)
        self.assertEqual({f["function"] for f in deduped}, {"Foo", "Bar"})


class TestRelativePath(unittest.TestCase):
    def test_under_root(self):
        self.assertEqual(
            sc.relative_path(Path("/crate/src/lib.rs"), Path("/crate")),
            "src/lib.rs",
        )

    def test_outside_root_returns_absolute(self):
        self.assertEqual(
            sc.relative_path(Path("/other/x.rs"), Path("/crate")), "/other/x.rs"
        )


class TestBuildReport(unittest.TestCase):
    _DISCOVERY = {
        "project_root": "/crate",
        "scan_root": "/crate",
        "crate_name": "demo",
        "module_name": "demo",
        "pyo3_version": "0.29",
        "pyo3_features": ["extension-module"],
        "python_floor": None,
        "build_backend": "cargo",
        "free_threading_target": False,
        "source_files": ["src/lib.rs"],
    }

    def test_envelope_shape(self):
        findings = [
            sc.make_finding("t1", classification="FIX", description="a"),
            sc.make_finding("t1", classification="CONSIDER", description="b"),
            sc.make_finding("t2", classification="FIX", description="c"),
        ]
        report = sc.build_report(self._DISCOVERY, findings, functions_analyzed=7)
        self.assertEqual(report["project_root"], "/crate")
        self.assertEqual(report["functions_analyzed"], 7)
        self.assertEqual(report["findings"], findings)
        self.assertEqual(report["crate_info"]["crate_name"], "demo")
        self.assertEqual(report["crate_info"]["pyo3_version"], "0.29")
        self.assertTrue(report["crate_info"]["tree_sitter_available"])

    def test_summary_counts(self):
        findings = [
            sc.make_finding("t1", classification="FIX", description="a"),
            sc.make_finding("t1", classification="CONSIDER", description="b"),
            sc.make_finding("t2", classification="FIX", description="c"),
        ]
        report = sc.build_report(self._DISCOVERY, findings)
        self.assertEqual(report["summary"]["by_type"], {"t1": 2, "t2": 1})
        self.assertEqual(
            report["summary"]["by_classification"], {"FIX": 2, "CONSIDER": 1}
        )

    def test_empty_findings(self):
        report = sc.build_report(self._DISCOVERY, [])
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["summary"]["by_type"], {})


class TestParseCommonArgs(unittest.TestCase):
    def test_target_and_max_files(self):
        self.assertEqual(
            sc.parse_common_args(["crate", "--max-files", "7"]), ("crate", 7)
        )

    def test_defaults(self):
        self.assertEqual(sc.parse_common_args([]), (".", 0))

    def test_flag_before_target(self):
        self.assertEqual(sc.parse_common_args(["--max-files", "3", "x"]), ("x", 3))


class TestSuppression(unittest.TestCase):
    def test_safety_comment_suppresses(self):
        code = (
            "fn f() {\n"
            "    // SAFETY: obj is valid and the GIL is held\n"
            "    unsafe { g(); }\n"
            "}\n"
        )
        src = code.encode("utf-8")
        tree = rtu.parse_bytes(src)
        self.assertTrue(sc.is_suppressed_by_comment(src, tree, 3))

    def test_plain_comment_does_not_suppress(self):
        code = "fn f() {\n    // just a note\n    unsafe { g(); }\n}\n"
        src = code.encode("utf-8")
        tree = rtu.parse_bytes(src)
        self.assertFalse(sc.is_suppressed_by_comment(src, tree, 3))


if __name__ == "__main__":
    unittest.main()
