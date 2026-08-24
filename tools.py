"""Hermes tool surface: exactly one model-facing tool, ``prime_rlm_run``.

The handler never lets an exception escape (always returns JSON), resolves
operator config through the plugin context, orchestrates admission → run →
verify → evidence → receipt, and returns a bounded result.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import UTC
from pathlib import Path

from .models import (
    CandidateStability,
    ChangedPaths,
    PrimeObservation,
    Status,
)
from .prime_process import merge_observation, run_prime
from .prime_protocol import parse_event_stream
from .receipt import build_receipt, default_platform_record, write_receipt_atomic
from .schemas import (
    CHECKS_MAX_ENTRIES,
    RUNTIME_TIMEOUT_DEFAULT_SECONDS,
    ValidationError,
    validate_checks,
    validate_command_prefix,
    validate_goal,
    validate_repository_path,
    validate_runtime_timeout,
)
from .validation import _LAST_PROBED_VERSION_OUTPUT, admit_request
from .verification import checks_all_passed, run_all_checks
from .workspace import (
    create_detached_worktree,
    create_run_directory,
)

TOOLSET_NAME = "prime_rlm"
RUNS_SUBDIR = "runs"


# ---------------------------------------------------------------------------
# Tool schema exposed to the model
# ---------------------------------------------------------------------------

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "prime_rlm_run",
        "description": (
            "Run a bounded coding goal through Prime Agent (RLM) inside a "
            "detached Git worktree created from the repository's current HEAD. "
            "The source checkout is never modified and the candidate is never "
            "applied: you get a run id, the candidate path, independently "
            "executed verification results, and a deterministic receipt for "
            "human review. Requires a clean repository and a configured "
            "prime-agent command."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "The bounded coding goal for Prime Agent.",
                },
                "repository_path": {
                    "type": "string",
                    "description": "Absolute path to the exact Git repository root.",
                },
                "checks": {
                    "type": "array",
                    "description": (
                        f"Verification commands executed by the host inside the "
                        f"candidate after Prime finishes (max {CHECKS_MAX_ENTRIES}). "
                        "Empty means COMPLETED_UNVERIFIED, never success."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "pattern": r"^[A-Za-z0-9._-]{1,64}$",
                            },
                            "argv": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                            },
                            "timeout_seconds": {"type": "integer", "minimum": 1},
                        },
                        "required": ["name", "argv"],
                    },
                },
                "runtime_timeout_seconds": {
                    "type": "integer",
                    "description": (
                        "Prime runtime bound in seconds "
                        f"(default {RUNTIME_TIMEOUT_DEFAULT_SECONDS})."
                    ),
                    "minimum": 30,
                    "maximum": 3600,
                },
            },
            "required": ["goal", "repository_path", "checks"],
        },
    },
}


def register_tools(ctx) -> None:
    """Register the single tool (called by Hermes' loader).

    Stores the context on the handler function itself (mirroring how Hermes
    hands plugins their context once at registration); no subprocess, network,
    or filesystem work happens here.
    """
    handle_prime_rlm_run.registration_context = ctx  # type: ignore[attr-defined]
    ctx.register_tool(
        name="prime_rlm_run",
        toolset=TOOLSET_NAME,
        schema=TOOL_SCHEMA["function"],
        handler=handle_prime_rlm_run,
        description=TOOL_SCHEMA["function"]["description"],
    )


# ---------------------------------------------------------------------------
# Operator config resolution
# ---------------------------------------------------------------------------


def resolve_prime_command(ctx) -> list[str]:
    """Read ``prime_agent_command`` from plugin settings (never model input)."""
    raw = None
    getter = getattr(ctx, "get_config", None)
    if callable(getter):
        try:
            raw = getter("prime_agent_command")
        except Exception:
            raw = None
    return validate_command_prefix(raw)


def resolve_plugin_data_dir(ctx) -> Path:
    """Resolve the sanctioned per-plugin durable data directory.

    Prefers ``ctx.state.data_dir`` (profile-scoped, matches portable
    PLUGIN_DATA); falls back to ``<hermes home>/plugin-data/prime-rlm`` via
    the host helper when a bare fake context omits state.
    """
    state = getattr(ctx, "state", None)
    data_dir = getattr(state, "data_dir", None) if state is not None else None
    if data_dir:
        return Path(data_dir)
    try:
        from plugins.plugin_storage import plugin_data_dir

        return Path(plugin_data_dir("prime-rlm"))
    except Exception as exc:
        raise ValidationError(
            "PLUGIN_DATA_UNAVAILABLE",
            "could not resolve the Hermes plugin-data directory for this profile.",
        ) from exc


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------


def _validation_error_payload(exc: ValidationError) -> dict:
    return {
        "ok": False,
        "stage": "validation",
        "error_code": exc.error_code,
        "message": str(exc),
    }


def _compact_result(
    *,
    status: Status,
    run_id: str,
    base_commit: str | None,
    candidate_path: str | None,
    receipt_path: str | None,
    receipt_sha256: str | None,
    changed_paths: ChangedPaths | None,
    check_rows: list[dict] | None,
    prime_final_text: str,
    error_code: str | None = None,
    message: str | None = None,
    stage: str = "run",
) -> dict:
    uncertain = status is Status.UNCERTAIN
    if uncertain:
        next_action = (
            "Inspect the preserved candidate and process evidence before "
            "deciding whether to start a new run."
        )
    elif status is Status.VERIFIED:
        next_action = "Inspect the candidate and receipt. No changes were applied."
    else:
        next_action = "Inspect the candidate and receipt. No changes were applied."
    payload: dict = {
        "ok": status is Status.VERIFIED or status is Status.COMPLETED_UNVERIFIED,
        "status": status.value,
        "run_id": run_id,
    }
    if base_commit:
        payload["base_commit"] = base_commit
    if candidate_path:
        payload["candidate_path"] = candidate_path
    if receipt_path:
        payload["receipt_path"] = receipt_path
    if receipt_sha256:
        payload["receipt_sha256"] = receipt_sha256
    payload["changed_paths"] = (changed_paths or ChangedPaths()).to_dict()
    payload["checks"] = check_rows or []
    payload["prime_final_text"] = prime_final_text
    if uncertain:
        payload["candidate_stability"] = "unknown"
        payload["automatic_retry_allowed"] = False
    if error_code:
        payload["error_code"] = error_code
        payload["message"] = message or ""
        payload["stage"] = stage
    payload["next_action"] = next_action
    return payload


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


def handle_prime_rlm_run(args: dict, **_kwargs) -> str:
    """Tool entry point. Always returns a JSON string; never raises."""
    try:
        return json.dumps(_run(args or {}), ensure_ascii=False)
    except ValidationError as exc:
        return json.dumps(_validation_error_payload(exc), ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - tool boundary must not leak
        return json.dumps(
            {
                "ok": False,
                "stage": "internal",
                "error_code": "UNEXPECTED_FAILURE",
                "message": f"{type(exc).__name__}: {exc}",
            },
            ensure_ascii=False,
        )


def _run(args: dict) -> dict:
    started_monotonic = time.monotonic()

    # 1. Input admission (pure).
    goal = validate_goal(args.get("goal"))
    repository_path = validate_repository_path(args.get("repository_path"))
    checks = validate_checks(args.get("checks"))
    runtime_timeout = validate_runtime_timeout(args.get("runtime_timeout_seconds"))

    # Handler receives its context through the attribute captured at
    # registration time (register_tools stores it on this function).
    ctx = getattr(handle_prime_rlm_run, "registration_context", None)
    if ctx is None:
        raise ValidationError(
            "PLUGIN_NOT_REGISTERED",
            "prime_rlm_run was invoked without plugin registration context.",
        )
    command_prefix = resolve_prime_command(ctx)

    # 2. Repository + version admission (read-only; no files created yet).
    repo_root, base_commit = admit_request(repository_path, command_prefix)

    # 3. Workspace creation (first filesystem side effects).
    data_root = resolve_plugin_data_dir(ctx)
    run_id, layout = create_run_directory(
        data_root=data_root,
        repository_path=repo_root,
        base_commit=base_commit,
        goal=goal,
        checks=checks,
        runtime_timeout_seconds=runtime_timeout,
        prime_command_prefix=command_prefix,
    )

    try:
        return _execute_admitted_run(
            ctx=ctx,
            run_id=run_id,
            layout=layout,
            repo_root=repo_root,
            base_commit=base_commit,
            checks=checks,
            runtime_timeout=runtime_timeout,
            command_prefix=command_prefix,
            started_monotonic=started_monotonic,
        )
    except ValidationError:
        raise
    except Exception:
        # Known-failure bucket: preserve evidence, report FAILED.
        _write_status(layout, Status.FAILED.value)
        return _compact_result(
            status=Status.FAILED,
            run_id=run_id,
            base_commit=base_commit,
            candidate_path=candidate_display_path(layout),
            receipt_path=None,
            receipt_sha256=None,
            changed_paths=ChangedPaths(),
            check_rows=[],
            prime_final_text="",
            error_code="RUN_FAILED",
            message="the run failed after admission; preserved evidence is available.",
        )


def _execute_admitted_run(
    *,
    ctx,
    run_id: str,
    layout,
    repo_root: str,
    base_commit: str,
    checks: list[dict],
    runtime_timeout: int,
    command_prefix: list[str],
    started_monotonic: float,
) -> dict:
    # Persist the probed Prime version text (no environment contents).
    version_text = _LAST_PROBED_VERSION_OUTPUT.get()
    if version_text:
        from .workspace import atomic_write_text

        atomic_write_text(layout.prime_version_txt, version_text.strip()[:2000] + "\n")

    create_detached_worktree(repo_root, layout, base_commit)

    # 4. Run Prime inside the candidate.
    observation, argv_record = run_prime(command_prefix, layout, runtime_timeout)
    del argv_record  # recorded implicitly via events/stderr digests; kept minimal

    if observation.timed_out:
        return _finish_uncertain(
            run_id=run_id,
            layout=layout,
            repo_root=repo_root,
            base_commit=base_commit,
            checks=checks,
            observation=observation,
            reason_code="PRIME_RUNTIME_TIMEOUT",
            started_monotonic=started_monotonic,
        )

    if observation.exit_code != 0:
        return _finish_failed(
            run_id=run_id,
            layout=layout,
            repo_root=repo_root,
            base_commit=base_commit,
            checks=checks,
            observation=observation,
            error_code="PRIME_NONZERO_EXIT" if observation.launched else "PRIME_LAUNCH_FAILED",
            started_monotonic=started_monotonic,
        )

    # Exit 0 → independent protocol validation.
    protocol_result = parse_event_stream(layout.prime_events, layout.candidate)
    merge_observation(observation, protocol_result)
    if not protocol_result.valid:
        return _finish_uncertain(
            run_id=run_id,
            layout=layout,
            repo_root=repo_root,
            base_commit=base_commit,
            checks=checks,
            observation=observation,
            reason_code=protocol_result.error_code or "INVALID_STREAM",
            started_monotonic=started_monotonic,
        )

    # 5. Host verification (only on exit-0 + valid terminal stream).
    if checks:
        results = run_all_checks(checks, layout.candidate, layout.checks_dir)
        all_passed = checks_all_passed(results)
    else:
        results = []
        all_passed = False

    # 6. Evidence + receipt.
    changed_paths, digests, tree_digest = _collect_evidence(layout, base_commit)
    status = _map_status(bool(checks), all_passed)
    stability = CandidateStability.KNOWN

    receipt = build_receipt(
        run_id=run_id,
        status=status,
        request_sha256=_request_sha256_of(layout),
        prime_agent_version=_prime_version_string(),
        platform_record=default_platform_record(),
        repository_root=repo_root,
        base_commit=base_commit,
        candidate_path=os.fspath(Path(layout.candidate).resolve(strict=False)),
        candidate_head=_candidate_head(layout),
        candidate_tree_sha256=tree_digest,
        tracked_patch_sha256=digests["tracked_patch"],
        prime_events_sha256=digests["events"],
        prime_stderr_sha256=digests["stderr"],
        changed_paths=changed_paths,
        observation=observation,
        checks=results,
        started_at=_iso_from_monotonic(started_monotonic),
        candidate_stability=stability,
    )
    receipt_sha256 = write_receipt_atomic(receipt, layout.receipt_json)
    _write_status(layout, status.value)

    return _compact_result(
        status=status,
        run_id=run_id,
        base_commit=base_commit,
        candidate_path=candidate_display_path(layout),
        receipt_path=layout.receipt_json,
        receipt_sha256=receipt_sha256,
        changed_paths=changed_paths,
        check_rows=[_check_row(r) for r in results],
        prime_final_text=observation.final_text,
    )


# ---------------------------------------------------------------------------
# Terminal-path helpers
# ---------------------------------------------------------------------------


def _finish_uncertain(
    *,
    run_id: str,
    layout,
    repo_root: str,
    base_commit: str,
    checks: list[dict],
    observation: PrimeObservation,
    reason_code: str,
    started_monotonic: float,
) -> dict:
    """UNCERTAIN: no checks, preserve everything, never retry."""
    del checks  # never executed on this path
    changed_paths = ChangedPaths()
    digests = {"tracked_patch": "", "events": "", "stderr": ""}
    tree_digest = ""
    with contextlib.suppress(Exception):
        changed_paths, digests, tree_digest = _collect_evidence(layout, base_commit)
    receipt = build_receipt(
        run_id=run_id,
        status=Status.UNCERTAIN,
        request_sha256=_request_sha256_of(layout),
        prime_agent_version=_prime_version_string(),
        platform_record=default_platform_record(),
        repository_root=repo_root,
        base_commit=base_commit,
        candidate_path=os.fspath(Path(layout.candidate).resolve(strict=False)),
        candidate_head=_candidate_head(layout),
        candidate_tree_sha256=tree_digest,
        tracked_patch_sha256=digests["tracked_patch"],
        prime_events_sha256=digests["events"],
        prime_stderr_sha256=digests["stderr"],
        changed_paths=changed_paths,
        observation=observation,
        checks=[],
        started_at=_iso_from_monotonic(started_monotonic),
        candidate_stability=CandidateStability.UNKNOWN,
    )
    receipt_sha256 = write_receipt_atomic(receipt, layout.receipt_json)
    _write_status(layout, Status.UNCERTAIN.value)
    return _compact_result(
        status=Status.UNCERTAIN,
        run_id=run_id,
        base_commit=base_commit,
        candidate_path=candidate_display_path(layout),
        receipt_path=layout.receipt_json,
        receipt_sha256=receipt_sha256,
        changed_paths=changed_paths,
        check_rows=[],
        prime_final_text=observation.final_text,
        error_code=reason_code,
        message=(
            "candidate stability is unknown; no verification checks were run."
        ),
    )


def _finish_failed(
    *,
    run_id: str,
    layout,
    repo_root: str,
    base_commit: str,
    checks: list[dict],
    observation: PrimeObservation,
    error_code: str,
    started_monotonic: float,
) -> dict:
    """FAILED: known failure after admission; candidate may hold partial edits."""
    del checks
    changed_paths = ChangedPaths()
    digests = {"tracked_patch": "", "events": "", "stderr": ""}
    tree_digest = ""
    with contextlib.suppress(Exception):
        changed_paths, digests, tree_digest = _collect_evidence(layout, base_commit)
    partial = error_code == "PRIME_NONZERO_EXIT"
    observation_partial = PrimeObservation(
        launched=observation.launched,
        exit_code=observation.exit_code,
        session_id=observation.session_id,
        saw_agent_start=observation.saw_agent_start,
        saw_agent_end=observation.saw_agent_end,
        event_count=observation.event_count,
        final_text=observation.final_text,
        timed_out=observation.timed_out,
        stream_valid=observation.stream_valid,
        error_code=observation.error_code,
    )
    receipt = build_receipt(
        run_id=run_id,
        status=Status.FAILED,
        request_sha256=_request_sha256_of(layout),
        prime_agent_version=_prime_version_string(),
        platform_record=default_platform_record(),
        repository_root=repo_root,
        base_commit=base_commit,
        candidate_path=os.fspath(Path(layout.candidate).resolve(strict=False)),
        candidate_head=_candidate_head(layout),
        candidate_tree_sha256=tree_digest,
        tracked_patch_sha256=digests["tracked_patch"],
        prime_events_sha256=digests["events"],
        prime_stderr_sha256=digests["stderr"],
        changed_paths=changed_paths,
        observation=observation_partial,
        checks=[],
        started_at=_iso_from_monotonic(started_monotonic),
        candidate_stability=CandidateStability.UNKNOWN
        if partial
        else CandidateStability.KNOWN,
    )
    receipt_sha256 = write_receipt_atomic(receipt, layout.receipt_json)
    _write_status(layout, Status.FAILED.value)
    return _compact_result(
        status=Status.FAILED,
        run_id=run_id,
        base_commit=base_commit,
        candidate_path=candidate_display_path(layout),
        receipt_path=layout.receipt_json,
        receipt_sha256=receipt_sha256,
        changed_paths=changed_paths,
        check_rows=[],
        prime_final_text=observation.final_text,
        error_code=error_code,
        message=(
            "Prime exited nonzero; the admitted candidate may contain partial changes."
            if partial
            else "Prime could not be launched; the candidate was created but untouched."
        ),
    )


# ---------------------------------------------------------------------------
# Evidence + status plumbing
# ---------------------------------------------------------------------------


def _collect_evidence(layout, base_commit: str):
    from .evidence import (
        candidate_tree_digest,
        collect_status_z,
        parse_status_z,
        sha256_file,
        write_tracked_diff,
    )

    entries = collect_status_z(layout.candidate)
    changed = parse_status_z(entries)
    patch_sha = write_tracked_diff(layout.candidate, base_commit, layout.tracked_patch)
    tree_digest = candidate_tree_digest(layout.candidate)
    digests = {
        "tracked_patch": patch_sha,
        "events": sha256_file(layout.prime_events),
        "stderr": sha256_file(layout.prime_stderr),
    }
    return changed, digests, tree_digest


def _map_status(had_checks: bool, all_passed: bool) -> Status:
    if not had_checks:
        return Status.COMPLETED_UNVERIFIED
    return Status.VERIFIED if all_passed else Status.FAILED_VERIFICATION


def _check_row(result) -> dict:
    return {
        "name": result.name,
        "status": result.status.value,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
    }


def _request_sha256_of(layout) -> str:
    import hashlib

    try:
        with open(layout.request_json, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return ""


def _candidate_head(layout) -> str:
    from .validation import run_git

    try:
        return run_git(["rev-parse", "HEAD"], cwd=layout.candidate).strip()
    except Exception:
        return ""


def _prime_version_string() -> str | None:
    text = _LAST_PROBED_VERSION_OUTPUT.get()
    if not text:
        return None
    return " ".join(text.strip().split())[:64]


def _write_status(layout, status_value: str) -> None:
    from .workspace import atomic_write_text

    atomic_write_text(layout.status_txt, status_value + "\n")


def _iso_from_monotonic(started_monotonic: float) -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def candidate_display_path(layout) -> str:
    return os.fspath(Path(layout.candidate).resolve(strict=False))


# ---------------------------------------------------------------------------
# Registration context lives on handle_prime_rlm_run.registration_context
# (set by register_tools); no module-level mutable state is needed.
# ---------------------------------------------------------------------------
