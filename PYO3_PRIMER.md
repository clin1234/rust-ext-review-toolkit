# PyO3 Primer for the rust-ext-review-toolkit Implementer

This is the conceptual brief for the chat building this toolkit. Read it after
`rust-ext-review-toolkit-design.md` and before opening `IMPLEMENTATION_ROADMAP.md`.

Goal: by the end of this document you should be able to recognize the patterns
each scanner needs to detect, and you should know which Rust/PyO3 features are
relevant vs noise.

If you already know PyO3 well, skim §6 (version chronology) and §9 (concept
translation from cext-review-toolkit) and skip the rest.

---

## 1. PyO3 in five minutes

PyO3 is a Rust crate that lets Rust code expose itself to Python and call into
Python. It does two related things:

1. **Generate `PyInit_<modname>`** — a `cdylib` exposing the symbol the CPython
   loader looks for, so `import mymod` works.
2. **Translate between Rust types and Python objects** — `PyDict`, `PyList`,
   `PyAny`, generic `T: IntoPyObject`, etc.

The user-facing surface is *attribute-driven*. The four attributes you need to
recognize on sight:

- `#[pyfunction]` — Rust function exposed to Python
- `#[pyclass]` — Rust struct/enum exposed as a Python class
- `#[pymethods]` — `impl` block whose contents become methods/dunder slots on
  the `#[pyclass]` it targets
- `#[pymodule]` — entry-point function that registers the above with the
  generated `PyInit_*`

Everything else (lifetimes, handle types, the GIL system, the FFI surface,
free-threading, abi3) is mechanism in service of those four.

### What PyO3 already prevents

Most C-extension bug classes are gone:

- **Manual `Py_INCREF`/`Py_DECREF`** — `Py<T>` and `Bound<'py, T>` implement
  `Drop`, so refcount goes down automatically when the handle leaves scope.
- **Most NULL-pointer derefs** — fallible APIs return `PyResult<T>` or
  `Option<T>`.
- **Most use-after-free on Python objects** — the `'py` lifetime on
  `Bound<'py, T>` is tied to a `Python<'py>` GIL token; you cannot outlive the
  GIL in safe code.
- **General memory safety** — the borrow checker.

### What PyO3 does *not* prevent

This is the toolkit's reason to exist:

1. Soundness bugs inside `unsafe` (raw `pyo3-ffi`, transmutes, manual buffer
   protocol, FFI to C libraries)
2. `#[pyclass]` trait-bound violations
3. Holding `Py<T>` across `Python::detach` (legacy `allow_threads`) and using
   it incorrectly on the other side
4. Panic crossing the `extern "C"` boundary
5. `PyResult` propagation gaps (`.unwrap()`, `.ok()`, `let _ =`, missing `?`)
6. Wrong handle kind for the use case
7. `#[pymethods]` slot signature mistakes (`__traverse__`/`__clear__`,
   `__richcmp__`, `#[new]` vs `__init__`)
8. PyO3 version migration debt (legacy `&PyAny` etc.)
9. Free-threaded readiness (`gil_used` declaration, `frozen`)
10. `extract::<T>()` / `cast::<T>()` (pre-0.27 `downcast`) misuse

See design doc §1.2 for the canonical version of this list. The agent inventory
in §3 of the design doc is one-to-one with these.

---

## 2. The handle triad: `Bound<'py, T>` vs `Py<T>` vs `Borrowed<'a, 'py, T>`

This is the most important conceptual model in modern PyO3. Almost every
scanner needs to recognize these three handle kinds.

### `Bound<'py, T>`

The everyday handle for Python objects in current PyO3 (0.21+). Carries:

- A strong reference (owns one refcount)
- A `'py` lifetime tied to a `Python<'py>` GIL token — proves the GIL is held

```rust
fn use_dict<'py>(py: Python<'py>, d: &Bound<'py, PyDict>) -> PyResult<()> {
    let key = PyString::new(py, "x");
    let val: i64 = d.get_item(&key)?.extract()?;
    Ok(())
}
```

