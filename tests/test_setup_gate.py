"""Transactional Prime setup/readiness contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from conftest import setup_gate, tools, validation


def test_readiness_uses_effective_command_and_rejects_stale_model(fake_ctx):
    configured = ["prime-agent", "--model", "stealth/ox-alpha"]
    fake_ctx._settings["prime_agent_command"] = configured
    observed_commands: list[list[str]] = []

    def probe(command_prefix: list[str]):
        observed_commands.append(command_prefix)
        return setup_gate.ReadinessObservation(
            prime_version="0.8.1",
            provider="openrouter",
            model_id="stealth/ox-alpha",
            available_models=(("prime-inference", "z-ai/glm-5.2"),),
            auto_retry_disabled=True,
            kernel_proven=True,
            residual_processes=0,
        )

    report = setup_gate.inspect_readiness(fake_ctx, probe_fn=probe)

    assert observed_commands == [configured]
    assert report["ready"] is False
    assert report["state"] == "NOT_READY"
    assert report["reason_code"] == "EFFECTIVE_MODEL_NOT_AVAILABLE"
    assert report["effective_command_sha256"]
    assert report["provider"] == "openrouter"
    assert report["model"] == "stealth/ox-alpha"


def test_doctor_json_is_read_only_and_emits_machine_report(fake_ctx, monkeypatch, capsys):
    expected = {
        "ready": False,
        "state": "NOT_READY",
        "reason_code": "EFFECTIVE_MODEL_NOT_AVAILABLE",
    }
    monkeypatch.setattr(setup_gate, "inspect_readiness", lambda _ctx: expected)
    before = dict(fake_ctx._settings)
    parser = argparse.ArgumentParser()
    setup_gate.configure_cli(parser)
    args = parser.parse_args(["doctor", "--json"])

    exit_code = setup_gate.handle_cli(args, ctx=fake_ctx)

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == expected
    assert fake_ctx._settings == before


def test_readiness_preserves_uncertain_probe_failure(fake_ctx):
    def probe(_command_prefix: list[str]):
        raise setup_gate.ReadinessProbeError("RPC_TIMEOUT")

    report = setup_gate.inspect_readiness(fake_ctx, probe_fn=probe)

    assert report["ready"] is False
    assert report["state"] == "UNCERTAIN"
    assert report["reason_code"] == "RPC_TIMEOUT"
    assert report["automatic_retry_allowed"] is False


def test_default_probe_combines_version_rpc_and_kernel_observations(monkeypatch):
    configured = ["prime-agent", "--model", "z-ai/glm-5.2"]
    monkeypatch.setattr(setup_gate, "_probe_version", lambda _command: "0.8.1")
    monkeypatch.setattr(
        setup_gate,
        "_probe_rpc_state",
        lambda _command, **_kwargs: {
            "stream_valid": True,
            "error_code": None,
            "provider": "prime-inference",
            "model": "z-ai/glm-5.2",
            "available_models": (("prime-inference", "z-ai/glm-5.2"),),
            "auto_retry_disabled": True,
            "host_terminated": False,
        },
    )
    monkeypatch.setattr(setup_gate, "_probe_kernel_health", lambda _path=None: True)

    observation = setup_gate._probe_effective_command(configured)

    assert observation.prime_version == "0.8.1"
    assert observation.provider == "prime-inference"
    assert observation.model_id == "z-ai/glm-5.2"
    assert observation.kernel_proven is True
    assert observation.residual_processes == 0


def test_version_probe_adapter_matches_current_validation_return_shape(monkeypatch):
    monkeypatch.setattr(
        validation,
        "probe_prime_version",
        lambda _command: ((0, 8, 1), "0.8.1"),
    )

    assert setup_gate._probe_version(["prime-agent"]) == "0.8.1"


def test_setup_plan_requires_digest_and_commits_only_after_ready(fake_ctx):
    old_command = ["prime-agent", "--model", "stealth/ox-alpha"]
    proposed_command = ["prime-agent"]
    fake_ctx._settings["prime_agent_command"] = old_command
    plan = setup_gate.build_setup_plan(fake_ctx, proposed_command=proposed_command)

    cancelled = setup_gate.commit_setup_plan(
        fake_ctx,
        plan,
        accepted_plan_digest=None,
        verify_fn=lambda _ctx: {"ready": True, "state": "READY"},
    )

    assert cancelled["state"] == "CANCELLED"
    assert fake_ctx._settings["prime_agent_command"] == old_command
    assert not (fake_ctx.state.data_dir / "setup-transactions").exists()

    committed = setup_gate.commit_setup_plan(
        fake_ctx,
        plan,
        accepted_plan_digest=plan["plan_sha256"],
        verify_fn=lambda ctx: {
            "ready": setup_gate._configured_runtime(ctx)["command"]
            == proposed_command,
            "state": "READY",
        },
    )

    assert committed["ready"] is True
    assert committed["state"] == "READY"
    assert setup_gate._configured_runtime(fake_ctx)["command"] == proposed_command
    receipt_path = Path(committed["receipt_path"])
    assert receipt_path.is_file()
    envelope = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt = envelope["receipt"]
    assert envelope["receipt_sha256"] == setup_gate._canonical_sha256(receipt)
    assert receipt["plan_sha256"] == plan["plan_sha256"]
    assert receipt["state_ownership"]["l1_task_acceptance"] == "hermes"
    assert receipt["state_ownership"]["l3_prime_sessions"] == "disabled"
    assert receipt["automatic_retry_allowed"] is False
    assert receipt["runtime_provenance"] == "EXTERNAL_OBSERVED"
    assert receipt["model_calls"] == 0
    assert receipt["model_tokens"] == 0
    assert receipt["model_cost"] == 0
    assert receipt["residual_owned_processes"] == 0
    assert receipt["provider_canary"] == "NOT_RUN"
    assert receipt["release_ready"] is False


def test_setup_proves_bare_native_command_before_default_negative_consent(
    fake_ctx, tmp_path, monkeypatch
):
    stale = ["prime-agent", "--model", "stealth/ox-alpha"]
    fake_ctx._settings["prime_agent_command"] = stale
    proposed_commands: list[list[str]] = []
    kernel = tmp_path / "kernel-venv" / "Scripts" / "python.exe"
    kernel.parent.mkdir(parents=True)
    kernel.write_bytes(b"")
    monkeypatch.setattr(
        setup_gate, "_kernel_python_candidates", lambda _ctx=None: (kernel,)
    )
    monkeypatch.setattr(
        setup_gate, "_probe_kernel_health", lambda _kernel=None: True
    )

    def current_report(_ctx):
        return {
            "ready": False,
            "state": "NOT_READY",
            "reason_code": "EFFECTIVE_MODEL_NOT_AVAILABLE",
        }

    def inspect_command(command_prefix: list[str], **_kwargs):
        proposed_commands.append(command_prefix)
        return {"ready": True, "state": "READY", "reason_code": None}

    result = setup_gate.run_setup(
        fake_ctx,
        inspect_current_fn=current_report,
        inspect_command_fn=inspect_command,
        consent_fn=lambda _plan: None,
    )

    assert proposed_commands == [["prime-agent"]]
    assert result["state"] == "CANCELLED"
    assert result["reason_code"] == "CONSENT_NOT_GRANTED"
    assert fake_ctx._settings["prime_agent_command"] == stale


def test_setup_repairs_windows_kernel_path_in_same_plan(fake_ctx, tmp_path, monkeypatch):
    kernel = tmp_path / "kernel-venv" / "Scripts" / "python.exe"
    kernel.parent.mkdir(parents=True)
    kernel.write_bytes(b"")
    observed = {}

    monkeypatch.setattr(
        setup_gate, "_kernel_python_candidates", lambda _ctx=None: (kernel,)
    )
    monkeypatch.setattr(
        setup_gate,
        "_probe_kernel_health",
        lambda kernel_python=None: kernel_python is not None
        and Path(kernel_python).resolve() == kernel.resolve(),
    )

    result = setup_gate.run_setup(
        fake_ctx,
        inspect_current_fn=lambda _ctx: {
            "ready": False,
            "state": "NOT_READY",
            "reason_code": "KERNEL_NOT_PROVEN",
        },
        inspect_command_fn=lambda command, **kwargs: observed.update(
            {"command": command, **kwargs}
        )
        or {"ready": True, "state": "READY"},
        consent_fn=lambda plan: observed.update({"plan": plan}) or None,
    )

    assert result["state"] == "CANCELLED"
    assert observed["kernel_python"] == str(kernel.resolve())
    assert observed["plan"]["proposed_prime_runtime"] == {
        "command": setup_gate._configured_runtime(fake_ctx)["command"],
        "kernel_python": str(kernel.resolve()),
    }


def test_kernel_candidates_include_profile_scoped_managed_runtime(fake_ctx):
    candidates = setup_gate._kernel_python_candidates(fake_ctx)

    expected_root = (
        fake_ctx.state.data_dir / "runtime" / "kernel" / "prime-0.8.1"
    )
    assert any(expected_root in candidate.parents for candidate in candidates)


def test_packaged_runtime_lock_pins_all_prime_release_tarballs():
    lock_path = Path(__file__).resolve().parent.parent / "runtime_lock.json"

    lock = setup_gate.load_runtime_lock(lock_path)

    assert lock["prime_version"] == "0.8.1"
    assert lock["prime_commit"] == "514633727bf26d74f39f3119c2b0e31a5ceb2a9d"
    assert {asset["name"] for asset in lock["prime_assets"]} == {
        "prime-agent-0.8.1.tgz",
        "prime-agent-ai-0.8.1.tgz",
        "prime-agent-core-0.8.1.tgz",
        "prime-agent-tui-0.8.1.tgz",
    }
    assert all(len(asset["sha256"]) == 64 for asset in lock["prime_assets"])


def test_runtime_lock_rejects_unapproved_origin(tmp_path):
    path = tmp_path / "runtime-lock.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "prime_version": "0.8.1",
                "prime_commit": "514633727bf26d74f39f3119c2b0e31a5ceb2a9d",
                "allowed_origins": ["https://github.com"],
                "prime_assets": [
                    {
                        "name": "prime-agent-0.8.1.tgz",
                        "url": "https://evil.example/prime-agent-0.8.1.tgz",
                        "sha256": "a" * 64,
                        "size": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        setup_gate.load_runtime_lock(path)
    except setup_gate.RuntimeLockError as exc:
        assert exc.error_code == "RUNTIME_LOCK_ORIGIN_NOT_ALLOWED"
    else:  # pragma: no cover - explicit fail-closed assertion
        raise AssertionError("unapproved origin was accepted")


def test_setup_cli_is_default_negative_and_doctor_fix_uses_same_engine(
    fake_ctx, monkeypatch, capsys
):
    calls = []

    def fake_run(ctx, *, consent_fn, **_kwargs):
        plan = {"plan_sha256": "a" * 64, "mutations": ["prime_agent_command"]}
        calls.append({"ctx": ctx, "accepted": consent_fn(plan)})
        return {
            "ready": False,
            "state": "CANCELLED",
            "reason_code": "CONSENT_NOT_GRANTED",
        }

    monkeypatch.setattr(setup_gate, "run_setup", fake_run)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    parser = argparse.ArgumentParser()
    setup_gate.configure_cli(parser)

    assert setup_gate.handle_cli(parser.parse_args(["setup"]), ctx=fake_ctx) == 1
    assert setup_gate.handle_cli(parser.parse_args(["doctor", "--fix"]), ctx=fake_ctx) == 1
    assert calls == [
        {"ctx": fake_ctx, "accepted": None},
        {"ctx": fake_ctx, "accepted": None},
    ]
    assert "a" * 64 in capsys.readouterr().out


def test_noninteractive_setup_requires_scoped_consent_flags():
    parser = argparse.ArgumentParser()
    setup_gate.configure_cli(parser)

    args = parser.parse_args(
        [
            "setup",
            "--non-interactive",
            "--plan-digest",
            "b" * 64,
            "--accept-config",
        ]
    )
    assert args.non_interactive is True
    assert args.plan_digest == "b" * 64
    assert args.accept_config is True
    with pytest.raises(SystemExit):
        parser.parse_args(["setup", "--yes"])


def test_managed_config_refusal_is_receipted_without_yaml_fallback(
    fake_ctx, monkeypatch
):
    old_command = ["prime-agent", "--model", "stale/model"]
    fake_ctx._settings["prime_agent_command"] = old_command
    plan = setup_gate.build_setup_plan(fake_ctx, proposed_command=["prime-agent"])

    def refuse(_key, _value):
        raise PermissionError("Plugin settings cannot be changed in a managed install")

    monkeypatch.setattr(fake_ctx, "set_config", refuse)
    result = setup_gate.commit_setup_plan(
        fake_ctx,
        plan,
        accepted_plan_digest=plan["plan_sha256"],
        verify_fn=lambda _ctx: pytest.fail("verification must not run"),
    )

    assert result["state"] == "NOT_READY"
    assert result["reason_code"] == "SETUP_CONFIG_MANAGED"
    assert fake_ctx._settings["prime_agent_command"] == old_command
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))[
        "receipt"
    ]
    assert receipt["state"] == "NOT_READY"
    assert receipt["reason_code"] == "SETUP_CONFIG_MANAGED"


def test_setup_commits_command_and_kernel_as_one_runtime_object(fake_ctx, tmp_path):
    kernel = tmp_path / "kernel" / "python.exe"
    kernel.parent.mkdir()
    kernel.write_bytes(b"")
    fake_ctx._settings["prime_agent_command"] = [
        "prime-agent",
        "--model",
        "stale/model",
    ]
    plan = setup_gate.build_setup_plan(
        fake_ctx,
        proposed_command=["prime-agent"],
        proposed_kernel_python=str(kernel.resolve()),
    )

    result = setup_gate.commit_setup_plan(
        fake_ctx,
        plan,
        accepted_plan_digest=plan["plan_sha256"],
        verify_fn=lambda ctx: {
            "ready": setup_gate._configured_runtime(ctx)
            == {
                "command": ["prime-agent"],
                "kernel_python": str(kernel.resolve()),
            },
            "state": "READY",
        },
    )

    assert result["state"] == "READY"
    assert fake_ctx.set_config_calls == [
        (
            "prime_runtime",
            {
                "command": ["prime-agent"],
                "kernel_python": str(kernel.resolve()),
            },
        )
    ]


def test_journal_permission_failure_returns_uncertain_without_config_write(
    fake_ctx, monkeypatch
):
    plan = setup_gate.build_setup_plan(
        fake_ctx,
        proposed_command=setup_gate._configured_runtime(fake_ctx)["command"],
    )
    monkeypatch.setattr(
        setup_gate,
        "_start_setup_transaction",
        lambda _ctx, _plan: (_ for _ in ()).throw(PermissionError("denied")),
    )

    result = setup_gate.commit_setup_plan(
        fake_ctx,
        plan,
        accepted_plan_digest=plan["plan_sha256"],
        verify_fn=lambda _ctx: pytest.fail("verification must not run"),
    )

    assert result["state"] == "UNCERTAIN_SETUP"
    assert result["reason_code"] == "SETUP_JOURNAL_UNAVAILABLE"
    assert fake_ctx.set_config_calls == []


def test_post_commit_verifier_exception_rolls_back_and_is_receipted(fake_ctx):
    original = setup_gate._configured_runtime(fake_ctx)
    plan = setup_gate.build_setup_plan(
        fake_ctx,
        proposed_command=["prime-agent"],
    )

    def explode(_ctx):
        raise RuntimeError("unexpected verifier failure")

    result = setup_gate.commit_setup_plan(
        fake_ctx,
        plan,
        accepted_plan_digest=plan["plan_sha256"],
        verify_fn=explode,
    )

    assert result["state"] == "UNCERTAIN_SETUP"
    assert result["reason_code"] == "POST_COMMIT_VERIFIER_ERROR"
    assert setup_gate._configured_runtime(fake_ctx) == original
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))[
        "receipt"
    ]
    assert receipt["state"] == "UNCERTAIN_SETUP"


def test_json_setup_plan_never_discloses_raw_command_tokens(fake_ctx, capsys):
    credential_argument = "credential-bearing-value"
    fake_ctx._settings["prime_agent_command"] = [
        "prime-agent",
        "--api-key",
        credential_argument,
    ]
    plan = setup_gate.build_setup_plan(
        fake_ctx,
        proposed_command=["prime-agent", "--api-key", credential_argument],
    )
    args = argparse.Namespace(
        json_output=True,
        non_interactive=True,
        accept_config=False,
        plan_digest="",
    )

    assert setup_gate._cli_consent(args)(plan) is None
    output = capsys.readouterr().out

    assert credential_argument not in output
    public_plan = json.loads(output)["setup_plan"]
    assert public_plan["plan_sha256"] == plan["plan_sha256"]
    assert "current_prime_agent_command" not in public_plan
    assert "proposed_prime_runtime" not in public_plan


def test_config_read_failure_never_falls_back_to_bare_prime(tmp_path):
    class BrokenContext:
        def get_config(self, _key, _default=None):
            raise RuntimeError("config backend unavailable")

    with pytest.raises(validation.ValidationError) as captured:
        tools.resolve_prime_command(BrokenContext())

    assert captured.value.error_code == "PRIME_CONFIG_READ_FAILED"


def test_unproven_rollback_returns_uncertain_setup(fake_ctx, monkeypatch):
    original = setup_gate._configured_runtime(fake_ctx)
    plan = setup_gate.build_setup_plan(
        fake_ctx,
        proposed_command=["prime-agent"],
    )
    writes = []

    def partial_set(key, value):
        writes.append((key, value))
        if len(writes) == 1:
            fake_ctx._settings[key] = value
        # rollback write is deliberately ignored

    monkeypatch.setattr(fake_ctx, "set_config", partial_set)
    result = setup_gate.commit_setup_plan(
        fake_ctx,
        plan,
        accepted_plan_digest=plan["plan_sha256"],
        verify_fn=lambda _ctx: {
            "ready": False,
            "state": "NOT_READY",
            "reason_code": "POST_COMMIT_NOT_READY",
        },
    )

    assert result["state"] == "UNCERTAIN_SETUP"
    assert result["reason_code"] == "CONFIG_ROLLBACK_UNPROVEN"
    assert setup_gate._configured_runtime(fake_ctx) != original


def test_partial_managed_write_is_uncertain_when_rollback_cannot_be_proven(
    fake_ctx, monkeypatch
):
    plan = setup_gate.build_setup_plan(
        fake_ctx,
        proposed_command=["prime-agent"],
    )
    writes = []

    def partial_managed_set(key, value):
        writes.append((key, value))
        if len(writes) == 1:
            fake_ctx._settings[key] = value
            raise PermissionError("administrator-managed")
        # rollback write is deliberately ignored

    monkeypatch.setattr(fake_ctx, "set_config", partial_managed_set)
    result = setup_gate.commit_setup_plan(
        fake_ctx,
        plan,
        accepted_plan_digest=plan["plan_sha256"],
        verify_fn=lambda _ctx: pytest.fail("verification must not run"),
    )

    assert result["state"] == "UNCERTAIN_SETUP"
    assert result["reason_code"] == "CONFIG_ROLLBACK_UNPROVEN"


def test_keyboard_interrupt_after_commit_rolls_back_and_is_receipted(fake_ctx):
    original = setup_gate._configured_runtime(fake_ctx)
    plan = setup_gate.build_setup_plan(
        fake_ctx,
        proposed_command=["prime-agent"],
    )

    def interrupt(_ctx):
        raise KeyboardInterrupt

    result = setup_gate.commit_setup_plan(
        fake_ctx,
        plan,
        accepted_plan_digest=plan["plan_sha256"],
        verify_fn=interrupt,
    )

    assert result["state"] == "UNCERTAIN_SETUP"
    assert result["reason_code"] == "POST_COMMIT_INTERRUPTED"
    assert setup_gate._configured_runtime(fake_ctx) == original
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))[
        "receipt"
    ]
    assert receipt["state"] == "UNCERTAIN_SETUP"
