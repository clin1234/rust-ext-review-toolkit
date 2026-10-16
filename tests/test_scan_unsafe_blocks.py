"""Tests for scan_unsafe_blocks — `unsafe`-block soundness audit."""

import unittest

from fixtures import (
    MINIMAL_PYO3_CRATE,
    PYO3_CRATE_WITH_BUGS,
    PYO3_CRATE_WITH_PYCLASS,
    PYO3_CRATE_WITH_UNSAFE,
)
from helpers import TempRustExtension, import_script

sub = import_script("scan_unsafe_blocks")


def run(code: str, **kw) -> dict:
    """Analyze a single-file crate and return the report."""
    with TempRustExtension({"src/lib.rs": code}, **kw) as ext:
        return sub.analyze(str(ext.root))


def findings_of(code: str, **kw) -> list[dict]:
    return run(code, **kw)["findings"]


# --- Test fixtures (recipes SS3.1) -----------------------------------------

_FFI_CALL = """\
use pyo3::prelude::*;
use pyo3::ffi;

#[pyfunction]
fn bump<'py>(py: Python<'py>, obj: &Bound<'py, PyAny>) {
    unsafe { ffi::Py_INCREF(obj.as_ptr()); }
}
"""

_TRANSMUTE = """\
use pyo3::prelude::*;

// v0.2: the explicit turbofish makes the handle types visible in the call
// text -- the scanner now requires this (see the v0.2 toolkit feedback
// from the cryptography review: a bare `mem::transmute(obj)` is no longer
// classified as `unsafe_handle_transmute` because it false-positived on
// lifetime relabels of pure-Rust types).
#[pyfunction]
fn bad(obj: Py<PyAny>) -> Py<PyAny> {
    unsafe { std::mem::transmute::<Py<PyAny>, Py<PyAny>>(obj) }
}
"""

_REFCOUNT_WRITE = """\
use pyo3::prelude::*;

#[pyfunction]
fn manip<'py>(obj: &Bound<'py, PyAny>) {
    unsafe { (*obj.as_ptr()).ob_refcnt += 1; }
}
"""

_PRIVATE_CAPI = """\
use pyo3::prelude::*;
use pyo3::ffi;

#[pyfunction]
fn sneaky<'py>(py: Python<'py>) {
    unsafe { ffi::_PyObject_GC_New(std::ptr::null_mut()); }
}
"""

_SEND_IMPL_BAD = """\
use std::rc::Rc;

struct Holder { inner: Rc<u8> }
unsafe impl Send for Holder {}
"""

_SEND_IMPL_CLEAN = """\
struct Plain { value: i64 }
unsafe impl Send for Plain {}
"""

_SEND_IMPL_DOCUMENTED = """\
struct Plain { value: i64 }
// SAFETY: Plain holds only an i64; it is trivially thread-safe.
unsafe impl Send for Plain {}
"""

_UNSAFE_FN = """\
unsafe fn raw_helper(p: *mut u8) -> u8 { *p }
"""

_NESTED = "fn a() { unsafe { unsafe { let _x = 1; } } }\n"


# --- True positives: one per pattern ---------------------------------------


class TestFfiCall(unittest.TestCase):
    def test_raw_ffi_call_is_consider(self):
        findings = findings_of(_FFI_CALL)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["type"], "unsafe_ffi_call")
        self.assertEqual(f["classification"], "CONSIDER")
        self.assertEqual(f["category"], "soundness")
        self.assertEqual(f["function"], "bump")
        self.assertTrue(f["details"]["ffi_calls"])
        self.assertTrue(f["details"]["python_in_scope"])


class TestHandleTransmute(unittest.TestCase):
    def test_transmute_of_handles_is_fix(self):
        findings = findings_of(_TRANSMUTE)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["type"], "unsafe_handle_transmute")
        self.assertEqual(f["classification"], "FIX")
        self.assertTrue(f["details"]["transmutes"][0]["handle_types"])


class TestRefcountManipulation(unittest.TestCase):
    def test_ob_refcnt_write_is_fix(self):
        findings = findings_of(_REFCOUNT_WRITE)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["type"], "unsafe_refcount_manipulation")
        self.assertEqual(f["classification"], "FIX")
        self.assertTrue(f["details"]["refcount_manipulation"])


class TestPrivateCapi(unittest.TestCase):
    def test_private_symbol_fix_under_abi3t(self):
        for features in (["abi3t"], ["abi3t-py315"], ["abi3", "abi3t"]):
            with self.subTest(features=features):
                findings = findings_of(_PRIVATE_CAPI, pyo3_features=features)
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0]["type"], "unsafe_private_capi")
                self.assertEqual(findings[0]["classification"], "FIX")

    def test_private_symbol_consider_without_abi3(self):
        findings = findings_of(_PRIVATE_CAPI)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "unsafe_private_capi")
        self.assertEqual(findings[0]["classification"], "CONSIDER")

    def test_private_symbol_fix_under_abi3(self):
        findings = findings_of(
            _PRIVATE_CAPI, pyo3_features=["extension-module", "abi3-py310"]
        )
        self.assertEqual(findings[0]["type"], "unsafe_private_capi")
        self.assertEqual(findings[0]["classification"], "FIX")
        self.assertTrue(findings[0]["details"]["private_capi_calls"])


