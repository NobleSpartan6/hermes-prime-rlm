"""Hermes tool surface: exactly one model-facing tool, ``prime_agent``.

The handler never lets an exception escape (always returns JSON), resolves
operator config through the plugin context, orchestrates admission → run →
verify → evidence → receipt, and returns a bounded result.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from .models import (
    CandidateStability,
    ChangedPaths,
    PrimeObservation,
    Status,
)
from .prime_rpc import run_prime_rpc
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
from .validation import admit_request
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
        "name": "prime_agent",
        "description": (
            "Run one bounded coding goal through a strict ephemeral Prime Agent "
            "v0.8.1 JSONL RPC session inside a detached Git worktree. "
            "The plugin never auto-applies the candidate: it returns a run id, "
            "candidate path, host-observed check results, and a canonically "
            "encoded unsigned review receipt. This is not a sandbox or "
            "tamper-resistant attestation system. Requires a clean repository "
            "and a configured prime-agent command."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["run"],
                    "description": "Bounded one-shot RPC execution; defaults to run.",
                },
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
    """Register one context-bound handler without module-global run state."""

    def bound_handler(args: dict, **kwargs) -> str:
        return handle_prime_agent(args, _ctx=ctx, **kwargs)

    ctx.register_tool(
        name="prime_agent",
        toolset=TOOLSET_NAME,
        schema=TOOL_SCHEMA["function"],
        handler=bound_handler,
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
            runtime = getter("prime_runtime")
            raw = runtime.get("command") if isinstance(runtime, dict) else None
            if raw is None:
                raw = getter("prime_agent_command")
        except Exception as exc:
            raise ValidationError(
                "PRIME_CONFIG_READ_FAILED",
                "could not read the configured Prime runtime.",
            ) from exc
    return validate_command_prefix(raw)


def resolve_prime_environment(ctx, source_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build Prime's filtered environment plus one non-secret kernel path."""
    from .prime_rpc_process import build_rpc_environment

    env = build_rpc_environment(dict(os.environ) if source_env is None else source_env)
    getter = getattr(ctx, "get_config", None)
    runtime = getter("prime_runtime") if callable(getter) else None
    raw = runtime.get("kernel_python") if isinstance(runtime, dict) else None
    if raw is None and callable(getter):
        raw = getter("prime_agent_kernel_python")
    if raw is None:
        return env
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(
            "KERNEL_PYTHON_INVALID",
            "prime_agent_kernel_python must be a non-empty absolute path.",
        )
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        raise ValidationError(
            "KERNEL_PYTHON_INVALID",
            "prime_agent_kernel_python must name an existing absolute file.",
        )
    env["PRIME_AGENT_KERNEL_PYTHON"] = str(path.resolve(strict=True))
    return env


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
    verified = status is Status.VERIFIED
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
        # ``ok`` means ONLY strong verification (host checks ran and passed).
        # COMPLETED_UNVERIFIED is a completed run, not a success claim: the
        # separate ``completed`` / ``verified`` booleans make that explicit
        # for callers that previously read ok=true as "verification passed".
        "ok": verified,
        "completed": status
        in (Status.VERIFIED, Status.COMPLETED_UNVERIFIED),
        "verified": verified,
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


def handle_prime_agent(args: dict, **kwargs) -> str:
    """Unified v0.2 entry point. Always returns JSON and exposes only run."""
    ctx = kwargs.pop("_ctx", None)
    try:
        if args is None:
            normalized = {}
        elif not isinstance(args, dict):
            raise ValidationError(
                "INVALID_ARGUMENTS", "prime_agent arguments must be a JSON object."
            )
        else:
            normalized = dict(args)
        action = normalized.get("action", "run")
        if action != "run":
            raise ValidationError("INVALID_ACTION", "v0.2 supports only action=run.")
        normalized["action"] = "run"
        return json.dumps(_run(normalized, ctx), ensure_ascii=False)
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


def handle_prime_rlm_run(args: dict, **kwargs) -> str:
    """Python compatibility alias for the v0.1 one-shot handler name."""
    if not isinstance(args, dict):
        return handle_prime_agent(args, **kwargs)
    normalized = dict(args)
    normalized["action"] = "run"
    return handle_prime_agent(normalized, **kwargs)


