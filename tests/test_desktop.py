"""Desktop contract tests; also runnable with --noconftest without Hermes/Prime."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import threading
import time
import types
from pathlib import Path
from uuid import uuid4

import pytest

# Reuse the normal suite's package when present. A standalone run exercises the
# exact same source without importing the unrelated process/runtime fixtures.
if "prime_rlm_pkg" not in sys.modules:
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "prime_rlm_pkg", root / "__init__.py", submodule_search_locations=[str(root)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
desktop = importlib.import_module("prime_rlm_pkg.desktop")


def arguments():
    return {"goal": "test goal", "repository_path": "operator-selected-repository", "checks": []}


def outcome(status="VERIFIED", **overrides):
    result = {
        "status": status, "ok": status == "VERIFIED", "verified": status == "VERIFIED",
        "checks": [{"status": "passed", "exit_code": 0}] if status == "VERIFIED" else [],
        "run_id": "e14d2f6f-c1cc-40e8-a1b3-d302b1446bf1",
    }
    result.update(overrides)
    return json.dumps(result)


def finish(controller, key):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = controller.snapshot(controller.session_id, key)
        if value["state"] in {"finished", "rejected"}:
            return value
        time.sleep(0.001)
    pytest.fail("desktop worker did not reach a terminal state")


@pytest.fixture()
def factory():
    controllers = []

    def create(runner=None, **options):
        controller = desktop.DesktopRunController(
            object(), runner=runner or (lambda _args, _ctx: outcome()), **options,
        )
        controllers.append(controller)
        return controller

    yield create
    for controller in controllers:
        controller.close(wait=True)


def test_default_path_calls_existing_handler_once_with_bound_context(monkeypatch):
    calls = []
    module = types.ModuleType("prime_rlm_pkg.tools")

    def handler(args, **kwargs):
        calls.append((args, kwargs["_ctx"]))
        return outcome()

    module.handle_prime_agent = handler
    monkeypatch.setitem(sys.modules, module.__name__, module)
    ctx = object()
    controller = desktop.DesktopRunController(ctx)
    key = str(uuid4())
    try:
        controller.submit(controller.session_id, key, arguments())
        assert finish(controller, key)["verified"] is True
        controller.submit(controller.session_id, key, arguments())
        assert len(calls) == 1
        assert calls[0] == ({**arguments(), "action": "run"}, ctx)
    finally:
        controller.close(wait=True)


def test_snapshot_and_busy_rejection_do_not_wait_for_worker(factory):
    entered, release = threading.Event(), threading.Event()

    def run(_args, _ctx):
        entered.set()
        assert release.wait(5)
        return outcome()

    controller = factory(run)
    key = str(uuid4())
    try:
        accepted = controller.submit(controller.session_id, key, arguments())
        assert accepted["state"] == "accepted"
        assert entered.wait(2)
        assert controller.snapshot(controller.session_id, key)["state"] == "running"
        with pytest.raises(desktop.DesktopError, match="DESKTOP_BUSY"):
            controller.submit(controller.session_id, str(uuid4()), arguments())
        assert controller.close() is False  # no cancellation, no join
        assert not release.is_set()
    finally:
        release.set()
    assert finish(controller, key)["status"] == "VERIFIED"


def test_concurrent_duplicate_submissions_single_flight(factory):
    entered, release = threading.Event(), threading.Event()
    calls, errors = [], []

    def run(args, _ctx):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return outcome()

    controller = factory(run)
    key = str(uuid4())

    def submit():
        try:
            controller.submit(controller.session_id, key, arguments())
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(16)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        assert entered.wait(2)
        assert not errors
        assert len(calls) == 1
        with pytest.raises(desktop.DesktopError, match="REQUEST_ID_CONFLICT"):
            controller.submit(controller.session_id, key, {**arguments(), "goal": "different"})
    finally:
        release.set()
    finish(controller, key)


def test_caller_mutation_does_not_change_admitted_request(factory):
    entered, release = threading.Event(), threading.Event()
    seen = []

    def run(args, _ctx):
        entered.set()
        assert release.wait(5)
        seen.append(args)
        return outcome()

    controller = factory(run)
    args = arguments()
    args["checks"] = [{"name": "check", "argv": ["python", "test.py"]}]
    key = str(uuid4())
    try:
        controller.submit(controller.session_id, key, args)
        assert entered.wait(2)
        args["checks"][0]["argv"][1] = "changed.py"
    finally:
        release.set()
    finish(controller, key)
    assert seen[0]["checks"][0]["argv"][1] == "test.py"


def test_history_full_never_evicts_idempotency_keys(factory):
    controller = factory(history_limit=1)
    key = str(uuid4())
    controller.submit(controller.session_id, key, arguments())
    finish(controller, key)
    with pytest.raises(desktop.DesktopError, match="HISTORY_FULL"):
        controller.submit(controller.session_id, str(uuid4()), arguments())
    assert controller.submit(controller.session_id, key, arguments())["verified"] is True
    controller.close()
    assert controller.submit(controller.session_id, key, arguments())["verified"] is True


def test_restart_or_wrong_profile_session_is_never_replayed(factory):
    first, second = factory(), factory()
    with pytest.raises(desktop.DesktopError, match="SESSION_CHANGED_DO_NOT_REPLAY"):
        second.submit(first.session_id, str(uuid4()), arguments())
    assert second.poll(second.session_id)["events"] == []


def test_event_backpressure_exposes_gap_and_preserves_terminal_snapshot(factory):
    controller = factory(event_capacity=2)
    key = str(uuid4())
    controller.submit(controller.session_id, key, arguments())
    final = finish(controller, key)
    first = controller.poll(controller.session_id, after=0, limit=1)
    assert first["gap"] and first["has_more"]
    assert first["next_cursor"] == 2
    second = controller.poll(controller.session_id, after=first["next_cursor"])
    assert not second["gap"] and not second["has_more"]
    assert second["events"][0] == final
    second["events"][0]["checks"]["passed"] = -1
    assert controller.snapshot(controller.session_id, key)["checks"]["passed"] == 1


def test_projection_excludes_sensitive_text_paths_and_command_arguments(factory):
    sensitive_fixture = "PRIVATE_PAYLOAD_DO_NOT_RENDER"
    raw = outcome(
        prime_final_text=sensitive_fixture * 1000, message=sensitive_fixture,
        candidate_path=f"/private/{sensitive_fixture}/candidate", receipt_path=f"/private/{sensitive_fixture}/receipt.json",
        receipt_sha256="a" * 64,
        checks=[{"status": "passed", "exit_code": 0, "argv": [sensitive_fixture], "stdout": sensitive_fixture}],
    )
    controller = factory(lambda _args, _ctx: raw)
    key = str(uuid4())
    controller.submit(controller.session_id, key, arguments())
    snapshot = finish(controller, key)
    assert sensitive_fixture not in json.dumps(snapshot)
    assert sensitive_fixture not in json.dumps(controller.poll(controller.session_id))
    assert len(json.dumps(snapshot).encode()) < 2048
    assert snapshot["authenticity"] == "UNSIGNED"
    assert snapshot["acceptance_status"] == "PENDING"
    assert snapshot["artifact_integrity"] == "NOT_REVALIDATED_BY_DESKTOP"
    assert controller.review_references(controller.session_id, key)["candidate_path"].endswith("candidate")


@pytest.mark.parametrize("status", ["COMPLETED_UNVERIFIED", "FAILED_VERIFICATION", "FAILED", "UNCERTAIN"])
def test_statuses_are_never_promoted_to_verified(factory, status):
    controller = factory(lambda _args, _ctx: outcome(status, ok=True, verified=True))
    key = str(uuid4())
    controller.submit(controller.session_id, key, arguments())
    snapshot = finish(controller, key)
    assert snapshot["status"] == status
    assert not snapshot["verified"] and not snapshot["automatic_retry_allowed"]
    if status == "UNCERTAIN":
        assert snapshot["candidate_stability"] == "unknown"


@pytest.mark.parametrize("overrides", [
    {"checks": []}, {"ok": False}, {"verified": False},
    {"checks": [{"status": "passed", "exit_code": False}]},
    {"checks": [{"status": "failed", "exit_code": 0}]},
    {"checks": [{"status": "passed", "exit_code": 1}]},
])
def test_contradictory_verification_fails_closed(factory, overrides):
    controller = factory(lambda _args, _ctx: outcome(**overrides))
    key = str(uuid4())
    controller.submit(controller.session_id, key, arguments())
    result = finish(controller, key)
    assert result["status"] == "UNCERTAIN"
    assert result["error_code"] == "DESKTOP_INVALID_RESULT"


@pytest.mark.parametrize("raw", [
    "not JSON", "[]", "null", '{}', '{"status": ["VERIFIED"]}',
    "[" * 2000 + "0" + "]" * 2000, "x" * (desktop.MAX_RESULT_BYTES + 1),
    "é" * desktop.MAX_RESULT_BYTES, {},
])
def test_malformed_or_oversized_results_are_bounded_unknowns(factory, raw):
    controller = factory(lambda _args, _ctx: raw)
    key = str(uuid4())
    controller.submit(controller.session_id, key, arguments())
    assert finish(controller, key)["status"] == "UNCERTAIN"
    assert controller.review_references(controller.session_id, key) == {}


@pytest.mark.parametrize("error", [RuntimeError("DO_NOT_LEAK"), SystemExit("DO_NOT_LEAK")])
def test_worker_failure_never_retries_or_exposes_exception(factory, error):
    calls = []

    def run(_args, _ctx):
        calls.append(1)
        raise error

    controller = factory(run)
    key = str(uuid4())
    controller.submit(controller.session_id, key, arguments())
    value = finish(controller, key)
    assert value["error_code"] == "DESKTOP_WORKER_FAILED"
    assert "DO_NOT_LEAK" not in json.dumps(value)
    controller.submit(controller.session_id, key, arguments())
    assert len(calls) == 1


def test_admission_errors_are_distinct_from_finished_runs(factory):
    controller = factory(lambda _args, _ctx: json.dumps({
        "ok": False, "stage": "validation", "error_code": "DIRTY_REPOSITORY", "message": "PRIVATE",
    }))
    key = str(uuid4())
    controller.submit(controller.session_id, key, arguments())
    result = finish(controller, key)
    assert result["state"] == "rejected" and result["status"] is None
    assert result["error_code"] == "DIRTY_REPOSITORY"
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_thread_launch_failure_retains_no_replay_tombstone(factory, monkeypatch, phase):
    controller = factory()

    def fail(*_args, **_kwargs):
        raise RuntimeError("PRIVATE")

    if phase == "construct":
        monkeypatch.setattr(desktop.threading, "Thread", fail)
    else:
        monkeypatch.setattr(desktop.threading.Thread, "start", fail)
    key = str(uuid4())
    value = controller.submit(controller.session_id, key, arguments())
    assert value["error_code"] == "DESKTOP_WORKER_NOT_STARTED"
    assert controller.submit(controller.session_id, key, arguments()) == value
    assert controller.close(wait=True)


@pytest.mark.parametrize("args, code", [
    ({"model": "gpt-6-astra"}, "INVALID_ARGUMENTS"),
    ({"action": "resume"}, "INVALID_ACTION"),
    ({"goal": "x" * (desktop.MAX_REQUEST_BYTES + 1)}, "REQUEST_TOO_LARGE"),
    ({"goal": "é" * 30000}, "REQUEST_TOO_LARGE"),
    ({"checks": [0] * 4097}, "REQUEST_TOO_COMPLEX"),
    ({"runtime_timeout_seconds": float("nan")}, "INVALID_ARGUMENTS"),
    ({"runtime_timeout_seconds": 2**100}, "INVALID_ARGUMENTS"),
])
def test_invalid_requests_do_not_consume_slots(factory, args, code):
    controller = factory()
    with pytest.raises(desktop.DesktopError, match=code):
        controller.submit(controller.session_id, str(uuid4()), args)
    assert controller.poll(controller.session_id)["events"] == []


def test_cyclic_request_is_rejected_before_json_encoding(factory):
    controller = factory()
    args = {"checks": []}
    args["checks"].append(args)
    with pytest.raises(desktop.DesktopError, match="REQUEST_TOO_COMPLEX"):
        controller.submit(controller.session_id, str(uuid4()), args)


@pytest.mark.parametrize("method, kwargs", [
    ("poll", {"after": True}), ("poll", {"after": -1}), ("poll", {"after": 1}),
    ("poll", {"limit": 0}), ("poll", {"limit": 257}),
    ("snapshot", {"request_id": "invalid"}),
    ("snapshot", {"request_id": "e14d2f6f-c1cc-40e8-a1b3-d302b1446bf1"}),
])
def test_invalid_reads_fail_closed(factory, method, kwargs):
    controller = factory()
    with pytest.raises(desktop.DesktopError):
        getattr(controller, method)(controller.session_id, **kwargs)


def test_factory_is_import_light_and_does_not_register_tools(monkeypatch):
    package = sys.modules["prime_rlm_pkg"]

    def forbidden(*_args, **_kwargs):
        pytest.fail("construction started a thread")

    monkeypatch.setattr(desktop.threading, "Thread", forbidden)
    controller = package.create_desktop_controller(object())
    assert controller.capabilities()["max_active_runs"] == 1
    assert controller.capabilities()["cancel"] is False
    assert controller.capabilities()["persistent_sessions"] is False
    assert controller.close(wait=True)


def test_renderer_can_rediscover_runs_with_bounded_history_pages(factory):
    controller = factory()
    ids = [str(uuid4()) for _ in range(3)]
    for key in ids:
        controller.submit(controller.session_id, key, arguments())
        finish(controller, key)
    page = controller.list_runs(controller.session_id, limit=2)
    assert [run["request_id"] for run in page["runs"]] == ids[:2]
    assert page["has_more"] and page["next_offset"] == 2
    last = controller.list_runs(controller.session_id, offset=page["next_offset"], limit=2)
    assert [run["request_id"] for run in last["runs"]] == ids[2:]
    assert not last["has_more"]
    with pytest.raises(desktop.DesktopError):
        controller.list_runs(controller.session_id, offset=4)


def test_shared_nested_request_cannot_expand_unbounded_pending_work(factory):
    leaf = [None] * 1000
    shared = [leaf] * 1000
    controller = factory()
    with pytest.raises(desktop.DesktopError, match="REQUEST_TOO_COMPLEX"):
        controller.submit(controller.session_id, str(uuid4()), {**arguments(), "checks": shared})
    assert controller.list_runs(controller.session_id)["runs"] == []


@pytest.mark.parametrize("raw", [
    '{"status":"VERIFIED","status":"FAILED"}',
    '{"status":"FAILED","value":1e400}',
    '{"status":"FAILED","value":NaN}',
])
def test_nonstandard_result_json_fails_closed(factory, raw):
    controller = factory(lambda _args, _ctx: raw)
    key = str(uuid4())
    controller.submit(controller.session_id, key, arguments())
    assert finish(controller, key)["status"] == "UNCERTAIN"
