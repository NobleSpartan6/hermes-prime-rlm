"""Private-pipe framing, backpressure, no-replay and data minimization."""

from __future__ import annotations

import importlib
import io
import json
import threading
from uuid import uuid4

import pytest

from tests.test_desktop import arguments, desktop, finish, outcome

transport = importlib.import_module("prime_rlm_pkg.desktop_transport")


def frame(method, params=None, identifier=1):
    return json.dumps({
        "jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {},
    }).encode() + b"\n"


@pytest.fixture()
def controller():
    value = desktop.DesktopRunController(object(), runner=lambda _args, _ctx: outcome())
    yield value
    value.close(wait=True)


def test_round_trip_and_reattach_do_not_reinvoke(controller):
    capabilities = json.loads(transport.dispatch(controller, frame("capabilities")))["result"]
    params = {"session_id": capabilities["session_id"], "request_id": str(uuid4()), "args": arguments()}
    accepted = json.loads(transport.dispatch(controller, frame("submit", params)))
    assert accepted["result"]["state"] == "accepted"
    finish(controller, params["request_id"])
    repeated = json.loads(transport.dispatch(controller, frame("submit", params, identifier=2)))
    assert repeated["result"]["verified"] is True
    history = json.loads(transport.dispatch(controller, frame("list_runs", {"session_id": controller.session_id})))
    assert len(history["result"]["runs"]) == 1
    events = json.loads(transport.dispatch(controller, frame("poll", {"session_id": controller.session_id})))
    assert len(events["result"]["events"]) == 3


@pytest.mark.parametrize("method", ["cancel", "resume", "set_config", "review_references", "__dict__"])
def test_only_explicit_renderer_methods_are_exposed(controller, method):
    result = json.loads(transport.dispatch(controller, frame(method)))
    assert result["error"]["message"] == "UNKNOWN_METHOD"
    assert controller.list_runs(controller.session_id)["runs"] == []


@pytest.mark.parametrize("payload", [
    b"no JSON\n", b"[]\n", b"\xff\n", b'{"jsonrpc":"2.0","jsonrpc":"2.0"}\n',
    b'{"id":NaN}\n', b"[" * 2000 + b"0" + b"]" * 2000,
    frame("capabilities", identifier=True), frame("capabilities", identifier="SECRET"),
    frame("capabilities", identifier=2**53), frame("capabilities", {"extra": "SECRET"}),
    b"x" * (transport.MAX_FRAME_BYTES + 1),
])
def test_malformed_frames_cannot_admit_work_or_echo_data(controller, payload):
    reply = transport.dispatch(controller, payload)
    result = json.loads(reply)
    assert "error" in result and b"SECRET" not in reply
    assert controller.list_runs(controller.session_id)["runs"] == []
    assert reply.endswith(b"\n")
    assert len(reply) < 256


def test_oversize_line_terminates_connection_without_unbounded_drain(controller):
    source = io.BytesIO(b"x" * (transport.MAX_FRAME_BYTES * 3))
    sink = io.BytesIO()
    transport.serve(controller, source, sink)
    assert source.tell() == transport.MAX_FRAME_BYTES + 1
    assert json.loads(sink.getvalue())["error"]["message"] == "FRAME_TOO_LARGE"
    with pytest.raises(desktop.DesktopError, match="CONTROLLER_CLOSED"):
        controller.submit(controller.session_id, str(uuid4()), arguments())


def test_partial_final_frame_is_never_executed(controller):
    params = {"session_id": controller.session_id, "request_id": str(uuid4()), "args": arguments()}
    sink = io.BytesIO()
    transport.serve(controller, io.BytesIO(frame("submit", params).rstrip(b"\n")), sink)
    assert json.loads(sink.getvalue())["error"]["message"] == "INCOMPLETE_FRAME"
    assert controller.list_runs(controller.session_id)["runs"] == []


def test_eof_waits_for_admitted_run_without_replay_or_cancellation():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def run(_args, _ctx):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return outcome()

    controller = desktop.DesktopRunController(object(), runner=run)
    key = str(uuid4())
    source = io.BytesIO(frame("submit", {
        "session_id": controller.session_id, "request_id": key, "args": arguments(),
    }))
    sink = io.BytesIO()
    server = threading.Thread(target=transport.serve, args=(controller, source, sink))
    try:
        server.start()
        assert entered.wait(2)
        assert server.is_alive()
        assert controller.snapshot(controller.session_id, key)["state"] == "running"
    finally:
        release.set()
        server.join(5)
        controller.close(wait=True)
    assert not server.is_alive()
    assert calls == [1]
    assert finish(controller, key)["status"] == "VERIFIED"


def test_broken_output_pipe_never_replays_admitted_work():
    calls = []

    def run(_args, _ctx):
        calls.append(1)
        return outcome()

    class BrokenSink:
        def write(self, _payload):
            raise BrokenPipeError

    controller = desktop.DesktopRunController(object(), runner=run)
    key = str(uuid4())
    source = io.BytesIO(frame("submit", {
        "session_id": controller.session_id, "request_id": key, "args": arguments(),
    }))
    transport.serve(controller, source, BrokenSink())
    assert calls == [1]
    assert finish(controller, key)["verified"]