Use `Bound<'py, T>` for parameters and return types in `#[pyfunction]` /
`#[pymethods]` whenever the caller already has a GIL token. This is the default.

### `Py<T>`

A reference to a Python object that is *not* tied to a `'py` lifetime. You can
hold it across `detach` (legacy `allow_threads`), store it in a struct field,
store it in a `static`. To use it as Python, you must `bind` it back:

```rust
let dict_handle: Py<PyDict> = obj.into();
py.detach(|| {
    // dict_handle is here but you cannot call .get_item() — interpreter detached
    long_running_computation();
});
let bound = dict_handle.bind(py);  // re-acquire GIL view
bound.get_item("x")?;
```

Use `Py<T>` when you need to store a Python handle past the current GIL token's
lifetime: `#[pyclass]` fields, module-level `OnceLock`s, anything that survives
a thread context switch.

### `Borrowed<'a, 'py, T>`

A non-owning, GIL-bound view — `&'a Bound<'py, T>` minus the dereference layer.
Rare in user code. The toolkit usually only sees it in PyO3-internal APIs
returning iterator items.

You almost never write `Borrowed` yourself. When the toolkit detects user code
typing `Borrowed`, that's a candidate finding for lifetime-handle-checker:
either it's correct and should be commented, or the user meant
`&Bound<'py, T>`.

### Pre-0.21 legacy

Before 0.21, the everyday handle was `&PyAny`, `&PyDict`, `&PyList`, etc. — a
direct reference rather than a `Bound`. PyO3 0.21 introduced `Bound`; the
GIL-ref style was deprecated then removed in a later release (kept
transitionally behind the `gil-refs` feature). Crates partway through migration
mix the two; lifetime-handle-checker exists to surface that mix.

**Translation table:**

| Legacy | Current | Notes |
|--------|---------|-------|
| `&PyAny`, `&PyDict`, ... | `&Bound<'py, PyAny>`, ... | GIL-ref types; deprecated 0.21, removed in a recent release |
| `Py<T>::as_ref(py)` | `Py<T>::bind(py)` | `as_ref` removed in 0.26 |
| `Py<T>::clone()` | `Py<T>::clone_ref(py)` | bare `Clone` gated behind the `py-clone` feature since 0.22 |
| `PyString::new_bound(py, ...)` | `PyString::new(py, ...)` | the `_bound` suffix was dropped in 0.23 — un-suffixed names are the `Bound` versions |
| `.downcast::<T>()` | `.cast::<T>()` | renamed in 0.27 (`DowncastError` → `CastError`) |
| `Python::with_gil` / `allow_threads` | `Python::attach` / `detach` | renamed in 0.25 |
| `GILOnceCell` | `PyOnceLock` | renamed in 0.25 |
| `GILProtected` | `std::sync::Mutex` + `MutexExt` | `GILProtected` deprecated in 0.25 |

### Mental model for scanners

When you parse a function signature with `tree-sitter-rust`:

- `&PyAny` / `&PyDict` / `&PyList` etc. → **legacy reference** → flag for
  `lifetime-handle-checker` if PyO3 version ≥ 0.21
- `Bound<'py, T>` or `&Bound<'py, T>` → **current handle**, normal
- `Py<T>` → **GIL-detached handle**; verify the body either binds it (`.bind(py)`)
  or stores it; flag if it's an immediate-use parameter
- `Borrowed<'a, 'py, T>` → **rarely-needed handle**; flag for review

---

## 3. The GIL model

PyO3 represents interpreter access with the `Python<'py>` token. A function that
takes `py: Python<'py>` is statically guaranteed to have the interpreter
attached — the borrow-checker enforces this because every `Bound<'py, T>` shares
the lifetime.

**Terminology — renamed in PyO3 0.25.** Because "GIL" is a misnomer under
free-threading, PyO3 0.25 renamed the GIL API:

| Pre-0.25 (legacy) | 0.25+ (current) | What it does |
|---|---|---|
| `Python::with_gil` | `Python::attach` | Attach the interpreter, run a closure |
| `py.allow_threads` | `py.detach` | Detach the interpreter for the closure |
| `Python::with_gil_unchecked` | `Python::attach_unchecked` | `attach` without the state check (`unsafe`) |

