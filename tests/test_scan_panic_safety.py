"""Tests for scan_panic_safety — panic-across-the-FFI-boundary audit."""

import unittest

from fixtures import MINIMAL_PYO3_CRATE, PYO3_CRATE_WITH_BUGS
from helpers import TempRustExtension, import_script

sub = import_script("scan_panic_safety")


def run(code: str, **kw) -> dict:
    with TempRustExtension({"src/lib.rs": code}, **kw) as ext:
        return sub.analyze(str(ext.root))


def findings_of(code: str, **kw) -> list[dict]:
    return run(code, **kw)["findings"]


def types_of(code: str, **kw) -> set[str]:
    return {f["type"] for f in findings_of(code, **kw)}


# --- panic_unwrap ----------------------------------------------------------

_UNWRAP_TP = """\
use pyo3::prelude::*;

#[pyfunction]
fn first(v: Vec<i32>) -> i32 {
    *v.first().unwrap()
}
"""

_PYRESULT_UNWRAP = """\
use pyo3::prelude::*;

#[pyfunction]
fn imp(py: Python<'_>) -> PyObject {
    py.import("json").unwrap().into()
}
"""


class TestPanicUnwrap(unittest.TestCase):
    def test_unwrap_on_option_is_consider(self):
        findings = findings_of(_UNWRAP_TP)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "panic_unwrap")
        self.assertEqual(findings[0]["classification"], "CONSIDER")
        self.assertEqual(findings[0]["details"]["reached_via"], "direct")

    def test_pyresult_unwrap_is_not_flagged_here(self):
        # `.unwrap()` on a PyResult belongs to pyresult-propagation-checker.
        self.assertNotIn("panic_unwrap", types_of(_PYRESULT_UNWRAP))


# --- index_panic -----------------------------------------------------------

_INDEX_TP = """\
use pyo3::prelude::*;

#[pyfunction]
fn at(v: Vec<i32>) -> i32 {
    v[0]
}
"""


class TestIndexPanic(unittest.TestCase):
    def test_indexing_is_consider(self):
        findings = findings_of(_INDEX_TP)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "index_panic")
        self.assertEqual(findings[0]["classification"], "CONSIDER")


# --- panic_macro -----------------------------------------------------------

_MACRO_TP = """\
use pyo3::prelude::*;

#[pyfunction]
fn not_done() -> i32 {
    todo!()
}
"""

_DEBUG_ASSERT = """\
use pyo3::prelude::*;

#[pyfunction]
fn checked(x: i32) -> i32 {
    debug_assert!(x > 0);
    x
}
"""


class TestPanicMacro(unittest.TestCase):
    def test_todo_macro_is_consider(self):
        findings = findings_of(_MACRO_TP)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "panic_macro")
        self.assertEqual(findings[0]["classification"], "CONSIDER")
        self.assertEqual(findings[0]["confidence"], "HIGH")

    def test_debug_assert_is_acceptable(self):
        findings = findings_of(_DEBUG_ASSERT)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "panic_macro")
        self.assertEqual(findings[0]["classification"], "ACCEPTABLE")


# --- slice_panic_method ----------------------------------------------------

_SLICE_TP = """\
use pyo3::prelude::*;

#[pyfunction]
fn take(mut v: Vec<i32>) -> i32 {
    v.swap_remove(0)
}
"""


class TestSlicePanicMethod(unittest.TestCase):
    def test_swap_remove_is_consider_low(self):
        findings = findings_of(_SLICE_TP)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "slice_panic_method")
        self.assertEqual(findings[0]["classification"], "CONSIDER")
        self.assertEqual(findings[0]["confidence"], "LOW")


# --- extern "C" escalation -------------------------------------------------

_EXTERN_C_PANIC = """\
extern "C" fn handler() {
    panic!("unrecoverable");
}
"""


class TestExternCEscalation(unittest.TestCase):
    def test_panic_in_extern_c_is_fix(self):
        findings = findings_of(_EXTERN_C_PANIC)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "panic_macro")
        self.assertEqual(findings[0]["classification"], "FIX")
        self.assertEqual(findings[0]["details"]["reached_via"], "extern_c")


# --- one-hop reachability --------------------------------------------------

_ONE_HOP = """\
use pyo3::prelude::*;

#[pyfunction]
fn entry() -> i32 {
    helper()
}

fn helper() -> i32 {
    let o: Option<i32> = None;
    o.unwrap()
}
"""


class TestOneHop(unittest.TestCase):
    def test_panic_in_one_hop_callee_is_flagged(self):
        findings = findings_of(_ONE_HOP)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["type"], "panic_unwrap")
        self.assertEqual(f["function"], "helper")
        self.assertEqual(f["details"]["reached_via"], "one_hop")
        self.assertEqual(f["details"]["via_function"], "entry")


# --- negatives and edges ---------------------------------------------------

_UNREACHABLE_HELPER = """\
fn internal_helper() -> i32 {
    let o: Option<i32> = None;
    o.unwrap()
}
"""

_PYMETHODS_UNWRAP = """\
use pyo3::prelude::*;

#[pyclass]
struct Stack {
    items: Vec<i32>,
}

#[pymethods]
impl Stack {
    fn top(&self) -> i32 {
        *self.items.last().unwrap()
    }
}
"""

_SUPPRESSED = """\
use pyo3::prelude::*;

#[pyfunction]
fn known(v: Vec<i32>) -> i32 {
    // rust-ext-safe: v is guaranteed non-empty by the caller contract.
    *v.first().unwrap()
}
"""


class TestNegativesAndEdges(unittest.TestCase):
    def test_unreachable_helper_not_scanned(self):
        # A helper reached from no PyO3 callable and not extern "C".
        self.assertEqual(findings_of(_UNREACHABLE_HELPER), [])

    def test_minimal_crate_is_clean(self):
        self.assertEqual(findings_of(MINIMAL_PYO3_CRATE), [])

    def test_unwrap_inside_pymethods_is_flagged(self):
        findings = findings_of(_PYMETHODS_UNWRAP)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "panic_unwrap")
        self.assertEqual(findings[0]["function"], "top")

    def test_suppression_comment_downgrades_to_acceptable(self):
        findings = findings_of(_SUPPRESSED)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "ACCEPTABLE")

    def test_bugs_fixture_has_no_panic_findings(self):
        # Every `.unwrap()` in PYO3_CRATE_WITH_BUGS is on a PyResult, which
        # is pyresult-propagation-checker's territory, not panic-safety's.
        self.assertEqual(findings_of(PYO3_CRATE_WITH_BUGS), [])


if __name__ == "__main__":
    unittest.main()
