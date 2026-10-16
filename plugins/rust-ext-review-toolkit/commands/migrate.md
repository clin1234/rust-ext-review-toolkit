---
description: "Produce a PyO3 migration checklist for a Python extension written in Rust. Use when the user wants to upgrade their PyO3 version, adopt the Bound API, prepare for free-threading, or change the abi3 floor. The highest-value command given PyO3's frequent API churn."
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# PyO3 Migration Checklist

Produce an actionable, phased migration checklist for a Rust/PyO3 extension. **This is the highest-value command for Rust extensions** — PyO3 has reshuffled its API repeatedly (0.21 `Bound`, 0.23 `IntoPyObject`, 0.25 `attach`/`detach` rename, 0.27 conversion rework, 0.28 free-threading default) and the free-threading rollout is ongoing.

**Arguments:** "$ARGUMENTS"  (scope only — path or glob; default `.`)

**Plugin root:** `<plugin_root>` is the `plugins/rust-ext-review-toolkit/` directory.

## Workflow

1. **Discovery** — `python <plugin_root>/scripts/discover_rust_ext.py [scope]`. Capture the PyO3 version, features, build backend, Python floor, and free-threading signals. Stop if not a PyO3 extension.
2. **Preflight** — dispatch rust-codegen-mapper.
3. **Dispatch** the four migration agents:
   - **lifetime-handle-checker** — legacy `&PyAny` GIL-refs, `Py::as_ref`, non-idiomatic handles
   - **pyo3-version-compat-scanner** — removed / deprecated / renamed APIs at the crate's version
   - **pyclass-traits-checker** — `Send + Sync` bound gaps, missing `frozen` for free-threading
   - **module-init-checker** — `#[pymodule]` shape, process-global state
4. **Assemble the checklist** from their findings, grouped into migration phases.

## Output — Migration Report

```markdown
# Rust Extension Migration Report — [crate]

## Current State
- PyO3 version: [version]   (or "unresolved — path/workspace dependency")
- Active features: [list]
- Build backend: [maturin / setuptools-rust / cargo]
- Python floor: [3.N or none]
- Free-threading target: [yes/no — and the signals]

## Migration Checklist

### Phase 1: Bound API migration (PyO3 0.21+)
- [ ] Replace N `&PyAny`/`&PyDict`/... GIL references with `&Bound<'py, T>`  (lifetime-handle-checker `legacy_ref_type`)
- [ ] Replace N `Py::as_ref(py)` calls with `.bind(py)`  (`legacy_as_ref`)
- [ ] Convert N `Py<T>` parameters that are only `.bind()`-ed to `&Bound<'py, T>`  (`py_param_should_be_bound`)

### Phase 2: Renamed / deprecated APIs
- [ ] N renamed APIs to migrate (`with_gil`→`attach`, `downcast`→`cast`, ...)  (version-compat `renamed_api`)
- [ ] N deprecated APIs to replace  (`deprecated_api`)

### Phase 3: Removed APIs (blocking on the target version)
- [ ] N removed APIs that will not compile  (version-compat `removed_api`)
- [ ] Review the `Send + Sync` requirement: N `#[pyclass]` types with non-Send/Sync fields  (pyclass-traits `non_send_field`)

### Phase 4: Free-threading readiness (optional, for 3.13t / 3.14t, 3.15t)
- [ ] N `#[pyclass]` types with mutable shared state lack `frozen`  (pyclass-traits `missing_frozen_ft`)
- [ ] Add `frozen` + interior locking / atomics to the candidates above
- [ ] Declare the module's stance: `#[pymodule(gil_used = true)]` until the crate is free-threading-audited, or rely on the 0.28+ `gil_used = false` default once it is
- [ ] Build and test against a free-threaded interpreter (`python3.13t` / `python3.14t`, `python3.15t`)
- [ ] For a full GIL-discipline audit, run `/explore <scope> gil`

### Phase 5: Stable ABI targets and floors (optional)
- [ ] Record traditional `abi3-pyN` and `abi3t-pyN` floors separately (`abi3t_python_floor`), or "host-dependent" for bare features. A floor is not the actual build interpreter version.
- [ ] For Python 3.15+, consider `abi3t` / `abi3t-py315` with PyO3 0.29+ and compatible packaging tools. Traditional `abi3` alone still does not cover free-threaded builds.
- [ ] If supporting older Python versions, review the dual `abi3` + `abi3t` build matrix. Keep version-specific wheels for 3.13t/3.14t; bare `abi3t` is not a stable-ABI target on those interpreters.
- [ ] Verify build configuration, wheel tags, and tests on both GIL-enabled and free-threaded Python 3.15+. ABI compatibility does not establish thread safety.
- [ ] If raising the floor unlocks newer C-API, list what (version-compat / agent notes)
```

Only include a phase if it has items. Order the checklist so the **blocking** work (Phase 3 removed APIs — the crate will not compile) is clearly separated from the **optional** work (Phases 4–5). End with a one-paragraph recommendation: the smallest next PyO3 version the crate can move to cleanly, and the effort to get there.

## Tips

- Run this **before** bumping the `pyo3` version in `Cargo.toml` — it tells you what will break.
- A crate on an unresolved PyO3 version (path/workspace dep) is assessed against the 0.28 baseline; say so.
- Free-threading (Phase 4) is never blocking — it is readiness work, not a bug fix.
