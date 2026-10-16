#!/usr/bin/env python3
"""Detect PyO3 Rust extensions and resolve their build profile.

Detection cascade (first match wins):
  1. Cargo.toml with a `pyo3` dependency and a `cdylib` crate-type
  2. pyproject.toml with maturin as the build backend
  3. pyproject.toml / setup.py with setuptools-rust
  4. Cargo.toml with `pyo3` but no `cdylib` -- a Rust binary embedding
     Python; detected and reported, but out of scope for analysis
  5. Fallback: any `.rs` file that uses `pyo3`

Outputs a JSON description of the crate to stdout. Uses TOML parsing and
source-text heuristics only (no Tree-sitter).

Usage:
    python discover_rust_ext.py [path]
"""

import json
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_common import discover_rust_files, find_project_root  # noqa: E402


# `#[pymodule] ... fn NAME` — tolerates attribute args and stacked attributes.
_PYMODULE_RE = re.compile(
    r"#\[\s*(?:pyo3\s*::\s*)?pymodule\b[^\]]*\]"
    r"\s*(?:#\[[^\]]*\]\s*)*"
    r"(?:pub\s*(?:\([^)]*\)\s*)?)?fn\s+(\w+)"
)
_USES_PYO3_RE = re.compile(
    r"\buse\s+pyo3\b|#\[\s*(?:pyo3\s*::\s*)?(?:pymodule|pyfunction|pyclass)\b"
)


