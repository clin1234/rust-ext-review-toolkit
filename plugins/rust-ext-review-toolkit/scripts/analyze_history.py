#!/usr/bin/env python3
"""Analyze git history of a Rust/PyO3 extension for churn and bug patterns.

Adapted from code-review-toolkit / cext-review-toolkit's `analyze_history.py`.
Commit classification keywords are tuned for Rust/PyO3 (design SS3.2.11), and
function-boundary detection uses Tree-sitter-rust. Co-change coupling analysis
is dropped -- Rust extensions are small and the signal is noisy at that scale.

Output (consumed by the git-history-analyzer agent):
  - file_churn / function_churn -- the churn x quality risk matrix
  - recent_fixes -- bug/safety/panic/concurrency commits, with diffs, for
    similar-bug detection (the crown-jewel capability)
  - recent_migrations -- PyO3-migration commits, with diffs

Takes `argv` (not the `(target, max_files)` convention) to match
code-review-toolkit.

Usage:
    python analyze_history.py [path] [options]

Options:
    --days N          Analyze last N days (default: 365)
    --since DATE      Start date (ISO format, overrides --days)
    --until DATE      End date (ISO format, default: today)
    --last N          Analyze exactly the last N commits
    --max-commits N   Cap total commits analyzed (default: 2000)
    --max-files N     Cap files scanned for function boundaries
    --workers N       Parallel git subprocess workers (default: 8)
    --no-function     Skip function-level churn (file-level only, faster)
"""

import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rust_ts_utils import extract_fn_items, parse_bytes  # noqa: E402
from scan_common import find_project_root  # noqa: E402

# Commit categories, tuned for Rust/PyO3. First match wins; the Rust-specific
# categories precede the generic `bugfix` so "fix unsafe UB" ranks as safety.
CLASSIFICATION_RULES: list[tuple[str, list[str]]] = [
    (
        "safety",
        [
            "unsafe",
            "soundness",
            "miri",
            "use-after-free",
            "use after free",
            "data race",
            "undefined behavior",
            "undefined behaviour",
        ],
    ),
    ("panic", ["panic", "unwrap", ".expect(", "unwind", "abort"]),
    (
        "concurrency",
        [
            "frozen",
            "free-thread",
            "freethread",
            "free thread",
            "gil_used",
            "thread-safe",
            "thread safety",
            "deadlock",
            "send + sync",
            "non-send",
        ],
    ),
    (
        "migration",
        [
            "clone_ref",
            "into_pyobject",
            "deprecat",
            "migrat",
            "pyo3 0.",
            "pyo3 upgrade",
            "bound api",
            "_bound(",
        ],
    ),
    (
        "bugfix",
        ["fix", "bug", "crash", "segfault", "leak", "regression", "hotfix", "broken"],
    ),
    ("docs", ["doc", "readme", "typo", "changelog", "comment"]),
    ("test", ["test", "coverage", "fixture", "tsan"]),
    (
        "chore",
        ["bump", "dependency", "ci", "lint", "format", "release", "merge", "revert"],
    ),
    ("feature", ["add", "implement", "new feature", "introduce", "support"]),
]

# Commit categories whose diffs feed similar-bug detection.
_BUG_TYPES = frozenset({"bugfix", "safety", "panic", "concurrency"})

_GIT_TIMEOUT = 30
_SCRIPT_START: float = 0.0
_SCRIPT_TIMEOUT = 300
_MAX_DIFF_LINES = 150


def classify_commit(message: str) -> str:
    """Classify a commit message into a Rust/PyO3-tuned category."""
    msg_lower = message.lower()
    for category, keywords in CLASSIFICATION_RULES:
        for keyword in keywords:
            if keyword in msg_lower:
                return category
    return "unknown"


def _run_git(args: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT):
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=timeout,
    )


def _run_git_streaming(args: list[str], cwd: Path):
    return subprocess.Popen(
        ["git", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd),
    )


