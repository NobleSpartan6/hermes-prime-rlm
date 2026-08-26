"""Receipt construction and atomic, deterministic persistence (schema v1).

The canonical payload is JSON with sorted keys and compact separators; the
written envelope is ``{"receipt": payload, "receipt_sha256": digest}`` so the
digest never covers itself.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import sys
from datetime import UTC, datetime

from .models import CandidateStability, ChangedPaths, CheckResult, PrimeObservation, Status

PLUGIN_VERSION = "0.1.1"
RECEIPT_SCHEMA_VERSION = 1

LIMITATIONS = [
    "not_a_security_sandbox",
    "verification_is_limited_to_recorded_checks",
    "candidate_quiescence_not_proven",
    "evidence_not_independently_custodied",
    "receipt_unsigned",
    "checks_may_be_model_proposed",
    "candidate_not_applied",
    "no_automatic_retry",
]


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def canonical_payload_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def digest_of(payload: dict) -> str:
    return hashlib.sha256(canonical_payload_bytes(payload)).hexdigest()


def build_receipt(
    *,
    run_id: str,
    status: Status,
    request_sha256: str,
    prime_agent_version: str | None,
    platform_record: dict,
    repository_root: str,
    base_commit: str,
    candidate_path: str,
    candidate_head: str,
    candidate_tree_sha256: str,
    tracked_patch_sha256: str,
    prime_events_sha256: str,
    prime_stderr_sha256: str,
    changed_paths: ChangedPaths,
    observation: PrimeObservation,
    checks: list[CheckResult],
    started_at: str,
    candidate_stability: CandidateStability,
    prime_argv: list[str] | None = None,
    prime_command_identity: dict | None = None,
    proposal_tree_sha256: str = "",
    source_checkout_unchanged: bool | None = None,
    verification_authority: str = "NONE",
    error_code: str | None = None,
) -> dict:
    """Assemble the schema-v1 receipt payload. Never includes env or secrets.

    v0.1.1 additions (still schema 1; all new fields optional so the dashboard
    can render older receipts):
    * ``prime_command_identity`` — SHA-256 identities for the effective argv,
      resolved executable, and first file-valued script argument.
    * ``proposal_tree_sha256`` — candidate tree digest taken BEFORE checks ran,
      distinguishing Prime's proposal from verifier-induced mutation
      (``candidate_tree_sha256`` remains the post-check digest).
    * ``source_checkout_unchanged`` — post-run observational check of the
      active checkout against its pre-run HEAD + cleanliness identity.
    """
    payload: dict = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "run_id": run_id,
        "status": status.value,
        "execution_status": (
            "COMPLETED"
            if status
            in (Status.VERIFIED, Status.COMPLETED_UNVERIFIED, Status.FAILED_VERIFICATION)
            else "UNCERTAIN"
            if status is Status.UNCERTAIN
            else "FAILED"
        ),
        # v0.1.1 does not prove process-tree quiescence; state that explicitly.
        "candidate_status": "UNQUIESCED",
        "verification_status": (
            "PASSED"
            if status is Status.VERIFIED
            else "FAILED"
            if status is Status.FAILED_VERIFICATION
            else "NOT_RUN"
        ),
        "verification_authority": verification_authority,
        "integrity_status": "RECORDED_NOT_REVALIDATED",
        "authenticity_status": "UNSIGNED",
        "acceptance_status": "PENDING",
        "request_sha256": request_sha256,
        "plugin_version": PLUGIN_VERSION,
        "prime_agent_version": prime_agent_version,
        "prime_json_schema_version": 3,
        "platform": platform_record,
        "repository_root": repository_root,
        "base_commit": base_commit,
        "candidate_path": candidate_path,
        "candidate_head": candidate_head,
        "candidate_tree_sha256": candidate_tree_sha256,
        "tracked_patch_sha256": tracked_patch_sha256,
        "prime_events_sha256": prime_events_sha256,
        "prime_stderr_sha256": prime_stderr_sha256,
        "changed_paths": changed_paths.to_dict(),
        "prime": {
            "exit_code": observation.exit_code,
            "session_id": observation.session_id,
            "saw_agent_start": observation.saw_agent_start,
            "saw_agent_end": observation.saw_agent_end,
            "event_count": observation.event_count,
            "final_text": observation.final_text,
        },
        "checks": [check.to_dict() for check in checks],
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        # Descendant quiescence is not proven in v0.1.1, so the legacy fields
        # remain conservative even after a valid direct-child terminal event.
        "candidate_may_have_partial_changes": True,
        "candidate_stability": CandidateStability.UNKNOWN.value,
        "automatic_retry_allowed": False,
        "limitations": list(LIMITATIONS),
    }
    # Backward-compatible parameter only: raw argv is deliberately discarded
    # because operator prefix arguments may contain credentials.
    del prime_argv, candidate_stability
    if prime_command_identity:
        payload["prime_command_identity"] = dict(prime_command_identity)
    if proposal_tree_sha256:
        payload["proposal_tree_sha256"] = proposal_tree_sha256
    if source_checkout_unchanged is not None:
        payload["source_checkout_unchanged"] = bool(source_checkout_unchanged)
    if error_code:
        payload["error_code"] = error_code
    return payload


def default_platform_record() -> dict:
    return {
        "os_name": sys.platform,
        "sys_platform": platform.system(),
        "python_version": platform.python_version(),
    }


def write_receipt_atomic(receipt_payload: dict, receipt_path: str) -> str:
    """Write ``{receipt, receipt_sha256}`` atomically; returns the digest.

    The temp file lives in the final directory; flush → fsync → os.replace so
    a partial final file is never observable.
    """
    envelope = {
        "receipt": receipt_payload,
        "receipt_sha256": digest_of(receipt_payload),
    }
    encoded = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    target = os.fspath(receipt_path)
    import uuid

    tmp = f"{target}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    return envelope["receipt_sha256"]


__all__ = [
    "build_receipt",
    "write_receipt_atomic",
    "canonical_payload_bytes",
    "digest_of",
    "default_platform_record",
    "utc_now_iso",
    "PLUGIN_VERSION",
    "RECEIPT_SCHEMA_VERSION",
]
