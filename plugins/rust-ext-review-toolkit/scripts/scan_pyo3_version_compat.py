#!/usr/bin/env python3
"""Audit PyO3 version compatibility in Rust/PyO3 extension code.

Drives off `data/pyo3_api_tables.json`: each API entry records the PyO3
versions at which it was deprecated, removed, or renamed, plus a `detect`
rule. The scanner finds uses of those APIs and classifies each against the
crate's resolved PyO3 version (design SS3.2.9):

  - `removed_api` (FIX) -- the API is gone at the crate's version
  - `deprecated_api` (CONSIDER) -- the API still works but is deprecated
  - `renamed_api` (CONSIDER) -- the legacy name of a renamed API is migration
    debt

It also flags one feature-level conflict:

  - `abi3_free_threading_conflict` (CONSIDER) -- `abi3` is enabled on a
    free-threading-targeted crate; PyO3 silently ignores `abi3` under a
    free-threaded build.

Legacy handle types (`&PyAny`, `Py::as_ref`) are owned by
lifetime-handle-checker and marked `skip` in the data file. Private `_Py*`
calls under abi3 are unsafe-block-auditor's.

Usage:
    python scan_pyo3_version_compat.py [path] [--max-files N]
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_rust_ext import discover  # noqa: E402
from rust_ts_utils import (  # noqa: E402
    extract_fn_items,
    find_calls_in_scope,
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
    load_data_file,
    make_finding,
    parse_common_args,
    pyo3_version_tuple,
    relative_path,
)


def _version_floor(spec: str) -> tuple[int, int]:
    """(major, minor) for a version spec, tolerating a trailing `+`."""
    return pyo3_version_tuple(str(spec).rstrip("+"))


def _classify_api_use(entry: dict, version: tuple[int, int]) -> tuple[str, str] | None:
    """Return (finding_type, classification) for an API entry, or None."""
    removed = entry.get("removed")
    deprecated = entry.get("deprecated")
    renamed_in = entry.get("renamed_in")
    if removed and version >= _version_floor(removed):
        return ("removed_api", "FIX")
    if deprecated and version >= _version_floor(deprecated):
        return ("deprecated_api", "CONSIDER")
    if renamed_in and version >= _version_floor(renamed_in):
        return ("renamed_api", "CONSIDER")
    return None


def _describe(entry: dict, ftype: str) -> str:
    """Human-readable description for an API-compatibility finding."""
    pattern = entry.get("rust_signature_pattern", "<api>")
    if ftype == "removed_api":
        head = f"`{pattern}` was removed in PyO3 {entry.get('removed')}"
    elif ftype == "deprecated_api":
        head = f"`{pattern}` has been deprecated since PyO3 {entry.get('deprecated')}"
    else:
        head = (
            f"`{pattern}` was renamed to `{entry.get('renamed_to')}` in PyO3 "
            f"{entry.get('renamed_in')}"
        )
    replacement = entry.get("replacement") or entry.get("renamed_to")
    tail = f"; use `{replacement}`." if replacement else "."
    return head + tail + (f" {entry['note']}" if entry.get("note") else "")


def _build_detectors(apis: dict) -> tuple[dict, list, dict]:
    """Index API entries by their `detect` rule: (call, call_suffix, type).

    Two call entries may share the same leaf `name` if they are
    distinguished by a `receiver` field (e.g. `from_owned_ptr` matches both
    `Py::from_owned_ptr` and `Bound::from_owned_ptr`; the former is
    deprecated, the latter is its replacement). The `call_apis` map
    therefore stores a *list* of entries per name; `_scan_file` picks the
    matching one by checking the call's qualifying path.
    """
    call_apis: dict[str, list[dict]] = {}
    call_suffix_apis: list[tuple[str, dict]] = []
    type_apis: dict[str, dict] = {}
    for entry in apis.values():
        detect = entry.get("detect") or {}
        kind, name = detect.get("kind"), detect.get("name")
        if not name:
            continue
        if kind == "call":
            call_apis.setdefault(name, []).append(entry)
        elif kind == "call_suffix":
            call_suffix_apis.append((name, entry))
        elif kind == "type":
            type_apis[name] = entry
    return call_apis, call_suffix_apis, type_apis


def _call_qualifier(full_callee: str) -> str:
    """Return the segment immediately before the leaf method name.

    For `Py::from_owned_ptr` this is `Py`; for `pyo3::Py::from_owned_ptr`
    it is also `Py`; for `obj.from_owned_ptr` it is empty (method call,
    qualifier is the receiver variable not a type). Used by the v0.2
    `receiver` filter to distinguish `Py::from_owned_ptr` (deprecated) from
    `Bound::from_owned_ptr` (the replacement).
    """
    # Strip method-call dot syntax: `a.b.c.method` has no path qualifier.
    if "." in full_callee.split("::", 1)[0]:
        return ""
    head = full_callee.rsplit("::", 1)[0] if "::" in full_callee else ""
    return head.rsplit("::", 1)[-1] if head else ""


def _call_entry_for(call: dict, candidates: list[dict]) -> dict | None:
    """Pick the matching entry from a list sharing the same leaf name.

    If an entry has no `receiver` filter, it is the default. Entries with
    a `receiver` only match calls whose qualifier equals that receiver.
    """
    qualifier = _call_qualifier(call["full_callee"])
    fallback: dict | None = None
    for entry in candidates:
        receiver = (entry.get("detect") or {}).get("receiver")
        if receiver is None:
            fallback = entry
        elif qualifier == receiver:
            return entry
    return fallback


def _scan_file(
    tree: object,
    source: bytes,
    stripped: bytes,
    rel: str,
    version: tuple[int, int],
    detectors: tuple[dict, list, dict],
) -> list[dict]:
    """Find versioned-API uses in one parsed source file."""
    call_apis, call_suffix_apis, type_apis = detectors
    findings: list[dict] = []

    def _emit(entry, node, line, column, conf):
        verdict = _classify_api_use(entry, version)
        if verdict is None:
            return
        ftype, classification = verdict
        if classification != "FIX" and is_suppressed_by_comment(source, tree, line):
            classification = "ACCEPTABLE"
        findings.append(
            make_finding(
                ftype,
                classification=classification,
                confidence=conf,
                description=_describe(entry, ftype),
                file=rel,
                line=line,
                column=column,
                function=enclosing_function_name(node, source),
                category="version_compat",
                fix_template=(
                    f"Migrate to `{entry.get('replacement') or entry.get('renamed_to')}`."
                ),
                details={
                    "api": entry.get("rust_signature_pattern"),
                    "category": entry.get("category"),
                    "replacement": entry.get("replacement") or entry.get("renamed_to"),
                },
            )
        )

    for call in find_calls_in_scope(tree, stripped):
        node = call["node"]
        column = node.start_point[1] + 1
        candidates = call_apis.get(call["name"])
        if candidates:
            entry = _call_entry_for(call, candidates)
            if entry is not None:
                _emit(entry, node, call["line"], column, "MEDIUM")
                continue
        for suffix, suffix_entry in call_suffix_apis:
            if call["name"].endswith(suffix) and call["name"] != suffix:
                _emit(suffix_entry, node, call["line"], column, "MEDIUM")
                break

    for node in walk(tree, "type_identifier"):
        entry = type_apis.get(text_of(node, stripped))
        if entry is not None:
            _emit(
                entry,
                node,
                node.start_point[0] + 1,
                node.start_point[1] + 1,
                "HIGH",
            )
    return findings


def _abi3_free_threading(discovery: dict) -> list[dict]:
    """One crate-level finding: `abi3` enabled on a free-threading crate."""
    features = discovery.get("pyo3_features") or []
    if not any(str(f).startswith("abi3") for f in features):
        return []
    if not discovery.get("free_threading_target"):
        return []
    return [
        make_finding(
            "abi3_free_threading_conflict",
            classification="CONSIDER",
            confidence="HIGH",
            description=(
                "The `abi3` feature is enabled and the crate targets "
                "free-threaded Python. PyO3 silently ignores `abi3` under a "
                "free-threaded build -- the stable ABI has no free-threaded "
                "variant, so the produced wheel is not actually abi3."
            ),
            file="Cargo.toml",
            line=0,
            category="version_compat",
            fix_template=(
                "Build a separate non-abi3 wheel for free-threaded "
                "interpreters, or drop the free-threading target."
            ),
            details={"features": list(features)},
        )
    ]


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan a Rust/PyO3 crate for version-compatibility problems."""
    discovery = discover(target)
    project_root = Path(discovery["project_root"])
    files = discover_rust_files(Path(discovery["scan_root"]), max_files=max_files)
    version = pyo3_version_tuple(discovery.get("pyo3_version"))
    detectors = _build_detectors(load_data_file("pyo3_api_tables.json").get("apis", {}))

    findings: list[dict] = list(_abi3_free_threading(discovery))
    functions_analyzed = 0
    for path in files:
        source = path.read_bytes()
        tree = parse_bytes(source)
        stripped = strip_comments(source)
        rel = relative_path(path, project_root)
        functions_analyzed += len(extract_fn_items(tree, stripped))
        findings.extend(_scan_file(tree, source, stripped, rel, version, detectors))

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