def _is_git_repo(path: Path) -> bool:
    try:
        result = _run_git(["rev-parse", "--is-inside-work-tree"], path, timeout=5)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _is_shallow_repo(path: Path) -> bool:
    """True if the repository is a shallow clone.

    A shallow clone limits ``git log`` to the cloned depth -- ``--since`` /
    ``--last`` windows beyond that depth return truncated history, and
    `first_commit_in_range` dates reflect the shallow boundary rather than the
    actual first appearance. v0.1 emitted misleading "1 commit" file ages and
    empty `function_churn` against shallow checkouts (the polars review tripped
    on this -- the clone had 55 commits over 7 days; `--unshallow` got it to
    14,767 over 6 years). v0.2 detects and caveats up front.
    """
    try:
        result = _run_git(["rev-parse", "--is-shallow-repository"], path, timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _check_script_timeout() -> bool:
    return (time.monotonic() - _SCRIPT_START) > _SCRIPT_TIMEOUT


def _get_file_line_count(filepath: Path) -> int:
    try:
        return len(filepath.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def parse_git_log(lines, max_commits: int, project_root: Path | None = None):
    """Parse `git log --numstat` output into commits and per-file churn."""
    commits: list[dict] = []
    file_changes: dict[str, dict] = {}
    current_commit: dict | None = None
    commit_count = 0
    date_str = ""

    for raw in lines:
        line = raw.rstrip("\n")
        if line.startswith("COMMIT:"):
            if current_commit is not None:
                commits.append(current_commit)
            commit_count += 1
            if commit_count > max_commits:
                current_commit = None
                break
            parts = line[7:].split("|", 3)
            if len(parts) < 4:
                current_commit = None
                continue
            commit_hash, date_str, author, message = parts
            current_commit = {
                "hash": commit_hash,
                "date": date_str,
                "author": author,
                "message": message,
                "type": classify_commit(message),
                "files": [],
            }
        elif line.strip() and current_commit is not None:
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added_str, removed_str, filepath = parts
            try:
                added = int(added_str) if added_str != "-" else 0
                removed = int(removed_str) if removed_str != "-" else 0
            except ValueError:
                continue
            current_commit["files"].append(filepath)
            fc = file_changes.setdefault(
                filepath,
                {
                    "commits": 0,
                    "lines_added": 0,
                    "lines_removed": 0,
                    "authors": set(),
                    "first_date": date_str,
                    "last_date": date_str,
                },
            )
            fc["commits"] += 1
            fc["lines_added"] += added
            fc["lines_removed"] += removed
            fc["authors"].add(current_commit["author"])
            fc["first_date"] = min(fc["first_date"], date_str)
            fc["last_date"] = max(fc["last_date"], date_str)

    if current_commit is not None and commit_count <= max_commits:
        commits.append(current_commit)

    file_stats = []
    for filepath, fc in file_changes.items():
        line_count = (
            _get_file_line_count(project_root / filepath) if project_root else 0
        )
        churn_rate = (
            round((fc["lines_added"] + fc["lines_removed"]) / line_count, 2)
            if line_count > 0
            else 0.0
        )
        file_stats.append(
            {
                "file": filepath,
                "commits": fc["commits"],
                "lines_added": fc["lines_added"],
                "lines_removed": fc["lines_removed"],
                "churn_rate": churn_rate,
                "authors": len(fc["authors"]),
                "first_commit_in_range": fc["first_date"],
                "last_modified": fc["last_date"],
            }
        )
    file_stats.sort(key=lambda x: x["commits"], reverse=True)
    return commits, file_stats


def _relative_scope(scan_root: Path, project_root: Path) -> str:
    try:
        rel = scan_root.resolve().relative_to(project_root.resolve())
        return str(rel) if str(rel) != "." else "."
    except ValueError:
        return "."


def _rust_function_boundaries(filepath: Path) -> list[dict]:
    """Tree-sitter-rust function boundaries for one `.rs` file."""
    try:
        source_bytes = filepath.read_bytes()
    except OSError:
        return []
    tree = parse_bytes(source_bytes)
    return [
        {"name": f["name"], "line_start": f["start_line"], "line_end": f["end_line"]}
        for f in extract_fn_items(tree, source_bytes)
    ]


def compute_function_churn(
    commits, scan_root: Path, project_root: Path, max_files: int = 0, workers: int = 8
):
    """Map diff hunks to Rust function boundaries, parallelised over commits."""
    exclude = {".git", "target", ".tox", ".venv", "venv", "__pycache__", "dist"}
    if scan_root.is_file():
        all_files = [scan_root] if scan_root.suffix == ".rs" else []
    else:
        all_files = sorted(p for p in scan_root.rglob("*.rs") if p.is_file())

    filtered: list[Path] = []
    for f in all_files:
        try:
            parts = set(f.relative_to(project_root).parts)
        except ValueError:
            continue
        if parts & exclude:
            continue
        filtered.append(f)
    if max_files > 0:
        filtered = filtered[:max_files]

    file_functions: dict[str, list[dict]] = {}
    for f in filtered:
        try:
            rel_path = str(f.relative_to(project_root))
        except ValueError:
            rel_path = str(f)
        boundaries = _rust_function_boundaries(f)
        if boundaries:
            file_functions[rel_path] = boundaries

    work_items = [
        (commit["hash"], file_path)
        for commit in commits
        for file_path in commit["files"]
        if file_path in file_functions
    ]
    if not work_items:
        return []

    def _fetch_hunk(item: tuple[str, str]) -> tuple[str, str, set[int]]:
        commit_hash, file_path = item
        try:
            diff_result = _run_git(
                ["show", "--format=", "-U0", commit_hash, "--", file_path],
                project_root,
            )
        except subprocess.TimeoutExpired:
            return commit_hash, file_path, set()
        if diff_result.returncode != 0:
            return commit_hash, file_path, set()
        changed: set[int] = set()
        for diff_line in diff_result.stdout.splitlines():
            hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff_line)
            if hunk:
                start = int(hunk.group(1))
                count = int(hunk.group(2)) if hunk.group(2) else 1
                changed.update(range(start, start + count))
        return commit_hash, file_path, changed

    func_commits: dict[tuple[str, str], set[str]] = defaultdict(set)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for commit_hash, file_path, changed in pool.map(_fetch_hunk, work_items):
            if _check_script_timeout():
                break
            if not changed:
                continue
            for func in file_functions[file_path]:
                func_range = range(func["line_start"], func["line_end"] + 1)
                if changed & set(func_range):
                    func_commits[(file_path, func["name"])].add(commit_hash)

    results = []
    for (file_path, func_name), hashes in func_commits.items():
        info = next(
            (f for f in file_functions[file_path] if f["name"] == func_name), None
        )
        results.append(
            {
                "function": func_name,
                "file": file_path,
                "line_start": info["line_start"] if info else 0,
                "line_end": info["line_end"] if info else 0,
                "commits": len(hashes),
            }
        )
    results.sort(key=lambda x: x["commits"], reverse=True)
    return results


def _truncate_diff(diff_text: str, max_lines: int) -> str:
    lines = diff_text.splitlines()
    if len(lines) <= max_lines:
        return diff_text
    return "\n".join(lines[:max_lines]) + "\n[diff truncated]"


def get_commit_details(
    commits,
    commit_types: frozenset[str],
    project_root: Path,
    scan_root: Path,
    workers: int = 8,
) -> list[dict]:
    """Fetch details (with diffs) for commits whose type is in ``commit_types``."""
    typed = [c for c in commits if c["type"] in commit_types]
    if not typed:
        return []
    rel_scope = _relative_scope(scan_root, project_root)

    def _fetch(commit_hash: str) -> tuple[str, str]:
        diff_args = ["show", "--format=", "--patch", commit_hash, "--"]
        if rel_scope != ".":
            diff_args.append(rel_scope)
        try:
            dr = _run_git(diff_args, project_root)
            diff_text = dr.stdout if dr.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            diff_text = "[diff unavailable: timeout]"
        return commit_hash, _truncate_diff(diff_text, _MAX_DIFF_LINES)

    diff_map: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for commit_hash, diff_text in pool.map(_fetch, [c["hash"] for c in typed]):
            diff_map[commit_hash] = diff_text

    return [
        {
            "commit": c["hash"],
            "commit_short": c["hash"][:7],
            "type": c["type"],
            "message": c["message"],
            "date": c["date"],
            "author": c["author"],
            "files": c["files"],
            "diff": diff_map.get(c["hash"], ""),
        }
        for c in typed
    ]


def parse_args(argv: list[str]) -> dict:
    """Parse the history-analysis CLI arguments."""
    args: dict = {
        "path": ".",
        "days": 365,
        "since": None,
        "until": None,
        "last": None,
        "max_commits": 2000,
        "max_files": 0,
        "workers": 8,
        "no_function": False,
    }

    def _int(flag: str, value: str) -> int:
        try:
            return int(value)
        except ValueError:
            raise SystemExit(
                json.dumps({"error": f"{flag} requires an integer, got {value!r}"})
            )

    int_flags = {
        "--days": "days",
        "--last": "last",
        "--max-commits": "max_commits",
        "--max-files": "max_files",
        "--workers": "workers",
    }
    str_flags = {"--since": "since", "--until": "until"}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in int_flags and i + 1 < len(argv):
            args[int_flags[arg]] = _int(arg, argv[i + 1])
            i += 2
        elif arg in str_flags and i + 1 < len(argv):
            args[str_flags[arg]] = argv[i + 1]
            i += 2
        elif arg == "--no-function":
            args["no_function"] = True
            i += 1
        elif not arg.startswith("-"):
            args["path"] = arg
            i += 1
        else:
            i += 1
    return args


def analyze(argv: list[str] | None = None) -> dict:
    """Analyze git history for churn and Rust/PyO3 bug-pattern commits."""
    global _SCRIPT_START
    _SCRIPT_START = time.monotonic()
    args = parse_args(sys.argv[1:] if argv is None else argv)

    scan_root = Path(args["path"]).resolve()
    project_root = find_project_root(scan_root)
    if not _is_git_repo(project_root):
        return {"error": "Not a git repository", "project_root": str(project_root)}

    is_shallow = _is_shallow_repo(project_root)

    now = datetime.now(timezone.utc)
    since = args["since"] or (now - timedelta(days=args["days"])).isoformat()
    until = args["until"] or now.isoformat()

    git_args = ["log", "--numstat", "--format=COMMIT:%H|%aI|%an|%s"]
    if args["last"] is not None:
        git_args.append(f"-{args['last']}")
    else:
        git_args.extend([f"--since={since}", f"--until={until}"])
    git_args.append("--")
    rel_scope = _relative_scope(scan_root, project_root)
    if rel_scope != ".":
        git_args.append(rel_scope)

    proc = _run_git_streaming(git_args, project_root)
    try:
        commits, file_churn = parse_git_log(
            proc.stdout, args["max_commits"], project_root
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    commits_by_type: dict[str, int] = defaultdict(int)
    authors: set[str] = set()
    for c in commits:
        commits_by_type[c["type"]] += 1
        authors.add(c["author"])

    workers = args["workers"]
    function_churn: list[dict] = []
    function_churn_note = None
    if args["no_function"] or _check_script_timeout():
        function_churn_note = "Function-level churn skipped"
    else:
        function_churn = compute_function_churn(
            commits,
            scan_root,
            project_root,
            max_files=args["max_files"],
            workers=workers,
        )

    result: dict = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "time_range": {"start": since, "end": until, "days": args["days"]},
        "is_shallow_clone": is_shallow,
        "summary": {
            "total_commits": len(commits),
            "commits_by_type": dict(commits_by_type),
            "files_changed": len(file_churn),
            "functions_changed": len(function_churn),
            "authors": len(authors),
        },
        "file_churn": file_churn,
        "function_churn": function_churn,
        "recent_fixes": get_commit_details(
            commits, _BUG_TYPES, project_root, scan_root, workers=workers
        ),
        "recent_migrations": get_commit_details(
            commits,
            frozenset({"migration"}),
            project_root,
            scan_root,
            workers=workers,
        ),
    }
    if function_churn_note:
        result["function_churn_note"] = function_churn_note
    if is_shallow:
        result.setdefault("notes", []).append(
            "Repository is a SHALLOW clone -- history is truncated at the "
            "clone depth. `first_commit_in_range` dates reflect the shallow "
            "boundary, NOT the actual first appearance. `function_churn` and "
            "similar-bug detection are unreliable. Run `git fetch --unshallow` "
            "in the repository before re-running this analysis. (v0.2: this "
            "warning replaces the v0.1 silent emission of misleading '1 "
            "commit' file ages.)"
        )
    return result


def main() -> None:
    try:
        result = analyze()
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        if "error" in result:
            sys.exit(1)
    except Exception as e:  # noqa: BLE001 -- top-level guard, emit JSON error
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
