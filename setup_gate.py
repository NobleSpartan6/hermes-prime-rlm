"""Operator-only setup and readiness surfaces for Prime Agent.

Registration is import-safe: this module performs no subprocess, network, or
filesystem work until an operator invokes a command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

RUNTIME_LOCK_MAX_BYTES = 256 * 1024
RUNTIME_ASSET_MAX_BYTES = 64 * 1024 * 1024
PRIME_COMMIT = "514633727bf26d74f39f3119c2b0e31a5ceb2a9d"


class RuntimeLockError(ValueError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeLockError(
                "RUNTIME_LOCK_DUPLICATE_KEY", f"duplicate runtime-lock key: {key}"
            )
        result[key] = value
    return result


def load_runtime_lock(path: Path) -> dict:
    """Load and validate immutable acquisition metadata before network use."""
    payload = path.read_bytes()
    if len(payload) > RUNTIME_LOCK_MAX_BYTES:
        raise RuntimeLockError("RUNTIME_LOCK_TOO_LARGE", "runtime lock exceeded its bound")
    try:
        lock = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise RuntimeLockError("RUNTIME_LOCK_NOT_UTF8", "runtime lock was not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeLockError(
            "RUNTIME_LOCK_INVALID_JSON", "runtime lock was invalid JSON"
        ) from exc
    if not isinstance(lock, dict) or lock.get("schema_version") != 1:
        raise RuntimeLockError("RUNTIME_LOCK_SCHEMA", "unsupported runtime-lock schema")
    if lock.get("prime_version") != "0.8.1" or lock.get("prime_commit") != PRIME_COMMIT:
        raise RuntimeLockError("RUNTIME_LOCK_PRIME_IDENTITY", "Prime identity was not exact")
    origins = lock.get("allowed_origins")
    assets = lock.get("prime_assets")
    if (
        not isinstance(origins, list)
        or not origins
        or not all(isinstance(origin, str) and origin.startswith("https://") for origin in origins)
        or not isinstance(assets, list)
        or not assets
    ):
        raise RuntimeLockError("RUNTIME_LOCK_SCHEMA", "runtime lock omitted origins or assets")
    names: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise RuntimeLockError("RUNTIME_LOCK_SCHEMA", "runtime asset was not an object")
        name = asset.get("name")
        url = asset.get("url")
        digest = asset.get("sha256")
        size = asset.get("size")
        if not isinstance(name, str) or not name or name in names:
            raise RuntimeLockError(
                "RUNTIME_LOCK_DUPLICATE_ASSET",
                "runtime asset names must be unique",
            )
        names.add(name)
        if not isinstance(url, str):
            raise RuntimeLockError("RUNTIME_LOCK_SCHEMA", "runtime asset URL was invalid")
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in origins:
            raise RuntimeLockError(
                "RUNTIME_LOCK_ORIGIN_NOT_ALLOWED", f"runtime asset origin was not allowed: {origin}"
            )
        if parts.scheme != "https" or not parts.path.endswith(f"/{name}"):
            raise RuntimeLockError(
                "RUNTIME_LOCK_URL_MISMATCH",
                "runtime asset URL did not bind its name",
            )
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise RuntimeLockError("RUNTIME_LOCK_DIGEST", "runtime asset digest was invalid")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 < size <= RUNTIME_ASSET_MAX_BYTES
        ):
            raise RuntimeLockError("RUNTIME_LOCK_SIZE", "runtime asset size was invalid")
    return lock


@dataclass(frozen=True)
class ReadinessObservation:
    """Non-secret facts observed from the effective Prime command."""

    prime_version: str
    provider: str
    model_id: str
    available_models: tuple[tuple[str, str], ...]
    auto_retry_disabled: bool
    kernel_proven: bool
    residual_processes: int


class ReadinessProbeError(RuntimeError):
    """An ambiguous readiness boundary that must remain UNCERTAIN."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _effective_command_sha256(command_prefix: list[str]) -> str:
    payload = json.dumps(
        command_prefix, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _configured_runtime(ctx) -> dict:
    from .tools import resolve_prime_command

    getter = getattr(ctx, "get_config", None)
    raw = getter("prime_runtime") if callable(getter) else None
    if isinstance(raw, dict):
        return {
            "command": list(raw.get("command") or resolve_prime_command(ctx)),
            "kernel_python": raw.get("kernel_python"),
        }
    legacy_kernel = getter("prime_agent_kernel_python") if callable(getter) else None
    return {
        "command": resolve_prime_command(ctx),
        "kernel_python": legacy_kernel,
    }


def build_setup_plan(
    ctx,
    *,
    proposed_command: list[str],
    proposed_kernel_python: str | None = None,
) -> dict:
    """Build an immutable, non-secret config-repair plan without writing state."""
    from .schemas import validate_command_prefix

    current_runtime = _configured_runtime(ctx)
    current_command = current_runtime["command"]
    proposed = validate_command_prefix(proposed_command)
    if proposed_kernel_python is not None:
        kernel_path = Path(proposed_kernel_python)
        if not kernel_path.is_absolute() or not kernel_path.is_file():
            raise RuntimeLockError(
                "KERNEL_PYTHON_INVALID",
                "proposed kernel Python was not an existing absolute file",
            )
        proposed_kernel_python = str(kernel_path.resolve(strict=True))
    proposed_runtime = {
        "command": proposed,
        "kernel_python": proposed_kernel_python,
    }
    body = {
        "schema_version": 1,
        "current_prime_agent_command": current_command,
        "proposed_prime_agent_command": proposed,
        "current_prime_runtime": current_runtime,
        "proposed_prime_runtime": proposed_runtime,
        "mutations": ["plugins.entries.prime-rlm.settings.prime_runtime"],
        "network_required": False,
        "model_calls": 0,
        "persistent_sessions": False,
        "daemon_adoption": False,
    }
    return {**body, "plan_sha256": _canonical_sha256(body)}


def _plan_digest(plan: dict) -> str:
    body = {key: value for key, value in plan.items() if key != "plan_sha256"}
    return _canonical_sha256(body)


def _public_setup_plan(plan: dict) -> dict:
    """Expose consent metadata without credential-bearing command tokens."""
    current_runtime = plan.get("current_prime_runtime") or {}
    proposed_runtime = plan.get("proposed_prime_runtime") or {}
    return {
        "schema_version": plan["schema_version"],
        "plan_sha256": plan["plan_sha256"],
        "mutations": list(plan["mutations"]),
        "network_required": plan["network_required"],
        "model_calls": plan["model_calls"],
        "persistent_sessions": plan["persistent_sessions"],
        "daemon_adoption": plan["daemon_adoption"],
        "current_command_sha256": _effective_command_sha256(
            list(current_runtime.get("command") or [])
        ),
        "proposed_command_sha256": _effective_command_sha256(
            list(proposed_runtime.get("command") or [])
        ),
        "current_kernel_configured": bool(current_runtime.get("kernel_python")),
        "proposed_kernel_configured": bool(proposed_runtime.get("kernel_python")),
    }


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _start_setup_transaction(ctx, plan: dict) -> tuple[str, Path]:
    from .tools import resolve_plugin_data_dir

    transaction_id = str(uuid.uuid4())
    transaction_dir = resolve_plugin_data_dir(ctx) / "setup-transactions" / transaction_id
    _atomic_write_json(
        transaction_dir / "journal.json",
        {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "phase": "COMMIT_CONFIG",
            "plan_sha256": plan["plan_sha256"],
            "automatic_retry_allowed": False,
        },
    )
    return transaction_id, transaction_dir


def _restore_runtime(setter, ctx, planned_runtime: dict) -> bool:
    try:
        setter("prime_runtime", dict(planned_runtime))
        return _configured_runtime(ctx) == planned_runtime
    except Exception:
        return False


def _finish_setup_transaction(
    transaction_id: str,
    transaction_dir: Path,
    plan: dict,
    report: dict,
) -> tuple[str, str]:
    receipt = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "created_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "state": report["state"],
        "ready": report.get("ready") is True,
        "reason_code": report.get("reason_code"),
        "plan_sha256": plan["plan_sha256"],
        "effective_command_sha256": _effective_command_sha256(
            list(plan["proposed_prime_agent_command"])
        ),
        "runtime_provenance": "EXTERNAL_OBSERVED",
        "kernel_provenance": "PLUGIN_MANAGED_NOT_FULLY_LOCKED",
        "automatic_retry_allowed": False,
        "model_calls": 0,
        "model_tokens": 0,
        "model_cost": 0,
        "residual_owned_processes": 0,
        "provider_canary": "NOT_RUN",
        "release_ready": False,
        "state_ownership": {
            "l0_model_provider": "prime-native",
            "l1_task_acceptance": "hermes",
            "l2_repl": "prime-invocation-bounded",
            "l2_recursive_workers": "prime-bounded-accounted",
            "l3_prime_sessions": "disabled",
            "l3_refinement": "disabled",
            "l3_skills_memories_prompts": "not-mutated",
            "candidate_acceptance": "hermes-human-only",
        },
    }
    receipt_sha256 = _canonical_sha256(receipt)
    receipt_path = transaction_dir / "receipt.json"
    _atomic_write_json(
        receipt_path,
        {"receipt": receipt, "receipt_sha256": receipt_sha256},
    )
    _atomic_write_json(
        transaction_dir / "journal.json",
        {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "phase": report["state"],
            "plan_sha256": plan["plan_sha256"],
            "receipt_sha256": receipt_sha256,
            "automatic_retry_allowed": False,
        },
    )
    return os.fspath(receipt_path), receipt_sha256


