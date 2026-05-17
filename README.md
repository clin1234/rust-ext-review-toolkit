# rust-ext-review-toolkit

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin for
reviewing Python extensions written in **Rust with PyO3** — finding soundness
bugs in `unsafe` code, PyO3 API misuse, GIL/lifetime discipline issues,
`#[pyclass]` trait-bound violations, panic-safety problems, and PyO3
version-migration debt.

*Find the soundness bugs in your Rust extension before your users do.*

> **Status: pre-v0.1, under active construction.** The toolkit is being built
> from its design documents. Until v0.1, the installed plugin has an incomplete
> agent set. See [`IMPLEMENTATION_ROADMAP.md`](IMPLEMENTATION_ROADMAP.md) for
> progress.

## What PyO3 prevents — and what it doesn't

PyO3's type system already eliminates most classic C-extension bug classes, so
this toolkit does **not** scan for them:

- Manual `Py_INCREF`/`Py_DECREF` — `Py<T>` and `Bound<'py, T>` implement `Drop`
- Most NULL-pointer dereferences — fallible APIs return `PyResult<T>` / `Option<T>`
- Most use-after-free of Python objects — the `'py` lifetime ties handles to the
  interpreter
- Memory safety in safe Rust — the borrow checker

What PyO3 does **not** prevent — and what this toolkit is *for*:

- Soundness bugs inside `unsafe` (raw `pyo3-ffi`, `transmute`, manual buffer
  protocol, C-library FFI)
- `#[pyclass]` trait-bound violations — non-`Send`/`Sync` fields without
  `unsendable`
- Panics crossing the `extern "C"` FFI boundary
- `PyResult` propagation gaps — `.unwrap()`, `.ok()`, `let _ =`, missing `?`,
  exception clobbering
- Wrong handle kind — `Bound` vs `Py` vs `Borrowed`; legacy `&PyAny`
- `#[pymethods]` protocol mistakes — missing `__traverse__`/`__clear__`,
  `__richcmp__` semantics
- PyO3 version-migration debt and `abi3` consistency
- Free-threaded Python (3.13t/3.14t) readiness

## Installation

> Pre-v0.1: installation gives an incomplete plugin. The instructions below are
> the intended UX.

### Marketplace install

```bash
claude plugin marketplace add devdanzin/rust-ext-review-toolkit
claude plugin install rust-ext-review-toolkit@rust-ext-review-toolkit
```

### Try it without installing

```bash
git clone https://github.com/devdanzin/rust-ext-review-toolkit.git
claude --plugin-dir rust-ext-review-toolkit/plugins/rust-ext-review-toolkit
```

### Prerequisites

- **Claude Code** installed and running
- **Python 3.10+** for the analysis scripts
- **tree-sitter** and **tree-sitter-rust**: `pip install tree-sitter tree-sitter-rust`
- Optional: a Rust toolchain with `cargo clippy` / `cargo miri` / `cargo expand`
  for deeper cross-referencing

## Quick start

In a PyO3 crate:

```bash
/rust-ext-review-toolkit:health      # Quick scored dashboard
/rust-ext-review-toolkit:hotspots    # Worst functions: unsafe + panic + complexity
/rust-ext-review-toolkit:explore     # Full analysis (all agents, phased)
/rust-ext-review-toolkit:migrate     # Bound-API / free-threading / abi3 checklist
```

## Agents

13 agents — 11 script-backed, 1 qualitative, 1 preflight.

### Preflight

- **rust-codegen-mapper** — orientation guide: PyO3 version + features, file
  classification, project-specific idioms; runs first so downstream agents
  triage accurately.

### Safety-critical

