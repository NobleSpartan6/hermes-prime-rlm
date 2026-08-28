"""End-to-end Prime RPC subprocess tests with the deterministic fake runtime."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from conftest import make_fake_prime_command, prime_rpc, workspace


def _layout(tmp_path: Path):
    run_dir = tmp_path / "run"
    candidate = run_dir / "candidate"
    candidate.mkdir(parents=True)
    (candidate / "README.md").write_text("# candidate\n", encoding="utf-8")
    layout = workspace.run_layout(run_dir)
    Path(layout.prime_task_md).write_text(
        workspace.build_task_envelope("Add a greeting through strict RPC."), encoding="utf-8"
    )
    return layout


def _run(tmp_path: Path, monkeypatch, scenario: str, timeout_seconds: int = 10):
    command, _ = make_fake_prime_command(tmp_path)
    monkeypatch.setenv("FAKE_PRIME_SCENARIO", scenario)
    layout = _layout(tmp_path)
    observation, argv = prime_rpc.run_prime_rpc(
        command, layout, timeout_seconds=timeout_seconds, env=dict(os.environ)
    )
    return observation, argv, layout


def test_no_model_probe_stages_admission_and_never_sends_prompt(tmp_path, monkeypatch):
    command, _ = make_fake_prime_command(tmp_path)
    monkeypatch.setenv("FAKE_PRIME_SCENARIO", "rpc_success_fragmented")
    prompt_log = tmp_path / "prompt.txt"
    monkeypatch.setenv("FAKE_RPC_PROMPT_LOG", str(prompt_log))
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    before = sorted(candidate.iterdir())

    result, argv = prime_rpc.probe_prime_rpc(
        command,
        str(candidate),
        timeout_seconds=10,
        env=dict(os.environ),
    )

    assert result == {
        "stream_valid": True,
        "error_code": None,
        "provider": "openrouter",
        "model": "test/model",
        "available_models": (("openrouter", "test/model"),),
        "auto_retry_disabled": True,
        "host_terminated": False,
    }
    assert "--no-session" in argv
    assert "--offline" in argv
    assert not prompt_log.exists()
    assert sorted(candidate.iterdir()) == before


def test_no_model_probe_rejects_records_after_catalog(tmp_path, monkeypatch):
    command, _ = make_fake_prime_command(tmp_path)
    monkeypatch.setenv("FAKE_PRIME_SCENARIO", "rpc_readiness_extra_event")
    candidate = tmp_path / "candidate"
    candidate.mkdir()

    result, _argv = prime_rpc.probe_prime_rpc(
        command,
        str(candidate),
        timeout_seconds=10,
        env=dict(os.environ),
    )

    assert result["stream_valid"] is False
    assert result["error_code"] == "RPC_READINESS_UNEXPECTED_RECORD"
    assert result["host_terminated"] is True


def test_rpc_process_handles_fragmented_jsonl_and_edits_only_candidate(tmp_path, monkeypatch):
    prompt_log = tmp_path / "rpc-prompt.txt"
    monkeypatch.setenv("FAKE_RPC_PROMPT_LOG", str(prompt_log))
    observation, argv, layout = _run(tmp_path, monkeypatch, "rpc_success_fragmented")

    assert observation.launched is True
    assert observation.exit_code == 0
    assert observation.stream_valid is True
    assert observation.saw_agent_start is True
    assert observation.saw_agent_end is True
    assert observation.session_id == "fake-rpc-session"
    assert observation.final_text == "RPC candidate complete"
    assert "RPC edit by fake prime" in Path(layout.candidate, "README.md").read_text(
        encoding="utf-8"
    )
    assert "--mode" in argv and "rpc" in argv
    assert "--no-session" in argv
    assert "Add a greeting through strict RPC." not in "\x00".join(argv)
    prompt = prompt_log.read_text(encoding="utf-8")
    assert "Add a greeting through strict RPC." in prompt
    assert "HOST RULES" in prompt
    assert f"@{layout.prime_task_md}" not in prompt
    assert Path(layout.prime_events).is_file()
    assert Path(layout.prime_stderr).is_file()


@pytest.mark.parametrize(
    ("scenario", "error_code"),
    [
        ("rpc_malformed_json", "RPC_MALFORMED_JSON"),
        ("rpc_premature_eof", "RPC_PREMATURE_EOF"),
        ("rpc_oversized_record", "RPC_RECORD_TOO_LARGE"),
        ("rpc_prompt_rejected", "RPC_PROMPT_REJECTED"),
    ],
)
def test_rpc_process_protocol_failures_are_not_success(tmp_path, monkeypatch, scenario, error_code):
    observation, _argv, _layout = _run(tmp_path, monkeypatch, scenario)

    assert observation.launched is True
    assert observation.stream_valid is False
    assert observation.error_code == error_code
    assert observation.saw_agent_end is False


@pytest.mark.parametrize(
    ("scenario", "error_code", "timeout_seconds"),
    [
        ("rpc_oversized_record", "RPC_RECORD_TOO_LARGE", 10),
        ("rpc_malformed_json", "RPC_MALFORMED_JSON", 10),
        ("partial_change_then_timeout", "RPC_TIMEOUT", 1),
    ],
)
def test_protocol_failure_leaves_no_rpc_reader_thread(
    tmp_path, monkeypatch, scenario, error_code, timeout_seconds
):
    observation, _argv, _layout = _run(
        tmp_path, monkeypatch, scenario, timeout_seconds=timeout_seconds
    )

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and any(
        thread.name.startswith("prime-rpc-stdout-") for thread in threading.enumerate()
    ):
        time.sleep(0.05)

    assert observation.error_code == error_code
    assert not any(
        thread.name.startswith("prime-rpc-stdout-") for thread in threading.enumerate()
    )


def test_rpc_process_environment_does_not_require_goal_on_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PRIME_SCENARIO", "rpc_success_fragmented")
    monkeypatch.setenv("RPC_TEST_SENTINEL", "present")
    command, _ = make_fake_prime_command(tmp_path)
    layout = _layout(tmp_path)

    observation, argv = prime_rpc.run_prime_rpc(
        command,
        layout,
        timeout_seconds=10,
        env={**os.environ, "RPC_TEST_SENTINEL": "present"},
    )

    assert observation.stream_valid is True
    assert "RPC_TEST_SENTINEL" not in "\x00".join(argv)


def test_default_rpc_environment_excludes_ambient_provider_credentials():
    from prime_rlm_pkg.prime_rpc_process import build_rpc_environment

    child = build_rpc_environment(
        {
            "PATH": "bin",
            "HOME": "home",
            "OPENAI_API_KEY": "secret-openai",
            "PRIME_API_KEY": "secret-prime",
            "AWS_SECRET_ACCESS_KEY": "secret-aws",
            "PRIME_AGENT_KERNEL_PYTHON": "python-kernel",
        }
    )

    assert child["PATH"] == "bin"
    assert child["HOME"] == "home"
    assert child["PRIME_AGENT_KERNEL_PYTHON"] == "python-kernel"
    assert child["PI_SKIP_VERSION_CHECK"] == "1"
    assert child["PI_OFFLINE"] == "1"
    assert "OPENAI_API_KEY" not in child
    assert "PRIME_API_KEY" not in child
    assert "AWS_SECRET_ACCESS_KEY" not in child


def test_rpc_process_awaits_each_admission_response_before_prompt(tmp_path, monkeypatch):
    observation, _argv, _layout = _run(
        tmp_path, monkeypatch, "rpc_requires_staged_admission"
    )

    assert observation.stream_valid is True
    assert observation.saw_agent_end is True


def test_success_does_not_wait_for_descendant_inheriting_stderr(tmp_path, monkeypatch):
    started = time.monotonic()

    observation, _argv, _layout = _run(
        tmp_path, monkeypatch, "rpc_success_stderr_descendant"
    )

    assert observation.stream_valid is True
    assert time.monotonic() - started < 2.0
    assert not any(
        thread.name.startswith("prime-rlm-drain-prime-stderr")
        for thread in threading.enumerate()
    )
