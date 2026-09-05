"""Pure, operator-facing form validation and plain-language desktop messages.

No process, filesystem probing, provider call, or Tk import occurs here. The
existing handler remains the authority for Git admission and verification.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

PRESETS = {
    "Python: pytest": ("pytest", "Run the project's pytest suite."),
    "Python: unittest": ("unittest", "Discover and run Python unittest tests."),
    "Node.js: npm test": ("npm", "Run the project's npm test script."),
    "Rust: cargo test": ("cargo", "Run the project's Rust tests."),
    "No verification": ("none", "Create a proposal only. It will NOT count as verified."),
}

# Static messages only: never display raw exceptions, model text, or logs here.
_HELP = {
    "REPOSITORY_DIRTY": "Save and commit or stash your existing changes, then start a new run.",
    "DIRTY_REPOSITORY": "Save and commit or stash your existing changes, then start a new run.",
    "NOT_GIT_REPOSITORY": "Choose a folder that is already a Git repository.",
    "REPOSITORY_ROOT_MISMATCH": "Choose the repository's top-level folder, not a subfolder.",
    "PRIME_COMMAND_NOT_FOUND": "Install Prime Agent 0.8.1, then run: hermes prime setup",
    "UNSUPPORTED_PRIME_VERSION": "This integration requires Prime Agent 0.8.1 exactly.",
    "PRIME_COMMAND_REQUIRED": "Configure Prime from a terminal with: hermes prime setup",
    "KERNEL_NOT_PROVEN": "Repair the Python kernel from a terminal: hermes prime setup",
    "KERNEL_CONFIG_NOT_COMMITTED": "Finish the setup transaction: hermes prime setup",
    "EFFECTIVE_MODEL_NOT_AVAILABLE": (
        "Check your model and login in Prime, then run hermes prime setup."
    ),
    "RPC_TIMEOUT": (
        "The run timed out. Inspect the preserved candidate before starting another run."
    ),
    "DESKTOP_BUSY": "A run is already active. This window does not queue or retry runs.",
    "HISTORY_FULL": (
        "This window's history is full. Finish reviewing, close it, and open a new window."
    ),
    "SESSION_CHANGED_DO_NOT_REPLAY": (
        "The backend changed. Inspect preserved runs; do not replay blindly."
    ),
    "READINESS_FAILED": "Setup could not be checked. Run hermes prime doctor --json in a terminal.",
}


class FormError(ValueError):
    """A deliberately safe message that can be shown directly in the form."""


def build_request(folder: str, goal: str, preset: str, minutes: str,
                  python_executable: str = "") -> dict:
    """Prepare fixed argv lists, never a shell expression or repository script guess."""
    from .schemas import GOAL_MAX_CHARS

    folder = folder.strip()
    if not folder or not Path(folder).is_absolute() or "\x00" in folder:
        raise FormError("Choose an absolute repository folder using Browse.")
    if not goal.strip() or len(goal) > GOAL_MAX_CHARS or "\x00" in goal:
        raise FormError(f"Describe the task in 1 to {GOAL_MAX_CHARS:,} characters.")
    if preset not in PRESETS:
        raise FormError("Choose a verification preset, including No verification when appropriate.")
    if not re.fullmatch(r"[0-9]{1,2}", minutes.strip()):
        raise FormError("Enter a whole number of minutes from 1 to 60.")
    duration = int(minutes)
    if not 1 <= duration <= 60:
        raise FormError("Enter a whole number of minutes from 1 to 60.")
    kind = PRESETS[preset][0]
    interpreter = python_executable.strip() or sys.executable
    if kind in {"pytest", "unittest"} and (
        not Path(interpreter).is_absolute() or "\x00" in interpreter
    ):
        raise FormError(
            "Choose an absolute Python executable, or leave it blank to use Hermes Python."
        )
    argv = {
        "pytest": [interpreter, "-m", "pytest", "-q"],
        "unittest": [interpreter, "-m", "unittest", "discover", "-v"],
        "npm": ["npm", "test"],
        "cargo": ["cargo", "test"],
    }.get(kind)
    return {
        "action": "run", "repository_path": folder, "goal": goal,
        "checks": [] if argv is None else [
            {"name": kind, "argv": argv, "timeout_seconds": 120}
        ],
        "runtime_timeout_seconds": duration * 60,
    }


def approval_text(args: dict) -> str:
    """The exact task, repository, and checks are visible BEFORE any admission."""
    commands = "\n".join(json.dumps(c["argv"], ensure_ascii=False) for c in args["checks"])
    return (
        f"Repository: {args['repository_path']}\n\nTask:\n{args['goal']}\n\n"
        "Host checks (argument lists, not shell commands):\n"
        f"{commands or 'NONE — unverified proposal'}"
        f"\n\nPrime runtime limit: {args['runtime_timeout_seconds'] // 60} minutes."
        " Each host check has an additional 2-minute limit.\n\n"
        "Prime executes generated code with your user permissions; this is NOT a sandbox. "
        "Provider charges may apply and repository content may reach your configured provider. "
        "Verification commands also execute repository code. "
        "The plugin uses a separate candidate and never automatically applies or retries it."
    )


def submit_with_consent(controller, args: dict, confirm: Callable[[str], bool]) -> str | None:
    """Freeze exactly the reviewed request. A declined prompt has no run side effects."""
    frozen = copy.deepcopy(args)
    if confirm(approval_text(frozen)) is not True:
        return None
    request_id = str(uuid4())
    controller.submit(controller.session_id, request_id, frozen)
    return request_id


def error_help(code: object) -> str:
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
        return "Open the preserved receipt for details, or run hermes prime doctor --json."
    help_text = _HELP.get(code, "Inspect the preserved receipt, or run hermes prime doctor --json.")
    return f"{help_text}\nCode: {code}"


def readiness_text(report: dict) -> str:
    if report.get("state") == "READY" and report.get("ready") is True:
        return (
            "Local setup checks passed. This does not prove provider login, live inference, "
            "or compatibility with every model. No model request was sent."
        )
    return "Setup needs attention.\n" + error_help(report.get("reason_code"))


def result_text(snapshot: dict, *, demo: bool = False) -> str:
    if demo:
        return (
            "DEMO COMPLETE — simulated workflow only.\n"
            "No model was called, no checks ran, and no repository or receipt was created.\n"
            "Close this demo and use hermes prime --ui for real work."
        )
    headings = {
        "VERIFIED": "Recorded host checks passed — review the proposal before accepting it.",
        "COMPLETED_UNVERIFIED": (
            "Proposal completed without verification. This is NOT a verified fix."
        ),
        "FAILED_VERIFICATION": "The proposal did not pass its recorded host checks.",
        "FAILED": "The run failed. A candidate may contain partial changes.",
        "UNCERTAIN": (
            "The outcome is uncertain. Inspect the candidate before starting another run."
        ),
    }
    status = snapshot.get("status")
    text = headings.get(status, "The run was not admitted.")
    counts = snapshot.get("checks", {})
    text += f"\nHost checks passed: {counts.get('passed', 0)} / {counts.get('total', 0)}."
    if snapshot.get("error_code"):
        text += "\n" + error_help(snapshot["error_code"])
    if snapshot.get("run_id"):
        text += "\nRun: " + snapshot["run_id"]
    if snapshot.get("receipt_sha256"):
        text += "\nReceipt SHA-256: " + snapshot["receipt_sha256"]
    return text + (
        "\n\nNo changes were automatically applied. No automatic retry was attempted. "
        "Receipts are unsigned, artifacts are not revalidated by this window, "
        "and passing checks are not a guarantee of correctness."
    )


def demo_runner(_args: dict, _ctx: object) -> str:
    """Deterministic, zero-provider demonstration; never invent a verified result."""
    return json.dumps({
        "status": "COMPLETED_UNVERIFIED", "ok": False, "verified": False,
        "completed": True, "checks": [],
    })
