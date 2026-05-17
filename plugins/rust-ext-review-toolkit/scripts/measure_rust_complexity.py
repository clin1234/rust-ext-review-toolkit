#!/usr/bin/env python3
"""Measure per-function complexity in Rust/PyO3 extension code.

For every `fn` the scanner computes a complexity score (design SS3.2.10,
recipes SS3.10):

    score = LOC/100 + cyclomatic/10 + nesting/5 + params/7 + unsafe_bonus

where cyclomatic = 1 + count of `if` / `match` arms / `while` / `for` /
`loop` / `?` / `&&` / `||`, nesting is the deepest block/closure/match
nesting, and `unsafe_bonus` adds 1 per `unsafe` block (an unsafe-heavy
function is reviewed at higher priority regardless of size).

Functions scoring at or above the hotspot threshold (5.0) are reported as
`complex_function` findings, ranked by score; the report also carries a
`complexity_stats` distribution summary.

Usage:
    python measure_rust_complexity.py [path] [--max-files N]
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_rust_ext import discover  # noqa: E402
from rust_ts_utils import (  # noqa: E402
    extract_fn_items,
    parse_bytes,
    strip_comments,
    text_of,
    walk,
)
from scan_common import (  # noqa: E402
    build_report,
    discover_rust_files,
    make_finding,
    parse_common_args,
    relative_path,
)

# Nodes that each add 1 to cyclomatic complexity.
_DECISION_TYPES = frozenset(
    {
        "if_expression",
        "match_arm",
        "while_expression",
        "for_expression",
        "loop_expression",
        "try_expression",
    }
)
# Nodes that introduce a nesting level.
_NEST_TYPES = frozenset({"block", "closure_expression", "match_block"})

_HOTSPOT_THRESHOLD = 5.0


def _max_nesting(body: Any) -> int:
    """Deepest block/closure/match nesting within a function body."""
    best = 1
    for node in walk(body):
        if node.type not in _NEST_TYPES:
            continue
        depth = 0
        current: Any = node
        while current is not None:
            if current.type in _NEST_TYPES:
                depth += 1
            if current.id == body.id:
                break
            current = current.parent
        best = max(best, depth)
    return best


def _count_params(params_node: Any) -> int:
    """Count declared parameters (including `self`)."""
    if params_node is None:
        return 0
    return sum(
        1
        for child in params_node.children
        if child.type in ("parameter", "self_parameter")
    )


def _score_function(fn: dict, source: bytes) -> dict | None:
    """Compute the complexity metrics and score for one function."""
    body = fn["body_node"]
    if body is None:
        return None
    loc = fn["end_line"] - fn["start_line"] + 1
    cyclomatic = 1
    unsafe_blocks = 0
    for node in walk(body):
        node_type = node.type
        if node_type in _DECISION_TYPES:
            cyclomatic += 1
        elif node_type == "binary_expression":
            operator = node.child_by_field_name("operator")
            if operator is not None and text_of(operator, source) in ("&&", "||"):
                cyclomatic += 1
        elif node_type == "unsafe_block":
            unsafe_blocks += 1
    nesting = _max_nesting(body)
    params = _count_params(fn["params_node"])
    score = (
        loc / 100 + cyclomatic / 10 + nesting / 5 + params / 7 + float(unsafe_blocks)
    )
    return {
        "loc": loc,
        "cyclomatic": cyclomatic,
        "nesting": nesting,
        "params": params,
        "unsafe_blocks": unsafe_blocks,
        "score": round(score, 2),
    }


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Score every function in a Rust/PyO3 crate by complexity."""
    discovery = discover(target)
    project_root = Path(discovery["project_root"])
    files = discover_rust_files(Path(discovery["scan_root"]), max_files=max_files)

    scores: list[float] = []
    findings: list[dict] = []
    for path in files:
        source = path.read_bytes()
        tree = parse_bytes(source)
        stripped = strip_comments(source)
        rel = relative_path(path, project_root)
        for fn in extract_fn_items(tree, stripped):
            metrics = _score_function(fn, stripped)
            if metrics is None:
                continue
            scores.append(metrics["score"])
            if metrics["score"] < _HOTSPOT_THRESHOLD:
                continue
            findings.append(
                make_finding(
                    "complex_function",
                    classification="CONSIDER",
                    confidence="HIGH",
                    description=(
                        f"`{fn['name']}` has a complexity score of "
                        f"{metrics['score']} (LOC {metrics['loc']}, cyclomatic "
                        f"{metrics['cyclomatic']}, nesting {metrics['nesting']}, "
                        f"params {metrics['params']}, unsafe blocks "
                        f"{metrics['unsafe_blocks']}) -- a candidate for "
                        f"refactoring and closer review."
                    ),
                    file=rel,
                    line=fn["start_line"],
                    column=fn["node"].start_point[1] + 1,
                    function=fn["name"],
                    category="complexity",
                    fix_template=(
                        "Extract cohesive sub-steps into helper functions; "
                        "flatten nesting with early returns / `?`."
                    ),
                    details=metrics,
                )
            )

    findings.sort(key=lambda f: f["details"]["score"], reverse=True)
    report = build_report(discovery, findings, functions_analyzed=len(scores))
    report["complexity_stats"] = {
        "functions_scored": len(scores),
        "hotspot_threshold": _HOTSPOT_THRESHOLD,
        "hotspots": len(findings),
        "max_score": round(max(scores), 2) if scores else 0.0,
        "mean_score": round(sum(scores) / len(scores), 2) if scores else 0.0,
    }
    return report


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
