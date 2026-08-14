"""Resolve the package version. Prefers the installed distribution's metadata,
falling back to reading `pyproject.toml` for a bare source checkout."""

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_DIST_NAME = "amzdl"


def _from_pyproject() -> str:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return "0+unknown"
    match = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', text)
    return match.group(1) if match else "0+unknown"


try:
    VERSION = version(_DIST_NAME)
except PackageNotFoundError:
    VERSION = _from_pyproject()
