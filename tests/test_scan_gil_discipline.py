"""Tests for scan_gil_discipline — GIL discipline audit."""

import unittest

from fixtures import MINIMAL_PYO3_CRATE, PYO3_CRATE_WITH_BUGS
from helpers import TempRustExtension, import_script

sub = import_script("scan_gil_discipline")


def run(code: str, **kw) -> dict:
    with TempRustExtension({"src/lib.rs": code}, **kw) as ext:
        return sub.analyze(str(ext.root))


def findings_of(code: str, **kw) -> list[dict]:
    return run(code, **kw)["findings"]


def types_of(code: str, **kw) -> set[str]:
    return {f["type"] for f in findings_of(code, **kw)}


# --- Patterns 1-3: Python work inside a detached closure -------------------

_BIND_IN_DETACH = """\
use pyo3::prelude::*;

fn dangerous<'py>(py: Python<'py>, obj: Py<PyAny>) {
    py.detach(|| {
        let _b = obj.bind(py);
    });
}
"""

# Legacy-vocabulary twin -- pre-0.25 crates use `allow_threads`.
_BIND_IN_ALLOW_THREADS = _BIND_IN_DETACH.replace(".detach(", ".allow_threads(")

_FFI_IN_DETACH = """\
use pyo3::prelude::*;
use pyo3::ffi;

fn worker(py: Python<'_>, obj: *mut ffi::PyObject) {
    py.detach(|| {
        unsafe { ffi::Py_IncRef(obj); }
    });
}
"""

_DETACH_CLEAN = """\
use pyo3::prelude::*;

fn worker(py: Python<'_>, data: Vec<i32>) -> i32 {
    py.detach(|| data.iter().sum())
}
"""

_DETACH_WITH_REATTACH = """\
use pyo3::prelude::*;

fn worker(py: Python<'_>, obj: Py<PyAny>) {
    py.detach(|| {
        Python::attach(|inner| {
            let _b = obj.bind(inner);
        });
    });
}
"""


class TestDetachClosureViolations(unittest.TestCase):
    def test_bind_in_detach_is_fix(self):
        findings = findings_of(_BIND_IN_DETACH)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "gil_violation_in_detach")
        self.assertEqual(findings[0]["classification"], "FIX")
        self.assertEqual(findings[0]["details"]["violations"][0]["kind"], "bind_call")

    def test_legacy_allow_threads_vocabulary_is_caught(self):
        findings = findings_of(_BIND_IN_ALLOW_THREADS)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "gil_violation_in_detach")
        self.assertEqual(findings[0]["details"]["closure_callee"], "allow_threads")

    def test_ffi_call_in_detach_is_fix(self):
        findings = findings_of(_FFI_IN_DETACH)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["details"]["violations"][0]["kind"], "ffi_call")

    def test_clean_detach_closure_not_flagged(self):
        self.assertEqual(findings_of(_DETACH_CLEAN), [])

    def test_reattach_inside_detach_is_safe(self):
        # A nested `Python::attach` re-acquires the GIL: not a violation.
        self.assertEqual(findings_of(_DETACH_WITH_REATTACH), [])


# --- Pattern 4: nested attach ----------------------------------------------

_NESTED_ATTACH = """\
use pyo3::prelude::*;

fn process(py: Python<'_>) {
    Python::attach(|inner| {
        let _ = inner;
    });
}
"""

_TOP_LEVEL_ATTACH = """\
use pyo3::prelude::*;

fn entry() {
    Python::attach(|py| {
        let _ = py;
    });
}
"""


class TestNestedAttach(unittest.TestCase):
    def test_nested_attach_is_consider(self):
        findings = findings_of(_NESTED_ATTACH)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "nested_gil_attach")
        self.assertEqual(findings[0]["classification"], "CONSIDER")

    def test_top_level_attach_not_flagged(self):
        self.assertNotIn("nested_gil_attach", types_of(_TOP_LEVEL_ATTACH))


# --- Pattern 5: extern "C" without attach ----------------------------------

