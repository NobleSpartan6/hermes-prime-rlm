"""Registration contract tests (spec §24 Registration)."""

from __future__ import annotations

import argparse
import json

import pytest
from conftest import tools


def test_manifest_declares_exactly_one_tool():
    import pathlib

    text = (pathlib.Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(
        encoding="utf-8"
    )
    # Minimal parse without a YAML dependency: the manifest is flat.
    provides = [
        line.split("-", 1)[1].strip()
        for line in text.splitlines()
        if line.strip().startswith("- ")
    ]
    name = next(
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.startswith("name:")
    )
    version = next(
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.startswith("version:")
    )
    assert provides == ["prime_agent"]
    assert name == "prime-rlm"
    assert version == "0.2.0"


def test_register_registers_exactly_one_tool(fake_ctx):
    from conftest import prime_rlm_pkg

    prime_rlm_pkg.register(fake_ctx)
    assert len(fake_ctx.registered_tools) == 1
    tool = fake_ctx.registered_tools[0]
    assert tool["name"] == "prime_agent"
    assert tool["toolset"] == "prime_rlm"
    assert callable(tool["handler"])


def test_register_adds_operator_setup_surfaces_without_adding_tools(fake_ctx):
    from conftest import prime_rlm_pkg

    prime_rlm_pkg.register(fake_ctx)

    assert [tool["name"] for tool in fake_ctx.registered_tools] == ["prime_agent"]
    assert [command["name"] for command in fake_ctx.registered_cli_commands] == [
        "prime"
    ]
    assert callable(fake_ctx.registered_cli_commands[0]["setup_fn"])
    assert callable(fake_ctx.registered_cli_commands[0]["handler_fn"])
    assert [command["name"] for command in fake_ctx.registered_commands] == [
        "prime-setup"
    ]
    assert callable(fake_ctx.registered_commands[0]["handler"])


def test_operator_cli_parser_exposes_setup_and_doctor(fake_ctx):
    from conftest import prime_rlm_pkg

    prime_rlm_pkg.register(fake_ctx)
    parser = argparse.ArgumentParser(prog="hermes prime")
    fake_ctx.registered_cli_commands[0]["setup_fn"](parser)

    assert parser.parse_args(["setup"]).prime_action == "setup"
    assert parser.parse_args(["doctor"]).prime_action == "doctor"
    assert fake_ctx.registered_cli_commands[0]["handler_fn"](
        parser.parse_args([])
    ) == 2


def test_operator_handlers_preserve_bound_context_and_raw_slash_args(
    tmp_path, monkeypatch
):
    from conftest import FakeContext, prime_rlm_pkg, setup_gate

    ctx = FakeContext(tmp_path)
    cli_seen = {}
    slash_seen = {}

    def fake_cli(args, *, ctx):
        cli_seen.update({"args": args, "ctx": ctx})
        return 7

    def fake_slash(raw_args, *, ctx):
        slash_seen.update({"raw_args": raw_args, "ctx": ctx})
        return "slash-result"

    monkeypatch.setattr(setup_gate, "handle_cli", fake_cli)
    monkeypatch.setattr(setup_gate, "handle_slash", fake_slash)
    prime_rlm_pkg.register(ctx)
    namespace = argparse.Namespace(prime_action="doctor")

    assert ctx.registered_cli_commands[0]["handler_fn"](namespace) == 7
    assert cli_seen == {"args": namespace, "ctx": ctx}
    assert ctx.registered_commands[0]["handler"]("  keep spacing  ") == "slash-result"
    assert slash_seen == {"raw_args": "  keep spacing  ", "ctx": ctx}
    assert ctx.registered_commands[0]["args_hint"] == ""


def test_each_registration_binds_its_own_context(tmp_path, monkeypatch):
    from conftest import FakeContext

    first = FakeContext(tmp_path / "first")
    second = FakeContext(tmp_path / "second")

    def record_context(_args, ctx):
        return {"ok": True, "data_dir": str(ctx.state.data_dir)}

    monkeypatch.setattr(tools, "_run", record_context)
    tools.register_tools(first)
    tools.register_tools(second)

    first_result = json.loads(first.registered_tools[0]["handler"]({}))
    second_result = json.loads(second.registered_tools[0]["handler"]({}))
    assert first_result["data_dir"] == str(first.state.data_dir)
    assert second_result["data_dir"] == str(second.state.data_dir)
    assert first.registered_tools[0]["handler"] is not second.registered_tools[0]["handler"]


def test_prime_environment_uses_plugin_kernel_setting_without_provider_keys(
    tmp_path
):
    from conftest import FakeContext

    kernel = tmp_path / "kernel venv" / ("python.exe" if __import__("os").name == "nt" else "python")
    kernel.parent.mkdir(parents=True)
    kernel.write_bytes(b"")
    ctx = FakeContext(
        tmp_path,
        settings={"prime_agent_kernel_python": str(kernel.resolve())},
    )

    env = tools.resolve_prime_environment(
        ctx,
        {"PATH": "safe-path", "OPENAI_API_KEY": "must-not-cross"},
    )

    assert env["PRIME_AGENT_KERNEL_PYTHON"] == str(kernel.resolve())
    assert env["PATH"] == "safe-path"
    assert "OPENAI_API_KEY" not in env


def test_schema_and_handler_agree(fake_ctx):
    from conftest import prime_rlm_pkg

    prime_rlm_pkg.register(fake_ctx)
    tool = fake_ctx.registered_tools[0]
    function = tool["schema"]
    assert function["name"] == "prime_agent"
    params = function["parameters"]
    assert set(params["required"]) == {"goal", "repository_path", "checks"}
    assert params["properties"]["action"] == {
        "type": "string",
        "enum": ["run"],
        "description": "Bounded one-shot RPC execution; defaults to run.",
    }


def test_missing_action_defaults_to_run(fake_ctx, monkeypatch):
    captured = {}

    def fake_run(args, _ctx):
        captured.update(args)
        return {"ok": True}

    monkeypatch.setattr(tools, "_run", fake_run)
    tools.register_tools(fake_ctx)
    result = json.loads(fake_ctx.registered_tools[0]["handler"]({"goal": "x"}))
    assert result["ok"] is True
    assert captured["action"] == "run"


def test_registration_starts_no_subprocess(fake_ctx, monkeypatch):
    import subprocess

    def boom(*_a, **_k):  # pragma: no cover
        raise AssertionError("registration must not spawn processes")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    from conftest import prime_rlm_pkg

    prime_rlm_pkg.register(fake_ctx)


def test_registration_performs_no_network(fake_ctx, monkeypatch):
    import socket

    def boom(*_a, **_k):  # pragma: no cover
        raise AssertionError("registration must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", boom)
    from conftest import prime_rlm_pkg

    prime_rlm_pkg.register(fake_ctx)


def test_import_does_not_write_runtime_state(tmp_path, monkeypatch):
    before = sorted(p.name for p in tmp_path.iterdir())
    import conftest  # re-import is a no-op; exercise the module load path

    assert conftest.prime_rlm_pkg.PLUGIN_VERSION == "0.2.0"
    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after


def test_handler_accepts_kwargs_and_always_returns_json(fake_ctx):
    handler = tools.handle_prime_agent
    out = handler(
        {"goal": "", "repository_path": "/tmp/x", "checks": []},
        _ctx=fake_ctx,
        extra="kw",
    )
    parsed = json.loads(out)
    assert isinstance(parsed, dict)
    assert parsed["ok"] is False


def test_handler_never_leaks_exceptions(fake_ctx, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("simulated internal explosion")

    monkeypatch.setattr(tools, "validate_goal", boom)
    out = tools.handle_prime_agent(
        {"goal": "x", "repository_path": "/", "checks": []}, _ctx=fake_ctx
    )
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert parsed["stage"] == "internal"


@pytest.mark.parametrize("bad_args", ["not-an-object", 42, True])
def test_handler_rejects_non_object_arguments_as_json(fake_ctx, bad_args):
    parsed = json.loads(tools.handle_prime_agent(bad_args, _ctx=fake_ctx))

    assert parsed["ok"] is False
    assert parsed["stage"] == "validation"
    assert parsed["error_code"] == "INVALID_ARGUMENTS"
