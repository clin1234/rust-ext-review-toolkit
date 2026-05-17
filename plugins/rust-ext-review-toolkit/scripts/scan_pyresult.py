#!/usr/bin/env python3
"""Audit `PyResult` propagation in Rust/PyO3 extension code.

A `PyResult<T>` carries a Python exception in its `Err` arm. Dropping that
error -- with `.unwrap()`, `.expect()`, `.ok()`, `let _ =`, or a bare
statement -- either panics across the FFI boundary or silently swallows a
Python exception. This scanner finds the syntactic shapes of those mistakes.

Patterns detected (design SS3.2.2):
  1. `.unwrap()` / `.expect()` on a PyResult-shaped receiver, inside a
     `#[pyfunction]` / `#[pymethods]` / `#[pymodule]` body
  2. `.ok()` on a PyResult-shaped receiver (discards the `PyErr`)
  3. `let _ = <pyresult>;` (drops the `PyErr`)
  4. a PyResult-returning call used as a bare statement without `?`
  5. exception clobbering -- a `PyErr` captured with `.err()`, with a later
     fallible (`?`) call before it is re-raised
  6. manual `PyErr` state juggling (`take`/`fetch`/`restore`) -- balance must
     be verified, especially inside `Drop` / `__traverse__` / `__clear__`
  7. `PyErr_Clear` -- silently clears the active exception

There is no type inference: "this expression is a `PyResult`" is inferred
syntactically (callee-name heuristics). False positives are expected and are
resolved by the agent triage step.

Usage:
    python scan_pyresult.py [path] [--max-files N]
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
    enclosing_function_name,
    is_in_region,
    is_suppressed_by_comment,
    looks_like_pyresult,
    make_finding,
    parse_common_args,
    relative_path,
)

# Method names that themselves return a PyResult -- used to flag a bare
# statement-position call whose result is dropped without `?`.
_PYRESULT_METHOD_NAMES = frozenset(
    {
        "call_method",
        "call_method0",
        "call_method1",
        "call0",
        "call1",
        "call",
        "getattr",
        "setattr",
        "delattr",
        "import",
        "import_bound",
        "extract",
        "downcast",
        "cast",
        "get_item",
        "set_item",
        "del_item",
        "eval",
        "try_iter",
        "add_class",
        "add_function",
        "add_submodule",
        "add",
        "add_wrapped",
    }
)

_PYERR_STATE_OPS = frozenset({"take", "fetch", "restore"})


def _pyo3_callable_ranges(tree: object, source: bytes) -> list[tuple[int, int]]:
    """Byte ranges of bodies of `#[pyfunction]`/`#[pymodule]`/`#[pymethods]` fns."""
    pymethods_impls: set[int] = set()
    for impl in extract_impl_blocks(tree, source):
        attrs = extract_attributes(impl["node"], source)
        if any(a["name"] == "pymethods" for a in attrs):
            pymethods_impls.add(impl["node"].id)

    ranges: list[tuple[int, int]] = []
    for fn in extract_fn_items(tree, source):
        body = fn["body_node"]
        if body is None:
            continue
        attrs = extract_attributes(fn["node"], source)
        callable_fn = any(a["name"] in ("pyfunction", "pymodule") for a in attrs)
        if not callable_fn:
            enclosing_impl = find_enclosing(fn["node"], "impl_item")
            callable_fn = (
                enclosing_impl is not None and enclosing_impl.id in pymethods_impls
            )
        if callable_fn:
            ranges.append((body.start_byte, body.end_byte))
    return ranges


def _enclosing_drop_or_gc(node: Any, source: bytes) -> str | None:
    """Return 'Drop'/'__traverse__'/'__clear__' if node sits in such a scope."""
    fn = find_enclosing(node, "function_item")
    if fn is not None:
        name = fn.child_by_field_name("name")
        fn_name = text_of(name, source) if name is not None else ""
        if fn_name in ("__traverse__", "__clear__"):
            return fn_name
    impl = find_enclosing(node, "impl_item")
    if impl is not None:
        trait = impl.child_by_field_name("trait")
        if trait is not None and text_of(trait, source).rsplit("::", 1)[-1] == "Drop":
            return "Drop"
    return None


def _scan_file(tree: object, source: bytes, stripped: bytes, rel: str) -> list[dict]:
    """Run every PyResult pattern over one parsed source file.

    ``source`` is the original bytes (used for comment-aware suppression and
    display text); ``stripped`` has comments blanked (used for pattern
    matching, so a keyword inside a comment cannot cause a false hit).
    """
    findings: list[dict] = []
    callable_ranges = _pyo3_callable_ranges(tree, stripped)
    calls = find_calls_in_scope(tree, stripped)

    for call in calls:
        leaf = call["name"]
        node = call["node"]
        receiver = call["receiver_text"] or ""
        fn_name = enclosing_function_name(node, source) or "<module>"
        in_callable = is_in_region(node.start_byte, callable_ranges)
        # `suppressed` is computed lazily, only inside a matched pattern --
        # `is_suppressed_by_comment` walks the whole tree, so calling it once
        # per call (rather than once per finding) is quadratic on big files.

        # Pattern 1: .unwrap() / .expect() on a PyResult, in a PyO3 callable.
        if (
            call["kind"] == "method"
            and leaf in ("unwrap", "expect")
            and in_callable
            and looks_like_pyresult(receiver)
        ):
            is_unwrap = leaf == "unwrap"
            classification = "FIX" if is_unwrap else "CONSIDER"
            if is_suppressed_by_comment(source, tree, call["line"]):
                classification = "ACCEPTABLE"
            verb = "`.unwrap()`" if is_unwrap else "`.expect()`"
            findings.append(
                make_finding(
                    "unwrap_on_pyresult",
                    classification=classification,
                    confidence="MEDIUM",
                    description=(
                        f"{verb} on a `PyResult` in `{fn_name}` -- a panic here "
                        f"is converted to a `PanicException` that crosses the "
                        f"FFI boundary, which is almost never intended."
                    ),
                    file=rel,
                    line=call["line"],
                    column=node.start_point[1] + 1,
                    function=fn_name,
                    category="error_handling",
                    fix_template=(
                        "Return `PyResult<T>` and propagate the error with `?` "
                        "instead of panicking."
                    ),
                    details={"method": leaf, "receiver": receiver},
                )
            )

        # Pattern 2: .ok() discards the PyErr.
        elif (
            call["kind"] == "method" and leaf == "ok" and looks_like_pyresult(receiver)
        ):
            findings.append(
                make_finding(
                    "ok_on_pyresult",
                    classification=(
                        "ACCEPTABLE"
                        if is_suppressed_by_comment(source, tree, call["line"])
                        else "CONSIDER"
                    ),
                    confidence="MEDIUM",
                    description=(
                        f"`.ok()` discards the `PyErr` from a `PyResult` in "
                        f"`{fn_name}`, silently swallowing a Python exception."
                    ),
                    file=rel,
                    line=call["line"],
                    column=node.start_point[1] + 1,
                    function=fn_name,
                    category="error_handling",
                    fix_template=(
                        "Handle the error explicitly or propagate it with `?`; "
                        "do not discard the `PyErr`."
                    ),
                    details={"receiver": receiver},
                )
            )

        # Pattern 4: a PyResult-returning call dropped as a bare statement.
        elif (
            leaf in _PYRESULT_METHOD_NAMES
            and in_callable
            and node.parent is not None
            and node.parent.type == "expression_statement"
        ):
            findings.append(
                make_finding(
                    "unhandled_pyresult_statement",
                    classification=(
                        "ACCEPTABLE"
                        if is_suppressed_by_comment(source, tree, call["line"])
                        else "CONSIDER"
                    ),
                    confidence="MEDIUM",
                    description=(
                        f"The `PyResult` of `{call['full_callee']}` is dropped "
                        f"as a statement in `{fn_name}` without `?` -- any "
                        f"`PyErr` would be silently ignored."
                    ),
                    file=rel,
                    line=call["line"],
                    column=node.start_point[1] + 1,
                    function=fn_name,
                    category="error_handling",
                    fix_template="Append `?` to propagate the error, or handle it.",
                    details={"callee": call["full_callee"]},
                )
            )

        # Pattern 6: manual PyErr state juggling.
        if leaf in _PYERR_STATE_OPS and "PyErr" in call["full_callee"]:
            scope = _enclosing_drop_or_gc(node, source)
            findings.append(
                make_finding(
                    "pyerr_state_manipulation",
                    classification="CONSIDER",
                    confidence="HIGH",
                    description=(
                        f"Manual `PyErr` state manipulation "
                        f"(`{call['full_callee']}`) in `{fn_name}` -- verify "
                        f"save/restore is balanced on every control-flow path"
                        + (f" (inside `{scope}`)." if scope else ".")
                    ),
                    file=rel,
                    line=call["line"],
                    column=node.start_point[1] + 1,
                    function=fn_name,
                    category="error_handling",
                    fix_template=(
                        "Pair every `take`/`fetch` with a `restore` on all "
                        "paths; prefer scoped helpers over manual juggling."
                    ),
                    details={"operation": call["full_callee"], "scope": scope},
                )
            )

        # Pattern 7: PyErr_Clear / PyErr::clear silently drops the exception.
        if leaf == "PyErr_Clear" or (
            leaf == "clear" and "PyErr" in call["full_callee"]
        ):
            findings.append(
                make_finding(
                    "silent_pyerr_clear",
                    classification="CONSIDER",
                    confidence="HIGH",
                    description=(
                        f"`{call['full_callee']}` clears the active Python "
                        f"exception in `{fn_name}` -- this silently swallows "
                        f"errors, including `KeyboardInterrupt` / `MemoryError`."
                    ),
                    file=rel,
                    line=call["line"],
                    column=node.start_point[1] + 1,
                    function=fn_name,
                    category="error_handling",
                    fix_template=(
                        "Only clear an exception you have inspected and decided "
                        "to handle; otherwise propagate it."
                    ),
                    details={"operation": call["full_callee"]},
                )
            )

    findings.extend(_scan_let_declarations(tree, source, stripped, rel))
    return findings


def _scan_let_declarations(
    tree: object, source: bytes, stripped: bytes, rel: str
) -> list[dict]:
    """Patterns 3 (`let _ = <pyresult>`) and 5 (exception clobbering)."""
    findings: list[dict] = []
    for node in walk(tree, "let_declaration"):
        pattern = node.child_by_field_name("pattern")
        value = node.child_by_field_name("value")
        if pattern is None or value is None:
            continue
        fn_name = enclosing_function_name(node, source) or "<module>"
        suppressed = is_suppressed_by_comment(source, tree, node_line(node))
        value_text = text_of(value, source)

        # Pattern 3: `let _ = <pyresult-shaped expr>;`
        if text_of(pattern, stripped).strip() == "_" and looks_like_pyresult(
            text_of(value, stripped)
        ):
            findings.append(
                make_finding(
                    "let_underscore_pyresult",
                    classification="ACCEPTABLE" if suppressed else "CONSIDER",
                    confidence="MEDIUM",
                    description=(
                        f"`let _ = ...;` drops a `PyResult` in `{fn_name}`, "
                        f"silently swallowing any `PyErr`."
                    ),
                    file=rel,
                    line=node_line(node),
                    column=node.start_point[1] + 1,
                    function=fn_name,
                    category="error_handling",
                    fix_template="Bind and handle the result, or propagate with `?`.",
                    details={"value": value_text[:120]},
                )
            )
            continue

        # Pattern 5: `let <pat> = <pyresult>.err();` with a later `?`.
        if value.type != "call_expression":
            continue
        fn_node = value.child_by_field_name("function")
        if fn_node is None or fn_node.type != "field_expression":
            continue
        field = fn_node.child_by_field_name("field")
        recv = fn_node.child_by_field_name("value")
        if field is None or recv is None or text_of(field, stripped) != "err":
            continue
        if not looks_like_pyresult(text_of(recv, stripped)):
            continue
        enclosing_fn = find_enclosing(node, "function_item")
        body = enclosing_fn.child_by_field_name("body") if enclosing_fn else None
        if body is None:
            continue
        later_fallible = any(
            t.start_byte > node.end_byte for t in walk(body, "try_expression")
        )
        if not later_fallible:
            continue
        findings.append(
            make_finding(
                "exception_clobbering",
                classification="ACCEPTABLE" if suppressed else "CONSIDER",
                confidence="LOW",
                description=(
                    f"A `PyErr` is captured with `.err()` in `{fn_name}` and a "
                    f"later fallible call (`?`) runs before it is re-raised -- "
                    f"the original exception may be clobbered."
                ),
                file=rel,
                line=node_line(node),
                column=node.start_point[1] + 1,
                function=fn_name,
                category="error_handling",
                fix_template=(
                    "Re-raise the captured `PyErr` before making further "
                    "fallible Python calls, or restructure to avoid holding it."
                ),
                details={"captured": text_of(value, source)[:120]},
            )
        )
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan a Rust/PyO3 crate for PyResult-propagation mistakes."""
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
        findings.extend(_scan_file(tree, source, stripped, rel))

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
