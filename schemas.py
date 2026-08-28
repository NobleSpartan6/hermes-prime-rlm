"""Input schemas: constants and pure validation helpers for model-facing input.

Everything here is deterministic and side-effect free. The tool schema exposed
to the model lives in :mod:`tools`; this module owns the admission rules.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Bounds (single source of truth for schema, validation, and docs)
# ---------------------------------------------------------------------------

GOAL_MAX_CHARS = 20_000
CHECK_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CHECK_ARGV_MAX_ENTRIES = 32
CHECK_ARGV_ENTRY_MAX_CHARS = 4_096
CHECKS_MAX_ENTRIES = 8
CHECK_TIMEOUT_DEFAULT_SECONDS = 300
CHECK_TIMEOUT_MIN_SECONDS = 1
CHECK_TIMEOUT_MAX_SECONDS = 1_800
RUNTIME_TIMEOUT_DEFAULT_SECONDS = 1_200
RUNTIME_TIMEOUT_MIN_SECONDS = 30
RUNTIME_TIMEOUT_MAX_SECONDS = 3_600
COMMAND_PREFIX_MAX_TOKENS = 8
TOKEN_MAX_CHARS = 4_096

# Protocol / evidence bounds
MAX_EVENT_FILE_BYTES = 64 * 1024 * 1024  # 64 MiB
MAX_EVENT_RECORD_BYTES = 4 * 1024 * 1024  # 4 MiB
FINAL_TEXT_MAX_CHARS = 10_000
RPC_KERNEL_HEALTH_MARKER = "__HERMES_PRIME_KERNEL_HEALTH_V1__"
RPC_KERNEL_HEALTH_CODE = (
    "import rlm\n"
    "assert callable(rlm)\n"
    f"'{RPC_KERNEL_HEALTH_MARKER}'"
)

# Candidate-tree digest bounds
TREE_DIGEST_MAX_ENTRIES = 50_000
TREE_DIGEST_MAX_TOTAL_FILE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


class ValidationError(ValueError):
    """Raised when model-facing input or operator config fails admission.

    Carries a stable ``error_code`` so the handler can emit structured JSON
    without string matching.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Primitive text helpers
# ---------------------------------------------------------------------------


def _reject_nul(field: str, value: str, code: str = "INVALID_INPUT") -> None:
    if "\x00" in value:
        raise ValidationError(code, f"{field} must not contain NUL characters.")


def validate_goal(raw: object) -> str:
    """Validate and normalize the ``goal`` field. Returns the stripped goal."""
    if not isinstance(raw, str):
        raise ValidationError("INVALID_GOAL", "goal must be a string.")
    goal = raw.strip()
    if not goal:
        raise ValidationError("EMPTY_GOAL", "goal must be a nonempty string.")
    if len(goal) > GOAL_MAX_CHARS:
        raise ValidationError(
            "GOAL_TOO_LONG",
            f"goal exceeds the maximum of {GOAL_MAX_CHARS} characters.",
        )
    _reject_nul("goal", goal, "INVALID_GOAL")
    return goal


def validate_repository_path(raw: object) -> str:
    """Validate the ``repository_path`` field syntactically.

    Semantic checks (exists, is dir, is repo root, clean, no submodules) live
    in :mod:`validation`, which runs actual ``git`` commands.
    """
    if not isinstance(raw, str):
        raise ValidationError(
            "INVALID_REPOSITORY_PATH", "repository_path must be a string."
        )
    _reject_nul("repository_path", raw, "INVALID_REPOSITORY_PATH")
    path = raw.strip()
    if not path:
        raise ValidationError(
            "REPOSITORY_PATH_REQUIRED", "repository_path must be a nonempty path."
        )
    if not re.match(r"^[A-Za-z]:[\\/]", path) and not path.startswith("/"):
        raise ValidationError(
            "RELATIVE_REPOSITORY_PATH",
            "repository_path must be an absolute path.",
        )
    return path


def validate_check_name(raw: object) -> str:
    if not isinstance(raw, str) or not CHECK_NAME_RE.fullmatch(raw):
        raise ValidationError(
            "INVALID_CHECK_NAME",
            "check name must match ^[A-Za-z0-9._-]{1,64}$.",
        )
    return raw


def validate_check_argv(raw: object) -> list[str]:
    if not isinstance(raw, list):
        raise ValidationError("INVALID_CHECK_ARGV", "check argv must be an array.")
    if len(raw) < 1 or len(raw) > CHECK_ARGV_MAX_ENTRIES:
        raise ValidationError(
            "INVALID_CHECK_ARGV",
            f"check argv must contain 1..{CHECK_ARGV_MAX_ENTRIES} entries.",
        )
    out: list[str] = []
    for i, token in enumerate(raw):
        if not isinstance(token, str) or not token:
            raise ValidationError(
                "INVALID_CHECK_ARGV", "every argv entry must be a nonempty string."
            )
        if len(token) > CHECK_ARGV_ENTRY_MAX_CHARS:
            raise ValidationError(
                "ARGV_ENTRY_TOO_LONG",
                f"argv entry {i} exceeds {CHECK_ARGV_ENTRY_MAX_CHARS} characters.",
            )
        _reject_nul(f"argv[{i}]", token, "INVALID_CHECK_ARGV")
        out.append(token)
    return out


