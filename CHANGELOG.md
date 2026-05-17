# Changelog

All notable changes to rust-ext-review-toolkit are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

The toolkit is pre-v0.1 and under active construction from
`rust-ext-review-toolkit-design.md` and `IMPLEMENTATION_ROADMAP.md`.

### Added
- Design document set, reconciled to current PyO3 (0.28 stable, 0.29 in
  development): `rust-ext-review-toolkit-design.md`, `PYO3_PRIMER.md`,
  `IMPLEMENTATION_ROADMAP.md`, `TREE_SITTER_RUST_RECIPES.md`.
- Top-level documentation (`README.md`, `CLAUDE.md`) and plugin metadata
  rewritten for the PyO3 focus and the 13-agent design.
- Foundation code (roadmap phase 1): `rust_ts_utils.py` (the tree-sitter-rust
  parsing core), `scan_common.py` (project root, file discovery, finding
  shape, deduplication, comment-aware suppression, the shared report
  envelope), and `discover_rust_ext.py` (PyO3 crate detection — build backend,
  version, features, free-threading intent). Seed data files
  (`pyo3_api_tables.json`, `non_send_sync_types.json`, `panic_unwind_apis.json`)
  and a `unittest` suite with the `TempRustExtension` helper and fixture
  crates.
- `scan_unsafe_blocks.py` + the `unsafe-block-auditor` agent: `unsafe`-block
  soundness audit — raw `pyo3-ffi` calls, handle-type `transmute`, raw
  `ob_refcnt` manipulation, private `_Py*` symbols, and `unsafe impl
  Send`/`Sync` bounds.
- `scan_pyresult.py` + the `pyresult-propagation-checker` agent: `PyResult`
  propagation audit — `.unwrap()`/`.expect()` panics across the FFI boundary,
  swallowed errors (`.ok()`, `let _`, dropped statements), exception
  clobbering, and manual `PyErr` state handling.
- `scan_gil_discipline.py` + the `gil-discipline-checker` agent: GIL-discipline
  audit — Python work inside `detach`/`allow_threads` closures, nested
  `attach`, `extern "C"` callbacks that skip `Python::attach`,
  `attach_unchecked`, and a version-aware `gil_used` declaration check for
  free-threading. Recognises both the current and pre-0.25 GIL vocabularies.
- `scan_panic_safety.py` + the `panic-safety-checker` agent: panic-safety
  audit — `.unwrap()`/`.expect()` on non-`PyResult` values, slice indexing,
  panic macros, and panicking `Vec` methods reachable from
  `#[pyfunction]`/`#[pymethods]` bodies and their one-hop callees, with panics
  inside `extern "C"` callbacks escalated to FIX (process-abort risk).
- `scan_pyclass_traits.py` + the `pyclass-traits-checker` agent: `#[pyclass]`
  trait-bound audit — non-`Send`/`Sync` fields without `unsendable`, the
  `unsendable` opt-out, missing `frozen` on free-threading-targeted crates,
  and `extends` + `Drop` pitfalls (version-aware: `Send + Sync` since 0.23).
- `scan_pyclass_protocol.py` + the `pyclass-protocol-checker` agent:
  `#[pymethods]` protocol audit — missing `__traverse__`/`__clear__` on
  cycle-capable classes, `#[getter]`s taking arguments, incomplete
  `__richcmp__` `CompareOp` matches, `__hash__` return types, and
  `#[new]`/`__init__` collisions.
- `scan_module_init.py` + the `module-init-checker` agent: `#[pymodule]`
  initialization audit — process-global `Py<_>` statics, registrar calls
  (`add_class`/`add`/...) whose `PyResult` is dropped, and `#[pymodule]`
  functions not returning `PyResult<()>`.
- `scan_lifetime_handles.py` + the `lifetime-handle-checker` agent: PyO3
  handle-kind audit — legacy `&PyAny`/`&PyDict` GIL-reference types,
  `Py::as_ref`, `Py<T>` parameters that should be `&Bound<'py, T>`, and
  `Borrowed` handles; severity is keyed to the resolved PyO3 version.
- `scan_pyo3_version_compat.py` + the `pyo3-version-compat-scanner` agent:
  version-compatibility audit — removed/deprecated/renamed APIs classified
  against the crate's PyO3 version (table-driven from `pyo3_api_tables.json`),
  and the `abi3` + free-threading wheel conflict.
- `measure_rust_complexity.py` + the `rust-complexity-analyzer` agent:
  per-function complexity scoring (LOC, cyclomatic, nesting, parameters, and
  an `unsafe`-block weighting), ranked hotspots, and a distribution summary.
- The `parity-checker` agent (qualitative, no script): finds Python-visible
  behavioral differences between a Rust extension and its pure-Python
  fallback when a crate ships both.
- `data/pyo3_api_tables.json` gained a machine-readable `detect` rule per
  entry (schema version 2) so the version scanner is fully table-driven.
- `analyze_history.py` + the `git-history-analyzer` agent: git-history
  temporal analysis — churn metrics, Rust/PyO3-tuned commit classification
  (safety / panic / concurrency / migration / bugfix / ...), and fix-commit
  diffs for similar-bug detection. Co-change analysis is dropped as noisy at
  Rust-extension scale.
- `run_external_tools.py`: opportunistic external-tool baseline — runs
  `cargo metadata` (no compilation) to confirm the resolved PyO3 version;
  `cargo clippy` is opt-in via `--run-clippy` (it executes build scripts);
  miri / cargo-geiger / cargo-expand are detection-only.
- The `rust-codegen-mapper` preflight agent (qualitative, no script) and
  `data/pyo3_idioms.json` it consumes: a project-orientation guide —
  PyO3 version/features, file classification, and the crate's
  ACCEPTABLE-noise idioms — that every downstream agent reads first.
- The four orchestration commands: `explore` (phased full review),
  `health` (scored dashboard), `hotspots` (worst functions), and `migrate`
  (phased PyO3 migration checklist).
- `WORKING_WITH_MAINTAINERS.md` — the maintainer-engagement social contract,
  adapted from cext-review-toolkit.
- `docs/real-world-validation.md` — the Phase 7 validation report: the
  scanners run against pydantic-core, cryptography, rust-numpy, tokenizers,
  and polars-python, with the finding matrix and per-scanner assessment.

### Changed
- Phase 7 real-world calibration against five PyO3 crates fixed one
  performance defect and five false-positive classes:
  - scan_pyresult / scan_panic_safety compute comment-suppression lazily
    (once per finding, not once per call) — a ~6x speedup on large crates.
  - `missing_traverse` / `missing_clear` are now CONSIDER, not FIX — the
    scanner cannot prove a reference cycle is reachable; the agent escalates.
  - `getter_with_args` recognises by-value receivers (`self_: PyRef<Self>`,
    `this: Bound<'_, Self>`) as the receiver, not a data argument.
  - The PyResult-shape heuristic dropped the over-broad `.len()`, `.downcast`,
    and `.cast` hints (they collide with `Vec::len`, `Any::downcast_ref`,
    `Series::cast`).
  - `data/pyo3_api_tables.json`: `_bound`-suffix auto-detection removed — the
    suffix collides with the current `Py::into_bound` and stdlib
    `lower_bound`/`upper_bound`.

### Removed
- The initial repository was a find-and-replace copy of cext-review-toolkit.
  All C-extension agents, scripts, tests, and C-side data files have been
  removed to make room for the PyO3-specific implementation. The CPython
  stable-ABI / limited-API data files are kept (vendored, used for raw
  `pyo3-ffi` audits under the `abi3` feature).
