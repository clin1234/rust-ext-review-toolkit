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
pyo3 = { version = "0.29", features = ["auto-initialize"] }
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
        self.assertEqual(r["pyo3_version"], "0.29")
        self.assertEqual(r["crate_types"], ["cdylib"])


class TestPythonFloor(unittest.TestCase):
    def test_abi3t_feature_sets_floor_and_intent(self):
        with TempRustExtension(
            {"src/lib.rs": MINIMAL_PYO3_CRATE},
            pyo3_features=["abi3t-py315"],
        ) as ext:
            r = dre.discover(str(ext.root))
        self.assertEqual(r["python_floor"], "3.15")
        self.assertEqual(r["abi3t_python_floor"], "3.15")
        self.assertTrue(r["free_threading_target"])
        self.assertIn("abi3t", " ".join(r["free_threading_signals"]))

    def test_dual_abi_floors_remain_distinct(self):
        for features in (
            ["abi3-py310", "abi3t-py315"],
            ["abi3t-py315", "abi3-py310"],
        ):
            with (
                self.subTest(features=features),
                TempRustExtension(
                    {"src/lib.rs": MINIMAL_PYO3_CRATE}, pyo3_features=features
                ) as ext,
            ):
                r = dre.discover(str(ext.root))
                self.assertEqual(r["python_floor"], "3.10")
                self.assertEqual(r["abi3t_python_floor"], "3.15")

    def test_lowest_floor_wins_within_each_abi(self):
        with TempRustExtension(
            {"src/lib.rs": MINIMAL_PYO3_CRATE},
            pyo3_features=["abi3-py314", "abi3-py39", "abi3t-py315"],
        ) as ext:
            r = dre.discover(str(ext.root))
        self.assertEqual(r["python_floor"], "3.9")
        self.assertEqual(r["abi3t_python_floor"], "3.15")

    def test_bare_abi3t_does_not_invent_a_build_floor(self):
        with TempRustExtension(
            {"src/lib.rs": MINIMAL_PYO3_CRATE}, pyo3_features=["abi3t"]
        ) as ext:
            r = dre.discover(str(ext.root))
        self.assertIsNone(r["python_floor"])
        self.assertIsNone(r["abi3t_python_floor"])
        self.assertTrue(r["free_threading_target"])

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


# v0.2 regression: cryptography's `src/rust/` scan returned `pyo3_version:
# null` because the `Cargo.lock` lives one directory up at the workspace
# root, and the v0.1 lookup only checked `root / "Cargo.lock"`.
_WORKSPACE_ROOT_CARGO_TOML = """\
[workspace]
members = ["extension"]
"""

_WORKSPACE_MEMBER_CARGO_TOML = """\
[package]
name = "extension"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]

[dependencies]
pyo3 = { workspace = true }
"""

_WORKSPACE_CARGO_LOCK = """\
version = 3

[[package]]
name = "extension"
version = "0.1.0"

[[package]]
name = "pyo3"
version = "0.29.3"
"""


class TestWorkspaceCargoLock(unittest.TestCase):
    def test_pyo3_version_resolved_from_workspace_lock(self):
        with TempRustExtension(
            {
                "Cargo.toml": _WORKSPACE_ROOT_CARGO_TOML,
                "Cargo.lock": _WORKSPACE_CARGO_LOCK,
                "extension/Cargo.toml": _WORKSPACE_MEMBER_CARGO_TOML,
                "extension/src/lib.rs": MINIMAL_PYO3_CRATE,
            },
            write_cargo_toml=False,
        ) as ext:
            member = ext.root / "extension"
            r = dre.discover(str(member))
        self.assertEqual(r["pyo3_version"], "0.29.3")
        self.assertEqual(r["pyo3_version_source"], "workspace Cargo.lock")
        # The note should record where the lock came from.
        self.assertTrue(
            any("workspace `Cargo.lock`" in n for n in r["notes"]),
            f"expected workspace-lock note in: {r['notes']}",
        )


# v0.2 regression: polars puts the cdylib in workspace shim crates
# (`py-polars/runtime/polars-runtime-{32,64,compat}/src/lib.rs` =
# `pub use polars_python::c_api::*;`). v0.1 classified the actual extension
# crate as `out_of_scope: true` because the cdylib was not in its own
# Cargo.toml. v0.2: presence of `#[pymodule]` keeps the crate in scope.

_SHIM_WORKSPACE_ROOT = """\
[workspace]
members = ["extension", "shim"]
"""

_SHIM_EXTENSION_CARGO = """\
[package]
name = "ext-lib"
version = "0.1.0"
edition = "2021"

[dependencies]
pyo3 = "0.29"
"""

_SHIM_SHIM_CARGO = """\
[package]
name = "ext-shim"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]

[dependencies]
ext-lib = { path = "../extension" }
"""

_SHIM_SHIM_LIB_RS = """\
pub use ext_lib::*;
"""


class TestShimCdylibLayout(unittest.TestCase):
    def test_shim_workspace_in_scope(self):
        # The extension crate has #[pymodule] but no cdylib; sibling shim
        # exposes it. v0.2 keeps the extension crate in scope and labels
        # the detection method `workspace_shim_cdylib`.
        with TempRustExtension(
            {
                "Cargo.toml": _SHIM_WORKSPACE_ROOT,
                "extension/Cargo.toml": _SHIM_EXTENSION_CARGO,
                "extension/src/lib.rs": MINIMAL_PYO3_CRATE,
                "shim/Cargo.toml": _SHIM_SHIM_CARGO,
                "shim/src/lib.rs": _SHIM_SHIM_LIB_RS,
            },
            write_cargo_toml=False,
        ) as ext:
            r = dre.discover(str(ext.root / "extension"))
        self.assertTrue(r["is_pyo3_extension"])
        self.assertFalse(r["out_of_scope"])
        self.assertEqual(r["detection_method"], "workspace_shim_cdylib")
        self.assertTrue(
            any("workspace sibling" in n for n in r["notes"]),
            f"expected shim-sibling note in: {r['notes']}",
        )

    def test_pymodule_without_cdylib_or_shim_in_scope_with_caveat(self):
        # `#[pymodule]` + pyo3 dep + no cdylib + no shim sibling: in scope,
        # but flagged with a note (the cdylib might be in a non-workspace
        # downstream consumer the toolkit can't see).
        with TempRustExtension(
            {"src/lib.rs": MINIMAL_PYO3_CRATE},
            cargo_toml=_CARGO_NO_CDYLIB.replace("embedder", "lonely"),
        ) as ext:
            # Replace src/main.rs scaffolding with src/lib.rs + pymodule;
            # _CARGO_NO_CDYLIB is reused but we override the source.
            r = dre.discover(str(ext.root))
        # Note: the existing `_CARGO_NO_CDYLIB` fixture omits the [lib]
        # section; that's OK -- we still get the pyo3 dep + no cdylib +
        # #[pymodule] combination this branch handles.
        self.assertTrue(r["is_pyo3_extension"])
        self.assertFalse(r["out_of_scope"])
        self.assertEqual(r["detection_method"], "pymodule_no_cdylib")


if __name__ == "__main__":
    unittest.main()
