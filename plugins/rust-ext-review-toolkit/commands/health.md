---
description: "Quick scored health dashboard for a Python extension written in Rust with PyO3. Use when the user wants a fast overall-quality read rather than a full review -- runs every agent in summary mode and scores each dimension 1-10."
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# PyO3 Extension Health Dashboard

Produce a quick, scored health read of a Rust/PyO3 extension. Every agent runs in **summary mode** — enough to score each dimension, not a full finding-by-finding audit.

**Arguments:** "$ARGUMENTS"  (scope only — path or glob; default `.`)

**Plugin root:** `<plugin_root>` is the `plugins/rust-ext-review-toolkit/` directory.

## Workflow

1. **Discovery** — run `python <plugin_root>/scripts/discover_rust_ext.py [scope]`; print the one-line crate profile. Stop if it is not a PyO3 extension.
2. **Preflight** — dispatch rust-codegen-mapper (it is fast and every agent benefits).
3. **Dimensions** — dispatch each agent in summary mode. Each runs its scanner and reports only counts (FIX / CONSIDER / POLICY) and the single worst finding.

| Dimension | Agent |
|-----------|-------|
| Unsafe Soundness | unsafe-block-auditor |
| PyResult Propagation | pyresult-propagation-checker |
| GIL Discipline | gil-discipline-checker |
| Panic Safety | panic-safety-checker |
| Pyclass Traits | pyclass-traits-checker |
| Pyclass Protocol | pyclass-protocol-checker |
| Module Init | module-init-checker |
| Lifetime Handles | lifetime-handle-checker |
| PyO3 Version Compat | pyo3-version-compat-scanner |
| Complexity | rust-complexity-analyzer |
| Rust/Python Parity | parity-checker (only when discovery reports `has_python_source`) |

## Scoring

Each dimension is scored **1–10**, starting at 10:

- **−3 per FIX finding** (a real bug / unsound code / a non-compiling API).
- **−1 per CONSIDER finding**, capped at −4 total.
- **−0 for POLICY / ACCEPTABLE** — these are not deductions.
- Floor the score at 1.

Map to a letter: **9–10 = A**, **7–8 = B**, **5–6 = C**, **3–4 = D**, **1–2 = F**.

## Output

```markdown
# Health Dashboard — [crate]

PyO3 [version] | features: [...] | Python floor: [N] | free-threading target: [yes/no]

| Dimension | Score | Grade | FIX | CONSIDER | Worst Finding |
|-----------|-------|-------|-----|----------|---------------|
| Unsafe Soundness | 7/10 | B | 1 | 0 | [1-line] |
| ... | | | | | |

**Overall: [average]/10 — [grade]**

## Headline
[2–3 sentences: the single most important thing to fix, and the crate's overall state.]

## Recommended next step
[Either "/explore . <aspects>" for the weakest dimensions, or "/migrate" if version-compat scored low, or "/hotspots" if complexity + unsafe both scored low.]
```

Keep it to one screen. For a finding-by-finding audit, point the user at `/explore`.
