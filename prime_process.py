"""Prime Agent invocation: argv construction and process lifecycle.

The command is built from the operator-owned prefix plus fixed arguments. The
goal travels ONLY through the plugin-owned task file plus a short fixed
instruction — never on the command line (spec §14/§16).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .models import PrimeObservation
from .platform_runtime import (
    SpawnSpec,
    build_shim_argv,
    effective_command_record,
    guard_shim_argument,
    is_cmd_shim,
    resolve_command_prefix,
    resolve_shim_target,
    spawn_and_wait,
)
from .workspace import PRIME_FIXED_INSTRUCTION

# Bounded probe window: after a timeout we still poll briefly for an exit code
# produced during tree termination; None means no reliable boundary.
POST_TERMINATION_POLL_SECONDS = 2.0


@dataclass
class PrimeInvocation:
    """Fully-resolved plan for one Prime process run."""

    argv: list[str]
    cwd: str
    env_extra: dict


def build_prime_argv(
    command_prefix_resolved: list[str],
    candidate_path: str,
    task_file_path: str,
) -> list[str]:
    """Assemble the fixed JSON-mode invocation.

    Shape (spec §16, confirmed against the installed Prime 0.8.0 CLI)::

        <prefix> --mode json --no-session --cwd <candidate> @<task-file> -- <fixed-instruction>

    The task file travels as an ``@file`` argument (Prime's documented file
    reference form), so the user goal never appears on the command line.
    ``--`` terminates option parsing so the trailing instruction can never be
    interpreted as flags, and so a goal-shaped instruction cannot inject
    options even if a future Prime reads the tail as text.
    """
    head = command_prefix_resolved[0]
    tail = [
        "--mode",
        "json",
        "--no-session",
        "--cwd",
        candidate_path,
        f"@{task_file_path}",
        "--",
        PRIME_FIXED_INSTRUCTION,
    ]
    if is_cmd_shim(head):
        # npm .cmd shims re-split space-bearing args at the cmd.exe layer
        # (verified empirically), which would corrupt the task-file path and
        # the fixed instruction. When the shim matches npm's template, resolve
        # its real target (node.exe + cli.js) and spawn that directly — full
        # literal argv, no cmd.exe involved. Non-npm shims fall back to the
        # guarded short-path route.
        target = resolve_shim_target(head)
        if target is not None:
            node_exe, cli_js = target
            return [
                node_exe,
                cli_js,
                *command_prefix_resolved[1:],
                *tail,
            ]
        return build_shim_argv(head, tail)
    return [head, *command_prefix_resolved[1:], *tail]


def assert_no_goal_on_command_line(argv: list[str], goal: str) -> None:
    """Defense-in-depth: refuse any argv that embeds user goal text.

    Guards both the direct and shim routes against future edits that might
    place the goal on the command line (spec §14/§15 hostile-content rules).
    Long goals are chunked because the goal may exceed any single argument;
    any 64-byte sliding window match fails the run pre-spawn.
    """
    joined = "\x00".join(argv)
    normalized_goal = " ".join(goal.split())
    if len(normalized_goal) < 8:
        needle = normalized_goal
        if needle and needle in joined:
            raise ValueError("goal text leaked into command line")
        return
    step = 32
    window = 64
    compact = " ".join(joined.split())
    for i in range(0, min(len(normalized_goal), 4096), step):
        chunk = normalized_goal[i : i + window]
        if len(chunk) >= 8 and chunk in compact:
            raise ValueError("goal text leaked into command line")


def run_prime(
    command_prefix: list[str],
    layout,
    runtime_timeout_seconds: int,
) -> tuple[PrimeObservation, list[str]]:
    """Launch Prime inside the candidate and observe it to a terminal boundary.

    Returns ``(observation, effective_argv_record)``. Never retries.
    """
    try:
        resolved = resolve_command_prefix(command_prefix)
    except FileNotFoundError:
        # Launch failure is a known terminal state (FAILED), not an exception.
        return (
            PrimeObservation(
                launched=False,
                exit_code=None,
                session_id=None,
                saw_agent_start=False,
                saw_agent_end=False,
                event_count=0,
                error_code="PRIME_COMMAND_NOT_FOUND",
            ),
            [],
        )
    argv = build_prime_argv(resolved, os.fspath(layout.candidate), layout.prime_task_md)

    # The task file path is argv-bound; when the head resolves to a .cmd/.bat
    # shim, paths with spaces are fine but cmd-active characters are not.
    # Fail closed before spawn rather than risk reinterpretation.
    if is_cmd_shim(resolved[0]):
        for token in argv[1:]:
            if token.startswith("cmd.exe"):
                continue
            guard_shim_argument(token)

    assert_no_goal_on_command_line(argv, read_goal_from_task(layout))

    spec = SpawnSpec(
        argv=argv,
        cwd=os.fspath(layout.candidate),
        stdout_path=layout.prime_events,
        stderr_path=layout.prime_stderr,
        env={**os.environ, "PI_SKIP_VERSION_CHECK": "1"},
    )
    exit_code, timed_out = spawn_and_wait(spec, float(runtime_timeout_seconds))
    record = effective_command_record(argv)

    if timed_out:
        return (
            PrimeObservation(
                launched=True,
                exit_code=None,
                session_id=None,
                saw_agent_start=False,
                saw_agent_end=False,
                event_count=0,
                timed_out=True,
                stream_valid=False,
            ),
            record,
        )
    if exit_code is None:
        return (
            PrimeObservation(
                launched=False,
                exit_code=None,
                session_id=None,
                saw_agent_start=False,
                saw_agent_end=False,
                event_count=0,
                error_code="PRIME_LAUNCH_FAILED",
            ),
            record,
        )
    return (
        PrimeObservation(
            launched=True,
            exit_code=exit_code,
            session_id=None,
            saw_agent_start=False,
            saw_agent_end=False,
            event_count=0,
        ),
        record,
    )


def read_goal_from_task(layout) -> str:
    """Recover the goal from the plugin-owned envelope for leak assertions."""
    try:
        with open(layout.prime_task_md, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return ""
    marker = "=" * 9
    try:
        head, rest = text.split("USER GOAL", 1)
        _m, goal_block = rest.split(marker + "\n", 1) if marker in rest else ("", rest)
        return goal_block.split(marker)[0].strip()
    except ValueError:
        return ""


def merge_observation(observation: PrimeObservation, protocol_result) -> PrimeObservation:
    """Fold protocol validation results into the observation immutably-ish."""
    observation.session_id = protocol_result.session_id
    observation.saw_agent_start = protocol_result.saw_agent_start
    observation.saw_agent_end = protocol_result.saw_agent_end
    observation.event_count = protocol_result.event_count
    observation.final_text = protocol_result.final_text
    observation.stream_valid = protocol_result.valid
    observation.error_code = observation.error_code or protocol_result.error_code
    return observation
