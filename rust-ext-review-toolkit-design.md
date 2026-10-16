# rust-ext-review-toolkit — Design Document

## 1. Project Identity

**Name:** rust-ext-review-toolkit
**Purpose:** A Claude Code plugin for reviewing Python extensions written in Rust — finding soundness bugs in `unsafe` code, PyO3 API misuse, lifetime/GIL discipline issues, `#[pyclass]` trait-bound violations, panic-safety problems, and PyO3 version compatibility concerns.

**Tagline:** *Find the soundness bugs in your Rust extension before your users do.*

**First-cut scope:** PyO3 only. Other binding ecosystems (legacy `rust-cpython`, `pyo3-async-runtimes` add-ons, `cffi`-style raw FFI crates) are out of scope for v0.1 and may be added later (see §10, Open Questions and Future Work).

### 1.1 Relationship to Sibling Projects

| Project | Target | Parsing | Key Concern |
|---------|--------|---------|-------------|
| code-review-toolkit | Python source | `ast` | Logic errors, dead code, test gaps |
| cpython-review-toolkit | CPython runtime C code | Regex (PEP 7) | Refcount leaks, GIL, NULL safety |
| cext-review-toolkit | C extensions | Tree-sitter-c | API misuse, borrowed-ref lifetime, module state, type slots |
| **rust-ext-review-toolkit** | **Rust extensions** | **Tree-sitter-rust** | **`unsafe` soundness, `Bound`/`Py` lifetime, `#[pyclass]` Send/Sync, GIL release across handles, panic at FFI boundary** |

The core lesson from cext-review-toolkit transfers: use Tree-sitter because Rust extension code styles vary wildly and macro-heavy code (procedural macros in particular) defeats regex-based analysis. But the bug *catalog* is fundamentally different because PyO3's type system already eliminates most of the C-extension bug classes (manual `Py_INCREF`/`Py_DECREF`, NULL pointer derefs, borrowed-ref-across-call) while introducing its own.

### 1.2 What PyO3 Already Prevents — and What It Doesn't

**PyO3's type system eliminates** (so we don't need scanners for):
- Manual reference counting — `Py<T>` and `Bound<'py, T>` implement `Drop`, refcount goes down automatically
- Most NULL-pointer dereferences — fallible APIs return `PyResult<T>` or `Option<T>`
- Most use-after-free of Python objects — the `'py` lifetime on `Bound<'py, T>` is tied to a `Python<'py>` GIL token; you can't outlive the GIL in safe code
- Memory safety in safe Rust — guaranteed by the borrow checker

**PyO3 does *not* prevent** (so we *do* need scanners for):
1. **Soundness bugs in `unsafe` blocks** — raw `pyo3-ffi` calls, `transmute`, manual buffer-protocol implementations, FFI into C libraries
2. **`#[pyclass]` trait-bound violations** — since PyO3 0.23 a `#[pyclass]` requires `Send + Sync` (not just `Send`); types with non-`Send`/non-`Sync` fields (e.g. `Rc`, `RefCell`, `Cell`, raw pointers) need `#[pyclass(unsendable)]`. Free-threading additionally wants `frozen`.
3. **Holding `Py<T>` across `Python::detach`** (legacy `allow_threads`) — releasing the interpreter is fine, but binding the handle on the other side without re-attaching is UB
4. **Panic crossing the FFI boundary** — `.unwrap()` / `.expect()` in `#[pymethods]`, panic-unwind across `extern "C"` boundaries
5. **`PyResult` propagation gaps** — `.ok()` to swallow exceptions, `let _ = result;`, missing `?` operators, exception clobbering
6. **`Bound<'py, T>` vs `Py<T>` vs `Borrowed<'a, 'py, T>` choice** — wrong handle kind for the use case
7. **`#[pymethods]` slot correctness** — `__traverse__`/`__clear__` gaps for GC types containing `Py<T>`, `#[new]` vs `__init__` confusion, `#[pyclass(extends = ...)]` inheritance pitfalls, `__richcmp__` semantics
8. **PyO3 version migration debt** — `&PyAny` / `&PyDict` (pre-0.21) coexisting with `Bound<'py, T>` (0.21+), and use of APIs renamed/removed across the 0.21–0.29 reshufflings (exact versions live in `data/pyo3_api_tables.json`)
9. **Free-threaded Python readiness (3.13t/3.14t, PEP 703)** — the `gil_used` module declaration, atomic operations on shared state, `#[pyclass(frozen)]` for thread-safe sharing
10. **`extract::<T>()` / `downcast::<T>()` misuse** — `.unwrap()` on fallible conversions, wrong target type

This list is the agent inventory for v0.1.

### 1.3 Audience

- Rust extension authors (maturin/PyO3 library maintainers)
- Contributors reviewing PyO3 crates in third-party projects
- Teams migrating extensions across PyO3 major versions (0.20 → 0.21 → … → 0.30; current stable is 0.29)
- Developers preparing PyO3 extensions for free-threaded Python

### 1.4 Non-Goals

- General Rust code analysis (use `clippy`, `cargo audit`, `cargo geiger`)
- Reviewing pure-Rust code with no Python boundary
- Reviewing Cython, pybind11, or hand-written C extensions (use cext-review-toolkit)
- Requiring a buildable environment (must work on raw source files; `cargo expand` and `cargo metadata` are opportunistic, not required)

---

## 2. Architecture

### 2.1 Parsing Layer: Tree-sitter-rust

All scripts use `tree-sitter` with `tree-sitter-rust` as the primary parsing backend.

**Dependencies:** `tree-sitter` and `tree-sitter-rust` (pip-installable, pure wheels on most platforms, no system dependencies).

**What Tree-sitter-rust gives us:**

| Capability | Why it matters |
|------------|----------------|
| Reliable extraction of `fn` items, `impl` blocks, `mod` items | Function/method boundaries for scope-bounded analysis |
| Attribute macro identification (`#[pyclass]`, `#[pymethods]`, `#[pymodule]`, `#[pyfunction]`, `#[new]`) | The entire PyO3 surface area is attribute-driven |
| `unsafe` block / `unsafe fn` detection | Single most important node type for this toolkit |
| Type-path analysis (e.g. `Bound<'py, PyDict>`, `Py<MyClass>`) | Distinguishes handle kinds in signatures |
| `use` statement analysis | Detects which PyO3 modules and version-gated APIs are imported |
| Closure body extraction | `Python::attach(\|py\| { ... })` / `py.detach(\|\| { ... })` (and the pre-0.25 names `with_gil` / `allow_threads`) are closures whose body we need to walk |
| Trait-bound parsing (`where T: Send`) | `#[pyclass]` trait-bound analysis |

**Limitations to be honest about:**
- Tree-sitter does NOT expand procedural macros. `#[pyclass]` generates substantial code we don't see at the source level. Some analyses (e.g., "is `__traverse__` actually wired?") need either macro-expansion knowledge baked into the scanner, or opportunistic `cargo expand` integration.
- Trait-bound *satisfaction* (does `MyType: Send` hold?) requires the compiler. We can detect *declared* bounds and known-non-Send field types (`Rc<_>`, `RefCell<_>`, `*const _`, `*mut _`, `Cell<_>`, etc.) syntactically, but we can't fully verify.

**Shared parsing utilities** (`rust_ts_utils.py`):

```
parse_file(path) → Tree
extract_fn_items(tree, source) → list[FnNode]            # all fn items + methods in impls
extract_impl_blocks(tree, source) → list[ImplBlock]      # for #[pymethods] analysis
extract_struct_defs(tree, source) → list[StructDef]      # #[pyclass] targets
extract_attribute_targets(tree, source, attr) → list     # all items carrying a given #[attr]
extract_attributes(node, source) → list[AttributeNode]   # all #[...] on an item
extract_unsafe_blocks(node, source) → list[UnsafeNode]   # both unsafe { ... } and unsafe fn
find_calls_in_scope(node, source, fn_names=None) → list  # method/function calls
find_closure_bodies(node, source, callee=None) → list    # closure bodies, optionally filtered
extract_use_statements(tree, source) → list[UseNode]     # for version-API detection
extract_generic_params(node, source) → list[Generic]     # for lifetime/trait-bound analysis
```

