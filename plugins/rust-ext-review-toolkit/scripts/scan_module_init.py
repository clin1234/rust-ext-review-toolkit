#!/usr/bin/env python3
"""Audit `#[pymodule]` initialization in Rust/PyO3 extension code.

The `#[pymodule]` function is the extension's entry point. This scanner
checks it and the module-level state around it (design SS3.2.7):

  - a `static OnceLock<Py<_>>` / `LazyLock<Py<_>>` / `Lazy<Py<_>>` holding a
    Python object -- a process-global that bypasses per-interpreter state
    (`module_level_py_static`)
  - a `module.add_class()` / `add` / `add_function` / `add_submodule` call
    whose `PyResult` is dropped without `?` -- a silent registration failure
    (`module_add_unchecked`)
  - a `#[pymodule]` function that does not return `PyResult<()>`
    (`pymodule_return_type`)

The `gil_used` free-threading declaration is checked by
gil-discipline-checker, not here.

Usage:
    python scan_module_init.py [path] [--max-files N]
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_rust_ext import discover  # noqa: E402
from rust_ts_utils import (  # noqa: E402
    extract_attribute_targets,
    extract_fn_items,
    find_calls_in_scope,
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
    make_finding,
    parse_common_args,
    relative_path,
)

# Methods that register something into the module and return a `PyResult`.
_REGISTRARS = frozenset(
    {"add_class", "add", "add_function", "add_submodule", "add_wrapped"}
)
# Once-cell wrappers that, holding a `Py<_>`, make a process-global object.
# `PyOnceLock` is PyO3's GIL-aware once-cell (`pyo3::sync::PyOnceLock`),
# distinct from `std::sync::OnceLock`. v0.2: added after the polars review
# missed three `PyOnceLock<Py<PyType>>` statics outside the canonical
# `py_modules.rs` location.
_PY_GLOBAL_CELLS = ("PyOnceLock", "OnceLock", "LazyLock", "OnceCell", "Lazy")


def _scan_module_statics(
    tree: object, source: bytes, stripped: bytes, rel: str
) -> list[dict]:
    """Flag `static` items that hold a Python object in a once-cell."""
    findings: list[dict] = []
    for node in walk(tree, "static_item"):
        type_node = node.child_by_field_name("type")
        if type_node is None:
            continue
        type_text = text_of(type_node, stripped)
        if "Py<" not in type_text and "PyObject" not in type_text:
            continue
        if not any(cell in type_text for cell in _PY_GLOBAL_CELLS):
            continue
        name_node = node.child_by_field_name("name")
        name = text_of(name_node, stripped) if name_node is not None else "<static>"
        line = node_line(node)
        suppressed = is_suppressed_by_comment(source, tree, line)
        findings.append(
            make_finding(
                "module_level_py_static",
                classification="ACCEPTABLE" if suppressed else "CONSIDER",
                confidence="HIGH",
                description=(
                    f"`static {name}: {type_text}` is a process-global Python "
                    f"object. It is shared by every (sub)interpreter and "
                    f"outlives interpreter finalization -- a hazard for "
                    f"subinterpreter support and clean shutdown."
                ),
                file=rel,
                line=line,
                column=node.start_point[1] + 1,
                category="module_state",
                fix_template=(
                    "Store the object in module state (`PyModule` attribute or "
                    "a `#[pyclass]`) instead of a process-global static."
                ),
                details={"name": name, "type": type_text},
            )
        )
    return findings


def _scan_pymodule_fn(
    hit: dict, source: bytes, stripped: bytes, tree: object, rel: str
) -> list[dict]:
    """Check one `#[pymodule]` function's return type and registrar calls."""
    target = hit["target"]
    if target.type != "function_item":
        return []
    name_node = target.child_by_field_name("name")
    fn_name = text_of(name_node, stripped) if name_node is not None else "<module>"
    findings: list[dict] = []

    ret = target.child_by_field_name("return_type")
    ret_text = text_of(ret, stripped) if ret is not None else ""
    if "PyResult<()>" not in ret_text.replace(" ", ""):
        findings.append(
            make_finding(
                "pymodule_return_type",
                classification="CONSIDER",
                confidence="HIGH",
                description=(
                    f"The `#[pymodule]` `{fn_name}` returns "
                    f"`{ret_text or '()'}` -- a module init function must "
                    f"return `PyResult<()>` so registration errors propagate."
                ),
                file=rel,
                line=hit["line"],
                column=target.start_point[1] + 1,
                function=fn_name,
                category="module_state",
                fix_template="Change the return type to `PyResult<()>`.",
                details={"return_type": ret_text},
            )
        )

    body = target.child_by_field_name("body")
    if body is not None:
        for call in find_calls_in_scope(body, stripped):
            if call["name"] not in _REGISTRARS:
                continue
            parent = call["node"].parent
            if parent is None or parent.type != "expression_statement":
                continue  # `?`-propagated or result captured -- handled
            line = call["line"]
            suppressed = is_suppressed_by_comment(source, tree, line)
            findings.append(
                make_finding(
                    "module_add_unchecked",
                    classification="ACCEPTABLE" if suppressed else "FIX",
                    confidence="HIGH",
                    description=(
                        f"`{call['full_callee']}` in `{fn_name}` drops its "
                        f"`PyResult` -- if registration fails the module is "
                        f"left half-initialised with no error raised."
                    ),
                    file=rel,
                    line=line,
                    column=call["node"].start_point[1] + 1,
                    function=fn_name,
                    category="module_state",
                    fix_template="Append `?` to propagate the registration error.",
                    details={"callee": call["full_callee"]},
                )
            )
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan a Rust/PyO3 crate for `#[pymodule]` initialization problems."""
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
        findings.extend(_scan_module_statics(tree, source, stripped, rel))
        for hit in extract_attribute_targets(tree, stripped, "pymodule"):
            findings.extend(_scan_pymodule_fn(hit, source, stripped, tree, rel))

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