_EXTERN_C_NO_ATTACH = """\
use pyo3::prelude::*;

extern "C" fn on_event(state: *mut pyo3::ffi::PyObject) {
    let any: Py<PyAny> = unsafe { Py::from_borrowed_ptr(state) };
    let _ = any;
}
"""

_EXTERN_C_WITH_ATTACH = """\
use pyo3::prelude::*;

extern "C" fn on_event() {
    Python::attach(|py| {
        let _ = py.None();
    });
}
"""

_EXTERN_C_PURE = """\
extern "C" fn doubler(x: i32) -> i32 {
    x * 2
}
"""


class TestExternC(unittest.TestCase):
    def test_extern_c_using_pyo3_without_attach_is_fix(self):
        findings = findings_of(_EXTERN_C_NO_ATTACH)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "extern_c_pyo3_no_attach")
        self.assertEqual(findings[0]["classification"], "FIX")

    def test_extern_c_that_attaches_not_flagged(self):
        self.assertNotIn("extern_c_pyo3_no_attach", types_of(_EXTERN_C_WITH_ATTACH))

    def test_pure_c_callback_not_flagged(self):
        self.assertEqual(findings_of(_EXTERN_C_PURE), [])


# --- Pattern 6: unchecked attach -------------------------------------------

_UNCHECKED_ATTACH = """\
use pyo3::prelude::*;

fn risky() {
    unsafe { Python::attach_unchecked(|py| { let _ = py; }) };
}
"""


class TestUncheckedAttach(unittest.TestCase):
    def test_attach_unchecked_is_flagged(self):
        findings = findings_of(_UNCHECKED_ATTACH)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "unchecked_attach")
        self.assertEqual(findings[0]["classification"], "CONSIDER")


# --- Pattern 8: gil_used declaration (version-aware) -----------------------

_FT_CI = """\
jobs:
  test:
    strategy:
      matrix:
        python-version: ["3.13t"]
"""

_FT_DECLARED = """\
use pyo3::prelude::*;

#[pymodule]
fn ft(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.gil_used(false)?;
    Ok(())
}
"""


class TestGilUsedDeclaration(unittest.TestCase):
    def test_ft_target_without_gil_used_is_flagged(self):
        with TempRustExtension(
            {"src/lib.rs": MINIMAL_PYO3_CRATE, ".github/workflows/ci.yml": _FT_CI}
        ) as ext:
            findings = sub.analyze(str(ext.root))["findings"]
        gil_used = [f for f in findings if f["type"] == "gil_used_undeclared"]
        self.assertEqual(len(gil_used), 1)
        self.assertEqual(gil_used[0]["classification"], "CONSIDER")
        self.assertIn("0.28+", gil_used[0]["description"])

    def test_legacy_version_wording_differs(self):
        with TempRustExtension(
            {"src/lib.rs": MINIMAL_PYO3_CRATE, ".github/workflows/ci.yml": _FT_CI},
            pyo3_version="0.25",
        ) as ext:
            findings = sub.analyze(str(ext.root))["findings"]
        gil_used = [f for f in findings if f["type"] == "gil_used_undeclared"]
        self.assertEqual(len(gil_used), 1)
        self.assertIn("< 0.28", gil_used[0]["description"])

    def test_declared_gil_used_not_flagged(self):
        # `_FT_DECLARED` is free-threading-targeted (the `gil_used` source
        # signal) but declares it, so no finding.
        self.assertNotIn("gil_used_undeclared", types_of(_FT_DECLARED))

    def test_non_ft_crate_not_flagged(self):
        self.assertNotIn("gil_used_undeclared", types_of(MINIMAL_PYO3_CRATE))


# --- Negatives and the combined bug fixture --------------------------------


class TestNegativesAndBugs(unittest.TestCase):
    def test_minimal_crate_is_clean(self):
        self.assertEqual(findings_of(MINIMAL_PYO3_CRATE), [])

    def test_bugs_fixture_has_detach_violation(self):
        findings = findings_of(PYO3_CRATE_WITH_BUGS)
        detach = [f for f in findings if f["type"] == "gil_violation_in_detach"]
        self.assertEqual(len(detach), 1)
        self.assertEqual(detach[0]["classification"], "FIX")


if __name__ == "__main__":
    unittest.main()
