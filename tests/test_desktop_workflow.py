"""Isolated form, consent, and UI tests. No installed Hermes or provider needed."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "prime_ui_under_test"
if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = package
    spec.loader.exec_module(package)
workflow = importlib.import_module(f"{PACKAGE}.desktop_workflow")
desktop = importlib.import_module(f"{PACKAGE}.desktop")
ui = importlib.import_module(f"{PACKAGE}.desktop_ui")
schemas = importlib.import_module(f"{PACKAGE}.schemas")


@pytest.mark.parametrize("preset,tail", [
    ("Python: pytest", ["-m", "pytest", "-q"]),
    ("Python: unittest", ["-m", "unittest", "discover", "-v"]),
    ("Node.js: npm test", ["test"]),
    ("Rust: cargo test", ["test"]),
])
def test_presets_make_fixed_argv_and_keep_spaces_unicode(tmp_path, preset, tail):
    request = workflow.build_request(str(tmp_path / "résumé project"), "Fix parsing", preset, "20")
    assert request["checks"][0]["argv"][1:] == tail
    assert request["checks"][0]["timeout_seconds"] == 120
    assert request["runtime_timeout_seconds"] == 1200
    assert request["repository_path"].endswith("résumé project")


def test_none_is_explicitly_unverified_and_custom_python_is_one_argument(tmp_path):
    interpreter = str(tmp_path / "environment & test" / "python")
    result = workflow.build_request(str(tmp_path), "Fix", "Python: pytest", "1", interpreter)
    assert result["checks"][0]["argv"][0] == interpreter
    assert workflow.build_request(str(tmp_path), "Fix", "No verification", "60")["checks"] == []


@pytest.mark.parametrize("minutes", ["", "0", "61", "-1", "1.5", "1;echo hi", "999", "１２"])
def test_invalid_runtime_rejected(tmp_path, minutes):
    with pytest.raises(workflow.FormError):
        workflow.build_request(str(tmp_path), "Fix", "Python: pytest", minutes)


@pytest.mark.parametrize("folder,goal,preset,python_path", [
    ("relative", "Fix", "Python: pytest", ""),
    ("", "Fix", "Python: pytest", ""),
    (None, " ", "Python: pytest", ""),
    (None, "x" * (schemas.GOAL_MAX_CHARS + 1), "Python: pytest", ""),
    (None, "Fix\x00", "Python: pytest", ""),
    (None, "Fix", "execute anything", ""),
    (None, "Fix", "Python: pytest", "python -c anything"),
], ids=["relative-folder", "empty-folder", "empty-goal", "over-goal-bound",
        "nul-goal", "unknown-preset", "shell-as-interpreter"])
def test_bad_form_rejected(tmp_path, folder, goal, preset, python_path):
    with pytest.raises(workflow.FormError):
        workflow.build_request(str(tmp_path) if folder is None else folder, goal, preset, "20", python_path)


def test_no_probe_or_run_on_declined_confirmation(tmp_path):
    controller = SimpleNamespace(session_id="session", submit=lambda *_args: pytest.fail("not approved"))
    args = workflow.build_request(str(tmp_path), "Fix", "No verification", "20")
    for choice in (False, None, "yes", 1):
        assert workflow.submit_with_consent(controller, args, lambda _text, c=choice: c) is None


def test_exact_reviewed_request_is_frozen_before_confirmation(tmp_path):
    calls = []
    args = workflow.build_request(str(tmp_path), "Original task", "Python: pytest", "20")
    controller = SimpleNamespace(session_id="session", submit=lambda *values: calls.append(values))

    def confirm(text):
        assert "Original task" in text and "NOT a sandbox" in text and "pytest" in text
        args["goal"] = "Changed while prompt was open"
        args["checks"][0]["argv"].append("--changed")
        return True

    request_id = workflow.submit_with_consent(controller, args, confirm)
    assert len(request_id) == 36
    assert calls[0][2]["goal"] == "Original task"
    assert "--changed" not in calls[0][2]["checks"][0]["argv"]


@pytest.mark.parametrize("status,phrase", [
    ("VERIFIED", "Recorded host checks passed"),
    ("COMPLETED_UNVERIFIED", "NOT a verified fix"),
    ("FAILED_VERIFICATION", "did not pass"),
    ("FAILED", "run failed"),
    ("UNCERTAIN", "outcome is uncertain"),
    (None, "not admitted"),
])
def test_plain_language_preserves_status_semantics(status, phrase):
    text = workflow.result_text({"status": status, "checks": {"total": 1, "passed": 0}})
    assert phrase in text
    assert "No automatic retry" in text
    assert "unsigned" in text


def test_diagnostics_never_echo_raw_unrecognized_payload():
    payload = "private/path?credential=value"
    assert payload not in workflow.error_help(payload)
    assert "not prove provider login" in workflow.readiness_text({"ready": True, "state": "READY"})
    assert "Code: RPC_TIMEOUT" in workflow.error_help("RPC_TIMEOUT")


def test_demo_uses_real_controller_but_no_work_files_or_verified_claims(tmp_path):
    before = list(tmp_path.iterdir())
    controller = desktop.DesktopRunController(None, runner=workflow.demo_runner)
    try:
        request_id = workflow.submit_with_consent(controller, workflow.build_request(str(tmp_path), "demo", "No verification", "20"), lambda _: True)
        controller.close(wait=True)
        snapshot = controller.snapshot(controller.session_id, request_id)
        assert snapshot["state"] == "finished"
        assert snapshot["status"] == "COMPLETED_UNVERIFIED"
        assert snapshot["verified"] is False
        assert controller.review_references(controller.session_id, request_id) == {}
        assert "DEMO COMPLETE" in workflow.result_text(snapshot, demo=True)
        assert list(tmp_path.iterdir()) == before
    finally:
        controller.close(wait=True)


@pytest.fixture()
def window():
    try:
        import tkinter as tk
    except ImportError:
        if os.environ.get("HERMES_UI_REQUIRE_DISPLAY") == "1":
            pytest.fail("Native UI gate requires tkinter; do not silently skip")
        pytest.skip("Python has no Tk support")
    from tkinter import filedialog, scrolledtext, ttk

    try:
        root = tk.Tk()
    except tk.TclError:
        if os.environ.get("HERMES_UI_REQUIRE_DISPLAY") == "1":
            pytest.fail("Native UI gate requires a desktop display; do not silently skip")
        pytest.skip("No graphical display; run with xvfb-run on Linux")
    messages = []
    dialogs = (filedialog, SimpleNamespace(
        showinfo=lambda *_a, **_k: messages.append("info"),
        showerror=lambda *_a, **_k: messages.append("error"),
    ))
    windows = []

    def make(**kwargs):
        launcher = ui.Launcher(root, tk, ttk, dialogs, scrolledtext.ScrolledText, None, **kwargs)
        windows.append(launcher)
        return launcher

    yield root, make, messages
    for launcher in windows:
        launcher.controller.close(wait=True)
    if root.winfo_exists():
        root.destroy()


def pump_until(root, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert predicate(), "UI did not reach the expected bounded terminal state"


def test_native_demo_completes_and_has_no_idle_polling(window):
    root, make, _messages = window
    launcher = make(demo=True)
    root.update()
    assert not root.tk.call("after", "info")
    launcher.start()
    pump_until(root, lambda: not launcher.active)
    root.update()
    assert "DEMO COMPLETE" in launcher.output.get("1.0", "end")
    assert not root.tk.call("after", "info")
    assert not launcher.references
    assert str(launcher.setup_button["state"]) == "disabled"


def test_native_form_rejects_input_before_execution(window):
    _root, make, messages = window
    launcher = make()
    launcher.start()
    assert messages == ["error"]
    assert launcher.request_id is None
    assert launcher.controller.list_runs(launcher.controller.session_id)["runs"] == []


def test_native_approval_decline_does_not_start_worker(window, tmp_path):
    _root, make, _messages = window
    launcher = make()
    launcher.folder.set(str(tmp_path))
    launcher.goal.insert("1.0", "Fix tests")
    launcher.confirm = lambda _: False
    launcher.start()
    assert not launcher.active
    assert launcher.controller.list_runs(launcher.controller.session_id)["runs"] == []


def test_native_close_does_not_abandon_worker_and_double_click_does_not_repeat(window, tmp_path):
    root, make, messages = window
    release = threading.Event()
    calls = []

    def blocked(args, ctx):
        calls.append(args)
        release.wait(3)
        return workflow.demo_runner(args, ctx)

    controller = desktop.DesktopRunController(None, runner=blocked)
    launcher = make(controller=controller)
    launcher.folder.set(str(tmp_path))
    launcher.goal.insert("1.0", "Fix tests")
    launcher.confirm = lambda _: True
    try:
        launcher.start()
        launcher.start()
        launcher.close()
        assert messages == ["info"]
        assert root.winfo_exists()
    finally:
        release.set()
    pump_until(root, lambda: not launcher.active)
    assert len(calls) == 1
    assert "NOT a verified fix" in launcher.output.get("1.0", "end")


def test_native_setup_is_off_thread_and_read_only(window):
    root, make, _messages = window
    seen = []

    def inspect(_ctx):
        seen.append(threading.current_thread())
        return {"ready": True, "state": "READY"}

    launcher = make(inspect_fn=inspect)
    launcher.check_setup()
    pump_until(root, lambda: not launcher.readiness_running)
    assert seen[0] is not threading.current_thread()
    assert "No configuration was changed" in launcher.status.get()
    assert "Local setup checks passed" in launcher.output.get("1.0", "end")


def test_native_setup_failure_does_not_show_exception_text(window):
    root, make, _messages = window

    def inspect(_ctx):
        raise RuntimeError("private/path?credential=value")

    launcher = make(inspect_fn=inspect)
    launcher.check_setup()
    pump_until(root, lambda: not launcher.readiness_running)
    assert "private/path" not in launcher.output.get("1.0", "end")
    assert "READINESS_FAILED" in launcher.output.get("1.0", "end")


def test_real_launch_requires_bound_hermes_context(capsys):
    assert ui.launch_ui() == 2
    assert "hermes prime --ui" in capsys.readouterr().err


def test_module_import_does_not_require_tk_or_launch_processes():
    import ast

    tree = ast.parse((ROOT / "desktop_ui.py").read_text(encoding="utf-8"))
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            assert all(not alias.name.startswith("tkinter") for alias in statement.names)
        if isinstance(statement, ast.ImportFrom):
            assert not (statement.module or "").startswith("tkinter")


def test_cli_extension_preserves_existing_handler_and_rejects_mixed_modes(monkeypatch, capsys):
    import argparse

    from_context = []

    def configure(parser):
        sub = parser.add_subparsers(dest="prime_action")
        sub.add_parser("doctor")
        sub.add_parser("setup")

    setup = SimpleNamespace(configure_cli=configure, handle_cli=lambda args, ctx: from_context.append(ctx) or 7)
    monkeypatch.setitem(sys.modules, f"{PACKAGE}.setup_gate", setup)
    parent = sys.modules[PACKAGE]
    monkeypatch.setattr(parent, "setup_gate", setup, raising=False)
    cli = importlib.import_module(f"{PACKAGE}.ui_cli")
    parser = argparse.ArgumentParser()
    cli.configure_cli(parser)
    ctx = object()
    assert cli.handle_cli(parser.parse_args(["doctor"]), ctx=ctx) == 7
    assert from_context == [ctx]
    assert cli.handle_cli(parser.parse_args(["--demo"]), ctx=ctx) == 2
    assert cli.handle_cli(parser.parse_args(["--ui", "setup"]), ctx=ctx) == 2
    assert "without a setup" in capsys.readouterr().out
    launches = []
    monkeypatch.setattr(ui, "launch_ui", lambda ctx, demo: launches.append((ctx, demo)) or 0)
    assert cli.handle_cli(parser.parse_args(["--ui", "--demo"]), ctx=ctx) == 0
    assert launches == [(ctx, True)]


def test_workflow_does_not_accept_model_claim_as_verified():
    projection, _refs = desktop._project(json.dumps({
        "ok": True, "verified": True, "status": "VERIFIED", "checks": [],
        "prime_final_text": "Everything passed",
    }))
    assert projection["status"] == "UNCERTAIN"
    assert "outcome is uncertain" in workflow.result_text(projection)


def test_missing_tk_shows_actionable_fallback(monkeypatch, capsys):
    import builtins

    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "tkinter":
            raise ModuleNotFoundError("no Tk")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    assert ui.launch_ui(demo=True) == 2
    assert "optional Tk support" in capsys.readouterr().err


def test_missing_display_shows_headless_fallback(monkeypatch, capsys):
    tk = pytest.importorskip("tkinter")

    def no_display():
        raise tk.TclError("display inaccessible")

    monkeypatch.setattr(tk, "Tk", no_display)
    assert ui.launch_ui(demo=True) == 2
    assert "headless" in capsys.readouterr().err


def test_native_review_dialog_defaults_to_decline(window):
    root, make, _messages = window
    launcher = make()

    def dismiss():
        for widget in root.winfo_children():
            if widget.winfo_class() == "Toplevel":
                widget.destroy()

    root.after(50, dismiss)
    assert launcher.confirm("Exact request\nDo not start unless approved") is False
    assert launcher.controller.list_runs(launcher.controller.session_id)["runs"] == []


def test_form_and_guide_match_the_real_schema_goal_bound(tmp_path):
    request = workflow.build_request(
        str(tmp_path), "x" * schemas.GOAL_MAX_CHARS, "No verification", "20",
    )
    assert len(request["goal"]) == schemas.GOAL_MAX_CHARS
    guide = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    assert f"{schemas.GOAL_MAX_CHARS:,} characters" in guide
