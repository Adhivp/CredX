"""Keep project metadata and the default interpreter aligned with deployment."""

import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet


def test_python_314_is_supported_and_selected():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    supported = SpecifierSet(project["project"]["requires-python"])
    assert "3.14.0" in supported, "Python 3.14 must be supported by project metadata"
    assert (root / ".python-version").read_text().strip() == "3.14"
