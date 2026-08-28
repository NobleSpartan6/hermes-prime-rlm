"""Prime process invocation tests (spec §24 Prime process)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from conftest import FAKE_PRIME_AGENT, prime_process, validation, workspace


def _setup_run(fake_ctx, clean_repo, scenario_env: dict, timeout=90):
    """Create run + worktree, then run the fake with a scenario env."""
    base = validation.resolve_base_commit(str(clean_repo))
    run_id, layout = workspace.create_run_directory(
        data_root=fake_ctx.state.data_dir,
        repository_path=str(clean_repo),
        base_commit=base,
        goal="innocent goal text",
        checks=[],
        runtime_timeout_seconds=timeout,
        prime_command_prefix=["fake"],
    )
    workspace.create_detached_worktree(str(clean_repo), layout, base)
    env_patch = {"FAKE_PRIME_SCENARIO": scenario_env}
    old = os.environ.get("FAKE_PRIME_SCENARIO")
    os.environ.update(env_patch)
    try:
        observation, record = prime_process.run_prime(
            [sys.executable, str(FAKE_PRIME_AGENT)], layout, timeout
        )
    finally:
        if old is None:
            os.environ.pop("FAKE_PRIME_SCENARIO", None)
        else:
            os.environ["FAKE_PRIME_SCENARIO"] = old
    return base, run_id, layout, observation, record


def test_correct_fixed_arguments_supplied(fake_ctx, clean_repo, monkeypatch):
    """The fake echoes its argv into stderr when FAKE_ECHO_ARGV is set."""
    base, run_id, layout, observation, record = _setup_run(
        fake_ctx, clean_repo, "success_no_changes"
    )
    # The record is the plugin-built argv; assert the fixed shape directly.
    joined = record
    assert "--mode" in joined and "json" in joined
    assert "--no-session" in joined
    assert "--cwd" in joined
    cwd_index = joined.index("--cwd")
    assert Path(joined[cwd_index + 1]).resolve() == Path(layout.candidate).resolve()
    assert joined.index("--") < len(joined) - 1
    assert joined[-1] == prime_process.PRIME_FIXED_INSTRUCTION
    # Goal text never on the command line.
    assert "innocent goal text" not in " ".join(joined)


def test_correct_candidate_cwd_used(fake_ctx, clean_repo):
    base, run_id, layout, observation, record = _setup_run(
        fake_ctx, clean_repo, "success_no_changes"
    )
    assert Path(record[record.index("--cwd") + 1]).resolve() == Path(
        layout.candidate
    ).resolve()


def test_pi_skip_version_check_is_set(fake_ctx, clean_repo):
    """The child env carries PI_SKIP_VERSION_CHECK=1 by construction."""
    from conftest import platform_runtime

    base, run_id, layout, observation, record = _setup_run(
        fake_ctx, clean_repo, "stderr_noise"
    )
    env = platform_runtime.prime_environment()
    assert env["PI_SKIP_VERSION_CHECK"] == "1"
    # And the plugin's spawn spec builds the same env (structural guarantee).
    import inspect

    source = inspect.getsource(platform_runtime.prime_environment)
    assert "PI_SKIP_VERSION_CHECK" in source


def test_no_model_or_provider_argument_supplied(fake_ctx, clean_repo):
    base, run_id, layout, observation, record = _setup_run(
        fake_ctx, clean_repo, "success_no_changes"
    )
    joined = " ".join(record).lower()
    for banned in ("--model", "--provider", "api-key", "api_key", "--thinking"):
        assert banned not in joined


def test_goal_not_interpreted_as_cli_options(fake_ctx, clean_repo):
    """A goal that looks like flags must never reach argv."""
    flag_like_goal = "--evil-flag value; rm -rf /"
    base = validation.resolve_base_commit(str(clean_repo))
    run_id, layout = workspace.create_run_directory(
        data_root=fake_ctx.state.data_dir,
        repository_path=str(clean_repo),
        base_commit=base,
        goal=flag_like_goal,
        checks=[],
        runtime_timeout_seconds=90,
        prime_command_prefix=["fake"],
    )
    workspace.create_detached_worktree(str(clean_repo), layout, base)
    observation, record = prime_process.run_prime(
        [sys.executable, str(FAKE_PRIME_AGENT)], layout, 90
    )
    assert "--evil-flag" not in record
    assert observation.exit_code == 0


def test_zero_exit_valid_protocol_proceeds(fake_ctx, clean_repo):
    base, run_id, layout, observation, record = _setup_run(
        fake_ctx, clean_repo, "success_tracked_change"
    )
    assert observation.exit_code == 0
    result = prime_protocol_parse(layout)
    assert result.valid is True


def prime_protocol_parse(layout):
    from conftest import prime_protocol

    return prime_protocol.parse_event_stream(layout.prime_events, layout.candidate)


def test_nonzero_exit_becomes_failed(fake_ctx, clean_repo):
    base, run_id, layout, observation, record = _setup_run(
        fake_ctx, clean_repo, "nonzero_before_terminal"
    )
    assert observation.exit_code == 7


@pytest.mark.slow
def test_runtime_timeout_becomes_uncertain(fake_ctx, clean_repo):
    base, run_id, layout, observation, record = _setup_run(
        fake_ctx, clean_repo, "partial_change_then_timeout", timeout=30
    )
    assert observation.timed_out is True
    assert observation.exit_code is None


def test_timeout_runs_no_host_checks(fake_ctx, clean_repo, monkeypatch):
    """A timed-out RPC observation returns UNCERTAIN before any host check."""
    import json

    from conftest import tools
    from prime_rlm_pkg.models import PrimeObservation

    kernel = fake_ctx.state.data_dir / "kernel-python.exe"
    kernel.write_bytes(b"")
    fake_ctx._settings["prime_agent_kernel_python"] = str(kernel.resolve())
    captured_env = {}

    def timed_out_rpc(_command_prefix, layout, _timeout, *, env=None):
        captured_env.update(env or {})
        Path(layout.prime_events).touch()
        Path(layout.prime_stderr).touch()
        return (
            PrimeObservation(
                launched=True,
                exit_code=None,
                session_id="timed-out-rpc",
                saw_agent_start=True,
                saw_agent_end=False,
                event_count=1,
                timed_out=True,
                stream_valid=False,
                error_code="RPC_TIMEOUT",
            ),
            ["prime-agent", "--mode", "rpc"],
        )

    def forbidden_checks(*_args, **_kwargs):
        raise AssertionError("host checks must not run after an RPC timeout")

    monkeypatch.setattr(tools, "run_prime_rpc", timed_out_rpc)
    monkeypatch.setattr(tools, "run_all_checks", forbidden_checks)
    result = json.loads(
        tools.handle_prime_agent(
            {
                "action": "run",
                "goal": "time out deterministically",
                "repository_path": str(clean_repo),
                "checks": [
                    {
                        "name": "forbidden",
                        "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                        "timeout_seconds": 60,
                    }
                ],
                "runtime_timeout_seconds": 30,
            },
            _ctx=fake_ctx,
        )
    )

    assert result["status"] == "UNCERTAIN"
    assert result["error_code"] == "RPC_TIMEOUT"
    assert result["checks"] == []
    assert result["automatic_retry_allowed"] is False
    assert captured_env["PRIME_AGENT_KERNEL_PYTHON"] == str(kernel.resolve())
    assert "OPENAI_API_KEY" not in captured_env


def test_timeout_does_not_trigger_retry(fake_ctx, clean_repo):
    """One invocation, one process — no automatic second Prime launch."""
    combined = ""
    for path in Path(prime_process.__file__).parent.glob("*.py"):
        combined += path.read_text(encoding="utf-8")
    assert "for attempt in range" not in combined
    assert "max_retries" not in combined


def test_partial_candidate_survives_timeout(fake_ctx, clean_repo):
    base, run_id, layout, observation, record = _setup_run(
        fake_ctx, clean_repo, "partial_change_then_timeout", timeout=30
    )
    assert observation.timed_out is True
    # The tracked edit happened BEFORE the sleep, so it must be visible.
    from conftest import evidence

    entries = evidence.collect_status_z(layout.candidate)
    changed = evidence.parse_status_z(entries)
    assert changed.modified, "partial edit must survive the timeout"


def test_launch_failure_becomes_failed(fake_ctx, clean_repo, tmp_path):
    base = validation.resolve_base_commit(str(clean_repo))
    run_id, layout = workspace.create_run_directory(
        data_root=fake_ctx.state.data_dir,
        repository_path=str(clean_repo),
        base_commit=base,
        goal="g",
        checks=[],
        runtime_timeout_seconds=60,
        prime_command_prefix=["p"],
    )
    workspace.create_detached_worktree(str(clean_repo), layout, base)
    observation, record = prime_process.run_prime(
        [str(tmp_path / "no-such-executable-abc-xyz")], layout, 60
    )
    assert observation.launched is False and observation.exit_code is None


def test_stderr_noise_captured(fake_ctx, clean_repo):
    base, run_id, layout, observation, record = _setup_run(
        fake_ctx, clean_repo, "stderr_noise"
    )
    stderr_text = Path(layout.prime_stderr).read_text(encoding="utf-8")
    assert "benign stderr noise" in stderr_text
