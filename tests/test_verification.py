"""Host verification tests (spec §24 Verification)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import verification


def _check(name, argv, timeout=60):
    return {"name": name, "argv": argv, "timeout_seconds": timeout}


@pytest.fixture()
def candidate(tmp_path):
    c = tmp_path / "cand"
    c.mkdir()
    return str(c)


@pytest.fixture()
def checks_dir(tmp_path):
    d = tmp_path / "checks"
    d.mkdir()
    return str(d)


def test_passing_check_records_pass(candidate, checks_dir):
    result = verification.run_check(
        _check("ok", [sys.executable, "-c", "import sys; sys.exit(0)"]), candidate, checks_dir
    )
    assert result.status.value == "passed"
    assert result.exit_code == 0
    assert result.duration_ms >= 0


def test_failing_check_yields_failed_verification(candidate, checks_dir):
    result = verification.run_check(
        _check("bad", [sys.executable, "-c", "import sys; sys.exit(2)"]), candidate, checks_dir
    )
    assert result.status.value == "failed"
    assert result.exit_code == 2


def test_timed_out_check_yields_failed_verification(candidate, checks_dir):
    result = verification.run_check(
        _check("slow", [sys.executable, "-c", "import time; time.sleep(120)"], timeout=2),
        candidate,
        checks_dir,
    )
    assert result.status.value == "timed_out"


def test_unlaunchable_check_yields_failed_verification(candidate, checks_dir):
    result = verification.run_check(
        _check("ghost", ["no-such-binary-xyz-123", "--version"]), candidate, checks_dir
    )
    assert result.status.value == "launch_error"


def test_checks_stop_after_first_failure(candidate, checks_dir):
    ran = []
    original = verification.run_check

    def spy(check, cwd, cd):
        ran.append(check["name"])
        return original(check, cwd, cd)

    verification.run_check = spy
    try:
        results = verification.run_all_checks(
            [
                _check("first-fails", [sys.executable, "-c", "exit 1"]),
                _check("never-runs", [sys.executable, "-c", "exit 0"]),
            ],
            candidate,
            checks_dir,
        )
    finally:
        verification.run_check = original
    assert ran == ["first-fails"]
    assert len(results) == 1


def test_checks_run_in_candidate(candidate, checks_dir):
    probe = "import os, json; print(json.dumps({'cwd': os.getcwd()}))"
    verification.run_check(_check("where", [sys.executable, "-c", probe]), candidate, checks_dir)
    out_file = Path(checks_dir) / "where" / "stdout.log"
    body = out_file.read_text(encoding="utf-8")
    # JSON escapes backslashes; compare case-insensitively after unescaping.
    unescaped = body.encode().decode("unicode_escape").lower()
    assert str(Path(candidate).resolve()).lower() in unescaped


def test_empty_checks_yield_completed_unverified_status_mapping():
    from conftest import tools

    assert tools._map_status(had_checks=False, all_passed=True).value == (
        "COMPLETED_UNVERIFIED"
    )


def test_empty_checks_can_never_yield_verified():
    from conftest import tools

    # Direct unit assertion of the mapping rule.
    status = tools._map_status(had_checks=False, all_passed=True)
    assert status.value == "COMPLETED_UNVERIFIED"
    status2 = tools._map_status(had_checks=False, all_passed=False)
    assert status2.value == "COMPLETED_UNVERIFIED"


def test_prime_textual_claim_cannot_affect_status():
    """Mapping depends only on host check outcomes, never on final_text."""
    from conftest import tools

    assert tools._map_status(True, False).value == "FAILED_VERIFICATION"
    assert tools._map_status(True, True).value == "VERIFIED"


SHELL_METACHAR_ARGS = [
    '"; & echo pwned > sentinel.txt',
    "$(touch sentinel2)",
    "a|b",
    "a>b",
    "a<b",
]


def test_metacharacters_remain_literal_argv(tmp_path, candidate, checks_dir):
    sentinel = tmp_path / "sentinel.txt"
    for i, hostile in enumerate(SHELL_METACHAR_ARGS):
        probe_name = f"probe{i}"
        result = verification.run_check(
            _check(
                probe_name,
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write(repr(sys.argv[1:]))",
                    hostile,
                ],
            ),
            candidate,
            checks_dir,
        )
        assert result.status.value == "passed", f"arg {hostile!r} broke the run"
        body = (Path(checks_dir) / probe_name / "stdout.log").read_text(encoding="utf-8")
        assert hostile in body
    assert not sentinel.exists()


def test_windows_cmd_shims_exercised(tmp_path, candidate, checks_dir):
    if sys.platform != "win32":
        pytest.skip("Windows shim route")
    shim = tmp_path / "echoer.cmd"
    shim.write_text('@echo off\necho shim-ran %1\n', encoding="utf-8")
    result = verification.run_check(
        _check("shim", [str(shim), "hello"]), candidate, checks_dir
    )
    assert result.status.value == "passed"


def test_macos_executable_checks_exercised(tmp_path, candidate, checks_dir):
    if sys.platform == "win32":
        pytest.skip("POSIX executable route")
    script = tmp_path / "checker.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    result = verification.run_check(_check("posix", [str(script)]), candidate, checks_dir)
    assert result.status.value == "passed"
