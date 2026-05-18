"""Tests for scan_pyclass_protocol — `#[pymethods]` protocol audit."""

import unittest

from fixtures import MINIMAL_PYO3_CRATE, PYO3_CRATE_WITH_PYCLASS
from helpers import TempRustExtension, import_script

sub = import_script("scan_pyclass_protocol")


def run(code: str, **kw) -> dict:
    with TempRustExtension({"src/lib.rs": code}, **kw) as ext:
        return sub.analyze(str(ext.root))


def findings_of(code: str, **kw) -> list[dict]:
    return run(code, **kw)["findings"]


def types_of(code: str, **kw) -> set[str]:
    return {f["type"] for f in findings_of(code, **kw)}


_MISSING_TRAVERSE = """\
use pyo3::prelude::*;

#[pyclass]
struct Node {
    child: Py<PyAny>,
}

#[pymethods]
impl Node {
    #[new]
    fn new(child: Py<PyAny>) -> Self {
        Self { child }
    }
}
"""

_HAS_TRAVERSE = """\
use pyo3::prelude::*;

#[pyclass]
struct Node {
    child: Py<PyAny>,
}

#[pymethods]
impl Node {
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.child)
    }
    fn __clear__(&mut self) {}
}
"""

_GETTER_ARGS = """\
use pyo3::prelude::*;

#[pyclass]
struct Config {
    value: i64,
}

#[pymethods]
impl Config {
    #[getter]
    fn scaled(&self, factor: i64) -> i64 {
        self.value * factor
    }
}
"""

_RICHCMP_INCOMPLETE = """\
use pyo3::prelude::*;
use pyo3::basic::CompareOp;

#[pyclass]
struct Version {
    num: i64,
}

#[pymethods]
impl Version {
    fn __richcmp__(&self, other: &Self, op: CompareOp) -> PyResult<bool> {
        match op {
            CompareOp::Eq => Ok(self.num == other.num),
            CompareOp::Ne => Ok(self.num != other.num),
        }
    }
}
"""

_RICHCMP_OK = """\
use pyo3::prelude::*;
use pyo3::basic::CompareOp;

#[pyclass]
struct Version {
    num: i64,
}

#[pymethods]
impl Version {
    fn __richcmp__(&self, other: &Self, op: CompareOp) -> bool {
        op.matches(self.num.cmp(&other.num))
    }
}
"""

_HASH_BAD = """\
use pyo3::prelude::*;

#[pyclass]
struct Key {
    id: i64,
}

#[pymethods]
impl Key {
    fn __hash__(&self) -> i32 {
        self.id as i32
    }
}
"""

_NEW_INIT = """\
use pyo3::prelude::*;

#[pyclass]
struct Widget {
    value: i64,
}

#[pymethods]
impl Widget {
    #[new]
    fn new() -> Self {
        Self { value: 0 }
    }
    fn __init__(&self) {}
}
"""


class TestTraverseClear(unittest.TestCase):
    def test_py_field_without_gc_slots_is_consider(self):
        # CONSIDER, not FIX: the scanner cannot prove a cycle is possible --
        # that is the agent's escalation call.
        types = types_of(_MISSING_TRAVERSE)
        self.assertIn("missing_traverse", types)
        self.assertIn("missing_clear", types)
        traverse = [
            f for f in findings_of(_MISSING_TRAVERSE) if f["type"] == "missing_traverse"
        ]
        self.assertEqual(traverse[0]["classification"], "CONSIDER")

    def test_py_field_with_gc_slots_is_clean(self):
        self.assertEqual(findings_of(_HAS_TRAVERSE), [])


class TestGetterWithArgs(unittest.TestCase):
    def test_getter_taking_an_argument_is_fix(self):
        findings = findings_of(_GETTER_ARGS)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "getter_with_args")
        self.assertEqual(findings[0]["classification"], "FIX")


class TestRichcmp(unittest.TestCase):
    def test_incomplete_compareop_match_is_consider(self):
        findings = findings_of(_RICHCMP_INCOMPLETE)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "richcmp_incomplete")
        self.assertEqual(findings[0]["classification"], "CONSIDER")
        self.assertEqual(
            sorted(findings[0]["details"]["missing_ops"]), ["Ge", "Gt", "Le", "Lt"]
        )

    def test_matches_helper_is_not_flagged(self):
        self.assertNotIn("richcmp_incomplete", types_of(_RICHCMP_OK))


class TestHashReturn(unittest.TestCase):
    def test_hash_returning_i32_is_consider(self):
        findings = findings_of(_HASH_BAD)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "hash_return_type")
        self.assertEqual(findings[0]["classification"], "CONSIDER")


class TestNewInitCollision(unittest.TestCase):
    def test_new_and_init_together_is_consider(self):
        findings = findings_of(_NEW_INIT)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "new_init_collision")
        self.assertEqual(findings[0]["classification"], "CONSIDER")


class TestNegatives(unittest.TestCase):
    def test_no_pyclass_minimal_crate(self):
        self.assertEqual(findings_of(MINIMAL_PYO3_CRATE), [])

    def test_plain_pyclass_is_clean(self):
        self.assertEqual(findings_of(PYO3_CRATE_WITH_PYCLASS), [])


if __name__ == "__main__":
    unittest.main()