**Fallback:** If Tree-sitter is not installed, scripts print a clear error: `"rust-ext-review-toolkit requires tree-sitter and tree-sitter-rust: pip install tree-sitter tree-sitter-rust"` and exit. No silent degradation.

### 2.2 Shared Data

```
data/
├── pyo3_api_tables.json        # By PyO3 version: stable APIs, deprecated, removed
├── pyo3_version_matrix.json    # Version-to-version migration map (0.20→0.21 etc.)
├── abi3_compatible_apis.json   # APIs available under the abi3 feature, by Python floor
├── non_send_sync_types.json    # Types not Send and/or not Sync (Rc, RefCell, Cell, *const T, NonNull, ...)
├── panic_unwind_apis.json      # Stdlib APIs that panic (slice index, .unwrap, .expect, ...)
├── pyo3_idioms.json            # Common PyO3 patterns + their ACCEPTABLE forms
├── stable_abi.json             # Vendored from cext-review-toolkit (for pyo3-ffi limited-API audits)
└── limited_api_headers.json    # Vendored from cext-review-toolkit (same purpose)
```

`pyo3_api_tables.json` is the largest and most maintenance-intensive — PyO3 evolves rapidly and several major releases (0.21, 0.23, 0.25, 0.27, 0.28, 0.29) reshaped the API. The format mirrors cext-review-toolkit's `deprecated_apis.json` but is keyed by PyO3 version rather than Python version.

`stable_abi.json` and `limited_api_headers.json` are **vendored directly from cext-review-toolkit** — they describe CPython's limited API surface, which is exactly what raw `pyo3-ffi` calls inside `unsafe` blocks are subject to when the crate targets the `abi3` feature. The synchronization surface is just these data files.

### 2.3 External Tool Integration (Optional)

| Tool | Detection | What It Adds | Fallback |
|------|-----------|--------------|----------|
| clippy | `cargo clippy --message-format=json` from project root | Idiom lints, common Rust mistakes | Tree-sitter only |
| miri | `cargo +nightly miri --version` | UB detection in `unsafe` code (when tests exist) | Tree-sitter only |
| cargo-geiger | `cargo geiger --version` | Counts of `unsafe` usage per dep | Tree-sitter only |
| cargo-expand | `cargo expand --version` | Expanded macro output for deep `#[pyclass]` analysis | Source-level analysis |
| cargo metadata | `cargo metadata --format-version=1` | Resolved PyO3 version, feature flags, target Python floor | Parse `Cargo.toml` heuristically |

Agent prompts note whether external tool results are available:
- "clippy results available — cross-reference for additional Rust idiom findings"
- "cargo metadata available — PyO3 version confirmed as 0.22.5"
- "No external tools available — analysis based on Tree-sitter and Cargo.toml parsing"

### 2.4 Project Discovery

Detection strategy (first match wins, but multiple can co-exist):

1. **`Cargo.toml` with `pyo3` dependency and `[lib] crate-type = ["cdylib"]` or `["cdylib", "rlib"]`** — strongest signal
2. **`pyproject.toml` with `[build-system] requires = ["maturin"]`** — confirms a maturin-built PyO3 extension
3. **`pyproject.toml` with `[build-system] requires = ["setuptools-rust"]`** + `[[tool.setuptools-rust.ext-modules]]` — setuptools-rust path (pyproject.toml-driven)
4. **`setup.py` with `setuptools_rust.RustExtension(...)` entries** — older setuptools-rust projects still use this style
5. **`Cargo.toml` with `pyo3 = { ... }` but no `cdylib`** — Rust *binary* using PyO3 to embed an interpreter; out of scope but detected and reported
6. **Fallback:** any `.rs` file containing `use pyo3::` or `#[pymodule]` / `#[pyfunction]` / `#[pyclass]` attributes
7. **Manual scope:** user points to a specific directory or file

The discovery script produces:
- Crate name (from `[package] name` or `[lib] name`)
- Module name (from `#[pymodule] fn xxx` — must match the cdylib name after `lib` stripping)
- PyO3 version (exact if `Cargo.lock` exists, otherwise the version spec from `Cargo.toml`)
- Active PyO3 features (`extension-module`, `abi3`, `abi3-py39`/`abi3-py310`/etc., `abi3t`, `abi3t-py315`, `multiple-pymethods`, `experimental-async`, `chrono`, `num-complex`, ...). There is no `gil-disabled` Cargo feature; `abi3t` selects a stable ABI, not whether the interpreter has a GIL.
- Declared Python version floors: `python_floor` retains the lowest traditional `abi3-pyNM` floor, falling back to the `abi3t-pyNM` floor when no traditional floor is declared. `abi3t_python_floor` separately records the lowest explicit `abi3t` floor. Bare features leave their floors unknown; these fields do not establish the effective build ABI.
- Build backend (maturin / setuptools-rust [pyproject] / setuptools-rust [setup.py])
- Whether free-threading is targeted. Infer intent from `gil_used` declarations, free-threaded CI interpreters, and explicit `abi3t`/`abi3t-pyNM` features. Traditional `abi3` alone is not an FT signal and does not cover free-threaded builds, including Python 3.15+. PyO3 0.29+ supports the separate `abi3t` ABI for Python 3.15+; older free-threaded interpreters still require version-specific wheels. These signals do not prove thread safety or the final wheel ABI.
- All `.rs` source files in the crate, with classification (entry-point module vs supporting modules)
- Whether the crate has accompanying Python source (`python/` or similar — relevant for parity-checker)

---

## 3. Agents

### 3.1 Agent Overview

**13 agents total** — 11 script-backed, 1 qualitative (parity-checker), 1 preflight (rust-codegen-mapper). Grouped by concern:

#### Safety-critical (script-backed, Tree-sitter-powered) — **the highest-value category**

| Agent | What It Finds | Script |
|-------|---------------|--------|
| **unsafe-block-auditor** | Soundness concerns in every `unsafe` block; pyo3-ffi raw calls; transmutes; manual buffer-protocol impls | `scan_unsafe_blocks.py` |
| **pyresult-propagation-checker** | `.unwrap()`/`.expect()` on `PyResult` inside `#[pymethods]`; swallowed errors via `.ok()`/`let _ =`; missing `?` operators; exception clobbering | `scan_pyresult.py` |
| **gil-discipline-checker** | `Bound<'py, T>` held across `detach`/`allow_threads`; calling Python APIs in a detached closure without re-attaching; nested `attach`/`with_gil`; foreign-callback GIL acquisition; finalization-time `attach`; free-threading `gil_used` declarations | `scan_gil_discipline.py` |
| **panic-safety-checker** | Panic-prone calls (`.unwrap()`, slice indexing, integer overflow with `-C overflow-checks`) reachable from `#[pymethods]` / `#[pyfunction]` | `scan_panic_safety.py` |

#### PyO3-specific (script-backed, Tree-sitter-powered, new)

| Agent | What It Finds | Script |
|-------|---------------|--------|
| **pyclass-traits-checker** | `#[pyclass]` containing non-`Send` fields without `unsendable`; missing `frozen` for free-threading; `Sync` requirements; raw-pointer fields without justification | `scan_pyclass_traits.py` |
| **pyclass-protocol-checker** | `#[pymethods]` slot signatures; `__traverse__`/`__clear__` gaps for types holding `Py<T>`; `#[new]` vs `__init__` confusion; `#[pyclass(extends = ...)]` inheritance correctness; `__richcmp__` semantics | `scan_pyclass_protocol.py` |
| **module-init-checker** | `#[pymodule]` shape; static `OnceLock<Py<...>>` module-level handles; multi-phase init opt-in; `module.add_class` / `module.add` error handling | `scan_module_init.py` |
| **lifetime-handle-checker** | `Bound<'py, T>` vs `Py<T>` vs `Borrowed<'a, 'py, T>` choice mismatches; legacy `&PyAny` / `&PyDict` references in PyO3 ≥ 0.21 code; `.clone()` on `Py<T>` where `.clone_ref(py)` is needed | `scan_lifetime_handles.py` |

#### Compatibility (script-backed, new)

