"""Tests for discover_rust_ext — PyO3 crate detection."""

import unittest

from fixtures import MINIMAL_PYO3_CRATE
from helpers import TempRustExtension, import_script

dre = import_script("discover_rust_ext")

_MATURIN_PYPROJECT = """\
[build-system]
requires = ["maturin>=1.0,<2.0"]
build-backend = "maturin"
"""

_CARGO_NO_CDYLIB = """\
[package]
name = "embedder"
version = "0.1.0"
edition = "2021"

[dependencies]
pyo3 = { version = "0.28", features = ["auto-initialize"] }
"""

_CARGO_PLAIN = """\
[package]
name = "plain"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]
"""

_FT_LIB_RS = """\
use pyo3::prelude::*;

#[pymodule]
fn ft(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.gil_used(false)?;
    Ok(())
}
"""


class TestCargoCdylibDetection(unittest.TestCase):
    def test_minimal_crate(self):
        with TempRustExtension({"src/lib.rs": MINIMAL_PYO3_CRATE}) as ext:
            r = dre.discover(str(ext.root))
        self.assertTrue(r["is_pyo3_extension"])
        self.assertFalse(r["out_of_scope"])
        self.assertEqual(r["detection_method"], "cargo_toml_cdylib")
        self.assertEqual(r["crate_name"], "testcrate")
        self.assertEqual(r["module_name"], "minimal")
        self.assertEqual(r["pyo3_version"], "0.28")
        self.assertEqual(r["crate_types"], ["cdylib"])


class TestPythonFloor(unittest.TestCase):
    def test_abi3_feature_sets_floor(self):
        with TempRustExtension(
            {"src/lib.rs": MINIMAL_PYO3_CRATE},
            pyo3_features=["extension-module", "abi3-py310"],
        ) as ext:
            r = dre.discover(str(ext.root))
        self.assertEqual(r["python_floor"], "3.10")
        self.assertIn("abi3-py310", r["pyo3_features"])


class TestBuildBackend(unittest.TestCase):
    def test_maturin_pyproject(self):
        with TempRustExtension(
            {"src/lib.rs": MINIMAL_PYO3_CRATE}, pyproject_toml=_MATURIN_PYPROJECT
        ) as ext:
            r = dre.discover(str(ext.root))
        self.assertEqual(r["build_backend"], "maturin")


class TestOutOfScope(unittest.TestCase):
    def test_pyo3_without_cdylib(self):
        with TempRustExtension(
            {"src/main.rs": "use pyo3::prelude::*;\nfn main() {}\n"},
            cargo_toml=_CARGO_NO_CDYLIB,
        ) as ext:
            r = dre.discover(str(ext.root))
        self.assertTrue(r["is_pyo3_extension"])
        self.assertTrue(r["out_of_scope"])
        self.assertEqual(r["detection_method"], "cargo_toml_no_cdylib")


class TestFallback(unittest.TestCase):
    def test_rs_fallback_without_cargo_toml(self):
        with TempRustExtension(
            {"src/lib.rs": "use pyo3::prelude::*;\n"}, write_cargo_toml=False
        ) as ext:
            r = dre.discover(str(ext.root))
        self.assertTrue(r["is_pyo3_extension"])
        self.assertEqual(r["detection_method"], "rs_fallback")


class TestFreeThreading(unittest.TestCase):
    def test_gil_used_is_a_signal(self):
        with TempRustExtension({"src/lib.rs": _FT_LIB_RS}) as ext:
            r = dre.discover(str(ext.root))
        self.assertTrue(r["free_threading_target"])
        self.assertTrue(r["free_threading_signals"])


class TestNotPyO3(unittest.TestCase):
    def test_plain_rust_crate(self):
        with TempRustExtension(
            {"src/lib.rs": "pub fn add(a: i64, b: i64) -> i64 { a + b }"},
            cargo_toml=_CARGO_PLAIN,
        ) as ext:
            r = dre.discover(str(ext.root))
        self.assertFalse(r["is_pyo3_extension"])


if __name__ == "__main__":
    unittest.main()
