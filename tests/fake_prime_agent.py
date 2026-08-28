#!/usr/bin/env python
"""Deterministic fake Prime Agent for the automated suite. NEVER calls a model.

Behavior is selected through the test-only ``--fake-scenario`` command-prefix
argument or, for direct process tests, ``FAKE_PRIME_SCENARIO``. It emulates the
pinned Prime v0.8.1 CLI subset used by the plugin:

    fake --version                       -> prints "prime-agent 0.8.1"
    fake --mode rpc --no-session --cwd <dir>
    fake --mode json --no-session --cwd <dir> <taskfile> -- <instruction>

The fake writes deterministic JSONL RPC responses/events to stdout,
performs deterministic candidate edits per scenario, and exits with a
scenario-controlled code. Timeouts are produced by sleeping past the runtime
budget; partial-edit-then-timeout scenarios edit BEFORE sleeping.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

FAKE_VERSION = "0.8.1"

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
    "rpc_success_fragmented",
    "rpc_malformed_json",
    "rpc_premature_eof",
    "rpc_oversized_record",
    "rpc_prompt_rejected",
    "rpc_requires_staged_admission",
    "rpc_readiness_extra_event",
    "rpc_success_stderr_descendant",
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


_RPC_WRITE_LOCK = threading.Lock()


def _rpc_write(value: dict, *, fragmented: bool) -> None:
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"
    with _RPC_WRITE_LOCK:
        if not fragmented:
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
            return
        index = 0
        step = 1
        while index < len(payload):
            sys.stdout.buffer.write(payload[index : index + step])
            sys.stdout.buffer.flush()
            index += step
            step = 1 if step == 7 else step + 1


def run_rpc_mode(scenario: str) -> int:
    argv = sys.argv[1:]
    try:
        cwd = argv[argv.index("--cwd") + 1]
    except (ValueError, IndexError):
        print("--cwd required", file=sys.stderr)
        return 3
    candidate = Path(cwd)
    model = {"provider": "openrouter", "id": "test/model"}
    fragmented = scenario == "rpc_success_fragmented"
    retry_disabled = threading.Event()
    for raw_line in sys.stdin.buffer:
        command = json.loads(raw_line.decode("utf-8"))
        command_id = command.get("id")
        command_type = command.get("type")
        if command_type == "get_state":
            _rpc_write(
                {
                    "id": command_id,
                    "type": "response",
                    "command": "get_state",
                    "success": True,
                    "data": {
                        "sessionId": "fake-rpc-session",
                        "isStreaming": False,
                        "model": model,
                    },
                },
                fragmented=fragmented,
            )
        elif command_type == "set_auto_retry":
            if scenario == "rpc_requires_staged_admission":
                response_id = command_id
                enabled = command.get("enabled")

                def delayed_retry_response(
                    response_id=response_id,
                    enabled=enabled,
                ) -> None:
                    time.sleep(0.15)
                    retry_disabled.set()
                    _rpc_write(
                        {
                            "id": response_id,
                            "type": "response",
                            "command": "set_auto_retry",
                            "success": enabled is False,
                        },
                        fragmented=False,
                    )

                threading.Thread(target=delayed_retry_response, daemon=True).start()
                continue
            _rpc_write(
                {
                    "id": command_id,
                    "type": "response",
                    "command": "set_auto_retry",
                    "success": command.get("enabled") is False,
                },
                fragmented=fragmented,
            )
        elif command_type == "get_available_models":
            if scenario == "rpc_requires_staged_admission" and not retry_disabled.is_set():
                _rpc_write(
                    {
                        "id": command_id,
                        "type": "response",
                        "command": "get_available_models",
                        "success": False,
                        "error": "retry disable response was not awaited",
                    },
                    fragmented=False,
                )
                continue
            _rpc_write(
                {
                    "id": command_id,
                    "type": "response",
                    "command": "get_available_models",
                    "success": True,
                    "data": {"models": [model]},
                },
                fragmented=fragmented,
            )
            if scenario == "rpc_readiness_extra_event":
                _rpc_write({"type": "agent_start"}, fragmented=False)
        elif command_type == "prompt":
            prompt_log = os.environ.get("FAKE_RPC_PROMPT_LOG")
            if prompt_log:
                Path(prompt_log).write_text(str(command.get("message", "")), encoding="utf-8")
            accepted = scenario != "rpc_prompt_rejected"
            _rpc_write(
                {
                    "id": command_id,
                    "type": "response",
                    "command": "prompt",
                    "success": accepted,
                    **({} if accepted else {"error": "rejected by fake"}),
                },
                fragmented=fragmented,
            )
            if not accepted:
                return 0
            if scenario in {"rpc_malformed_json", "malformed_json"}:
                sys.stdout.buffer.write(b"{not-json}\n")
                sys.stdout.buffer.flush()
                return 0
            if scenario == "rpc_premature_eof":
                sys.stdout.buffer.write(b'{"type":"agent_start"')
                sys.stdout.buffer.flush()
                return 0
            if scenario == "rpc_oversized_record":
                sys.stdout.buffer.write(
                    b'{"type":"message_update","padding":"'
                    + b"x" * (5 * 1024 * 1024)
                    + b'"}\n'
                )
                sys.stdout.buffer.flush()
                return 0
            if scenario == "nonzero_before_terminal":
                return 7
            if scenario == "partial_change_then_timeout":
                apply_changes(candidate, scenario)
                time.sleep(int(os.environ.get("FAKE_PRIME_SLEEP_SECONDS", "600")))
                return 0
            if scenario == "rpc_success_fragmented":
                readme = candidate / "README.md"
                readme.write_text(
                    readme.read_text(encoding="utf-8") + "\nRPC edit by fake prime.\n",
                    encoding="utf-8",
                )
            elif scenario == "rpc_success_stderr_descendant":
                subprocess.Popen(  # noqa: S603 - deterministic test child
                    [sys.executable, "-c", "import time; time.sleep(4)"],
                    stdout=subprocess.DEVNULL,
                    stderr=None,
                )
            else:
                apply_changes(candidate, scenario)
            final_text = (
                "All tests passed. Verification complete. Everything works."
                if scenario == "success_claims_tests_passed"
                else "RPC candidate complete"
            )
            for event in [
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
                    "message": {
                        "role": "assistant",
                        "content": final_text,
                    },
                },
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": final_text}
                    ],
                },
            ]:
                _rpc_write(event, fragmented=fragmented)
        elif command_type == "get_session_stats":
            _rpc_write(
                {
                    "id": command_id,
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {"tokens": {"total": 123}, "cost": 0.01},
                },
                fragmented=fragmented,
            )
        else:
            _rpc_write(
                {
                    "id": command_id,
                    "type": "response",
                    "command": command_type,
                    "success": False,
                    "error": "unsupported fake RPC command",
                },
                fragmented=fragmented,
            )
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        print(f"prime-agent {FAKE_VERSION}")
        return 0

    if "--fake-scenario" in argv:
        try:
            scenario = argv[argv.index("--fake-scenario") + 1]
        except IndexError:
            print("--fake-scenario requires a value", file=sys.stderr)
            return 64
    else:
        scenario = os.environ.get("FAKE_PRIME_SCENARIO", "success_tracked_change")
    if scenario not in SCENARIOS:
        print(f"unknown FAKE_PRIME_SCENARIO: {scenario}", file=sys.stderr)
        return 64

    if "--mode" in argv and "rpc" in argv:
        return run_rpc_mode(scenario)

    if "--mode" in argv and "json" in argv:
        return run_json_mode(scenario)

    print("fake prime agent: unsupported arguments", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
