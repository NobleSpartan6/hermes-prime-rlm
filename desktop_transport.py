"""Bounded JSON-RPC/JSONL for a desktop host's PRIVATE parent-owned pipes.

Not an HTTP service and not a standalone launcher. The authenticated Hermes
host supplies the profile-scoped controller after enforcing its approvals.
Only this thread writes replies; the worker never writes protocol messages.
"""

from __future__ import annotations

import json
from typing import BinaryIO

from .desktop import (
    MAX_REQUEST_BYTES,
    DesktopError,
    DesktopRunController,
    _constant,
    _float,
    _object,
)

MAX_FRAME_BYTES = MAX_REQUEST_BYTES + 16 * 1024
MAX_REPLY_BYTES = 512 * 1024
_METHODS = {
    "capabilities": (frozenset(), frozenset()),
    "submit": (frozenset({"session_id", "request_id", "args"}), frozenset()),
    "snapshot": (frozenset({"session_id", "request_id"}), frozenset()),
    "poll": (frozenset({"session_id"}), frozenset({"after", "limit"})),
    "list_runs": (frozenset({"session_id"}), frozenset({"offset", "limit"})),
}


def _error(request_id: int | None, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def dispatch(controller: DesktopRunController, frame: bytes) -> bytes:
    """One finite request/reply; IDs are integers, notifications/batches refused."""
    request_id = None
    try:
        if not isinstance(frame, bytes) or len(frame) > MAX_FRAME_BYTES:
            raise DesktopError("FRAME_TOO_LARGE")
        try:
            envelope = json.loads(
                frame.decode("utf-8"), object_pairs_hook=_object,
                parse_constant=_constant, parse_float=_float,
            )
        except (ValueError, RecursionError):
            raise DesktopError("INVALID_JSON") from None
        if not isinstance(envelope, dict) or set(envelope) != {"jsonrpc", "id", "method", "params"}:
            raise DesktopError("INVALID_ENVELOPE")
        identifier = envelope["id"]
        if type(identifier) is not int or not 0 <= identifier <= 2**53 - 1:
            raise DesktopError("INVALID_RPC_ID")
        request_id = identifier
        if envelope["jsonrpc"] != "2.0":
            raise DesktopError("INVALID_ENVELOPE")
        method, params = envelope["method"], envelope["params"]
        if not isinstance(method, str) or method not in _METHODS:
            reply = _error(request_id, -32601, "UNKNOWN_METHOD")
        else:
            required, optional = _METHODS[method]
            if not isinstance(params, dict) or not required <= params.keys() <= required | optional:
                raise DesktopError("INVALID_PARAMS")
            # Fixed allowlist, not arbitrary getattr from renderer-controlled text.
            handlers = {
                "capabilities": controller.capabilities, "submit": controller.submit,
                "snapshot": controller.snapshot, "poll": controller.poll,
                "list_runs": controller.list_runs,
            }
            reply = {"jsonrpc": "2.0", "id": request_id, "result": handlers[method](**params)}
    except DesktopError as exc:
        reply = _error(request_id, -32000, exc.code)
    except Exception:
        # Do not leak the message: provider/runtime errors may contain secrets.
        reply = _error(request_id, -32603, "DESKTOP_INTERNAL_ERROR")
    encoded = json.dumps(reply, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_REPLY_BYTES:
        encoded = json.dumps(_error(request_id, -32603, "REPLY_TOO_LARGE")).encode("ascii")
    return encoded + b"\n"


def serve(controller: DesktopRunController, source: BinaryIO, sink: BinaryIO) -> None:
    """Serve inherited pipes; EOF/disconnect closes admission, never retries work.

    Oversized frames terminate this connection without draining an unbounded
    line. Graceful backend shutdown waits for already-admitted work. Do not call
    this blocking loop on a renderer/UI thread or expose it on a public socket.
    """
    try:
        while True:
            frame = source.readline(MAX_FRAME_BYTES + 1)
            if not frame:
                break
            # A partial final frame is not an execution request. This prevents
            # a disconnected renderer from accidentally admitting truncated work.
            if len(frame) > MAX_FRAME_BYTES:
                sink.write(dispatch(controller, frame))
                sink.flush()
                break
            if not frame.endswith(b"\n"):
                sink.write(json.dumps(_error(None, -32000, "INCOMPLETE_FRAME")).encode() + b"\n")
                sink.flush()
                break
            sink.write(dispatch(controller, frame))
            sink.flush()
    except (BrokenPipeError, ConnectionError):
        pass
    finally:
        controller.close(wait=True)
