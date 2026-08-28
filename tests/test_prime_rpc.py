"""Strict LF-only JSONL framing for Prime RPC stdout."""

from __future__ import annotations

import json

import pytest
from conftest import prime_rpc, schemas


def _line(value: dict, ending: bytes = b"\n") -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8") + ending


def test_fragmented_records_are_reassembled_without_generic_line_reader():
    framer = prime_rpc.RpcJsonlFramer(max_record_bytes=1024, max_total_bytes=4096)
    payload = _line({"type": "response", "id": "a", "data": "hello"}) + _line(
        {"type": "message_end", "message": {"content": "world"}}
    )

    records = []
    for byte in payload:
        records.extend(framer.feed(bytes([byte])))
    records.extend(framer.finish())

    assert [record["type"] for record in records] == ["response", "message_end"]


def test_crlf_is_accepted_but_unicode_line_separators_do_not_split_records():
    framer = prime_rpc.RpcJsonlFramer(max_record_bytes=1024, max_total_bytes=4096)
    text = "left\u2028middle\u2029right"
    records = framer.feed(_line({"type": "message_end", "text": text}, b"\r\n"))
    records.extend(framer.finish())

    assert records == [{"type": "message_end", "text": text}]


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        (b"{not-json}\n", "RPC_MALFORMED_JSON"),
        (b"[1,2]\n", "RPC_NON_OBJECT_RECORD"),
        (b'"unterminated', "RPC_PREMATURE_EOF"),
        (b"\xff\n", "RPC_NOT_UTF8"),
    ],
)
def test_bad_records_fail_closed(payload, error_code):
    framer = prime_rpc.RpcJsonlFramer(max_record_bytes=1024, max_total_bytes=4096)
    with pytest.raises(prime_rpc.RpcProtocolError) as exc:
        framer.feed(payload)
        framer.finish()
    assert exc.value.error_code == error_code


def test_per_record_and_total_bounds_are_independent():
    record_framer = prime_rpc.RpcJsonlFramer(max_record_bytes=16, max_total_bytes=4096)
    with pytest.raises(prime_rpc.RpcProtocolError) as record_exc:
        record_framer.feed(b'{"padding":"xxxxxxxx"}\n')
    assert record_exc.value.error_code == "RPC_RECORD_TOO_LARGE"

    total_framer = prime_rpc.RpcJsonlFramer(max_record_bytes=64, max_total_bytes=20)
    with pytest.raises(prime_rpc.RpcProtocolError) as total_exc:
        total_framer.feed(_line({"a": 1}) + _line({"b": 2}) + _line({"c": 3}))
    assert total_exc.value.error_code == "RPC_STREAM_TOO_LARGE"


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        (b'{"type":"agent_start","value":NaN}\n', "RPC_NONSTANDARD_JSON"),
        (b'{"type":"agent_start","value":1e400}\n', "RPC_NONSTANDARD_JSON"),
        (
            b'{"type":"agent_start","type":"agent_end"}\n',
            "RPC_DUPLICATE_JSON_KEY",
        ),
    ],
)
def test_nonstandard_numbers_and_duplicate_keys_fail_closed(payload, error_code):
    framer = prime_rpc.RpcJsonlFramer(max_record_bytes=1024, max_total_bytes=4096)

    with pytest.raises(prime_rpc.RpcProtocolError) as exc:
        framer.feed(payload)

    assert exc.value.error_code == error_code


def _valid_rpc_records(candidate: str) -> list[dict]:
    model = {"provider": "openrouter", "id": "test/model"}
    return [
        {
            "id": "handshake-state",
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {
                "sessionId": "prime-session-1",
                "isStreaming": False,
                "model": model,
            },
        },
        {
            "id": "disable-retry",
            "type": "response",
            "command": "set_auto_retry",
            "success": True,
        },
        {
            "id": "available-models",
            "type": "response",
            "command": "get_available_models",
            "success": True,
            "data": {"models": [model]},
        },
        {
            "id": "prompt",
            "type": "response",
            "command": "prompt",
            "success": True,
        },
        {"type": "agent_start"},
        {
            "type": "tool_execution_start",
            "toolCallId": "kernel-health-1",
            "toolName": "ipython",
            "args": {
                "code": "import rlm\nassert callable(rlm)\n'__HERMES_PRIME_KERNEL_HEALTH_V1__'"
            },
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "kernel-health-1",
            "toolName": "ipython",
            "result": "__HERMES_PRIME_KERNEL_HEALTH_V1__",
            "isError": False,
        },
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": "candidate complete"},
        },
        {
            "type": "agent_end",
            "messages": [{"role": "assistant", "content": "done"}],
        },
        {
            "id": "stats",
            "type": "response",
            "command": "get_session_stats",
            "success": True,
            "data": {"tokens": {"total": 123}, "cost": 0.01},
        },
        {
            "id": "final-state",
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {
                "sessionId": "prime-session-1",
                "isStreaming": False,
                "model": model,
            },
        },
    ]


def test_lifecycle_validator_requires_full_handshake_terminal_event_and_stats(tmp_path):
    candidate = str(tmp_path / "candidate")
    validator = prime_rpc.RpcLifecycleValidator(candidate_path=candidate)
    for record in _valid_rpc_records(candidate):
        validator.consume(record)

    result = validator.finish()

    assert result.session_id == "prime-session-1"
    assert result.model == {"provider": "openrouter", "id": "test/model"}
    assert result.final_text == "candidate complete"
    assert result.stats["tokens"]["total"] == 123
    assert result.event_count == 5