- **unsafe-block-auditor** — soundness of every `unsafe` block; raw `pyo3-ffi`
  calls; `transmute`; manual buffer protocol *(the #1 agent)*
- **pyresult-propagation-checker** — `.unwrap()`/`.ok()` on `PyResult`, missing
  `?`, exception clobbering
- **gil-discipline-checker** — handles held across `detach`; nested `attach`;
  foreign-callback GIL acquisition; free-threading declarations
- **panic-safety-checker** — panic-prone calls reachable from `#[pymethods]` /
  `#[pyfunction]`

### PyO3-specific

- **pyclass-traits-checker** — `#[pyclass]` `Send`/`Sync`/`frozen`/`unsendable`
- **pyclass-protocol-checker** — `#[pymethods]` slot signatures,
  `__traverse__`/`__clear__`, `__richcmp__`
- **module-init-checker** — `#[pymodule]` shape, module-level static state
- **lifetime-handle-checker** — `Bound` vs `Py` vs `Borrowed`; legacy `&PyAny`

### Compatibility, quality, parity

- **pyo3-version-compat-scanner** — deprecated/removed APIs by version; abi3
  consistency
- **rust-complexity-analyzer** — complexity scoring (includes `unsafe`-block
  density)
- **git-history-analyzer** — similar-bug detection, churn-based risk
  prioritization
- **parity-checker** — behavioral divergence between a Rust extension and its
  pure-Python fallback

## Commands

| Command | Purpose |
|---------|---------|
| `explore` | Full analysis, phased agent groups, selectable aspects |
| `health` | Quick scored dashboard, all agents in summary mode |
| `hotspots` | Worst functions first — unsafe + pyresult + panic + complexity |
| `migrate` | Bound-API migration, free-threading readiness, abi3 — checklist output |

## How it works

**Tree-sitter-rust parsing.** Scripts parse `.rs` source with `tree-sitter-rust`
— style-agnostic and robust against macro-heavy code. Tree-sitter does not
expand procedural macros, so `#[pyclass]`/`#[pymethods]` analysis is syntactic
inference over the attribute + struct + method shape.

**Crate discovery.** Auto-detects PyO3 crates: `Cargo.toml` with a `pyo3`
dependency and a `cdylib`, `pyproject.toml` with maturin or setuptools-rust, or
`setup.py` with `setuptools_rust.RustExtension`. Resolves the PyO3 version,
active features, Python floor, and free-threading intent.

**Classification.** Every finding is tagged:

| Tag | Meaning |
|-----|---------|
| **FIX** | Soundness bug, panic across FFI on a reachable path, error swallowing that surfaces wrong data |
| **CONSIDER** | Likely improvement; PyO3 idiom migration; free-threading readiness gap |
| **POLICY** | Maintainer's design call (adopt abi3? target free-threading? support subinterpreters?) |
| **ACCEPTABLE** | Noted, no action — e.g. an `unsafe` block with a verified `// SAFETY:` comment |

**External tools (optional).** When a Rust toolchain is present, `clippy` /
`miri` / `cargo-expand` / `cargo metadata` are used opportunistically. Never
required — Tree-sitter analysis is the baseline.

## Limitations

- **Tree-sitter, not a compiler.** Cannot expand procedural macros, resolve
  trait-bound satisfaction, or do type inference. Scripts emit candidates
  (~20–40% false positives); agents confirm or dismiss each.
- **Macro-generated code is invisible.** What `#[pyclass]` emits is inferred
  from the source-level shape, not seen directly.
- **PyO3 evolves fast.** API knowledge lives in version-keyed data files; the
  analysis logic stays stable across PyO3 releases.

## Sibling projects

| Project | Target | Parsing |
|---------|--------|---------|
| [code-review-toolkit](https://github.com/devdanzin/code-review-toolkit) | Python source | `ast` |
| [cpython-review-toolkit](https://github.com/devdanzin/cpython-review-toolkit) | CPython runtime C | Regex |
| [cext-review-toolkit](https://github.com/devdanzin/cext-review-toolkit) | C extensions | Tree-sitter-c |
| **rust-ext-review-toolkit** | **Rust/PyO3 extensions** | **Tree-sitter-rust** |

## Authors

Charlie Lin and Danzin.

## License

MIT — see [LICENSE](LICENSE).
