"""Cross-platform subprocess layer. The ONLY module with platform-specific
process-group logic (spec §15).

Design invariants:
* ``shell=False`` everywhere. ``os.system`` nowhere. No interpolated shells.
* stdout/stderr stream straight to files — output is never accumulated
  unbounded in memory.
* Monotonic time for durations.
* Windows: ``CREATE_NEW_PROCESS_GROUP`` + ``CREATE_NO_WINDOW``; tree kill via
  argv-list ``taskkill /PID <pid> /T /F`` (never through a shell).
* macOS/POSIX: ``start_new_session=True``; timeout escalates
  SIGTERM→SIGKILL against the owned process group.
* Never claims control over independently daemonized descendants.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Characters that cmd.exe actively interprets in batch-file argument
# positions. When a resolved executable is a .cmd/.bat shim, arguments
# containing any of these are REFUSED before spawn rather than risk cmd-level
# reinterpretation (command splitting, redirection, variable expansion).
# Verified empirically on CPython 3.11/Windows: a bare ``%*`` shim turns
# ``a&b`` into two commands and expands ``%TEMP%``.
_CMD_SHIM_FORBIDDEN_CHARS = tuple('&|<>^%!";')

IS_WINDOWS = os.name == "nt"
IS_MACOS = os.name == "posix" and os.uname().sysname == "Darwin"  # type: ignore[attr-defined]


class ProcessLaunchError(RuntimeError):
    """The owned process could not be started at all."""


class ProcessTimeout(RuntimeError):
    """The owned process exceeded its deadline and termination was attempted."""


# ---------------------------------------------------------------------------
# Executable resolution
# ---------------------------------------------------------------------------


def _which(executable: str) -> str | None:
    return shutil.which(executable)


def _resolve_taskkill() -> str | None:
    """Absolute taskkill path (System32), avoiding partial-executable resolution."""
    if not IS_WINDOWS:
        return None
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("SYSTEMROOT".title())  # SystemRoot
    candidate = Path(system_root or r"C:\Windows") / "System32" / "taskkill.exe"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("taskkill")


def resolve_executable(token: str) -> str | None:
    """Resolve one command token to an absolute executable path.

    Absolute/relative paths are validated for existence (PATHEXT-aware on
    Windows); bare names go through :func:`shutil.which` (also PATHEXT-aware).
    Returns None when unresolvable.
    """
    if not token:
        return None
    candidate = Path(token)
    if candidate.is_absolute() or os.sep in token or "/" in token:
        if _path_is_executable(candidate):
            return str(candidate)
        return None
    return _which(token)


def _path_is_executable(path: Path) -> bool:
    if not path.exists():
        return False
    if os.name == "nt":
        exts = [e.lower() for e in os.environ.get("PATHEXT", "").split(";") if e]
        suffix = path.suffix.lower()
        if suffix:
            return path.is_file()
        return any(path.with_suffix(ext).is_file() for ext in exts)
    return path.is_file()


def is_cmd_shim(executable: str) -> bool:
    """True when the resolved executable is a Windows ``.cmd``/``.bat`` shim."""
    return IS_WINDOWS and executable.lower().endswith((".cmd", ".bat"))


def guard_shim_argument(value: str) -> None:
    """Fail closed when a shim-bound argument contains cmd-active characters.

    Rationale documented on :data:`_CMD_SHIM_FORBIDDEN_CHARS`. Spaces,
    parentheses, commas-in-words, unicode, and newlines-free ordinary text all
    pass; anything cmd.exe could turn into structure is refused pre-spawn.
    """
    for ch in _CMD_SHIM_FORBIDDEN_CHARS:
        if ch in value:
            raise ValueError(
                "argument cannot be passed through a Windows .cmd/.bat shim "
                f"(contains {ch!r}); re-run with a direct executable instead"
            )


def _short_path(path: str) -> str | None:
    """Return the 8.3 short path on Windows (None when unavailable).

    Verified empirically (probe on this host): an 8.3 path contains no spaces
    or cmd-active characters and passes through a ``%*`` batch shim as ONE
    intact argv element, which quoted paths do NOT (cmd re-splits them).
    """
    if not IS_WINDOWS:
        return None
    import ctypes

    buf = ctypes.create_unicode_buffer(1024)
    get_short = ctypes.windll.kernel32.GetShortPathNameW  # type: ignore[attr-defined]
    length = get_short(path, buf, 1024)
    if length and buf.value:
        return buf.value
    return None


def build_shim_argv(shim: str, args: list[str]) -> list[str]:
    """Build a cmd.exe invocation for a ``.cmd``/``.bat`` shim.

    Empirical contract on CPython 3.11/Windows (probes on this host):

    * Quoted args through a ``%*`` shim are RE-SPLIT by cmd — ``"a b"`` arrives
      as ``['"a', 'b"']``. Quoting does NOT survive batch argument parsing.
    * An unquoted 8.3 short path (no spaces, no metacharacters) passes through
      as ONE argv element and the child resolves it correctly.
    * Bare ``%*`` tails execute cmd metacharacters (``a&b`` runs ``b``);
      :func:`guard_shim_argument` refuses such arguments before spawn.

    Therefore: every arg must pass the guard, then be converted to its 8.3
    short form when one exists (real paths); non-path string args must contain
    no spaces (single bare token). The shim path itself is short-formed.
    """
    for arg in args:
        guard_shim_argument(arg)

    def token(value: str) -> str:
        short = _short_path(value)
        if short is not None:
            return short
        if " " in value or "\t" in value:
            raise ValueError(
                "argument contains spaces and cannot be passed through a "
                ".cmd/.bat shim (cmd re-splits quoted args); use a direct "
                "executable instead"
            )
        return value

    shim_token = token(shim)
    tail = " ".join(token(a) for a in args)
    return ["cmd.exe", "/d", "/s", "/c", f"{shim_token} {tail}".strip()]


def resolve_command_prefix(prefix: list[str]) -> list[str]:
    """Resolve an operator-owned command prefix to concrete absolute tokens.

    The first token is resolved through PATHEXT/which; later tokens (e.g. a
    script path for ``bash -c``-style prefixes) are preserved verbatim — they
    are arguments to the first executable, not executables themselves.
    """
    if not prefix:
        raise ValueError("empty command prefix")
    resolved_first = resolve_executable(prefix[0])
    if resolved_first is None:
        raise FileNotFoundError(f"cannot resolve executable: {prefix[0]!r}")
    return [resolved_first, *prefix[1:]]


# ---------------------------------------------------------------------------
# Owned process
# ---------------------------------------------------------------------------

_TERMINATION_GRACE_SECONDS = 5.0


@dataclass
class SpawnSpec:
    """Everything needed to start one owned process."""

    argv: list[str]
    cwd: str
    stdout_path: str
    stderr_path: str
    env: dict | None = None


class OwnedProcess:
    """A started subprocess whose lifetime this plugin owns."""

    def __init__(self, popen: subprocess.Popen, argv: list[str]) -> None:
        self.popen = popen
        self.argv = argv

    @property
    def pid(self) -> int:
        return self.popen.pid

    def poll(self) -> int | None:
        return self.popen.poll()

    def wait_monotonic(self, timeout: float) -> int:
        """Wait up to *timeout* seconds; return exit code or raise TimeoutError."""
        deadline = time.monotonic() + timeout
        remaining = max(0.0, deadline - time.monotonic())
        try:
            return self.popen.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"process {self.pid} exceeded {timeout}s") from None

    def terminate_tree_best_effort(self) -> None:
        """Best-effort tree termination. Never raises.

        Windows: ``taskkill.exe /PID <pid> /T /F`` as an argv list (no shell),
        then reap the direct child. POSIX: TERM the process group, grace
        period, KILL the group, reap the direct child. Daemonized or
        supervisor-owned descendants elsewhere in the tree are NOT guaranteed
        stopped — callers must treat such runs as UNCERTAIN.
        """
        try:
            with contextlib.suppress(Exception):
                if IS_WINDOWS:
                    self._terminate_windows()
                else:
                    self._terminate_posix()
        finally:
            with contextlib.suppress(Exception):
                self.popen.wait(timeout=_TERMINATION_GRACE_SECONDS)

    def _terminate_windows(self) -> None:
        taskkill = _resolve_taskkill()
        if taskkill is None:
            return
        with contextlib.suppress(Exception):
            subprocess.run(  # noqa: S603 - fixed argv, no shell, own child only
                [taskkill, "/PID", str(self.pid), "/T", "/F"],
                capture_output=True,
                timeout=_TERMINATION_GRACE_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    def _terminate_posix(self) -> None:
        pgid = self.pid  # start_new_session makes pid == pgid
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline:
            if self.popen.poll() is not None:
                return
            time.sleep(0.1)
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)


def windows_spawn_flags() -> int:
    """Creation flags for long-running children on Windows."""
    if not IS_WINDOWS:
        return 0
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    return flags


def popen_kwargs_for_platform() -> dict:
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = windows_spawn_flags()
        kwargs["close_fds"] = False  # stdio redirect handles must inherit
    else:
        kwargs["start_new_session"] = True
    return kwargs


def spawn_owned(spec: SpawnSpec) -> OwnedProcess:
    """Start an owned process with stdout/stderr redirected to files."""
    # Files are opened for the child's lifetime and closed immediately after
    # Popen inherits the handles; context managers would close before spawn.
    stdout_f = open(spec.stdout_path, "wb")  # noqa: SIM115
    try:
        stderr_f = open(spec.stderr_path, "wb")  # noqa: SIM115
        try:
            popen = subprocess.Popen(  # noqa: S603 - argv list, shell=False
                spec.argv,
                cwd=spec.cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout_f,
                stderr=stderr_f,
                env=spec.env,
                **popen_kwargs_for_platform(),
            )
        finally:
            stderr_f.close()
    finally:
        stdout_f.close()
    return OwnedProcess(popen, spec.argv)


def spawn_and_wait(
    spec: SpawnSpec,
    timeout_seconds: float,
) -> tuple[int | None, bool]:
    """Spawn, wait bounded, terminate tree on timeout.

    Returns ``(exit_code, timed_out)``. ``exit_code`` is None when the process
    could not be launched or did not produce a code; ``timed_out`` marks runs
    whose terminal boundary is unreliable (callers map these to UNCERTAIN /
    FAILED_VERIFICATION per status rules).
    """
    try:
        proc = spawn_owned(spec)
    except OSError:
        return None, False
    try:
        code = proc.wait_monotonic(timeout_seconds)
        return code, False
    except TimeoutError:
        proc.terminate_tree_best_effort()
        return proc.poll(), True


def effective_command_record(argv: list[str]) -> list[str]:
    """Return the command record stored in evidence/receipts.

    Contains no secrets by construction: argv is paths + flags + the goal-free
    fixed instruction. Kept as a function so future redaction has one choke
    point and so tests can assert records exist and contain no env dumps.
    """
    return list(argv)


def prime_environment() -> dict:
    """Child environment: inherited copy plus PI_SKIP_VERSION_CHECK=1.

    The environment is never enumerated into logs or receipts.
    """
    env = os.environ.copy()
    env["PI_SKIP_VERSION_CHECK"] = "1"
    return env
