#!/usr/bin/env python3
"""Audit panic safety across the FFI boundary in Rust/PyO3 extension code.

A Rust panic that unwinds into PyO3-managed code is caught and re-raised as a
`PanicException` -- almost never the maintainer's intent. A panic that unwinds
across a non-PyO3 `extern "C"` boundary instead **aborts the process**.

This scanner walks the bodies of `#[pyfunction]` / `#[pymethods]` /
`#[pymodule]` functions, the local helpers they call (one hop), and every
`extern "C" fn`, flagging panic-prone constructs (design SS3.2.4):

  - `.unwrap()` / `.expect()` / `.unwrap_unchecked()` / `unwrap_err` /
    `expect_err` on a **non-PyResult** value (`panic_unwrap`)
  - `[i]` slice / `Vec` / array indexing -- panics out of bounds (`index_panic`)
  - `panic!` / `todo!` / `unimplemented!` / `unreachable!` / `assert!` macros
    (`panic_macro`)
  - panicking `Vec`/slice methods such as `swap_remove`, `drain`, `split_at`
    (`slice_panic_method`)

`.unwrap()` on a `PyResult` is **out of scope here** -- that is
pyresult-propagation-checker's territory. Panic-prone calls inside an
`extern "C" fn` are escalated to FIX (process-abort risk). Integer-overflow
panics are not flagged (too pervasive; see `data/panic_unwind_apis.json`).

Usage:
    python scan_panic_safety.py [path] [--max-files N]
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_rust_ext import discover  # noqa: E402
from rust_ts_utils import (  # noqa: E402
    extract_attributes,
    extract_fn_items,
    extract_impl_blocks,
    find_calls_in_scope,
    find_enclosing,
    node_line,
    parse_bytes,
    strip_comments,
    text_of,
    walk,
)
from scan_common import (  # noqa: E402
    build_report,
    deduplicate_findings,
    discover_rust_files,
    is_suppressed_by_comment,
    load_data_file,
    looks_like_pyresult,
    make_finding,
    parse_common_args,
    relative_path,
)


def _classify(base: str, suppressed: bool, escalate: bool) -> str:
    """Apply the extern-C escalation and comment-suppression downgrade."""
    if escalate:
        return "FIX"
    if suppressed and base != "FIX":
        return "ACCEPTABLE"
    return base


def _snippet(node: Any, source: bytes, limit: int = 80) -> str:
    """One-line, whitespace-collapsed, length-capped text for a node."""
    text = " ".join(text_of(node, source).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _classify_fns(
    tree: object, source: bytes
) -> tuple[list[dict], list[dict], dict[str, dict]]:
    """Partition functions into (pyo3-callable, extern-"C", name->fn map)."""
    fns = extract_fn_items(tree, source)
    fn_by_name: dict[str, dict] = {}
    for fn in fns:
        fn_by_name.setdefault(fn["name"], fn)

    pymethods_impls: set[int] = set()
    for impl in extract_impl_blocks(tree, source):
        attrs = extract_attributes(impl["node"], source)
        if any(a["name"] == "pymethods" for a in attrs):
            pymethods_impls.add(impl["node"].id)

    pyo3_callable: list[dict] = []
    extern_c: list[dict] = []
    for fn in fns:
        if fn["body_node"] is None:
            continue
        if fn["extern_abi"] == "C":
            extern_c.append(fn)
            continue
        attrs = extract_attributes(fn["node"], source)
        callable_fn = any(a["name"] in ("pyfunction", "pymodule") for a in attrs)
        if not callable_fn:
            enclosing_impl = find_enclosing(fn["node"], "impl_item")
            callable_fn = (
                enclosing_impl is not None and enclosing_impl.id in pymethods_impls
            )
        if callable_fn:
            pyo3_callable.append(fn)
    return pyo3_callable, extern_c, fn_by_name


def _scan_body(
    body: object,
    source: bytes,
    stripped: bytes,
    tree: object,
    rel: str,
    fn_name: str,
    *,
    reached_via: str,
    via: str | None,
    panic_apis: dict,
) -> list[dict]:
    """Flag panic-prone constructs in one function body.

    ``source`` is the original bytes and ``tree`` the tree parsed from it --
    both are needed so comment-aware suppression sees real `// ...` text;
    ``stripped`` (comments blanked) drives pattern matching.
    """
    escalate = reached_via == "extern_c"
    boundary = (
        'a panic inside a non-PyO3 `extern "C"` callback aborts the process'
        if escalate
        else "a panic here is caught by PyO3 and raised as `PanicException`"
    )
    unwrap_methods = set(panic_apis.get("unwrap_methods", ()))
    slice_methods = set(panic_apis.get("panicking_slice_methods", ()))
    panic_macros = set(panic_apis.get("panic_macros", ()))
    findings: list[dict] = []

    def _details(extra: dict) -> dict:
        return {"reached_via": reached_via, "via_function": via, **extra}

    for call in find_calls_in_scope(body, stripped):
        if call["kind"] != "method":
            continue
        leaf, node = call["name"], call["node"]
        line = call["line"]
        # `is_suppressed_by_comment` walks the whole tree; call it only when a
        # finding is actually emitted, not once per method call (quadratic).
        if leaf in unwrap_methods:
            if looks_like_pyresult(call["receiver_text"] or ""):
                continue  # PyResult unwraps belong to pyresult-propagation-checker
            findings.append(
                make_finding(
                    "panic_unwrap",
                    classification=_classify(
                        "CONSIDER",
                        is_suppressed_by_comment(source, tree, line),
                        escalate,
                    ),
                    confidence="MEDIUM",
                    description=(
                        f"`.{leaf}()` on a non-`PyResult` value in `{fn_name}` "
                        f"panics if the value is `None`/`Err` -- {boundary}."
                    ),
                    file=rel,
                    line=line,
                    column=node.start_point[1] + 1,
                    function=fn_name,
                    category="panic_safety",
                    fix_template=(
                        "Handle the `None`/`Err` case explicitly (`match`, "
                        "`if let`, `?`, `ok_or_else`) instead of panicking."
                    ),
                    details=_details({"method": leaf}),
                )
            )
        elif leaf in slice_methods:
            findings.append(
                make_finding(
                    "slice_panic_method",
                    classification=_classify(
                        "CONSIDER",
                        is_suppressed_by_comment(source, tree, line),
                        escalate,
                    ),
                    confidence="LOW",
                    description=(
                        f"`.{leaf}()` in `{fn_name}` panics on a bad index/range "
                        f"when the receiver is a `Vec`/slice -- {boundary}."
                    ),
                    file=rel,
                    line=line,
                    column=node.start_point[1] + 1,
                    function=fn_name,
                    category="panic_safety",
                    fix_template=(
                        "Validate the index/range first, or use a non-panicking "
                        "API; ignore if the receiver is a map (it cannot panic)."
                    ),
                    details=_details({"method": leaf}),
                )
            )

    for node in walk(body, "index_expression"):
        line = node_line(node)
        suppressed = is_suppressed_by_comment(source, tree, line)
        findings.append(
            make_finding(
                "index_panic",
                classification=_classify("CONSIDER", suppressed, escalate),
                confidence="MEDIUM",
                description=(
                    f"`{_snippet(node, source)}` in `{fn_name}` panics if the "
                    f"index is out of bounds -- {boundary}."
                ),
                file=rel,
                line=line,
                column=node.start_point[1] + 1,
                function=fn_name,
                category="panic_safety",
                fix_template="Use `.get(i)` (returns `Option`) and handle `None`.",
                details=_details({"expression": _snippet(node, source)}),
            )
        )

    for node in walk(body, "macro_invocation"):
        macro = node.child_by_field_name("macro")
        name = text_of(macro, stripped).rsplit("::", 1)[-1] if macro is not None else ""
        if name not in panic_macros:
            continue
        line = node_line(node)
        suppressed = is_suppressed_by_comment(source, tree, line)
        base = "ACCEPTABLE" if name.startswith("debug_assert") else "CONSIDER"
        findings.append(
            make_finding(
                "panic_macro",
                classification=_classify(base, suppressed, escalate),
                confidence="HIGH",
                description=(
                    f"`{name}!` in `{fn_name}` is an unconditional panic point "
                    f"-- {boundary}."
                ),
                file=rel,
                line=line,
                column=node.start_point[1] + 1,
                function=fn_name,
                category="panic_safety",
                fix_template=(
                    "Return a `PyErr` (`PyResult`) instead of panicking; reserve "
                    "`unreachable!` for genuinely impossible states."
                ),
                details=_details({"macro": name}),
            )
        )
    return findings


def _scan_file(
    tree: object, source: bytes, stripped: bytes, rel: str, panic_apis: dict
) -> list[dict]:
    """Scan pyo3-callable bodies, their one-hop callees, and extern-C bodies."""
    pyo3_callable, extern_c, fn_by_name = _classify_fns(tree, stripped)
    callable_ids = {fn["node"].id for fn in pyo3_callable}
    extern_ids = {fn["node"].id for fn in extern_c}
    findings: list[dict] = []
    scanned: set[int] = set()

    for fn in pyo3_callable:
        findings.extend(
            _scan_body(
                fn["body_node"],
                source,
                stripped,
                tree,
                rel,
                fn["name"],
                reached_via="direct",
                via=None,
                panic_apis=panic_apis,
            )
        )
        scanned.add(fn["node"].id)

    # One hop: local free functions a pyo3-callable calls directly.
    for fn in pyo3_callable:
        for call in find_calls_in_scope(fn["body_node"], stripped):
            if call["kind"] != "function":
                continue
            callee = fn_by_name.get(call["name"])
            if callee is None or callee["body_node"] is None:
                continue
            cid = callee["node"].id
            if cid in scanned or cid in callable_ids or cid in extern_ids:
                continue
            findings.extend(
                _scan_body(
                    callee["body_node"],
                    source,
                    stripped,
                    tree,
                    rel,
                    callee["name"],
                    reached_via="one_hop",
                    via=fn["name"],
                    panic_apis=panic_apis,
                )
            )
            scanned.add(cid)

    for fn in extern_c:
        if fn["node"].id in scanned:
            continue
        findings.extend(
            _scan_body(
                fn["body_node"],
                source,
                stripped,
                tree,
                rel,
                fn["name"],
                reached_via="extern_c",
                via=None,
                panic_apis=panic_apis,
            )
        )
        scanned.add(fn["node"].id)
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan a Rust/PyO3 crate for panics reachable across the FFI boundary."""
    discovery = discover(target)
    project_root = Path(discovery["project_root"])
    files = discover_rust_files(Path(discovery["scan_root"]), max_files=max_files)
    panic_apis = load_data_file("panic_unwind_apis.json")

    findings: list[dict] = []
    functions_analyzed = 0
    for path in files:
        source = path.read_bytes()
        tree = parse_bytes(source)
        stripped = strip_comments(source)
        rel = relative_path(path, project_root)
        functions_analyzed += len(extract_fn_items(tree, stripped))
        findings.extend(_scan_file(tree, source, stripped, rel, panic_apis))

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
