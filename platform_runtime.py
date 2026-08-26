"""Cross-platform subprocess layer. The ONLY module with platform-specific
process-group logic (spec §15).

Design invariants:
* ``shell=False`` everywhere. ``os.system`` nowhere. No interpolated shells.
* stdout/stderr are concurrently drained through bounded pipes — output is
  never accumulated unbounded in memory or on disk.
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
import re
import shutil
import signal
import subprocess
import threading
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


# Write-time cap for child stdout/stderr log sinks. A child that ignores its
# runtime bound can still not fill the disk through these files. 64 MiB per
# stream matches the event-stream parser's own record-file bound.
LOG_SINK_MAX_BYTES = 64 * 1024 * 1024
_TRUNCATION_MARKER_SUFFIX = ".truncated"


class BoundedFileSink:
    """Drain a child pipe to disk without ever exceeding ``max_bytes``.

    Once the cap is reached, the sink replaces the tail with an explicit
    truncation notice, discards all further bytes while continuing to drain the
    pipe (so the child cannot deadlock), and writes a ``.truncated`` sidecar.
    """

    _TRUNCATION_NOTICE = (
        b"\n[prime-rlm: output truncated at LOG_SINK_MAX_BYTES; "
        b"excess discarded]\n"
    )

    def __init__(self, path: str, max_bytes: int | None = None) -> None:
        self.path = path
        self.max_bytes = LOG_SINK_MAX_BYTES if max_bytes is None else max_bytes
        self.truncated = False
        self.error: OSError | None = None

    def drain(self, stream) -> None:
        """Read *stream* to EOF, writing at most ``max_bytes`` bytes."""
        notice = self._TRUNCATION_NOTICE[: self.max_bytes]
        content_limit = max(0, self.max_bytes - len(notice))
        written = 0
        try:
            with open(self.path, "wb") as handle:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    if self.truncated:
                        continue
                    if written + len(chunk) <= self.max_bytes:
                        handle.write(chunk)
                        written += len(chunk)
                        continue

                    # First overflow: reserve room for the marker inside the
                    # cap, retain the prefix, then discard every later byte.
                    if written > content_limit:
                        handle.truncate(content_limit)
                        handle.seek(content_limit)
                        written = content_limit
                    elif written < content_limit:
                        keep = min(len(chunk), content_limit - written)
                        handle.write(chunk[:keep])
                        written += keep
                    handle.write(notice)
                    written += len(notice)
                    self.truncated = True
                handle.flush()
                os.fsync(handle.fileno())
            if self.truncated:
                Path(self.path + _TRUNCATION_MARKER_SUFFIX).touch(exist_ok=True)
        except OSError as exc:
            self.error = exc
        finally:
            with contextlib.suppress(Exception):
                stream.close()


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


def _resolve_cmd() -> str | None:
    """Resolve the Windows command processor to an absolute trusted path."""
    if not IS_WINDOWS:
        return None
    comspec = os.environ.get("COMSPEC")
    if comspec and Path(comspec).is_absolute() and Path(comspec).is_file():
        return str(Path(comspec))
    system_root = os.environ.get("SYSTEMROOT")
    candidate = Path(system_root or r"C:\Windows") / "System32" / "cmd.exe"
    if candidate.is_file():
        return str(candidate)
    resolved = shutil.which("cmd.exe")
    return str(Path(resolved).resolve()) if resolved else None


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
    """Fail closed when a shim-bound argument contains cmd structure."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(
            "argument cannot be passed through a Windows .cmd/.bat shim "
            "(contains a control character); use a direct executable instead"
        )
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