def _load_toml(path: Path) -> dict:
    """Parse a TOML file, returning {} on any error."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _safe_read(path: Path) -> str:
    """Read a file as text, returning '' on error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _rel(path: Path, root: Path) -> str:
    """Path relative to root if possible, else the absolute path."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _parse_cargo_toml(root: Path) -> dict | None:
    """Extract crate metadata from Cargo.toml; None if there is no Cargo.toml."""
    data = _load_toml(root / "Cargo.toml")
    if not data:
        return None
    info: dict = {
        "crate_name": "",
        "lib_name": None,
        "crate_types": [],
        "pyo3_version": None,
        "pyo3_features": [],
        "has_pyo3": False,
        "is_workspace": "workspace" in data and "package" not in data,
    }
    pkg = data.get("package", {})
    if isinstance(pkg, dict):
        info["crate_name"] = pkg.get("name", "") or ""
    lib = data.get("lib", {})
    if isinstance(lib, dict):
        info["lib_name"] = lib.get("name")
        ctypes = lib.get("crate-type") or lib.get("crate_type") or []
        info["crate_types"] = ctypes if isinstance(ctypes, list) else [ctypes]
    for section in ("dependencies", "build-dependencies"):
        deps = data.get(section, {})
        dep = deps.get("pyo3") if isinstance(deps, dict) else None
        if dep is None:
            continue
        info["has_pyo3"] = True
        if isinstance(dep, str):
            info["pyo3_version"] = dep
        elif isinstance(dep, dict):
            info["pyo3_version"] = dep.get("version")
            feats = dep.get("features", [])
            info["pyo3_features"] = feats if isinstance(feats, list) else []
        break
    return info


def _resolve_workspace_root(root: Path) -> Path | None:
    """Walk up from ``root`` to the nearest ancestor whose ``Cargo.toml``
    declares a ``[workspace]`` table. Returns ``None`` if not in a workspace.

    Used to find the `Cargo.lock` that resolves a workspace dependency's
    version, and to enumerate workspace siblings (for shim-cdylib detection).
    Cryptography's `src/rust/` is a workspace member; its `Cargo.lock` lives
    one level up, which the v0.1 lookup at `root / "Cargo.lock"` missed.
    """
    cur = root.parent
    while cur != cur.parent:
        toml_path = cur / "Cargo.toml"
        if toml_path.is_file():
            data = _load_toml(toml_path)
            if isinstance(data, dict) and "workspace" in data:
                return cur
        cur = cur.parent
    return None


def _pyo3_from_lock(root: Path) -> tuple[str | None, str | None]:
    """Resolve the exact pyo3 version from a ``Cargo.lock``.

    Looks at ``root`` first, then climbs to a workspace root (if any). The
    workspace fallback fixes the v0.1 gap where cryptography's `src/rust/`
    scan returned `pyo3_version: null` even though `Cargo.lock` was one
    level up. Returns ``(version, lock_dir)`` so the discovery envelope can
    record where the lock came from; both are ``None`` on miss.
    """
    candidates: list[Path] = [root]
    ws_root = _resolve_workspace_root(root)
    if ws_root is not None and ws_root != root:
        candidates.append(ws_root)

    for candidate in candidates:
        data = _load_toml(candidate / "Cargo.lock")
        packages = data.get("package", []) if isinstance(data, dict) else []
        if isinstance(packages, list):
            for pkg in packages:
                if isinstance(pkg, dict) and pkg.get("name") == "pyo3":
                    version = pkg.get("version")
                    if version:
                        return version, str(candidate)
    return None, None


def _find_shim_cdylib_sibling(root: Path, crate_name: str) -> Path | None:
    """Return a workspace sibling that emits a cdylib re-exporting this crate.

    Polars's layout puts the cdylib emission in tiny downstream crates
    (`py-polars/runtime/polars-runtime-{32,64,compat}/src/lib.rs`, each two
    lines: `pub use polars_python::c_api::*;`). When we see that pattern,
    this crate IS the PyO3 surface even though it has no cdylib in its own
    Cargo.toml -- so we override the v0.1 ``out_of_scope: true`` decision.

    The match is conservative: require (a) the sibling builds a cdylib and
    (b) the sibling's ``lib.rs`` references this crate's identifier as a
    ``use``/``pub use`` path. Returns the sibling's directory, or ``None``.
    """
    if not crate_name:
        return None
    ws_root = _resolve_workspace_root(root)
    if ws_root is None:
        return None
    ws_data = _load_toml(ws_root / "Cargo.toml")
    workspace = ws_data.get("workspace", {}) if isinstance(ws_data, dict) else {}
    members = workspace.get("members", []) if isinstance(workspace, dict) else []
    if not isinstance(members, list):
        return None
    # The dependency name uses hyphens (`polars-python`); the source-side
    # `use` path uses underscores (`polars_python`). Tolerate both.
    crate_ident = crate_name.replace("-", "_")
    for member_pattern in members:
        if not isinstance(member_pattern, str):
            continue
        for member_dir in sorted(ws_root.glob(member_pattern)):
            if not member_dir.is_dir() or member_dir.resolve() == root.resolve():
                continue
            sibling_cargo = _parse_cargo_toml(member_dir)
            if sibling_cargo is None or "cdylib" not in sibling_cargo["crate_types"]:
                continue
            lib_rs = member_dir / "src" / "lib.rs"
            if not lib_rs.is_file():
                continue
            text = _safe_read(lib_rs)
            if (
                f"pub use {crate_ident}" in text
                or f"use {crate_ident}::" in text
                or f" {crate_ident}::" in text
            ):
                return member_dir
    return None


def _python_floor(features: list[str], abi: str = "abi3") -> str | None:
    """Return the lowest declared version floor for one stable ABI flavor."""
    floors: list[int] = []
    for feat in features:
        m = re.fullmatch(rf"{abi}-py3(\d+)", str(feat))
        if m:
            floors.append(int(m.group(1)))
    return f"3.{min(floors)}" if floors else None


def _find_pymodule(rs_files: list[Path]) -> tuple[str | None, Path | None]:
    """Find the `#[pymodule]` entry-point function name and its file."""
    for path in rs_files:
        m = _PYMODULE_RE.search(_safe_read(path))
        if m:
            return m.group(1), path
    return None, None


def _detect_build_backend(root: Path) -> str:
    """Identify the build backend (maturin / setuptools-rust / cargo)."""
    pyproject = _load_toml(root / "pyproject.toml")
    build_system = pyproject.get("build-system", {})
    requires = (
        build_system.get("requires", []) if isinstance(build_system, dict) else []
    )
    req_text = " ".join(str(r) for r in requires).lower()
    if "maturin" in req_text:
        return "maturin"
    if "setuptools-rust" in req_text or "setuptools_rust" in req_text:
        return "setuptools-rust (pyproject)"
    if (root / "setup.py").is_file():
        if "setuptools_rust" in _safe_read(root / "setup.py"):
            return "setuptools-rust (setup.py)"
    if (root / "Cargo.toml").is_file():
        return "cargo"
    return "unknown"


def _free_threading(root: Path, rs_files: list[Path]) -> tuple[bool, list[str]]:
    """Infer free-threading intent from soft signals.

    Look for an explicit `gil_used` module declaration and CI configured
    against a free-threaded interpreter. Discovery also records `abi3t`
    features as intent, not proof of thread safety or the effective build ABI.
    """
    signals: list[str] = []
    for path in rs_files:
        text = _safe_read(path)
        if "gil_used" in text or "Py_MOD_GIL" in text:
            signals.append(f"gil_used declaration ({path.name})")
            break
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        for wf in sorted(workflows.rglob("*.y*ml")):
            if re.search(r"3\.1[3-9]t\b|free.?threaded", _safe_read(wf), re.IGNORECASE):
                signals.append(f"free-threaded interpreter in CI ({wf.name})")
                break
    return bool(signals), signals


def discover(target: str) -> dict:
    """Discover a PyO3 Rust extension at the given path."""
    target_path = Path(target).resolve()
    root = target_path if target_path.is_dir() else find_project_root(target_path)

    rs_files = discover_rust_files(root)
    cargo = _parse_cargo_toml(root)
    backend = _detect_build_backend(root)
    module_name, entry_file = _find_pymodule(rs_files)

    has_cdylib = bool(cargo and "cdylib" in cargo["crate_types"])
    has_pyo3_dep = bool(cargo and cargo["has_pyo3"])
    uses_pyo3_in_src = any(_USES_PYO3_RE.search(_safe_read(p)) for p in rs_files)

    notes: list[str] = []
    is_pyo3 = False
    out_of_scope = False
    detection_method = "none"

    if has_pyo3_dep and has_cdylib:
        is_pyo3, detection_method = True, "cargo_toml_cdylib"
    elif backend == "maturin":
        is_pyo3, detection_method = True, "pyproject_maturin"
    elif backend.startswith("setuptools-rust"):
        is_pyo3, detection_method = True, "setuptools_rust"
    elif has_pyo3_dep and not has_cdylib:
        # v0.2: a crate with `pyo3` and `#[pymodule]` IS the PyO3 surface
        # regardless of where the cdylib is emitted. The polars layout has
        # the cdylib in downstream shim crates that re-export this crate's
        # `c_api::*`. Distinguish that case from the rare "Rust binary
        # embedding Python" case (which has no `#[pymodule]`).
        if module_name:
            shim_sibling = _find_shim_cdylib_sibling(
                root, cargo["crate_name"] if cargo else ""
            )
            if shim_sibling is not None:
                is_pyo3, detection_method = True, "workspace_shim_cdylib"
                notes.append(
                    f"cdylib is emitted by workspace sibling "
                    f"`{shim_sibling.name}/` (re-exports this crate's "
                    f"`#[pymodule]` entry points). This crate is the PyO3 "
                    f"surface; review accordingly."
                )
            else:
                is_pyo3, detection_method = True, "pymodule_no_cdylib"
                notes.append(
                    "This crate defines `#[pymodule]` entry points but has "
                    "no `cdylib` crate-type and no obvious workspace shim. "
                    "The cdylib may be emitted by a downstream consumer."
                )
        else:
            is_pyo3, out_of_scope, detection_method = True, True, "cargo_toml_no_cdylib"
            notes.append(
                "pyo3 dependency without a cdylib crate-type and no "
                "`#[pymodule]` in source: this looks like a Rust binary "
                "embedding Python, which this toolkit does not review."
            )
    elif uses_pyo3_in_src:
        is_pyo3, detection_method = True, "rs_fallback"
        notes.append("Detected via `use pyo3` in source; no build-config signal.")

    if cargo and cargo["is_workspace"]:
        notes.append(
            "Cargo.toml is a workspace root; member crates may need separate analysis."
        )

    lock_version, lock_dir = _pyo3_from_lock(root)
    toml_version = cargo["pyo3_version"] if cargo else None
    pyo3_version = lock_version or toml_version
    version_source: str | None
    if lock_version:
        # Identify whether the lock came from the crate root or a workspace
        # ancestor; downstream agents care because the toml-declared version
        # range may differ from the workspace-resolved version.
        if lock_dir == str(root):
            version_source = "Cargo.lock"
        else:
            version_source = "workspace Cargo.lock"
            notes.append(
                f"pyo3 version resolved from workspace `Cargo.lock` at "
                f"`{lock_dir}` (this crate's directory has no `Cargo.lock`)."
            )
    else:
        version_source = "Cargo.toml" if toml_version else None
    features = cargo["pyo3_features"] if cargo else []
    ft_target, ft_signals = _free_threading(root, rs_files)
    if any(f == "abi3t" or f.startswith("abi3t-py3") for f in features):
        ft_target = True
        ft_signals.append("abi3t stable ABI requested in pyo3 features")
    abi3_floor = _python_floor(features)
    abi3t_floor = _python_floor(features, "abi3t")

    if has_pyo3_dep and pyo3_version is None:
        notes.append(
            "pyo3 dependency present but its version is unresolved -- likely a "
            "path or workspace dependency, or no Cargo.lock alongside the crate. "
            "Downstream agents should treat the PyO3 version as unknown."
        )

    return {
        "project_root": str(root),
        "scan_root": str(target_path),
        "is_pyo3_extension": is_pyo3,
        "out_of_scope": out_of_scope,
        "detection_method": detection_method,
        "crate_name": cargo["crate_name"] if cargo else "",
        "module_name": module_name,
        "entry_point_file": _rel(entry_file, root) if entry_file else None,
        "pyo3_version": pyo3_version,
        "pyo3_version_source": version_source,
        "pyo3_features": features,
        "python_floor": abi3_floor or abi3t_floor,
        "abi3t_python_floor": abi3t_floor,
        "build_backend": backend,
        "crate_types": cargo["crate_types"] if cargo else [],
        "free_threading_target": ft_target,
        "free_threading_signals": ft_signals,
        "source_files": [_rel(p, root) for p in rs_files],
        "total_rs_files": len(rs_files),
        "has_python_source": (root / "python").is_dir(),
        "notes": notes,
    }


def main() -> None:
    try:
        target = sys.argv[1] if len(sys.argv) > 1 else "."
        result = discover(target)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:  # noqa: BLE001 -- top-level guard, emit JSON error
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