The toolkit audits crates across versions, so every scanner recognizes **both**
vocabularies. This primer uses the current names with the legacy name noted.

### `Python::attach` (legacy `with_gil`)

Attach the interpreter from a context that doesn't have it (a foreign C
callback, a background thread, a non-PyO3 entry point):

```rust
Python::attach(|py| {
    let s = PyString::new(py, "hello");
    // use the interpreter...
});
```

Gotchas:

- **Nesting** is almost always wrong. Inside an `attach` you already have a
  `py: Python<'py>` — pass it down, don't attach again.
- **Finalization** panics. If `attach` runs after the interpreter has finalized
  (long-lived callbacks, signal handlers, foreign-runtime threads), the program
  aborts. Guard with an explicit "is the interpreter still alive?" check before
  attaching. `attach_unchecked` is **not** such a guard — it is the `unsafe`
  variant that *skips* the interpreter-state check, so it is more dangerous,
  not less.
- **`#[pyfunction]` and `#[pymethods]`** bodies *already have* a `py`
  parameter (often implicit via `&Bound<'py, T>` parameters) — never wrap them
  in `attach`.

### `py.detach` (legacy `allow_threads`)

Detach the interpreter (release the GIL) while running a closure:

```rust
fn parse_large_blob<'py>(py: Python<'py>, data: &Bound<'py, PyBytes>) -> PyResult<...> {
    let bytes = data.as_bytes();
    let parsed = py.detach(|| {
        // Interpreter detached here. Do CPU- or I/O-bound work.
        do_expensive_parse(bytes)
    });
    // Re-attached here.
    Ok(parsed.into())
}
```

The closure body cannot use any `Bound<'py, T>` or `Python<'py>` token — the
borrow checker enforces it. Inside `unsafe`, that protection is gone, and *that*
is what gil-discipline-checker is hunting for: any pyo3-ffi call, `Py::bind`,
or use of a previously-captured `Bound` inside a `detach` (legacy
`allow_threads`) closure.

### Free-threaded Python (PEP 703, `python3.13t` / `python3.14t`)

Free-threaded Python removes the GIL. PyO3 still uses the `Python<'py>` token,
but its meaning is "interpreter attached," not "GIL held" — the mutual exclusion
the GIL used to provide implicitly must now be provided explicitly by the
maintainer.

**Free-threading is not feature-gated.** There is no `gil-disabled` Cargo
feature (a common misconception — unlearn it). PyO3 detects a free-threaded
interpreter automatically at build time (the `Py_GIL_DISABLED` cfg). You target
free-threading by building against a free-threaded CPython.

What the scanners look for:

1. **The module `gil_used` declaration** (`#[pymodule(gil_used = ...)]` or
   `m.gil_used(...)?`). The *default* is version-dependent:
   - **PyO3 0.28+**: an undeclared module defaults to thread-safe
     (`gil_used = false`). A crate not yet audited for FT safety should *opt
     out* with `#[pymodule(gil_used = true)]`.
   - **PyO3 0.23–0.27**: the default was the opposite — a module had to *opt in*
     with `gil_used = false`.
   Always check the resolved PyO3 version before classifying.

2. **`abi3` is incompatible with free-threading.** The free-threaded build has a
   distinct ABI with no limited-API equivalent. If a crate sets `abi3`/`abi3-pyNM`
   *and* builds free-threaded, PyO3 warns and **ignores** the abi3 setting — so an
   `abi3` feature is not a free-threading signal.

3. **`#[pyclass]` thread-safety.** PyO3's default for `#[pyclass]` data is
   RefCell-style *runtime* borrow checking — conflicting `&mut self` access
   raises/panics rather than being a silent data race. For sound concurrent use:
   `#[pyclass(frozen)]` (immutable from Python, opts out of `&mut self`), or
   explicit interior locking (`Mutex`, `RwLock`, atomics).

When the project has FT signals (maintainer-stated intent, CI building
`3.13t`/`3.14t`) and the implementation doesn't match, that's a CONSIDER
finding. With *no* FT signals, FT gaps are POLICY — don't push someone toward FT
migration who hasn't asked.

