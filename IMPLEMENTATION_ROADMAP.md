# Implementation Roadmap

This is the operational guide for the chat building rust-ext-review-toolkit
from the design doc.

**Read in this order:**

1. `rust-ext-review-toolkit-design.md` — the spec (you have it)
2. `PYO3_PRIMER.md` — what PyO3 is, why each scanner exists
3. **This document** — what to do, in what order
4. `TREE_SITTER_RUST_RECIPES.md` — concrete patterns, open as reference while
   coding

This roadmap is the action document. The other three are inputs to it.

---

## 1. Starting-state diagnosis

The repo is a **mostly-broken find-and-replace** of cext-review-toolkit. The
design doc is good. Everything else needs surgery. Before any new work, you
need to know what's there.

### 1.1 What's correct (keep)

| Path | State |
|------|-------|
| `rust-ext-review-toolkit-design.md` | **Canonical spec**. Don't modify unless you find a real defect — and if you do, flag it back first. |
| `LICENSE` | MIT, fine as-is. |
| `tests/helpers.py` | Has `TempExtension` + `import_script` — keep the *infrastructure*, rename to `TempRustExtension` per design §9.1, but the temp-dir + dict-of-files pattern is reusable. |

### 1.2 What's misleadingly named or wrong-content (rewrite from scratch)

The plugin directory is the worst-affected. Every agent / script / data file
under `plugins/rust-ext-review-toolkit/` is a literal copy of cext content.
The find-and-replace has introduced specific contamination:

- **`agents/rust-complexity-analyzer.md`** has YAML `name: c-complexity-analyzer`
  and body discussing `goto __PYX_L*_error;` (Cython artifact, irrelevant to
  Rust)
- **`agents/refcount-auditor.md`** exists but the design **drops** this agent
  (§11, §3.1). Same with `null-safety-scanner.md`, `pyerr-clear-auditor.md`,
  `resource-lifecycle-checker.md`, `stable-abi-checker.md`
- **All scripts** under `plugins/rust-ext-review-toolkit/scripts/` are
  C-extension scanners. None are usable as a starting point — the analysis
  logic is different per agent.
- **All Cython scripts** (`scan_cython_*.py`, `cython_ast_utils.py`) are
  irrelevant — design §3.1 doesn't include Cython coverage
- **All tests** under `tests/` are C-extension tests. None correspond to
  agents in the design.

### 1.3 What's worth scavenging (read for patterns, don't keep code)

