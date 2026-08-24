"""Workspace: run-directory creation, request envelope, detached worktree.

Nothing here runs before admission succeeds. Every admitted run keeps its
candidate worktree after ANY terminal outcome (spec §13). Note precisely: the
active checkout's ordinary files remain unchanged, but creating a registered
worktree updates Git's shared worktree metadata in the source ``.git``.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from pathlib import Path

from .models import RunPaths
from .schemas import ValidationError
from .validation import run_git, same_path


def new_run_id() -> str:
    return str(uuid.uuid4())


def runs_root(plugin_data_dir: Path) -> Path:
    return plugin_data_dir / "runs"


def run_layout(run_dir: Path) -> RunPaths:
    return RunPaths(
        run_dir=str(run_dir),
        request_json=str(run_dir / "request.json"),
        prime_task_md=str(run_dir / "prime-task.md"),
        candidate=str(run_dir / "candidate"),
        prime_events=str(run_dir / "prime-events.jsonl"),
        prime_stderr=str(run_dir / "prime-stderr.log"),
        prime_version_txt=str(run_dir / "prime-version.txt"),
        tracked_patch=str(run_dir / "tracked.patch"),
        status_txt=str(run_dir / "status.txt"),
        checks_dir=str(run_dir / "checks"),
        receipt_json=str(run_dir / "receipt.json"),
    )


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Write via temp file in the final directory, fsync, os.replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def atomic_write_text(path: str | Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def canonical_json(payload: dict) -> bytes:
    """Deterministic JSON bytes used for every durable envelope."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def request_digest(request_payload: dict) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(request_payload)).hexdigest()


def write_request_atomic(paths: RunPaths, request_payload: dict) -> None:
    atomic_write_bytes(paths.request_json, canonical_json(request_payload))


TASK_TEMPLATE = """You are executing a bounded candidate implementation inside an isolated,
detached Git worktree.

USER GOAL
=========
{goal}

HOST RULES
==========
1. Work only inside the current working directory.
2. Inspect all applicable AGENTS.md and repository instructions before editing.
3. Implement the smallest complete solution satisfying the user goal.
4. Add or update tests for changed behavior.
5. Do not commit, push, modify remotes, remove the worktree, or modify another checkout.
6. Do not claim that host verification passed. The host will execute the exact
   verification commands independently after this process exits.
7. Do not place credentials or private environment values into source files,
   logs, tests, or output.
8. Finish with a concise summary of:
   - files changed,
   - behavior implemented,
   - tests you ran,
   - known limitations.

This checkout is only a candidate. A human or host process decides whether it
is accepted.
"""


def build_task_envelope(goal: str) -> str:
    """Render the fixed task envelope around the user goal (spec §14)."""
    safe_goal = goal.replace("\x00", "")
    return TASK_TEMPLATE.format(goal=safe_goal)


PRIME_FIXED_INSTRUCTION = (
    "Read the referenced task file and execute it completely."
)


def create_run_directory(
    data_root: Path,
    repository_path: str,
    base_commit: str,
    goal: str,
    checks: list[dict],
    runtime_timeout_seconds: int,
    prime_command_prefix: list[str],
    *,
    run_id: str | None = None,
) -> tuple[str, RunPaths]:
    """Create ``<data_root>/runs/<run-id>/`` and the request envelope.

    Returns ``(run_id, layout)``. Raises ValidationError when the directory
    already exists (UUID4 collision — practically impossible, fail closed).
    """
    rid = run_id or new_run_id()
    run_dir = Path(data_root) / "runs" / rid
    try:
        run_dir.mkdir(parents=True)
    except FileExistsError as exc:
        raise ValidationError("RUN_ID_COLLISION", f"run directory already exists: {rid}") from exc

    layout = run_layout(run_dir)
    request_payload = {
        "schema_version": 1,
        "run_id": rid,
        "goal": goal,
        "repository_path": repository_path,
        "base_commit": base_commit,
        "checks": checks,
        "runtime_timeout_seconds": runtime_timeout_seconds,
        # Operator-owned command prefix; never a model-facing argument.
        "prime_agent_command_tokens": len(prime_command_prefix),
    }
    write_request_atomic(layout, request_payload)
    atomic_write_text(layout.prime_task_md, build_task_envelope(goal))
    (Path(layout.checks_dir)).mkdir(parents=True, exist_ok=True)
    return rid, layout


def create_detached_worktree(
    repository_path: str,
    layout: RunPaths,
    base_commit: str,
) -> None:
    """Create the detached candidate worktree and verify its HEAD."""
    run_git(
        ["worktree", "add", "--detach", layout.candidate, base_commit],
        cwd=repository_path,
    )
    head = run_git(["rev-parse", "HEAD"], cwd=layout.candidate).strip()
    if head != base_commit:
        raise ValidationError(
            "WORKTREE_HEAD_MISMATCH",
            f"candidate HEAD {head} does not match admitted base {base_commit}",
        )


def verify_candidate_isolation(
    repository_path: str,
    candidate_path: str,
) -> bool:
    """True when the candidate differs from the active checkout by more than case."""
    return not same_path(repository_path, candidate_path)


def source_checkout_unchanged(
    repository_path: str,
    base_commit: str,
) -> tuple[bool, str]:
    """Post-run sanity probe of the ACTIVE checkout.

    Returns ``(clean_against_base, detail)``. This is an observational check
    for evidence; admission already guaranteed cleanliness.
    """
    output = run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository_path,
    )
    return not output.strip(), output.strip()[:2000]


def remove_run_directory_never(*_args, **_kwargs) -> None:  # pragma: no cover
    """Guard against future 'cleanup' helpers.

    Deliberate no-op that exists so a code search for automatic removal finds
    an explicit refusal instead of silence: candidates survive every terminal
    outcome; removal is a human decision via ``git worktree remove``.
    """
    raise RuntimeError(
        "automatic run/candidate deletion is forbidden in hermes-prime-rlm"
    )
