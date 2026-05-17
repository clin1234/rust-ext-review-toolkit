# CLAUDE.md — rust-ext-review-toolkit development guide

## Project overview
rust-ext-review-toolkit is a [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin for reviewing Python extensions written in Rust with **PyO3**. It finds soundness bugs in `unsafe` code, PyO3 API misuse, GIL/lifetime discipline issues, `#[pyclass]` trait-bound violations, panic-safety problems, and PyO3 version-migration debt.

Part of a family of review toolkits:
- [code-review-toolkit](https://github.com/devdanzin/code-review-toolkit) — Python source code
- [cpython-review-toolkit](https://github.com/devdanzin/cpython-review-toolkit) — CPython runtime C code
- [cext-review-toolkit](https://github.com/devdanzin/cext-review-toolkit) — C extensions
- **rust-ext-review-toolkit** — Rust/PyO3 extensions (this project)

Key point: PyO3's type system already eliminates most C-extension bug classes (manual refcounting, NULL derefs, borrowed-ref-across-call). The toolkit's bug *catalog* is therefore PyO3-specific — the danger concentrates in `unsafe` blocks, panics crossing the FFI boundary, `#[pyclass]` `Send`/`Sync` bounds, and swallowed `PyResult`s. Like its siblings it uses Tree-sitter (here, `tree-sitter-rust`) rather than regex, because Rust extension styles and procedural-macro-heavy code defeat regex-based analysis.

> **Status:** pre-v0.1, under active construction. The plugin is being built from the design documents per `IMPLEMENTATION_ROADMAP.md`; `agents/`, `commands/`, and `scripts/` are populated phase by phase.

## Design documents (authoritative — read in this order)
1. `rust-ext-review-toolkit-design.md` — the spec: agents, scripts, schemas, classification.
2. `PYO3_PRIMER.md` — the domain brief: what PyO3 is, what each scanner detects and why.
3. `IMPLEMENTATION_ROADMAP.md` — the action plan: what to build, in what order, with exit criteria.
4. `TREE_SITTER_RUST_RECIPES.md` — coding reference: tree-sitter-rust node types and per-scanner walker recipes.

All four are reconciled to current PyO3 (0.28 stable, 0.29 in development). If current PyO3 docs and a design doc disagree, trust the PyO3 docs and flag the discrepancy.

## Prerequisites
- Python 3.10+
- `tree-sitter` and `tree-sitter-rust`: `pip install tree-sitter tree-sitter-rust`
- Optional, used opportunistically when present: `cargo clippy`, `cargo miri`, `cargo expand`, `cargo metadata` (from a Rust toolchain) — enable external-tool cross-referencing
- No other dependencies — all scripts use only the standard library plus tree-sitter

## Dev commands
```bash
# Activate the project venv
source ~/venvs/rust-ext-review-toolkit/bin/activate

# Run all tests
python -m unittest discover tests -v

# Run a specific test file (use discover -p; the tests/ dir is not a package,
# so `python -m unittest tests.test_x` cannot resolve the fixtures/helpers import)
python -m unittest discover tests -p test_scan_unsafe_blocks.py -v

# Run a single script standalone (all output JSON to stdout)
python plugins/rust-ext-review-toolkit/scripts/scan_unsafe_blocks.py /path/to/crate
python plugins/rust-ext-review-toolkit/scripts/discover_rust_ext.py /path/to/crate

# Lint and format (install ruff/mypy into the venv if not present)
ruff format <changed-files>
ruff check <changed-files>
mypy plugins/rust-ext-review-toolkit/scripts/
```

## Code style
- Python 3.10+ (uses `X | Y` union syntax)
- Double quotes for strings
- Type hints on all function signatures
- Docstrings on classes and public functions
- Tests use `unittest` — never pytest
- Linted and formatted with ruff, type checked with mypy

## Project structure

This is a Claude Code plugin, not a pip-installable package.

```
rust-ext-review-toolkit/
├── CLAUDE.md                          # This file
├── README.md                          # User-facing documentation
├── CHANGELOG.md                       # Keep a Changelog format
├── LICENSE                            # MIT
├── rust-ext-review-toolkit-design.md  # Design spec
├── PYO3_PRIMER.md                     # Domain brief
├── IMPLEMENTATION_ROADMAP.md          # Action plan
├── TREE_SITTER_RUST_RECIPES.md        # Coding reference
├── .claude-plugin/                    # marketplace.json + plugin.json
├── .claude/skills/task-workflow/      # Claude Code skill for the dev workflow
├── plugins/rust-ext-review-toolkit/   # The actual plugin
│   ├── .claude-plugin/plugin.json
│   ├── agents/                        # 13 agent prompt definitions (markdown)
│   ├── commands/                      # 4 command definitions (markdown)
│   ├── scripts/                       # 15 Python scripts (the core code)
│   └── data/                          # 8 JSON data files
└── tests/                             # unittest test suite
```

## Architecture

### Scripts (the core analysis code)

15 scripts in `plugins/rust-ext-review-toolkit/scripts/`. Every analysis script parses `.rs` files with Tree-sitter-rust, finds candidate issues, and outputs JSON to stdout.

| Script | Purpose |
|--------|---------|
| `rust_ts_utils.py` | Core tree-sitter-rust parsing module — all scanners import from here |
| `scan_common.py` | Shared utilities: project root, file discovery, data tables, args, dedup |
| `discover_rust_ext.py` | Crate/module detection, PyO3 version + feature resolution |
| `scan_unsafe_blocks.py` | `unsafe` block soundness audit |
| `scan_pyresult.py` | `PyResult` propagation, swallowed errors, exception clobbering |
| `scan_gil_discipline.py` | GIL handling — `detach`/`attach`, foreign callbacks, free-threading |
| `scan_panic_safety.py` | Panic-prone calls reachable across the FFI boundary |
| `scan_pyclass_traits.py` | `#[pyclass]` `Send`/`Sync`/`frozen`/`unsendable` |
| `scan_pyclass_protocol.py` | `#[pymethods]` slots, `__traverse__`/`__clear__`, `__richcmp__` |
| `scan_module_init.py` | `#[pymodule]` shape, module-level state |
| `scan_lifetime_handles.py` | `Bound` vs `Py` vs `Borrowed`, legacy `&PyAny` |
| `scan_pyo3_version_compat.py` | Deprecated/removed APIs by PyO3 version, abi3 consistency |
| `measure_rust_complexity.py` | Function complexity scoring |
| `analyze_history.py` | Git history — similar-bug detection, churn prioritization |
| `run_external_tools.py` | clippy / miri / cargo-expand / cargo-metadata integration |

**Dependency graph:** `rust_ts_utils.py` is the leaf. `scan_common.py` imports from it. All scanners import from both. `discover_rust_ext.py` and `run_external_tools.py` import only from `scan_common`. No cycles.

**Script calling convention:** every analysis script exposes `analyze(target: str, *, max_files: int = 0) -> dict` and a `main()` that outputs JSON to stdout via shared `parse_common_args()`. Exception: `analyze_history.py` takes `argv` (matches the code-review-toolkit convention).

**Data files** in `data/` (design §2.2): `pyo3_api_tables.json`, `pyo3_version_matrix.json`, `abi3_compatible_apis.json`, `non_send_sync_types.json`, `panic_unwind_apis.json`, `pyo3_idioms.json`, plus `stable_abi.json` and `limited_api_headers.json` — the last two vendored from cext-review-toolkit, used for raw `pyo3-ffi` audits under the `abi3` feature.

### Agents (prompt definitions for Claude Code)

13 markdown files in `agents/`. Each has YAML frontmatter and a structured prompt telling Claude Code how to run the corresponding script and triage its output. Scripts find *candidates* (deliberately high recall, ~20–40% false positives); agents confirm or dismiss each by reading real code context.

| Group | Agents |
|-------|--------|
| Preflight | rust-codegen-mapper (orientation guide; runs first) |
| Safety-critical | unsafe-block-auditor, pyresult-propagation-checker, gil-discipline-checker, panic-safety-checker |
| PyO3-specific | pyclass-traits-checker, pyclass-protocol-checker, module-init-checker, lifetime-handle-checker |
| Compatibility | pyo3-version-compat-scanner |
| Quality & history | rust-complexity-analyzer, git-history-analyzer |
| Parity | parity-checker (qualitative, no script) |

The crown-jewel finding is the `unsafe`-block soundness violation (and `Bound`/`Py` held across `detach`) — the Rust analogue of cext's borrowed-ref-across-callback.

### Commands (orchestration)

4 markdown files in `commands/`:
- `explore.md` — primary command, runs agents in phased groups
- `health.md` — quick scored dashboard, all agents in summary mode
- `hotspots.md` — unsafe + pyresult + panic + complexity; find the worst functions
- `migrate.md` — Bound-API / free-threading / abi3 migration checklist (the highest-value command, given PyO3's version cadence)

### Classification system

Every finding is tagged FIX / CONSIDER / POLICY / ACCEPTABLE — see design §6. Calibration notes: every `unsafe` block defaults to CONSIDER (a soundness obligation) until escalated to FIX or downgraded to ACCEPTABLE; free-threading and subinterpreter gaps are CONSIDER or POLICY, never FIX, unless they cause a bug in the current execution model.

## Testing notes
- All tests use `unittest` — never pytest
- Test helper in `tests/helpers.py`: `TempRustExtension` context manager, `import_script()` loader
- Fixture crate strings (roadmap §6): `MINIMAL_PYO3_CRATE`, `PYO3_CRATE_WITH_PYCLASS`, `PYO3_CRATE_WITH_UNSAFE`, `PYO3_CRATE_WITH_BUGS`, `PYO3_FT_TARGET_CRATE`
- Tests create temp crates (`Cargo.toml` + `.rs` files), run scripts, and check JSON output
- Version-aware scanners need tests across PyO3 0.21 / 0.25 / 0.28

## Adding a new analysis script
1. Create `plugins/rust-ext-review-toolkit/scripts/scan_newcheck.py`
2. Import from `rust_ts_utils` and `scan_common`
3. Implement `analyze(target, *, max_files=0) -> dict` following the common JSON envelope (design §4.2)
4. Add `main()` using `parse_common_args()`
5. Create `tests/test_scan_newcheck.py`: true positive, true negative, ≥1 edge case
6. Create the matching `agents/<agent>.md` with YAML frontmatter and prompt
7. Add the agent to the right phase group in `commands/explore.md`
8. Update CHANGELOG.md

## Gotchas
- **Scanners must recognize both PyO3 vocabularies.** PyO3 0.25 renamed `with_gil`→`attach`, `allow_threads`→`detach`, `with_gil_unchecked`→`attach_unchecked`; 0.27 renamed `downcast`→`cast`. The toolkit audits crates across PyO3 0.20–0.29, so scanners match *both* the legacy and current names. Rename pairs live in `data/pyo3_api_tables.json`.
- **There is no `gil-disabled` Cargo feature.** Free-threading is detected from the interpreter (the `Py_GIL_DISABLED` cfg), not feature-gated. Don't scan for a feature flag that doesn't exist. The module-level `gil_used` *default* also flipped in 0.28 (thread-safe by default) — classify version-aware.
- **Strip comments before pattern matching.** A comment containing `unsafe` or `unwrap` causes false negatives — always `strip_comments()` first.
- **Tree-sitter does not expand procedural macros.** `#[pyclass]`/`#[pymethods]` generate code the scanner never sees; analyses are *syntactic inference* over the attribute + struct + method shape. Document this limit in scanner output.
- **`sys.path.insert` for imports.** Scripts do `sys.path.insert(0, str(Path(__file__).resolve().parent))` to import siblings — the scripts directory is not a Python package. Tests use `import_script()`, which does the same.
- **Parse the source bytes once.** Read a file's bytes once and parse those; never have a helper re-read the file (double I/O + TOCTOU). Slice on `node.start_byte`/`end_byte` and decode the slice last (byte offsets, not char offsets).
- **Never compare tree-sitter nodes with `is`.** py-tree-sitter hands back a *fresh* `Node` wrapper on every access, so `node_a is node_b` is `False` even for the same underlying node (and `child_by_field_name(...) is some_walked_node` always fails). Compare identity with `node_a.id == node_b.id`; key sets/dicts on `node.id`.
- **`analyze_history.py` has a different signature.** It takes `argv`, not `(target, max_files)` — matches code-review-toolkit.
- **`#[pyclass]` requires `Send + Sync` since PyO3 0.23** (before 0.23, only `Send`). `data/non_send_sync_types.json` classifies each type by which bound it breaks.

## Workflow
- Use `/task-workflow <description>` for the full issue → branch → code → test → commit → PR → merge cycle.
