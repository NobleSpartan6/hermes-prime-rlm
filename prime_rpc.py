"""Strict Prime Agent v0.8.1 RPC transport primitives."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from .schemas import (
    FINAL_TEXT_MAX_CHARS,
    RPC_KERNEL_HEALTH_CODE,
    RPC_KERNEL_HEALTH_MARKER,
)
from .validation import path_equivalent_for_header

RPC_MAX_RECORD_BYTES = 4 * 1024 * 1024
RPC_MAX_STREAM_BYTES = 64 * 1024 * 1024


class RpcProtocolError(ValueError):
    """Fail-closed RPC framing or lifecycle error with a stable code."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class RpcJsonlFramer:
    """Incremental LF-only JSONL decoder with record and stream bounds."""

    def __init__(self, *, max_record_bytes: int, max_total_bytes: int) -> None:
        if max_record_bytes < 1 or max_total_bytes < 1:
            raise ValueError("invalid RPC framer bounds")
        self.max_record_bytes = max_record_bytes
        self.max_total_bytes = max_total_bytes
        self._buffer = bytearray()
        self._total_bytes = 0
        self._finished = False

    def feed(self, chunk: bytes) -> list[dict]:
        if self._finished:
            raise RpcProtocolError("RPC_FRAMER_CLOSED", "cannot feed a finished RPC framer.")
        if not isinstance(chunk, bytes):
            raise TypeError("RPC chunks must be bytes")
        self._total_bytes += len(chunk)
        if self._total_bytes > self.max_total_bytes:
            raise RpcProtocolError(
                "RPC_STREAM_TOO_LARGE", "Prime RPC stdout exceeded its total byte bound."
            )
        self._buffer.extend(chunk)
        records: list[dict] = []
        while True:
            try:
                newline = self._buffer.index(0x0A)
            except ValueError:
                break
            raw = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw.strip():
                continue
            records.append(self._decode_record(raw))
        if len(self._buffer) > self.max_record_bytes:
            raise RpcProtocolError(
                "RPC_RECORD_TOO_LARGE", "Prime RPC record exceeded its byte bound."
            )
        return records

    def finish(self) -> list[dict]:
        if self._finished:
            return []
        self._finished = True
        if self._buffer.strip():
            if len(self._buffer) > self.max_record_bytes:
                raise RpcProtocolError(
                    "RPC_RECORD_TOO_LARGE", "Prime RPC record exceeded its byte bound."
                )
            raise RpcProtocolError(
                "RPC_PREMATURE_EOF", "Prime RPC stdout ended before an LF record boundary."
            )
        self._buffer.clear()
        return []

    def _decode_record(self, raw: bytes) -> dict:
        if len(raw) > self.max_record_bytes:
            raise RpcProtocolError(
                "RPC_RECORD_TOO_LARGE", "Prime RPC record exceeded its byte bound."
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RpcProtocolError("RPC_NOT_UTF8", "Prime RPC record is not UTF-8.") from exc
        try:
            value = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
            _reject_nonfinite_numbers(value)
        except RpcProtocolError:
            raise
        except ValueError as exc:
            raise RpcProtocolError(
                "RPC_MALFORMED_JSON", "Prime RPC record is not valid JSON."
            ) from exc
        if not isinstance(value, dict):
            raise RpcProtocolError(
                "RPC_NON_OBJECT_RECORD", "Prime RPC record must be a JSON object."
            )
        return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise RpcProtocolError(
                "RPC_DUPLICATE_JSON_KEY", f"Prime RPC record repeated JSON key {key!r}."
            )
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise RpcProtocolError(
        "RPC_NONSTANDARD_JSON", f"Prime RPC record used nonstandard JSON number {value}."
    )


def _reject_nonfinite_numbers(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise RpcProtocolError(
            "RPC_NONSTANDARD_JSON", "Prime RPC record contained a non-finite number."
        )
    if isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _reject_nonfinite_numbers(item)


@dataclass(frozen=True)
class RpcLifecycleResult:
    session_id: str
    model: dict
    final_text: str
    stats: dict
    event_count: int


class RpcLifecycleValidator:
    """Validate the correlated admission, run, and terminal RPC transcript."""

    def __init__(self, *, candidate_path: str) -> None:
        self.candidate_path = candidate_path
        self._responses: dict[str, dict] = {}
        self._session_id: str | None = None
        self._model: dict | None = None
        self._available_models: list[dict] | None = None
        self._prompt_accepted = False
        self._agent_starts = 0
        self._agent_ends = 0
        self._event_count = 0
        self._final_text = ""
        self._kernel_health_call_id: str | None = None
        self._kernel_health_proven = False
        self._stats: dict | None = None
        self._final_state: dict | None = None

    def consume(self, record: dict) -> None:
        if record.get("type") == "response":
            self._consume_response(record)
            return
        self._consume_event(record)

    def _consume_response(self, response: dict) -> None:
        response_id = response.get("id")
        if not isinstance(response_id, str) or not response_id:
            raise RpcProtocolError(
                "RPC_UNCORRELATED_RESPONSE", "Prime RPC response omitted its request id."
            )
        if response_id in self._responses:
            raise RpcProtocolError(
                "RPC_DUPLICATE_RESPONSE", f"duplicate Prime RPC response id: {response_id}."
            )
        if response_id not in {
            "handshake-state",
            "disable-retry",
            "available-models",
            "prompt",
            "stats",
            "final-state",
        }:
            raise RpcProtocolError(
                "RPC_UNEXPECTED_RESPONSE",
                f"Prime emitted an unexpected correlated response: {response_id}.",
            )
        response_order = (
            "handshake-state",
            "disable-retry",
            "available-models",
            "prompt",
            "stats",
            "final-state",
        )
        expected_response = response_order[len(self._responses)]
        if response_id != expected_response:
            raise RpcProtocolError(
                "RPC_RESPONSE_OUT_OF_ORDER",
                f"expected Prime response {expected_response}, got {response_id}.",
            )
        expected_command = {
            "handshake-state": "get_state",
            "disable-retry": "set_auto_retry",
            "available-models": "get_available_models",
            "prompt": "prompt",
            "stats": "get_session_stats",
            "final-state": "get_state",
        }[response_id]
        if response.get("command") != expected_command:
            raise RpcProtocolError(
                "RPC_RESPONSE_COMMAND_MISMATCH",
                f"Prime response {response_id} did not match {expected_command}.",
            )
        if response_id in {"stats", "final-state"} and self._agent_ends != 1:
            raise RpcProtocolError(
                "RPC_TERMINAL_RESPONSE_BEFORE_AGENT_END",
                "Prime emitted terminal observation before agent_end.",
            )
        self._responses[response_id] = response
        success = response.get("success") is True
        if response_id == "handshake-state":
            if not success:
                raise RpcProtocolError("RPC_STATE_HANDSHAKE_FAILED", "get_state failed.")
            self._accept_state(response.get("data"), final=False)
        elif response_id == "disable-retry":
            if not success or response.get("command") != "set_auto_retry":
                raise RpcProtocolError(
                    "RPC_AUTO_RETRY_DISABLE_FAILED", "Prime auto-retry was not disabled."
                )
        elif response_id == "available-models":
            data = response.get("data")
            models = data.get("models") if isinstance(data, dict) else None
            if not success or not isinstance(models, list) or not all(
                isinstance(item, dict) for item in models
            ):
                raise RpcProtocolError(
                    "RPC_MODEL_CATALOG_FAILED", "Prime model catalog response was invalid."
                )
            self._available_models = models
        elif response_id == "prompt":
            if not success or response.get("command") != "prompt":
                raise RpcProtocolError("RPC_PROMPT_REJECTED", "Prime rejected the RPC prompt.")
            self._prompt_accepted = True
        elif response_id == "stats":
            if not success or not isinstance(response.get("data"), dict):
                raise RpcProtocolError("RPC_STATS_INVALID", "Prime stats response was invalid.")
            self._stats = response["data"]
        elif response_id == "final-state":
            if not success:
                raise RpcProtocolError("RPC_FINAL_STATE_INVALID", "final get_state failed.")
            self._accept_state(response.get("data"), final=True)

    def _accept_state(self, data: object, *, final: bool) -> None:
        if not isinstance(data, dict):
            raise RpcProtocolError("RPC_STATE_INVALID", "Prime state response was not an object.")
        cwd = data.get("cwd")
        if cwd is not None and (
            not isinstance(cwd, str)
            or not path_equivalent_for_header(cwd, self.candidate_path)
        ):
            raise RpcProtocolError(
                "RPC_CWD_MISMATCH", "Prime RPC state cwd did not match the candidate."
            )
        session_id = data.get("sessionId")
        model = data.get("model")
        if not isinstance(session_id, str) or not session_id or not isinstance(model, dict):
            raise RpcProtocolError(
                "RPC_STATE_INVALID", "Prime state omitted session or model identity."
            )
        if not all(
            isinstance(part, str) and bool(part.strip()) for part in _model_identity(model)
        ):
            raise RpcProtocolError(
                "RPC_MODEL_IDENTITY_INVALID",
                "Prime state omitted provider or model id.",
            )
        if self._session_id is not None and self._session_id != session_id:
            raise RpcProtocolError(
                "RPC_SESSION_CHANGED", "Prime session identity changed during one RPC run."
            )
        if self._model is not None and _model_identity(self._model) != _model_identity(model):
            raise RpcProtocolError(
                "RPC_MODEL_CHANGED", "Prime model identity changed during one RPC run."
            )
        self._session_id = session_id
        self._model = model
        if final:
            if data.get("isStreaming") is not False:
                raise RpcProtocolError(
                    "RPC_FINAL_STATE_BUSY", "Prime remained streaming after agent_end."
                )
            self._final_state = data

    def _consume_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type in {"auto_retry_start", "auto_retry_end"}:
            raise RpcProtocolError(
                "RPC_AUTO_RETRY_OBSERVED",
                "Prime emitted auto-retry events after retries were disabled.",
            )
        if self._agent_ends:
            raise RpcProtocolError(
                "RPC_EVENT_AFTER_AGENT_END",
                "Prime emitted a session event after the terminal agent_end.",
            )
        if event_type not in {
            "agent_start",
            "agent_end",
            "turn_start",
            "turn_end",
            "message_start",
            "message_update",
            "message_end",
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
            "session_action_update",
            "compaction_start",
            "compaction_end",
            "extension_error",
            "ipython_sent_agent_message",
            "session_info_changed",
            "thinking_level_changed",
            "service_tier_changed",
            "auth_stale",
            "rlm_child_update",
            "recap_update",
            "goal_update",
            "bash_start",
            "bash_output",
            "bash_end",
            "refine_complete",
            "refine_failed",
        }:
            raise RpcProtocolError(
                "RPC_UNKNOWN_RECORD", f"unknown Prime RPC record type: {event_type!r}."
            )
        if not self._prompt_accepted:
            raise RpcProtocolError(
                "RPC_EVENT_BEFORE_PROMPT_ACCEPTED",
                "Prime emitted agent events before acknowledging the prompt.",
            )
        self._event_count += 1
        if event_type == "agent_start":
            self._agent_starts += 1
            if self._agent_starts != 1:
                raise RpcProtocolError(
                    "RPC_DUPLICATE_AGENT_START", "Prime emitted duplicate agent_start."
                )
        elif event_type == "tool_execution_start":
            tool_call_id = event.get("toolCallId")
            tool_name = event.get("toolName")
            args = event.get("args")
            code = args.get("code") if isinstance(args, dict) else None
            if self._kernel_health_call_id is None:
                if (
                    self._agent_starts != 1
                    or not isinstance(tool_call_id, str)
                    or not tool_call_id
                    or tool_name != "ipython"
                    or not isinstance(code, str)
                    or code.strip() != RPC_KERNEL_HEALTH_CODE
                ):
                    raise RpcProtocolError(
                        "RPC_KERNEL_HEALTH_REQUIRED",
                        "the first tool must be the exact IPython runtime health probe.",
                    )
                self._kernel_health_call_id = tool_call_id
            elif not self._kernel_health_proven:
                raise RpcProtocolError(
                    "RPC_KERNEL_HEALTH_REQUIRED",
                    "another tool started before IPython runtime health was proven.",
                )
        elif event_type == "tool_execution_end" and not self._kernel_health_proven:
            if self._kernel_health_call_id is None:
                raise RpcProtocolError(
                    "RPC_KERNEL_HEALTH_REQUIRED",
                    "a tool ended before the IPython runtime health probe started.",
                )
            if (
                event.get("toolCallId") != self._kernel_health_call_id
                or event.get("toolName") != "ipython"
                or event.get("isError") is not False
                or not _contains_text(event.get("result"), RPC_KERNEL_HEALTH_MARKER)
            ):
                raise RpcProtocolError(
                    "RPC_KERNEL_HEALTH_FAILED",
                    "the IPython runtime health probe did not complete successfully.",
                )
            self._kernel_health_proven = True
        elif event_type == "agent_end":
            if self._agent_starts != 1:
                raise RpcProtocolError(
                    "RPC_AGENT_END_BEFORE_START", "Prime emitted agent_end before agent_start."
                )
            if not self._kernel_health_proven:
                raise RpcProtocolError(
                    "RPC_KERNEL_HEALTH_NOT_PROVEN",
                    "Prime ended before IPython and the rlm runtime were proven healthy.",
                )
            self._agent_ends += 1
            if self._agent_ends != 1:
                raise RpcProtocolError(
                    "RPC_DUPLICATE_AGENT_END", "Prime emitted duplicate agent_end."
                )
        elif event_type == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("stopReason") in {
                "error",
                "aborted",
            }:
                raise RpcProtocolError(
                    "RPC_AGENT_ERROR",
                    str(message.get("errorMessage") or "Prime assistant message failed."),
                )
            text = _assistant_text(message)
            if text:
                self._final_text = _bound_final_text(text)

    def finish(self) -> RpcLifecycleResult:
        required = {
            "handshake-state": "RPC_STATE_HANDSHAKE_MISSING",
            "disable-retry": "RPC_AUTO_RETRY_DISABLE_FAILED",
            "available-models": "RPC_MODEL_CATALOG_MISSING",
            "prompt": "RPC_PROMPT_RESPONSE_MISSING",
        }
        for response_id, error_code in required.items():
            if response_id not in self._responses:
                raise RpcProtocolError(error_code, f"missing response: {response_id}.")
        if self._agent_starts != 1:
            raise RpcProtocolError("RPC_MISSING_AGENT_START", "agent_start was not observed.")
        if self._agent_ends != 1:
            raise RpcProtocolError("RPC_MISSING_AGENT_END", "agent_end was not observed.")
        if not self._kernel_health_proven:
            raise RpcProtocolError(
                "RPC_KERNEL_HEALTH_NOT_PROVEN",
                "IPython and the rlm runtime were not proven healthy.",
            )
        if self._stats is None:
            raise RpcProtocolError("RPC_STATS_MISSING", "session stats were not observed.")
        if self._final_state is None:
            raise RpcProtocolError("RPC_FINAL_STATE_MISSING", "final state was not observed.")
        if self._session_id is None or self._model is None:
            raise RpcProtocolError("RPC_STATE_INVALID", "session identity was not established.")
        if self._available_models is None:
            raise RpcProtocolError(
                "RPC_MODEL_CATALOG_MISSING", "Prime model catalog was not established."
            )
        identity = (self._model.get("provider"), self._model.get("id"))
        if identity not in {
            (item.get("provider"), item.get("id")) for item in self._available_models
        }:
            raise RpcProtocolError(
                "RPC_MODEL_UNAVAILABLE", "active Prime model was absent from its model catalog."
            )
        return RpcLifecycleResult(
            session_id=self._session_id,
            model=dict(self._model),
            final_text=self._final_text,
            stats=dict(self._stats),
            event_count=self._event_count,
        )


def _assistant_text(message: object) -> str:
    if not isinstance(message, dict) or message.get("role") not in {None, "assistant"}:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _bound_final_text(text: str) -> str:
    if len(text) > FINAL_TEXT_MAX_CHARS:
        return text[:FINAL_TEXT_MAX_CHARS] + "…[truncated]"
    return text


def _model_identity(model: dict) -> tuple[object, object]:
    return model.get("provider"), model.get("id")


def _contains_text(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return expected in value
    if isinstance(value, dict):
        return any(_contains_text(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_text(item, expected) for item in value)
    return False


def build_rpc_argv(command_prefix: list[str], candidate_path: str) -> list[str]:
    from .prime_rpc_process import build_rpc_argv as _build

    return _build(command_prefix, candidate_path)


def probe_prime_rpc(
    command_prefix: list[str], candidate_path: str, timeout_seconds: int, *, env=None
):
    from .prime_rpc_process import probe_prime_rpc as _probe

    return _probe(command_prefix, candidate_path, timeout_seconds, env=env)


def run_prime_rpc(command_prefix: list[str], layout, timeout_seconds: int, *, env=None):
    from .prime_rpc_process import run_prime_rpc as _run

    return _run(command_prefix, layout, timeout_seconds, env=env)


__all__ = [
    "RpcProtocolError",
    "RpcJsonlFramer",
    "RpcLifecycleResult",
    "RpcLifecycleValidator",
    "build_rpc_argv",
    "probe_prime_rpc",
    "run_prime_rpc",
]