| Agent | What It Finds | Script |
|-------|---------------|--------|
| **pyo3-version-compat-scanner** | Deprecated APIs by PyO3 version; removed APIs still in use; abi3 feature consistency; raw `pyo3-ffi` calls outside the abi3 surface when abi3 is on | `scan_pyo3_version_compat.py` |

#### Code quality and history

| Agent | What It Finds | Script |
|-------|---------------|--------|
| **rust-complexity-analyzer** | Functions scored by cyclomatic complexity, nesting depth, line count, and `unsafe`-block density | `measure_rust_complexity.py` |
| **git-history-analyzer** | Similar bugs elsewhere in the crate; churn-based risk prioritization | `analyze_history.py` |

#### Cross-implementation parity (qualitative)

| Agent | What It Finds |
|-------|---------------|
| **parity-checker** | When the project has both a Rust extension and a pure-Python fallback (common pattern: `_xxx.so` + `xxx.py`), behavioral divergences in error messages, type coercion, edge cases |

#### Preflight (qualitative, runs first)

| Agent | What It Finds |
|-------|---------------|
| **rust-codegen-mapper** | Detects PyO3 version + features; classifies files (hand-written `.rs` vs `build.rs`-generated vs vendored); catalogues project-specific patterns; produces an orientation guide consumed by every downstream agent |

### 3.2 Agent Details

#### 3.2.1 unsafe-block-auditor

**Why this is the #1 agent** (not refcount-auditor as in cext-review-toolkit): in PyO3 code, every `unsafe` block is a soundness obligation that the type system has explicitly stopped enforcing. Soundness bugs there are the analogue of borrowed-ref-across-call in C — they cause use-after-free, data races, and UB that may not manifest until a particular Python version, allocator, or thread schedule.

**Patterns to detect:**

1. **Raw `pyo3-ffi` calls.** `ffi::Py_INCREF`, `ffi::PyDict_SetItem`, `ffi::Py_DECREF`, etc. — each requires the GIL held, valid non-null pointers, and correct ownership semantics. Flag each call with the API it makes and whether the local context has a `Python<'py>` token in scope.
2. **Raw pointer dereferences from `as_ptr()` / `as_mut_ptr()`.** `(*obj.as_ptr()).ob_refcnt += 1;` — directly manipulating refcounts bypasses PyO3's tracking.
3. **`transmute` between PyO3 handle types.** `mem::transmute::<Py<T>, Bound<'py, T>>(...)` and similar — almost always wrong; the correct path is `Py::bind(py)` or `Py::into_bound(py)`.
4. **Manual `__getbuffer__` / `__releasebuffer__` implementations.** These require populating the `Py_buffer` struct correctly; check that `obj`, `buf`, `len`, `itemsize`, `ndim`, `shape`, `strides`, and `suboffsets` are all set, and that the matching `__releasebuffer__` undoes whatever `__getbuffer__` allocated.
5. **`unsafe impl Send` / `unsafe impl Sync` on `#[pyclass]` types.** These bypass the trait-bounds checker; flag every occurrence for human review with the field list of the type.
6. **FFI into C libraries.** Any `extern "C"` declared function called from a `#[pymethods]` block — flag for review of error-handling and resource ownership.
7. **Private `_Py*` symbol use.** Calls to `pyo3_ffi::_Py*` or `pyo3_ffi::_PyObject_*` — these are private CPython APIs, not part of any stable surface, comparable to cext-review-toolkit's stable-abi-checker findings.

**Cext-style checks apply inside `unsafe` blocks calling `pyo3-ffi`.** When the agent finds raw FFI calls, it should apply the borrowed-ref / new-ref / stolen-ref reasoning from cext-review-toolkit's `scan_refcounts.py` using the same `api_tables.json` data. Inside `unsafe { ffi::PyList_GetItem(list, 0) }` you really are receiving a borrowed reference and the C-extension hazards apply.

**Script:** `scan_unsafe_blocks.py`
- Walks the tree finding `unsafe_block` and `function_item` nodes with `unsafe` modifier
- For each, extracts the body, identifies the kind of unsafe operation, and notes whether `Python<'py>` is in lexical scope
- Cross-references function-call targets against `pyo3-ffi` symbol patterns and the vendored `stable_abi.json` / `limited_api_headers.json`