def resolve_shim_target(shim: str) -> tuple[str, str] | None:
    """Resolve an npm ``.cmd`` shim to ``(node_exe, cli_js)`` for direct spawn.

    npm's cmd-shim template is deterministic: it runs ``node.exe <shim-dir>\\
    node_modules\\<pkg>\\<bin-rel-path> %*``. Parsing the shim's final command
    line gives us the real entry script, letting us bypass cmd.exe entirely —
    which restores true literal argv (spaces and all) for the direct route.
    Returns None when the shim doesn't match the npm template.
    """
    if not IS_WINDOWS:
        return None
    try:
        text = Path(shim).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Match npm's cmd-shim tail: `& "%_prog%"  "<...js>" %*` (the script path
    # is a %dp0%-relative literal). No $ anchor — the shim ends with a newline.
    match = re.search(r'"%_prog%"\s+"?([^"\r\n]+?\.js)"?\s+%*', text)
    if match is None:
        return None
    script = Path(match.group(1).strip())
    if "%dp0%" in str(script) or not script.is_absolute():
        # Expand the shim's %dp0% (its own directory) into the script path.
        raw = match.group(1).strip()
        dp0 = str(Path(shim).parent)
        raw = raw.replace("%dp0%\\", dp0 + "\\").replace("%dp0%", dp0)
        script = Path(raw)
    if not script.is_file():
        return None
    node = shutil.which("node")
    if node is None:
        shim_dir = Path(shim).parent
        local_node = shim_dir / "node.exe"
        if local_node.is_file():
            node = str(local_node)
        else:
            return None
    return node, str(script)


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
    cmd = _resolve_cmd()
    if cmd is None:
        raise FileNotFoundError("cannot resolve trusted absolute cmd.exe path")
    return [cmd, "/d", "/s", "/c", f"{shim_token} {tail}".strip()]


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
    stdout_max_bytes: int | None = None
    stderr_max_bytes: int | None = None


class OwnedProcess:
    """A started subprocess whose lifetime and output drains this plugin owns."""

    def __init__(
        self,
        popen: subprocess.Popen,
        argv: list[str],
        drains: list[tuple[BoundedFileSink, object]],
    ) -> None:
        self.popen = popen
        self.argv = argv
        self._drains: list[tuple[BoundedFileSink, object, threading.Thread]] = []
        for sink, stream in drains:
            thread = threading.Thread(
                target=sink.drain,
                args=(stream,),
                name=f"prime-rlm-drain-{Path(sink.path).name}",
                daemon=True,
            )
            thread.start()
            self._drains.append((sink, stream, thread))

    def finish_output(self, timeout: float = _TERMINATION_GRACE_SECONDS) -> bool:
        """Wait for both pipe drains; false means output custody is ambiguous."""
        deadline = time.monotonic() + timeout
        for _sink, _stream, thread in self._drains:
            thread.join(max(0.0, deadline - time.monotonic()))
        complete = all(not thread.is_alive() for _s, _p, thread in self._drains)
        if not complete:
            # A descendant may still hold inherited pipe handles. Close our
            # readers and report ambiguity rather than blocking forever.
            for _sink, stream, thread in self._drains:
                if thread.is_alive():
                    with contextlib.suppress(Exception):
                        stream.close()
            for _sink, _stream, thread in self._drains:
                thread.join(0.2)
        return complete and all(sink.error is None for sink, _p, _t in self._drains)

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
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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
    """Start an owned process with concurrently drained, bounded pipes."""
    try:
        popen = subprocess.Popen(  # noqa: S603 - argv list, shell=False
            spec.argv,
            cwd=spec.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=spec.env,
            bufsize=0,
            **popen_kwargs_for_platform(),
        )
    except OSError:
        # Preserve the expected artifact shape on launch failure.
        Path(spec.stdout_path).touch(exist_ok=True)
        Path(spec.stderr_path).touch(exist_ok=True)
        raise
    if popen.stdout is None or popen.stderr is None:  # pragma: no cover - Popen contract
        raise ProcessLaunchError("failed to create child output pipes")
    return OwnedProcess(
        popen,
        spec.argv,
        [
            (BoundedFileSink(spec.stdout_path, spec.stdout_max_bytes), popen.stdout),
            (BoundedFileSink(spec.stderr_path, spec.stderr_max_bytes), popen.stderr),
        ],
    )


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
        output_complete = proc.finish_output()
        return code, not output_complete
    except TimeoutError:
        proc.terminate_tree_best_effort()
        result = proc.poll()
        proc.finish_output()
        return result, True


def effective_command_record(argv: list[str]) -> list[str]:
    """Return a process-local argv copy used only to derive non-secret hashes.

    The raw tokens are never persisted because operator prefix arguments may
    contain credentials. Receipts store only ``prime_command_identity``.
    """
    return list(argv)


def prime_environment() -> dict:
    """Child environment: inherited copy plus PI_SKIP_VERSION_CHECK=1.

    The environment is never enumerated into logs or receipts.
    """
    env = os.environ.copy()
    env["PI_SKIP_VERSION_CHECK"] = "1"
    return env
