#!/usr/bin/env python3
"""Shared utilities for rust-ext-review-toolkit analysis scripts.

Project-root detection, Rust source-file discovery, data-table loading, the
common finding shape, deduplication, comment-based suppression, and CLI
argument parsing. Every scanner imports from here and from ``rust_ts_utils``.
"""

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rust_ts_utils import find_enclosing, text_of, walk  # noqa: E402


EXCLUDE_DIRS = frozenset(
    {
        ".git",
        "target",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
    }
)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def find_project_root(start: Path) -> Path:
    """Find the crate/project root by climbing for a marker file."""
    current = start if start.is_dir() else start.parent
    for _ in range(25):
        for marker in ("Cargo.toml", ".git", "pyproject.toml"):
            if (current / marker).exists():
                return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return start if start.is_dir() else start.parent


def discover_rust_files(root: Path, *, max_files: int = 0) -> list[Path]:
    """Return `.rs` source files under root, skipping build/vendor dirs."""
    root = Path(root)
    if root.is_file():
        return [root] if root.suffix == ".rs" else []
    found: list[Path] = []
    for path in sorted(root.rglob("*.rs")):
        if not path.is_file():
            continue
        try:
            parts = set(path.relative_to(root).parts)
        except ValueError:
            continue
        if parts & EXCLUDE_DIRS:
            continue
        found.append(path)
        if max_files and len(found) >= max_files:
            break
    return found


def relative_path(path: Path, root: Path) -> str:
    """Path relative to root if possible, else the absolute path string."""
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def load_data_file(name: str) -> dict:
    """Load a JSON data file from the plugin's data/ directory.

    Returns {} (and warns on stderr) if the file is missing or malformed.
    """
    path = _DATA_DIR / name
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"failed to load {name}: {e}"}), file=sys.stderr)
        return {}


def make_finding(
    finding_type: str,
    *,
    classification: str,
    description: str,
    file: str = "",
    line: int = 0,
    column: int = 0,
    function: str = "",
    category: str = "",
    confidence: str = "HIGH",
    fix_template: str = "",
    details: dict | None = None,
    **extra: object,
) -> dict:
    """Build a finding dict in the common JSON shape (design §4.2)."""
    finding: dict = {
        "type": finding_type,
        "file": file,
        "line": line,
        "column": column,
        "function": function,
        "category": category,
        "classification": classification,
        "confidence": confidence,
        "description": description,
        "fix_template": fix_template,
        "details": details if details is not None else {},
    }
    finding.update(extra)
    return finding


def deduplicate_findings(findings: list[dict]) -> list[dict]:
    """Collapse findings sharing (type, file, normalised description).

    The first occurrence is kept and annotated with ``duplicate_count`` and
    ``duplicate_locations``.
    """

    def _normalise(text: str) -> str:
        text = re.sub(r"\bline \d+\b", "line N", text)
        text = re.sub(r"`[^`]+`", "`X`", text)
        text = re.sub(r"'[^']+'", "'X'", text)
        return text

    groups: dict[tuple[str, str, str, str], list[dict]] = {}
    for f in findings:
        # Include `function` in the dedup key so that the SAME finding-type on
        # TWO different targets in the same file does not collapse into one --
        # the description normaliser back-ticks out identifier text, which
        # would otherwise make "`Foo` missing __traverse__" and "`Bar` missing
        # __traverse__" key-equal. (See v0.2 toolkit feedback from cryptography
        # review: `declarative_asn1::Type` was missed this way.)
        groups.setdefault(
            (
                str(f.get("type", "")),
                str(f.get("file", "")),
                str(f.get("function", "")),
                _normalise(str(f.get("description", ""))),
            ),
            [],
        ).append(f)

    result: list[dict] = []
    for group in groups.values():
        canonical = group[0]
        if len(group) > 1:
            canonical["duplicate_count"] = len(group) - 1
            canonical["duplicate_locations"] = [
                {"file": d.get("file", ""), "line": d.get("line", 0)} for d in group[1:]
            ]
        result.append(canonical)
    return result


def _tool_available(executable: str) -> bool:
    """True if an external executable is resolvable on PATH."""
    return shutil.which(executable) is not None


