# Changelog

All notable changes to rust-ext-review-toolkit are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- `scan_pyclass_protocol`: `#[pyclass]` on `enum` is now audited (v0.1 only matched
  `struct_item` targets, so cryptography's `declarative_asn1::Type` enum was silently
  skipped). `rust_ts_utils.extract_enum_defs` mirrors `extract_struct_defs` and the
  scanner's GC-slot check uses the flat union of variant fields.
- `scan_common.deduplicate_findings`: include `function` in the dedup key so that the
  same finding type on two distinct targets in one file no longer collapses into one
  (the description normaliser strips identifier text, which would otherwise key-equal
  them). Restored the per-target distinction lost in v0.1.
- `discover_rust_ext`: walk up to the workspace root to resolve `Cargo.lock` when the
  scanned crate has none of its own (cryptography's `src/rust/` scan returned
  `pyo3_version: null` because of this). Discovery now records whether the lock came
  from the crate root or a workspace ancestor.
- `discover_rust_ext`: recognise workspace-shim cdylib layouts. A crate with `pyo3`
  and `#[pymodule]` but no cdylib is no longer mechanically `out_of_scope: true` --
  if a workspace sibling re-exports its `c_api::*` as a cdylib (the polars layout),
  detection method `workspace_shim_cdylib` keeps it in scope. The narrow "Rust
  binary embedding Python" classification still applies when `#[pymodule]` is absent.
- `scan_pyclass_traits`: detect `#[pyclass]`es mutated via `&mut <T>` parameters of
  a `#[pyfunction]` (cryptography's `LoadedProviders` was missed this way -- no
  `#[pymethods]` impl at all, but a free function mutates the type). The
  `missing_frozen_ft` check fires when either a `&mut self` method OR a free-fn
  mutator exists.
- `analyze_history`: detect shallow git clones (`git rev-parse --is-shallow-repository`)
  and surface `is_shallow_clone: true` plus an explicit `notes` entry in the envelope.
  v0.1 silently emitted misleading "1 commit" file ages and empty `function_churn`
  against truncated history; v0.2 tells the reviewer to run `git fetch --unshallow`
  before trusting the analysis.
- `scan_lifetime_handles`: suppress `unjustified_borrowed` findings inside `impl
  FromPyObject for ...` blocks. PyO3 0.28's `FromPyObject::extract_bound` mandates
  `Borrowed<'a, 'py, PyAny>` as the parameter type -- the trait signature is fixed,
  not a developer choice. Polars hit 61/61 false positives from this single shape.
- `scan_gil_discipline`: `_has_outer_gil` recognises `*::spawn(...)`,
  `*::spawn_blocking(...)`, `*::spawn_handler(...)`, and polars-style
  `enter_polars(...)` wrappers as GIL-releasing boundaries. A `Python::attach`
  inside such a closure is a valid re-attach on a worker thread, not a nested
  attach. Polars hit 2/3 false positives in this category.
- `scan_module_init`: recognise `PyOnceLock` (PyO3's GIL-aware once-cell) as a
  Python once-cell wrapper. Polars's `static NDARRAY_TYPE: PyOnceLock<Py<PyType>>`
  and similar statics outside `py_modules.rs` were missed because the cell-name
  list only included `std::sync::OnceLock`-family names.
- `scan_unsafe_blocks`: require the handle marker (`Bound`, `Borrowed`, `Py<`,
  `Py[A-Z]`) in the transmute CALL TEXT (turbofish or args) -- the enclosing fn
  signature is no longer used as a fallback signal. Cryptography's
  `pkcs7.rs::try_list_of_certificates` bare `mem::transmute(val)` (a pure-Rust
  lifetime relabel) was false-positived as `unsafe_handle_transmute` because the
  enclosing fn had `Py<PyBytes>` in scope. Bare transmutes without explicit
  turbofish now classify as `unsafe_block` instead.
- `scan_pyo3_version_compat`: support a `receiver` field on `call` detect rules so
  `Py::from_owned_ptr` (deprecated in 0.28) is distinguished from
  `Bound::from_owned_ptr` (its replacement). v0.1 matched the leaf method name only
  and false-positived every `Bound::from_owned_ptr` call.

### Added
- `rust_ts_utils.extract_enum_defs` -- enum extraction mirroring `extract_struct_defs`,
  returning variants (with `kind`, `fields`, line info) and a flat `all_fields` view
  for cycle-risk checks across all variants.
- `discover_rust_ext._resolve_workspace_root` -- walk up to the nearest ancestor
  `Cargo.toml` declaring `[workspace]`.
- `discover_rust_ext._find_shim_cdylib_sibling` -- recognise polars-style downstream
  shim cdylib crates that re-export an upstream crate's `c_api`.
- `analyze_history._is_shallow_repo` -- shallow-clone detection via
  `git rev-parse --is-shallow-repository`.
- `scan_gil_discipline._GIL_RELEASING_BOUNDARIES` -- the set of callee names that
  cut the outer-GIL inheritance chain (spawn / spawn_blocking / spawn_handler /
  enter_polars / enter_polars_df / enter_polars_ok / enter_polars_series).
- `scan_pyo3_version_compat._call_qualifier` and `_call_entry_for` -- pick the
  matching API entry for a call by the qualifier preceding the leaf method.

### Documentation
- `docs/writing-maintainer-facing-reports.md` -- methodology guide for converting
  toolkit output into maintainer-facing reports. Codifies patterns from the
  cryptography v0 and polars v1 reviews: pre-flight protocol, scope-citation,
  channel-per-item, reproducer-driven re-triage, the separate `<crate>_BEYOND_SCOPE.md`
  companion, and per-extension lessons. Worked examples in `reports/cryptography-rust_v1/`
  (v0) and `reports/polars_v1/` (v1).

## [0.1.0] - 2026-05-17

The first release: the PyO3 review toolkit, built from
`rust-ext-review-toolkit-design.md` and validated against five real PyO3
extensions.

### Added
- The design spec `rust-ext-review-toolkit-design.md`, reconciled to current
  PyO3 (0.28 stable, 0.29 in development). The build-time companion docs (a
  PyO3 domain primer, an implementation roadmap, and a tree-sitter-rust recipe
  reference) guided the implementation and remain in git history.
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
