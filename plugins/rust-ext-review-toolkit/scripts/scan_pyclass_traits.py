#!/usr/bin/env python3
"""Audit `#[pyclass]` trait bounds in Rust/PyO3 extension code.

Since PyO3 0.23 a `#[pyclass]` requires `Send + Sync` (before 0.23, only
`Send`): PyO3 may move the object between threads and, under free-threaded
Python, share it. A field that is not `Send`/`Sync` makes the derive fail
unless the class opts out with `#[pyclass(unsendable)]`.

Patterns detected (design SS3.2.5):
  - a `#[pyclass]` struct with a field that breaks `Send`/`Sync` and no
    `unsendable` opt-out (`non_send_field`)
  - a deliberate `#[pyclass(unsendable)]` with such a field (`unsendable_pyclass`)
  - on a free-threading-targeted crate, a non-`frozen` `#[pyclass]` with
    `&mut self` methods (`missing_frozen_ft`)
  - `#[pyclass(extends = Parent)]` together with a `Drop` impl
    (`pyclass_extends_with_drop`)

`unsafe impl Send`/`Sync` detection lives in unsafe-block-auditor; this
scanner steps back when the class is covered by one. Missing
`__traverse__`/`__clear__` is pyclass-protocol-checker's domain.

Usage:
    python scan_pyclass_traits.py [path] [--max-files N]
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_rust_ext import discover  # noqa: E402
from rust_ts_utils import (  # noqa: E402
    extract_attribute_targets,
    extract_attributes,
    extract_fn_items,
    extract_impl_blocks,
    extract_struct_defs,
    parse_bytes,
    strip_comments,
    text_of,
)
from scan_common import (  # noqa: E402
    build_report,
    deduplicate_findings,
    discover_rust_files,
    is_suppressed_by_comment,
    load_data_file,
    make_finding,
    parse_attribute_options,
    parse_common_args,
    pyo3_version_tuple,
    relative_path,
    type_breaks_bound,
)


def _type_name(type_text: str) -> str:
    """Bare type name: strip generics and path qualification."""
    return type_text.split("<", 1)[0].strip().rsplit("::", 1)[-1]


def _has_mut_self_method(impl: dict, stripped: bytes) -> bool:
    """True if any method of an impl block takes `&mut self`."""
    body = impl["body_node"]
    if body is None:
        return False
    for fn in extract_fn_items(body, stripped):
        params = fn["params_node"]
        if params is not None and "&mut self" in text_of(params, stripped):
            return True
    return False


def _free_fn_mutates_pyclass(
    fn: dict, stripped: bytes, pyclass_names: set[str]
) -> str | None:
    """If a free `#[pyfunction]` takes `&mut <PyclassName>` as a parameter,
    return the pyclass name; else ``None``.

    A pyclass mutated via a free `#[pyfunction]` (rather than `&mut self` in
    `#[pymethods]`) is just as exposed to free-threading races as a method
    mutator -- the v0.1 detector missed this shape (cryptography's
    `LoadedProviders` is the canonical example).
    """
    params = fn["params_node"]
    if params is None:
        return None
    for child in params.children:  # type: ignore[attr-defined]
        if child.type != "parameter":
            continue
        ty = child.child_by_field_name("type")
        if ty is None:
            continue
        type_text = text_of(ty, stripped).replace(" ", "")
        if not type_text.startswith("&mut"):
            continue
        # `&mut PyClassName` -- strip the `&mut`, any lifetime, any generics.
        remainder = type_text[len("&mut") :].lstrip("'")
        # Drop a lifetime token like 'a if present (after lstrip removes the
        # apostrophe leaving e.g. `aPyClass`, fall through to identifier
        # extraction below -- we keep things simple and read the bare type
        # name from the original text).
        bare = text_of(ty, stripped).strip()
        # Strip `&mut`, optional `'lifetime`, then take leading identifier.
        bare = re.sub(r"^&\s*mut\s+(?:'\w+\s+)?", "", bare)
        m = re.match(r"([A-Za-z_]\w*)", bare)
        if m and m.group(1) in pyclass_names:
            return m.group(1)
        # Fall back: spaceless lookup for `&mut'aType` shape.
        if remainder in pyclass_names:
            return remainder
    return None


def _collect(parsed: list) -> dict:
    """First pass: gather struct fields, impls, pymethods, and #[pyfunction]
    free-fn mutators of `#[pyclass]` types -- crate-wide."""
    struct_fields: dict[str, list[dict]] = {}
    unsafe_send_sync: set[str] = set()
    drop_types: set[str] = set()
    pymethods: dict[str, tuple[dict, bytes]] = {}
    pyclass_names: set[str] = set()
    free_fn_mutators: set[str] = set()
    pyfunctions: list[tuple[dict, bytes]] = []
    for _rel, tree, _source, stripped in parsed:
        for struct in extract_struct_defs(tree, stripped):
            struct_fields.setdefault(struct["name"], struct["fields"])
        # `#[pyclass]` may decorate a struct or an enum; treat both as
        # pyclass names for the free-fn-mutator lookup below.
        for hit in extract_attribute_targets(tree, stripped, "pyclass"):
            tgt = hit["target"]
            name_node = tgt.child_by_field_name("name")
            if name_node is not None:
                pyclass_names.add(text_of(name_node, stripped))
        for impl in extract_impl_blocks(tree, stripped):
            tname = _type_name(impl["type"])
            trait = (impl["trait"] or "").rsplit("::", 1)[-1].strip()
            if impl["is_unsafe"] and trait in ("Send", "Sync"):
                unsafe_send_sync.add(tname)
            if trait == "Drop":
                drop_types.add(tname)
            if any(
                a["name"] == "pymethods"
                for a in extract_attributes(impl["node"], stripped)
            ):
                pymethods.setdefault(tname, (impl, stripped))
        # Collect `#[pyfunction]` free functions for a second pass once the
        # pyclass set is known.
        for hit in extract_attribute_targets(tree, stripped, "pyfunction"):
            tgt = hit["target"]
            if tgt.type != "function_item":
                continue
            # Recover the fn-item dict shape that `extract_fn_items` returns
            # so `_free_fn_mutates_pyclass` can read `params_node` directly.
            params_node = tgt.child_by_field_name("parameters")
            pyfunctions.append(({"params_node": params_node}, stripped))
    for fn, stripped in pyfunctions:
        mutated = _free_fn_mutates_pyclass(fn, stripped, pyclass_names)
        if mutated:
            free_fn_mutators.add(mutated)
    return {
        "struct_fields": struct_fields,
        "unsafe_send_sync": unsafe_send_sync,
        "drop_types": drop_types,
        "pymethods": pymethods,
        "pyclass_names": pyclass_names,
        "free_fn_mutators": free_fn_mutators,
    }


def _scan_pyclass(
    hit: dict, source: bytes, stripped: bytes, tree: object, rel: str, ctx: dict
) -> list[dict]:
    """Analyze one `#[pyclass]` struct."""
    target = hit["target"]
    if target.type != "struct_item":
        return []  # enum #[pyclass]: field-level analysis is out of scope for v0.1
    name_node = target.child_by_field_name("name")
    struct_name = text_of(name_node, stripped) if name_node is not None else ""
    options = parse_attribute_options(hit["attr"]["args_text"])
    fields = ctx["struct_fields"].get(struct_name, [])
    line = hit["line"]
    suppressed = is_suppressed_by_comment(source, tree, line)
    findings: list[dict] = []

    # Pattern 1: a field that breaks Send/Sync.
    offending: list[dict] = []
    for field in fields:
        reason = type_breaks_bound(field["type"], "Send", ctx["non_send_data"])
        bound = "Send"
        if reason is None and ctx["needs_sync"]:
            reason = type_breaks_bound(field["type"], "Sync", ctx["non_send_data"])
            bound = "Sync"
        if reason:
            offending.append(
                {
                    "field": field["name"],
                    "type": field["type"],
                    "bound": bound,
                    "reason": reason,
                }
            )
    if offending:
        first = offending[0]
        if options.get("unsendable"):
            findings.append(
                make_finding(
                    "unsendable_pyclass",
                    classification="POLICY",
                    confidence="HIGH",
                    description=(
                        f"`#[pyclass(unsendable)] {struct_name}` deliberately "
                        f"opts out of `Send`/`Sync` (field `{first['field']}: "
                        f"{first['type']}`). The class is single-thread only "
                        f"and PyO3 enforces that at runtime; it cannot be used "
                        f"under free-threaded Python."
                    ),
                    file=rel,
                    line=line,
                    column=target.start_point[1] + 1,
                    category="thread_safety",
                    fix_template=(
                        "Keep `unsendable` only if single-thread use is "
                        "intended; otherwise replace the field with a "
                        "thread-safe type."
                    ),
                    details={"struct": struct_name, "offending_fields": offending},
                )
            )
        elif struct_name not in ctx["unsafe_send_sync"]:
            findings.append(
                make_finding(
                    "non_send_field",
                    classification="FIX",
                    confidence="MEDIUM",
                    description=(
                        f"`#[pyclass] {struct_name}` has field "
                        f"`{first['field']}: {first['type']}`, which is not "
                        f"`{first['bound']}` ({first['reason']}). Since PyO3 "
                        f"0.23 a `#[pyclass]` requires `Send + Sync`; this "
                        f"will not compile without `#[pyclass(unsendable)]`."
                    ),
                    file=rel,
                    line=line,
                    column=target.start_point[1] + 1,
                    category="thread_safety",
                    fix_template=(
                        "Use a thread-safe field type (`Arc`/`Mutex` instead "
                        "of `Rc`/`RefCell`), or add `#[pyclass(unsendable)]` "
                        "if the class is intentionally single-thread."
                    ),
                    details={"struct": struct_name, "offending_fields": offending},
                )
            )
        # else: an `unsafe impl Send/Sync` covers it -> unsafe-block-auditor.

    # Pattern 3: missing `frozen` on a free-threading-targeted crate.
    if ctx["ft_target"] and not options.get("frozen"):
        pym = ctx["pymethods"].get(struct_name)
        mut_method = pym is not None and _has_mut_self_method(*pym)
        free_fn_mut = struct_name in ctx["free_fn_mutators"]
        if mut_method or free_fn_mut:
            mutation_via = (
                "`&mut self` method(s)"
                if mut_method
                else f"`&mut {struct_name}` parameter in a `#[pyfunction]`"
            )
            findings.append(
                make_finding(
                    "missing_frozen_ft",
                    classification="ACCEPTABLE" if suppressed else "CONSIDER",
                    confidence="MEDIUM",
                    description=(
                        f"`#[pyclass] {struct_name}` is mutated via "
                        f"{mutation_via} and is not `frozen`. On a "
                        f"free-threading crate, concurrent calls mutate "
                        f"shared state without synchronisation."
                    ),
                    file=rel,
                    line=line,
                    column=target.start_point[1] + 1,
                    category="free_threading",
                    fix_template=(
                        "Make the class `#[pyclass(frozen)]` and move mutable "
                        "state behind a `Mutex`/`RwLock` or atomics."
                    ),
                    details={
                        "struct": struct_name,
                        "mutated_via_method": mut_method,
                        "mutated_via_free_fn": free_fn_mut,
                    },
                )
            )

    # Pattern 4: `extends` + a `Drop` impl.
    if options.get("extends") and struct_name in ctx["drop_types"]:
        findings.append(
            make_finding(
                "pyclass_extends_with_drop",
                classification="ACCEPTABLE" if suppressed else "CONSIDER",
                confidence="MEDIUM",
                description=(
                    f"`#[pyclass(extends = {options['extends']})] {struct_name}` "
                    f"has a `Drop` impl. Subclass `Drop` ordering relative to "
                    f"the parent's deallocation is a known pitfall -- verify "
                    f"the subclass `Drop` runs before the parent is freed."
                ),
                file=rel,
                line=line,
                column=target.start_point[1] + 1,
                category="lifecycle",
                fix_template=(
                    "Confirm the `Drop` impl does not touch the parent's "
                    "state; prefer `__del__`-free designs where possible."
                ),
                details={"struct": struct_name, "extends": options["extends"]},
            )
        )
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan a Rust/PyO3 crate for `#[pyclass]` trait-bound problems."""
    discovery = discover(target)
    project_root = Path(discovery["project_root"])
    files = discover_rust_files(Path(discovery["scan_root"]), max_files=max_files)

    parsed: list[tuple[str, Any, bytes, bytes]] = []
    functions_analyzed = 0
    for path in files:
        source = path.read_bytes()
        tree = parse_bytes(source)
        stripped = strip_comments(source)
        rel = relative_path(path, project_root)
        parsed.append((rel, tree, source, stripped))
        functions_analyzed += len(extract_fn_items(tree, stripped))

    ctx = _collect(parsed)
    ctx["non_send_data"] = load_data_file("non_send_sync_types.json")
    ctx["needs_sync"] = pyo3_version_tuple(discovery.get("pyo3_version")) >= (0, 23)
    ctx["ft_target"] = discovery.get("free_threading_target", False)

    findings: list[dict] = []
    for rel, tree, source, stripped in parsed:
        for hit in extract_attribute_targets(tree, stripped, "pyclass"):
            findings.extend(_scan_pyclass(hit, source, stripped, tree, rel, ctx))

    findings = deduplicate_findings(findings)
    return build_report(discovery, findings, functions_analyzed=functions_analyzed)


def main() -> None:
    try:
        target, max_files = parse_common_args(sys.argv[1:])
        result = analyze(target, max_files=max_files)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:  # noqa: BLE001 -- top-level guard, emit JSON error
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