def _run(args: dict, ctx) -> dict:
    started_monotonic = time.monotonic()
    started_wall = datetime.now(UTC)

    # 1. Input admission (pure).
    goal = validate_goal(args.get("goal"))
    repository_path = validate_repository_path(args.get("repository_path"))
    checks = validate_checks(args.get("checks"))
    runtime_timeout = validate_runtime_timeout(args.get("runtime_timeout_seconds"))

    if ctx is None:
        raise ValidationError(
            "PLUGIN_NOT_REGISTERED",
            "prime_agent was invoked without plugin registration context.",
        )
    command_prefix = resolve_prime_command(ctx)

    # 2. Repository + version admission (read-only; no files created yet).
    # The probed version text returns with the call — no shared mutable memo.
    repo_root, base_commit, prime_version_text = admit_request(
        repository_path, command_prefix
    )

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
            started_wall=started_wall,
            prime_version_text=prime_version_text,
        )
    except ValidationError as exc:
        error_code = exc.error_code
    except Exception:
        error_code = "RUN_FAILED"
        # Diagnosability: the real cause goes to stderr when
        # PRIME_RLM_TRACEBACK=1 — never into the model-facing payload.
        if os.environ.get("PRIME_RLM_TRACEBACK"):
            import sys as _sys
            import traceback as _tb

            print("[prime-rlm] run failed after admission:", file=_sys.stderr)
            _tb.print_exc()
    return _finish_internal_failure(
        run_id=run_id,
        layout=layout,
        repo_root=repo_root,
        base_commit=base_commit,
        started_wall=started_wall,
        prime_version_text=prime_version_text,
        verification_authority=_verification_authority(checks),
        error_code=error_code,
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
    started_wall,
    prime_version_text: str | None = None,
) -> dict:
    # Persist the probed Prime version text (no environment contents).
    if not prime_version_text:
        prime_version_text = None
    if prime_version_text:
        from .workspace import atomic_write_text

        atomic_write_text(
            layout.prime_version_txt, prime_version_text.strip()[:2000] + "\n"
        )

    # Source-identity evidence captured BEFORE any untrusted code runs: the
    # active checkout must be at base and clean; re-checked after the run too.
    source_before = source_checkout_identity(repo_root, base_commit)

    create_detached_worktree(repo_root, layout, base_commit)

    # 4. Run Prime through one strict ephemeral RPC session. Raw argv remains
    # process-local; the receipt records only hashes and allowlisted basenames.
    observation, argv_record = run_prime_rpc(
        command_prefix,
        layout,
        runtime_timeout,
        env=resolve_prime_environment(ctx),
    )

    if observation.timed_out:
        return _finish_uncertain(
            run_id=run_id,
            layout=layout,
            repo_root=repo_root,
            base_commit=base_commit,
            observation=observation,
            reason_code=observation.error_code or "RPC_TIMEOUT",
            started_monotonic=started_monotonic,
            started_wall=started_wall,
            prime_version_text=prime_version_text,
            argv_record=argv_record,
            source_before=source_before,
            verification_authority=_verification_authority(checks),
        )

    if observation.host_terminated:
        return _finish_uncertain(
            run_id=run_id,
            layout=layout,
            repo_root=repo_root,
            base_commit=base_commit,
            observation=observation,
            reason_code=observation.error_code or "RPC_HOST_TERMINATED",
            started_monotonic=started_monotonic,
            started_wall=started_wall,
            prime_version_text=prime_version_text,
            argv_record=argv_record,
            source_before=source_before,
            verification_authority=_verification_authority(checks),
        )

    if not observation.launched or observation.exit_code not in (0, None):
        return _finish_failed(
            run_id=run_id,
            layout=layout,
            repo_root=repo_root,
            base_commit=base_commit,
            observation=observation,
            error_code=(
                "PRIME_NONZERO_EXIT" if observation.launched else "PRIME_LAUNCH_FAILED"
            ),
            started_monotonic=started_monotonic,
            started_wall=started_wall,
            prime_version_text=prime_version_text,
            argv_record=argv_record,
            source_before=source_before,
            verification_authority=_verification_authority(checks),
        )

    if not observation.stream_valid:
        return _finish_uncertain(
            run_id=run_id,
            layout=layout,
            repo_root=repo_root,
            base_commit=base_commit,
            observation=observation,
            reason_code=observation.error_code or "INVALID_RPC_STREAM",
            started_monotonic=started_monotonic,
            started_wall=started_wall,
            prime_version_text=prime_version_text,
            argv_record=argv_record,
            source_before=source_before,
            verification_authority=_verification_authority(checks),
        )

    # 5. Candidate digests BEFORE checks — this binds Prime's proposal itself,
    # independent of anything a check might later do to the tree.
    proposal_tree_digest = candidate_tree_digest_safe(layout)
    if not proposal_tree_digest:
        return _finish_uncertain(
            run_id=run_id,
            layout=layout,
            repo_root=repo_root,
            base_commit=base_commit,
            observation=observation,
            reason_code="PROPOSAL_DIGEST_UNAVAILABLE",
            started_monotonic=started_monotonic,
            started_wall=started_wall,
            prime_version_text=prime_version_text,
            argv_record=argv_record,
            source_before=source_before,
            verification_authority=_verification_authority(checks),
        )

    # 6. Host verification (only on exit-0 + valid terminal stream).
    if checks:
        results = run_all_checks(checks, layout.candidate, layout.checks_dir)
        all_passed = checks_all_passed(results)
    else:
        results = []
        all_passed = False

    # 7. Evidence + receipt. The post-check digest distinguishes verifier-
    # induced mutation from Prime's proposal; both are recorded.
    changed_paths, digests, tree_digest = _collect_evidence(layout, base_commit)
    status = _map_status(bool(checks), all_passed)
    stability = CandidateStability.UNKNOWN

    receipt = build_receipt(
        run_id=run_id,
        status=status,
        request_sha256=_request_sha256_of(layout),
        prime_agent_version=_prime_version_string(prime_version_text),
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
        started_at=_iso_from_wall(started_wall),
        candidate_stability=stability,
        prime_command_identity=prime_command_identity(argv_record),
        proposal_tree_sha256=proposal_tree_digest,
        source_checkout_unchanged=source_after_matches(repo_root, source_before),
        verification_authority=_verification_authority(checks),
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


def _finish_internal_failure(
    *,
    run_id: str,
    layout,
    repo_root: str,
    base_commit: str,
    started_wall,
    prime_version_text: str | None,
    verification_authority: str,
    error_code: str,
) -> dict:
    """Fail closed after admission while still finalizing a minimal receipt."""
    from .evidence import sha256_file

    def digest(path: str) -> str:
        try:
            return sha256_file(path) if Path(path).is_file() else ""
        except OSError:
            return ""

    observation = PrimeObservation(
        launched=False,
        exit_code=None,
        session_id=None,
        saw_agent_start=False,
        saw_agent_end=False,
        event_count=0,
        error_code=error_code,
    )
    source = source_checkout_identity(repo_root, base_commit)
    receipt = build_receipt(
        run_id=run_id,
        status=Status.FAILED,
        request_sha256=_request_sha256_of(layout),
        prime_agent_version=_prime_version_string(prime_version_text),
        platform_record=default_platform_record(),
        repository_root=repo_root,
        base_commit=base_commit,
        candidate_path=candidate_display_path(layout),
        candidate_head=_candidate_head(layout),
        candidate_tree_sha256=candidate_tree_digest_safe(layout),
        tracked_patch_sha256="",
        prime_events_sha256=digest(layout.prime_events),
        prime_stderr_sha256=digest(layout.prime_stderr),
        changed_paths=ChangedPaths(),
        observation=observation,
        checks=[],
        started_at=_iso_from_wall(started_wall),
        candidate_stability=CandidateStability.UNKNOWN,
        prime_command_identity=prime_command_identity([]),
        source_checkout_unchanged=bool(
            source["head"] == base_commit and source["clean"]
        ),
        verification_authority=verification_authority,
        error_code=error_code,
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
        changed_paths=ChangedPaths(),
        check_rows=[],
        prime_final_text="",
        error_code=error_code,
        message="the run failed after admission; a minimal review receipt was finalized.",
    )


def _finish_uncertain(
    *,
    run_id: str,
    layout,
    repo_root: str,
    base_commit: str,
    observation: PrimeObservation,
    reason_code: str,
    started_monotonic: float,
    started_wall=None,
    prime_version_text: str | None = None,
    argv_record: list[str] | None = None,
    source_before: dict | None = None,
    verification_authority: str = "NONE",
) -> dict:
    """UNCERTAIN: no checks, preserve everything, never retry."""
    changed_paths = ChangedPaths()
    digests = {"tracked_patch": "", "events": "", "stderr": ""}
    tree_digest = ""
    with contextlib.suppress(Exception):
        changed_paths, digests, tree_digest = _collect_evidence(layout, base_commit)
    receipt = build_receipt(
        run_id=run_id,
        status=Status.UNCERTAIN,
        request_sha256=_request_sha256_of(layout),
        prime_agent_version=_prime_version_string(prime_version_text),
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
        started_at=_iso_from_wall(started_wall),
        candidate_stability=CandidateStability.UNKNOWN,
        prime_command_identity=prime_command_identity(argv_record or []),
        source_checkout_unchanged=(
            source_after_matches(repo_root, source_before)
            if source_before is not None
            else None
        ),
        verification_authority=verification_authority,
        error_code=reason_code,
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
    observation: PrimeObservation,
    error_code: str,
    started_monotonic: float,
    started_wall=None,
    prime_version_text: str | None = None,
    argv_record: list[str] | None = None,
    source_before: dict | None = None,
    verification_authority: str = "NONE",
) -> dict:
    """FAILED: known failure after admission; candidate may hold partial edits."""
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
        prime_agent_version=_prime_version_string(prime_version_text),
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
        started_at=_iso_from_wall(started_wall),
        candidate_stability=CandidateStability.UNKNOWN,
        prime_command_identity=prime_command_identity(argv_record or []),
        source_checkout_unchanged=(
            source_after_matches(repo_root, source_before)
            if source_before is not None
            else None
        ),
        verification_authority=verification_authority,
        error_code=error_code,
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


def _verification_authority(checks: list[dict]) -> str:
    return "MODEL_PROPOSED" if checks else "NONE"


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


def _prime_version_string(version_text: str | None) -> str | None:
    if not version_text:
        return None
    return " ".join(version_text.strip().split())[:64]


def _write_status(layout, status_value: str) -> None:
    from .workspace import atomic_write_text

    atomic_write_text(layout.status_txt, status_value + "\n")


def _iso_from_wall(started_wall) -> str:
    """Render the recorded wall-clock start time (real run start, not now)."""
    from datetime import datetime

    if isinstance(started_wall, datetime):
        return started_wall.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    # Defensive fallback for legacy callers that still pass a monotonic float:
    # convert through a wall-clock anchor captured at call time.
    return utc_now_iso()


def prime_command_identity(argv: list[str]) -> dict:
    """Hash the effective argv and executable/script files without path leakage."""
    import hashlib

    from .evidence import sha256_file

    argv_bytes = json.dumps(
        argv, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    identity = {
        "argv_sha256": hashlib.sha256(argv_bytes).hexdigest(),
        "executable_name": "",
        "executable_sha256": "",
        "script_name": "",
        "script_sha256": "",
    }
    if argv:
        executable = Path(argv[0])
        identity["executable_name"] = executable.name
        if executable.is_file():
            identity["executable_sha256"] = sha256_file(str(executable))
    for token in argv[1:]:
        if token.startswith("@"):
            continue
        candidate = Path(token)
        if candidate.is_file():
            identity["script_name"] = candidate.name
            identity["script_sha256"] = sha256_file(str(candidate))
            break
    return identity


def candidate_tree_digest_safe(layout) -> str:
    """Tree digest that never turns evidence collection into a crash.

    Bound overruns and unreadable trees degrade to an empty digest (absence is
    visible in the receipt; it is not silently equated with any real value).
    """
    from .evidence import TreeDigestLimitExceeded, candidate_tree_digest

    try:
        return candidate_tree_digest(layout.candidate)
    except (TreeDigestLimitExceeded, OSError):
        return ""


def source_checkout_identity(repository_path: str, base_commit: str) -> dict:
    """Pre-run snapshot of the active checkout's identity (HEAD + cleanliness)."""
    head = ""
    clean = False
    detail = ""
    try:
        from .validation import run_git

        head = run_git(["rev-parse", "HEAD"], cwd=repository_path).strip()
    except Exception:
        head = ""
    try:
        from .workspace import source_checkout_unchanged

        clean, detail = source_checkout_unchanged(repository_path, base_commit)
    except Exception:
        clean = False
    return {"head": head, "clean": clean, "detail": detail}


def source_after_matches(
    repository_path: str, before: dict | None
) -> bool | None:
    """True when the active checkout still matches its pre-run identity."""
    if not before:
        return None
    after = source_checkout_identity(repository_path, "")
    return bool(after["head"] == before["head"] and after["clean"])


def utc_now_iso() -> str:
    from .receipt import utc_now_iso as _now

    return _now()


def candidate_display_path(layout) -> str:
    return os.fspath(Path(layout.candidate).resolve(strict=False))