def commit_setup_plan(
    ctx,
    plan: dict,
    *,
    accepted_plan_digest: str | None,
    verify_fn,
) -> dict:
    """Commit one validated config repair and roll back unless readiness is proven."""
    if accepted_plan_digest is None:
        return {
            "ready": False,
            "state": "CANCELLED",
            "reason_code": "CONSENT_NOT_GRANTED",
            "automatic_retry_allowed": False,
        }
    expected_digest = _plan_digest(plan)
    if plan.get("plan_sha256") != expected_digest or accepted_plan_digest != expected_digest:
        return {
            "ready": False,
            "state": "NOT_READY",
            "reason_code": "PLAN_DIGEST_MISMATCH",
            "automatic_retry_allowed": False,
        }
    current = _configured_runtime(ctx)
    planned_current = plan.get("current_prime_runtime")
    if current != planned_current:
        return {
            "ready": False,
            "state": "UNCERTAIN_SETUP",
            "reason_code": "CONFIG_CHANGED_SINCE_PLAN",
            "automatic_retry_allowed": False,
        }
    proposed = dict(plan["proposed_prime_runtime"])
    setter = getattr(ctx, "set_config", None)
    if not callable(setter):
        return {
            "ready": False,
            "state": "NOT_READY",
            "reason_code": "CONFIG_WRITE_UNAVAILABLE",
            "automatic_retry_allowed": False,
        }
    try:
        transaction_id, transaction_dir = _start_setup_transaction(ctx, plan)
    except (OSError, PermissionError):
        return {
            "ready": False,
            "state": "UNCERTAIN_SETUP",
            "reason_code": "SETUP_JOURNAL_UNAVAILABLE",
            "automatic_retry_allowed": False,
        }
    try:
        setter("prime_runtime", proposed)
        if _configured_runtime(ctx) != proposed:
            raise OSError("plugin config read-back did not match")
        report = verify_fn(ctx)
        if report.get("state") == "READY" and report.get("ready") is True:
            receipt_path, receipt_sha256 = _finish_setup_transaction(
                transaction_id, transaction_dir, plan, report
            )
            return {
                **report,
                "plan_sha256": expected_digest,
                "receipt_path": receipt_path,
                "receipt_sha256": receipt_sha256,
            }
        rollback_proven = _restore_runtime(setter, ctx, planned_current)
        state = "UNCERTAIN_SETUP" if report.get("state") == "UNCERTAIN" else "NOT_READY"
        failed_report = {
            **report,
            "ready": False,
            "state": state,
            "reason_code": report.get("reason_code") or "POST_COMMIT_READINESS_FAILED",
            "automatic_retry_allowed": False,
        }
        if not rollback_proven:
            failed_report.update(
                state="UNCERTAIN_SETUP",
                reason_code="CONFIG_ROLLBACK_UNPROVEN",
            )
        receipt_path, receipt_sha256 = _finish_setup_transaction(
            transaction_id, transaction_dir, plan, failed_report
        )
        return {
            **failed_report,
            "receipt_path": receipt_path,
            "receipt_sha256": receipt_sha256,
        }
    except (KeyboardInterrupt, SystemExit):
        rollback_proven = _restore_runtime(setter, ctx, planned_current)
        failed_report = {
            "ready": False,
            "state": "UNCERTAIN_SETUP",
            "reason_code": (
                "POST_COMMIT_INTERRUPTED"
                if rollback_proven
                else "CONFIG_ROLLBACK_UNPROVEN"
            ),
            "automatic_retry_allowed": False,
        }
        receipt_path, receipt_sha256 = _finish_setup_transaction(
            transaction_id, transaction_dir, plan, failed_report
        )
        return {
            **failed_report,
            "receipt_path": receipt_path,
            "receipt_sha256": receipt_sha256,
        }
    except PermissionError:
        try:
            unchanged = _configured_runtime(ctx) == planned_current
        except Exception:
            unchanged = False
        rollback_proven = unchanged or _restore_runtime(setter, ctx, planned_current)
        failed_report = (
            {
                "ready": False,
                "state": "NOT_READY",
                "reason_code": "SETUP_CONFIG_MANAGED",
                "automatic_retry_allowed": False,
            }
            if rollback_proven
            else {
                "ready": False,
                "state": "UNCERTAIN_SETUP",
                "reason_code": "CONFIG_ROLLBACK_UNPROVEN",
                "automatic_retry_allowed": False,
            }
        )
        receipt_path, receipt_sha256 = _finish_setup_transaction(
            transaction_id, transaction_dir, plan, failed_report
        )
        return {
            **failed_report,
            "receipt_path": receipt_path,
            "receipt_sha256": receipt_sha256,
        }
    except OSError:
        rollback_proven = _restore_runtime(setter, ctx, planned_current)
        failed_report = {
            "ready": False,
            "state": "UNCERTAIN_SETUP",
            "reason_code": (
                "CONFIG_COMMIT_UNCERTAIN"
                if rollback_proven
                else "CONFIG_ROLLBACK_UNPROVEN"
            ),
            "automatic_retry_allowed": False,
        }
        receipt_path, receipt_sha256 = _finish_setup_transaction(
            transaction_id, transaction_dir, plan, failed_report
        )
        return {
            **failed_report,
            "receipt_path": receipt_path,
            "receipt_sha256": receipt_sha256,
        }
    except Exception:
        rollback_proven = _restore_runtime(setter, ctx, planned_current)
        failed_report = {
            "ready": False,
            "state": "UNCERTAIN_SETUP",
            "reason_code": (
                "POST_COMMIT_VERIFIER_ERROR"
                if rollback_proven
                else "CONFIG_ROLLBACK_UNPROVEN"
            ),
            "automatic_retry_allowed": False,
        }
        receipt_path, receipt_sha256 = _finish_setup_transaction(
            transaction_id, transaction_dir, plan, failed_report
        )
        return {
            **failed_report,
            "receipt_path": receipt_path,
            "receipt_sha256": receipt_sha256,
        }