def build_report(
    discovery: dict,
    findings: list[dict],
    *,
    functions_analyzed: int = 0,
) -> dict:
    """Assemble the common JSON report envelope (design §4.2).

    ``discovery`` is the dict returned by ``discover_rust_ext.discover``;
    its crate metadata is projected into ``crate_info``. ``findings`` should
    already be deduplicated by the caller. The ``summary`` counts are derived
    from the findings list.
    """
    crate_info = {
        "crate_name": discovery.get("crate_name", ""),
        "module_name": discovery.get("module_name"),
        "pyo3_version": discovery.get("pyo3_version"),
        "pyo3_features": discovery.get("pyo3_features", []),
        "python_floor": discovery.get("python_floor"),
        "abi3t_python_floor": discovery.get("abi3t_python_floor"),
        "build_backend": discovery.get("build_backend", "unknown"),
        "free_threading_target": discovery.get("free_threading_target", False),
        "source_files": discovery.get("source_files", []),
        "tree_sitter_available": True,
        "clippy_available": _tool_available("cargo-clippy"),
        "miri_available": _tool_available("cargo-miri"),
        "cargo_metadata_available": _tool_available("cargo"),
    }
    by_type: dict[str, int] = {}
    by_classification: dict[str, int] = {}
    for finding in findings:
        ftype = str(finding.get("type", ""))
        cls = str(finding.get("classification", ""))
        by_type[ftype] = by_type.get(ftype, 0) + 1
        by_classification[cls] = by_classification.get(cls, 0) + 1
    return {
        "project_root": discovery.get("project_root", ""),
        "scan_root": discovery.get("scan_root", ""),
        "crate_info": crate_info,
        "functions_analyzed": functions_analyzed,
        "findings": findings,
        "summary": {"by_type": by_type, "by_classification": by_classification},
    }


# Comment markers that suppress a finding (the Rust `// SAFETY:` convention
# plus toolkit-specific opt-outs).
_SAFETY_KEYWORDS = (
    "safety:",
    "rust-ext-safe:",
    "safe because",
    "safe:",
    "invariant:",
    "intentional",
    "by design",
    "checked above",
    "verified",
    "not a bug",
    "deliberately",
)


def extract_nearby_comments(
    source_bytes: bytes, tree: object, line: int, radius: int = 4
) -> list[str]:
    """Return the text of comments within +/- radius lines of ``line``."""
    lo = max(0, line - radius - 1)
    hi = line + radius - 1
    comments: list[str] = []
    for node in walk(tree):
        if not node.type.endswith("comment"):
            continue
        if lo <= node.start_point[0] <= hi:
            comments.append(text_of(node, source_bytes))
    return comments


def has_safety_annotation(comments: list[str]) -> bool:
    """True if any comment carries a safety / suppression annotation."""
    for comment in comments:
        lower = comment.lower()
        if any(kw in lower for kw in _SAFETY_KEYWORDS):
            return True
    return False


def is_suppressed_by_comment(
    source_bytes: bytes, tree: object, line: int, radius: int = 3
) -> bool:
    """True if a finding at ``line`` is suppressed by a nearby `// SAFETY:` comment."""
    return has_safety_annotation(
        extract_nearby_comments(source_bytes, tree, line, radius)
    )


def is_in_region(offset: int, regions: list[tuple[int, int]]) -> bool:
    """True if a byte offset falls within any (start, end) region."""
    return any(start <= offset < end for start, end in regions)


# A CPython C-API symbol name: `Py_INCREF`, `PyDict_New`, `_PyObject_New`.
# Matched against a *called leaf name* -- inside an `unsafe` block or a
# GIL-released closure this reliably marks a raw `pyo3-ffi` call.
_CAPI_SYMBOL_RE = re.compile(r"_?Py_?[A-Z]")


def is_capi_symbol(name: str) -> bool:
    """True if a called name follows the CPython C-API naming convention."""
    return bool(_CAPI_SYMBOL_RE.match(name))


