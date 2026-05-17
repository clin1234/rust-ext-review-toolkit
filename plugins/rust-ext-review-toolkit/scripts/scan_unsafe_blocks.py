#!/usr/bin/env python3
"""Audit `unsafe` blocks in Rust/PyO3 extension code for soundness hazards.

Every `unsafe` block is a soundness obligation the compiler has stopped
enforcing -- the Rust analogue of cext-review-toolkit's borrowed-ref-across-call.
This scanner reports each `unsafe` block / `unsafe fn` and escalates the ones
carrying a recognisable hazard:

  - raw CPython C-API (`pyo3-ffi`) calls -- GIL + non-null + ownership duties
  - `transmute` involving PyO3 handle types -- almost always a soundness bug
  - raw `ob_refcnt` manipulation through `as_ptr()` pointers
  - private `_Py*` CPython symbol use -- not a stable surface
  - `unsafe impl Send` / `unsafe impl Sync` -- escalated when a field type is
    itself not Send/Sync

The scanner is deliberately high-recall: every `unsafe` block defaults to
CONSIDER (design SS6). The agent triage step confirms, escalates, or downgrades
each one, honouring adjacent `// SAFETY:` comments.

Out of scope here (covered by sibling scanners): manual buffer-protocol
`__getbuffer__` impls -> pyclass-protocol-checker; `extern "C"` callbacks into
foreign C libraries -> gil-discipline-checker; the full abi3 stable-ABI surface
-> pyo3-version-compat-scanner.

Usage:
    python scan_unsafe_blocks.py [path] [--max-files N]
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_rust_ext import discover  # noqa: E402
from rust_ts_utils import (  # noqa: E402
    extract_fn_items,
    extract_impl_blocks,
    extract_struct_defs,
    extract_unsafe_blocks,
    find_calls_in_scope,
    find_enclosing,
    parse_bytes,
    strip_comments,
    text_of,
)
from scan_common import (  # noqa: E402
    build_report,
    deduplicate_findings,
    discover_rust_files,
    is_capi_symbol,
    is_suppressed_by_comment,
    load_data_file,
    make_finding,
    parse_common_args,
    relative_path,
    type_breaks_bound,
)

# PyO3 handle-type markers, used to decide whether a `transmute` crosses
# handle types. `Py[A-Z]` matches `PyAny`/`PyDict`/... but not `Python`.
_HANDLE_MARKER_RE = re.compile(r"\bBound\b|\bBorrowed\b|\bPy<|\bPy[A-Z]\w*")

# A write to the raw `ob_refcnt` field (`+=`, `-=`, `*=`, or plain `=`).
_REFCOUNT_WRITE_RE = re.compile(r"\bob_refcnt\b\s*(?:\+=|-=|\*=|=(?!=))")

_TRANSMUTE_NAMES = frozenset({"transmute", "transmute_copy"})
_RAW_PTR_METHODS = frozenset({"as_ptr", "as_mut_ptr"})


def _snippet(node: Any, source: bytes, limit: int = 120) -> str:
    """One-line, whitespace-collapsed, length-capped text for a node."""
    text = " ".join(text_of(node, source).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _enclosing_fn_signature(block: dict, stripped: bytes) -> str:
    """Parameter list + return type of the fn enclosing an unsafe block."""
    node = block["node"]
    fn = node if block["kind"] == "fn" else find_enclosing(node, "function_item")
    if fn is None:
        return ""
    parts: list[str] = []
    for field in ("parameters", "return_type"):
        child = fn.child_by_field_name(field)
        if child is not None:
            parts.append(text_of(child, stripped))
    return " ".join(parts)


def _python_in_scope(block: dict, stripped: bytes) -> bool:
    """True if a `Python<'py>` token is a parameter of an enclosing fn/closure."""
    node = block["node"]
    current = node if block["kind"] == "fn" else node.parent
    while current is not None:
        if current.type in ("function_item", "closure_expression"):
            params = current.child_by_field_name("parameters")
            if params is not None and "Python<'" in text_of(params, stripped):
                return True
        current = current.parent
    return False


_DESCRIPTIONS = {
    "unsafe_block": (
        "`unsafe` block in `{fn}` carries a soundness obligation the compiler "
        "no longer enforces; review the invariants it relies on.",
        "Add a `// SAFETY:` comment justifying every operation, or replace the "
        "block with a safe PyO3 API.",
    ),
    "unsafe_ffi_call": (
        "`unsafe` block in `{fn}` calls raw CPython C-API function(s): {names}. "
        "Each requires the GIL held, valid non-null pointers, and correct "
        "borrowed/new/stolen-reference handling.",
        "Prefer the safe PyO3 wrapper (`Bound`/`Py` methods) over the raw "
        "`pyo3-ffi` call where one exists.",
    ),
    "unsafe_raw_pointer": (
        "`unsafe` block in `{fn}` dereferences raw pointer(s) obtained via "
        "`as_ptr()` / `as_mut_ptr()`.",
        "Confirm the pointer is non-null and outlives the dereference; prefer "
        "safe `Bound`/`Py` accessors.",
    ),
    "unsafe_handle_transmute": (
        "`transmute` involving PyO3 handle types in `{fn}` -- almost always a "
        "soundness bug; lifetimes and ownership are not interchangeable.",
        "Convert handles with `Py::bind(py)` / `Bound::unbind()` / "
        "`Bound::into_any()` instead of `transmute`.",
    ),
    "unsafe_refcount_manipulation": (
        "`unsafe` block in `{fn}` writes `ob_refcnt` through a raw pointer, "
        "bypassing PyO3's reference tracking.",
        "Use `Py`/`Bound` clone/drop semantics; never mutate `ob_refcnt` "
        "directly (it is also incorrect under free-threading).",
    ),
    "unsafe_private_capi": (
        "`unsafe` block in `{fn}` calls private CPython symbol(s): {names}. "
        "`_Py*` symbols are not a stable surface and may change between point "
        "releases.",
        "Replace the private `_Py*` call with a public C-API or PyO3 "
        "equivalent; mandatory if abi3 is targeted.",
    ),
}


def _scan_unsafe_block(
    block: dict,
    stripped: bytes,
    source: bytes,
    tree: object,
    rel: str,
    abi3: bool,
) -> dict:
    """Build one finding for a single `unsafe` block or `unsafe fn`."""
    body = block["body_node"]
    calls = find_calls_in_scope(body, stripped) if body is not None else []
    signature = _enclosing_fn_signature(block, stripped)
    sig_has_handle = bool(_HANDLE_MARKER_RE.search(signature))

    ffi_calls: list[dict] = []
    private_calls: list[dict] = []
    transmutes: list[dict] = []
    raw_ptr_uses: list[dict] = []
    for call in calls:
        leaf = call["name"]
        if leaf in _TRANSMUTE_NAMES:
            call_text = text_of(call["node"], stripped)
            is_handle = sig_has_handle or bool(_HANDLE_MARKER_RE.search(call_text))
            transmutes.append(
                {
                    "snippet": _snippet(call["node"], source),
                    "line": call["line"],
                    "handle_types": is_handle,
                }
            )
        if is_capi_symbol(leaf):
            entry = {"callee": call["full_callee"], "line": call["line"]}
            ffi_calls.append(entry)
            if leaf.startswith("_Py"):
                private_calls.append(entry)
        if call["kind"] == "method" and leaf in _RAW_PTR_METHODS:
            raw_ptr_uses.append(
                {
                    "receiver": call["receiver_text"],
                    "method": leaf,
                    "line": call["line"],
                }
            )

    body_text = text_of(body, stripped) if body is not None else ""
    refcount_manip = bool(_REFCOUNT_WRITE_RE.search(body_text))
    has_handle_transmute = any(t["handle_types"] for t in transmutes)

    if has_handle_transmute:
        ftype, classification, confidence = "unsafe_handle_transmute", "FIX", "MEDIUM"
    elif refcount_manip:
        ftype, classification, confidence = (
            "unsafe_refcount_manipulation",
            "FIX",
            "MEDIUM",
        )
    elif private_calls:
        ftype = "unsafe_private_capi"
        classification = "FIX" if abi3 else "CONSIDER"
        confidence = "HIGH"
    elif ffi_calls:
        ftype, classification, confidence = "unsafe_ffi_call", "CONSIDER", "HIGH"
    elif raw_ptr_uses:
        ftype, classification, confidence = "unsafe_raw_pointer", "CONSIDER", "HIGH"
    else:
        ftype, classification, confidence = "unsafe_block", "CONSIDER", "HIGH"

    safety_comment = is_suppressed_by_comment(source, tree, block["start_line"])
    if classification != "FIX" and safety_comment:
        classification = "ACCEPTABLE"

    fn_name = block["function"] or "<module>"
    desc_template, fix_template = _DESCRIPTIONS[ftype]
    names = ", ".join(
        f"`{c['callee']}`"
        for c in (private_calls if ftype == "unsafe_private_capi" else ffi_calls)
    )
    description = desc_template.format(fn=fn_name, names=names)

    node = block["node"]
    return make_finding(
        ftype,
        classification=classification,
        confidence=confidence,
        description=description,
        file=rel,
        line=block["start_line"],
        column=node.start_point[1] + 1,
        function=fn_name,
        category="soundness",
        fix_template=fix_template,
        details={
            "kind": block["kind"],
            "python_in_scope": _python_in_scope(block, stripped),
            "ffi_calls": ffi_calls,
            "private_capi_calls": private_calls,
            "transmutes": transmutes,
            "raw_pointer_uses": raw_ptr_uses,
            "refcount_manipulation": refcount_manip,
            "safety_comment": safety_comment,
        },
    )


def _scan_unsafe_impls(
    tree: object,
    stripped: bytes,
    source: bytes,
    rel: str,
    struct_fields: dict[str, list[dict]],
    non_send_data: dict,
) -> list[dict]:
    """Flag every `unsafe impl Send` / `unsafe impl Sync`."""
    findings: list[dict] = []
    for impl in extract_impl_blocks(tree, stripped):
        if not impl["is_unsafe"]:
            continue
        trait = (impl["trait"] or "").rsplit("::", 1)[-1].strip()
        if trait not in ("Send", "Sync"):
            continue
        type_name = impl["type"].split("<", 1)[0].strip().rsplit("::", 1)[-1]
        fields = struct_fields.get(type_name)
        offending: list[dict] = []
        if fields:
            for field in fields:
                reason = type_breaks_bound(field["type"], trait, non_send_data)
                if reason:
                    offending.append(
                        {
                            "field": field["name"],
                            "type": field["type"],
                            "reason": reason,
                        }
                    )
        safety_comment = is_suppressed_by_comment(source, tree, impl["start_line"])
        if offending:
            classification, confidence = "FIX", "MEDIUM"
            first = offending[0]
            description = (
                f"`unsafe impl {trait} for {type_name}` is unsound: field "
                f"`{first['field']}: {first['type']}` is {first['reason']}."
            )
            fix_template = (
                f"Remove the `unsafe impl {trait}`, or change the field to a "
                f"`{trait}` type (e.g. `Arc`/`Mutex` instead of `Rc`/`RefCell`)."
            )
        else:
            classification = "ACCEPTABLE" if (safety_comment and fields) else "CONSIDER"
            confidence = "HIGH"
            description = (
                f"`unsafe impl {trait} for {type_name}` manually asserts "
                f"thread-safety the compiler could not verify"
                + (
                    "."
                    if fields is not None
                    else "; the type definition was not found in the scanned files."
                )
            )
            fix_template = (
                "Document the invariant in a `// SAFETY:` comment and confirm no "
                "field can be observed unsynchronised from another thread."
            )
        node = impl["node"]
        findings.append(
            make_finding(
                "unsafe_send_sync_impl",
                classification=classification,
                confidence=confidence,
                description=description,
                file=rel,
                line=impl["start_line"],
                column=node.start_point[1] + 1,
                category="thread_safety",
                fix_template=fix_template,
                details={
                    "trait": trait,
                    "for_type": type_name,
                    "offending_fields": offending,
                    "type_definition_found": fields is not None,
                    "safety_comment": safety_comment,
                },
            )
        )
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan a Rust/PyO3 crate for `unsafe`-block soundness hazards."""
    discovery = discover(target)
    project_root = Path(discovery["project_root"])
    files = discover_rust_files(Path(discovery["scan_root"]), max_files=max_files)
    abi3 = any(
        str(feat).startswith("abi3") for feat in discovery.get("pyo3_features", [])
    )
    non_send_data = load_data_file("non_send_sync_types.json")

    parsed: list[tuple[str, Any, bytes, bytes]] = []
    struct_fields: dict[str, list[dict]] = {}
    functions_analyzed = 0
    for path in files:
        source = path.read_bytes()
        tree = parse_bytes(source)
        stripped = strip_comments(source)
        rel = relative_path(path, project_root)
        parsed.append((rel, tree, source, stripped))
        functions_analyzed += len(extract_fn_items(tree, stripped))
        for struct in extract_struct_defs(tree, stripped):
            struct_fields.setdefault(struct["name"], struct["fields"])

    findings: list[dict] = []
    for rel, tree, source, stripped in parsed:
        for block in extract_unsafe_blocks(tree, stripped):
            findings.append(
                _scan_unsafe_block(block, stripped, source, tree, rel, abi3)
            )
        findings.extend(
            _scan_unsafe_impls(
                tree, stripped, source, rel, struct_fields, non_send_data
            )
        )

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
