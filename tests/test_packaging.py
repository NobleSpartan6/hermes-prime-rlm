"""Release packaging contract: version alignment and pip plugin discovery."""

from __future__ import annotations

import tomllib
from pathlib import Path

from conftest import prime_rlm_pkg, receipt_mod

ROOT = Path(__file__).resolve().parents[1]


def _manifest_field(name: str) -> str:
    for line in (ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"missing plugin.yaml field: {name}")


def test_release_versions_and_pip_entry_point_are_aligned():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = config["project"]["version"]
    assert version == "0.1.1"
    assert version == _manifest_field("version")
    assert version == receipt_mod.PLUGIN_VERSION
    assert version == prime_rlm_pkg.PLUGIN_VERSION

    entries = config["project"]["entry-points"]["hermes_agent.plugins"]
    assert entries == {"prime-rlm": "hermes_prime_rlm"}
    assert config["build-system"]["requires"] == ["setuptools>=77"]
    assert config["tool"]["setuptools"]["packages"] == ["hermes_prime_rlm"]
    assert config["tool"]["setuptools"]["package-dir"] == {"hermes_prime_rlm": "."}