def inspect_command_readiness(
    command_prefix: list[str], *, kernel_python: str | None = None, probe_fn=None
) -> dict:
    """Evaluate one exact command prefix without mutating plugin config."""
    if probe_fn is None:
        def probe_fn(command):
            return _probe_effective_command(command, kernel_python=kernel_python)
    command_sha256 = _effective_command_sha256(command_prefix)
    try:
        observation = probe_fn(list(command_prefix))
    except ReadinessProbeError as exc:
        return {
            "ready": False,
            "state": "UNCERTAIN",
            "reason_code": exc.error_code,
            "prime_version": None,
            "provider": None,
            "model": None,
            "effective_command_sha256": command_sha256,
            "automatic_retry_allowed": False,
        }
    active_model = (observation.provider, observation.model_id)
    reason_code = None
    if observation.prime_version != "0.8.1":
        reason_code = "UNSUPPORTED_PRIME_VERSION"
    elif not all(part.strip() for part in active_model):
        reason_code = "MODEL_IDENTITY_MISSING"
    elif active_model not in observation.available_models:
        reason_code = "EFFECTIVE_MODEL_NOT_AVAILABLE"
    elif not observation.auto_retry_disabled:
        reason_code = "AUTO_RETRY_NOT_DISABLED"
    elif not observation.kernel_proven:
        reason_code = "KERNEL_NOT_PROVEN"
    elif observation.residual_processes != 0:
        reason_code = "RESIDUAL_PROCESS_OBSERVED"

    return {
        "ready": reason_code is None,
        "state": "READY" if reason_code is None else "NOT_READY",
        "reason_code": reason_code,
        "prime_version": observation.prime_version,
        "provider": observation.provider,
        "model": observation.model_id,
        "effective_command_sha256": command_sha256,
        "automatic_retry_allowed": False,
    }


