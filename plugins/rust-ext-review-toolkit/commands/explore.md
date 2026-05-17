---
description: "Comprehensive review of a Python extension written in Rust with PyO3. Use when the user asks to analyze, audit, or review a PyO3 extension, find bugs in Rust extension code, run all checks, or do a full extension review. Covers unsafe-block soundness, PyResult propagation, GIL discipline, panic safety, #[pyclass] trait bounds and protocol correctness, #[pymodule] init, handle lifetimes, PyO3 version compatibility, complexity, git history, and Rust/Python parity."
argument-hint: "[scope] [aspects] [options]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# Comprehensive PyO3 Extension Review

Run a comprehensive review of a Python extension written in Rust with PyO3, using specialized agents that each focus on one aspect of extension correctness. Discovery and a preflight orientation run first.

**Arguments:** "$ARGUMENTS"

**Plugin root:** `<plugin_root>` is the `plugins/rust-ext-review-toolkit/` directory — this command file's grandparent. Resolve it relative to this file.

## Argument Parsing

**Scope** (path or glob): `.` or omitted → whole crate; `src/` → a subtree; `src/lib.rs` → one file.

**Aspects** (which agents to run; default `all`):

| Aspect | Agent |
|--------|-------|
| `unsafe` | unsafe-block-auditor |
| `pyresult` | pyresult-propagation-checker |
| `gil` | gil-discipline-checker |
| `panic` | panic-safety-checker |
| `pyclass-traits` | pyclass-traits-checker |
| `pyclass-protocol` | pyclass-protocol-checker |
| `module-init` | module-init-checker |
| `lifetimes` | lifetime-handle-checker |
| `compat` | pyo3-version-compat-scanner |
| `complexity` | rust-complexity-analyzer |
| `parity` | parity-checker |
| `history` | git-history-analyzer |
| `all` | every agent (default) |

**Options:** `deep` (full detail), `summary` (summary tier only), `parallel` (run agents within a group concurrently), `--max-parallel N` (cap, default 2), `--max-files N` (forward to scanners).

## Execution Workflow

### Phase 0 — Discovery

Run `python <plugin_root>/scripts/discover_rust_ext.py [scope]` and parse the JSON. Print a brief summary:

```
Crate: mycrate  (module `mymod`, 6 .rs files)
PyO3: 0.27.2  | features: extension-module, abi3-py310
Build backend: maturin  | Python floor: 3.10  | Free-threading target: no
```

If `is_pyo3_extension` is false, tell the user and stop. If `out_of_scope` is true (a Rust binary embedding Python, not an extension), report it and stop.

### Phase 0.5 — External Tool Baseline (optional)

Run `python <plugin_root>/scripts/run_external_tools.py [scope]`. This runs `cargo metadata` (safe — no compilation) to confirm the exact PyO3 version, and reports which external tools are available. Pass `--run-clippy` only if the user explicitly asks (clippy compiles the crate, executing build scripts). Store the output; Phase 2 agents cross-reference it.

### Phase 1 — Preflight Orientation

Dispatch **rust-codegen-mapper**. It writes `reports/<crate>_v1/preflight/rust_codegen_map.md` — the orientation guide every Phase 2 agent reads first. Wait for it to finish before Phase 2.

### Phase 2 — Targeted Analysis

Dispatch the agents for the requested aspects, in groups. Pass each agent the scope, the discovery output, and a pointer to the preflight guide.

| Group | Agents | Focus |
|-------|--------|-------|
| **2A** | unsafe-block-auditor, pyresult-propagation-checker | Safety-critical (highest value) |
| **2B** | gil-discipline-checker, panic-safety-checker | GIL and panic safety |
| **2C** | pyclass-traits-checker, pyclass-protocol-checker, module-init-checker | PyO3-specific correctness |
| **2D** | lifetime-handle-checker, pyo3-version-compat-scanner | Compatibility and migration |
| **2E** | rust-complexity-analyzer | Code quality |
| **2F** | parity-checker | Cross-implementation behavior — only when discovery's `has_python_source` is true |
| **2G** | git-history-analyzer | Churn + similar-bug detection — runs last, uses all prior findings |

Groups run sequentially (2G must be last). With `parallel`, agents *within* a group run concurrently, at most `--max-parallel` at once.

### Phase 3 — Synthesis

Deduplicate, resolve conflicts, and produce a unified report.

> **Before sharing this report externally** (filing issues, opening PRs, contacting a maintainer), read [`WORKING_WITH_MAINTAINERS.md`](../../../WORKING_WITH_MAINTAINERS.md). A large unsolicited findings report is a burden, not a gift: ask the maintainer first, do not auto-file or auto-PR, and disclose security findings privately. The synthesis below is an internal artifact; sharing it is a separate human decision.

**Deduplication:** when two agents flag the same `file:line`, merge them and list both agents. Attribute to the most specific agent (a `#[pymethods]` signature issue → pyclass-protocol-checker, not pyresult). Surface genuine disagreements as **Tensions**.

**Summary template:**

```markdown
# PyO3 Extension Review — [crate]

## Scope: [what was analyzed]   ## Agents: [list]

## Executive Summary
[3–5 sentences: overall health, most critical findings, key recommendations]

## Crate Profile
- Module: [name]  ([N] .rs files)
- PyO3: [version]  | features: [list]  | Python floor: [N]
- Build backend: [maturin / setuptools-rust / cargo]  | Free-threading target: [yes/no]

## Key Metrics
| Dimension | Status | FIX | CONSIDER | Top Finding |
|-----------|--------|-----|----------|-------------|
| Unsafe Soundness | G/Y/R | N | N | [1-line] |
| PyResult Propagation | G/Y/R | N | N | [1-line] |
| GIL Discipline | G/Y/R | N | N | [1-line] |
| Panic Safety | G/Y/R | N | N | [1-line] |
| Pyclass Traits | G/Y/R | N | N | [1-line] |
| Pyclass Protocol | G/Y/R | N | N | [1-line] |
| Module Init | G/Y/R | N | N | [1-line] |
| Lifetime Handles | G/Y/R | N | N | [1-line] |
| PyO3 Version Compat | G/Y/R | N | N | [1-line] |
| Complexity | G/Y/R | - | N | [1-line] |
| Rust/Python Parity | G/Y/R | N | N | [1-line] |

G = no FIX findings | Y = 1–3 FIX | R = 4+ FIX

## Findings by Priority
Number ALL findings sequentially across sections (FIX first, then CONSIDER, then POLICY) so "Finding 12" is referenceable.

### Must Fix (FIX) — N
| # | Finding | File:Line | Agents |
|---|---------|-----------|--------|

### Should Consider (CONSIDER) — M
| # | Finding | File:Line |
|---|---------|-----------|

### Tensions
[Where agents disagree or a trade-off exists]

### Policy Decisions (POLICY) — P
| # | Finding |
|---|---------|

## Strengths
[Correct patterns the crate gets right]

## Recommended Action Plan
Immediate (FIX) → Short-term (CONSIDER) → Longer-term (POLICY), referencing finding numbers.
```

## Usage Examples

```
/rust-ext-review-toolkit:explore
/rust-ext-review-toolkit:explore src/
/rust-ext-review-toolkit:explore . unsafe pyresult gil panic
/rust-ext-review-toolkit:explore . all summary
/rust-ext-review-toolkit:explore . all deep parallel
```

## Tips

- Start with `summary` on an unfamiliar crate to get the lay of the land.
- `unsafe pyresult` finds the highest-impact bugs first.
- Before a PyO3 upgrade, use the `/migrate` command instead.
- For a large crate, `--max-parallel 3` speeds things up.
