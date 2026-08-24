"""Host-side verification: run the caller's exact checks inside the candidate.

Checks run ONLY after Prime exited 0 with a valid, terminal stream. Each check
executes with argv preserved literally, ``shell=False``, the candidate as cwd,
separate stdout/stderr capture, and its own timeout. Execution stops after the
first failure, timeout, or unlaunchable check.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from .models import CheckResult, CheckStatus
from .platform_runtime import (
    SpawnSpec,
    build_shim_argv,
    guard_shim_argument,
    is_cmd_shim,
    resolve_executable,
    spawn_and_wait,
)


def _sha256_file(path: str) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def run_check(
    check: dict,
    candidate_path: str,
    checks_dir: str,
) -> CheckResult:
    """Execute one check; never raises. Maps every outcome to a CheckStatus."""
    name = check["name"]
    out_dir = Path(checks_dir) / name
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = str(out_dir / "stdout.log")
    stderr_path = str(out_dir / "stderr.log")

    argv = list(check["argv"])
    resolved_first = resolve_executable(argv[0])
    if resolved_first is None:
        return CheckResult(
            name=name,
            status=CheckStatus.LAUNCH_ERROR,
            exit_code=None,
            duration_ms=0,
        )
    effective = [resolved_first, *argv[1:]]
    if is_cmd_shim(resolved_first):
        try:
            for token in argv[1:]:
                guard_shim_argument(token)
        except ValueError:
            return CheckResult(
                name=name,
                status=CheckStatus.LAUNCH_ERROR,
                exit_code=None,
                duration_ms=0,
            )
        effective = build_shim_argv(resolved_first, argv[1:])

    spec = SpawnSpec(
        argv=effective,
        cwd=candidate_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        env={**os.environ, "PI_SKIP_VERSION_CHECK": "1"},
    )
    started = time.monotonic()
    exit_code, timed_out = spawn_and_wait(spec, float(check["timeout_seconds"]))
    duration_ms = int((time.monotonic() - started) * 1000)

    if timed_out:
        return CheckResult(
            name=name,
            status=CheckStatus.TIMED_OUT,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_sha256=_sha256_file(stdout_path),
            stderr_sha256=_sha256_file(stderr_path),
        )
    if exit_code is None:
        return CheckResult(
            name=name,
            status=CheckStatus.LAUNCH_ERROR,
            exit_code=None,
            duration_ms=duration_ms,
        )
    status = CheckStatus.PASSED if exit_code == 0 else CheckStatus.FAILED
    return CheckResult(
        name=name,
        status=status,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout_sha256=_sha256_file(stdout_path),
        stderr_sha256=_sha256_file(stderr_path),
    )


def run_all_checks(
    checks: list[dict],
    candidate_path: str,
    checks_dir: str,
) -> list[CheckResult]:
    """Run checks in order, stopping after the first non-pass."""
    results: list[CheckResult] = []
    for check in checks:
        result = run_check(check, candidate_path, checks_dir)
        results.append(result)
        if result.status is not CheckStatus.PASSED:
            break
    return results


def checks_all_passed(results: list[CheckResult]) -> bool:
    return bool(results) and all(r.status is CheckStatus.PASSED for r in results)
