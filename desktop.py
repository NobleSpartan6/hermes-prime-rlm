"""Operator-only desktop adapter for the existing one-shot Hermes tool.

No UI toolkit, server, provider client, process policy, or extra model tool lives
here. A trusted desktop backend owns ONE controller per profile and calls the
existing handler off its event loop. This is not a daemon or a security boundary.
See docs/desktop-rlm-v1.md for lifetime, approval, and reconnect requirements.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import islice
from uuid import UUID, uuid4

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESULT_BYTES = 256 * 1024
_ARGUMENTS = frozenset({"action", "goal", "repository_path", "checks", "runtime_timeout_seconds"})
_STATUSES = frozenset({
    "VERIFIED", "COMPLETED_UNVERIFIED", "FAILED_VERIFICATION", "FAILED", "UNCERTAIN",
})
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class DesktopError(ValueError):
    """Stable error code only: do not echo goals, arguments, paths, or exceptions."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _integer(value: object, low: int, high: int, code: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise DesktopError(code)
    return value


def _uuid(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise DesktopError("INVALID_REQUEST_ID")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise DesktopError("INVALID_REQUEST_ID") from exc
    return value


def _request(args: object) -> tuple[dict, str]:
    """Bound traversal BEFORE encoding; deep-copy without retaining caller objects."""
    if type(args) is not dict or args.keys() - _ARGUMENTS:
        raise DesktopError("INVALID_ARGUMENTS")
    # Iterative traversal also rejects cycles, excessive nesting, and non-JSON
    # objects without invoking user-defined deepcopy/JSON serialization methods.
    pending = [(args, 0)]
    count = 0
    characters = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > 4096 or depth > 12:
            raise DesktopError("REQUEST_TOO_COMPLEX")
        if type(value) is dict:
            if count + len(pending) + 2 * len(value) > 4096:
                raise DesktopError("REQUEST_TOO_COMPLEX")
            for key, item in value.items():
                if type(key) is not str:
                    raise DesktopError("INVALID_ARGUMENTS")
                pending.extend(((key, depth + 1), (item, depth + 1)))
        elif type(value) is list:
            if count + len(pending) + len(value) > 4096:
                raise DesktopError("REQUEST_TOO_COMPLEX")
            pending.extend((item, depth + 1) for item in value)
        elif type(value) is str:
            characters += len(value)
        elif type(value) is int:
            if value.bit_length() > 64:
                raise DesktopError("INVALID_ARGUMENTS")
        elif value is not None and type(value) is not bool:
            raise DesktopError("INVALID_ARGUMENTS")
        if characters > MAX_REQUEST_BYTES:
            raise DesktopError("REQUEST_TOO_LARGE")
    normalized = dict(args)
    normalized.setdefault("action", "run")
    if normalized["action"] != "run":
        raise DesktopError("INVALID_ACTION")
    raw = json.dumps(normalized, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    if len(raw) > MAX_REQUEST_BYTES:
        raise DesktopError("REQUEST_TOO_LARGE")
    return json.loads(raw), hashlib.sha256(raw.encode("ascii")).hexdigest()


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _unknown(code: str) -> dict:
    return {
        "status": "UNCERTAIN", "verified": False, "completed": False,
        "candidate_stability": "unknown", "error_code": code,
        "checks": {"total": 0, "passed": 0},
    }


def _project(raw: object) -> tuple[dict, dict]:
    """Project host results, never model text, into a small renderer-safe view.

    This does not revalidate artifacts or establish authenticity. Review paths
    stay in a separate backend-only object. Inconsistent results fail closed.
    """
    if type(raw) is not str or len(raw) > MAX_RESULT_BYTES:
        return _unknown("DESKTOP_INVALID_RESULT"), {}
    if len(raw.encode("utf-8", errors="surrogatepass")) > MAX_RESULT_BYTES:
        return _unknown("DESKTOP_INVALID_RESULT"), {}
    try:
        result = json.loads(
            raw, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float,
        )
    except (ValueError, RecursionError):
        return _unknown("DESKTOP_INVALID_RESULT"), {}
    if not isinstance(result, dict):
        return _unknown("DESKTOP_INVALID_RESULT"), {}
    status = result.get("status")
    if status is None and result.get("stage") == "validation" and result.get("ok") is False:
        code = result.get("error_code")
        return {
            "status": None, "verified": False, "completed": False,
            "error_code": code if isinstance(code, str) and _CODE.fullmatch(code)
            else "DESKTOP_ADMISSION_REJECTED",
            "checks": {"total": 0, "passed": 0},
        }, {}
    if not isinstance(status, str) or status not in _STATUSES:
        return _unknown("DESKTOP_INVALID_RESULT"), {}
    checks = result.get("checks", [])
    if not isinstance(checks, list) or len(checks) > 256:
        return _unknown("DESKTOP_INVALID_RESULT"), {}
    passed = sum(
        isinstance(row, dict) and row.get("status") == "passed"
        and type(row.get("exit_code")) is int and row["exit_code"] == 0
        for row in checks
    )
    verified = status == "VERIFIED"
    if verified and not (
        result.get("ok") is True and result.get("verified") is True
        and checks and passed == len(checks)
    ):
        return _unknown("DESKTOP_INVALID_RESULT"), {}
    projection = {
        "status": status, "verified": verified,
        "completed": status in ("VERIFIED", "COMPLETED_UNVERIFIED"),
        "candidate_stability": "unknown" if status == "UNCERTAIN" else "UNQUIESCED",
        "checks": {"total": len(checks), "passed": passed},
    }
    code = result.get("error_code")
    if isinstance(code, str) and _CODE.fullmatch(code):
        projection["error_code"] = code
    try:
        projection["run_id"] = _uuid(result.get("run_id"))
    except DesktopError:
        pass
    digest = result.get("receipt_sha256")
    if isinstance(digest, str) and _DIGEST.fullmatch(digest):
        projection["receipt_sha256"] = digest
    references = {
        key: result[key] for key in ("candidate_path", "receipt_path")
        if isinstance(result.get(key), str) and 0 < len(result[key]) <= 4096
        and "\x00" not in result[key]
    }
    return projection, references


def _invoke(args: dict, ctx: object) -> str:
    # Lazy import: constructing a controller must not probe Prime or load Hermes.
    from .tools import handle_prime_agent

    return handle_prime_agent(args, _ctx=ctx)


@dataclass
class _Job:
    fingerprint: str
    started: float = field(default_factory=time.monotonic)
    state: str = "accepted"
    finished: float | None = None
    sequence: int = 0
    result: dict = field(default_factory=dict)
    references: dict = field(default_factory=dict)


class DesktopRunController:
    """One active run, no run queue, no retry, bounded in-memory history.

    Keep this object in the trusted backend, NOT in a renderer/web page or a
    model tool registry. Polling never joins a worker or reads run artifacts.
    ``runner`` is a test seam; production must use the default Hermes handler.
    """

    def __init__(
        self, ctx: object, *, history_limit: int = 64, event_capacity: int = 128,
        runner: Callable[[dict, object], str] | None = None,
    ) -> None:
        self._history_limit = _integer(history_limit, 1, 1024, "INVALID_HISTORY_LIMIT")
        capacity = _integer(event_capacity, 1, 4096, "INVALID_EVENT_CAPACITY")
        self.session_id = str(uuid4())
        self._ctx = ctx
        self._runner = runner if runner is not None else _invoke
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._events: deque[dict] = deque(maxlen=capacity)
        self._sequence = 0
        self._active: str | None = None
        self._worker: threading.Thread | None = None
        self._closed = False

    def capabilities(self) -> dict:
        return {
            "protocol_version": PROTOCOL_VERSION, "session_id": self.session_id,
            "max_active_runs": 1, "queue_capacity": 0,
            "history_limit": self._history_limit, "event_capacity": self._events.maxlen,
            "max_request_bytes": MAX_REQUEST_BYTES,
            "automatic_retry": False, "cancel": False, "resume": False,
            "persistent_sessions": False, "agent_tree": False,
            "progress": ["accepted", "running", "finished", "rejected"],
        }

    def _session(self, session_id: str) -> None:
        if session_id != self.session_id:
            raise DesktopError("SESSION_CHANGED_DO_NOT_REPLAY")

    def submit(self, session_id: str, request_id: str, args: dict) -> dict:
        """Called after host approval; duplicate identical submissions reattach.

        A stale session token is rejected even if a new backend has free slots.
        No work is ever queued while another run is active. IDs are retained
        until controller destruction, not silently evicted for reuse.
        """
        self._session(session_id)
        request_id = _uuid(request_id)
        arguments, fingerprint = _request(args)
        with self._lock:
            previous = self._jobs.get(request_id)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise DesktopError("REQUEST_ID_CONFLICT")
                return self._snapshot(request_id)
            if self._closed:
                raise DesktopError("CONTROLLER_CLOSED")
            if self._active is not None:
                raise DesktopError("DESKTOP_BUSY")
            if len(self._jobs) >= self._history_limit:
                raise DesktopError("HISTORY_FULL")
            self._jobs[request_id] = _Job(fingerprint=fingerprint)
            self._active = request_id
            self._emit(request_id)
            # Non-daemon: normal backend shutdown must not abandon its worker.
            # Closing a renderer is safe only while its backend stays alive.
            try:
                worker = threading.Thread(
                    target=self._execute, args=(request_id, arguments),
                    name="hermes-prime-desktop", daemon=False,
                )
                self._worker = worker
                worker.start()
            except Exception:
                self._worker = None
                self._finish(request_id, _unknown("DESKTOP_WORKER_NOT_STARTED"), {})
            return self._snapshot(request_id)

    def _execute(self, request_id: str, args: dict) -> None:
        with self._lock:
            self._jobs[request_id].state = "running"
            self._emit(request_id)
        try:
            projection, references = _project(self._runner(args, self._ctx))
        except BaseException:  # Worker boundary: even SystemExit must leave an outcome.
            projection, references = _unknown("DESKTOP_WORKER_FAILED"), {}
        with self._lock:
            self._finish(request_id, projection, references)

    def _finish(self, request_id: str, projection: dict, references: dict) -> None:
        job = self._jobs[request_id]
        job.state = "rejected" if projection.get("status") is None else "finished"
        job.finished = time.monotonic()
        job.result = projection
        job.references = references
        self._active = None
        self._emit(request_id)

    def _snapshot(self, request_id: str) -> dict:
        job = self._jobs.get(request_id)
        if job is None:
            raise DesktopError("UNKNOWN_REQUEST")
        return {
            "protocol_version": PROTOCOL_VERSION, "session_id": self.session_id,
            "request_id": request_id, "sequence": job.sequence, "state": job.state,
            "elapsed_ms": round(((job.finished or time.monotonic()) - job.started) * 1000, 3),
            "status": None, "verified": False, "completed": False,
            "automatic_retry_allowed": False,
            "verification_scope": "recorded_host_checks_only",
            "acceptance_status": "PENDING", "authenticity": "UNSIGNED",
            "artifact_integrity": "NOT_REVALIDATED_BY_DESKTOP",
            **copy.deepcopy(job.result),
        }

    def _emit(self, request_id: str) -> None:
        self._sequence += 1
        self._jobs[request_id].sequence = self._sequence
        self._events.append(self._snapshot(request_id))

    def snapshot(self, session_id: str, request_id: str) -> dict:
        self._session(session_id)
        request_id = _uuid(request_id)
        with self._lock:
            return self._snapshot(request_id)

    def poll(self, session_id: str, *, after: int = 0, limit: int = 64) -> dict:
        """Read a bounded event page. A gap requires refreshing known snapshots."""
        self._session(session_id)
        limit = _integer(limit, 1, 256, "INVALID_EVENT_LIMIT")
        with self._lock:
            after = _integer(after, 0, self._sequence, "INVALID_CURSOR")
            oldest = self._events[0]["sequence"] if self._events else self._sequence + 1
            events = []
            for event in self._events:
                if event["sequence"] > after:
                    events.append(copy.deepcopy(event))
                    if len(events) == limit:
                        break
            cursor = events[-1]["sequence"] if events else after
            return {
                "protocol_version": PROTOCOL_VERSION, "session_id": self.session_id,
                "events": events, "next_cursor": cursor,
                "gap": after < oldest - 1, "has_more": cursor < self._sequence,
            }

    def list_runs(self, session_id: str, *, offset: int = 0, limit: int = 32) -> dict:
        """Rediscover this backend's runs after a renderer reload, without disk I/O."""
        self._session(session_id)
        limit = _integer(limit, 1, 64, "INVALID_HISTORY_PAGE_LIMIT")
        with self._lock:
            offset = _integer(offset, 0, len(self._jobs), "INVALID_HISTORY_OFFSET")
            runs = [self._snapshot(key) for key in islice(self._jobs, offset, offset + limit)]
            next_offset = offset + len(runs)
            return {
                "protocol_version": PROTOCOL_VERSION, "session_id": self.session_id,
                "runs": runs, "next_offset": next_offset,
                "has_more": next_offset < len(self._jobs),
            }

    def review_references(self, session_id: str, request_id: str) -> dict:
        """Backend-only local references, NOT proof that files still match a receipt.

        Do not expose this method as a general renderer filesystem/open command.
        The host must review/validate destinations; never interpolate into a shell.
        """
        self._session(session_id)
        request_id = _uuid(request_id)
        with self._lock:
            if request_id not in self._jobs:
                raise DesktopError("UNKNOWN_REQUEST")
            return dict(self._jobs[request_id].references)

    def close(self, *, wait: bool = False) -> bool:
        """Refuse new submissions; NEVER cancel or replay admitted work.

        Returns whether work has finished. ``wait=True`` joins the worker and
        belongs only on a backend shutdown path, never the renderer event loop.
        """
        with self._lock:
            self._closed = True
            worker = self._worker
        if wait and worker is not None and worker is not threading.current_thread():
            worker.join()
        with self._lock:
            return self._active is None