def validate_check_timeout(raw: object) -> int:
    if raw is None:
        return CHECK_TIMEOUT_DEFAULT_SECONDS
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValidationError(
            "INVALID_CHECK_TIMEOUT", "timeout_seconds must be a number."
        )
    timeout = int(raw)
    if not CHECK_TIMEOUT_MIN_SECONDS <= timeout <= CHECK_TIMEOUT_MAX_SECONDS:
        raise ValidationError(
            "INVALID_CHECK_TIMEOUT",
            "timeout_seconds must be between "
            f"{CHECK_TIMEOUT_MIN_SECONDS} and {CHECK_TIMEOUT_MAX_SECONDS}.",
        )
    return timeout


def validate_checks(raw: object) -> list[dict]:
    """Validate the full ``checks`` array. Returns normalized check dicts."""
    if raw is None:
        raise ValidationError("CHECKS_REQUIRED", "checks must be an array (may be empty).")
    if not isinstance(raw, list):
        raise ValidationError("CHECKS_REQUIRED", "checks must be an array.")
    if len(raw) > CHECKS_MAX_ENTRIES:
        raise ValidationError(
            "TOO_MANY_CHECKS", f"checks may contain at most {CHECKS_MAX_ENTRIES} entries."
        )
    normalized: list[dict] = []
    seen_names: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValidationError("INVALID_CHECK", "each check must be an object.")
        name = validate_check_name(entry.get("name"))
        if name in seen_names:
            raise ValidationError(
                "DUPLICATE_CHECK_NAME", f"duplicate check name: {name}"
            )
        seen_names.add(name)
        normalized.append(
            {
                "name": name,
                "argv": validate_check_argv(entry.get("argv")),
                "timeout_seconds": validate_check_timeout(entry.get("timeout_seconds")),
            }
        )
    return normalized


def validate_runtime_timeout(raw: object) -> int:
    if raw is None:
        return RUNTIME_TIMEOUT_DEFAULT_SECONDS
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValidationError(
            "INVALID_RUNTIME_TIMEOUT", "runtime_timeout_seconds must be a number."
        )
    timeout = int(raw)
    if not RUNTIME_TIMEOUT_MIN_SECONDS <= timeout <= RUNTIME_TIMEOUT_MAX_SECONDS:
        raise ValidationError(
            "INVALID_RUNTIME_TIMEOUT",
            "runtime_timeout_seconds must be between "
            f"{RUNTIME_TIMEOUT_MIN_SECONDS} and {RUNTIME_TIMEOUT_MAX_SECONDS}.",
        )
    return timeout


# ---------------------------------------------------------------------------
# Operator configuration (prime_agent_command)
# ---------------------------------------------------------------------------


def validate_command_prefix(raw: object) -> list[str]:
    """Validate the operator-owned ``prime_agent_command`` setting.

    Must be a list of 1..N nonempty strings; tokens are used verbatim as an
    argv prefix — never shell-split. The model cannot supply this value.
    """
    if raw is None:
        return ["prime-agent"]
    if not isinstance(raw, list):
        raise ValidationError(
            "INVALID_PRIME_COMMAND",
            "prime_agent_command must be a list of command-prefix strings.",
        )
    if len(raw) < 1 or len(raw) > COMMAND_PREFIX_MAX_TOKENS:
        raise ValidationError(
            "INVALID_PRIME_COMMAND",
            f"prime_agent_command must contain 1..{COMMAND_PREFIX_MAX_TOKENS} strings.",
        )
    tokens: list[str] = []
    for token in raw:
        if not isinstance(token, str) or not token.strip():
            raise ValidationError(
                "INVALID_PRIME_COMMAND",
                "every prime_agent_command entry must be a nonempty string.",
            )
        if len(token) > TOKEN_MAX_CHARS:
            raise ValidationError(
                "INVALID_PRIME_COMMAND",
                f"prime_agent_command entries must be at most {TOKEN_MAX_CHARS} characters.",
            )
        _reject_nul("prime_agent_command", token, "INVALID_PRIME_COMMAND")
        tokens.append(token)
    return tokens


# ---------------------------------------------------------------------------
# Prime version gate (stdlib semver parsing, exact RPC compatibility pin)
# ---------------------------------------------------------------------------

PRIME_RPC_VERSION = (0, 8, 1)

_SEMVER_RE = re.compile(
    r"(?<![0-9A-Za-z._-])v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?(?![0-9A-Za-z._-])"
)


def extract_semver(version_output: str) -> tuple[int, int, int]:
    """Extract exactly one semantic version from trimmed ``--version`` output.

    Fail closed when zero or multiple plausible versions appear. Accepts a
    leading ``v`` and a binary-name prefix ("prime-agent 0.8.2"). Rejects
    dates, hashes, and prose containing two or more versions.
    """
    text = (version_output or "").strip()
    all_matches = [
        (int(m.group("major")), int(m.group("minor")), int(m.group("patch")))
        for m in _SEMVER_RE.finditer(text)
    ]
    if len(all_matches) == 1:
        return all_matches[0]
    raise ValidationError(
        "UNPARSEABLE_PRIME_VERSION",
        "could not extract exactly one semantic version from prime-agent --version output.",
    )


def is_supported_prime_version(version: tuple[int, int, int]) -> bool:
    return version == PRIME_RPC_VERSION


def check_prime_version_compatible(version_output: str) -> tuple[int, int, int]:
    """Parse and enforce the exact Prime RPC compatibility pin."""
    version = extract_semver(version_output)
    if not is_supported_prime_version(version):
        raise ValidationError(
            "UNSUPPORTED_PRIME_VERSION",
            "prime-agent version "
            f"{version[0]}.{version[1]}.{version[2]} does not match the "
            "required v0.8.1 RPC compatibility pin.",
        )
    return version


# ---------------------------------------------------------------------------
# Prime JSON event-stream schema pin
# ---------------------------------------------------------------------------

PRIME_JSON_SCHEMA_VERSION = 3
