# Real-world validation (Phase 7)

The toolkit's scanners were built against synthetic fixtures. Phase 7 ran them
against five real PyO3 extensions to measure the false-positive rate and
calibrate. This document records the run, the calibration changes it forced,
and the per-scanner assessment.

## Crates

| Crate | Scan root | PyO3 version | `.rs` files | Role (roadmap §7) |
|-------|-----------|--------------|-------------|-------------------|
| pydantic-core | repo root | 0.26 | 122 | Baseline noise level |
| cryptography | `src/rust/` | workspace (→ 0.28 assumed) | 101 | unsafe-block calibration |
| rust-numpy | repo root | 0.28 | 33 | Small sanity check |
| tokenizers | `bindings/python/` | 0.28 | 18 | `#[pyclass]` calibration |
| polars-python | `crates/polars-python/` | workspace (→ 0.28 assumed) | 111 | Runtime stress test |

Each of the ten Tree-sitter scanners was run via its `analyze()` entry point.
The roadmap §7 calibration threshold is **>50 findings on one crate** — at that
point a scanner is "more noise than signal".

## Final finding matrix

After the calibration changes below, no scanner exceeds the threshold on any
crate, and FIX-class findings total ~10 across all 50 scanner×crate cells.

| Scanner | pydantic-core | cryptography | rust-numpy | tokenizers | polars-python |
|---------|---------------|--------------|------------|------------|---------------|
| unsafe-blocks | 10 (4C 6A) | 18 (1F 5C 12A) | 34 (2F 28C 4A) | 7 (1F 6C) | 28 (2F 23C 3A) |
| pyresult | 11 C | 0 | 3 (1F 2C) | 1 C | 9 (3F 6C) |
| gil-discipline | 0 | 0 | 1 A | 0 | 3 C |
| panic-safety | 4 C | 37 C | 0 | 7 C | 36 C |
| pyclass-traits | 3 C | 9 C | 0 | 10 C | 1 C |
| pyclass-protocol | 17 C | 34 C | 0 | 14 C | 1 C |
| module-init | 11 C | 0 | 1 C | 0 | 3 C |
| lifetime-handles | 3 C | 1 C | 4 C | 5 C | 10 C |
| version-compat | 12 C | 0 | 2 C | 0 | 3 C |
| complexity | 26 C | 29 C | 1 C | 5 C | 21 C |

(F = FIX, C = CONSIDER, A = ACCEPTABLE. Counts are raw scanner output, before
agent triage — the agent confirms or downgrades each.)

Full scan time per crate is 3–9 s for ~100 files; the runtime stress target
(polars-python) completes all ten scanners in 6.7 s.

## Calibration changes Phase 7 forced

Validation surfaced one performance defect and five false-positive classes.
All six were fixed and the matrix above is post-fix.

| # | Scanner | Problem found on a real crate | Fix |
|---|---------|-------------------------------|-----|
| A | scan_pyresult, scan_panic_safety | `is_suppressed_by_comment` (a whole-tree walk) was called once **per call**, not per finding — quadratic on big files; scan_pyresult took 12–19 s. | Compute suppression lazily, only when a finding is emitted. 19 s → ~3 s. |
| B | scan_pyclass_protocol | `missing_traverse` / `missing_clear` were FIX. cryptography produced 34 FIX-class — almost all on classes that hold a leaf `Py<PyInt>` and cannot form a cycle. | Emit CONSIDER; the agent escalates to FIX once it confirms a cycle is reachable. Design §3.2.6 said FIX — flagged as a design-doc over-classification. |
| C | scan_pyo3-version-compat | The `_bound` call-suffix detector flagged `Py::into_bound` (current modern API) and stdlib `lower_bound`/`upper_bound` as `removed_api` FIX — 24 false FIX across pydantic-core, cryptography, polars. | Dropped `bound_suffix` auto-detection (`detect: skip`); the `_bound` suffix is not syntactically distinguishable. |
| D | scan_pyresult | `.len()` was a PyResult-shape hint, so `data.len().try_into().unwrap()` (a Rust integer conversion) was flagged `unwrap_on_pyresult` FIX. | Removed `.len()` from the hint set — it is the universal Rust collection method. |
| E | scan_pyclass-protocol | `getter_with_args` flagged getters that take their receiver by value (`self_: PyRef<Self>`, `this: Bound<'_, Self>`) — 7 false FIX across tokenizers and rust-numpy. | `getter_with_args` now treats the first parameter as the receiver when there is no `&self`, regardless of its type. |
| F | scan_pyresult | `.downcast` / `.cast` hints matched `Any::downcast_ref`, polars' `ChunkedArray::downcast_iter`, and `Series::cast` — false `unwrap_on_pyresult` FIX on polars. | Removed `.downcast` / `.cast` from the hint set; such `.unwrap()`s are still caught by panic-safety-checker at CONSIDER. |

## Per-scanner assessment

- **unsafe-block-auditor** — well-tuned. 1–2 FIX per crate (the design predicted "~0–2"); rust-numpy's 34 (28 CONSIDER) is correct — it is the NumPy FFI bridge and every `unsafe` block is a CONSIDER baseline.
- **pyresult-propagation-checker** — clean after D + F. polars' 3 FIX were spot-checked and are genuine (`.add_class().unwrap()`, `.getattr(...).unwrap()`).
- **gil-discipline-checker** — very quiet (0–3 findings). These mature crates use the GIL correctly; the scanner does not invent work.
- **panic-safety-checker** — cryptography and polars are genuinely panic-heavy (indexing, `.unwrap()`); 36–37 CONSIDER, under threshold, no FIX. Expected.
- **pyclass-traits / pyclass-protocol / module-init** — all CONSIDER after B/E. No FIX-class false positives remain.
- **lifetime-handle-checker** — quiet; these crates are on modern PyO3 and have migrated off the legacy handle API.
- **pyo3-version-compat-scanner** — clean after C. All remaining findings are CONSIDER (legitimate renamed/deprecated-API migration debt).
- **rust-complexity-analyzer** — a ranking, not a bug list; the CONSIDER counts are the hotspot tail and are expected.

## Conclusion

After six calibration changes, every scanner is under the noise threshold on
every crate, and the FIX-class rate is low and (on spot-check) genuine. The
toolkit is calibrated for v0.1. The validation was run against
pydantic-core, cryptography, rust-numpy, tokenizers, and polars-python — five
crates spanning small-to-very-large and PyO3 0.26–0.28.
