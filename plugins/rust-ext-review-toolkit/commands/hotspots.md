---
description: "Find the worst functions in a Python extension written in Rust with PyO3. Use when the user asks where to focus review effort -- runs the safety-critical and complexity agents and ranks the riskiest code."
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# PyO3 Extension Hotspots

Answer one question: **where should review effort go first?** This runs the agents whose findings concentrate in specific functions, then ranks those functions by combined risk.

**Arguments:** "$ARGUMENTS"  (scope: path or glob; default `.`; optional external-tool flags below)

**Plugin root:** `<plugin_root>` is the `plugins/rust-ext-review-toolkit/` directory.

## Why these agents

A Rust/PyO3 extension's danger concentrates in `unsafe` blocks, panic-prone call paths, dropped `PyResult`s, and over-complex functions — **not** in reference counting (PyO3's type system handles that). So hotspots runs:

- **unsafe-block-auditor** — every `unsafe` block is a soundness obligation
- **pyresult-propagation-checker** — panics across FFI and swallowed exceptions
- **panic-safety-checker** — panics reachable from `#[pymethods]` / `extern "C"`
- **rust-complexity-analyzer** — the functions hardest to review

(This mix differs from cext-review-toolkit's hotspots — Rust's safety-critical surface is the `unsafe`/panic path, not refcount management.)

## Optional external tools

Follow `<plugin_root>/external-tools.md` for explicitly requested `--run-clippy`,
`--run-expand`, `--run-geiger`, or `--run-miri --miri-test NAME` and their
configuration/output options. Run once before preflight and pass the saved JSON
path to the mapper and dispatched agents. Reuse existing evidence when supplied.
Default to the source workflow; missing or failed tools do not block it and tool
counts/availability alone do not affect scores or classifications.

## Workflow

1. **Discovery** — `python <plugin_root>/scripts/discover_rust_ext.py [scope]`; print the crate profile. Stop if not a PyO3 extension.
2. **Preflight** — dispatch rust-codegen-mapper.
3. **Dispatch** the four agents above against the scope.
4. **Rank.** Group all findings by function. A function's risk score:
   - **+3** per FIX finding, **+1** per CONSIDER finding
   - **+2** if the rust-complexity-analyzer flagged it a `complex_function`
   - **×1.5** if the function carries both `unsafe` findings *and* a complexity flag — a complex `unsafe` function is the single most dangerous shape in the crate.

## Output

```markdown
# Hotspots — [crate]

## Top Functions to Review
| Rank | Function | File:Line | Risk | unsafe | panic | pyresult | complex |
|------|----------|-----------|------|--------|-------|----------|---------|
| 1 | `MyType::from_raw` | src/lib.rs:120 | 14 | 2 FIX | 1 | - | yes |
| 2 | ... | | | | | | |

## The Worst Three
For each of the top three, 2–3 sentences: what makes it dangerous, and the one change that would most reduce its risk.

## Where to go next
[Point at `/explore <scope> <aspects>` for the files containing the top hotspots.]
```

List the top 10–15 functions. This command finds *where* to look; `/explore` does the finding-by-finding audit.
