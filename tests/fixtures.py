"""Canonical PyO3 crate fixtures for rust-ext-review-toolkit tests.

These are Rust source strings — they need to *parse*, not necessarily
*compile*. See IMPLEMENTATION_ROADMAP.md §6.
"""

MINIMAL_PYO3_CRATE = """\
use pyo3::prelude::*;

#[pyfunction]
fn add(a: i64, b: i64) -> i64 {
    a + b
}

#[pymodule]
fn minimal(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(add, m)?)?;
    Ok(())
}
"""

PYO3_CRATE_WITH_PYCLASS = """\
use pyo3::prelude::*;

#[pyclass]
struct Counter {
    value: i64,
}

#[pymethods]
impl Counter {
    #[new]
    fn new() -> Self {
        Self { value: 0 }
    }

    fn increment(&mut self) {
        self.value += 1;
    }

    #[getter]
    fn value(&self) -> i64 {
        self.value
    }
}

#[pymodule]
fn classy(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Counter>()?;
    Ok(())
}
"""

PYO3_CRATE_WITH_UNSAFE = """\
use pyo3::prelude::*;
use pyo3::ffi;

#[pyfunction]
fn refcount_of<'py>(obj: &Bound<'py, PyAny>) -> isize {
    // SAFETY: obj is a valid borrowed reference; reading ob_refcnt is a
    // simple field read on a non-null pointer.
    unsafe { (*obj.as_ptr()).ob_refcnt }
}

#[pymodule]
fn safe_unsafe(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(refcount_of, m)?)?;
    Ok(())
}
"""

# Every scanner-relevant bug, one per #[pyfunction]/item. Bug numbers map to
# the agent that should catch them (IMPLEMENTATION_ROADMAP.md §6.4).
PYO3_CRATE_WITH_BUGS = """\
use pyo3::prelude::*;
use pyo3::ffi;
use std::rc::Rc;

// Bug 1 (pyclass-traits-checker): Rc field without `unsendable`
#[pyclass]
struct BadShared {
    data: Rc<Vec<u8>>,
}

// Bug 2 (pyresult-propagation-checker): .unwrap() on PyResult
// Bug 3 (panic-safety-checker): same .unwrap()
// Bug 4 (gil-discipline-checker): Py<T>::bind inside a detach closure
#[pyfunction]
fn bad_function<'py>(py: Python<'py>, obj: Py<PyAny>) -> PyObject {
    py.detach(|| {
        // Bug 4: bind needs an attached interpreter; detach released it.
        let _bound = obj.bind(py);
    });

    // Bug 2 & 3: .unwrap() on a PyResult inside a #[pyfunction]
    let imported = py.import("json").unwrap();
    imported.getattr("dumps").unwrap().into()
}

// Bug 5 (unsafe-block-auditor): transmute between handle types
#[pyfunction]
fn bad_transmute(obj: Py<PyAny>) -> Py<PyAny> {
    unsafe { std::mem::transmute(obj) }
}

// Bug 6 (lifetime-handle-checker): legacy &PyAny
#[pyfunction]
fn old_style_param(d: &PyDict) -> PyResult<()> {
    Ok(())
}

// Bug 7 (module-init-checker): static OnceLock<Py<...>> global
static GLOBAL: std::sync::OnceLock<Py<PyAny>> = std::sync::OnceLock::new();

#[pymodule]
fn buggy(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Bug 8 (module-init-checker): add_class result not checked (no `?`)
    m.add_class::<BadShared>();
    m.add_function(wrap_pyfunction!(bad_function, m)?)?;
    m.add_function(wrap_pyfunction!(bad_transmute, m)?)?;
    m.add_function(wrap_pyfunction!(old_style_param, m)?)?;
    Ok(())
}
"""

PYO3_FT_TARGET_CRATE = """\
use pyo3::prelude::*;

// Mutable field, no `frozen` — flagged on a free-threading-target crate.
#[pyclass]
struct MutableCounter {
    value: i64,
}

#[pymethods]
impl MutableCounter {
    #[new]
    fn new() -> Self { Self { value: 0 } }
    fn increment(&mut self) { self.value += 1; }
}

// Free-threading-friendly counterpart.
#[pyclass(frozen)]
struct FrozenCounter {
    value: std::sync::atomic::AtomicI64,
}

#[pymethods]
impl FrozenCounter {
    #[new]
    fn new() -> Self {
        Self { value: std::sync::atomic::AtomicI64::new(0) }
    }
    fn increment(&self) {
        self.value.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    }
}

#[pymodule]
fn ft_target(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<MutableCounter>()?;
    m.add_class::<FrozenCounter>()?;
    Ok(())
}
"""
