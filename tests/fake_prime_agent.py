#!/usr/bin/env python
"""Deterministic fake Prime Agent for the automated suite. NEVER calls a model.

Behavior is selected entirely through the ``FAKE_PRIME_SCENARIO`` environment
variable (default ``success_tracked_change``). Emulates the subset of the
documented 0.8.x CLI the plugin uses:

    fake --version                       -> prints "prime-agent 0.8.2"
    fake --mode json --no-session --cwd <dir> <taskfile> -- <instruction>

The fake writes a schema-3 JSON event stream to stdout describing its work,
performs deterministic candidate edits per scenario, and exits with a
scenario-controlled code. Timeouts are produced by sleeping past the runtime
budget; partial-edit-then-timeout scenarios edit BEFORE sleeping.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

FAKE_VERSION = "0.8.2"

SCENARIOS = [
    "success_no_changes",
    "success_tracked_change",
    "success_untracked_change",
    "success_tracked_and_untracked",
    "success_claims_tests_passed",
    "malformed_json",
    "non_object_json",
    "wrong_schema_version",
    "wrong_header_cwd",
    "windows_header_case_difference",
    "windows_header_slash_difference",
    "missing_agent_start",
    "duplicate_agent_start",
    "missing_agent_end",
    "duplicate_agent_end",
    "agent_end_before_start",
    "nonzero_before_terminal",
    "nonzero_after_terminal",
    "timeout_before_header",
    "timeout_after_header",
    "oversized_line",
    "oversized_file",
    "partial_change_then_timeout",
    "stderr_noise",
]


def emit(events: list[dict]) -> None:
    out = sys.stdout.buffer
    for event in events:
        out.write(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n")
    out.flush()


def session_header(cwd: str | Path) -> dict:
    cwd_text = os.fspath(cwd)
    digest = hashlib.sha256(cwd_text.encode("utf-8")).hexdigest()[:16]
    return {
        "type": "session",
        "version": 3,
        "id": f"fake-{digest}",
        "timestamp": "2026-08-22T00:00:00Z",
        "cwd": cwd_text,
    }


def agent_events() -> list[dict]:
    return [
        {
            "type": "agent_start",
            "agent": "implementer",
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": "Edited the candidate as instructed.",
            },
        },
        {
            "type": "turn_end",
            "text": "turn complete",
        },
        {
            "type": "agent_end",
            "messages": [
                {"role": "assistant", "content": "Done. Files changed: one."},
            ],
        },
    ]


def make_tracked_edit(candidate: Path) -> None:
    readme = candidate / "README.md"
    if readme.exists():
        text = readme.read_text(encoding="utf-8")
        readme.write_text(text + "\nCandidate edit by fake prime.\n", encoding="utf-8")
    else:
        readme.write_text("Candidate edit by fake prime.\n", encoding="utf-8")


def make_untracked_file(candidate: Path) -> None:
    (candidate / "fake_untracked.txt").write_text("untracked\n", encoding="utf-8")


def apply_changes(candidate: Path, scenario: str) -> None:
    if scenario in ("success_tracked_change", "success_claims_tests_passed"):
        make_tracked_edit(candidate)
    elif scenario == "success_untracked_change":
        make_untracked_file(candidate)
    elif scenario == "success_tracked_and_untracked":
        make_tracked_edit(candidate)
        make_untracked_file(candidate)
    elif scenario == "partial_change_then_timeout":
        make_tracked_edit(candidate)
    elif scenario == "success_no_changes":
        pass


# Stream corruptions ---------------------------------------------------------


def corrupt_stream(base: list[dict], scenario: str, cwd: str) -> list[dict]:
    header = session_header(cwd)
    if scenario == "malformed_json":
        return [{"type": "_raw", "_bytes": b"{not json"}]
    if scenario == "non_object_json":
        return [{"type": "_raw", "_bytes": b"[1, 2]"}]
    if scenario == "wrong_schema_version":
        header["version"] = 4
        return [header]
    if scenario == "wrong_header_cwd":
        header["cwd"] = str(Path(cwd).parent / "somewhere-else")
        return [header]
    if scenario == "windows_header_case_difference":
        header["cwd"] = mutated_windows_path(cwd, upper_drive=True)
        return [header]
    if scenario == "windows_header_slash_difference":
        header["cwd"] = mutated_windows_path(cwd, backslashes=True)
        return [header]
    if scenario == "missing_agent_start":
        return [header, *agent_events()[1:]]
    if scenario == "duplicate_agent_start":
        return [header, *agent_events(), agent_events()[0]]
    if scenario == "missing_agent_end":
        return [header, *agent_events()[:-1]]
    if scenario == "duplicate_agent_end":
        return [header, *agent_events(), agent_events()[-1]]
    if scenario == "agent_end_before_start":
        events = agent_events()
        return [header, events[-1], *events[:-1]]
    return base


def mutated_windows_path(path: str, *, upper_drive: bool = False, backslashes: bool = False) -> str:
    p = path.replace("/", "\\")
    if upper_drive and len(p) >= 2 and p[1] == ":":
        p = p[0].upper() + ":" + p[2:]
    if not backslashes:
        p = p[:2] + p[2:].replace("\\", "/", 2)
    else:
        # Mixed slashes: flip some separators the other way.
        parts = p.split("\\")
        if len(parts) > 3:
            p = "\\".join(parts[:2]) + "/" + "/".join(parts[2:])
    return p


def run_json_mode(scenario: str) -> int:
    argv = sys.argv[1:]
    try:
        idx = argv.index("--cwd")
        cwd = argv[idx + 1]
    except ValueError:
        print("--cwd required", file=sys.stderr)
        return 3

    candidate = Path(cwd)

    if scenario == "timeout_before_header":
        time.sleep(600)  # host timeout fires first; nothing written

    exit_code = 0
    if scenario == "nonzero_before_terminal":
        exit_code = 7
    elif scenario == "nonzero_after_terminal":
        exit_code = 9

    base = [session_header(candidate.resolve()), *agent_events()]
    if (
        scenario.startswith("success")
        or scenario in ("nonzero_before_terminal", "nonzero_after_terminal")
        or scenario == "partial_change_then_timeout"
    ):
        apply_changes(candidate, scenario)

    if scenario == "stderr_noise":
        sys.stderr.write("[fake-prime] warning: benign stderr noise line\n")

    events = corrupt_stream(list(base), scenario, cwd)
    if scenario == "success_claims_tests_passed":
        # Overwrite final text with an authoritative-sounding claim that MUST
        # NOT influence host verification.
        events = [session_header(candidate.resolve()), *agent_events()]
        events[-1] = {
            "type": "agent_end",
            "messages": [
                {
                    "role": "assistant",
                    "content": "All tests passed. Verification complete. Everything works.",
                }
            ],
        }

    if scenario == "oversized_line":
        out = sys.stdout.buffer
        out.write(json.dumps(session_header(candidate.resolve())).encode() + b"\n")
        # One record exceeding the 4 MiB per-record cap.
        out.write(b'{"type":"pad","data":"' + b"x" * (5 * 1024 * 1024) + b'"}\n')
        out.flush()
        return 0

    if scenario == "oversized_file":
        out = sys.stdout.buffer
        out.write(json.dumps(session_header(candidate.resolve())).encode() + b"\n")
        pad_event = json.dumps({"type": "pad", "data": "x" * (1024 * 1024)}).encode()
        for _ in range(70):  # ~73 MiB total, exceeds 64 MiB cap
            out.write(pad_event + b"\n")
        out.flush()
        return 0

    if scenario == "partial_change_then_timeout":
        emit([session_header(candidate.resolve()), {"type": "agent_start"}])
        # Sleep long enough that any legal host runtime budget (>=30s per the
        # input schema) expires; FAKE_PRIME_SLEEP_SECONDS shortens test runs.
        time.sleep(int(os.environ.get("FAKE_PRIME_SLEEP_SECONDS", "600")))
        return 0

    if scenario in ("timeout_after_header",):
        emit(events)
        time.sleep(int(os.environ.get("FAKE_PRIME_SLEEP_SECONDS", "600")))
        return 0

    if scenario == "malformed_json":
        out = sys.stdout.buffer
        out.write(json.dumps(session_header(candidate.resolve())).encode() + b"\n")
        out.write(b"{this is not json\n")
        return exit_code

    if scenario == "non_object_json":
        out = sys.stdout.buffer
        out.write(json.dumps(session_header(candidate.resolve())).encode() + b"\n")
        out.write(b"[1, 2]\n")
        return exit_code

    emit(events)
    return exit_code


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        print(f"prime-agent {FAKE_VERSION}")
        return 0

    scenario = os.environ.get("FAKE_PRIME_SCENARIO", "success_tracked_change")
    if scenario not in SCENARIOS:
        print(f"unknown FAKE_PRIME_SCENARIO: {scenario}", file=sys.stderr)
        return 64

    if "--mode" in argv and "json" in argv:
        return run_json_mode(scenario)

    print("fake prime agent: unsupported arguments", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