---

## 4. The `unsafe` surface

In PyO3, every `unsafe` block is a soundness obligation the type system stopped
enforcing. The toolkit's #1 priority agent (`unsafe-block-auditor`) walks each
one. Kinds of `unsafe` you'll encounter:

### 4.1 Raw `pyo3-ffi` calls

`pyo3-ffi` is the low-level FFI to CPython's C API. Calling it requires:

- The GIL held (check that `Python<'py>` is in lexical scope)
- Valid, non-null pointers
- Correct ownership semantics (new/borrowed/stolen) — *this is exactly the
  domain of cext-review-toolkit's `scan_refcounts.py` analysis*

```rust
unsafe {
    let result = pyo3_ffi::PyDict_GetItem(d.as_ptr(), key.as_ptr());
    // result is a BORROWED reference. Bumping refcount is the caller's job
    // if they want to keep it past the dict's lifetime.
    if !result.is_null() {
        pyo3_ffi::Py_IncRef(result);
    }
}
```

Apply the borrowed-ref / new-ref / stolen-ref reasoning from cext-review-toolkit
inside `unsafe` blocks that call `pyo3-ffi::*`. The same `api_tables.json` data
applies. This is why `data/stable_abi.json` and `data/limited_api_headers.json`
are vendored from cext-review-toolkit per design doc §2.2 — the data is reused
verbatim.

### 4.2 Raw pointer derefs from `as_ptr()` / `as_mut_ptr()`

`Bound::as_ptr()` returns a `*mut ffi::PyObject`. Once you dereference, you're
working below PyO3's safety net:

```rust
unsafe {
    (*obj.as_ptr()).ob_refcnt += 1;  // bypasses PyO3's tracking — bug
}
```

Almost always wrong. The correct path is `Py::clone_ref(py)` or
`pyo3_ffi::Py_IncRef(obj.as_ptr())`.

### 4.3 `transmute` between handle types

Almost always wrong, even when the source compiles:

```rust
let bound: Bound<'py, MyClass> = unsafe {
    std::mem::transmute(py_handle)  // ← never correct
};
```

The right path is `Py::bind(py)` or `Py::into_bound(py)`.

### 4.4 Manual `__getbuffer__` / `__releasebuffer__`

The buffer protocol requires populating `Py_buffer` correctly:

```rust
#[pymethods]
impl MyArray {
    unsafe fn __getbuffer__(
        slf: PyRefMut<'_, Self>,
        view: *mut ffi::Py_buffer,
        flags: i32,
    ) -> PyResult<()> {
        // Must set: buf, len, itemsize, ndim, shape, strides,
        //          suboffsets, readonly, format.
        // Matching __releasebuffer__ must undo whatever this allocated.
        ...
    }
}
```

The agent inspects whether `obj`, `buf`, `len`, `itemsize`, `ndim`, `shape`,
`strides`, `suboffsets` are all set. Any unset field is a soundness bug —
NumPy and downstream consumers read those fields and segfault if they're
garbage.

### 4.5 `unsafe impl Send` / `unsafe impl Sync` on `#[pyclass]`

Bypasses the trait-bounds checker:

```rust
#[pyclass]
struct Tricky {
    inner: Rc<MyData>,
}

unsafe impl Send for Tricky {}  // ← BUG: Rc is genuinely not Send
```

Every occurrence is a flag for human review. The agent prints the field list
of the affected type so the reviewer can decide.

### 4.6 FFI to C libraries

`extern "C"` declarations + their callers. The toolkit flags these for review
because:

- Error reporting from C is by-convention (negative return code, `errno`,
  out-param) — easy to mishandle
- Resource ownership is by-contract — easy to leak / double-free
- Panic safety: if a Rust panic crosses an `extern "C"` boundary registered
  with a C library (not PyO3), the program aborts

### 4.7 Private `_Py*` symbols

`pyo3_ffi::_Py*` or `pyo3_ffi::_PyObject_*` calls. These are CPython internals,
not part of any stable surface. Treat them like cext-review-toolkit treats
private API uses: FIX when abi3 is targeted, CONSIDER otherwise.

