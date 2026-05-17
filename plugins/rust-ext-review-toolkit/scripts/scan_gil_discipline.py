#!/usr/bin/env python3
"""Audit GIL discipline in Rust/PyO3 extension code.

PyO3 ties the right to touch Python objects to a `Python<'py>` token: holding
one means the interpreter is *attached* to the current thread. Releasing it
(`detach`, legacy `allow_threads`) lets other threads run but forbids any
Python work until the interpreter is re-attached. This scanner finds code that
breaks that discipline.

Vocabulary: PyO3 0.25 renamed the GIL API -- `Python::with_gil` -> `attach`,
`py.allow_threads` -> `detach`. The toolkit audits crates across 0.20-0.29, so
the scanner recognises **both** name sets.

Patterns detected (design SS3.2.3):
  1-3. A `.bind()` call or a raw `pyo3-ffi` call inside a `detach` /
       `allow_threads` closure -- Python work with no interpreter attached
       (`gil_violation_in_detach`).
  4.   A nested `Python::attach` / `with_gil` inside an already-attached
       scope (`nested_gil_attach`).
  5.   An `extern "C" fn` that uses PyO3 APIs without acquiring the GIL
       (`extern_c_pyo3_no_attach`).
  6.   `attach_unchecked` / `with_gil_unchecked` -- skips the interpreter-state
       check (`unchecked_attach`).
  8.   On a free-threading-targeted crate, a `#[pymodule]` that does not
       declare `gil_used` (`gil_used_undeclared`) -- classified version-aware.

Pattern 7 (long synchronous work that could release the GIL) is a
performance heuristic left to the agent's manual review.

Usage:
    python scan_gil_discipline.py [path] [--max-files N]
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_rust_ext import discover  # noqa: E402
from rust_ts_utils import (  # noqa: E402
    extract_attribute_targets,
    extract_fn_items,
    find_calls_in_scope,
    find_closure_bodies,
    parse_bytes,
    strip_comments,
    text_of,
)
from scan_common import (  # noqa: E402
    build_report,
    deduplicate_findings,
    discover_rust_files,
    enclosing_function_name,
    is_capi_symbol,
    is_in_region,
    is_suppressed_by_comment,
    make_finding,
    parse_common_args,
    pyo3_version_tuple,
    relative_path,
)

# GIL-API vocabulary -- both the current (0.25+) and legacy names.
_DETACH_NAMES = frozenset({"detach", "allow_threads"})
_ATTACH_NAMES = frozenset({"attach", "with_gil"})
_UNCHECKED_NAMES = frozenset({"attach_unchecked", "with_gil_unchecked"})
# Closures whose body runs with the interpreter attached (a `py` is in scope).
_GIL_HOLDING_CALLEES = _ATTACH_NAMES | _UNCHECKED_NAMES

# Substrings that mark a function body as doing Python work.
_PYO3_USAGE_MARKERS = (
    "pyo3::",
    "Python::",
    "Bound<",
    "Bound::",
    "Py<",
    "Py::",
    ".bind(",
    "PyResult",
    "PyErr",
)


def _scan_detach_closures(
    tree: object, source: bytes, stripped: bytes, rel: str
) -> list[dict]:
    """Pattern 1-3: Python work inside a GIL-released closure."""
    findings: list[dict] = []
    for closure in find_closure_bodies(tree, stripped, callee=_DETACH_NAMES):
        body = closure["body_node"]
        if body is None:
            continue
        # A nested attach/with_gil closure re-acquires the GIL -- its body is
        # a safe region inside the detached scope.
        safe_regions: list[tuple[int, int]] = []
        for inner in find_closure_bodies(body, stripped, callee=_GIL_HOLDING_CALLEES):
            inner_body = inner["body_node"]
            if inner_body is not None:
                safe_regions.append((inner_body.start_byte, inner_body.end_byte))

        violations: list[dict] = []
        for call in find_calls_in_scope(body, stripped):
            if is_in_region(call["node"].start_byte, safe_regions):
                continue
            leaf = call["name"]
            if leaf == "bind":
                kind = "bind_call"
            elif is_capi_symbol(leaf):
                kind = "ffi_call"
            else:
                continue
            violations.append(
                {"kind": kind, "callee": call["full_callee"], "line": call["line"]}
            )
        if not violations:
            continue
        node = closure["node"]
        fn_name = enclosing_function_name(node, source) or "<module>"
        findings.append(
            make_finding(
                "gil_violation_in_detach",
                classification="FIX",
                confidence="MEDIUM",
                description=(
                    f"A `{closure['callee']}` closure in `{fn_name}` performs "
                    f"Python work ({len(violations)} call(s)) with no "
                    f"interpreter attached -- `.bind()` and raw `pyo3-ffi` "
                    f"calls require an attached `Python<'py>` token."
                ),
                file=rel,
                line=closure["line"],
                column=node.start_point[1] + 1,
                function=fn_name,
                category="gil_discipline",
                fix_template=(
                    "Move the Python work outside the closure, or re-attach "
                    "with `Python::attach(|py| ...)` inside it."
                ),
                details={"closure_callee": closure["callee"], "violations": violations},
            )
        )
    return findings


def _has_outer_gil(
    node: Any, closure_callees: dict[int, str | None], source: bytes
) -> bool:
    """True if an `attach` call already runs with the interpreter attached."""
    current = node.parent
    while current is not None:
        if current.type == "closure_expression":
            callee = closure_callees.get(current.id)
            if callee in _DETACH_NAMES:
                return False  # the GIL was released before reaching here
            if callee in _GIL_HOLDING_CALLEES:
                return True
        elif current.type == "function_item":
            params = current.child_by_field_name("parameters")
            if params is not None and "Python<'" in text_of(params, source):
                return True
            return False  # the enclosing fn boundary -- no token in scope
        current = current.parent
    return False


def _scan_attach_calls(
    tree: object,
    source: bytes,
    stripped: bytes,
    rel: str,
    closure_callees: dict[int, str | None],
) -> list[dict]:
    """Patterns 4 and 6: nested `attach` and `attach_unchecked`."""
    findings: list[dict] = []
    for call in find_calls_in_scope(tree, stripped):
        leaf = call["name"]
        if "Python" not in call["full_callee"]:
            continue
        node = call["node"]
        fn_name = enclosing_function_name(node, source) or "<module>"
        suppressed = is_suppressed_by_comment(source, tree, call["line"])

        if leaf in _UNCHECKED_NAMES:
            findings.append(
                make_finding(
                    "unchecked_attach",
                    classification="ACCEPTABLE" if suppressed else "CONSIDER",
                    confidence="HIGH",
                    description=(
                        f"`{call['full_callee']}` in `{fn_name}` skips the "
                        f"interpreter-state check -- it does not guard against "
                        f"a finalized interpreter and is unsound if called "
                        f"after interpreter shutdown."
                    ),
                    file=rel,
                    line=call["line"],
                    column=node.start_point[1] + 1,
                    function=fn_name,
                    category="gil_discipline",
                    fix_template=(
                        "Use the checked `Python::attach` unless you can prove "
                        "the interpreter is initialised and not finalizing."
                    ),
                    details={"callee": call["full_callee"]},
                )
            )
        elif leaf in _ATTACH_NAMES and _has_outer_gil(node, closure_callees, stripped):
            findings.append(
                make_finding(
                    "nested_gil_attach",
                    classification="ACCEPTABLE" if suppressed else "CONSIDER",
                    confidence="MEDIUM",
                    description=(
                        f"`{call['full_callee']}` in `{fn_name}` runs inside an "
                        f"already-attached scope -- a nested attach is "
                        f"unnecessary and a deadlock risk on some platforms; "
                        f"reuse the outer `py` token."
                    ),
                    file=rel,
                    line=call["line"],
                    column=node.start_point[1] + 1,
                    function=fn_name,
                    category="gil_discipline",
                    fix_template=(
                        "Pass the outer `Python<'py>` token down instead of "
                        "re-attaching."
                    ),
                    details={"callee": call["full_callee"]},
                )
            )
    return findings


def _scan_extern_c(
    tree: object, source: bytes, stripped: bytes, rel: str
) -> list[dict]:
    """Pattern 5: `extern "C" fn` using PyO3 APIs without acquiring the GIL."""
    findings: list[dict] = []
    for fn in extract_fn_items(tree, stripped):
        if fn["extern_abi"] != "C":
            continue
        body = fn["body_node"]
        if body is None:
            continue
        body_text = text_of(body, stripped)
        calls = find_calls_in_scope(body, stripped)
        uses_pyo3 = any(m in body_text for m in _PYO3_USAGE_MARKERS) or any(
            is_capi_symbol(c["name"]) for c in calls
        )
        if not uses_pyo3:
            continue
        acquires_gil = any(
            c["name"] in _ATTACH_NAMES or c["name"] in _UNCHECKED_NAMES for c in calls
        )
        if acquires_gil:
            continue
        node = fn["node"]
        suppressed = is_suppressed_by_comment(source, tree, fn["start_line"])
        findings.append(
            make_finding(
                "extern_c_pyo3_no_attach",
                classification="ACCEPTABLE" if suppressed else "FIX",
                confidence="MEDIUM",
                description=(
                    f'`extern "C" fn {fn["name"]}` calls PyO3 APIs but never '
                    f"attaches the interpreter. A callback registered with a "
                    f"foreign C library may run on a thread with no GIL held; "
                    f"it must acquire one via `Python::attach` first."
                ),
                file=rel,
                line=fn["start_line"],
                column=node.start_point[1] + 1,
                function=fn["name"],
                category="gil_discipline",
                fix_template=(
                    "Wrap the body in `Python::attach(|py| { ... })`; guard "
                    "against a finalized interpreter for long-lived callbacks."
                ),
                details={"extern_abi": "C"},
            )
        )
    return findings


def _scan_gil_used(
    tree: object, source: bytes, stripped: bytes, rel: str, discovery: dict
) -> list[dict]:
    """Pattern 8: a free-threading-targeted crate that does not declare gil_used."""
    if not discovery.get("free_threading_target"):
        return []
    modern = pyo3_version_tuple(discovery.get("pyo3_version")) >= (0, 28)
    findings: list[dict] = []
    for hit in extract_attribute_targets(tree, stripped, "pymodule"):
        target = hit["target"]
        if target.type != "function_item":
            continue
        args_text = hit["attr"]["args_text"] or ""
        declares = "gil_used" in args_text
        body = target.child_by_field_name("body")
        if not declares and body is not None:
            declares = any(
                c["name"] == "gil_used" for c in find_calls_in_scope(body, stripped)
            )
        if declares:
            continue
        name_node = target.child_by_field_name("name")
        fn_name = text_of(name_node, source) if name_node is not None else "<module>"
        if modern:
            description = (
                f"The `#[pymodule]` `{fn_name}` does not declare `gil_used`. On "
                f"PyO3 0.28+ an undeclared module defaults to `gil_used = false` "
                f"-- it implicitly attests free-threading safety. If this crate "
                f"has not been audited for free-threading, opt out explicitly."
            )
            fix_template = (
                "Add `#[pymodule(gil_used = true)]` until the crate is audited "
                "for free-threaded safety."
            )
        else:
            description = (
                f"The `#[pymodule]` `{fn_name}` does not declare `gil_used`. On "
                f"PyO3 < 0.28 an undeclared module defaults to GIL-required and "
                f"is unavailable to free-threaded interpreters."
            )
            fix_template = (
                "Once audited, declare `#[pymodule(gil_used = false)]` to "
                "support free-threaded builds."
            )
        suppressed = is_suppressed_by_comment(source, tree, hit["line"])
        findings.append(
            make_finding(
                "gil_used_undeclared",
                classification="ACCEPTABLE" if suppressed else "CONSIDER",
                confidence="HIGH",
                description=description,
                file=rel,
                line=hit["line"],
                column=target.start_point[1] + 1,
                function=fn_name,
                category="free_threading",
                fix_template=fix_template,
                details={
                    "module": fn_name,
                    "pyo3_version": discovery.get("pyo3_version"),
                },
            )
        )
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan a Rust/PyO3 crate for GIL-discipline problems."""
    discovery = discover(target)
    project_root = Path(discovery["project_root"])
    files = discover_rust_files(Path(discovery["scan_root"]), max_files=max_files)

    findings: list[dict] = []
    functions_analyzed = 0
    for path in files:
        source = path.read_bytes()
        tree = parse_bytes(source)
        stripped = strip_comments(source)
        rel = relative_path(path, project_root)
        functions_analyzed += len(extract_fn_items(tree, stripped))
        closure_callees = {
            c["node"].id: c["callee"] for c in find_closure_bodies(tree, stripped)
        }
        findings.extend(_scan_detach_closures(tree, source, stripped, rel))
        findings.extend(
            _scan_attach_calls(tree, source, stripped, rel, closure_callees)
        )
        findings.extend(_scan_extern_c(tree, source, stripped, rel))
        findings.extend(_scan_gil_used(tree, source, stripped, rel, discovery))

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
