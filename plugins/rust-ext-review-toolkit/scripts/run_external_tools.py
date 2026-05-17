#!/usr/bin/env python3
"""Run external Rust tooling (cargo metadata, clippy) on a PyO3 crate.

This is the opportunistic external-tool baseline (design SS2.3). It is
deliberately conservative about executing code:

  - `cargo metadata` runs by default -- it resolves the dependency graph
    without compiling, so it executes no build scripts. It confirms the exact
    PyO3 version and feature set.
  - `cargo clippy` runs **only** with `--run-clippy`. Clippy compiles the
    crate, which executes `build.rs` and procedural macros -- arbitrary code.
    A review tool should not do that to possibly-untrusted code unprompted.
  - miri / cargo-geiger / cargo-expand are detected and reported but not run
    (nightly-only, slow, or deep-mode tools); the agent is told the command.

All output is JSON. Every tool is optional; unavailable ones are listed in
`skipped_tools` with a reason.

Usage:
    python run_external_tools.py [path] [--run-clippy] [--max-files N]
"""

import json
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_common import find_project_root  # noqa: E402

_METADATA_TIMEOUT = 120
_CLIPPY_TIMEOUT = 600


def _tool_available(executable: str) -> bool:
    """True if an external executable is resolvable on PATH."""
    return shutil.which(executable) is not None


def _run_cargo_metadata(root: Path) -> dict:
    """Resolve the dependency graph (no compilation) and extract PyO3 info."""
    try:
        result = subprocess.run(
            ["cargo", "metadata", "--format-version", "1"],
            capture_output=True,
            text=True,
            cwd=str(root),
            timeout=_METADATA_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return {"error": f"cargo metadata failed: {e}"}
    if result.returncode != 0:
        return {
            "error": "cargo metadata returned non-zero",
            "stderr": result.stderr[-400:],
        }
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "cargo metadata output was not valid JSON"}

    pyo3_pkgs = [p for p in data.get("packages", []) if p.get("name") == "pyo3"]
    if not pyo3_pkgs:
        return {"pyo3_in_graph": False}
    pyo3 = pyo3_pkgs[0]
    enabled_features: list[str] = []
    for node in data.get("resolve", {}).get("nodes", []):
        if node.get("id") == pyo3.get("id"):
            enabled_features = node.get("features", [])
            break
    return {
        "pyo3_in_graph": True,
        "pyo3_version": pyo3.get("version"),
        "pyo3_enabled_features": enabled_features,
        "pyo3_versions_in_graph": sorted(
            {p.get("version") for p in pyo3_pkgs if p.get("version")}
        ),
    }


def _run_clippy(root: Path, project_root: Path) -> tuple[list[dict], str | None]:
    """Run `cargo clippy --message-format=json` and parse the lints."""
    try:
        result = subprocess.run(
            ["cargo", "clippy", "--message-format=json", "--quiet"],
            capture_output=True,
            text=True,
            cwd=str(root),
            timeout=_CLIPPY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return [], "clippy timed out"
    except (FileNotFoundError, OSError) as e:
        return [], f"clippy could not run: {e}"

    findings: list[dict] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("reason") != "compiler-message":
            continue
        message = record.get("message") or {}
        code = ((message.get("code") or {}).get("code")) or ""
        if not code.startswith("clippy::"):
            continue
        primary = next(
            (s for s in message.get("spans", []) if s.get("is_primary")), None
        )
        if primary is None:
            continue
        fpath = primary.get("file_name", "")
        try:
            rel = str(Path(fpath).resolve().relative_to(project_root))
        except ValueError:
            rel = fpath
        findings.append(
            {
                "type": "clippy_lint",
                "file": rel,
                "line": primary.get("line_start", 0),
                "lint": code,
                "severity": message.get("level", "warning"),
                "detail": message.get("message", ""),
                "tool": "clippy",
            }
        )
    return findings, None


def analyze(target: str, *, max_files: int = 0, run_clippy: bool = False) -> dict:
    """Run the external-tool baseline. ``max_files`` is accepted but unused
    (cargo tooling operates on the whole crate)."""
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else target_path.parent

    tools_available = {
        "cargo": _tool_available("cargo"),
        "clippy": _tool_available("cargo-clippy"),
        "miri": _tool_available("cargo-miri"),
        "cargo_geiger": _tool_available("cargo-geiger"),
        "cargo_expand": _tool_available("cargo-expand"),
    }

    findings: list[dict] = []
    skipped: list[dict] = []
    cargo_metadata: dict | None = None

    if tools_available["cargo"]:
        cargo_metadata = _run_cargo_metadata(project_root)
    else:
        skipped.append({"tool": "cargo metadata", "reason": "cargo is not installed"})

    if not run_clippy:
        skipped.append(
            {
                "tool": "clippy",
                "reason": (
                    "not run by default -- clippy compiles the crate, which "
                    "executes build.rs and proc-macros; pass --run-clippy to "
                    "opt in"
                ),
            }
        )
    elif not tools_available["clippy"]:
        skipped.append({"tool": "clippy", "reason": "cargo-clippy is not installed"})
    else:
        clippy_findings, error = _run_clippy(project_root, project_root)
        if error is not None:
            skipped.append({"tool": "clippy", "reason": error})
        else:
            findings.extend(clippy_findings)

    # Detection-only tools: report availability and the manual command.
    for tool, available, command in (
        ("miri", tools_available["miri"], "cargo +nightly miri test"),
        ("cargo-geiger", tools_available["cargo_geiger"], "cargo geiger"),
        ("cargo-expand", tools_available["cargo_expand"], "cargo expand"),
    ):
        skipped.append(
            {
                "tool": tool,
                "reason": (
                    f"detection only -- run manually: `{command}`"
                    if available
                    else "not installed"
                ),
            }
        )

    by_lint: dict[str, int] = defaultdict(int)
    by_severity: dict[str, int] = defaultdict(int)
    for finding in findings:
        by_lint[finding["lint"]] += 1
        by_severity[finding["severity"]] += 1

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "tools_available": tools_available,
        "cargo_metadata": cargo_metadata,
        "findings": findings,
        "summary": {
            "total_findings": len(findings),
            "by_lint": dict(by_lint),
            "by_severity": dict(by_severity),
        },
        "skipped_tools": skipped,
    }


def main() -> None:
    try:
        argv = sys.argv[1:]
        target = "."
        max_files = 0
        run_clippy = False
        i = 0
        while i < len(argv):
            if argv[i] == "--max-files" and i + 1 < len(argv):
                try:
                    max_files = int(argv[i + 1])
                except ValueError:
                    pass
                i += 2
            elif argv[i] == "--run-clippy":
                run_clippy = True
                i += 1
            elif not argv[i].startswith("-"):
                target = argv[i]
                i += 1
            else:
                i += 1
        result = analyze(target, max_files=max_files, run_clippy=run_clippy)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:  # noqa: BLE001 -- top-level guard, emit JSON error
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