---

## 5. The `#[pyclass]` machinery

`#[pyclass]` is a procedural macro. The user writes:

```rust
#[pyclass]
struct Counter {
    value: i64,
}

#[pymethods]
impl Counter {
    #[new]
    fn new() -> Self { Self { value: 0 } }

    fn increment(&mut self) { self.value += 1; }
}
```

The macro generates a lot of code — a Python `type` definition, slot tables,
method registration, GC integration, etc. **Tree-sitter does not expand this**.
You see only the source.

### 5.1 What the macro generates (and what we have to infer)

Things the macro emits that affect correctness but aren't visible at source
level:

- The `tp_traverse` / `tp_clear` GC slots (if the type holds `Py<T>` fields, the
  macro emits these; if not, it doesn't). Whether they exist correctly is
  driven by what fields the user declared. The scanner checks field types
  syntactically.
- The `Send` / `Sync` trait-bound assertions. Since PyO3 0.23, `#[pyclass]`
  requires `Send + Sync` (before 0.23, only `Send`); `#[pyclass(unsendable)]`
  opts out of both. If you declare a field of type `Rc<T>` (neither `Send` nor
  `Sync`) or `RefCell<T>` (`Send` but not `Sync`), the compile fails at the
  macro site, not at the field declaration.
- The `__init__` signature derived from `#[new]`.
- The "subclass" wiring when `#[pyclass(extends = Parent)]` is used.

Because the scanner can't see post-macro code, the strategy is *syntactic
inference*: look at the attribute, look at the struct fields, look at the
methods, and apply the rules the macro applies.

### 5.2 `#[pyclass]` options the scanners care about

```rust
#[pyclass(
    unsendable,           // skip Send bound; class is single-thread
    frozen,               // immutable from Python; required for FT-sharing
    subclass,             // allow Python subclasses
    extends = Parent,     // inherit from a #[pyclass] parent
    name = "MyClass",     // Python-visible name (defaults to Rust name)
    module = "mymod",     // canonical module location
    dict,                 // give instances a __dict__
    weakref,              // give instances a __weakref__
    eq, ord, hash,        // derive comparison + hash from #[derive]
)]
struct MyClass {
    field: Type,
}
```

Each option flips a scanner expectation:

- `unsendable` → field-type check skipped (`pyclass-traits-checker`)
- `frozen` → `&mut self` methods rejected at compile time; FT-shareable
- `dict`, `weakref` → type is GC-collectable; needs `__traverse__`/`__clear__`
  *if* it also holds `Py<T>` fields
- `extends` → `#[new]` must return `(Self, PyClassInitializer<Parent>)`

### 5.3 `#[pymethods]` slot conventions

Method attributes map to CPython slots:

| Attribute | CPython slot | Signature shape |
|---|---|---|
| `#[new]` | `tp_new` / `__new__` | `() -> Self` or `() -> PyResult<Self>` |
| `#[getter]` | `tp_getattro` (synthesized) | `(&self) -> PyResult<T>` |
| `#[setter]` | `tp_setattro` (synthesized) | `(&mut self, value: T) -> PyResult<()>` |
| `#[classmethod]` | `__classmethod__` | `(cls: &Bound<'_, PyType>, ...)` |
| `#[staticmethod]` | `__staticmethod__` | `(...)` |
| `__traverse__` | `tp_traverse` | `(&self, visit: PyVisit) -> Result<(), PyTraverseError>` |
| `__clear__` | `tp_clear` | `(&mut self)` |
| `__richcmp__` | `tp_richcompare` | `(&self, other, op: CompareOp) -> PyResult<PyObject>` |
| `__hash__` | `tp_hash` | `(&self) -> isize` or `u64` |
| `__len__` | `sq_length` | `(&self) -> usize` |

The `pyclass-protocol-checker` validates signature shape for each.

---

## 6. PyO3 version chronology

This is what `pyo3-version-compat-scanner` and `lifetime-handle-checker` operate
against. Memorize the inflection points. Current stable is **0.28** (0.29 is the
development line as of this writing).

| Version | Inflection points |
|---|---|
| **0.20** (late 2023) | Last pre-`Bound` release. `&PyAny`/`&PyDict` style; `PyString::new(py, ...)`; `obj.as_ref(py)`. `Py<T>: Clone` available. |
| **0.21** (early 2024) | `Bound<'py, T>` introduced as the canonical handle. `_bound`-suffixed constructors added (`PyString::new_bound`, ...). GIL-ref API deprecated; opt-in `gil-refs` feature silences the warnings. `PyCell` deprecated; `extract_bound` added. |
| **0.22** | `Py<T>: Clone` gated behind the opt-in `py-clone` feature (and panics if used without the GIL). `GILOnceCell` introduced; `#[pyo3(signature = …)]` required for `Option<T>` args. |
| **0.23** (late 2024) | Free-threading support; **`#[pyclass]` now requires `Send + Sync`**. `IntoPyObject` replaces `IntoPy`/`ToPyObject`. The `_bound` suffix becomes the *default* — un-suffixed names (`PyString::new`) now return `Bound`. |
| **0.24** (early 2025) | Stability release; continued GIL-ref deprecation. |
| **0.25** | **`Python::with_gil` → `Python::attach`; `allow_threads` → `detach`; `with_gil_unchecked` → `attach_unchecked`.** `GILOnceCell` → `PyOnceLock`; `GILProtected` → `std::sync::Mutex` + `MutexExt`. `prepare_freethreaded_python` → `Python::initialize`. |
| **0.26** | `Py::as_ref(py)` removed (use `.bind(py)`). |
| **0.27** | `FromPyObject` reworked (gains an `'a` lifetime; `extract` takes `Borrowed`). **`.downcast()` → `.cast()`** (`DowncastError` → `CastError`). |
| **0.28** (current) | **Free-threading default flipped**: modules attest thread-safe by default; opt out with `#[pymodule(gil_used = true)]`. `Py<T>` raw-pointer constructors deprecated. Multi-phase `#[pymodule]` init internally. |
| **0.29** (development) | Build-config refactor; some `PyErr` `From` conversions removed. |

### Mental cheat sheet

```
0.20:  &PyAny normal,  Py<T>: Clone works
0.21:  Bound introduced,  &PyAny deprecated,  *_bound constructors exist
0.22:  Py<T>: Clone behind the `py-clone` feature
0.23:  _bound suffix dropped (un-suffixed = Bound),  #[pyclass] needs Send + Sync
0.25:  with_gil -> attach,  allow_threads -> detach
0.26:  Py::as_ref GONE
0.27:  downcast -> cast
0.28:  free-threading is the default; opt OUT with gil_used = true
```

When the scanner sees a legacy GIL-ref type (`&PyAny` etc.), severity is:
- `0.20`: ACCEPTABLE
- `0.21-0.22`: CONSIDER (deprecated but compiles, possibly behind `gil-refs`)
- recent versions where it no longer compiles: FIX (useful for mid-migration audits)

The precise per-API introduced/deprecated/removed versions are data, not prose —
they live in `data/pyo3_api_tables.json`, which the scanners load.

---

## 7. Common idioms vs anti-patterns (cheat sheet)

### Idiomatic

```rust
// Convert a Bound<'py, Foo> into something callable from Python
let result = obj.call_method1("do_thing", (arg1, arg2))?;

// Store a Python object on a Rust struct
struct Config { callback: Py<PyAny> }

// Use the callback later
fn fire<'py>(&self, py: Python<'py>) -> PyResult<()> {
    self.callback.bind(py).call0()?;
    Ok(())
}

// Release the GIL for CPU work
let parsed = py.detach(|| do_cpu_work(&data));

// Idiomatic error propagation
fn parse_int<'py>(s: &Bound<'py, PyString>) -> PyResult<i64> {
    let raw: &str = s.to_str()?;
    raw.parse().map_err(|e| PyValueError::new_err(e.to_string()))
}
```

### Anti-patterns (each maps to a scanner)

```rust
// .unwrap() in #[pymethods] body  →  panic-safety-checker FIX
fn bad(&self, py: Python<'_>) -> PyObject {
    py.import("json").unwrap().getattr("dumps").unwrap().into()
}

// PyResult swallowed via .ok()  →  pyresult-propagation-checker FIX
let _ = obj.call_method0("close").ok();

// Holding Bound across detach (only reachable via unsafe)
// →  unsafe-block-auditor + gil-discipline-checker
let b: Bound<'py, PyDict> = ...;
py.detach(|| {
    unsafe {
        let _x = b.get_item("k");  // interpreter detached — soundness violation
    }
});

// Non-Send field without unsendable  →  pyclass-traits-checker FIX
#[pyclass]
struct Bad { data: std::rc::Rc<Vec<u8>> }

// Missing __traverse__/__clear__ on a class with Py<T> fields
// →  pyclass-protocol-checker FIX (subtle: depends on `dict`/`weakref`)
#[pyclass(dict)]
struct Node { neighbors: Vec<Py<Node>> }
// (no #[pymethods] traverse/clear)

// Legacy &PyAny in recent PyO3  →  lifetime-handle-checker FIX (compile error)
fn old<'py>(d: &'py PyDict) -> PyResult<&'py PyAny> { ... }

// Wrong handle: Py<T> param immediately bound  →  lifetime-handle-checker CONSIDER
fn awkward<'py>(py: Python<'py>, item: Py<PyAny>) -> PyResult<i64> {
    item.bind(py).extract()  // ← should have taken Bound<'py, PyAny> directly
}
```

---

## 8. Free-threading story (PEP 703, `python3.13t+`)

A major Python evolution. Free-threaded builds (`python3.13t`, `python3.14t`)
remove the GIL. PyO3 supports it; see §3 for the mechanics — it is *not*
feature-gated (PyO3 detects a free-threaded interpreter automatically).

For each `#[pyclass]` on an FT-target crate, the question is:

1. Can multiple threads call `&mut self` methods on it? PyO3's default is
   runtime borrow checking, which *raises/panics* on conflicting access rather
   than racing. If the class is logically immutable from Python,
   `#[pyclass(frozen)]` expresses that and opts out of `&mut self`.
2. Does it own `Py<T>` fields? Concurrent reads of a `Py<T>` are fine (PyO3
   handles refcounting atomically). Concurrent *mutation* of the field
   requires interior locking (`Mutex<Py<T>>` or `RwLock<Py<T>>`).
3. The module's `gil_used` declaration. Since PyO3 0.28 a module attests
   thread-safety by default; a not-yet-audited crate should *opt out* with
   `#[pymodule(gil_used = true)]`. On 0.23–0.27 it was opt-in (`gil_used = false`).

The toolkit doesn't *fix* free-threading bugs — it surfaces them as CONSIDER
when the crate's signals (maintainer-stated FT intent, CI building `3.13t`/
`3.14t`) say FT is targeted and the implementation doesn't match. An `abi3`
feature is *not* an FT signal — PyO3 ignores `abi3` under a free-threaded build.

