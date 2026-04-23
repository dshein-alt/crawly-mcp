from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PACKAGE_NAME = "crawly-mcp"


def get_package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return _read_pyproject_version() or "unknown"


def _read_pyproject_version() -> str | None:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject_path.is_file():
        return None

    with pyproject_path.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    project = pyproject.get("project")
    if not isinstance(project, dict):
        return None

    version_value = project.get("version")
    return version_value if isinstance(version_value, str) else None