def test_lifecycle_validator_bounds_final_assistant_text(tmp_path):
    candidate = str(tmp_path / "candidate")
    rows = _valid_rpc_records(candidate)
    rows[7]["message"]["content"] = "x" * (schemas.FINAL_TEXT_MAX_CHARS + 1)
    validator = prime_rpc.RpcLifecycleValidator(candidate_path=candidate)

    for record in rows:
        validator.consume(record)

    assert validator.finish().final_text == (
        "x" * schemas.FINAL_TEXT_MAX_CHARS + "…[truncated]"
    )


def test_terminal_observation_responses_before_agent_end_fail_closed(tmp_path):
    candidate = str(tmp_path / "candidate")
    rows = _valid_rpc_records(candidate)
    stats = rows.pop(9)
    final_state = rows.pop(9)
    rows[4:4] = [stats, final_state]
    validator = prime_rpc.RpcLifecycleValidator(candidate_path=candidate)

    with pytest.raises(prime_rpc.RpcProtocolError) as exc:
        for record in rows:
            validator.consume(record)

    assert exc.value.error_code == "RPC_TERMINAL_RESPONSE_BEFORE_AGENT_END"


@pytest.mark.parametrize(
    "event_type",
    [
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
    ],
)
def test_v081_documented_auxiliary_event_types_are_accepted(tmp_path, event_type):
    candidate = str(tmp_path / "candidate")
    rows = _valid_rpc_records(candidate)
    rows.insert(5, {"type": event_type})
    validator = prime_rpc.RpcLifecycleValidator(candidate_path=candidate)

    for record in rows:
        validator.consume(record)

    assert validator.finish().session_id == "prime-session-1"


@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (
            lambda rows: rows.__setitem__(
                0,
                {
                    **rows[0],
                    "data": {**rows[0]["data"], "cwd": "C:/wrong-candidate"},
                },
            ),
            "RPC_CWD_MISMATCH",
        ),
        (
            lambda rows: rows.__setitem__(
                1, {**rows[1], "success": False, "error": "not supported"}
            ),
            "RPC_AUTO_RETRY_DISABLE_FAILED",
        ),
        (
            lambda rows: rows.__setitem__(
                3, {**rows[3], "success": False, "error": "busy"}
            ),
            "RPC_PROMPT_REJECTED",
        ),
        (lambda rows: rows.__setitem__(slice(8, None), []), "RPC_MISSING_AGENT_END"),
        (lambda rows: rows.__setitem__(slice(9, None), []), "RPC_STATS_MISSING"),
        (
            lambda rows: rows[10]["data"].__setitem__("isStreaming", True),
            "RPC_FINAL_STATE_BUSY",
        ),
        (
            lambda rows: rows.insert(
                4,
                {
                    "type": "auto_retry_start",
                    "attempt": 1,
                    "maxAttempts": 3,
                    "delayMs": 100,
                    "errorMessage": "transient",
                },
            ),
            "RPC_AUTO_RETRY_OBSERVED",
        ),
        (
            lambda rows: rows.insert(
                4,
                {
                    "id": "surprise",
                    "type": "response",
                    "command": "unknown",
                    "success": True,
                },
            ),
            "RPC_UNEXPECTED_RESPONSE",
        ),
        (
            lambda rows: rows[7]["message"].update(
                {"stopReason": "error", "errorMessage": "provider failed"}
            ),
            "RPC_AGENT_ERROR",
        ),
        (
            lambda rows: rows[0].__setitem__("command", "bash"),
            "RPC_RESPONSE_COMMAND_MISMATCH",
        ),
        (
            lambda rows: rows.__setitem__(slice(0, 2), [rows[1], rows[0]]),
            "RPC_RESPONSE_OUT_OF_ORDER",
        ),
        (
            lambda rows: rows[10]["data"].__setitem__(
                "model", {"provider": "openrouter", "id": "changed/model"}
            ),
            "RPC_MODEL_CHANGED",
        ),
        (
            lambda rows: rows.insert(9, {"type": "session_action_update"}),
            "RPC_EVENT_AFTER_AGENT_END",
        ),
        (
            lambda rows: rows.__setitem__(slice(5, 7), []),
            "RPC_KERNEL_HEALTH_NOT_PROVEN",
        ),
        (
            lambda rows: rows[5]["args"].__setitem__("code", "pass"),
            "RPC_KERNEL_HEALTH_REQUIRED",
        ),
        (
            lambda rows: rows[6].__setitem__("result", "WRONG"),
            "RPC_KERNEL_HEALTH_FAILED",
        ),
        (
            lambda rows: rows[0]["data"].__setitem__("model", {}),
            "RPC_MODEL_IDENTITY_INVALID",
        ),
    ],
)
def test_lifecycle_validator_fails_closed_on_handshake_or_terminal_gaps(
    tmp_path, mutate, error_code
):
    candidate = str(tmp_path / "candidate")
    rows = _valid_rpc_records(candidate)
    mutate(rows)
    validator = prime_rpc.RpcLifecycleValidator(candidate_path=candidate)

    with pytest.raises(prime_rpc.RpcProtocolError) as exc:
        for record in rows:
            validator.consume(record)
        validator.finish()
    assert exc.value.error_code == error_code