def inspect_readiness(ctx, *, probe_fn=None) -> dict:
    """Evaluate readiness using the exact operator-configured command prefix."""
    runtime = _configured_runtime(ctx)
    report = inspect_command_readiness(
        runtime["command"],
        kernel_python=runtime.get("kernel_python"),
        probe_fn=probe_fn,
    )
    if (
        os.name == "nt"
        and not runtime.get("kernel_python")
        and report.get("state") == "READY"
    ):
        return {
            **report,
            "ready": False,
            "state": "NOT_READY",
            "reason_code": "KERNEL_CONFIG_NOT_COMMITTED",
        }
    return report


def _without_model_override(command_prefix: list[str]) -> list[str]:
    repaired: list[str] = []
    skip_next = False
    for argument in command_prefix:
        if skip_next:
            skip_next = False
            continue
        if argument == "--model":  # noqa: S105 - CLI flag, not a password
            skip_next = True
            continue
        if argument.startswith("--model="):
            continue
        repaired.append(argument)
    return repaired


def run_setup(
    ctx,
    *,
    inspect_current_fn=inspect_readiness,
    inspect_command_fn=inspect_command_readiness,
    consent_fn,
) -> dict:
    """Repair a stale model override through a default-negative transaction."""
    current_report = inspect_current_fn(ctx)
    if current_report.get("state") == "READY":
        return current_report
    repairable = {
        "EFFECTIVE_MODEL_NOT_AVAILABLE",
        "KERNEL_NOT_PROVEN",
        "KERNEL_CONFIG_NOT_COMMITTED",
    }
    if current_report.get("reason_code") not in repairable:
        return current_report
    current_runtime = _configured_runtime(ctx)
    current_command = current_runtime["command"]
    proposed_command = (
        _without_model_override(current_command)
        if current_report.get("reason_code") == "EFFECTIVE_MODEL_NOT_AVAILABLE"
        else list(current_command)
    )
    proposed_kernel = current_runtime.get("kernel_python")
    if not proposed_kernel:
        proposed_kernel = next(
            (
                str(path.resolve())
                for path in _kernel_python_candidates(ctx)
                if path.is_file() and _probe_kernel_health(str(path.resolve()))
            ),
            None,
        )
    if not proposed_command or not proposed_kernel:
        return {
            "ready": False,
            "state": "NOT_READY",
            "reason_code": "SETUP_REPAIR_UNAVAILABLE",
            "automatic_retry_allowed": False,
        }
    proposed_report = inspect_command_fn(
        proposed_command, kernel_python=proposed_kernel
    )
    if proposed_report.get("state") != "READY" or proposed_report.get("ready") is not True:
        return proposed_report
    plan = build_setup_plan(
        ctx,
        proposed_command=proposed_command,
        proposed_kernel_python=proposed_kernel,
    )
    accepted_digest = consent_fn(plan)
    return commit_setup_plan(
        ctx,
        plan,
        accepted_plan_digest=accepted_digest,
        verify_fn=inspect_current_fn,
    )


