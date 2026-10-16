# Optional external-tool evidence

The command orchestrator owns one external-tool run and passes its JSON path to
preflight and downstream agents. Reuse that report; do not launch the same tools
in each agent. A standalone agent may use this workflow if no report was supplied.
Run the source scanners regardless of missing tools, failed builds, or timeouts.

## Opt in deliberately

Presence on PATH is only a hint (rustup shims can exist without the component).
`deep` is not permission to run external tools. Run only tools explicitly requested
by the user or already authorized for this review. Do not repeatedly ask for
permission that was already given. Never install components, change toolchains,
modify Cargo features, or disable Miri checks to make a review pass.

The runner's default is cargo metadata plus availability detection. Metadata can
resolve/download dependencies and update Cargo.lock, though it does not compile.
Clippy, expansion, and Miri compile build scripts/proc macros; Miri also executes
tests. Geiger may resolve dependencies and scans their source. All are optional.

```bash
python <plugin_root>/scripts/run_external_tools.py <scope> \
  --run-expand --run-geiger --output-dir <report-dir>/external
```

Forward only requested flags: `--run-clippy`, `--run-expand`, `--run-geiger`,
`--run-miri`. For the latter three, `--output-dir` is required; each invocation
creates unique artifact subdirectories. `--package`, `--features`, and
`--no-default-features` select those three tools only, not clippy or metadata.
Use the actual reviewed build configuration; do not silently turn on all features.
Cargo tools operate on a crate, not the file subset or `--max-files` limit.
Save stdout JSON as `<report-dir>/external-tools.json` and pass that path to agents.

Miri requires an existing, configured nightly with Miri and its sysroot prepared,
and a fully qualified **pure-Rust library unit test** selected in advance:

```bash
RUSTUP_TOOLCHAIN=<installed-nightly> python <plugin_root>/scripts/run_external_tools.py <scope> \
  --run-miri --miri-test tests::buffer_roundtrip \
  --package <pure-rust-package> --output-dir <report-dir>/external
```

The runner invokes `cargo miri test --lib ... -- --exact <name>`. It does not run
all tests, integration tests, doctests, or set up Miri. Noninteractive stdin and
rustup auto-install disabled prevent interactive setup in this workflow. If a
project needs additional target flags or special environment setup, report that
limitation and use a separately authorized manual invocation with recorded logs.

**Push back on Miri for Python-calling tests:** ordinary CPython FFI is generally
unsupported. Use an existing isolated Rust test or skip Miri with the reason;
do not invent a passing test or replace Python calls with stubs. A nonzero exit
may mean unsupported FFI, setup trouble, a test assertion, or actual UB; read the
diagnostic. Even exit zero can mean zero tests matched or all were ignored: verify
the selected test actually ran before citing it. Passing tests cover only exercised
paths, never prove the extension sound or free-threading ready.

## Consume evidence, not availability

`tool_runs` maps each attempted evidence tool to `command`, `cwd`, `status`,
`returncode` when available, and `stdout_path` / `stderr_path` when logs could be
created. Status is `completed`, `failed`, `timeout`, or `error`. `skipped_tools`
explains tools not run or not completed. Read both logs; partial failed output is
not a completed analysis. The Cargo process has a 600-second timeout; no retries.
Keep logs private unless the user authorizes sharing them, just like source code.

| Evidence | Consumer | How it changes the review |
|---|---|---|
| cargo-expand stdout | rust-codegen-mapper, relevant PyO3 agents | Inspect generated wrappers/slot wiring for the selected configuration; map back to original macro/attribute locations. |
| cargo-geiger stdout | unsafe-block-auditor | Prioritize unsafe usage in the reviewed crate and distinguish dependency totals; no automatic findings or score changes. |
| Miri stdout + stderr | unsafe-block-auditor; gil/lifetime/panic agents when relevant | Corroborate a concrete source-level defect with the exercised test and diagnostic; distinguish UB from unsupported operations/build failures. |

Expanded text is a lossy debugging aid, not authoritative compilable source. Do
not feed it into source scanners or cite its line numbers as original source.
Geiger counts are inventory, not a measure of unsoundness: high counts are not
bugs, and zero counts are not proof of safety. Preserve FIX/CONSIDER/POLICY/
ACCEPTABLE rules; tools supply evidence, not classifications. No tool result or
absence alone changes health or hotspot scores. All agents must cite original
source and avoid duplicating scanner findings corroborated by external tools.

Record the command/configuration and relevant log excerpts in the preflight
handoff. When reporting unavailable/failed/skipped tools, say what remains
unverified; never describe an installed tool as having completed an analysis.

References: [Miri](https://github.com/rust-lang/miri#using-miri),
[cargo-expand limitations](https://github.com/dtolnay/cargo-expand#disclaimer),
[cargo-geiger intended use](https://github.com/geiger-rs/cargo-geiger#intended-use).
