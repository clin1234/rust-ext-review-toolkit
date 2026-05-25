"""Tests for scan_module_init — `#[pymodule]` initialization audit."""

import unittest

from fixtures import MINIMAL_PYO3_CRATE, PYO3_CRATE_WITH_BUGS, PYO3_CRATE_WITH_PYCLASS
from helpers import TempRustExtension, import_script

sub = import_script("scan_module_init")


def run(code: str, **kw) -> dict:
    with TempRustExtension({"src/lib.rs": code}, **kw) as ext:
        return sub.analyze(str(ext.root))


def findings_of(code: str, **kw) -> list[dict]:
    return run(code, **kw)["findings"]


def types_of(code: str, **kw) -> set[str]:
    return {f["type"] for f in findings_of(code, **kw)}


_BAD_RETURN = """\
use pyo3::prelude::*;

#[pymodule]
fn mymod(m: &Bound<'_, PyModule>) {
}
"""

_LAZY_PY_STATIC = """\
use pyo3::prelude::*;
use std::sync::LazyLock;

static CACHE: LazyLock<Py<PyAny>> = LazyLock::new(|| todo!());

#[pymodule]
fn cached(m: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}
"""


class TestModuleLevelPyStatic(unittest.TestCase):
    def test_oncelock_py_static_is_consider(self):
        findings = [
            f
            for f in findings_of(PYO3_CRATE_WITH_BUGS)
            if f["type"] == "module_level_py_static"
        ]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "CONSIDER")
        self.assertEqual(findings[0]["details"]["name"], "GLOBAL")

    def test_lazylock_py_static_is_flagged(self):
        self.assertIn("module_level_py_static", types_of(_LAZY_PY_STATIC))


class TestModuleAddUnchecked(unittest.TestCase):
    def test_add_class_without_question_mark_is_fix(self):
        findings = [
            f
            for f in findings_of(PYO3_CRATE_WITH_BUGS)
            if f["type"] == "module_add_unchecked"
        ]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "FIX")
        self.assertIn("add_class", findings[0]["details"]["callee"])

    def test_question_mark_propagation_not_flagged(self):
        self.assertNotIn("module_add_unchecked", types_of(MINIMAL_PYO3_CRATE))
        self.assertNotIn("module_add_unchecked", types_of(PYO3_CRATE_WITH_PYCLASS))


class TestPymoduleReturnType(unittest.TestCase):
    def test_missing_pyresult_return_is_consider(self):
        findings = findings_of(_BAD_RETURN)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "pymodule_return_type")
        self.assertEqual(findings[0]["classification"], "CONSIDER")

    def test_correct_return_type_not_flagged(self):
        self.assertNotIn("pymodule_return_type", types_of(MINIMAL_PYO3_CRATE))


class TestNegatives(unittest.TestCase):
    def test_minimal_crate_is_clean(self):
        self.assertEqual(findings_of(MINIMAL_PYO3_CRATE), [])

    def test_pyclass_crate_is_clean(self):
        self.assertEqual(findings_of(PYO3_CRATE_WITH_PYCLASS), [])


# v0.2 regression: `PyOnceLock<Py<…>>` (PyO3's GIL-aware once-cell, distinct
# from `std::sync::OnceLock`) holds a process-global Python object. The
# polars review hit this -- `NDARRAY_TYPE` and `DECIMAL_TYPE` in
# `src/conversion/any_value.rs` and similar statics in non-canonical
# locations were silently missed because `PyOnceLock` was not in the
# scanner's once-cell wrapper list.

_PYONCELOCK_STATIC = """\
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyType;

static NDARRAY_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();

#[pymodule]
fn m(m: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}
"""

_PYONCELOCK_FUNCTION_LOCAL = """\
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyType;

fn pick<'py>(py: Python<'py>) {
    static DECIMAL_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
    let _ = DECIMAL_TYPE.import(py, "decimal", "Decimal");
}

#[pymodule]
fn m(m: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}
"""


class TestPyOnceLockStatic(unittest.TestCase):
    def test_pyoncelock_module_level_static_flagged(self):
        types = {f["type"] for f in findings_of(_PYONCELOCK_STATIC)}
        self.assertIn("module_level_py_static", types)

    def test_pyoncelock_function_local_static_flagged(self):
        # Function-local statics with the PyOnceLock shape are equally
        # process-global; the scanner must catch them too.
        types = {f["type"] for f in findings_of(_PYONCELOCK_FUNCTION_LOCAL)}
        self.assertIn("module_level_py_static", types)


if __name__ == "__main__":
    unittest.main()
