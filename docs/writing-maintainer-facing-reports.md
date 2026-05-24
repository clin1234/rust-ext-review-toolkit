# Writing Maintainer-Facing Reports

How to turn a rust-ext-review-toolkit review into a polished, shippable artifact
for the extension's maintainers — without burying them in an analytical document
they didn't ask for.

This is a living document. It draws on prior art in cext-review-toolkit's
[`writing-actionable-items-lists.md`](https://github.com/devdanzin/cext-review-toolkit/blob/main/docs/writing-actionable-items-lists.md)
and extends it with patterns the rust-ext-review-toolkit's first real-world reviews
exposed (scope-citation, channel-per-item, inline-plus-pointer reproducers,
small-list mode, free-threading pitfalls). It is **v0** — calibrated against one
worked example ([`reports/cryptography-rust_v1/`](../reports/cryptography-rust_v1/))
which is **unrepresentatively easy**: cryptography has unusually explicit scope
documentation, a small finding count, and very high code quality. Expect to
revise as we review more extensions.

> **Before sharing any maintainer-facing artifact, read
> [`WORKING_WITH_MAINTAINERS.md`](../WORKING_WITH_MAINTAINERS.md).** This guide
> assumes that social contract; it covers only *what* to write, not *whether*.

---

## Purpose

A maintainer-facing report is a **decision document for action**, not a summary
of the review. The maintainer should be able to work top-to-bottom and ship
fixes without reading anything else.

It is sharply distinct from the internal review:

| Artifact | Audience | Tone | Lives in |
|---|---|---|---|
| `REVIEW.md` | Reviewers, you in 6 months | Dense analytical synthesis | `reports/<crate>_v1/REVIEW.md` |
| `REVIEW_appendix.md` | Reviewers | Per-finding reproducer evidence | `reports/<crate>_v1/REVIEW_appendix.md` |
| `findings/*.md` | Reviewers | Per-agent raw detail | `reports/<crate>_v1/findings/` |
| **`<crate>_actionable_items.md`** | **A named maintainer** | **Tactical, directive** | `reports/<crate>_v1/` |

The actionable-items doc is the **only** artifact intended to leave the toolkit
machine. Everything else exists so this one can be honest.

---

## When to write one

Write one when **all** of these hold:

- You have both a `REVIEW.md` and a `REVIEW_appendix.md` with verdicts on every
  reproducible finding.
- The findings are non-trivial enough to warrant a curated list — even 4 items
  qualify if the curation is meaningful (see "small-list mode" below).
- You know who the maintainer is (named human or "<crate> maintainers" if a
  team).
- You have explicit social cover to share — see `WORKING_WITH_MAINTAINERS.md`.

**Don't** write one when:

- Findings ship as a single PR — open the PR.
- The report is still being calibrated.
- You haven't run the reproducer harness end-to-end.
- The verdicts on reproducible findings are still TODO.

### Small-list mode (<10 items)

The cext convention assumes ~15+ items across multiple tiers. For ≤10 items
(cryptography landed at 5), collapse to a single tier-less list, drop the
PR-grouping appendix, and integrate the "beyond scope" content as a section
rather than a separate file (see the "Beyond scope" section below for the
in-file vs separate-file decision). The structure should compress with the
content, not stay rigid.

---

## Pre-flight protocol

The cryptography review's local checkout HEAD was **10 weeks** older than the
day of review — the original work landed against stale code. Always run the
pre-flight, even if the checkout *feels* current.

### The four checks

1. **Fetch and diff upstream.**
   ```bash
   cd <checkout>
   git fetch --depth 500 origin main           # depth covers months of activity
   git log HEAD..origin/main --pretty='%h %ad %s' --date=short -- \
       <files your findings touch>
   ```
   Any commit that fixes a finding moves it to DONE; you do not request fixes
   that already shipped.

2. **`gh search` open issues for each finding's keywords.**
   ```bash
   for q in "padding panic" "block_size" "declarative_asn1" ...; do
       echo "---[$q]---"
       gh search issues "$q" --repo <owner>/<repo> --state open --limit 5
   done
   ```
   Empty results are common. If the maintainer already has it on the tracker,
   either link to that issue (don't open a duplicate) or skip the item.

3. **`gh search` open PRs** for the same keywords. If a fix is in flight, your
   item becomes "validate this PR" rather than "please write one."

4. **Sanity-check `gh search`.** Run a deliberately broad query known to match
   (e.g. `gh search issues "<top-level crate concept>" --state open --limit 3`)
   and verify it returns results. Empty results across all your narrow queries
   could mean "no overlap" — or it could mean `gh` is misconfigured. Verify.

### Pre-flight section in the document

Record what you did in the document itself:

```markdown
## Pre-flight (YYYY-MM-DD)

- Local checkout fetched to `origin/main` `<hash>` (date). N commits touched
  reviewed files; none fixes Findings <list>. Relevant adjacent context noted
  per item.
- No open issue or open PR matches any of these findings (searches: <list>,
  plus PR variants).
- Reproducers in [`reproducers/repro_<crate>.py`](reproducers/repro_<crate>.py)
  run on Python <version>; F<N> requires a free-threaded interpreter.
```

This earns trust before the maintainer reads a single item. They know you
checked.

---

## Item anatomy

A maintainer-facing item should compose like a polished GitHub issue with the
extra metadata stripped out. Required fields, in order:

```markdown
## N. <Verb-first title, GitHub-issue-ready>

- **Severity:** CRITICAL / HIGH / MEDIUM / LOW / POLICY
- **Scope citation:** [optional — the maintainer's own doc clause that puts this in scope]
- **Channel:** regular GitHub issue → PR / security advisory / mailing list / heads-up
- **Source:** REVIEW.md Finding N · appendix F<N>

**Where**
file:line (Rust panic site, Python validator site, ...)

**Why**
2–3 sentences. Concrete user impact first. Specific over general
("`md.update(other)` drops ~2% of entries" > "data loss under concurrency").

**Current code** *(optional, when it clarifies the fix)*
Short snippet, 5–10 lines.

**Reproducer**
Inline minimal runnable code. Expected output as comments. Pointer to the harness.

**Fix** *or* **Proposed approach**
Minimal patch (≤20 lines) when you're confident. "Proposed approach" when
direction-only.

**Upstream context**
One sentence on what recent maintainer activity in the area looks like — fits
this fix into where they are now.
```

Skip any field that has no content; do not pad. The cext doc's guidance on
"don't treat the list as a second report" still holds: items are **terse**,
details live in the internal review.

### Severity scale (rust-ext-review-toolkit)

| Severity | Means |
|---|---|
| CRITICAL | Process abort, memory unsafety, security bypass, undefined behavior on a reachable path |
| HIGH | Reachable from documented public API → uncatchable `PanicException` |
| MEDIUM | Reachable bug with a workaround, leak, wrong exception type |
| LOW | Performance / consistency / build-config |
| POLICY | Maintainer-acknowledged trade-off; heads-up only |

A `PanicException` is `BaseException`, not `Exception` — `except Exception:`
does not catch it. Treat that fact as user impact; many maintainers haven't
internalised it.

### Classifications (rust-ext-review-toolkit)

| Classification | Means | Where to look first |
|---|---|---|
| `PANIC` | Uncatchable `PanicException` from a public API path | panic-safety-checker |
| `GC-LEAK` | Missing `__traverse__` on a `#[pyclass]` that can cycle | pyclass-protocol-checker |
| `GIL` | Interpreter held during heavy work | gil-discipline-checker |
| `RACE` | Data race / UB on shared state | unsafe-block-auditor + ft work |
| `BUILD` | `Cargo.toml` / build-config issue | pyo3-version-compat-scanner |
| `COMPAT` | PyO3 deprecated / migration debt | pyo3-version-compat-scanner |
| `API` | Handle-kind / lifetime / modern-idiom drift | lifetime-handle-checker |

---

## The scope-citation pattern

When the maintainer's own documentation explicitly classifies a finding type,
**cite the clause**. This is the strongest possible in-scope argument: it is
the maintainer's stated policy, not your opinion.

Worked example from cryptography:

> `docs/security.rst` (their security policy): *"An uncaught `PanicException`
> from `pyo3` … bugs that should be filed as regular issues."*
> → validates Findings 1, 2, 3, 5 as in-scope as regular issues.

> `docs/api-stability.rst`: *"Types in cryptography are not intended to be
> sub-classed."*
> → puts Finding 9 (subclass-only leak) **out of scope.**

> `docs/security.rst`'s "memory unsafety or undefined behavior" line — the
> bright line between regular-issue and security-advisory.
> → routes Finding 11 (a data race) to the security advisory channel.

Where to look in a typical extension:

| File | What it says |
|---|---|
| `security.md` / `SECURITY.md` | What counts as a vulnerability; reporting channel |
| `docs/limitations.md` | What the project explicitly does *not* guarantee |
| `docs/api-stability.md` | Deprecation policy; subclassing; pre-stable features |
| `CONTRIBUTING.md` / `docs/development/submitting-patches.md` | Patch shape, channels, test policy |

If the project has none of these, scope-cite the **community consensus** (a PEP,
common Python convention, PyO3 documentation). Be honest if the scope is weak —
write "Why we believe this is in scope:" prose rather than fabricating a
citation.

If the maintainer's docs put a finding **out of scope**, surface it in the
"Beyond scope" section with the citation. Curation transparency builds trust.

---

## The channel-per-item pattern

Different items belong on different channels:

| Channel | Use when |
|---|---|
| Regular GitHub issue → PR | Ordinary bug, scope is in. Most items. |
| Security advisory page | Memory unsafety, UB, undocumented security-adjacent limitations. |
| Mailing list / discussion | "Larger changes" — refactors, design questions. |
| Heads-up only (this doc) | Maintainer-accepted trade-offs where you want to share new evidence without requesting action. |

When the channel differs across items, surface it:

- Top-of-file table column.
- Per-item `Channel:` field.
- For security advisory items, repeat the channel note inside the item — the
  reader may skim straight to the item without seeing the header.

Cryptography example: Findings 1, 2, 3, 5 → regular issue; Finding 11 →
security advisory if escalated.

---

## Reproducers — style

The cryptography review settled this question after one round of revision: the
reproducer must be **both** inline-readable *and* harness-runnable.

### The two roles

1. **Inline minimal** — a complete, self-contained Python block the maintainer
   can read and *see* the bug without running anything. Expected output as
   `#`-comments inside the block.
2. **Full harness pointer** — `python3 repro_<crate>.py --run F<N>`. Used when
   the maintainer wants to re-run against their own checkout, branch, or fix
   attempt.

Both, every item. The inline alone is enough to convince; the harness exists
for verification and CI integration.

### Anti-patterns

- **Pointer alone.** "Run `python3 repro.py --run F2`" without showing what
  happens. Forces the maintainer to set up the environment to *understand* the
  finding. They won't.
- **Inline 100-line stress test** when 10 lines demonstrate the bug. Minimal
  is part of the contract.
- **Inline depends on the toolkit.** No `from cext_review_toolkit import ...`,
  no in-tree helpers. The block must paste into a clean Python session and run.

### Reproducer-specific rules

- **`PanicException` reproducers**: no subprocess isolation needed for the
  inline minimum. PyO3 catches the panic; the process survives. The harness
  may still isolate for safety.
- **Free-threading race reproducers**: stress-test pattern with two known
  states + torn-read detection. Use **per-thread counters** (locals summed at
  end) or `list.append` (atomic via CPython's list lock under free-threading).
  **Do not** use shared `dict[k] += 1` — the increment is racy and your output
  will misreport.
- **Free-threading reproducers must hard-exit**: stress reproducers that spawn
  threads can stall interpreter shutdown. End with `os._exit(0)` (after
  flushing stdout) in the harness; for the inline minimum, `daemon=True`
  threads + `time.sleep` after `stop.set()` is usually enough.
- **Leak reproducers**: `weakref.ref` + `gc.collect()` + `assert wr() is not
  None`. Show a control cycle that *is* collected, so the maintainer sees the
  delta isn't an artefact of how Python GC works.
- **Perf reproducers**: time a workload on 1 thread vs N threads, and include
  a control workload known to behave differently. Ratio comparisons are
  noise-resistant; absolute timings aren't.

### Expected-output format

Inline the expected output as `#`-comments **inside** the code block, not in
prose afterwards. The maintainer's eye should stay on the block.

```python
padding.PKCS7(0).padder().finalize()
# pyo3_runtime.PanicException: attempt to calculate the remainder with
# a divisor of zero
```

For free-threading reproducers where the output depends on the interpreter
build, document both:

```python
# Free-threaded build: torn-rate ≈ 95–100 %.
# GIL build (same code): torn-rate = 0 % — the GIL serializes the read.
```

---

## The "Beyond scope" section

The maintainer-facing doc earns trust partly by what it **doesn't** request.
Include a compact "Beyond scope — reviewed and not requested" section that
lists every finding from the internal review that didn't make the actionable
list, with one-line rationale per item.

Categories:

| Category | Example |
|---|---|
| Retracted by reproduction | F4 — `load_der_ocsp_response` validates upstream; assert unreachable. |
| Out of scope per maintainer docs | F9 — `api-stability.rst` says no subclassing. |
| Gated by another item | F6 — closed by F2's fix. |
| Code-quality / refactor | F10 complexity — mailing-list territory per `submitting-patches.rst`. |
| Sound (reviewed, no bug) | F8 — 10 non-`frozen` `#[pyclass]`es, all `Send + Sync`. |

One line per item. No "future work" speculation; this is a list of what was
*evaluated and declined*, not a wish list.

### Format: in-file section vs separate companion file

Two valid layouts; pick by size and weight:

| Mode | When to use | Where it lives |
|---|---|---|
| **In-file `## Beyond scope` section** | Small list (≤10 actionable items). The not-requested list is shorter than the actionable list. Cryptography v0 used this. | Bottom of `<crate>_actionable_items.md` |
| **Separate `<crate>_BEYOND_SCOPE.md` companion** | Larger lists (15+ items) or when the not-requested list is itself substantial. Scope-cited declines deserve discussion-grade prose (citing the doc clause, summarizing why it was evaluated). | Sibling file in `reports/<crate>_v1/` |

cext-review-toolkit defaults to the separate-file pattern (see ujson). Switch
to it when **any** of these hold:

- The "Beyond scope" list outweighs the actionable list.
- Several items need a paragraph of rationale, not a one-liner (e.g.
  scope-cited subclass-only bugs the maintainer's docs explicitly forbid, but
  which were genuinely investigated and need a record).
- The actionable list is short enough that adding a 30-line scope-discussion
  section at the bottom would dwarf the items themselves.

Cross-link in both directions:

- Actionable doc: at the end of its in-file "Beyond scope" section (kept as a
  brief stub of categories + counts), link to the companion file:
  `See [BEYOND_SCOPE.md](<crate>_BEYOND_SCOPE.md) for the full per-finding rationale.`
- Companion file: open with `Companion to [<crate>_actionable_items.md](<crate>_actionable_items.md). Lists findings from the internal review that were evaluated and not requested, with rationale.`

The companion file should follow the same per-item style discipline as the
main doc — terse rationale, scope citations, no inflated language. It is a
**curation artifact**, not a wishlist or a place to vent. If you don't have
material for it, don't ship an empty one; surface the categories in the main
doc's stub instead.

---

## Writing style

- **Maintainer-named greeting.** "For the pyca/cryptography maintainers" or
  "For <Name>" when there's a clear lead. Anonymous "the maintainer" tells the
  reader they aren't seen.
- **Second person.** Use "you" for the maintainer's actions; "we" for what the
  review team concluded.
  - Good: "You can land this as a one-line patch."
  - Bad: "The maintainer should land this as a one-line patch."
- **Specific over general.** "`scrypt` 2-thread/1-thread ratio = 1.98 vs RSA
  sign 1.06" beats "scrypt holds the GIL during derivation."
- **Honest about uncertainty.** `Proposed approach:` instead of `Fix:` when
  you're not sure. Maintainers trust honest uncertainty.
- **No inflated language.** Severity badge carries the load. No
  "catastrophic", "devastating", "game-changing." If the prose adds adjectives
  to the severity, drop them.
- **Date the document.** Recent activity is recent for a reason; the
  maintainer reading three months later should know which week's `main` you
  pre-flighted against.

---

## Sharing protocol

Per `WORKING_WITH_MAINTAINERS.md`:

- Do not auto-file issues. Do not auto-open PRs.
- Ask the maintainer if they want a maintainer-facing report at all, in what
  shape, and at what time.
- Disclose security-channel items privately. The actionable items doc may sit
  in a private fork or as a shared draft; do not commit it to a public repo
  unless you have explicit permission.
- The doc is the **starting point** for the conversation, not the end of it.
  Expect the maintainer to redirect on scope, priority, channel, or whether
  to engage at all.

---

## Pitfalls (rust-ext-review-toolkit-specific)

Tripped on these during cryptography v1:

- **abi3 `.so` won't load on free-threaded Python.** A `cp3X-abi3` extension
  is built for the GIL-enabled ABI and cannot be imported by `python3.Xt`.
  Workaround: `pip install` the released wheel into the FT venv — recent
  cryptography ships `cp3Xt`-tagged wheels. This is itself useful evidence
  for the abi3 + free-threading scoping conversation.
- **`PanicException` semantics.** Subclasses `BaseException`, not
  `Exception`. State this explicitly in the item's "Why" — the difference
  determines whether a caller's existing exception handlers catch it.
- **`CffiBuf`-style buffer protocol issues are content races, not
  use-after-free.** The PyBuffer export blocks resize but not in-place
  content mutation. On free-threading, this becomes the common case rather
  than a rare window. Be precise.
- **Free-threaded counter races.** `counters["x"] += 1` from two threads
  drops increments. Use per-thread local summed after `join()`, or
  `list.append(1)` (atomic via the list lock).
- **Interpreter shutdown hang after stress reproducers.** A free-threaded
  stress test with live extension state can stall interpreter shutdown for
  minutes. Hard-exit with `os._exit(0)` (after `sys.stdout.flush()`).
- **Checkout staleness.** A checkout's HEAD date may have nothing to do with
  the day you run the review. Always pre-flight (`git fetch --depth N`).
- **`gh search` syntax drift.** `gh search issues` and `gh search prs` are
  separate; `--repo` takes `owner/name`. Sanity-check with a broad query
  before trusting an empty narrow query.

---

## Checklist

Before sharing a maintainer-facing report:

- [ ] Pre-flight done and recorded in the doc (`git fetch` + `gh search`).
- [ ] No item duplicates an open issue or open PR on the upstream tracker.
- [ ] Every item has Severity, Source, Where, Why, Reproducer, Fix (or
      Proposed approach).
- [ ] Every reproducer is inline-minimal and runnable from a clean Python
      session, with expected output as `#`-comments inside the block.
- [ ] Every item also points at the full harness (`python3 repro.py --run
      F<N>`).
- [ ] Free-threading reproducers handle counter-races and shutdown correctly.
- [ ] Items needing a different channel (security advisory, mailing list)
      surface that in both the top-of-file table and the per-item header.
- [ ] Where applicable, scope-citation pulled from the maintainer's docs.
- [ ] "Beyond scope" section lists everything reviewed and not requested,
      one line per item, with category.
- [ ] No inflated language. Severity carries the weight.
- [ ] Maintainer named in the greeting; second person throughout.
- [ ] `WORKING_WITH_MAINTAINERS.md` read; sharing decision is the
      maintainer's, not yours.
- [ ] Internal review (`REVIEW.md` + `REVIEW_appendix.md`) is **not** shared
      by default — it is the upstream of this document, not a deliverable.

---

## Templates

Copy-pasteable skeletons. Replace `<placeholders>` and trim what doesn't
apply.

### Top-of-file

```markdown
# <upstream/project> — Actionable Items

**For:** <named maintainer or "the <project> maintainers">.
**Distilled from:** [`REVIEW.md`](REVIEW.md) and [`REVIEW_appendix.md`](REVIEW_appendix.md).
**Status:** **Not yet shared.** Per `WORKING_WITH_MAINTAINERS.md`, sharing is the maintainer's call.

<N> items, each shippable as one small PR.

| # | Title | Severity | Channel |
|---|-------|----------|---------|
| 1 | ... | HIGH | issue + PR |
| ... |
```

### Pre-flight section

```markdown
## Pre-flight (YYYY-MM-DD)

- Local checkout fetched to `origin/main` `<hash>` (<date>). <N> commits touched reviewed files; none fixes the items below. Adjacent context noted per item.
- No open issue or open PR on `<owner>/<repo>` matches the items (searches: <list>).
- Reproducers in [`reproducers/repro_<crate>.py`](reproducers/repro_<crate>.py) — `python3 repro_<crate>.py --run F<N>`.
```

### Per-item

````markdown
## N. <Verb-first title>

- **Severity:** HIGH | MEDIUM | LOW | POLICY
- **Scope citation:** <quote from maintainer doc> — `<docfile>`
- **Channel:** regular GitHub issue → PR
- **Source:** REVIEW.md Finding N · appendix F<N>

**Where**

`<file:line>` (Rust panic site / Python validator site / ...).

**Why**

2-3 sentences. Concrete user impact first.

**Reproducer**

```python
# Minimal, runnable from a clean Python session.
# Expected output as comments.
```

Full harness: `python3 repro_<crate>.py --run F<N>`.

**Fix**

```rust
// Minimal patch, ≤20 lines.
```

**Upstream context:** <one sentence on recent maintainer activity in this area>.
````

### Beyond-scope row

```markdown
- **<Finding N> (<one-phrase description>)** — <category: Retracted / Out of scope / Gated / Refactor / Sound>. <One sentence rationale, with `<docfile>` citation if applicable.>
```

---

## Notes from extensions analyzed

### v0 — cryptography (2026-05)

- **Atypically clear scope.** `docs/security.rst` literally lists "uncaught
  `PanicException` from `pyo3`" as in-scope-bug-but-not-vulnerability, and
  `docs/api-stability.rst` says types aren't intended for subclassing. Most
  extensions won't be this explicit; expect to scope-cite community consensus
  more often.
- **Small list (5 items).** Forced the small-list-mode compression: no
  tiers, single file, "Beyond scope" as a section rather than a sibling
  document.
- **Per-item channel was essential.** Finding 11 (a `buf.rs` data race) is on
  the UB side of `security.rst`'s vulnerability bar — security-advisory
  channel — while the four panic findings are regular-issue. Without per-item
  channel routing the document would have been ambiguous about Finding 11.
- **Inline-plus-pointer settled the reproducer style.** First draft used
  pointer-only ("`python3 repro.py --run F2`"); the maintainer-facing version
  reverted to inline minimal + pointer. The pointer-only form fails the
  maintainer-confidence test — they shouldn't need to set up a Python
  environment to *see* the bug.
- **Pre-flight surfaced a key data point.** The checkout was 10 weeks stale.
  Eleven upstream commits had touched reviewed files; none fixed our findings,
  but several *expanded* declarative_asn1's surface (SET, SET OF, TLV
  decoding), which made Finding 1's leak class *larger*, not smaller. Without
  the pre-flight the doc would have under-stated the urgency.
- **Worked example.**
  [`reports/cryptography-rust_v1/cryptography_actionable_items.md`](../reports/cryptography-rust_v1/cryptography_actionable_items.md)
  — the v0 reference. Read it alongside this doc.

### v1 — polars (2026-05)

First **large-extension** review (110 `.rs` files, 27 FIX before triage). What v1
added to the methodology:

- **Separate `<crate>_BEYOND_SCOPE.md` companion validated.** Polars's actionable
  list landed at 8 items; the not-requested list was 50+ items (4 demotions, 1
  retraction, 11 source-validated-but-not-reproducer-validated FIXes, 11
  CONSIDER, 4 POLICY, 30+ engine-territory issues). The in-file `## Beyond
  scope` section would have dwarfed the actionable items. The separate-file
  pattern (now documented in the "Beyond scope" section above) is the right
  call when the not-requested list outweighs the actionable list, which is the
  typical shape for large or mature extensions.
- **AI usage policy is a new scope-citation surface.** Polars's
  [`AI_POLICY.md`](https://github.com/pola-rs/polars/blob/main/AI_POLICY.md)
  changed the recommended workflow per item: rather than "file issue + PR",
  the path is "file issue → wait for `accepted` label → PR with disclosure".
  Check for an `AI_POLICY.md` or equivalent at the top of every review; it
  affects channel routing more than security or contribution policies do,
  because it gates *contributor* PR cadence not just *external* reporters.
- **Reproducer-driven re-triage is significant credibility currency.** Of 27
  FIX findings, the harness produced verdicts on 13: 7 REPRODUCED, 4 DEMOTED
  (Python wrapper shields the Rust panic site), 1 RETRACTED (false positive
  of the history-analyzer pattern), and 1 NOT-REPRODUCED-but-kept (F6, needs
  internal-API access). The retraction (F26) is the most valuable signal —
  shipping 27 findings without it would have been 27 - 1 wasted-maintainer-
  time. **Always test the Python-wrapper shielding** (try the public-API call
  first, then the internal `_plr.*` bypass if shielded — the F17 pattern
  showed the Rust site is real even when the public API is safe). The
  "shielded but internal-bypass still panics" verdict is a real finding that
  the harness uniquely produces.
- **`exceptions.rst` (polars) is the analogue of `security.rst` (cryptography)**
  for `PanicException` framing. Polars's
  [`py-polars/docs/source/reference/exceptions.rst`](https://github.com/pola-rs/polars/blob/main/py-polars/docs/source/reference/exceptions.rst)
  documents `PanicException` as its own section alongside `Errors` and
  `Warnings`. This is an implicit scope-citation: the project recognizes the
  concept and frames it as an exception class users may see — a green light
  for "this is the kind of bug they accept on the regular tracker." When the
  project has no `security.md` clause about PanicException, an
  `exceptions.rst`/`.md` listing it as an official type is the next-best
  scope-citation. Look for it before defaulting to community consensus.
- **SECURITY policy's "segfault → ACE vector" was a direct scope-cite hit.**
  Polars's `SECURITY.md` text: *"segfaults are typically an indicator there
  is a vector for arbitrary code execution"*. The F1 reproducer (SIGSEGV
  from safe Python) maps onto this clause literally. Search every
  `SECURITY.md` for the word *segfault*; if present, it's usually the
  defining clause for soundness findings.
- **The "accepted" label workflow** matters for the recommended-action
  sequencing. Polars's `docs/source/development/contributing/index.md`
  explicitly says contributors should "comment on the issue to let others
  know" and pick "an issue with an `accepted` label". This makes the right
  cadence: **file issue with inline reproducer → wait for `accepted` → PR**
  rather than the cryptography pattern of bundling fixes into a single PR.
  Adapt the "Sharing protocol" section in the per-extension actionable doc
  to reflect this when the project has a labeled workflow.
- **Upstream PR-arc evidence is the strongest narrative.** Two upstream PRs
  (#26665, #26832) had landed partial fixes for patterns the review then
  identified the unfixed siblings of. Framing the actionable item as "this
  PR fixed X; here are Y and Z, same shape" is more credible than "we
  found these new bugs." Surface PR-arc completions in the actionable list's
  Item title where applicable (Item 3 of polars's list: "siblings of merged
  PR #26665").
- **Worked example.**
  [`reports/polars_v1/polars_actionable_items.md`](../reports/polars_v1/polars_actionable_items.md)
  +
  [`reports/polars_v1/polars_BEYOND_SCOPE.md`](../reports/polars_v1/polars_BEYOND_SCOPE.md)
  — the v1 reference. Read them alongside this doc; they exercise every
  pattern documented here.

*Future entries here as more extensions are reviewed.*