**Classification:**
- **FIX:** `transmute` between handle types, refcount manipulation via raw pointers, `unsafe impl Send` on a type with `Rc`/`RefCell` fields, FFI calls without error checking, private-`_Py*` API use when abi3 is targeted
- **CONSIDER:** Every other `unsafe` block (default — `unsafe` is a flag for review, even if the agent doesn't find a specific bug)
- **POLICY:** `unsafe impl Send` on types that the maintainer asserts are thread-safe (case-by-case)
- **ACCEPTABLE:** Documented patterns the maintainer has reviewed (looks for `// SAFETY:` comments adjacent to the block, as the cext-review-toolkit pattern)

#### 3.2.2 pyresult-propagation-checker

**Patterns to detect:**

1. **`.unwrap()` / `.expect()` on `PyResult<T>` inside `#[pymethods]` or `#[pyfunction]`.** Panics cross the FFI boundary; PyO3 catches them and raises `PanicException`, but this is almost never the maintainer's intent.
2. **`.ok()` on `PyResult` that discards the error.** Swallows Python exceptions silently.
3. **`let _ = some_pyresult;`** — same issue, harder to detect.
4. **Missing `?` operator** where a `PyResult<T>` is shadowed without being handled.
5. **Exception clobbering.** A pattern transplanted from cext-review-toolkit's error-path-analyzer: code that catches a `PyErr` from API call A, then calls API call B before propagating, where B can fail and overwrite the original exception. In Rust this manifests as:
   ```rust
   let err = some_call(py).err();        // captured PyErr
   another_call(py)?;                    // can fail, replacing context
   if let Some(e) = err { return Err(e); }
   ```
   The agent looks for explicit `PyErr` capture followed by additional Python-API calls before the error is re-raised.
6. **`PyErr::take()` / `PyErr::fetch()` / `PyErr::restore()` patterns** — verify save/restore is balanced inside `Drop` impls and `__traverse__` / `__clear__`.
7. **Silent `PyErr::clear()` calls** — clearing the current exception without a clear fallback pattern, the Rust analogue of cext's "silent exception swallowing."

**Script:** `scan_pyresult.py`
- Walks `#[pymethods]` and `#[pyfunction]` bodies
- Identifies all expressions returning `PyResult<T>` (heuristic: function calls in the pyo3 namespace, methods on `Bound<'_, T>` and `Py<T>`)
- Flags `.unwrap()`, `.expect()`, `.ok()`, unhandled shadowing, and the clobbering pattern

**Classification:**
- **FIX:** `.unwrap()` in `#[pymethods]` body where the called API can raise (not in a panic-is-impossible context); confirmed exception clobbering on a reachable path
- **CONSIDER:** `.expect()` with a descriptive message — better than `.unwrap()` but still panics; clobbering on a rare path
- **POLICY:** `.unwrap()` in `#[pyclass(unsendable)]` types only used from a single thread under documented invariants
- **ACCEPTABLE:** `.unwrap()` on a `PyResult` from an API documented as infallible under the call conditions (rare but exists)

#### 3.2.3 gil-discipline-checker

**Terminology note.** PyO3 renamed the GIL API in 0.25: `Python::with_gil` → `Python::attach`, `py.allow_threads` → `Python::detach`, `Python::with_gil_unchecked` → `Python::attach_unchecked`. The toolkit audits crates across PyO3 0.20–0.29, so the scanner recognizes **both** vocabularies (`with_gil`≡`attach`, `allow_threads`≡`detach`); these pairs are recorded in `data/pyo3_api_tables.json`. This section uses the current names, with the legacy name in parentheses on first mention.

**Patterns to detect:**

1. **`Bound<'py, T>` referenced inside a `Python::detach` (legacy `allow_threads`) closure.** This is a compile error in safe code (the `'py` lifetime is tied to `Python<'py>`, which is not available inside the closure), but `unsafe` code can violate it.
2. **`Py<T>::bind(py)` inside a `detach` closure.** No `py` is available — flag any `bind` call in a detached scope.
3. **Calls into the Python C API inside a `detach` closure.** Raw `pyo3-ffi` calls require an attached interpreter; calling them while detached is UB.
4. **Nested `Python::attach` (legacy `with_gil`).** Almost always wrong — `attach` inside an already-attached scope is unnecessary and a deadlock risk on some platforms; the inner one should use the outer `py` token.
5. **Foreign-callback GIL acquisition.** When a Rust `extern "C" fn` is registered with a non-PyO3 C library (e.g. via `libc`, `libsqlite3-sys`, or a hand-written binding) and the callback body uses PyO3 APIs, the callback must first attach via `Python::attach`. Flag any `extern "C" fn` containing PyO3 calls without a top-level `attach`.
6. **`attach` during interpreter finalization.** `Python::attach` panics if called after the interpreter has finalized. Long-lived callback bridges (timer threads, signal handlers, foreign-runtime callbacks) should guard with a finalization check first. Note `attach_unchecked` is **not** such a guard — it *skips* the interpreter-state check, making it more dangerous, not less.
7. **Long-running synchronous work without `detach`.** Heuristic: a function with a loop containing no Python API calls but an attached interpreter is a candidate for releasing via `detach`.
8. **Free-threading concerns** (PEP 703): on an FT-targeted crate, check the module's `gil_used` declaration. Since PyO3 0.28 a module attests thread-safety **by default** (undeclared ⇒ `gil_used = false`); a crate not yet audited for FT safety should *opt out* with `#[pymodule(gil_used = true)]`. On 0.23–0.27 the default was the opposite. Also flag `#[pyclass]` types with shared mutable state reached from multiple threads without `frozen` or interior locking.

**Script:** `scan_gil_discipline.py`
- Finds all `Python::attach`/`with_gil` and `Python::detach`/`py.allow_threads` closures (both vocabularies)
- For each detached-closure body, walks for any expression that requires `Python<'py>`
- For each `attach` outside of `#[pymodule]` initialization, checks for an ancestor `attach` or `Python<'py>` parameter
- Walks all `extern "C" fn` bodies, flags those using PyO3 APIs without a top-level `attach`

**Classification:**
- **FIX:** Confirmed `Bound`/`Py::bind` use inside a `detach` closure (via `unsafe` cheating); calling pyo3-ffi while detached; foreign callback using PyO3 APIs without attaching
- **CONSIDER:** Long synchronous work in an attached scope; missing/incorrect `gil_used` declaration on FT-targeted crates; finalization-vulnerable `attach`
- **POLICY:** Whether to release the interpreter around specific calls (performance vs simplicity trade-off)
- **ACCEPTABLE:** `attach` at module top level; nested `attach` in re-entrant callback bridges that document the rationale

#### 3.2.4 panic-safety-checker

**Patterns to detect:**

1. **`.unwrap()` / `.expect()` on non-`PyResult` values reachable from `#[pymethods]`.** (`Option::unwrap`, `Result::unwrap` for non-Python errors, slice/array indexing with `[i]`, `Vec::remove(i)`.)
2. **Integer arithmetic in debug builds** where `-C overflow-checks` is on — `a + b` can panic. Less critical but worth a note.
3. **`unimplemented!()` / `todo!()` / `unreachable!()`** in production code paths.
4. **Patterns that defeat PyO3's panic-catching:** panicking inside `extern "C"` callbacks registered with non-PyO3 C libraries (e.g. via the `libc` crate or a raw FFI binding); panics there abort the process.

**Script:** `scan_panic_safety.py`
- Walks `#[pymethods]` bodies and any function called transitively from them (one hop, heuristic)
- Identifies panic-prone expressions
- Cross-references with `extern "C" fn` definitions to flag callbacks

**Classification:**
- **FIX:** `.unwrap()` on a value that can be `None` in normal control flow inside `#[pymethods]`; panic inside non-PyO3 `extern "C" fn`
- **CONSIDER:** Slice indexing without bounds verification; `unimplemented!()` reachable from public methods
- **ACCEPTABLE:** `.unwrap()` on values proven non-`None` by an earlier check in the same function (within-scope reasoning); `unreachable!()` after an exhaustive match

#### 3.2.5 pyclass-traits-checker

**Patterns to detect:**

1. **`#[pyclass]` struct with non-`Send`/non-`Sync` fields and no `unsendable` attribute.** Since PyO3 0.23, `#[pyclass]` requires `Send + Sync` (before 0.23, only `Send`); if the struct contains `Rc<_>` (neither), `RefCell<_>`/`Cell<_>` (`Send` but not `Sync`), `*const _`, `*mut _`, or a generic `T` without the bounds, the type fails the requirement. Detect via field-type pattern matching against `data/non_send_sync_types.json`, which classifies each type by whether it breaks `Send`, `Sync`, or both.
2. **`unsafe impl Send for MyPyClass` without a `// SAFETY:` comment.** Almost always a code smell.
3. **Missing `#[pyclass(frozen)]` for free-threading.** On free-threading-targeted crates, mutable shared state in `#[pyclass]` needs `frozen` + interior locking (`Mutex`, `RwLock`) for sound concurrent access.
4. **`#[pyclass(extends = ParentType)]` with `Drop` impl.** Inheritance + custom Drop is a known pitfall; verify that subclass Drop runs before parent.
5. **`#[pyclass]` with `dict` or `weakref` options without GC slots.** If the class is collectable, it needs `__traverse__`/`__clear__`.

**Script:** `scan_pyclass_traits.py`
- For each `#[pyclass]` attribute, extract struct definition and field list
- Match field types against the non-Send list
- Cross-reference declared `unsendable`/`frozen`/`subclass`/`extends` options
- Note when `unsafe impl Send` is used and whether a SAFETY comment precedes it

**Classification:**
- **FIX:** Non-`Send` field without `unsendable`, no `unsafe impl Send` (compile error, but detectable in mid-edit drafts); `unsafe impl Send` on a type with `Rc`
- **CONSIDER:** Missing `frozen` on FT-target crates; missing `dict`/`weakref` GC handling
- **POLICY:** `unsendable` for performance/single-thread use cases
- **ACCEPTABLE:** Documented `unsafe impl Send` with verified invariants

#### 3.2.6 pyclass-protocol-checker

**Patterns to detect:**

1. **`#[pyclass]` containing `Py<T>` or `Bound<'py, T>` fields without `__traverse__`/`__clear__`.** Reference cycles through Python create leaks invisible to Rust's `Drop`.
2. **`#[pymethods]` signature mismatches.** `#[getter]` taking arguments, `#[setter]` returning non-`PyResult<()>`, `#[new]` not returning `Self` / `PyResult<Self>` / `(Self, base)` etc.
3. **`#[new]` vs `__init__` confusion.** PyO3 uses `#[new]` for construction; if the user writes both, semantics are ambiguous.
4. **`#[pyclass(extends = ...)]` `#[new]` returning bare `Self` instead of `(Self, PyClassInitializer<Parent>)`.**
5. **`__richcmp__` semantics.** Inherited from cext-review-toolkit's type-slot-checker findings about `tp_richcompare`:
   - Returning `py.NotImplemented()` as a borrowed reference (must be a new ref — `py.NotImplemented().into()` or equivalent that bumps the count)
   - Not handling all 6 comparison ops (`CompareOp::Lt` through `CompareOp::Ge`); missing default `Ok(py.NotImplemented().into())` for unhandled cases
   - Returning `bool` directly instead of `PyResult<PyObject>` (no way to express NotImplemented)
6. **Other dunder signature errors.** `__hash__` returning non-`u64`/`isize`; `__len__` returning `i64` instead of `usize`; `__eq__` returning `bool` instead of `PyResult<bool>`.
7. **Number/sequence protocol slots** — `__add__`/`__getitem__` etc. that don't return `py.NotImplemented()` for unrecognized operand types.
8. **Buffer-protocol misuse** (when `__getbuffer__` is manually impl'd in unsafe): see unsafe-block-auditor cross-reference.

**Script:** `scan_pyclass_protocol.py`
- For each `#[pyclass]` type, find the corresponding `#[pymethods] impl` block
- Enumerate the methods and their attributes (`#[new]`, `#[getter]`, `#[setter]`, `#[classmethod]`, `#[staticmethod]`, dunder names by convention)
- Check signatures against the PyO3 expected shape (encoded in scanner logic + `data/pyo3_idioms.json`)
- Walk the struct field list; if any `Py<_>` / `Bound<_>` fields, look for `__traverse__` and `__clear__` methods

**Classification:**
- **FIX:** Missing `__traverse__` on a type with `Py<T>` fields; wrong dunder signature that causes runtime errors; `__richcmp__` returning borrowed `NotImplemented`
- **CONSIDER:** `__richcmp__` handling fewer than all 6 ops without an explicit default; `__hash__` returning `u64` where `isize` is canonical
- **POLICY:** Whether to implement comparison dunders at all

#### 3.2.7 module-init-checker

**Patterns to detect:**

1. **Module-level `static OnceLock<Py<_>>` or `static Lazy<Py<_>>`.** Global Python objects bypass per-interpreter state. CONSIDER on extensions that don't target subinterpreters; FIX on those that do.
2. **`#[pymodule]` body that doesn't return `PyResult<()>`.** Missing error propagation.
3. **`module.add_class::<T>()` calls that don't propagate errors via `?`.**
4. **Submodule registration without proper parent linking.** A submodule should be added with `module.add_submodule()` and have its `__name__` set correctly to avoid pickle/import issues.
5. **Free-threading declarations.** Whether the module's `gil_used` is declared when the crate's other signals indicate FT intent (recall: free-threading is not feature-gated).

**Script:** `scan_module_init.py`
- Finds the `#[pymodule]` function
- Walks for static items at module scope, classifies each
- Cross-references with discovered features from `Cargo.toml`

**Classification:**
- **FIX:** Missing `?` on `add_class` / `add` calls (silent failures)
- **CONSIDER:** Global `OnceLock<Py<_>>` patterns (subinterpreter incompat); missing FT declarations on FT-targeted crates
- **POLICY:** Whether to support subinterpreters at all

#### 3.2.8 lifetime-handle-checker

**Patterns to detect:**

1. **`&PyAny` / `&PyDict` / `&PyList` / etc. references in PyO3 ≥ 0.21 code.** The pre-0.21 API; migration is the explicit goal.
2. **`Py<T>` parameter where `Bound<'py, T>` is more idiomatic.** A function that takes `Py<T>` and immediately calls `.bind(py)` should take `Bound<'py, T>` directly.
3. **`Borrowed<'a, 'py, T>` use without justification.** This handle is rarely needed; usually `&Bound<'py, T>` is what you want.
4. **`.clone()` on `Py<T>`.** `Py<T>: Clone` was available through 0.21; PyO3 0.22 gated it behind the opt-in `py-clone` feature. If the project uses it, flag for migration to `.clone_ref(py)`.
5. **`Py::as_ref(py)`** — the legacy pattern, removed in 0.26; should be `.bind(py)`.

**Script:** `scan_lifetime_handles.py`
- Walks for the specific type-path patterns above in function signatures and let bindings
- Cross-references with the resolved PyO3 version (from discovery) to determine FIX/CONSIDER severity

**Classification:**
- **FIX:** Use of GIL-ref APIs (`&PyAny` etc.) when `Cargo.toml` targets a PyO3 version that has removed them (compile error, but useful for partial-migration audits — the precise removal version is in `data/pyo3_api_tables.json`)
- **CONSIDER:** Mixed-style code in 0.21/0.22 crates (works but harder to read)

#### 3.2.9 pyo3-version-compat-scanner

**Patterns to detect:**

1. **APIs deprecated in the current PyO3 version.** Reference `data/pyo3_api_tables.json` keyed by version.
2. **abi3 feature consistency.** If `pyo3 = { features = ["abi3"] }` is set, raw `pyo3-ffi` calls must restrict to the limited-API subset (cross-reference `data/abi3_compatible_apis.json` and the vendored `stable_abi.json`).
3. **Python version floor consistency.** `abi3-py39` + use of `PyDict_GetItemRef` (3.13+) is incompatible; surface the conflict.
4. **`pyo3` feature combinations.** Some features are mutually exclusive or have known interactions; this is the maintenance burden.

**Script:** `scan_pyo3_version_compat.py`
- Loads `data/pyo3_api_tables.json` filtered by the discovered version
- Finds each use of the listed APIs
- Cross-references abi3 feature with raw FFI calls

#### 3.2.10 rust-complexity-analyzer

Mirrors `c-complexity-analyzer` but adapted:

**Patterns to detect:**
- Functions ranked by cyclomatic complexity (`if`/`else`/`match`/`while`/`for`/`?`/`&&`/`||` count + 1)
- Nesting depth (closures, blocks, match arms)
- Line count
- **Rust-specific:** `unsafe` block count per function (an `unsafe`-heavy function is reviewed at higher priority regardless of complexity)
- Trait-method count per `impl` (heuristic for "god types")

**Script:** `measure_rust_complexity.py`

#### 3.2.11 git-history-analyzer

Adapted from `code-review-toolkit`'s version, with the deliberate simplifications from cext-review-toolkit's adaptation.

**Capabilities retained:**
- Similar-bug detection (the crown jewel — find sibling occurrences of a flagged pattern elsewhere in the crate)
- Churn × quality risk matrix for prioritization

**Capabilities dropped:**
- Co-change coupling analysis — Rust extensions are typically a handful of files and the coupling signal is noisy at small scale
- New-feature review — not relevant for API-correctness focus
- Full fix-completeness review — retained only when the crate has ≥10 recent fix commits; otherwise skipped

**Script:** `analyze_history.py`
- Function-boundary detection uses Tree-sitter-rust (not Python `ast`)
- Classification keywords tuned for Rust/PyO3 patterns (`unsafe`, `unwrap`, `Send`, `Sync`, `Bound`, `attach`/`with_gil`, `detach`, ...)
- Takes `argv` (not `target, max_files`) to match the `code-review-toolkit` convention

#### 3.2.12 parity-checker (qualitative)

For crates with a dual implementation: pure-Python fallback alongside the Rust extension (a common pattern — e.g. `cryptography`, `pydantic-core`, `tokenizers`). The agent compares Python-visible behavior, error messages, type coercion, and edge cases.

No script — uses Read/Grep tools directly, guided by the discovery output that names both implementations.

### 3.3 rust-codegen-mapper (preflight)

Runs **first** in every `explore` pipeline and produces an orientation guide at `reports/<crate>_v1/preflight/rust_codegen_map.md`. Downstream agents read it before their own Phase 1.

**Difference from cext-review-toolkit's `generated-code-mapper`.** In cext, the mapper exists because a project may use Cython *or* pybind11 *or* nanobind *or* Argument Clinic — distinct generators, each with its own playbook, selected by a detection cascade. PyO3 has **one** generator: PyO3 itself. There is nothing to detect and no playbook to select, so this agent carries none of that machinery — there is no `data/playbooks/` directory. The PyO3-specific knowledge it needs lives directly in its own prompt plus `data/pyo3_idioms.json`.

It also does **not** see past procedural macros — `cargo expand` integration is deferred (§10). Its value is not "revealing generated code"; it is **orientation**:

**What it catalogs:**
- PyO3 version, edition, and active features (`abi3`/`abi3-pyNM`, `extension-module`, `multiple-pymethods`, `experimental-async`, ...) — a readable digest of `discover_rust_ext.py` output
- Build backend (maturin / setuptools-rust pyproject / setuptools-rust setup.py) and Python version floor
- Free-threading intent (Y/N + the signals it was inferred from)
- File classification: hand-written `.rs` vs `build.rs`-emitted vs vendored crates (under `vendor/` or a workspace)
- Project-specific idioms downstream agents should treat as ACCEPTABLE noise (crate-internal type aliases, helper `macro_rules!`, the crate's canonical FFI-RAII wrapper pattern) — the **highest-value** output
- AST-script-finding triage hints (when multiple scanners flag the same site, the mapper proposes a unified classification)
- Reference templates: if the project has a "correct" pattern for a recurring shape (RAII over an FFI handle, a specific `#[pyclass]` shape), document it so downstream agents can compare candidates against it

The mapper sets expectations once so downstream agents don't each re-derive the crate's conventions. (The agent file is named `rust-codegen-mapper.md` for continuity with cext; its job is PyO3 orientation, not generator detection.)

---

## 4. Scripts

### 4.1 Script Overview

15 scripts total.

| Script | Purpose | Tree-sitter? | Adapted From |
|--------|---------|--------------|--------------|
| `rust_ts_utils.py` | Shared parsing utilities | Core module | New |
| `scan_common.py` | Project root, file discovery, API tables, common args, dedup | Imports `rust_ts_utils` | cext-review-toolkit's `scan_common.py` |
| `discover_rust_ext.py` | Crate/module detection, version + feature resolution | No (TOML + `.rs` heuristics) | New |
| `scan_unsafe_blocks.py` | `unsafe` block soundness audit | Yes | New |
| `scan_pyresult.py` | `PyResult` propagation, exception clobbering | Yes | cext's `scan_error_paths.py` (rewritten for Rust) |
| `scan_gil_discipline.py` | GIL handling, `detach`/`allow_threads`, foreign callbacks, free-threading | Yes | cext's `scan_gil_usage.py` (rewritten) |
| `scan_panic_safety.py` | Panic-prone calls across FFI boundary | Yes | New |
| `scan_pyclass_traits.py` | `#[pyclass]` Send/Sync/frozen/unsendable | Yes | New |
| `scan_pyclass_protocol.py` | `#[pymethods]` slots, `__traverse__`/`__clear__`, `__richcmp__` | Yes | cext's `scan_type_slots.py` (rewritten) |
| `scan_module_init.py` | `#[pymodule]` shape, module-level state | Yes | cext's `scan_module_state.py` (rewritten) |
| `scan_lifetime_handles.py` | `Bound` vs `Py` vs `Borrowed`, legacy refs | Yes | New |
| `scan_pyo3_version_compat.py` | Deprecated/removed APIs, abi3 consistency | Yes | cext's `scan_version_compat.py` (rewritten) |
| `measure_rust_complexity.py` | Function complexity scoring | Yes | cext's `measure_c_complexity.py` |
| `analyze_history.py` | Git churn + similar-bug detection | Yes (Rust fn boundaries) | code-review-toolkit |
| `run_external_tools.py` | clippy / miri / cargo-expand / cargo-metadata integration | No | cext's `run_external_tools.py` (rewritten) |

**Dependency graph:** `rust_ts_utils.py` is at the center. `scan_common.py` imports from it. All scanners import from both. `run_external_tools.py` and `discover_rust_ext.py` import only from `scan_common`. No cycles.

**Script calling convention:** every analysis script exposes `analyze(target: str, *, max_files: int = 0) -> dict` and a `main()` that outputs JSON to stdout via shared `parse_common_args()`. Exception: `analyze_history.py` takes `argv` to match the `code-review-toolkit` convention.

### 4.2 Script Output Schema

All scripts emit JSON with a common envelope:

```json
{
  "project_root": "/path/to/crate",
  "scan_root": "/path/to/scanned/scope",
  "crate_info": {
    "crate_name": "mycrate",
    "module_name": "mymod",
    "pyo3_version": "0.22.5",
    "pyo3_features": ["extension-module", "abi3-py310"],
    "python_floor": "3.10",
    "abi3t_python_floor": null,
    "build_backend": "maturin",
    "free_threading_target": false,
    "source_files": ["src/lib.rs", "src/util.rs", ...],
    "tree_sitter_available": true,
    "clippy_available": true,
    "miri_available": false,
    "cargo_metadata_available": true
  },
  "functions_analyzed": 42,
  "findings": [
    {
      "type": "unsafe_ffi_call",
      "file": "src/buffer.rs",
      "line": 142,
      "column": 9,
      "function": "MyBuffer::__getbuffer__",
      "category": "soundness",
      "classification": "CONSIDER",
      "confidence": "HIGH",
      "description": "...",
      "fix_template": "...",
      "details": { ... }
    }
  ],
  "summary": {
    "by_type": { "unsafe_ffi_call": 3, ... },
    "by_classification": { "FIX": 1, "CONSIDER": 4, ... }
  }
}
```

### 4.3 Dependency Check

Every script begins with:

```python
try:
    import tree_sitter
    import tree_sitter_rust
except ImportError:
    print(json.dumps({
        "error": "tree-sitter not installed",
        "install": "pip install tree-sitter tree-sitter-rust",
    }))
    sys.exit(1)
```

---

## 5. Commands

4 commands, mirroring cext-review-toolkit but adapted for Rust priorities.

### 5.1 explore

```
/rust-ext-review-toolkit:explore [scope] [aspects] [options]
```

**Aspects:** `unsafe`, `pyresult`, `gil`, `panic`, `pyclass-traits`, `pyclass-protocol`, `module-init`, `lifetimes`, `compat`, `complexity`, `history`, `parity`, `all`

**Phases:**

| Phase | Agents | Purpose |
|-------|--------|---------|
| **0** | discover_rust_ext | Crate layout, PyO3 version, features, source files |
| **0.5** | run_external_tools | Opportunistic clippy / cargo-metadata baseline |
| **1** | rust-codegen-mapper | Orientation: file classification, project idioms, triage hints |
| **2A** | unsafe-block-auditor, pyresult-propagation-checker | Safety-critical (highest value) |
| **2B** | gil-discipline-checker, panic-safety-checker | Memory and panic safety |
| **2C** | pyclass-traits-checker, pyclass-protocol-checker, module-init-checker | PyO3-specific correctness |
| **2D** | lifetime-handle-checker, pyo3-version-compat-scanner | Compatibility and migration |
| **2E** | rust-complexity-analyzer | Code quality |
| **2F** | parity-checker (when applicable) | Cross-implementation behavior |
| **2G** | git-history-analyzer | Churn + similar-bug prioritization |
| **3** | (synthesis) | Deduplicate, resolve conflicts, produce unified summary |

If `parallel` is specified, run agents within each group concurrently (max 2 by default). Groups still execute sequentially.

### 5.2 health

```
/rust-ext-review-toolkit:health [scope]
```

Quick dashboard — all agents in summary mode.

| Dimension | Agent |
|-----------|-------|
| Unsafe Soundness | unsafe-block-auditor |
| Error Propagation | pyresult-propagation-checker |
| GIL Discipline | gil-discipline-checker |
| Panic Safety | panic-safety-checker |
| Pyclass Traits | pyclass-traits-checker |
| Pyclass Protocol | pyclass-protocol-checker |
| Module Init | module-init-checker |
| Lifetime Handles | lifetime-handle-checker |
| PyO3 Version Compat | pyo3-version-compat-scanner |
| Complexity | rust-complexity-analyzer |
| Rust/Python Parity | parity-checker (when applicable) |

Each scored 1–10. Score deductions follow the cext-review-toolkit rubric.

### 5.3 hotspots

```
/rust-ext-review-toolkit:hotspots [scope]
```

Runs unsafe-block-auditor, pyresult-propagation-checker, panic-safety-checker, and rust-complexity-analyzer. Answers "where should I focus?"

The agent mix differs from cext-review-toolkit's hotspots (refcount + errors + complexity) because Rust's safety-critical surface is concentrated in `unsafe` blocks and panic-prone call paths, not in refcount management.

### 5.4 migrate

```
/rust-ext-review-toolkit:migrate [scope]
```

**This is the highest-value command for Rust extensions** because PyO3 has reshuffled its API repeatedly (0.21 `Bound` migration, 0.23 `IntoPyObject`, 0.25 `attach`/`detach` rename, 0.27 `FromPyObject` rework, 0.28 free-threading default) and the free-threading rollout is ongoing.

Runs lifetime-handle-checker, pyo3-version-compat-scanner, pyclass-traits-checker, and module-init-checker. Produces a migration checklist:

```markdown
# Rust Extension Migration Report

## Current State
- PyO3 version: 0.21.2
- Active features: extension-module, abi3-py39
- Build backend: maturin
- Free-threading target: no

## Migration Checklist

### Phase 1: Bound API migration (PyO3 0.21+)
- [ ] Replace 14 `&PyAny` parameters with `&Bound<'py, PyAny>`
- [ ] Replace 8 `Py<T>::as_ref(py)` calls with `.bind(py)`
- [ ] Replace 3 `.clone()` on `Py<T>` with `.clone_ref(py)`

### Phase 2: PyO3 0.22+ readiness
- [ ] 5 deprecated APIs to replace (see scan output)
- [ ] Review 2 `unsafe impl Send` declarations against new trait-bound requirements

### Phase 3: PyO3 0.23+ readiness (when ready)
- [ ] 1 removed API to migrate
- [ ] Verify abi3 feature still works under stricter restrictions

### Phase 4: Free-threading readiness (optional, for 3.13t/3.14t)
- [ ] Audit 6 `#[pyclass]` types for shared mutable state
- [ ] Add `frozen` to 4 candidates; refactor 2 to use interior locking
- [ ] Declare module thread-safety: `#[pymodule(gil_used = false)]` (or leave the 0.28+ default), or `gil_used = true` if not yet FT-audited
- [ ] Build/test against a free-threaded interpreter (`python3.13t`/`3.14t`)

### Phase 5: abi3 floor bump (optional)
- [ ] Currently `abi3-py39`; bumping to `abi3-py310` unlocks `PyDict_GetItemRef`, ...
```

---

## 6. Classification System

Same four tags as cext-review-toolkit, with extension-specific calibration:

| Tag | Rust/PyO3 Context | Example |
|-----|-------------------|---------|
| **FIX** | Soundness bug, panic across FFI in a normally-reached path, removed-API use that breaks compile, error swallowing on a path that surfaces incorrect data | `transmute` between handle types; `.unwrap()` on `PyResult` in `#[pymethods]` body; non-`Send` field without `unsendable` |
| **CONSIDER** | Likely improvement; PyO3 idiom migration; FT readiness gap; abi3 inconsistency | Legacy `&PyAny` in 0.22 code; missing `frozen` on FT-target crate; `Py<T>` parameter that should be `Bound<'py, T>` |
| **POLICY** | Design decision | Whether to adopt abi3; whether to target free-threading; whether to support subinterpreters; `unsendable` for performance |
| **ACCEPTABLE** | Noted but no action needed | `unsafe` block with verified `// SAFETY:` comment; intentional global state in a singleton module; `.unwrap()` on a value proven non-`None` in scope |

**Calibration rules:**

- **Every `unsafe` block** defaults to CONSIDER even if no specific bug is found. The block is a soundness obligation; the agent's job is to either escalate to FIX (specific bug identified) or downgrade to ACCEPTABLE (verified-safe pattern with `// SAFETY:` justification).
- **`.unwrap()` in `#[pymethods]`** is FIX unless the agent can prove (from in-function context) that the value cannot be `None`/`Err`.
- **Free-threading and subinterpreter compatibility** are CONSIDER (not FIX) by default — they're roadmap items, not bugs in the current execution model.
- **`#[pyclass(unsendable)]`** is ACCEPTABLE when used deliberately; FIX only when it should have been Send but the field types prevent it.

---

## 7. Implementation Plan

### Phase 1: Foundation (Week 1)

- [ ] Project scaffolding: plugin.json, marketplace.json, README, LICENSE, CHANGELOG
- [ ] `rust_ts_utils.py` — shared parsing module
- [ ] `scan_common.py` — adapted from cext-review-toolkit
- [ ] `discover_rust_ext.py` — Cargo.toml + pyproject.toml + maturin + setuptools-rust (pyproject + setup.py) discovery
- [ ] `data/pyo3_api_tables.json` (seeded from PyO3 0.20–0.29 release notes)
- [ ] `data/non_send_sync_types.json`, `data/panic_unwind_apis.json`
- [ ] Vendor `data/stable_abi.json` and `data/limited_api_headers.json` from cext-review-toolkit
- [ ] Test helper: `TempRustExtension` context manager, `import_script` loader
- [ ] Fixture strings: `MINIMAL_PYO3_CRATE`, `PYO3_CRATE_WITH_PYCLASS`, `PYO3_CRATE_WITH_UNSAFE`, `PYO3_CRATE_WITH_BUGS`
- [ ] Tests for `rust_ts_utils` and `discover_rust_ext`

### Phase 2: Safety-critical agents (Week 2)

- [ ] `scan_unsafe_blocks.py` (Tree-sitter-based)
- [ ] `scan_pyresult.py` (including exception-clobbering detection)
- [ ] `unsafe-block-auditor` agent prompt
- [ ] `pyresult-propagation-checker` agent prompt
- [ ] Tests

### Phase 3: GIL and panic safety (Week 3)

- [ ] `scan_gil_discipline.py` (including foreign-callback and finalization-safety patterns)
- [ ] `scan_panic_safety.py`
- [ ] `gil-discipline-checker` agent prompt
- [ ] `panic-safety-checker` agent prompt
- [ ] Tests

### Phase 4: PyO3-specific agents (Week 4)

- [ ] `scan_pyclass_traits.py`
- [ ] `scan_pyclass_protocol.py` (including `__richcmp__` semantics)
- [ ] `scan_module_init.py`
- [ ] Three corresponding agent prompts
- [ ] Tests

### Phase 5: Compatibility, lifetimes, quality (Week 5)

- [ ] `scan_lifetime_handles.py`
- [ ] `scan_pyo3_version_compat.py`
- [ ] `measure_rust_complexity.py`
- [ ] Three corresponding agent prompts
- [ ] `parity-checker` agent prompt (qualitative, no script)
- [ ] Tests

### Phase 6: History, preflight, commands (Week 6)

- [ ] `analyze_history.py` (adapted)
- [ ] `git-history-analyzer` agent prompt
- [ ] `rust-codegen-mapper` agent prompt (preflight)
- [ ] All 4 command definitions: `explore`, `health`, `hotspots`, `migrate`
- [ ] `run_external_tools.py` (clippy / miri / cargo-expand / cargo-metadata)
- [ ] End-to-end test on a real PyO3 crate
- [ ] README, plugin README, marketplace listing

### Phase 7: Real-world validation (Week 7)

Run on at least 3 real PyO3 crates, ideally with diverse profiles:
- A small, well-maintained crate (sanity check + bug-yield baseline)
- A large, idiomatic crate on a recent PyO3 version (false-positive rate; candidates include `pydantic-core`, `cryptography`, `tokenizers`)
- A crate mid-migration (0.20 → 0.21/0.22 — exercise lifetime-handle-checker)

Iterate on classifier calibration and false-positive suppression based on findings.

---

## 8. Plugin Structure

```
rust-ext-review-toolkit/
├── .claude-plugin/
│   ├── plugin.json
│   └── marketplace.json
├── README.md
├── LICENSE
├── CHANGELOG.md
├── CLAUDE.md
├── rust-ext-review-toolkit-design.md      # This document
├── plugins/
│   └── rust-ext-review-toolkit/
│       ├── .claude-plugin/plugin.json
│       ├── README.md
│       ├── agents/
│       │   ├── rust-codegen-mapper.md           # preflight
│       │   ├── unsafe-block-auditor.md
│       │   ├── pyresult-propagation-checker.md
│       │   ├── gil-discipline-checker.md
│       │   ├── panic-safety-checker.md
│       │   ├── pyclass-traits-checker.md
│       │   ├── pyclass-protocol-checker.md
│       │   ├── module-init-checker.md
│       │   ├── lifetime-handle-checker.md
│       │   ├── pyo3-version-compat-scanner.md
│       │   ├── rust-complexity-analyzer.md
│       │   ├── git-history-analyzer.md
│       │   └── parity-checker.md
│       ├── commands/
│       │   ├── explore.md
│       │   ├── health.md
│       │   ├── hotspots.md
│       │   └── migrate.md
│       ├── scripts/
│       │   ├── rust_ts_utils.py
│       │   ├── scan_common.py
│       │   ├── discover_rust_ext.py
│       │   ├── scan_unsafe_blocks.py
│       │   ├── scan_pyresult.py
│       │   ├── scan_gil_discipline.py
│       │   ├── scan_panic_safety.py
│       │   ├── scan_pyclass_traits.py
│       │   ├── scan_pyclass_protocol.py
│       │   ├── scan_module_init.py
│       │   ├── scan_lifetime_handles.py
│       │   ├── scan_pyo3_version_compat.py
│       │   ├── measure_rust_complexity.py
│       │   ├── analyze_history.py
│       │   └── run_external_tools.py
│       └── data/
│           ├── pyo3_api_tables.json
│           ├── pyo3_version_matrix.json
│           ├── abi3_compatible_apis.json
│           ├── non_send_sync_types.json
│           ├── panic_unwind_apis.json
│           ├── pyo3_idioms.json
│           ├── stable_abi.json              # vendored from cext-review-toolkit
│           └── limited_api_headers.json     # vendored from cext-review-toolkit
├── tests/
│   ├── helpers.py
│   ├── test_rust_ts_utils.py
│   ├── test_discover_rust_ext.py
│   ├── test_scan_unsafe_blocks.py
│   ├── test_scan_pyresult.py
│   ├── test_scan_gil_discipline.py
│   ├── test_scan_panic_safety.py
│   ├── test_scan_pyclass_traits.py
│   ├── test_scan_pyclass_protocol.py
│   ├── test_scan_module_init.py
│   ├── test_scan_lifetime_handles.py
│   ├── test_scan_pyo3_version_compat.py
│   ├── test_measure_rust_complexity.py
│   └── test_analyze_history.py
└── .gitignore
```

---

## 9. Testing Strategy

### 9.1 Test Helper

Adapted from `TempExtension`:

```python
class TempRustExtension:
    """Create a temporary PyO3 crate for testing."""

    def __init__(
        self,
        files: dict[str, str],
        *,
        cargo_toml: str | None = None,
        pyproject_toml: str | None = None,
        setup_py: str | None = None,
        pyo3_version: str = "0.22",
        pyo3_features: list[str] | None = None,
    ):
        # Creates temp dir with given .rs files
        # Generates Cargo.toml with pyo3 dep + cdylib crate-type
        # Optionally creates pyproject.toml (maturin or setuptools-rust)
        # Optionally creates setup.py with setuptools_rust.RustExtension
        ...
```

### 9.2 Test Coverage Requirements

Each scanner must have tests covering:
- Detection of the primary bug pattern (true positive)
- Clean code producing no findings (true negative)
- At least one edge case (heavy attribute use, generics, macro-rich code)
- For version-aware scanners: behavior across the inflection versions (0.21, 0.25, 0.28, 0.29)

### 9.3 Real-world Validation

Before each release, run on at least 3 real PyO3 crates spanning small/medium/large surface and varying PyO3 versions. Track false-positive rate, missed-bug rate, and runtime per scan.

---

## 10. Open Questions and Future Work

*Deferred for after v0.1 ships. Listed here so contributors can see what's intentionally out of scope.*

1. **Procedural-macro expansion.** Should `run_external_tools.py` opportunistically run `cargo expand` and feed expanded output to scanners that need post-expansion visibility (e.g. for verifying `#[pyclass]` actually wires the slot the user thinks it does)? Cost: long runtime, requires nightly. Benefit: closes a real visibility gap.
2. **`miri` integration depth.** Miri can detect real UB but only in test code paths. Worth integrating as a quality signal? Probably yes but as a separate optional phase, not the primary analysis.
3. **Trait-bound satisfaction.** Some checks (e.g. "does `MyType` actually satisfy `Send + Sync`?") need compiler-level knowledge. Approach: encode known-non-`Send`/non-`Sync` patterns in `data/non_send_sync_types.json` and accept that we'll miss generic-parameter cases.
4. **Async PyO3 (`pyo3-async-runtimes`).** Tokio/async-std bridging has its own bug surface (Send across awaits, GIL held across await points, runtime-pinning issues). Plausible future agent: `async-pyo3-checker`. Out of scope for v0.1.
5. **`numpy` / `pyo3-numpy`.** Array-protocol-specific patterns (lifetime of buffers, copy semantics, dtype dispatching). Plausible future playbook.
6. **Cross-language parity tooling.** When a project has both Rust and Python implementations, the parity-checker is qualitative. Could a script detect signature parity (matching Python function names + arg counts)? Worth prototyping.
7. **PyO3 version detection without `Cargo.lock`.** `Cargo.toml` typically specifies a version range. Resolving to the exact installed version requires `cargo metadata` (network may be needed). Document this clearly in discovery output and degrade gracefully.

---

## 11. Differences from cext-review-toolkit (Summary)

| Dimension | cext-review-toolkit | rust-ext-review-toolkit |
|-----------|--------------------|-------------------------|
| Target | C extensions consuming `Python.h` | Rust extensions consuming `pyo3` |
| Parsing | Tree-sitter-c (+ optional tree-sitter-cpp) | Tree-sitter-rust |
| Dependencies | `tree-sitter`, `tree-sitter-c` | `tree-sitter`, `tree-sitter-rust` |
| Project discovery | `setup.py`, `pyproject.toml` (setuptools/meson-python/scikit-build), `meson.build`, `CMakeLists.txt`, `#include <Python.h>` fallback | `Cargo.toml` + `pyproject.toml` (maturin/setuptools-rust), `setup.py` with `setuptools_rust.RustExtension`, `use pyo3::` fallback |
| Perspective | Code that *consumes* the C API directly | Code that *consumes* `pyo3` (which wraps the C API) |
| Crown jewel finding | Borrowed-ref-across-callback | Unsafe-block soundness violation; `Bound`/`Py` held across `detach` |
| Agent count | 13 (+ preflight) | 13 (+ preflight) |
| Unique-to-this-toolkit agents | parity-checker (kept) | unsafe-block-auditor, pyclass-traits-checker, lifetime-handle-checker, panic-safety-checker |
| Dropped agents | — | refcount-auditor (Drop handles it), null-safety-scanner (type system), pyerr-clear-auditor (folded into pyresult), resource-lifecycle-checker (Drop), stable-abi-checker (folded into pyo3-version-compat) |
| Refcount-in-regular-code | Major concern, dedicated scanner | Non-concern (handled by `Py<T>`/`Bound` Drop); the C-style refcount checks resurface inside `unsafe { pyo3-ffi::… }` and are handled by unsafe-block-auditor |
| Static module state | Single-phase init vs multi-phase | `static OnceLock<Py<…>>` vs module state; `#[pymodule]` opt-in |
| Type definitions | `PyTypeObject` / `PyType_Spec` parsing | `#[pyclass]` attribute parsing; macro-expanded code is opaque (Tree-sitter limitation) |
| ABI compliance | Dedicated agent, internal-struct-access detection | Folded into `pyo3-version-compat-scanner`; PyO3 enforces most abi3 rules at compile time |
| External tools | clang-tidy, cppcheck | clippy, miri, cargo-geiger, cargo-expand, cargo-metadata |
| Unique command | migrate (multi-phase, stable ABI, version compat) | migrate (Bound API migration, FT readiness, abi3 floor) — significantly more central given PyO3's version cadence |
| Data files shared | — | `stable_abi.json` and `limited_api_headers.json` vendored directly from cext-review-toolkit |
| Generator-mapper preflight | Cython / pybind11 / nanobind / Argument Clinic (multiple generators, selectable playbooks) | Single generator (PyO3); the mapper instead captures version + features + project-specific idioms (no playbook indirection) |

---

## 12. Sibling Synchronization

When PyO3 ships a major release, the maintenance work is concentrated in:
- `data/pyo3_api_tables.json` — new/deprecated/removed APIs
- `data/pyo3_idioms.json` — new idiomatic patterns
- `data/abi3_compatible_apis.json` — abi3 surface area changes
- Tests covering the new version

The analysis logic itself should not need changes for routine PyO3 updates — that's the whole point of the data-driven design (mirroring cext-review-toolkit's approach of keeping API tables as data, logic stable).

Cross-toolkit synchronization with cext-review-toolkit: the JSON envelope, classification system, finding shape, test infrastructure conventions (`unittest`, `TempExtension` pattern), and CLI argument conventions should remain in lock-step. Helps Claude Code consume both toolkits consistently and reduces cognitive load for contributors who work on both. `data/stable_abi.json` and `data/limited_api_headers.json` are vendored from cext-review-toolkit and should be re-vendored whenever cext-review-toolkit updates them.