- `scripts/scan_common.py` — the `parse_common_args()`, `make_finding()`,
  `deduplicate_findings()`, `is_suppressed_by_comment()` helpers are the
  conventions you want to preserve (design §4.1 says "Adapted From
  cext-review-toolkit's scan_common.py"). Rewrite to Rust focus but keep the
  shape.
- `scripts/tree_sitter_utils.py` — the `parse_bytes_for_file()`,
  `extract_functions()` pattern is the *shape* for the new `rust_ts_utils.py`,
  but the node types and helpers are different. Read it for structure, write
  fresh for content.
- `scripts/discover_extension.py` — same role as the new
  `discover_rust_ext.py`, but the build-system detection is completely
  different (`Cargo.toml`, `pyproject.toml` with maturin, etc.). Read for the
  output-schema shape; rewrite the detection logic.
- `tests/helpers.py` `TempExtension` — the pattern for building temp project
  trees. Rename + adapt for Rust crates.
- `data/api_tables.json` — the schema shape (categories, version mapping) is
  the same; the data inside is C-API data, not PyO3 data.
- `CLAUDE.md` — the dev-workflow shape (venv, ruff, mypy, unittest, scripts'
  `analyze()` + `main()` contract) is what you want to inherit. The Rust-side
  content needs to be written fresh.

### 1.4 What to delete outright (before writing anything new)

Delete these files **before starting Phase 1** — leaving them around invites
confusion and accidental "extending" of stale content.

```bash
# Stale agents (the design drops these names)
rm plugins/rust-ext-review-toolkit/agents/refcount-auditor.md
rm plugins/rust-ext-review-toolkit/agents/null-safety-scanner.md
rm plugins/rust-ext-review-toolkit/agents/pyerr-clear-auditor.md
rm plugins/rust-ext-review-toolkit/agents/resource-lifecycle-checker.md
rm plugins/rust-ext-review-toolkit/agents/stable-abi-checker.md
rm plugins/rust-ext-review-toolkit/agents/error-path-analyzer.md   # folded into pyresult
rm plugins/rust-ext-review-toolkit/agents/version-compat-scanner.md  # renamed to pyo3-version-compat-scanner
rm plugins/rust-ext-review-toolkit/agents/type-slot-checker.md      # renamed to pyclass-protocol-checker
rm plugins/rust-ext-review-toolkit/agents/module-state-checker.md   # renamed to module-init-checker
rm plugins/rust-ext-review-toolkit/agents/generated-code-mapper.md  # renamed to rust-codegen-mapper

# Stale scripts (all of these — they're C-extension analysis, not Rust)
rm plugins/rust-ext-review-toolkit/scripts/scan_refcounts.py
rm plugins/rust-ext-review-toolkit/scripts/scan_null_checks.py
rm plugins/rust-ext-review-toolkit/scripts/scan_pyerr_clear.py
rm plugins/rust-ext-review-toolkit/scripts/scan_resource_lifecycle.py
rm plugins/rust-ext-review-toolkit/scripts/scan_error_paths.py
rm plugins/rust-ext-review-toolkit/scripts/scan_module_state.py
rm plugins/rust-ext-review-toolkit/scripts/scan_type_slots.py
rm plugins/rust-ext-review-toolkit/scripts/scan_version_compat.py
rm plugins/rust-ext-review-toolkit/scripts/scan_gil_usage.py
rm plugins/rust-ext-review-toolkit/scripts/scan_format_strings.py    # C-only
rm plugins/rust-ext-review-toolkit/scripts/measure_c_complexity.py
rm plugins/rust-ext-review-toolkit/scripts/discover_extension.py
rm plugins/rust-ext-review-toolkit/scripts/tree_sitter_utils.py

# Stale Cython suite (design §3.1 has no Cython coverage)
rm plugins/rust-ext-review-toolkit/scripts/scan_cython_*.py
rm plugins/rust-ext-review-toolkit/scripts/cython_ast_utils.py
rm plugins/rust-ext-review-toolkit/data/playbooks/cython.md

# Stale data files (schemas may inform but contents are C-side)
rm plugins/rust-ext-review-toolkit/data/api_tables.json
rm plugins/rust-ext-review-toolkit/data/deprecated_apis.json
rm plugins/rust-ext-review-toolkit/data/resource_pairs.json
# KEEP:  data/stable_abi.json  and  data/limited_api_headers.json  (vendored
# from cext per design §2.2 — they describe CPython's limited API surface,
# which raw pyo3-ffi calls under abi3 are subject to. Re-vendor only if you
# need the very latest from cext-review-toolkit.)

# Stale tests (all of them — they correspond to C-extension scanners)
rm tests/test_scan_refcounts.py
rm tests/test_scan_error_paths.py
rm tests/test_scan_null_checks.py
rm tests/test_scan_pyerr_clear.py
rm tests/test_scan_resource_lifecycle.py
rm tests/test_scan_module_state.py
rm tests/test_scan_type_slots.py
rm tests/test_scan_version_compat.py
rm tests/test_scan_gil_usage.py
rm tests/test_scan_format_strings.py
rm tests/test_scan_cython_*.py
rm tests/test_cython_ast_utils.py
rm tests/test_cpp_support.py            # C++ irrelevant
rm tests/test_measure_c_complexity.py
rm tests/test_discover_extension.py
rm tests/test_tree_sitter_utils.py
rm tests/test_run_external_tools.py     # will rewrite
rm tests/test_scan_common.py            # will rewrite

# Stale agent (the rust-complexity-analyzer.md file has the right NAME but
# contaminated CONTENT — easier to delete and re-write than patch)
rm plugins/rust-ext-review-toolkit/agents/rust-complexity-analyzer.md

# Stale commands (will rewrite — they reference dropped agents)
rm plugins/rust-ext-review-toolkit/commands/explore.md
rm plugins/rust-ext-review-toolkit/commands/health.md
rm plugins/rust-ext-review-toolkit/commands/hotspots.md
rm plugins/rust-ext-review-toolkit/commands/migrate.md

# Documentation
rm docs/init-reinit-safety-rule.md     # C-extension concept, irrelevant
# KEEP: docs/reproducer-techniques.md  (general patterns — keep as base, add
#       Rust-specific reproducer technique entries later)
# KEEP: docs/libfiu_helpers.py and libfiu_zstd_validation.py (general OOM
#       reproducer infrastructure, useful for any extension)
```

After this cleanup, `git status` should show a long list of deletions and a
mostly-bare plugin tree. That's the expected starting state.

### 1.5 What to rewrite (top-level docs)

These need fresh content keyed to the design:

- `README.md` — rewrite. The current copy has `clin1234/...` GitHub URLs
  copy-pasted from a fork, "Rust extension" mixed with "C extension", and a
  technical content that's wrong for the new agent set.
- `CLAUDE.md` — rewrite. The structure is right (project overview, dev
  commands, code style, project structure, architecture, testing, gotchas)
  but every section needs Rust-side content.
- `CHANGELOG.md` — clear it and start fresh with `[Unreleased]` carrying the
  initial scaffolding items.
- `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` —
  rewrite. Current descriptions reference C-extension concerns and dropped
  agents (refcount-auditor, null-safety, etc.). Update keywords, description,
  agent list.

---

## 2. Phase 1 — Foundation (Week 1)

Per design §7. Concrete file-by-file plan.

### 2.1 Project scaffolding

In order, *after the deletions above*:

- [ ] **Top-level docs** (rewrite the five `README.md` / `CLAUDE.md` /
      `CHANGELOG.md` / `.claude-plugin/plugin.json` / `.claude-plugin/marketplace.json`)
      with content that reflects the design's agent inventory and the PyO3 focus.
- [ ] **`plugins/rust-ext-review-toolkit/.claude-plugin/plugin.json`** —
      mirror the top-level metadata; declare the 13 agents (12 audit + 1
      preflight) per design §3.1.
- [ ] **`plugins/rust-ext-review-toolkit/README.md`** — short, references the
      top-level docs and lists the agents.

### 2.2 Core parsing utilities

- [ ] **`plugins/rust-ext-review-toolkit/scripts/rust_ts_utils.py`** — the
      central parsing module. Functions per design §2.1. Use
      `TREE_SITTER_RUST_RECIPES.md` §2 for node types. Include:
      - Dependency check (`tree_sitter`, `tree_sitter_rust`); exit with JSON
        error on missing
      - `parse_bytes(source: bytes) -> Tree`
      - `parse_file(path: Path) -> Tree` (read source bytes; do NOT re-read
        inside helpers per cext lesson)
      - `extract_fn_items(tree, source) -> list[FnNode]`
      - `extract_impl_blocks(tree, source) -> list[ImplBlock]`
      - `extract_struct_defs(tree, source) -> list[StructDef]`
      - `extract_attribute_targets(tree, source, attr_name) -> list`
      - `extract_attributes(node, source) -> list[AttributeNode]`
      - `extract_unsafe_blocks(node, source) -> list[UnsafeNode]`
      - `find_calls_in_scope(node, source, names=None) -> list`
      - `find_closure_bodies(node, source, callee=None) -> list`
      - `extract_use_statements(tree, source) -> list`
      - `extract_generic_params(node, source) -> list`
      - Helper: `strip_comments(source: bytes) -> bytes` — cext lesson: when
        checking function bodies for patterns like `unsafe`, comments
        containing the keyword cause FNs. Always strip first.
      - Helper: `text_of(node, source) -> str` — convenience.

      **Test approach**: `tests/test_rust_ts_utils.py` — small inline `.rs`
      strings, parse, assert the extractors return the expected node counts
      and text content. Don't test all node types exhaustively — test the
      cases each scanner relies on (see scanner-specific tests later).

- [ ] **`plugins/rust-ext-review-toolkit/scripts/scan_common.py`** — adapted
      from cext-review-toolkit. Functions:
      - `find_project_root(start: Path) -> Path` — climb until you find
        `Cargo.toml`
      - `discover_rust_files(root: Path) -> list[Path]` — walk `src/`,
        respect `Cargo.toml` `[lib]` and `[[bin]]` paths, skip `target/`
      - `parse_common_args(argv) -> Namespace` — `target`, `--max-files`,
        `--workers`, `--json` flags
      - `load_data_file(name: str) -> dict` — for `pyo3_api_tables.json` etc.
      - `make_finding(...)` — factory, same shape as cext's
      - `deduplicate_findings(findings)`
      - `is_suppressed_by_comment(node, source)` — looks for `// SAFETY:`,
        `// rust-ext-safe:`, `#[allow(...)]` adjacent

      **Tree-sitter dependency**: imports from `rust_ts_utils.py`, so this
      lives next to it; uses `sys.path.insert(0, str(Path(__file__).parent))`
      per cext convention.

### 2.3 Project discovery

- [ ] **`plugins/rust-ext-review-toolkit/scripts/discover_rust_ext.py`** —
      per design §2.4. Detection strategy: `Cargo.toml` is the strongest
      signal. Output schema per design §4.2.

      Build-system detection cases (handle each):

      1. `Cargo.toml` with `[lib] crate-type = ["cdylib"]` or `["cdylib",
         "rlib"]` + `pyo3` dep
      2. `pyproject.toml` with `[build-system] requires = ["maturin"]`
      3. `pyproject.toml` with `[build-system] requires = ["setuptools-rust"]`
         + `[[tool.setuptools-rust.ext-modules]]`
      4. `setup.py` containing `setuptools_rust.RustExtension(...)`
      5. Detected but out-of-scope: `Cargo.toml` with `pyo3` but no
         `cdylib` — report and exit
      6. Fallback: any `.rs` containing `use pyo3::`, `#[pymodule]`,
         `#[pyfunction]`, `#[pyclass]`

      Output must include: crate name, module name (from
      `#[pymodule] fn xxx`), PyO3 version (exact from `Cargo.lock` if
      present, else range from `Cargo.toml`), active PyO3 features
      (`extension-module`, `abi3`, `abi3-pyNM`, `multiple-pymethods`,
      `experimental-async`, ...; note there is no `gil-disabled` feature),
      Python version floor (from `abi3-pyNM` or maturin config),
      free-threading targeted (Y/N — inferred from `gil_used`/CI signals,
      not a feature), source files with classification,
      whether `python/` source exists.

      **Test fixture seeds**: see §6 of this document.

      **Use `tomllib`** (3.11+) for TOML parsing; `tomllib` is stdlib so no
      new dependency.

### 2.4 Initial data files

- [ ] **`data/pyo3_api_tables.json`** — seed structure (you can ship with
      partial data and iterate). Initial pass: list the headline API shifts
      across PyO3 0.20 → 0.29 (the `&PyAny` → `Bound` migration, `as_ref` →
      `bind`, the `_bound` suffix history, `with_gil`/`allow_threads` →
      `attach`/`detach`, `downcast` → `cast`). Record legacy↔current name
      pairs so scanners recognize both vocabularies. Format:

      ```json
      {
        "schema_version": 1,
        "apis": {
          "python_with_gil": {
            "renamed_to": "Python::attach",
            "renamed_in": "0.25",
            "rust_signature_pattern": "Python::with_gil",
            "category": "gil_api",
            "note": "legacy alias; scanners recognize both names"
          },
          "py_any_ref": {
            "deprecated": "0.21",
            "removed": "0.24+",
            "replacement": "Bound<'py, PyAny>",
            "rust_signature_pattern": "&PyAny",
            "category": "handle_kind",
            "note": "GIL-ref; transitionally kept behind the gil-refs feature"
          }
        }
      }
      ```

      Source the data from PyO3 release notes:
      <https://pyo3.rs/main/migration.html>. Don't aim for completeness on
      day one — aim for the 20 most-cited APIs so the scanner is useful
      from the start.

- [ ] **`data/non_send_sync_types.json`** — types that do not satisfy `Send`
      and/or `Sync`. Since PyO3 0.23 a `#[pyclass]` requires `Send + Sync`, so
      each entry records *which* bound it breaks: `Rc` (neither), `RefCell`/`Cell`
      (`Send` but not `Sync`), `*const _`/`*mut _`/`NonNull` (neither).
      Categories: stdlib, third-party (e.g. `gc-arena::Gc`). Consumed by
      `pyclass-traits-checker`.

      ```json
      {
        "types": [
          { "path": "std::rc::Rc", "aliases": ["rc::Rc", "Rc"], "send": false, "sync": false },
          { "path": "std::cell::RefCell", "aliases": ["cell::RefCell", "RefCell"], "send": true, "sync": false },
          { "path": "std::cell::Cell", "aliases": ["cell::Cell", "Cell"], "send": true, "sync": false }
        ],
        "raw_pointer_patterns": ["*const", "*mut", "NonNull"]
      }
      ```

- [ ] **`data/panic_unwind_apis.json`** — Stdlib methods that panic on
      certain inputs. Categories: unwrap-class (`.unwrap()`, `.expect()`),
      index-class (`[i]` on slices/vecs), arithmetic (`a + b` under
      overflow-checks), `unimplemented!()`, `todo!()`, `unreachable!()`,
      `panic!()`, `assert!()`. Consumed by `panic-safety-checker`.

- [ ] **Vendor `data/stable_abi.json` and `data/limited_api_headers.json`**
      from cext-review-toolkit per design §2.2. If they're already present
      from the find-and-replace, leave them alone (verify the content
      matches cext's current `main`).

### 2.5 Test infrastructure

- [ ] **`tests/helpers.py`** — rewrite. `TempRustExtension` per design §9.1.
      Context manager that creates a temp dir with a `Cargo.toml`, optional
      `pyproject.toml`, optional `setup.py`, and a dict of `.rs` files. Keep
      `import_script` exactly as cext has it.

- [ ] **`tests/test_rust_ts_utils.py`** — test the utility functions.

- [ ] **`tests/test_discover_rust_ext.py`** — test the discovery script
      against the fixture seeds.

- [ ] **`tests/test_scan_common.py`** — test the common helpers.

### 2.6 Phase 1 exit criteria

- [ ] Tests pass: `python -m unittest discover tests -v`
- [ ] `python plugins/rust-ext-review-toolkit/scripts/discover_rust_ext.py
      /path/to/a/real/pyo3/crate` outputs a sensible JSON with `crate_name`,
      `module_name`, `pyo3_version`, features, source files
- [ ] `data/pyo3_api_tables.json` exists with at least 10 API entries
      spanning 0.20-0.29
- [ ] No `import` cycles between scripts; `rust_ts_utils` is leaf, then
      `scan_common`, then everything else

If you can't get clean discovery output on a real crate, **stop and flag
back** — the rest of the toolkit depends on `discover_rust_ext` producing
correct data.

---

## 3. Phase 2 — Safety-critical agents (Week 2)

Per design §3.2.1 and §3.2.2. **This is the highest-value pair of agents** —
ship them and the toolkit is already useful even before later phases land.

### 3.1 `scan_unsafe_blocks.py` + `unsafe-block-auditor.md`

- [ ] **Script** — per design §3.2.1. Implementation notes:
  - Walk for both `unsafe_block` nodes *and* `function_item` nodes with
    `unsafe` modifier
  - For each, check whether `Python<'py>` is in lexical scope (walk ancestors
    for a parameter pattern with `Python<'py>` type)
  - For each call inside, cross-reference the callee against `pyo3-ffi`
    patterns (use `data/stable_abi.json` and `data/limited_api_headers.json`)
  - Special-case the seven patterns from design §3.2.1 (raw FFI calls, raw
    pointer derefs, `transmute`, manual buffer protocol, `unsafe impl Send`,
    FFI to C libraries, private `_Py*`)
  - Each finding gets `category` set (so the agent can group by type) and
    `details` populated with the specific API or pattern matched
  - Classification per design §3.2.1: FIX/CONSIDER/POLICY/ACCEPTABLE

  See `TREE_SITTER_RUST_RECIPES.md` §3.1 for the exact node patterns.

- [ ] **Tests** — `tests/test_scan_unsafe_blocks.py`. Use fixture seeds from
      §6 of this document. At minimum:
  - True positive: each of the 7 patterns triggers a finding
  - True negative: a `// SAFETY:`-commented `unsafe { ffi::Py_IncRef(x) }`
    produces ACCEPTABLE
  - Edge cases: nested `unsafe`, `unsafe fn` (not block), generics, lifetimes

- [ ] **Agent prompt** — `agents/unsafe-block-auditor.md`. Use cext's
      `refcount-auditor.md` as a structural template (Preflight Orientation,
      Key Concepts, Analysis Phases, Running the Script). Replace all
      C-specific content. The agent's job is to triage each script finding,
      check for nearby `// SAFETY:` comments, and produce a final
      classification per finding.

### 3.2 `scan_pyresult.py` + `pyresult-propagation-checker.md`

- [ ] **Script** — per design §3.2.2. The seven patterns:
  1. `.unwrap()` / `.expect()` on `PyResult` inside `#[pymethods]` /
     `#[pyfunction]`
  2. `.ok()` on `PyResult`
  3. `let _ = some_pyresult;`
  4. Missing `?` (heuristic: a function returning `PyResult<T>` with a let
     binding whose RHS returns `PyResult` and no `?`)
  5. Exception clobbering (capture `PyErr`, then call another Python API
     before propagating)
  6. `PyErr::take()` / `PyErr::fetch()` / `PyErr::restore()` balance in
     `Drop` impls
  7. Silent `PyErr::clear()` calls

  Type-inference is approximate. Heuristic: a method call or function call
  in the `pyo3::*` or `Bound<'_, T>::*` namespace returning a value to a
  binding is treated as a `PyResult<T>` for this scanner's purposes. False
  positives are managed via the agent triage step.

- [ ] **Tests** — `tests/test_scan_pyresult.py`. Each pattern with a
      true-positive case and a true-negative twin.

- [ ] **Agent prompt** — `agents/pyresult-propagation-checker.md`.

### 3.3 Phase 2 exit criteria

- [ ] Both scripts emit findings on a known-buggy fixture and zero findings
      on a known-clean fixture
- [ ] Both agent prompts have the standard sections (Preflight Orientation,
      Key Concepts, Phases, Running the Script, Output)
- [ ] Tests pass
- [ ] You've manually run `unsafe-block-auditor` against `pydantic-core`'s
      `src/lib.rs` (or another medium-sized real PyO3 crate) and the
      findings make sense (no obvious noise, no obvious miss)

---

## 4. Phase 3 — GIL and panic safety (Week 3)

Per design §3.2.3 and §3.2.4.

### 4.1 `scan_gil_discipline.py` + `gil-discipline-checker.md`

The hardest scanner so far. PyO3 0.25 renamed `with_gil`→`attach` and
`allow_threads`→`detach`; the scanner recognizes both vocabularies (see design
§3.2.3 and RECIPES §3.3). Key patterns:

- `Bound<'py, T>` referenced inside a `detach` (legacy `allow_threads`) closure
- `Py<T>::bind(py)` inside a `detach` closure
- pyo3-ffi calls inside a `detach` closure
- Nested `attach` (legacy `with_gil`)
- `extern "C" fn` body using PyO3 APIs without a top-level `attach`
- `attach` in long-lived callback bridges (finalization risk)
- Loops with no Python API calls but an attached interpreter (potential
  `detach` candidates)
- Free-threading: `gil_used` declarations vs `#[pyclass]` mutability

Closure-body extraction is the new wrinkle vs prior scripts. `find_closure_bodies`
in `rust_ts_utils.py` returns closure expressions; filter by callee name
(`detach`/`allow_threads`, `attach`/`with_gil`).

### 4.2 `scan_panic_safety.py` + `panic-safety-checker.md`

Walk `#[pymethods]` and `#[pyfunction]` bodies plus their callees (one
hop). Flag panic-prone calls per `data/panic_unwind_apis.json`. Special
attention to `extern "C" fn` callbacks registered with non-PyO3 C libraries
(panic abort risk).

### 4.3 Phase 3 exit criteria

- [ ] All four agents (Phase 2 + Phase 3) pass their tests
- [ ] Manual run on the same real crate from Phase 2 surfaces some findings
      with sensible classifications

---

## 5. Phases 4-6 (Weeks 4-6)

Follow design §7 verbatim. Notes per phase:

### 5.1 Phase 4 — PyO3-specific agents

- `scan_pyclass_traits.py` — Send/Sync/frozen/unsendable analysis
- `scan_pyclass_protocol.py` — `#[pymethods]` slots, `__traverse__`/`__clear__`,
  `__richcmp__` (the `tp_richcompare` lessons from cext apply directly —
  see PYO3_PRIMER §9)
- `scan_module_init.py` — `#[pymodule]` shape + static globals

### 5.2 Phase 5 — Compatibility, lifetimes, quality

- `scan_lifetime_handles.py` — `Bound` vs `Py` vs `Borrowed` vs legacy
  `&PyAny`. Cross-references `data/pyo3_api_tables.json` for version
  context.
- `scan_pyo3_version_compat.py` — deprecated/removed APIs by version, abi3
  consistency
- `measure_rust_complexity.py` — cyclomatic per design §3.2.10 (line count,
  nesting, `unsafe` density)
- `parity-checker.md` — qualitative, no script

### 5.3 Phase 6 — History, preflight, commands

- `analyze_history.py` — adapt from **cext-review-toolkit's** tree-sitter
  `analyze_history.py` (already ported off Python `ast`); swap C→Rust
  function-boundary detection and classification keywords. Per design §3.2.11.
- `rust-codegen-mapper.md` — preflight, no script (qualitative). PyO3
  orientation, **not** a generator detector — see design §3.3 (no playbook
  indirection)
- All 4 commands: `explore`, `health`, `hotspots`, `migrate`
- `run_external_tools.py` — clippy/miri/cargo-expand/cargo-metadata

---

## 6. Test fixture seeds

These are the canonical `.rs` snippets you'll use in tests. Inline them as
strings in `tests/fixtures.py` (or near the top of each test module). Use
them with `TempRustExtension`.

### 6.1 `MINIMAL_PYO3_CRATE`

```rust
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
```

`Cargo.toml`:

```toml
[package]
name = "minimal"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]

[dependencies]
pyo3 = { version = "0.22", features = ["extension-module", "abi3-py310"] }
```

### 6.2 `PYO3_CRATE_WITH_PYCLASS`

```rust
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
```

### 6.3 `PYO3_CRATE_WITH_UNSAFE` (clean unsafe)

```rust
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
```

(Note: with `pyo3-ffi`'s actual binding, `ob_refcnt` access for free-threaded
builds requires a different API. Don't worry about that detail in the
fixture — the scanner is checking the syntactic pattern.)

### 6.4 `PYO3_CRATE_WITH_BUGS` (every scanner-relevant bug)

```rust
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
        // rustc rejects this in safe code, but the scanner reads unparsed
        // source — the syntactic pattern is what it keys on.
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
    // Bug 8 (module-init-checker): add_class result not checked
    m.add_class::<BadShared>();  // <-- no `?` here
    m.add_function(wrap_pyfunction!(bad_function, m)?)?;
    m.add_function(wrap_pyfunction!(bad_transmute, m)?)?;
    m.add_function(wrap_pyfunction!(old_style_param, m)?)?;
    Ok(())
}
```

### 6.5 `PYO3_FT_TARGET_CRATE` (free-threading scenarios)

```rust
use pyo3::prelude::*;

// Should be `frozen` for FT, has mutable field
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

// Free-threading-friendly counterpart
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
    // No gil_used declaration. The checker's verdict is PyO3-version-dependent:
    // pre-0.28 a module defaults to GIL-required; 0.28+ it defaults to
    // thread-safe. See gil-discipline-checker.
    m.add_class::<MutableCounter>()?;
    m.add_class::<FrozenCounter>()?;
    Ok(())
}
```

`Cargo.toml` for this fixture pairs `abi3-py313` with FT-target intent. There is
**no `gil-disabled` feature** — free-threading is not feature-gated in PyO3. The
`abi3` + free-threading combination is itself a finding: PyO3 ignores `abi3`
under a free-threaded build, so this fixture doubles as the "abi3 silently
ignored under FT" negative test for `pyo3-version-compat-scanner`:

```toml
[dependencies]
pyo3 = { version = "0.22", features = ["extension-module", "abi3-py313"] }
```

---

## 7. Real-world validation candidates

Per design §7 phase 7. Run on these crates and use them to calibrate
false-positive rates:

| Crate | Why | What to check |
|---|---|---|
| **pydantic-core** | Large, idiomatic, modern PyO3 (0.22) | Baseline noise level. Low-FP run = scanners are well-tuned |
| **cryptography** (`src/rust/`) | Large, security-sensitive, has unsafe blocks | unsafe-block-auditor calibration |
| **tokenizers** (HF) | Medium, lots of pyclass, async-ish | pyclass-traits-checker + pyclass-protocol-checker calibration |
| **polars-python** | Very large, performance-tuned | Stress test for scanner runtime |
| **rust-numpy** | Small, well-maintained | Sanity check + bug-yield baseline |

For each, expected output per scanner:

- `unsafe-block-auditor` should find ~0-2 FIX-class items per crate; the rest
  should be CONSIDER (the bare unsafe-block flag) and a handful of
  ACCEPTABLE (with `// SAFETY:`)
- `pyresult-propagation-checker` should find a handful of `.unwrap()` cases
  in test-helper modules (ACCEPTABLE) and 0-3 in production paths (FIX)
- `lifetime-handle-checker` on a 0.21–0.22 crate finds CONSIDER-class `&PyAny`
  residue; on a version where the GIL-ref API no longer compiles it should
  find FIX (useful for mid-migration audits)
- `pyclass-traits-checker` finds CONSIDER missing-frozen on FT-target crates,
  POLICY `unsendable` on intentional cases

If any scanner produces > 50 findings on a real medium crate, **stop and
calibrate** before continuing — that's the false-positive threshold for
"this is more noise than signal".

---

## 8. Decision points — flag back, don't guess

Things the design doc leaves open or that depend on the current PyO3
ecosystem. Don't paper over these; surface them in your end-of-session
report to the human.

| Decision | When you hit it | What to ask |
|---|---|---|
| PyO3 minimum version to support | When seeding `pyo3_api_tables.json` | Current stable is 0.28 (0.29 in development). Pre-0.21 crates are very rare now; "0.21+" is a reasonable floor for migration auditing, with 0.28 as the assumed baseline. Confirm with the human. |
| `cargo expand` integration scope | Phase 6 | Run it on every project? Only on `--deep` runs? Out-of-process behind a flag? |
| `miri` UB integration | Phase 6 | Run miri's `cargo +nightly miri test` opportunistically, or document the command and let the user run it? |
| Async PyO3 coverage | Phase 7+ | Design §10 says out of scope for v0.1; flag if you find a crate where this matters during validation. |
| `pyo3-numpy` coverage | Phase 7+ | Same — surface real-world signal of need. |
| Type-inference fidelity for `PyResult` detection | Phase 2.2 | Heuristic (callee-namespace matching) vs more expensive `cargo metadata`-driven inference — pick the heuristic and document the FP rate. |
| When `&Bound<'py, T>` vs `Bound<'py, T>` is "right" | Phase 5 | `lifetime-handle-checker` will need a heuristic here — borrow-by-default is idiomatic but not always; consult `rust-numpy`'s style. |
| Whether to vendor or re-fetch `stable_abi.json` | Phase 1 | Re-vendor if a noticeable diff exists; otherwise keep the existing file. |

When in doubt: pick the conservative path (fewer findings, more
ACCEPTABLE-by-default) and flag it. The toolkit's reputation gets damaged
faster by false positives than by missed bugs.

---

## 9. Cross-cutting conventions

Inherit these from cext-review-toolkit verbatim:

- **Tests use `unittest`, never pytest** (per cext CLAUDE.md)
- **Double quotes for strings, type hints on all signatures, docstrings on
  public functions**
- **Scripts expose `analyze(target: str, *, max_files: int = 0) -> dict` and
  `main()` that outputs JSON to stdout**
- **Exception:** `analyze_history.py` takes `argv` to match code-review-toolkit
- **Tree-sitter parsing convention**: always use a `parse_bytes_for_file()`-style
  wrapper that auto-selects the parser by extension; never call `parse_file`
  inside scanners (double I/O + TOCTOU)
- **Strip comments before pattern matching** when checking body content
- **`sys.path.insert` shim** in scripts; the scripts directory is not a
  Python package
- **JSON output schema** per design §4.2
- **Classification system** per design §6 — FIX / CONSIDER / POLICY /
  ACCEPTABLE
- **Findings carry `confidence`** (HIGH ≥ 90%, MEDIUM 70-89%, LOW 50-69%);
  below-LOW not reported
- **Findings carry `category`** for grouping in agent output
- **Safety annotations**: respect `// SAFETY:`, `// rust-ext-safe:`,
  `#[allow(...)]`, `#[cfg_attr(rust_ext_safe, ...)]`

---

## 10. Communication with the prompting human

You will be running on behalf of someone building this toolkit. The standard
expectations:

- **Pull source before review.** Cext-review-toolkit's `feedback_pull_before_analysis`
  norm applies here too. The user's `~/.claude/projects/.../memory/` has it.
- **Don't auto-file issues, don't auto-PR.** Even if validation surfaces
  bugs in real crates (e.g., pydantic-core), the human decides what to file.
  See cext-review-toolkit's `WORKING_WITH_MAINTAINERS.md` for the model.
- **Use task-workflow skill** for landing changes if available. Issue →
  branch → code → test → commit → PR → merge.
- **Pause for clarification at the decision points in §8** rather than
  guessing.
- **Flag back design-doc errors** rather than implementing against a stale
  spec. PyO3 evolves; if `pyo3.rs` and the design doc disagree, trust
  current PyO3 docs and surface the discrepancy.

---

## 11. Done criteria for v0.1

The toolkit is "v0.1 ready" when:

- [ ] All 13 agents (12 audit + 1 preflight) have working prompts and
      (where applicable) working scripts
- [ ] All 15 scripts in design §4.1 exist and pass tests
- [ ] All 4 commands work end-to-end on a real PyO3 crate
- [ ] `tests/` passes via `python -m unittest discover tests -v`
- [ ] At least 3 real PyO3 crates have been validated against (§7 above)
      and the false-positive rate is documented per scanner
- [ ] README, CLAUDE.md, CHANGELOG.md, and plugin metadata are coherent
      with the actual implementation
- [ ] `WORKING_WITH_MAINTAINERS.md` is present (port verbatim from
      cext-review-toolkit — the social contract is the same)

Stretch goals for v0.1 → v0.2:

- Procedural-macro expansion via `cargo expand` (design §10.1)
- `miri` integration (design §10.2)
- `pyo3-async-runtimes` coverage (design §10.4)
- `numpy` / `pyo3-numpy` playbook (design §10.5)

Out of scope for v0.1 entirely:

- `rust-cpython` (legacy ecosystem)
- Embedding-Python-from-Rust use cases
- General Rust analysis (clippy / cargo audit / cargo geiger cover that)
