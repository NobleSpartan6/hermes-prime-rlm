"""Tests for scripts/generate_fixture.py -- the payments-api demo fixture.

These encode the manual verification we performed before committing the
fixture, per the project standard: manual observation -> automated test ->
revert check (buggy code must FAIL, reference code must PASS).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "generate_fixture.py"

EXPECTED_LINES = 84_600
EXPECTED_RATE = "0.174672"
# Reference implementation: stream lines, count only INFO/WARN/ERROR entries.
REFERENCE_APP = '''\
ENTRY_LEVELS = frozenset({"INFO", "WARN", "ERROR"})


def error_rate(path):
    total = errs = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            fields = line.split()
            if len(fields) >= 2 and fields[1] in ENTRY_LEVELS:
                total += 1
                if fields[1] == "ERROR":
                    errs += 1
    return errs / total
'''


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=120,
        check=False,
    )


def test_generator_produces_failing_oracle_then_reference_passes(tmp_path):
    """The full loop: generate -> oracle fails on buggy code -> passes on
    reference code. Reverting the 'fix' must flip PASS back to FAIL."""
    gen = _run([sys.executable, str(GENERATOR), "--dir", str(tmp_path)], cwd=tmp_path)
    assert gen.returncode == 0, gen.stderr

    # Fixture shape matches the documented numbers.
    log_lines = (tmp_path / "events.log").read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == EXPECTED_LINES
    assert (tmp_path / "expected_rate.txt").read_text(encoding="utf-8").strip() == EXPECTED_RATE

    # Committed app.py is the buggy one: oracle must FAIL (the demo's premise).
    buggy = _run([sys.executable, "run_tests.py"], cwd=tmp_path)
    assert buggy.returncode != 0
    assert "FAIL:" in buggy.stdout + buggy.stderr

    # Apply the reference fix; oracle must PASS with the exact rate.
    (tmp_path / "app.py").write_text(REFERENCE_APP, encoding="utf-8")
    fixed = _run([sys.executable, "run_tests.py"], cwd=tmp_path)
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert "PASS" in fixed.stdout
    assert EXPECTED_RATE in fixed.stdout


def test_generator_is_deterministic(tmp_path):
    """Same seed => byte-identical events.log across invocations."""
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    for out in (out_a, out_b):
        r = _run([sys.executable, str(GENERATOR), "--dir", str(out)], cwd=tmp_path)
        assert r.returncode == 0, r.stderr
    assert (out_a / "events.log").read_bytes() == (out_b / "events.log").read_bytes()
