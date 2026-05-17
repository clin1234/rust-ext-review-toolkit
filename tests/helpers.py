"""Test helpers for rust-ext-review-toolkit tests."""

import importlib.util
import shutil
import tempfile
from pathlib import Path
from types import ModuleType

_SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "rust-ext-review-toolkit"
    / "scripts"
)


def import_script(name: str) -> ModuleType:
    """Import a script from the plugin's scripts/ directory as a module.

    The scripts directory is not a Python package; scripts add it to
    ``sys.path`` themselves so they can import their siblings.
    """
    path = _SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load script: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CARGO_TEMPLATE = """\
[package]
name = "{name}"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]

[dependencies]
pyo3 = {{ version = "{version}", features = [{features}] }}
"""


class TempRustExtension:
    """Create a temporary PyO3 crate on disk for testing.

    Usage:
        with TempRustExtension({"src/lib.rs": code}) as ext:
            result = some_script.analyze(str(ext.root))

    A ``Cargo.toml`` with a pyo3 dependency and a ``cdylib`` crate-type is
    generated automatically unless an explicit ``cargo_toml`` is supplied or
    ``write_cargo_toml=False`` is passed (to exercise discovery fallbacks).

    Options:
        files            mapping of relative path -> file content
        name             crate name for the generated Cargo.toml
        cargo_toml       explicit Cargo.toml content (overrides generation)
        write_cargo_toml set False to write no Cargo.toml at all
        pyproject_toml   optional pyproject.toml content
        setup_py         optional setup.py content
        pyo3_version     pyo3 version for the generated Cargo.toml
        pyo3_features    feature list for the generated pyo3 dependency
    """

    def __init__(
        self,
        files: dict[str, str],
        *,
        name: str = "testcrate",
        cargo_toml: str | None = None,
        write_cargo_toml: bool = True,
        pyproject_toml: str | None = None,
        setup_py: str | None = None,
        pyo3_version: str = "0.28",
        pyo3_features: list[str] | None = None,
    ) -> None:
        self.files = files
        self.name = name
        self.cargo_toml = cargo_toml
        self.write_cargo_toml = write_cargo_toml
        self.pyproject_toml = pyproject_toml
        self.setup_py = setup_py
        self.pyo3_version = pyo3_version
        self.pyo3_features = pyo3_features
        self.root: Path = Path()
        self._tmpdir: str | None = None

    def _default_cargo_toml(self) -> str:
        feats = self.pyo3_features
        if feats is None:
            feats = ["extension-module"]
        features = ", ".join(f'"{f}"' for f in feats)
        return _CARGO_TEMPLATE.format(
            name=self.name, version=self.pyo3_version, features=features
        )

    def __enter__(self) -> "TempRustExtension":
        self._tmpdir = tempfile.mkdtemp(prefix="rustext_test_")
        root = Path(self._tmpdir)
        self.root = root
        for rel_path, content in self.files.items():
            full = root / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
        if self.write_cargo_toml:
            cargo = self.cargo_toml or self._default_cargo_toml()
            (root / "Cargo.toml").write_text(cargo, encoding="utf-8")
        if self.pyproject_toml is not None:
            (root / "pyproject.toml").write_text(self.pyproject_toml, encoding="utf-8")
        if self.setup_py is not None:
            (root / "setup.py").write_text(self.setup_py, encoding="utf-8")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
