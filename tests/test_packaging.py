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
    assert version == _manifest_field("version")
    assert version == receipt_mod.PLUGIN_VERSION
    assert version == prime_rlm_pkg.PLUGIN_VERSION

    entries = config["project"]["entry-points"]["hermes_agent.plugins"]
    assert entries == {"prime-rlm": "hermes_prime_rlm"}
    assert config["build-system"]["requires"] == ["setuptools>=77"]
    assert config["tool"]["setuptools"]["package-data"]["hermes_prime_rlm"] == [
        "plugin.yaml",
        "runtime_lock.json",
    ]
    assert config["tool"]["setuptools"]["packages"] == ["hermes_prime_rlm"]
    assert config["tool"]["setuptools"]["package-dir"] == {"hermes_prime_rlm": "."}


def test_ci_release_and_artifact_smoke_pin_rpc_v02_on_all_three_operating_systems():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    smoke = (ROOT / "scripts" / "artifact_smoke.py").read_text(encoding="utf-8")

    expected_matrix = "os: [windows-latest, macos-latest, ubuntu-latest]"
    assert expected_matrix in ci
    assert expected_matrix in release
    for workflow in (ci, release):
        assert 'p.manifest.version=="0.2.0"' in workflow
        assert '"prime_agent" in m._plugin_tool_names' in workflow
        assert '"prime" in m._cli_commands' in workflow
        assert '"prime-setup" in m._plugin_commands' in workflow
        assert 'p.commands_registered==["prime-setup"]' in workflow
        assert "prime_rlm_run" not in workflow
    assert 'definition["name"] == "prime_agent"' in smoke
    assert '"action": "run"' in smoke
    assert "def register_cli_command" in smoke
    assert "def register_command" in smoke
    assert 'entry["name"] == "prime"' in smoke
    assert 'entry["name"] == "prime-setup"' in smoke
    assert "commit_setup_plan" in smoke
    assert 'setup_result["state"] == "READY"' in smoke


def test_live_demo_does_not_report_ok_for_nonverified_receipt():
    demo = (ROOT / "scripts" / "demo.py").read_text(encoding="utf-8")

    assert 'result.get("status") != "VERIFIED"' in demo
    assert 'raise SystemExit(1)' in demo
    assert '[sys.executable, "-B", "-c"' in demo


def test_recording_launcher_is_silent_by_default_and_console_is_opt_in():
    launcher = (ROOT / "scripts" / "launch_record.py").read_text(encoding="utf-8")

    assert 'add_argument("--visible-console", action="store_true")' in launcher
    assert "subprocess.CREATE_NO_WINDOW" in launcher
    assert "subprocess.CREATE_NEW_CONSOLE if args.visible_console" in launcher
    assert '"-NoExit" if args.visible_console else "-NonInteractive"' in launcher