If the project has *no* FT signals, free-threading gaps are POLICY, not FIX
or CONSIDER. Don't push someone toward FT migration who hasn't asked.

---

## 9. Concept translation from cext-review-toolkit

For implementers who internalized cext-review-toolkit's model:

| cext concept | PyO3 equivalent | Where it lives now |
|---|---|---|
| Manual refcounting (`Py_INCREF`/`Py_DECREF`) | `Drop` on `Py<T>` and `Bound<'py, T>` | Resurfaces inside `unsafe { ffi::* }` — see unsafe-block-auditor |
| Borrowed-ref-across-call | Borrow-checker prevents at compile time | Resurfaces inside `unsafe` only |
| `PyErr_Clear` swallowing | `.ok()`, `let _ =`, swallowed `PyResult` | pyresult-propagation-checker |
| `PyErr_Occurred` after API call | `?` operator implicit | pyresult-propagation-checker (missing `?`) |
| NULL pointer deref | `Option<T>` / `PyResult<T>` | Resurfaces inside `unsafe { ffi::* }` |
| `Py_TPFLAGS_HAVE_GC` + `tp_traverse` / `tp_clear` | `#[pymethods]` `__traverse__` / `__clear__` | pyclass-protocol-checker |
| Single-phase vs multi-phase init | `#[pymodule]` shape; `static Py<...>` patterns | module-init-checker |
| Static `PyObject*` globals | `static OnceLock<Py<T>>`, `static Lazy<Py<T>>` | module-init-checker |
| Stable ABI compliance | abi3 feature + `pyo3-ffi` calls outside abi3 set | pyo3-version-compat-scanner |
| Limited API headers | abi3 surface + `pyo3-ffi` calls | pyo3-version-compat-scanner |
| Free-threading declaration (`Py_mod_gil`) | `#[pymodule(gil_used = …)]` / `m.gil_used(…)?` (default flipped in 0.28 — see §3) | gil-discipline-checker + module-init-checker |
| Type slot signature errors | `#[pymethods]` slot signature errors | pyclass-protocol-checker |
| `__richcmp__` returning borrowed NotImplemented | Same bug, `py.NotImplemented().into()` vs `&py.NotImplemented()` | pyclass-protocol-checker |
| Module state via `PyModule_GetState` | PyO3 doesn't expose this directly; users keep state in `static` or per-instance | module-init-checker (note: PyO3 has less expressive support here) |
| `cext-review-toolkit`'s generated-code-mapper preflight | `rust-codegen-mapper` preflight | Same role, PyO3-flavored output |

