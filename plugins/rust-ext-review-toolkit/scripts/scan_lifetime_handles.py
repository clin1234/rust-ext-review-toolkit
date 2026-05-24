#!/usr/bin/env python3
"""Audit PyO3 handle kinds and lifetimes in Rust/PyO3 extension code.

PyO3 0.21 replaced the old GIL-reference types (`&PyAny`, `&PyDict`, ...) with
the `Bound<'py, T>` / `Py<T>` / `Borrowed<'a, 'py, T>` handle family. This
scanner finds handle-API residue and non-idiomatic handle choices, with
severity keyed to the crate's PyO3 version (design SS3.2.8):

  - `&PyAny` / `&PyDict` / ... GIL-reference types (`legacy_ref_type`)
  - `Py::as_ref(py)`, removed in 0.26 (`legacy_as_ref`)
  - a `Py<T>` parameter only ever `.bind()`-ed -- it could be `&Bound<'py, T>`
    directly (`py_param_should_be_bound`)
  - a `Borrowed<'a, 'py, T>` handle, which is rarely the right choice
    (`unjustified_borrowed`)

`.clone()` on a `Py<T>` (the `py-clone` migration) is not detected -- it
needs type inference; review it by hand. The full deprecated-API surface is
pyo3-version-compat-scanner's.

Usage:
    python scan_lifetime_handles.py [path] [--max-files N]
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_rust_ext import discover  # noqa: E402
from rust_ts_utils import (  # noqa: E402
    extract_fn_items,
    find_calls_in_scope,
    find_enclosing,
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
    is_suppressed_by_comment,
    make_finding,
    parse_common_args,
    pyo3_version_tuple,
    relative_path,
)

# The pre-0.21 GIL-reference native types. `&<one of these>` is the legacy
# handle form replaced by `Bound<'py, T>`.
_GIL_REF_TYPES = frozenset(
    {
        "PyAny",
        "PyDict",
        "PyList",
        "PyTuple",
        "PySet",
        "PyFrozenSet",
        "PyString",
        "PyBytes",
        "PyByteArray",
        "PyBool",
        "PyInt",
        "PyLong",
        "PyFloat",
        "PyComplex",
        "PyType",
        "PyModule",
        "PySlice",
        "PyCapsule",
        "PyTraceback",
        "PyMapping",
        "PySequence",
        "PyIterator",
        "PyDate",
        "PyTime",
        "PyDateTime",
        "PyDelta",
        "PyTzInfo",
        "PyCode",
        "PyFrame",
    }
)

# PyO3 versions that removed each legacy API (approximate -- see notes in
# data/pyo3_api_tables.json).
_GIL_REF_REMOVED = (0, 24)
_AS_REF_REMOVED = (0, 26)


def _scan_file(
    tree: object,
    source: bytes,
    stripped: bytes,
    rel: str,
    version: tuple[int, int],
) -> list[dict]:
    """Run every handle-kind check over one parsed source file."""
    findings: list[dict] = []

    def _add(ftype, classification, confidence, line, column, fn, desc, fix, details):
        if classification != "FIX" and is_suppressed_by_comment(source, tree, line):
            classification = "ACCEPTABLE"
        findings.append(
            make_finding(
                ftype,
                classification=classification,
                confidence=confidence,
                description=desc,
                file=rel,
                line=line,
                column=column,
                function=fn,
                category="handle_kind",
                fix_template=fix,
                details=details,
            )
        )

    # `&PyAny` / `&PyDict` / ... -- legacy GIL-reference types.
    for node in walk(tree, "reference_type"):
        type_node = node.child_by_field_name("type")
        if type_node is None:
            continue
        type_text = text_of(type_node, stripped).strip()
        if type_text not in _GIL_REF_TYPES:
            continue
        removed = version >= _GIL_REF_REMOVED
        _add(
            "legacy_ref_type",
            "FIX" if removed else "CONSIDER",
            "HIGH",
            node.start_point[0] + 1,
            node.start_point[1] + 1,
            enclosing_function_name(node, source) or "<module>",
            (
                f"`&{type_text}` is a pre-0.21 GIL-reference type. "
                + (
                    "It no longer compiles on this PyO3 version; "
                    if removed
                    else "It has been deprecated since 0.21; "
                )
                + f"use `Bound<'py, {type_text}>` (borrow as `&Bound`)."
            ),
            f"Replace `&{type_text}` with `&Bound<'py, {type_text}>`.",
            {"legacy_type": type_text},
        )

    # `Py::as_ref(py)` -- removed in 0.26.
    for call in find_calls_in_scope(tree, stripped):
        if call["name"] != "as_ref" or not call["args_text"]:
            continue
        node = call["node"]
        removed = version >= _AS_REF_REMOVED
        _add(
            "legacy_as_ref",
            "FIX" if removed else "CONSIDER",
            "MEDIUM",
            call["line"],
            node.start_point[1] + 1,
            enclosing_function_name(node, source) or "<module>",
            (
                f"`.as_ref({call['args_text']})` looks like the legacy "
                f"`Py::as_ref(py)` GIL-reference accessor"
                + (
                    ", which was removed in PyO3 0.26."
                    if removed
                    else ", deprecated since 0.21."
                )
            ),
            "Use `Py::bind(py)`, which returns `&Bound<'py, T>`.",
            {"args": call["args_text"]},
        )

    # `Borrowed<'a, 'py, T>` in a signature position -- rarely the right handle.
    # (A `Borrowed` used only inside a function body is the maintainer's call.)
    #
    # v0.2: PyO3 0.28's `FromPyObject::extract` mandates `Borrowed<'a, 'py,
    # PyAny>` as the parameter type. Suppress findings inside such an impl
    # block -- the trait signature is fixed, the developer has no choice.
    # Polars hit 61/61 FPs from this single shape.
    for node in walk(tree, "generic_type"):
        base = node.child_by_field_name("type")
        if base is None:
            continue
        if text_of(base, stripped).rsplit("::", 1)[-1].strip() != "Borrowed":
            continue
        scope = find_enclosing(node, {"parameters", "block", "function_item"})
        if scope is not None and scope.type == "block":
            continue  # used inside a function body, not in a signature
        if _inside_frompyobject_impl(node, stripped):
            continue  # trait-mandated signature; not a developer choice
        _add(
            "unjustified_borrowed",
            "CONSIDER",
            "LOW",
            node.start_point[0] + 1,
            node.start_point[1] + 1,
            enclosing_function_name(node, source) or "<module>",
            (
                "A `Borrowed<'a, 'py, T>` handle is in use. It is rarely "
                "needed -- `&Bound<'py, T>` is usually the right choice and "
                "is simpler to reason about."
            ),
            "Confirm the extra `'a` lifetime is required; otherwise use "
            "`&Bound<'py, T>`.",
            {},
        )

    # A `Py<T>` parameter that is only ever `.bind()`-ed.
    findings.extend(_scan_py_params(tree, source, stripped, rel))
    return findings


def _scan_py_params(
    tree: object, source: bytes, stripped: bytes, rel: str
) -> list[dict]:
    """Flag `Py<T>` parameters whose every use is `.bind(py)`."""
    findings: list[dict] = []
    for fn in extract_fn_items(tree, stripped):
        params = fn["params_node"]
        body = fn["body_node"]
        if params is None or body is None:
            continue
        for child in params.children:  # type: ignore[attr-defined]
            if child.type != "parameter":
                continue
            pat = child.child_by_field_name("pattern")
            ty = child.child_by_field_name("type")
            if pat is None or ty is None:
                continue
            type_text = text_of(ty, stripped).strip()
            if not type_text.startswith("Py<"):
                continue
            name = text_of(pat, stripped).strip()
            if not name or not name.isidentifier():
                continue
            if not _bind_only(body, name, stripped):
                continue
            line = fn["start_line"]
            classification = "CONSIDER"
            if is_suppressed_by_comment(source, tree, line):
                classification = "ACCEPTABLE"
            findings.append(
                make_finding(
                    "py_param_should_be_bound",
                    classification=classification,
                    confidence="MEDIUM",
                    description=(
                        f"Parameter `{name}: {type_text}` in `{fn['name']}` is "
                        f"only ever `.bind()`-ed. Taking `&Bound<'py, T>` "
                        f"directly is more idiomatic and avoids the bind step."
                    ),
                    file=rel,
                    line=line,
                    column=child.start_point[1] + 1,
                    function=fn["name"],
                    category="handle_kind",
                    fix_template=(
                        f"Change `{name}: {type_text}` to a `&Bound<'py, T>` "
                        f"parameter and drop the `.bind(py)` call(s)."
                    ),
                    details={"param": name, "param_type": type_text},
                )
            )
    return findings


def _inside_frompyobject_impl(node: Any, stripped: bytes) -> bool:
    """True if ``node`` is inside an ``impl FromPyObject for ...`` block.

    PyO3 0.28's `FromPyObject::extract` and `FromPyObject::extract_bound`
    take a `Borrowed<'a, 'py, PyAny>` parameter; the trait signature is
    fixed, so flagging it as `unjustified_borrowed` is a categorical false
    positive.

    Tolerates path qualification (``pyo3::FromPyObject``,
    ``pyo3::conversion::FromPyObject``, ...).
    """
    impl_node = find_enclosing(node, "impl_item")
    if impl_node is None:
        return False
    trait_node = impl_node.child_by_field_name("trait")
    if trait_node is None:
        return False
    trait_name = text_of(trait_node, stripped).strip()
    # Strip generic args (`FromPyObject<'_>` -> `FromPyObject`) and path
    # qualification (`pyo3::FromPyObject` -> `FromPyObject`).
    trait_name = trait_name.split("<", 1)[0]
    trait_name = trait_name.rsplit("::", 1)[-1].strip()
    return trait_name == "FromPyObject"


def _bind_only(body: Any, name: str, stripped: bytes) -> bool:
    """True if every use of identifier ``name`` in ``body`` is a `.bind` receiver."""
    total = 0
    bind = 0
    for node in walk(body, "identifier"):
        if text_of(node, stripped) != name:
            continue
        total += 1
        parent = node.parent
        if parent is not None and parent.type == "field_expression":
            field = parent.child_by_field_name("field")
            value = parent.child_by_field_name("value")
            if (
                value is not None
                and value.id == node.id
                and field is not None
                and text_of(field, stripped) == "bind"
            ):
                bind += 1
    return total >= 1 and bind == total


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan a Rust/PyO3 crate for handle-kind and lifetime problems."""
    discovery = discover(target)
    project_root = Path(discovery["project_root"])
    files = discover_rust_files(Path(discovery["scan_root"]), max_files=max_files)
    version = pyo3_version_tuple(discovery.get("pyo3_version"))

    findings: list[dict] = []
    functions_analyzed = 0
    for path in files:
        source = path.read_bytes()
        tree = parse_bytes(source)
        stripped = strip_comments(source)
        rel = relative_path(path, project_root)
        functions_analyzed += len(extract_fn_items(tree, stripped))
        findings.extend(_scan_file(tree, source, stripped, rel, version))

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
