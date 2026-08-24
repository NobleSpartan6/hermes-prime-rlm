"""Registration contract tests (spec §24 Registration)."""

from __future__ import annotations

import json

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
    assert provides == ["prime_rlm_run"]
    assert name == "prime-rlm"
    assert version == "0.1.0"


def test_register_registers_exactly_one_tool(fake_ctx):
    from conftest import prime_rlm_pkg

    prime_rlm_pkg.register(fake_ctx)
    assert len(fake_ctx.registered_tools) == 1
    tool = fake_ctx.registered_tools[0]
    assert tool["name"] == "prime_rlm_run"
    assert tool["toolset"] == "prime_rlm"
    assert callable(tool["handler"])


def test_schema_and_handler_agree(fake_ctx):
    from conftest import prime_rlm_pkg

    prime_rlm_pkg.register(fake_ctx)
    tool = fake_ctx.registered_tools[0]
    function = tool["schema"]
    assert function["name"] == "prime_rlm_run"
    params = function["parameters"]
    assert set(params["required"]) == {"goal", "repository_path", "checks"}


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

    assert conftest.prime_rlm_pkg.PLUGIN_VERSION == "0.1.0"
    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after


def test_handler_accepts_kwargs_and_always_returns_json(fake_ctx):
    handler = tools.handle_prime_rlm_run
    out = handler({"goal": "", "repository_path": "/tmp/x", "checks": []}, extra="kw")
    parsed = json.loads(out)
    assert isinstance(parsed, dict)
    assert parsed["ok"] is False


def test_handler_never_leaks_exceptions(fake_ctx, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("simulated internal explosion")

    monkeypatch.setattr(tools, "validate_goal", boom)
    out = tools.handle_prime_rlm_run({"goal": "x", "repository_path": "/", "checks": []})
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert parsed["stage"] == "internal"
