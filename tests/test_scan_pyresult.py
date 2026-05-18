"""Tests for scan_pyresult — PyResult propagation audit."""

import unittest

from fixtures import (
    MINIMAL_PYO3_CRATE,
    PYO3_CRATE_WITH_BUGS,
    PYO3_CRATE_WITH_PYCLASS,
)
from helpers import TempRustExtension, import_script

sub = import_script("scan_pyresult")


def run(code: str, **kw) -> dict:
    """Analyze a single-file crate and return the report."""
    with TempRustExtension({"src/lib.rs": code}, **kw) as ext:
        return sub.analyze(str(ext.root))


def findings_of(code: str, **kw) -> list[dict]:
    return run(code, **kw)["findings"]


def types_of(code: str, **kw) -> set[str]:
    return {f["type"] for f in findings_of(code, **kw)}


# --- Pattern 1: .unwrap() / .expect() on a PyResult ------------------------

_UNWRAP_TP = """\
use pyo3::prelude::*;

#[pyfunction]
fn bad(py: Python<'_>) -> PyObject {
    py.import_bound("json").unwrap().getattr("dumps").unwrap().into()
}
"""

_EXPECT_TP = """\
use pyo3::prelude::*;

#[pyfunction]
fn risky(py: Python<'_>) -> PyResult<()> {
    let _m = py.import("os").expect("os import failed");
    Ok(())
}
"""

_UNWRAP_TN = """\
use pyo3::prelude::*;

fn helper(obj: &Bound<'_, PyAny>) {
    let _v = obj.getattr("x").unwrap();
}
"""


class TestUnwrapOnPyResult(unittest.TestCase):
    def test_unwrap_in_pyfunction_is_fix(self):
        findings = [
            f for f in findings_of(_UNWRAP_TP) if f["type"] == "unwrap_on_pyresult"
        ]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "FIX")
        self.assertEqual(findings[0]["details"]["method"], "unwrap")

    def test_expect_in_pyfunction_is_consider(self):
        findings = findings_of(_EXPECT_TP)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "unwrap_on_pyresult")
        self.assertEqual(findings[0]["classification"], "CONSIDER")
        self.assertEqual(findings[0]["details"]["method"], "expect")

    def test_unwrap_outside_pyo3_callable_not_flagged(self):
        self.assertEqual(findings_of(_UNWRAP_TN), [])


# --- Pattern 2: .ok() discards the PyErr -----------------------------------

_OK_TP = """\
use pyo3::prelude::*;

#[pyfunction]
fn closer(obj: &Bound<'_, PyAny>) -> PyResult<()> {
    obj.call_method0("close").ok();
    Ok(())
}
"""

_OK_TN = """\
fn helper() {
    let r: Result<i32, ()> = Ok(1);
    r.ok();
}
"""


class TestOkOnPyResult(unittest.TestCase):
    def test_ok_on_pyresult_is_consider(self):
        findings = findings_of(_OK_TP)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "ok_on_pyresult")
        self.assertEqual(findings[0]["classification"], "CONSIDER")

    def test_ok_on_plain_result_not_flagged(self):
        self.assertEqual(findings_of(_OK_TN), [])


# --- Pattern 3: let _ = <pyresult> -----------------------------------------

_LET_UNDERSCORE_TP = """\
use pyo3::prelude::*;

#[pyfunction]
fn dropper(obj: &Bound<'_, PyAny>) -> PyResult<()> {
    let _ = obj.getattr("name");
    Ok(())
}
"""

_LET_UNDERSCORE_TN = """\
fn helper() {
    let _ = compute_total();
}
"""


class TestLetUnderscorePyResult(unittest.TestCase):
    def test_let_underscore_pyresult_is_consider(self):
        findings = findings_of(_LET_UNDERSCORE_TP)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "let_underscore_pyresult")
        self.assertEqual(findings[0]["classification"], "CONSIDER")

    def test_let_underscore_non_pyresult_not_flagged(self):
        self.assertEqual(findings_of(_LET_UNDERSCORE_TN), [])


# --- Pattern 4: PyResult-returning call dropped as a statement -------------

_UNHANDLED_TP = """\
use pyo3::prelude::*;

#[pyfunction]
fn adder(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("answer", 42);
    Ok(())
}
"""

_UNHANDLED_TN = """\
use pyo3::prelude::*;

#[pyfunction]
fn adder(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("answer", 42)?;
    Ok(())
}
"""


