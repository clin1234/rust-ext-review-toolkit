"""Tests for scan_pyclass_traits — `#[pyclass]` trait-bound audit."""

import unittest

from fixtures import MINIMAL_PYO3_CRATE, PYO3_CRATE_WITH_PYCLASS, PYO3_FT_TARGET_CRATE
from helpers import TempRustExtension, import_script

sub = import_script("scan_pyclass_traits")

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


_NON_SEND = """\
use pyo3::prelude::*;
use std::rc::Rc;

#[pyclass]
struct Holder {
    data: Rc<Vec<u8>>,
}
"""

_UNSENDABLE = """\
use pyo3::prelude::*;
use std::rc::Rc;

#[pyclass(unsendable)]
struct LocalHolder {
    data: Rc<Vec<u8>>,
}
"""

_COVERED_BY_UNSAFE = """\
use pyo3::prelude::*;
use std::rc::Rc;

#[pyclass]
struct Forced {
    data: Rc<Vec<u8>>,
}

unsafe impl Send for Forced {}
unsafe impl Sync for Forced {}
"""

_EXTENDS_DROP = """\
use pyo3::prelude::*;

#[pyclass]
struct Base {}

#[pyclass(extends = Base)]
struct Child {
    value: i64,
}

impl Drop for Child {
    fn drop(&mut self) {}
}
"""


class TestNonSendField(unittest.TestCase):
    def test_rc_field_without_unsendable_is_fix(self):
        findings = findings_of(_NON_SEND)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["type"], "non_send_field")
        self.assertEqual(f["classification"], "FIX")
        self.assertEqual(f["details"]["offending_fields"][0]["field"], "data")

    def test_unsendable_optout_is_policy(self):
        findings = findings_of(_UNSENDABLE)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "unsendable_pyclass")
        self.assertEqual(findings[0]["classification"], "POLICY")

    def test_unsafe_impl_send_defers_to_unsafe_block_auditor(self):
        # An `unsafe impl Send/Sync` covers the type -> no non_send_field here.
        self.assertNotIn("non_send_field", types_of(_COVERED_BY_UNSAFE))


class TestExtendsWithDrop(unittest.TestCase):
    def test_extends_plus_drop_is_consider(self):
        findings = [
            f
            for f in findings_of(_EXTENDS_DROP)
            if f["type"] == "pyclass_extends_with_drop"
        ]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "CONSIDER")
        self.assertEqual(findings[0]["details"]["extends"], "Base")


class TestMissingFrozen(unittest.TestCase):
    def test_mutable_pyclass_on_ft_crate_is_flagged(self):
        with TempRustExtension(
            {"src/lib.rs": PYO3_FT_TARGET_CRATE, ".github/workflows/ci.yml": _FT_CI}
        ) as ext:
            findings = sub.analyze(str(ext.root))["findings"]
        frozen = [f for f in findings if f["type"] == "missing_frozen_ft"]
        self.assertEqual(len(frozen), 1)
        self.assertEqual(frozen[0]["details"]["struct"], "MutableCounter")
        self.assertEqual(frozen[0]["classification"], "CONSIDER")

    def test_no_frozen_finding_when_not_ft_targeted(self):
        self.assertNotIn("missing_frozen_ft", types_of(PYO3_FT_TARGET_CRATE))


class TestNegatives(unittest.TestCase):
    def test_no_pyclass_minimal_crate(self):
        self.assertEqual(findings_of(MINIMAL_PYO3_CRATE), [])

    def test_plain_pyclass_is_clean(self):
        self.assertEqual(findings_of(PYO3_CRATE_WITH_PYCLASS), [])


if __name__ == "__main__":
    unittest.main()