class TestUnsafeSendImpl(unittest.TestCase):
    def test_send_impl_with_rc_field_is_fix(self):
        findings = findings_of(_SEND_IMPL_BAD)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["type"], "unsafe_send_sync_impl")
        self.assertEqual(f["classification"], "FIX")
        self.assertEqual(f["category"], "thread_safety")
        self.assertEqual(f["details"]["offending_fields"][0]["field"], "inner")

    def test_clean_send_impl_is_consider(self):
        findings = findings_of(_SEND_IMPL_CLEAN)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "CONSIDER")

    def test_documented_send_impl_is_acceptable(self):
        findings = findings_of(_SEND_IMPL_DOCUMENTED)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "ACCEPTABLE")


# --- True negatives --------------------------------------------------------


class TestNegatives(unittest.TestCase):
    def test_no_unsafe_minimal_crate(self):
        self.assertEqual(findings_of(MINIMAL_PYO3_CRATE), [])

    def test_no_unsafe_pyclass_crate(self):
        self.assertEqual(findings_of(PYO3_CRATE_WITH_PYCLASS), [])

    def test_safety_comment_downgrades_to_acceptable(self):
        findings = findings_of(PYO3_CRATE_WITH_UNSAFE)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "ACCEPTABLE")
        self.assertTrue(findings[0]["details"]["safety_comment"])


# --- Edge cases ------------------------------------------------------------


class TestEdgeCases(unittest.TestCase):
    def test_unsafe_fn_reported_as_kind_fn(self):
        findings = findings_of(_UNSAFE_FN)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["details"]["kind"], "fn")
        self.assertEqual(findings[0]["classification"], "CONSIDER")

    def test_nested_unsafe_blocks_still_flagged(self):
        findings = findings_of(_NESTED)
        self.assertGreaterEqual(len(findings), 1)
        self.assertTrue(all(f["category"] == "soundness" for f in findings))

    def test_bugs_fixture_has_handle_transmute(self):
        findings = findings_of(PYO3_CRATE_WITH_BUGS)
        transmutes = [f for f in findings if f["type"] == "unsafe_handle_transmute"]
        self.assertEqual(len(transmutes), 1)
        self.assertEqual(transmutes[0]["classification"], "FIX")

    def test_report_envelope_is_well_formed(self):
        report = run(_FFI_CALL)
        self.assertIn("crate_info", report)
        self.assertIn("summary", report)
        self.assertEqual(report["summary"]["by_classification"], {"CONSIDER": 1})
        self.assertGreaterEqual(report["functions_analyzed"], 1)


# v0.2 regression: a BARE `mem::transmute(val)` (no turbofish, no handle
# marker in args) inside a fn whose signature *happens* to contain a PyO3
# handle type is NOT a handle transmute -- it is typically a pure-Rust
# lifetime relabel. v0.1 false-positived on cryptography's
# `pkcs7.rs::try_list_of_certificates` for exactly this shape; v0.2 only
# escalates to `unsafe_handle_transmute` when the CALL itself references
# handle types.

_BARE_TRANSMUTE_LIFETIME_RELABEL = """\
use pyo3::prelude::*;
use pyo3::types::PyBytes;

struct OwnedCertificate<'a>(&'a [u8]);

#[pyfunction]
fn try_list<'py>(py: Python<'py>, data: Py<PyBytes>) -> PyResult<()> {
    let val: &[u8] = &[1, 2, 3];
    // SAFETY: pure-Rust lifetime relabel; no PyO3 handle is transmuted.
    let _relabeled: OwnedCertificate<'static> = unsafe {
        std::mem::transmute(OwnedCertificate(val))
    };
    Ok(())
}
"""


class TestBareTransmuteFPRegression(unittest.TestCase):
    def test_bare_transmute_with_handle_in_enclosing_fn_not_handle_transmute(self):
        # The fn signature has `Py<PyBytes>`, but the transmute call text
        # contains no handle marker. v0.1 false-positived this as
        # `unsafe_handle_transmute`; v0.2 must classify it as a plain
        # `unsafe_block` (and the SAFETY comment downgrades to ACCEPTABLE).
        findings = findings_of(_BARE_TRANSMUTE_LIFETIME_RELABEL)
        # No handle_transmute classification:
        self.assertNotIn(
            "unsafe_handle_transmute",
            {f["type"] for f in findings},
            f"bare lifetime-relabel transmute misclassified: {findings}",
        )
        # The unsafe block is still observed (just not escalated):
        self.assertTrue(
            any(f["type"] == "unsafe_block" for f in findings),
            "the unsafe block itself should still be reported",
        )


if __name__ == "__main__":
    unittest.main()
