"""Prime JSON protocol tests (spec §24 Prime protocol)."""

from __future__ import annotations

import json

from conftest import prime_protocol
from conftest import sys as _sys


def _write_stream(tmp_path, records, *, candidate="C:/fake/candidate"):
    """Write a stream file from dicts or raw bytes entries."""
    stream = tmp_path / "prime-events.jsonl"
    out = bytearray()
    for rec in records:
        if isinstance(rec, bytes):
            out += rec + b"\n"
        else:
            if "cwd" not in rec and rec.get("type") == "session":
                rec = {**rec, "cwd": candidate}
            out += json.dumps(rec).encode("utf-8") + b"\n"
    stream.write_bytes(bytes(out))
    return str(stream)


def _header(**overrides):
    header = {
        "type": "session",
        "version": 3,
        "id": "s-1",
        "timestamp": "2026-08-22T00:00:00Z",
        "cwd": "PLACEHOLDER",
    }
    header.update(overrides)
    return header


AGENT_START = {"type": "agent_start"}
MESSAGE_END = {
    "type": "message_end",
    "message": {"role": "assistant", "content": "final answer text"},
}
TURN_END = {"type": "turn_end", "text": "turn done"}
AGENT_END = {
    "type": "agent_end",
    "messages": [{"role": "assistant", "content": "agent-end text"}],
}


def _valid_records(candidate):
    return [
        _header(cwd=candidate),
        AGENT_START,
        MESSAGE_END,
        TURN_END,
        AGENT_END,
    ]


def test_valid_schema3_stream_accepted(tmp_path):
    candidate = str(tmp_path / "candidate")
    path = _write_stream(tmp_path, _valid_records(candidate), candidate=candidate)
    result = prime_protocol.parse_event_stream(path, candidate)
    assert result.valid is True
    assert result.session_id == "s-1"
    assert result.saw_agent_start and result.saw_agent_end
    assert result.event_count == 5


def test_crlf_records_accepted(tmp_path):
    candidate = str(tmp_path / "c")
    stream = tmp_path / "events.jsonl"
    body = b"".join(
        json.dumps(r).encode() + b"\r\n" for r in _valid_records(candidate)
    )
    stream.write_bytes(body)
    result = prime_protocol.parse_event_stream(str(stream), candidate)
    assert result.valid is True


def test_unicode_line_separators_inside_strings_do_not_split(tmp_path):
    candidate = str(tmp_path / "c")
    event = {
        "type": "message_end",
        "message": {"role": "assistant", "content": "line1 line2  line3"},
    }
    records = [_header(cwd=candidate), AGENT_START, event, AGENT_END]
    path = _write_stream(tmp_path, records, candidate=candidate)
    result = prime_protocol.parse_event_stream(path, candidate)
    assert result.valid is True


def test_unknown_event_types_accepted(tmp_path):
    candidate = str(tmp_path / "c")
    records = [
        *_valid_records(candidate)[:2],
        {"type": "brand_new_future_event", "payload": {"x": 1}},
        *_valid_records(candidate)[2:],
    ]
    path = _write_stream(tmp_path, records, candidate=candidate)
    result = prime_protocol.parse_event_stream(path, candidate)
    assert result.valid is True
    assert result.event_count == 6


def test_malformed_json_rejected(tmp_path):
    candidate = str(tmp_path / "c")
    stream = tmp_path / "e.jsonl"
    stream.write_bytes(
        json.dumps(_header(cwd=candidate)).encode()
        + b"\n{broken json\n"
        + b'{"type":"agent_start"}\n'
    )
    result = prime_protocol.parse_event_stream(str(stream), candidate)
    assert result.valid is False
    assert result.error_code == "MALFORMED_EVENT_JSON"


def test_non_object_json_rejected(tmp_path):
    candidate = str(tmp_path / "c")
    stream = tmp_path / "e.jsonl"
    stream.write_bytes(
        json.dumps(_header(cwd=candidate)).encode() + b'\n[1, 2]\n{"type":"agent_start"}\n'
    )
    result = prime_protocol.parse_event_stream(str(stream), candidate)
    assert result.error_code == "NON_OBJECT_EVENT"


def test_wrong_schema_version_rejected(tmp_path):
    candidate = str(tmp_path / "c")
    records = [_header(cwd=candidate, version=4), AGENT_START, AGENT_END]
    path = _write_stream(tmp_path, records, candidate=candidate)
    result = prime_protocol.parse_event_stream(path, candidate)
    assert result.error_code == "WRONG_SCHEMA_VERSION"


