#!/usr/bin/env python3
"""Audit `#[pymethods]` protocol correctness in Rust/PyO3 extension code.

For each `#[pyclass]` (`struct` *or* `enum`) this scanner finds the matching
`#[pymethods] impl`, enumerates the methods, and checks the protocol shapes
(design SS3.2.6):

  - a `#[pyclass]` that can hold reference cycles (a `Py<T>`/`Bound` field
    or enum variant, or `dict`/`weakref`) but lacks `__traverse__` /
    `__clear__` (`missing_traverse`, `missing_clear`)
  - a `#[getter]` that takes arguments (`getter_with_args`)
  - a `__richcmp__` whose `match` on `CompareOp` omits operators with no
    wildcard arm (`richcmp_incomplete`)
  - a `__hash__` that does not return `isize`/`u64` (`hash_return_type`)
  - `#[new]` together with an `__init__` method, or a literal `__new__`
    method (`new_init_collision`)

For enum `#[pyclass]`es, "fields" is the union of every variant's fields --
the cycle-risk question is variant-agnostic.

The scan is per file: a `#[pyclass]` whose `#[pymethods] impl` is in another
file is skipped (conservative -- avoids false `missing_traverse` reports).
Manual buffer-protocol (`__getbuffer__`) review is unsafe-block-auditor's.

Usage:
    python scan_pyclass_protocol.py [path] [--max-files N]
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_rust_ext import discover  # noqa: E402
from rust_ts_utils import (  # noqa: E402
    extract_attribute_targets,
    extract_attributes,
    extract_enum_defs,
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
    make_finding,
    parse_attribute_options,
    parse_common_args,
    relative_path,
)

_RICHCMP_OPS = frozenset({"Lt", "Le", "Eq", "Ne", "Gt", "Ge"})


def _type_name(type_text: str) -> str:
    """Bare type name: strip generics and path qualification."""
    return type_text.split("<", 1)[0].strip().rsplit("::", 1)[-1]


def _holds_py_handle(type_text: str) -> bool:
    """True if a field type stores an owned Python handle (cycle risk)."""
    return "Py<" in type_text or "Bound<" in type_text or "PyObject" in type_text


def _data_param_count(params_node: object, stripped: bytes) -> int:
    """Count real data parameters of a method, excluding the receiver.

    The receiver is either a `self_parameter` (`&self`/`&mut self`/`self`) or,
    when a method takes its receiver by value, a typed first parameter
    (`self_: PyRef<Self>`, `this: Bound<'_, Self>`, `slf: Py<Self>`, ...). A
    `Python<'py>` token never counts either. A `#[getter]` has no data
    arguments, so any leftover parameter is a genuine mistake.
    """
    if params_node is None:
        return 0
    children = list(params_node.children)  # type: ignore[attr-defined]
    has_self = any(c.type == "self_parameter" for c in children)
    params = [c for c in children if c.type == "parameter"]
    if not has_self and params:
        params = params[1:]  # the first parameter is the by-value receiver
    count = 0
    for child in params:
        ty = child.child_by_field_name("type")
        type_text = text_of(ty, stripped) if ty is not None else ""
        if "Python" in type_text:
            continue
        count += 1
    return count


def _richcmp_missing_ops(body_text: str) -> list[str] | None:
    """Return CompareOp variants a `__richcmp__` match omits, or None if fine."""
    found = set(re.findall(r"CompareOp::(Lt|Le|Eq|Ne|Gt|Ge)\b", body_text))
    if not found:
        return None  # not a manual op-match (e.g. uses `op.matches(...)`)
    if found >= _RICHCMP_OPS or re.search(r"_\s*=>", body_text):
        return None  # all six handled, or a wildcard default arm exists
    return sorted(_RICHCMP_OPS - found)


def _hash_return_ok(return_type: str | None) -> bool:
    """True if a `__hash__` return type is the canonical `isize`/`u64`."""
    return return_type is not None and ("isize" in return_type or "u64" in return_type)


def _check_protocol(
    type_name: str,
    impl: dict,
    fields: list[dict],
    options: dict,
    source: bytes,
    stripped: bytes,
    tree: object,
    rel: str,
) -> list[dict]:
    """Check one `#[pyclass]` / `#[pymethods]` pair (struct or enum)."""
    body = impl["body_node"]
    methods = extract_fn_items(body, stripped) if body is not None else []
    method_names = {m["name"] for m in methods}
    findings: list[dict] = []

    def _finding(ftype, classification, confidence, line, desc, fix, **details):
        suppressed = is_suppressed_by_comment(source, tree, line)
        if suppressed and classification != "FIX":
            classification = "ACCEPTABLE"
        return make_finding(
            ftype,
            classification=classification,
            confidence=confidence,
            description=desc,
            file=rel,
            line=line,
            function=type_name,
            category="protocol",
            fix_template=fix,
            details={"struct": type_name, **details},
        )

    # Reference-cycle participation requires GC slots.
    needs_gc = any(_holds_py_handle(f["type"]) for f in fields) or bool(
        options.get("dict") or options.get("weakref")
    )
    if needs_gc:
        for slot in ("__traverse__", "__clear__"):
            if slot not in method_names:
                findings.append(
                    _finding(
                        f"missing_{slot.strip('_')}",
                        # CONSIDER, not FIX: the scanner cannot tell whether
                        # this class can actually take part in a reference
                        # cycle. The agent escalates to FIX once it confirms a
                        # cycle is possible (a `Py<T>` of a leaf type cannot).
                        "CONSIDER",
                        "HIGH",
                        impl["start_line"],
                        (
                            f"`#[pyclass] {type_name}` holds an owned Python "
                            f"handle (or `dict`/`weakref`) but its "
                            f"`#[pymethods]` impl has no `{slot}`. If the class "
                            f"can take part in a reference cycle, that cycle "
                            f"leaks -- invisible to Rust's `Drop`."
                        ),
                        (
                            f"If a cycle through this class is possible, "
                            f"implement `{slot}` so the cyclic GC can reach the "
                            f"stored Python objects."
                        ),
                    )
                )

    new_attr_method = None
    for method in methods:
        attr_names = {a["name"] for a in extract_attributes(method["node"], stripped)}
        if "new" in attr_names:
            new_attr_method = method
        if (
            "getter" in attr_names
            and _data_param_count(method["params_node"], stripped) > 0
        ):
            findings.append(
                _finding(
                    "getter_with_args",
                    "FIX",
                    "HIGH",
                    method["start_line"],
                    (
                        f"`#[getter] {method['name']}` on `{type_name}` takes "
                        f"argument(s). A getter is invoked as attribute access "
                        f"and must take only `&self` (optionally `py`)."
                    ),
                    "Remove the extra parameter(s), or use a plain method.",
                    method=method["name"],
                )
            )

    if "__richcmp__" in method_names:
        richcmp = next(m for m in methods if m["name"] == "__richcmp__")
        body_text = (
            text_of(richcmp["body_node"], stripped)
            if richcmp["body_node"] is not None
            else ""
        )
        missing = _richcmp_missing_ops(body_text)
        if missing:
            findings.append(
                _finding(
                    "richcmp_incomplete",
                    "CONSIDER",
                    "MEDIUM",
                    richcmp["start_line"],
                    (
                        f"`__richcmp__` on `{type_name}` matches `CompareOp` "
                        f"but omits {', '.join(missing)} with no wildcard arm "
                        f"-- unhandled operators will not fall back to "
                        f"`NotImplemented`."
                    ),
                    (
                        "Handle all six `CompareOp` variants, or add a "
                        "`_ => Ok(py.NotImplemented())` arm."
                    ),
                    missing_ops=missing,
                )
            )

    if "__hash__" in method_names:
        hash_fn = next(m for m in methods if m["name"] == "__hash__")
        if not _hash_return_ok(hash_fn["return_type"]):
            findings.append(
                _finding(
                    "hash_return_type",
                    "CONSIDER",
                    "MEDIUM",
                    hash_fn["start_line"],
                    (
                        f"`__hash__` on `{type_name}` returns "
                        f"`{hash_fn['return_type'] or '()'}`; PyO3 expects "
                        f"`isize` (or `u64`)."
                    ),
                    "Change the `__hash__` return type to `isize`.",
                    return_type=hash_fn["return_type"],
                )
            )

    if new_attr_method is not None and "__init__" in method_names:
        findings.append(
            _finding(
                "new_init_collision",
                "CONSIDER",
                "HIGH",
                new_attr_method["start_line"],
                (
                    f"`{type_name}` has both a `#[new]` constructor and an "
                    f"`__init__` method -- PyO3 constructs via `#[new]`; the "
                    f"`__init__` makes construction semantics ambiguous."
                ),
                "Remove `__init__`; do all construction work in `#[new]`.",
            )
        )
    elif "__new__" in method_names:
        new_fn = next(m for m in methods if m["name"] == "__new__")
        findings.append(
            _finding(
                "new_init_collision",
                "CONSIDER",
                "HIGH",
                new_fn["start_line"],
                (
                    f"`{type_name}` defines a method literally named "
                    f"`__new__`. PyO3 uses the `#[new]` attribute on a normal "
                    f"method (conventionally `new`), not a `__new__` method."
                ),
                "Rename the method and mark it `#[new]`.",
            )
        )
    return findings


def _scan_file(tree: object, source: bytes, stripped: bytes, rel: str) -> list[dict]:
    """Check every `#[pyclass]` whose `#[pymethods] impl` is in this file.

    `#[pyclass]` accepts both `struct` and `enum` targets; both shapes need
    the same protocol checks (GC slots, richcmp, hash, getter, new/__init__).
    For enums, the "fields" set is the union of all variant fields -- the
    `_holds_py_handle` question is "could this type hold a Python handle
    *anywhere*?", which is variant-agnostic.
    """
    type_fields: dict[str, list[dict]] = {}
    for s in extract_struct_defs(tree, stripped):
        type_fields[s["name"]] = s["fields"]
    for e in extract_enum_defs(tree, stripped):
        type_fields[e["name"]] = e["all_fields"]

    pymethods_impls: dict[str, dict] = {}
    for impl in extract_impl_blocks(tree, stripped):
        if any(
            a["name"] == "pymethods" for a in extract_attributes(impl["node"], stripped)
        ):
            pymethods_impls.setdefault(_type_name(impl["type"]), impl)

    findings: list[dict] = []
    for hit in extract_attribute_targets(tree, stripped, "pyclass"):
        target = hit["target"]
        if target.type not in ("struct_item", "enum_item"):
            continue
        name_node = target.child_by_field_name("name")
        type_name = text_of(name_node, stripped) if name_node is not None else ""
        pyimpl = pymethods_impls.get(type_name)
        if pyimpl is None:
            continue  # #[pymethods] impl not in this file -- skip conservatively
        findings.extend(
            _check_protocol(
                type_name,
                pyimpl,
                type_fields.get(type_name, []),
                parse_attribute_options(hit["attr"]["args_text"]),
                source,
                stripped,
                tree,
                rel,
            )
        )
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan a Rust/PyO3 crate for `#[pymethods]` protocol problems."""
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
