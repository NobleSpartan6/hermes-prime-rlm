"""Independent validation of the Prime JSON event stream (schema version 3).

Parsing happens only after a normal process exit. The stream's structural
truth — not Prime's textual claims — is what this module reports.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .schemas import (
    FINAL_TEXT_MAX_CHARS,
    MAX_EVENT_FILE_BYTES,
    MAX_EVENT_RECORD_BYTES,
    PRIME_JSON_SCHEMA_VERSION,
)
from .validation import path_equivalent_for_header


@dataclass
class ProtocolResult:
    valid: bool
    session_id: str | None = None
    saw_agent_start: bool = False
    saw_agent_end: bool = False
    event_count: int = 0
    final_text: str = ""
    error_code: str | None = None
    events: list[dict] = field(default_factory=list)


def parse_event_stream(
    path: str,
    candidate_path: str,
) -> ProtocolResult:
    """Parse and validate ``prime-events.jsonl`` against the schema-3 contract.

    Byte-level rules: split ONLY on 0x0A, tolerate one trailing 0x0D per
    record, strict UTF-8, per-record and whole-file size bounds, empty lines
    ignored, every record must be a JSON object. Unicode line separators
    inside JSON strings are ordinary data and never split records.
    """
    try:
        raw = os.stat(path)
        if raw.st_size > MAX_EVENT_FILE_BYTES:
            return _fail("EVENT_FILE_TOO_LARGE", "event file exceeds 64 MiB")
        with open(path, "rb") as handle:
            data = handle.read(MAX_EVENT_FILE_BYTES + 1)
        if len(data) > MAX_EVENT_FILE_BYTES:
            return _fail("EVENT_FILE_TOO_LARGE", "event file exceeds 64 MiB")
    except FileNotFoundError:
        return _fail("EVENT_STREAM_MISSING", "prime-events.jsonl was not produced")

    records: list[bytes] = []
    for line in data.split(b"\n"):
        if line.endswith(b"\r"):
            line = line[:-1]
        if not line.strip():
            continue
        if len(line) > MAX_EVENT_RECORD_BYTES:
            return _fail("EVENT_RECORD_TOO_LARGE", "single event record exceeds 4 MiB")
        records.append(line)

    parsed: list[dict] = []
    for index, rec in enumerate(records):
        try:
            text = rec.decode("utf-8")  # strict decode
        except UnicodeDecodeError:
            return _fail("EVENT_NOT_UTF8", f"record {index} is not valid UTF-8")
        try:
            value = json.loads(text)
        except ValueError:
            return _fail("MALFORMED_EVENT_JSON", f"record {index} is not valid JSON")
        if not isinstance(value, dict):
            return _fail("NON_OBJECT_EVENT", f"record {index} is not a JSON object")
        parsed.append(value)

    if not parsed:
        return _fail("EMPTY_EVENT_STREAM", "no event records found")

    header = parsed[0]
    if header.get("type") != "session":
        return _fail("MISSING_SESSION_HEADER", "first record must be type=session")
    if header.get("version") != PRIME_JSON_SCHEMA_VERSION:
        return _fail(
            "WRONG_SCHEMA_VERSION",
            f"session version must be {PRIME_JSON_SCHEMA_VERSION}",
        )
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        return _fail("INVALID_SESSION_ID", "session id missing or empty")
    if not isinstance(header.get("timestamp"), str) or not header.get("timestamp"):
        return _fail("INVALID_SESSION_TIMESTAMP", "timestamp missing or empty")
    header_cwd = header.get("cwd")
    if not isinstance(header_cwd, str) or not header_cwd:
        return _fail("INVALID_SESSION_CWD", "cwd missing or empty")
    if not path_equivalent_for_header(header_cwd, candidate_path):
        return _fail("HEADER_CWD_MISMATCH", "session cwd does not match the candidate")

    starts = [i for i, e in enumerate(parsed) if e.get("type") == "agent_start"]
    ends = [i for i, e in enumerate(parsed) if e.get("type") == "agent_end"]

    if len(starts) != 1:
        code = "DUPLICATE_AGENT_START" if len(starts) > 1 else "MISSING_AGENT_START"
        return _fail(code, f"expected exactly one agent_start, found {len(starts)}")
    if len(ends) != 1:
        code = "DUPLICATE_AGENT_END" if len(ends) > 1 else "MISSING_AGENT_END"
        return _fail(code, f"expected exactly one agent_end, found {len(ends)}")
    if ends[0] < starts[0]:
        return _fail("AGENT_END_BEFORE_START", "agent_end preceded agent_start")

    final_text = extract_final_assistant_text(parsed)
    return ProtocolResult(
        valid=True,
        session_id=session_id,
        saw_agent_start=True,
        saw_agent_end=True,
        event_count=len(parsed),
        final_text=bound_final_text(final_text),
        events=parsed,
    )


def bound_final_text(text: str) -> str:
    """Bound returned assistant text to the documented maximum."""
    if len(text) > FINAL_TEXT_MAX_CHARS:
        return text[:FINAL_TEXT_MAX_CHARS] + "…[truncated]"
    return text


def extract_final_assistant_text(events: list[dict]) -> str:
    """Best-effort extraction from the final assistant-bearing records.

    Accepted shapes (spec §17.16): the last ``message_end`` carrying assistant
    text, the last ``turn_end`` carrying text, then ``agent_end.messages``.
    Unknown shapes yield "".
    """
    candidates: list[str] = []

    def _text_of(event: dict) -> str:
        msg = event.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                return "".join(parts)
        if isinstance(event.get("text"), str):
            return event["text"]
        return ""

    def role_of(event: dict) -> str | None:
        message = event.get("message")
        if isinstance(message, dict):
            return message.get("role")
        return event.get("role")

    for event in reversed(events):
        etype = event.get("type")
        if etype != "message_end":
            continue
        if role_of(event) in ("assistant", None):
            text = _text_of(event)
            if text:
                candidates.append(text)
                break
    if not candidates:
        for event in reversed(events):
            if event.get("type") == "turn_end":
                text = _text_of(event)
                if text:
                    candidates.append(text)
                    break
    if not candidates:
        for event in reversed(events):
            if event.get("type") == "agent_end" and isinstance(event.get("messages"), list):
                messages = [m for m in event["messages"] if isinstance(m, dict)]
                for message in reversed(messages):
                    if message.get("role") == "assistant":
                        content = message.get("content")
                        if isinstance(content, str) and content:
                            candidates.append(content)
                            break
                        if isinstance(content, list):
                            parts = [
                                p.get("text", "")
                                for p in content
                                if isinstance(p, dict) and p.get("type") == "text"
                            ]
                            joined = "".join(parts)
                            if joined:
                                candidates.append(joined)
                                break
                break
    return candidates[0] if candidates else ""


def _fail(code: str, message: str) -> ProtocolResult:
    return ProtocolResult(valid=False, error_code=code)


__all__ = [
    "ProtocolResult",
    "parse_event_stream",
    "extract_final_assistant_text",
    "bound_final_text",
]