def test_wrong_candidate_path_rejected(tmp_path):
    candidate = str(tmp_path / "c")
    records = _valid_records(candidate)
    path = _write_stream(tmp_path, records, candidate=candidate)
    result = prime_protocol.parse_event_stream(path, str(tmp_path / "other"))
    assert result.error_code == "HEADER_CWD_MISMATCH"


def test_windows_path_case_equivalent_accepted(tmp_path):
    if _sys.platform != "win32":
        # The equivalence function still normalizes casefold on win-style paths.
        pass
    lower = str(tmp_path / "cand")
    upper = lower.replace("cand", "CAND")
    records = _valid_records(lower)
    path = _write_stream(tmp_path, records, candidate=lower)
    result = prime_protocol.parse_event_stream(path, upper if _sys.platform == "win32" else lower)
    assert result.valid is (_sys.platform == "win32")


def test_windows_slash_equivalent_accepted(tmp_path):
    if _sys.platform != "win32":
        import pytest

        pytest.skip("Windows slash semantics")
    back = str(tmp_path / "cand")
    forward = back.replace("\\", "/")
    records = _valid_records(back)
    path = _write_stream(tmp_path, records, candidate=back)
    result = prime_protocol.parse_event_stream(path, forward)
    assert result.valid is True


def test_duplicate_agent_start_rejected(tmp_path):
    candidate = str(tmp_path / "c")
    records = [_header(cwd=candidate), AGENT_START, dict(AGENT_START), AGENT_END]
    path = _write_stream(tmp_path, records, candidate=candidate)
    result = prime_protocol.parse_event_stream(path, candidate)
    assert result.error_code == "DUPLICATE_AGENT_START"


def test_missing_agent_start_rejected(tmp_path):
    candidate = str(tmp_path / "c")
    records = [_header(cwd=candidate), MESSAGE_END, AGENT_END]
    path = _write_stream(tmp_path, records, candidate=candidate)
    result = prime_protocol.parse_event_stream(path, candidate)
    assert result.error_code == "MISSING_AGENT_START"


def test_missing_agent_end_rejected(tmp_path):
    candidate = str(tmp_path / "c")
    records = [_header(cwd=candidate), AGENT_START]
    path = _write_stream(tmp_path, records, candidate=candidate)
    result = prime_protocol.parse_event_stream(path, candidate)
    assert result.error_code == "MISSING_AGENT_END"


def test_duplicate_agent_end_rejected(tmp_path):
    candidate = str(tmp_path / "c")
    records = [_header(cwd=candidate), AGENT_START, AGENT_END, dict(AGENT_END)]
    path = _write_stream(tmp_path, records, candidate=candidate)
    result = prime_protocol.parse_event_stream(path, candidate)
    assert result.error_code == "DUPLICATE_AGENT_END"


def test_reversed_lifecycle_order_rejected(tmp_path):
    candidate = str(tmp_path / "c")
    records = [_header(cwd=candidate), AGENT_END, AGENT_START]
    path = _write_stream(tmp_path, records, candidate=candidate)
    result = prime_protocol.parse_event_stream(path, candidate)
    assert result.error_code == "AGENT_END_BEFORE_START"


def test_final_assistant_text_extracted_message_end(tmp_path):
    candidate = str(tmp_path / "c")
    path = _write_stream(tmp_path, _valid_records(candidate), candidate=candidate)
    result = prime_protocol.parse_event_stream(path, candidate)
    assert result.final_text == "final answer text"


def test_final_assistant_text_falls_back_to_agent_end_messages(tmp_path):
    candidate = str(tmp_path / "c")
    records = [_header(cwd=candidate), AGENT_START, AGENT_END]
    path = _write_stream(tmp_path, records, candidate=candidate)
    result = prime_protocol.parse_event_stream(path, candidate)
    assert result.final_text == "agent-end text"


def test_returned_final_text_is_bounded(tmp_path):
    huge = "x" * (prime_protocol.FINAL_TEXT_MAX_CHARS * 2 if hasattr(prime_protocol, "FINAL_TEXT_MAX_CHARS") else 20001)
    bounded = prime_protocol.bound_final_text(huge)
    from conftest import schemas

    limit = schemas.FINAL_TEXT_MAX_CHARS
    assert len(bounded) <= limit + len("…[truncated]")