# Substrings that mark a receiver/value chain as PyResult-shaped. Deliberately
# loose: the agent triage step separates real PyResults from false hits. Kept
# to strongly PyO3-specific tokens -- generic ones (`.len()`, `.downcast`,
# `.cast`) collide with stdlib / third-party methods (`Vec::len`,
# `Any::downcast_ref`, `Series::cast`) and are false-positive magnets.
PYRESULT_HINTS = (
    ".call_method",
    ".call0",
    ".call1",
    ".call(",
    ".getattr",
    ".setattr",
    ".delattr",
    ".hasattr",
    ".import",
    ".import_bound",
    ".extract",
    ".get_item",
    ".set_item",
    ".del_item",
    ".eval",
    ".try_iter",
    ".get_type",
    ".add_class",
    ".add_function",
    ".add_submodule",
    "::new_bound",
    "PyResult",
)


def looks_like_pyresult(text: str) -> bool:
    """Heuristic: does this receiver/value text look like a PyResult source?"""
    return any(hint in text for hint in PYRESULT_HINTS)


def enclosing_function_name(node: Any, source: bytes) -> str:
    """Name of the `fn` item enclosing ``node``, or '' if there is none."""
    fn = find_enclosing(node, "function_item")
    if fn is None:
        return ""
    name = fn.child_by_field_name("name")
    return text_of(name, source) if name is not None else ""


def pyo3_version_tuple(version: str | None) -> tuple[int, int]:
    """(major, minor) for a PyO3 version string; (0, 28) baseline if unknown."""
    if not version:
        return (0, 28)
    parts = version.lstrip("=^~> v").split(".")
    try:
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (ValueError, IndexError):
        return (0, 28)


# Bare-flag options recognised inside `#[pyclass(...)]` and similar attributes.
_ATTR_FLAGS = (
    "frozen",
    "unsendable",
    "subclass",
    "dict",
    "weakref",
    "eq",
    "ord",
    "hash",
)


def parse_attribute_options(args_text: str | None) -> dict:
    """Parse an attribute option list, e.g. ``(frozen, extends = Parent)``.

    Returns recognised bare flags as ``True`` and the ``extends`` / ``name``
    key=value options as strings. Not a full Rust parser -- it recognises the
    option names the scanners care about (recipes SS3.5).
    """
    options: dict = {}
    if not args_text:
        return options
    text = args_text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    for flag in _ATTR_FLAGS:
        if re.search(rf"(?:^|[(,\s]){flag}\s*(?:,|$|[)\s])", text):
            options[flag] = True
    extends = re.search(r"\bextends\s*=\s*([A-Za-z_][\w:]*)", text)
    if extends:
        options["extends"] = extends.group(1)
    name = re.search(r'\bname\s*=\s*"([^"]*)"', text)
    if name:
        options["name"] = name.group(1)
    return options


def type_breaks_bound(type_text: str, trait: str, non_send_data: dict) -> str | None:
    """Return why ``type_text`` fails ``trait`` (Send/Sync), or None if it is fine.

    ``non_send_data`` is the parsed ``non_send_sync_types.json``.
    """
    want = "send" if trait == "Send" else "sync"
    for entry in non_send_data.get("types", []):
        if entry.get(want, True):
            continue  # this type satisfies the bound
        candidates = list(entry.get("aliases", []))
        if entry.get("path"):
            candidates.append(entry["path"])
        for alias in candidates:
            if alias and re.search(rf"\b{re.escape(alias)}\b", type_text):
                return entry.get("note") or f"`{alias}` is not `{trait}`"
    for pattern in non_send_data.get("raw_pointer_patterns", []):
        if pattern in type_text:
            return f"a raw pointer (`{pattern.strip()}`) is neither Send nor Sync"
    return None


def parse_common_args(argv: list[str]) -> tuple[str, int]:
    """Parse the common CLI arguments. Returns (target, max_files).

    Recognises ``--max-files N``; any other ``--flag`` is skipped so callers
    can pass extra options without breaking.
    """
    max_files = 0
    positional: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            try:
                max_files = int(argv[i + 1])
            except ValueError:
                print(
                    json.dumps(
                        {
                            "error": (
                                f"--max-files needs an integer, got {argv[i + 1]!r}"
                            )
                        }
                    )
                )
                sys.exit(2)
            i += 2
        elif argv[i].startswith("--"):
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    return (positional[0] if positional else "."), max_files