Things that have **no analog**:

- Refcount-auditor in cext, dropped here entirely — refcounting is handled by
  `Drop` in safe Rust, and the unsafe surface is covered by unsafe-block-auditor.
- Null-safety-scanner, dropped — fallible PyO3 APIs return `PyResult` or
  `Option`; null dereferences only happen in `unsafe { ffi::* }` and the unsafe
  agent covers them.
- Pyerr-clear-auditor, folded into pyresult-propagation-checker — clearing a
  current exception in Rust manifests as `.ok()` or `let _ =`, same root concept.
- Resource-lifecycle-checker, dropped — `Drop` handles it.
- Stable-abi-checker, folded into pyo3-version-compat-scanner — PyO3 enforces
  most abi3 rules at compile time when the `abi3` feature is set.

Things that are **new** (no cext analog):

- unsafe-block-auditor — the soundness obligation surface
- pyclass-traits-checker — `Send` / `Sync` / `unsendable` / `frozen`
- lifetime-handle-checker — `Bound` vs `Py` vs `Borrowed` vs legacy `&PyAny`
- panic-safety-checker — `.unwrap()`, slice indexing, `unreachable!()` in
  reachable code paths

---

## 10. Reading list (when you need to go deeper)

Authoritative references the toolkit's logic should align with:

- **PyO3 user guide**: <https://pyo3.rs/> — read at minimum: "Calling Python
  from Rust", "Class system", "Memory management", "GIL", "Free-threaded Python"
- **PyO3 migration guides**: <https://pyo3.rs/main/migration.html> — the
  `data/pyo3_api_tables.json` seeds come from these
- **PEP 703 (Making the GIL Optional)**: free-threading rationale
- **CPython buffer protocol docs**: `Py_buffer` field semantics for manual
  `__getbuffer__` audits
- **PyO3 source**: when in doubt about what the macros emit, `cargo expand` a
  small example or read the macro source under `pyo3-macros/`

When the design doc disagrees with current PyO3 docs (PyO3 evolves fast),
**trust current PyO3 docs and flag the design discrepancy back** rather than
implement against a stale spec.

---

## 11. What this primer deliberately does NOT cover

- Async PyO3 (`pyo3-async-runtimes`) — out of scope for v0.1 per design §10
- numpy bridging (`pyo3-numpy`) — out of scope per design §10
- Embedding a Python interpreter from Rust — different use case, not what this
  toolkit reviews
- `rust-cpython` (the legacy ecosystem) — out of scope per design §1

If you find yourself confused by something in those areas, that's a signal
it's out of scope. Move on.
