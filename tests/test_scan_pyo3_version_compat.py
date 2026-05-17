"""Tests for scan_pyo3_version_compat — PyO3 version-compatibility audit."""

import unittest

from fixtures import MINIMAL_PYO3_CRATE
from helpers import TempRustExtension, import_script

sub = import_script("scan_pyo3_version_compat")

_FT_CI = """\
jobs:
  test:
    strategy:
      matrix:
        python-version: ["3.13t"]
"""


def run(code: str, **kw) -> dict:
    with TempRustExtension({"src/lib.rs": code}, **kw) as ext:
        return sub.analyze(str(ext.root))


def findings_of(code: str, **kw) -> list[dict]:
    return run(code, **kw)["findings"]


def types_of(code: str, **kw) -> set[str]:
    return {f["type"] for f in findings_of(code, **kw)}


_WITH_GIL = """\
use pyo3::prelude::*;

fn run() {
    Python::with_gil(|py| {
        let _ = py;
    });
}
"""

# `_bound`-suffixed names must NOT be flagged: the suffix collides with the
# current `Py::into_bound` method and stdlib `lower_bound`/`upper_bound`.
# Phase 7 real-world validation found this false positive; the `bound_suffix`
# table entry is `detect: skip` as a result.
_BOUND_SUFFIX = """\
use pyo3::prelude::*;
use pyo3::types::PyString;

fn make<'py>(py: Python<'py>, obj: Py<PyAny>) -> Bound<'py, PyString> {
    let _b = obj.into_bound(py);
    PyString::new_bound(py, "hello")
}
"""

_TO_OBJECT = """\
use pyo3::prelude::*;

fn convert<'py>(py: Python<'py>, value: i64) -> PyObject {
    value.to_object(py)
}
"""

_GIL_ONCE_CELL = """\
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;

static CACHE: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
"""


class TestRenamedApi(unittest.TestCase):
    def test_with_gil_on_modern_crate_is_renamed(self):
        findings = findings_of(_WITH_GIL)  # default pyo3 version 0.28
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "renamed_api")
        self.assertEqual(findings[0]["classification"], "CONSIDER")

    def test_with_gil_on_pre_rename_crate_not_flagged(self):
        # `with_gil` was renamed in 0.25; on 0.24 it is the current name.
        self.assertEqual(findings_of(_WITH_GIL, pyo3_version="0.24"), [])

    def test_gil_once_cell_type_is_renamed(self):
        findings = [
            f for f in findings_of(_GIL_ONCE_CELL) if f["type"] == "renamed_api"
        ]
        self.assertEqual(len(findings), 1)
        self.assertIn("GILOnceCell", findings[0]["description"])


class TestBoundSuffixNotFlagged(unittest.TestCase):
    def test_bound_suffix_calls_are_not_flagged(self):
        # Neither `PyString::new_bound` nor the current `Py::into_bound` is
        # flagged -- the `_bound` suffix is too ambiguous to detect.
        self.assertEqual(findings_of(_BOUND_SUFFIX), [])


class TestDeprecatedApi(unittest.TestCase):
    def test_to_object_on_modern_crate_is_deprecated(self):
        findings = findings_of(_TO_OBJECT)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "deprecated_api")
        self.assertEqual(findings[0]["classification"], "CONSIDER")


class TestAbi3FreeThreading(unittest.TestCase):
    def test_abi3_plus_free_threading_is_flagged(self):
        with TempRustExtension(
            {"src/lib.rs": MINIMAL_PYO3_CRATE, ".github/workflows/ci.yml": _FT_CI},
            pyo3_features=["extension-module", "abi3-py313"],
        ) as ext:
            findings = sub.analyze(str(ext.root))["findings"]
        conflict = [f for f in findings if f["type"] == "abi3_free_threading_conflict"]
        self.assertEqual(len(conflict), 1)
        self.assertEqual(conflict[0]["classification"], "CONSIDER")

    def test_abi3_without_free_threading_not_flagged(self):
        types = types_of(MINIMAL_PYO3_CRATE, pyo3_features=["abi3-py310"])
        self.assertNotIn("abi3_free_threading_conflict", types)


class TestNegatives(unittest.TestCase):
    def test_minimal_crate_is_clean(self):
        self.assertEqual(findings_of(MINIMAL_PYO3_CRATE), [])


if __name__ == "__main__":
    unittest.main()
