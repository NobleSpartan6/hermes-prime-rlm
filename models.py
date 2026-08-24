"""Dataclasses and enums shared across the plugin."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Status(enum.StrEnum):
    """Terminal run statuses. See README for precise semantics."""

    VERIFIED = "VERIFIED"
    COMPLETED_UNVERIFIED = "COMPLETED_UNVERIFIED"
    FAILED_VERIFICATION = "FAILED_VERIFICATION"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class CheckStatus(enum.StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    LAUNCH_ERROR = "launch_error"


class CandidateStability(enum.StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    """Outcome of one host-executed verification check."""

    name: str
    status: CheckStatus
    exit_code: int | None
    duration_ms: int
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
        }


@dataclass
class PrimeObservation:
    """What the plugin independently observed about the Prime process/stream.

    Textual claims inside ``final_text`` carry no verification authority.
    """

    launched: bool
    exit_code: int | None
    session_id: str | None
    saw_agent_start: bool
    saw_agent_end: bool
    event_count: int
    final_text: str = ""
    timed_out: bool = False
    stream_valid: bool = False
    error_code: str | None = None


@dataclass
class ChangedPaths:
    """Paths the candidate changed relative to the base commit."""

    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "modified": list(self.modified),
            "deleted": list(self.deleted),
            "renamed": list(self.renamed),
            "untracked": list(self.untracked),
        }


@dataclass
class RunPaths:
    """Resolved filesystem layout of one run directory."""

    run_dir: str
    request_json: str
    prime_task_md: str
    candidate: str
    prime_events: str
    prime_stderr: str
    prime_version_txt: str
    tracked_patch: str
    status_txt: str
    checks_dir: str
    receipt_json: str
