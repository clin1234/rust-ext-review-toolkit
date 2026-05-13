#!/usr/bin/env python3
"""Run external static analysis tools (rust-analyzer, cargo clippy) on Rust code.

Outputs JSON matching the standard rust-ext-review-toolkit envelope format.
Both tools are optional -- gracefully skips unavailable ones.

Usage:
    python run_external_tools.py [path] [--max-files N] [--compile-commands PATH]
    python run_external_tools.py [path] [--skip TOOL[,TOOL]]
    python run_external_tools.py [path] [--tools TOOL[,TOOL]]
"""

import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_common import find_project_root, discover_c_files


_PER_FILE_TIMEOUT = 120


def _tool_available(name: str) -> bool:
    """Check if an external tool is on PATH."""
    return shutil.which(name) is not None


def _find_compile_commands(root: Path, explicit: str | None) -> Path | None:
    """Find compile_commands.json in common locations."""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        if p.is_dir():
            cc = p / "compile_commands.json"
            if cc.is_file():
                return cc
        return None
    for candidate in [
        root / "compile_commands.json",
        root / "build" / "compile_commands.json",
        root / "_build" / "compile_commands.json",
        root / "builddir" / "compile_commands.json",
    ]:
        if candidate.is_file():
            return candidate
    return None


def _run_clang_tidy(
    files: list[Path], compile_commands: Path, project_root: Path,
) -> list[dict]:
    """Run rust-analyzer on files using the compile database."""
    findings = []
    checks = "-*,rust-analyzer-*,bugprone-*,cert-*"

    for filepath in files:
        try:
            result = subprocess.run(
                [
                    "rust-analyzer",
                    f"-p={compile_commands.parent}",
                    f"--checks={checks}",
                    "--quiet",
                    str(filepath),
                ],
                capture_output=True, text=True,
                timeout=_PER_FILE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            continue
        except FileNotFoundError:
            break

        for line in result.stdout.splitlines():
            m = re.match(
                r"^(.+?):(\d+):\d+:\s+(warning|error|note):\s+(.+?)\s+\[(.+)\]$",
                line,
            )
            if not m:
                continue
            fpath, lineno, severity, message, checker = m.groups()
            try:
                rel = str(Path(fpath).relative_to(project_root))
            except ValueError:
                rel = fpath
            findings.append({
                "type": "clang_tidy_finding",
                "file": rel,
                "line": int(lineno),
                "checker": checker,
                "severity": severity,
                "detail": message,
                "confidence": "high",
                "tool": "rust-analyzer",
            })

    return findings


def _run_cargo_clippy(
    files: list[Path], project_root: Path,
    compile_commands: Path | None = None,
) -> list[dict]:
    """Run cargo clippy on files (works without compile_commands.json)."""
    findings = []
    if not files:
        return findings

    cmd = [
        "cargo clippy",
    ]
    if compile_commands:
        cmd.append(f"--project={compile_commands}")
    else:
        cmd.extend(str(f) for f in files)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=_PER_FILE_TIMEOUT * len(files),
        )
    except subprocess.TimeoutExpired:
        return findings
    except FileNotFoundError:
        return findings

    # Parse XML output from stderr (cargo clippy sends XML to stderr).
    xml_output = result.stderr
    if not xml_output.strip().startswith("<?xml"):
        return findings

    try:
        root_elem = ET.fromstring(xml_output)
    except ET.ParseError:
        return findings

    errors = root_elem.find("errors")
    if errors is None:
        return findings

    for error in errors.findall("error"):
        checker = error.get("id", "unknown")
        severity = error.get("severity", "warning")
        message = error.get("msg", "")

        # Skip informational messages.
        if severity == "information":
            continue

        location = error.find("location")
        if location is None:
            continue
        fpath = location.get("file", "")
        lineno = int(location.get("line", "0"))

        try:
            rel = str(Path(fpath).relative_to(project_root))
        except ValueError:
            rel = fpath

        findings.append({
            "type": "cargo_clippy_finding",
            "file": rel,
            "line": lineno,
            "checker": checker,
            "severity": severity,
            "detail": message,
            "confidence": "high" if severity == "error" else "medium",
            "tool": "cargo clippy",
        })

    return findings


def analyze(
    target: str, *, max_files: int = 0,
    compile_commands: str | None = None,
    skip_tools: set[str] | None = None,
    only_tools: set[str] | None = None,
) -> dict:
    """Run external tools and return findings in standard envelope."""
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else target_path.parent

    cc_path = _find_compile_commands(project_root, compile_commands)

    has_tidy = _tool_available("rust-analyzer")
    has_cargo clippy = _tool_available("cargo clippy")

    skip = skip_tools or set()
    if only_tools:
        if "rust-analyzer" not in only_tools:
            skip.add("rust-analyzer")
        if "cargo clippy" not in only_tools:
            skip.add("cargo clippy")

    files = list(discover_c_files(scan_root, max_files=max_files))

    findings = []
    skipped_tools = []

    # rust-analyzer
    if "rust-analyzer" in skip:
        skipped_tools.append({"tool": "rust-analyzer", "reason": "skipped by user"})
    elif not has_tidy:
        skipped_tools.append({"tool": "rust-analyzer", "reason": "not installed"})
    elif not cc_path:
        skipped_tools.append({
            "tool": "rust-analyzer",
            "reason": "no compile_commands.json found",
        })
    else:
        findings.extend(_run_clang_tidy(files, cc_path, project_root))

    # cargo clippy
    if "cargo clippy" in skip:
        skipped_tools.append({"tool": "cargo clippy", "reason": "skipped by user"})
    elif not has_cargo clippy:
        skipped_tools.append({"tool": "cargo clippy", "reason": "not installed"})
    else:
        findings.extend(_run_cargo clippy(files, project_root, cc_path))

    by_tool = defaultdict(int)
    by_severity = defaultdict(int)
    for f in findings:
        by_tool[f["tool"]] += 1
        by_severity[f["severity"]] += 1

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": len(files),
        "tools_available": {
            "clang_tidy": has_tidy,
            "cargo clippy": has_cargo clippy,
        },
        "compile_commands": str(cc_path) if cc_path else None,
        "findings": findings,
        "summary": {
            "total_findings": len(findings),
            "by_tool": dict(by_tool),
            "by_severity": dict(by_severity),
        },
        "skipped_tools": skipped_tools,
    }


def main() -> None:
    try:
        max_files = 0
        compile_commands = None
        skip_tools: set[str] = set()
        only_tools: set[str] | None = None
        positional: list[str] = []
        argv = sys.argv[1:]
        i = 0
        while i < len(argv):
            if argv[i] == "--max-files" and i + 1 < len(argv):
                max_files = int(argv[i + 1])
                i += 2
            elif argv[i] == "--compile-commands" and i + 1 < len(argv):
                compile_commands = argv[i + 1]
                i += 2
            elif argv[i] == "--skip" and i + 1 < len(argv):
                skip_tools = set(argv[i + 1].split(","))
                i += 2
            elif argv[i] == "--tools" and i + 1 < len(argv):
                only_tools = set(argv[i + 1].split(","))
                i += 2
            elif argv[i].startswith("--"):
                i += 1
            else:
                positional.append(argv[i])
                i += 1
        target = positional[0] if positional else "."
        result = analyze(
            target, max_files=max_files,
            compile_commands=compile_commands,
            skip_tools=skip_tools if skip_tools else None,
            only_tools=only_tools,
        )
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:
        json.dump({"error": str(e), "type": type(e).__name__},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