def configure_cli(parser: argparse.ArgumentParser) -> None:
    """Register ``hermes prime`` subcommands without executing setup work."""
    subcommands = parser.add_subparsers(dest="prime_action")
    setup_parser = subcommands.add_parser(
        "setup", help="Plan or execute transactional Prime setup"
    )
    setup_parser.add_argument("--check", action="store_true")
    setup_parser.add_argument("--json", action="store_true", dest="json_output")
    setup_parser.add_argument("--non-interactive", action="store_true")
    setup_parser.add_argument("--plan-digest", default="")
    setup_parser.add_argument("--accept-config", action="store_true")
    doctor_parser = subcommands.add_parser(
        "doctor", help="Inspect Prime integration readiness"
    )
    doctor_parser.add_argument("--json", action="store_true", dest="json_output")
    doctor_parser.add_argument("--fix", action="store_true")


def _probe_version(command_prefix: list[str]) -> str:
    from .schemas import ValidationError
    from .validation import probe_prime_version

    try:
        version, _output = probe_prime_version(command_prefix)
    except ValidationError as exc:
        raise ReadinessProbeError(exc.error_code) from exc
    major, minor, patch = version
    return f"{major}.{minor}.{patch}"


def _probe_rpc_state(
    command_prefix: list[str], *, kernel_python: str | None = None
) -> dict:
    from .prime_rpc import probe_prime_rpc
    from .prime_rpc_process import build_rpc_environment

    env = build_rpc_environment(dict(os.environ))
    if kernel_python:
        env["PRIME_AGENT_KERNEL_PYTHON"] = kernel_python
    with tempfile.TemporaryDirectory(prefix="prime-rlm-readiness-") as candidate:
        result, _argv = probe_prime_rpc(
            command_prefix,
            candidate,
            timeout_seconds=30,
            env=env,
        )
    if not result.get("stream_valid"):
        raise ReadinessProbeError(str(result.get("error_code") or "RPC_READINESS_INVALID"))
    return result


