"""Pre-admission validation: repository semantics and Prime version gate.

All git interaction here is read-only. No run directory may be created by any
function in this module — validation failures must leave no trace (spec §12).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePath

from .platform_runtime import SpawnSpec, spawn_and_wait
from .schemas import (
    ValidationError,
    check_prime_version_compatible,
)


class _GitError(ValidationError):
    pass


GIT_CAPTURE_MAX_BYTES = 8 * 1024 * 1024
GIT_STDERR_MAX_BYTES = 64 * 1024
PRIME_VERSION_MAX_BYTES = 64 * 1024


def _was_truncated(path: str) -> bool:
    return Path(path + ".truncated").exists()


def _read_bounded_text(path: str) -> str:
    return Path(path).read_bytes().decode("utf-8", errors="replace")


def run_git(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float = 60,
    max_stdout_bytes: int = GIT_CAPTURE_MAX_BYTES,
) -> str:
    """Run a Git command with bounded stdout/stderr capture."""
    with tempfile.TemporaryDirectory(prefix="prime-rlm-git-") as temp:
        stdout_path = os.fspath(Path(temp) / "stdout")
        run_git_to_file(
            args,
            cwd=cwd,
            output_path=stdout_path,
            timeout=timeout,
            max_stdout_bytes=max_stdout_bytes,
        )
        return _read_bounded_text(stdout_path)


def run_git_to_file(
    args: list[str],
    *,
    cwd: str | None,
    output_path: str,
    timeout: float = 60,
    max_stdout_bytes: int = GIT_CAPTURE_MAX_BYTES,
) -> None:
    """Stream Git stdout to a bounded file and fail closed on truncation."""
    git_exe = resolve_git()
    with tempfile.TemporaryDirectory(prefix="prime-rlm-git-stderr-") as temp:
        stderr_path = os.fspath(Path(temp) / "stderr")
        spec = SpawnSpec(
            argv=[git_exe, *args],
            cwd=cwd or os.getcwd(),
            stdout_path=output_path,
            stderr_path=stderr_path,
            stdout_max_bytes=max_stdout_bytes,
            stderr_max_bytes=GIT_STDERR_MAX_BYTES,
        )
        exit_code, ambiguous = spawn_and_wait(spec, timeout)
        if ambiguous:
            raise ValidationError("GIT_TIMEOUT", f"git {' '.join(args)} timed out.")
        if _was_truncated(output_path) or _was_truncated(stderr_path):
            raise ValidationError(
                "GIT_OUTPUT_LIMIT",
                f"git {' '.join(args)} exceeded its bounded output limit.",
            )
        if exit_code is None:
            raise ValidationError("GIT_NOT_FOUND", "git executable could not be launched.")
        if exit_code != 0:
            stderr = _read_bounded_text(stderr_path).strip()
            raise _GitError(
                "GIT_COMMAND_FAILED", f"git {' '.join(args)} failed: {stderr[:500]}"
            )



def resolve_git() -> str:
    """Resolve the ``git`` executable through PATH (PATHEXT-aware on Windows)."""
    resolved = _which("git")
    if resolved is None:
        raise ValidationError("GIT_NOT_FOUND", "git executable was not found on PATH.")
    return resolved


def _which(executable: str) -> str | None:
    return _shutil().which(executable)


def _shutil():  # indirection keeps module import light and test-patchable
    import shutil

    return shutil


# ---------------------------------------------------------------------------
# Path equivalence helpers
# ---------------------------------------------------------------------------


def same_path(a: str | PurePath, b: str | PurePath) -> bool:
    """Cross-platform strict path equality.

    Resolves both paths strictly (symlinks collapsed; macOS ``/var`` vs
    ``/private/var`` normalized), then compares case-insensitively on
    case-insensitive filesystems (Windows) and case-sensitively elsewhere.
    Slash direction differences collapse via normal OS parsing.
    """
    a_str, b_str = os.fspath(a), os.fspath(b)
    try:
        a_res = Path(a_str).resolve(strict=True)
        b_res = Path(b_str).resolve(strict=True)
    except OSError:
        return False
    if os.path.normcase(str(a_res)) == os.path.normcase(str(b_res)):
        return True
    # Case-insensitive platforms: also compare normcased strings directly so
    # minor casing differences that survive resolve() do not false-negative.
    return os.path.normcase(a_str) == os.path.normcase(b_str)


def path_equivalent_for_header(observed: str, expected: str) -> bool:
    """Header cwd equivalence used by protocol validation.

    More lenient than :func:`same_path`: when either path cannot be strictly
    resolved (e.g. the observer ran on another machine layout), fall back to
    normalized string comparison with case folding on Windows.
    """
    if same_path(observed, expected):
        return True
    a = os.path.normpath(observed)
    b = os.path.normpath(expected)
    if os.name == "nt":
        a, b = a.replace("/", "\\"), b.replace("/", "\\")
        return a.casefold() == b.casefold()
    return a == b


# ---------------------------------------------------------------------------
# Repository admission checks
# ---------------------------------------------------------------------------


def validate_repository_root(repository_path: str) -> str:
    """Require an existing directory that IS the Git repository root."""
    path = Path(repository_path)
    if not path.is_absolute():
        raise ValidationError(
            "RELATIVE_REPOSITORY_PATH", "repository_path must be absolute."
        )
    if not path.exists():
        raise ValidationError("REPOSITORY_PATH_MISSING", "repository_path does not exist.")
    if not path.is_dir():
        raise ValidationError(
            "REPOSITORY_PATH_NOT_DIRECTORY", "repository_path is not a directory."
        )
    try:
        toplevel = run_git(["rev-parse", "--show-toplevel"], cwd=str(path))
    except ValidationError as exc:
        if exc.error_code == "GIT_COMMAND_FAILED":
            raise ValidationError(
                "NOT_A_GIT_REPOSITORY", "repository_path is not a Git repository."
            ) from exc
        raise
    toplevel_clean = toplevel.strip()
    if not same_path(toplevel_clean, str(path)):
        raise ValidationError(
            "NESTED_REPOSITORY_SUBDIRECTORY",
            "repository_path must be the exact Git repository root "
            f"(resolved toplevel: {toplevel_clean}).",
        )
    return str(path.resolve(strict=True))


def reject_submodules(repository_path: str) -> None:
    if (Path(repository_path) / ".gitmodules").exists():
        raise ValidationError(
            "SUBMODULES_UNSUPPORTED",
            "repository contains .gitmodules; submodule repositories are unsupported.",
        )


def require_clean_repository(repository_path: str) -> None:
    """Reject both tracked and untracked dirtiness."""
    output = run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository_path,
    )
    if output.strip():
        raise ValidationError(
            "DIRTY_REPOSITORY",
            "The source repository contains tracked or untracked changes.",
        )


def resolve_base_commit(repository_path: str) -> str:
    output = run_git(["rev-parse", "HEAD"], cwd=repository_path).strip()
    if not output:
        raise ValidationError(
            "NO_BASE_COMMIT", "could not resolve HEAD in the source repository."
        )
    return output


def probe_prime_version(
    command_prefix: list[str],
) -> tuple[int, int, int, str | None]:
    """Run ``<prefix> --version`` with a bounded timeout and range-check it.

    Returns ``(major, minor, patch, version_output)``. The probed output text
    travels to the caller as a return value — never through module-global
    state — so concurrent invocations cannot overwrite each other's evidence.
    """
    import shutil

    resolved_first = _resolve_executable_token(command_prefix[0], shutil.which)
    if resolved_first is None:
        raise ValidationError(
            "PRIME_COMMAND_NOT_FOUND",
            f"prime-agent command prefix could not be resolved: {command_prefix[0]!r}",
        )
    argv = [resolved_first, *command_prefix[1:], "--version"]
    with tempfile.TemporaryDirectory(prefix="prime-rlm-version-") as temp:
        stdout_path = os.fspath(Path(temp) / "stdout")
        stderr_path = os.fspath(Path(temp) / "stderr")
        spec = SpawnSpec(
            argv=argv,
            cwd=os.getcwd(),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_max_bytes=PRIME_VERSION_MAX_BYTES,
            stderr_max_bytes=PRIME_VERSION_MAX_BYTES,
        )
        exit_code, ambiguous = spawn_and_wait(spec, 30)
        if ambiguous:
            raise ValidationError(
                "PRIME_VERSION_TIMEOUT", "prime-agent --version timed out or did not quiesce."
            )
        if _was_truncated(stdout_path) or _was_truncated(stderr_path):
            raise ValidationError(
                "PRIME_VERSION_OUTPUT_LIMIT",
                "prime-agent --version exceeded its bounded output limit.",
            )
        if exit_code is None:
            raise ValidationError(
                "PRIME_COMMAND_NOT_FOUND", "prime-agent executable could not be launched."
            )
        stdout = _read_bounded_text(stdout_path)
        stderr = _read_bounded_text(stderr_path)
    if exit_code != 0:
        raise ValidationError(
            "PRIME_VERSION_FAILED",
            f"prime-agent --version exited nonzero: {stderr.strip()[:300]}",
        )
    # npm .cmd shims on Windows may print the version to stderr.
    combined = f"{stdout}\n{stderr}"
    version = check_prime_version_compatible(combined)
    version_output = stdout.strip() or stderr.strip()
    return version, version_output


def _resolve_executable_token(token: str, which) -> str | None:
    """Resolve one command token: absolute/relative path kept, bare name via which."""
    from pathlib import Path as _P

    candidate = _P(token)
    if candidate.is_absolute():
        return token if _executable_exists(token) else None
    if os.sep in token or ("/" in token):
        return token if _executable_exists(token) else None
    return which(token)


def _executable_exists(path: str) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    if os.name == "nt":
        exts = [e.lower() for e in os.environ.get("PATHEXT", "").split(";") if e]
        suffix = p.suffix.lower()
        if suffix in exts:
            return p.is_file()
        return any(p.with_suffix(ext).is_file() for ext in [""] + exts)
    return p.is_file()


# ---------------------------------------------------------------------------
# Orchestrated admission (no side effects before this passes)
# ---------------------------------------------------------------------------


def admit_request(
    repository_path: str,
    command_prefix: list[str],
) -> tuple[str, str, str | None]:
    """Run every pre-admission check. Returns ``(repo_root, base_commit, prime_version_text)``.

    Order matters: all cheap local checks before the version probe. Any
    failure raises ValidationError and leaves zero new files behind.
    The probed version text is returned (not stored globally) so the caller
    persists it inside the same invocation without shared mutable state.
    """
    repo_root = validate_repository_root(repository_path)
    reject_submodules(repo_root)
    require_clean_repository(repo_root)
    base_commit = resolve_base_commit(repo_root)
    resolve_git()  # explicit gate per spec §12.11 even though run_git resolves it too
    _version_tuple, version_text = probe_prime_version(command_prefix)
    return repo_root, base_commit, version_text