class TestUnhandledPyResultStatement(unittest.TestCase):
    def test_dropped_pyresult_statement_is_consider(self):
        findings = findings_of(_UNHANDLED_TP)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "unhandled_pyresult_statement")
        self.assertEqual(findings[0]["classification"], "CONSIDER")

    def test_question_mark_propagation_not_flagged(self):
        self.assertEqual(findings_of(_UNHANDLED_TN), [])


# --- Pattern 5: exception clobbering ---------------------------------------

_CLOBBER_TP = """\
use pyo3::prelude::*;

#[pyfunction]
fn clobber(py: Python<'_>) -> PyResult<()> {
    let _saved = py.import("sys").err();
    py.import("os")?;
    Ok(())
}
"""

_CLOBBER_TN = """\
use pyo3::prelude::*;

#[pyfunction]
fn safe(py: Python<'_>) -> PyResult<()> {
    let saved = py.import("sys").err();
    Ok(())
}
"""


class TestExceptionClobbering(unittest.TestCase):
    def test_err_capture_then_fallible_call_is_flagged(self):
        findings = [
            f for f in findings_of(_CLOBBER_TP) if f["type"] == "exception_clobbering"
        ]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "CONSIDER")
        self.assertEqual(findings[0]["confidence"], "LOW")

    def test_err_capture_without_later_fallible_not_flagged(self):
        types = types_of(_CLOBBER_TN)
        self.assertNotIn("exception_clobbering", types)


# --- Patterns 6 & 7: PyErr state juggling / silent clear -------------------

_PYERR_STATE_TP = """\
use pyo3::prelude::*;

fn restore_it(py: Python<'_>, err: PyErr) {
    PyErr::restore(err, py);
}
"""

_PYERR_STATE_TN = """\
fn restore_it(widget: Widget) {
    widget.restore();
}
"""

_PYERR_CLEAR_TP = """\
use pyo3::prelude::*;
use pyo3::ffi;

fn clear_it() {
    unsafe { ffi::PyErr_Clear(); }
}
"""


class TestPyErrState(unittest.TestCase):
    def test_pyerr_restore_is_flagged(self):
        findings = findings_of(_PYERR_STATE_TP)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "pyerr_state_manipulation")
        self.assertEqual(findings[0]["classification"], "CONSIDER")

    def test_non_pyerr_restore_not_flagged(self):
        self.assertNotIn("pyerr_state_manipulation", types_of(_PYERR_STATE_TN))

    def test_pyerr_clear_is_flagged(self):
        findings = findings_of(_PYERR_CLEAR_TP)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "silent_pyerr_clear")
        self.assertEqual(findings[0]["classification"], "CONSIDER")


# --- True negatives and edge cases -----------------------------------------

_PYMETHODS_UNWRAP = """\
use pyo3::prelude::*;

#[pyclass]
struct Widget {
    obj: Py<PyAny>,
}

#[pymethods]
impl Widget {
    fn name(&self, py: Python<'_>) -> String {
        self.obj.getattr(py, "name").unwrap().to_string()
    }
}
"""

_SUPPRESSED_UNWRAP = """\
use pyo3::prelude::*;

#[pyfunction]
fn known_good(py: Python<'_>) -> PyResult<()> {
    // rust-ext-safe: the json module is always importable in our embedding.
    let _m = py.import("json").unwrap();
    Ok(())
}
"""


class TestNegativesAndEdges(unittest.TestCase):
    def test_clean_crates_have_no_findings(self):
        self.assertEqual(findings_of(MINIMAL_PYO3_CRATE), [])
        self.assertEqual(findings_of(PYO3_CRATE_WITH_PYCLASS), [])

    def test_unwrap_inside_pymethods_is_fix(self):
        findings = findings_of(_PYMETHODS_UNWRAP)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "unwrap_on_pyresult")
        self.assertEqual(findings[0]["classification"], "FIX")
        self.assertEqual(findings[0]["function"], "name")

    def test_suppression_comment_downgrades_to_acceptable(self):
        findings = findings_of(_SUPPRESSED_UNWRAP)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "ACCEPTABLE")

    def test_bugs_fixture_surfaces_unwrap_and_unhandled(self):
        types = types_of(PYO3_CRATE_WITH_BUGS)
        self.assertIn("unwrap_on_pyresult", types)
        self.assertIn("unhandled_pyresult_statement", types)


if __name__ == "__main__":
    unittest.main()