def _platform_arch_tag() -> str:
    machine = platform.machine().lower()
    arch = "x64" if machine in {"amd64", "x86_64"} else "arm64" if machine in {
        "arm64",
        "aarch64",
    } else machine
    system = "win32" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"
    return f"{system}-{arch}"


def _kernel_python_candidates(ctx=None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    explicit = os.environ.get("PRIME_AGENT_KERNEL_PYTHON")
    if explicit:
        candidates.append(Path(explicit))
    venv = os.environ.get("PRIME_AGENT_KERNEL_VENV")
    roots = [Path(venv)] if venv else [Path.home() / ".prime" / "agent" / "kernel-venv"]
    if ctx is not None:
        from .tools import resolve_plugin_data_dir

        roots.insert(
            0,
            resolve_plugin_data_dir(ctx)
            / "runtime"
            / "kernel"
            / "prime-0.8.1"
            / _platform_arch_tag(),
        )
    for root in roots:
        candidates.extend((root / "Scripts" / "python.exe", root / "bin" / "python"))
    return tuple(dict.fromkeys(candidates))


def _probe_kernel_health(kernel_python: str | None = None) -> bool:
    from .platform_runtime import SpawnSpec, spawn_and_wait
    from .prime_rpc_process import build_rpc_environment
    from .schemas import RPC_KERNEL_HEALTH_MARKER

    selected = Path(kernel_python) if kernel_python else next(
        (path for path in _kernel_python_candidates() if path.is_file()), None
    )
    if selected is None or not selected.is_file():
        return False
    code = (
        "import rlm; assert callable(rlm); "
        f"print({RPC_KERNEL_HEALTH_MARKER!r})"
    )
    with tempfile.TemporaryDirectory(prefix="prime-rlm-kernel-probe-") as temp:
        stdout_path = os.fspath(Path(temp) / "stdout")
        stderr_path = os.fspath(Path(temp) / "stderr")
        spec = SpawnSpec(
            argv=[os.fspath(selected), "-c", code],
            cwd=temp,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            env=build_rpc_environment(dict(os.environ)),
            stdout_max_bytes=64 * 1024,
            stderr_max_bytes=64 * 1024,
        )
        exit_code, ambiguous = spawn_and_wait(spec, 30)
        if ambiguous or exit_code != 0:
            return False
        output = Path(stdout_path).read_text(encoding="utf-8", errors="replace")
    return RPC_KERNEL_HEALTH_MARKER in output


def _probe_effective_command(
    command_prefix: list[str], *, kernel_python: str | None = None
) -> ReadinessObservation:
    version = _probe_version(command_prefix)
    rpc = _probe_rpc_state(command_prefix, kernel_python=kernel_python)
    return ReadinessObservation(
        prime_version=version,
        provider=str(rpc["provider"]),
        model_id=str(rpc["model"]),
        available_models=tuple(rpc["available_models"]),
        auto_retry_disabled=rpc.get("auto_retry_disabled") is True,
        kernel_proven=_probe_kernel_health(kernel_python),
        residual_processes=0,
    )


def _print_human_report(report: dict) -> None:
    print(f"Prime RLM setup: {report['state']}")
    if report.get("reason_code"):
        print(f"Reason: {report['reason_code']}")


def _report_exit_code(report: dict) -> int:
    if report.get("state") == "READY":
        return 0
    if report.get("state") in {"UNCERTAIN", "UNCERTAIN_SETUP"}:
        return 3
    return 1


def _cli_consent(args: argparse.Namespace):
    def consent(plan: dict) -> str | None:
        if getattr(args, "json_output", False):
            print(
                json.dumps(
                    {"setup_plan": _public_setup_plan(plan)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print("Prime RLM setup plan")
            print(f"Plan SHA-256: {plan['plan_sha256']}")
            print(f"Mutations: {', '.join(plan['mutations'])}")
            print("Model calls: 0")
            print("Persistent sessions: disabled")
        if getattr(args, "non_interactive", False):
            if not getattr(args, "accept_config", False):
                return None
            return getattr(args, "plan_digest", "") or None
        response = input("Commit this exact non-secret config repair? [y/N] ")
        return plan["plan_sha256"] if response.strip().casefold() in {"y", "yes"} else None

    return consent


def handle_cli(args: argparse.Namespace, *, ctx) -> int:
    """Dispatch read-only Doctor/check; setup mutation uses a separate engine."""
    action = getattr(args, "prime_action", None)
    if action not in {"setup", "doctor"}:
        print("Usage: hermes prime {setup|doctor}")
        return 2
    read_only = action == "doctor" and not getattr(args, "fix", False)
    read_only = read_only or (action == "setup" and getattr(args, "check", False))
    if read_only:
        report = inspect_readiness(ctx)
        if getattr(args, "json_output", False):
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        else:
            _print_human_report(report)
        return _report_exit_code(report)
    report = run_setup(ctx, consent_fn=_cli_consent(args))
    if getattr(args, "json_output", False):
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        _print_human_report(report)
    return _report_exit_code(report)


def handle_slash(raw_args: str, *, ctx) -> str:
    """Keep gateway slash usage read-only; local setup remains a CLI transaction."""
    del raw_args, ctx
    return "Run `hermes prime setup` in a local terminal to configure Prime RLM."


def register_operator_commands(ctx) -> None:
    """Register operator surfaces without adding model-facing tools."""

    def cli_handler(args: argparse.Namespace) -> int:
        return handle_cli(args, ctx=ctx)

    def slash_handler(raw_args: str) -> str:
        return handle_slash(raw_args, ctx=ctx)

    ctx.register_cli_command(
        name="prime",
        help="Set up and inspect the Prime RLM integration",
        setup_fn=configure_cli,
        handler_fn=cli_handler,
        description="Transactional setup and readiness for hermes-prime-rlm.",
    )
    ctx.register_command(
        "prime-setup",
        slash_handler,
        description="Show how to run the local Prime setup transaction.",
    )


__all__ = [
    "ReadinessObservation",
    "ReadinessProbeError",
    "build_setup_plan",
    "commit_setup_plan",
    "configure_cli",
    "handle_cli",
    "handle_slash",
    "inspect_command_readiness",
    "inspect_readiness",
    "register_operator_commands",
    "run_setup",
]
