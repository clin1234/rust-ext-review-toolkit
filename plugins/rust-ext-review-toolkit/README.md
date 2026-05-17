# rust-ext-review-toolkit (plugin)

PyO3 Rust-extension analysis plugin for Claude Code. See the
[project README](../../README.md) for full documentation, and
[`CLAUDE.md`](../../CLAUDE.md) for the development guide.

> **Status:** pre-v0.1, under active construction. `agents/`, `commands/`, and
> `scripts/` are populated phase by phase per `IMPLEMENTATION_ROADMAP.md`.

## Plugin structure

```
rust-ext-review-toolkit/
├── .claude-plugin/plugin.json
├── README.md
├── agents/        # 13 agent prompt definitions (markdown)
├── commands/      # 4 command definitions: explore, health, hotspots, migrate
├── scripts/       # 15 Python analysis scripts
└── data/          # 8 JSON data files (PyO3 API tables, abi3 surface, ...)
```

## Agents — 13

- **Preflight (1):** rust-codegen-mapper
- **Safety-critical (4):** unsafe-block-auditor, pyresult-propagation-checker,
  gil-discipline-checker, panic-safety-checker
- **PyO3-specific (4):** pyclass-traits-checker, pyclass-protocol-checker,
  module-init-checker, lifetime-handle-checker
- **Compatibility (1):** pyo3-version-compat-scanner
- **Quality & history (2):** rust-complexity-analyzer, git-history-analyzer
- **Parity (1):** parity-checker (qualitative, no script)

## Scripts — 15

`rust_ts_utils.py` (Tree-sitter-rust core) + `scan_common.py` (shared utilities)
+ `discover_rust_ext.py` (crate discovery) + 11 analysis scripts + the external
tools integration. Every analysis script exposes
`analyze(target, *, max_files=0) -> dict` and outputs JSON to stdout.

## Data — 8 JSON files

PyO3 API tables and version matrix, the abi3-compatible API surface, the
non-Send/Sync type list, panic-prone stdlib APIs, PyO3 idioms, and the vendored
CPython stable-ABI / limited-API-header lists.
