"""Tests for scan_lifetime_handles — PyO3 handle-kind audit."""

import unittest

from fixtures import MINIMAL_PYO3_CRATE, PYO3_CRATE_WITH_PYCLASS
from helpers import TempRustExtension, import_script

sub = import_script("scan_lifetime_handles")


def run(code: str, **kw) -> dict:
    with TempRustExtension({"src/lib.rs": code}, **kw) as ext:
        return sub.analyze(str(ext.root))


def findings_of(code: str, **kw) -> list[dict]:
    return run(code, **kw)["findings"]


def types_of(code: str, **kw) -> set[str]:
    return {f["type"] for f in findings_of(code, **kw)}


_LEGACY_REF = """\
use pyo3::prelude::*;
use pyo3::types::PyDict;

fn process(d: &PyDict) -> PyResult<()> {
    Ok(())
}
"""

_MODERN_REF = """\
use pyo3::prelude::*;
use pyo3::types::PyDict;

fn process(d: &Bound<'_, PyDict>) -> PyResult<()> {
    Ok(())
}
"""

_AS_REF = """\
use pyo3::prelude::*;

fn get_ref<'py>(py: Python<'py>, obj: Py<PyAny>) {
    let _r = obj.as_ref(py);
}
"""

_AS_REF_TN = """\
fn convert(s: String) -> usize {
    s.as_ref().len()
}
"""

_PY_PARAM_BIND = """\
use pyo3::prelude::*;

fn describe<'py>(py: Python<'py>, obj: Py<PyAny>) -> PyResult<()> {
    let _bound = obj.bind(py);
    Ok(())
}
"""

_PY_PARAM_STORED = """\
use pyo3::prelude::*;

fn passthrough(obj: Py<PyAny>) -> Py<PyAny> {
    obj
}
"""

_BORROWED = """\
use pyo3::prelude::*;

fn peek<'a, 'py>(x: Borrowed<'a, 'py, PyAny>) -> PyResult<()> {
    Ok(())
}
"""


class TestLegacyRefType(unittest.TestCase):
    def test_gil_ref_on_modern_crate_is_fix(self):
        findings = findings_of(_LEGACY_REF)  # default pyo3 version 0.28
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "legacy_ref_type")
        self.assertEqual(findings[0]["classification"], "FIX")

    def test_gil_ref_on_old_crate_is_consider(self):
        findings = findings_of(_LEGACY_REF, pyo3_version="0.22")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "CONSIDER")

    def test_bound_reference_is_not_flagged(self):
        self.assertEqual(findings_of(_MODERN_REF), [])


class TestLegacyAsRef(unittest.TestCase):
    def test_as_ref_with_py_arg_on_modern_crate_is_fix(self):
        findings = findings_of(_AS_REF)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "legacy_as_ref")
        self.assertEqual(findings[0]["classification"], "FIX")

    def test_plain_as_ref_without_args_not_flagged(self):
        self.assertEqual(findings_of(_AS_REF_TN), [])


class TestPyParamShouldBeBound(unittest.TestCase):
    def test_py_param_only_bound_is_consider(self):
        findings = findings_of(_PY_PARAM_BIND)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "py_param_should_be_bound")
        self.assertEqual(findings[0]["classification"], "CONSIDER")
        self.assertEqual(findings[0]["details"]["param"], "obj")

    def test_stored_py_param_not_flagged(self):
        self.assertNotIn("py_param_should_be_bound", types_of(_PY_PARAM_STORED))


class TestUnjustifiedBorrowed(unittest.TestCase):
    def test_borrowed_in_signature_is_consider(self):
        findings = findings_of(_BORROWED)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "unjustified_borrowed")
        self.assertEqual(findings[0]["classification"], "CONSIDER")


class TestNegatives(unittest.TestCase):
    def test_minimal_crate_is_clean(self):
        self.assertEqual(findings_of(MINIMAL_PYO3_CRATE), [])

    def test_pyclass_crate_is_clean(self):
        self.assertEqual(findings_of(PYO3_CRATE_WITH_PYCLASS), [])


# v0.2 regression: PyO3 0.28's `FromPyObject::extract` mandates
# `Borrowed<'a, 'py, PyAny>` as the parameter type. Polars hit 61/61 FPs
# from this single shape: every `Wrap<T>` / per-class `FromPyObject` impl
# triggered `unjustified_borrowed`. The fix suppresses findings whose
# enclosing impl is `FromPyObject`.

_BORROWED_IN_FROMPYOBJECT = """\
use pyo3::prelude::*;

struct Wrap<T>(T);

impl<'py> FromPyObject<'py> for Wrap<i64> {
    fn extract_bound(ob: Borrowed<'_, 'py, PyAny>) -> PyResult<Self> {
        Ok(Wrap(ob.extract()?))
    }
}
"""

_BORROWED_IN_QUALIFIED_FROMPYOBJECT = """\
use pyo3::prelude::*;

struct Wrap<T>(T);

impl<'py> pyo3::FromPyObject<'py> for Wrap<i64> {
    fn extract_bound(ob: Borrowed<'_, 'py, PyAny>) -> PyResult<Self> {
        Ok(Wrap(ob.extract()?))
    }
}
"""

_BORROWED_OUTSIDE_FROMPYOBJECT = """\
use pyo3::prelude::*;

// A genuine `Borrowed` choice -- in an ordinary fn, not a FromPyObject impl.
// This should still be flagged so the reviewer can confirm the lifetime
// extra-ness is justified.
fn peek<'a, 'py>(x: Borrowed<'a, 'py, PyAny>) -> PyResult<()> {
    let _ = x;
    Ok(())
}
"""


class TestBorrowedFromPyObjectSuppression(unittest.TestCase):
    def test_borrowed_inside_frompyobject_not_flagged(self):
        self.assertNotIn("unjustified_borrowed", types_of(_BORROWED_IN_FROMPYOBJECT))

    def test_borrowed_inside_qualified_frompyobject_not_flagged(self):
        self.assertNotIn(
            "unjustified_borrowed", types_of(_BORROWED_IN_QUALIFIED_FROMPYOBJECT)
        )

    def test_borrowed_in_ordinary_fn_still_flagged(self):
        # Sanity: the suppression is scoped to FromPyObject impls, not blanket.
        self.assertIn("unjustified_borrowed", types_of(_BORROWED_OUTSIDE_FROMPYOBJECT))


if __name__ == "__main__":
    unittest.main()
