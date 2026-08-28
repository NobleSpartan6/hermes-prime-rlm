"""Owned subprocess execution for the strict Prime RPC transport."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from .models import PrimeObservation
from .platform_runtime import (
    OwnedProcess,
    build_shim_argv,
    is_cmd_shim,
    popen_kwargs_for_platform,
    resolve_command_prefix,
    resolve_shim_target,
)
from .prime_rpc import (
    RPC_MAX_RECORD_BYTES,
    RPC_MAX_STREAM_BYTES,
    RpcJsonlFramer,
    RpcLifecycleValidator,
    RpcProtocolError,
)

RPC_TASK_MAX_BYTES = 64 * 1024
_RPC_ENV_ALLOWLIST = {
    "APPDATA",
    "COMSPEC",
    "CURL_CA_BUNDLE",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "NO_COLOR",
    "PATH",
    "PATHEXT",
    "PRIME_AGENT_KERNEL_PYTHON",
    "REQUESTS_CA_BUNDLE",
    "SHELL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "WINDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
}


def build_rpc_environment(source: dict[str, str]) -> dict[str, str]:
    """Build the default non-secret child environment for Prime RPC."""
    child = {
        key: value
        for key, value in source.items()
        if key.upper() in _RPC_ENV_ALLOWLIST
    }
    child["PI_SKIP_VERSION_CHECK"] = "1"
    child["PI_OFFLINE"] = "1"
    return child


def build_rpc_argv(command_prefix: list[str], candidate_path: str) -> list[str]:
    """Build a fixed RPC invocation; task content never enters argv."""
    resolved = resolve_command_prefix(command_prefix)
    head = resolved[0]
    tail = [
        *resolved[1:],
        "--mode",
        "rpc",
        "--no-session",
        "--cwd",
        candidate_path,
        "--offline",
        "--no-extensions",
        "--no-prompt-templates",
        "--no-themes",
    ]
    if is_cmd_shim(head):
        target = resolve_shim_target(head)
        if target is not None:
            node_exe, cli_js = target
            return [node_exe, cli_js, *tail]
        return build_shim_argv(head, tail)
    return [head, *tail]


def _rpc_command(command_id: str, command_type: str, **fields) -> bytes:
    return (
        json.dumps(
            {"id": command_id, "type": command_type, **fields},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _read_task_envelope(path: str) -> str:
    """Read the admitted task for RPC stdin without placing it in argv."""
    with open(path, "rb") as handle:
        payload = handle.read(RPC_TASK_MAX_BYTES + 1)
    if len(payload) > RPC_TASK_MAX_BYTES:
        raise RpcProtocolError(
            "RPC_TASK_TOO_LARGE", "Prime RPC task envelope exceeded its byte bound."
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RpcProtocolError(
            "RPC_TASK_NOT_UTF8", "Prime RPC task envelope was not UTF-8."
        ) from exc
    if not text.strip():
        raise RpcProtocolError("RPC_TASK_EMPTY", "Prime RPC task envelope was empty.")
    return text


def _failure_observation(
    validator: RpcLifecycleValidator,
    *,
    exit_code: int | None,
    error_code: str,
    timed_out: bool = False,
    host_terminated: bool = False,
) -> PrimeObservation:
    return PrimeObservation(
        launched=True,
        exit_code=exit_code,
        session_id=validator._session_id,
        saw_agent_start=validator._agent_starts == 1,
        saw_agent_end=validator._agent_ends == 1,
        event_count=validator._event_count,
        timed_out=timed_out,
        host_terminated=host_terminated,
        stream_valid=False,
        error_code=error_code,
    )


def _readiness_failure(error_code: str, *, host_terminated: bool = False) -> dict:
    return {
        "stream_valid": False,
        "error_code": error_code,
        "provider": "",
        "model": "",
        "available_models": (),
        "auto_retry_disabled": False,
        "host_terminated": host_terminated,
    }


def probe_prime_rpc(
    command_prefix: list[str],
    candidate_path: str,
    timeout_seconds: int,
    *,
    env: dict | None = None,
) -> tuple[dict, list[str]]:
    """Inspect state and catalog through production RPC without sending a prompt."""
    try:
        argv = build_rpc_argv(command_prefix, candidate_path)
    except (FileNotFoundError, ValueError):
        return _readiness_failure("PRIME_COMMAND_NOT_FOUND"), []
    child_env = (
        {**env, "PI_SKIP_VERSION_CHECK": "1", "PI_OFFLINE": "1"}
        if env is not None
        else build_rpc_environment(dict(os.environ))
    )
    try:
        popen = subprocess.Popen(  # noqa: S603 - resolved argv, shell=False
            argv,
            cwd=candidate_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_env,
            bufsize=0,
            **popen_kwargs_for_platform(),
        )
    except OSError:
        return _readiness_failure("PRIME_LAUNCH_FAILED"), argv
    if popen.stdin is None or popen.stdout is None:
        OwnedProcess(popen, argv, []).terminate_tree_best_effort()
        return _readiness_failure("PRIME_LAUNCH_FAILED"), argv

    owned = OwnedProcess(popen, argv, [])
    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=4)
    reader_stop = threading.Event()

    def enqueue(item: bytes | None) -> bool:
        while not reader_stop.is_set():
            try:
                chunks.put(item, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def read_stdout() -> None:
        try:
            while True:
                chunk = popen.stdout.read(64 * 1024)
                if not chunk or not enqueue(chunk):
                    return
        finally:
            if not reader_stop.is_set():
                enqueue(None)

    reader = threading.Thread(
        target=read_stdout,
        name=f"prime-rpc-readiness-{popen.pid}",
        daemon=True,
    )
    reader.start()
    framer = RpcJsonlFramer(
        max_record_bytes=RPC_MAX_RECORD_BYTES,
        max_total_bytes=RPC_MAX_STREAM_BYTES,
    )
    deadline = time.monotonic() + float(timeout_seconds)
    expected = ("readiness-state", "readiness-retry", "readiness-models")
    responses: list[dict] = []
    error_code: str | None = None
    host_terminated = False

    def terminate_owned() -> None:
        nonlocal host_terminated
        reader_stop.set()
        if popen.poll() is None:
            host_terminated = True
        owned.terminate_tree_best_effort()

    try:
        popen.stdin.write(_rpc_command("readiness-state", "get_state"))
        popen.stdin.flush()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error_code = "RPC_TIMEOUT"
                break
            try:
                chunk = chunks.get(timeout=remaining)
            except queue.Empty:
                error_code = "RPC_TIMEOUT"
                break
            if chunk is None:
                framer.finish()
                if len(responses) != len(expected):
                    error_code = "RPC_PREMATURE_EOF"
                break
            for record in framer.feed(chunk):
                if len(responses) >= len(expected):
                    raise RpcProtocolError(
                        "RPC_READINESS_UNEXPECTED_RECORD",
                        "Prime emitted a record after readiness admission completed.",
                    )
                if record.get("type") != "response":
                    raise RpcProtocolError(
                        "RPC_READINESS_UNEXPECTED_RECORD",
                        "Prime emitted a non-response before prompt admission.",
                    )
                response_id = record.get("id")
                if response_id != expected[len(responses)]:
                    raise RpcProtocolError(
                        "RPC_RESPONSE_OUT_OF_ORDER",
                        "Prime readiness responses were out of order.",
                    )
                responses.append(record)
                if response_id == "readiness-state":
                    if record.get("command") != "get_state" or record.get("success") is not True:
                        raise RpcProtocolError("RPC_STATE_HANDSHAKE_FAILED", "get_state failed.")
                    popen.stdin.write(
                        _rpc_command("readiness-retry", "set_auto_retry", enabled=False)
                    )
                    popen.stdin.flush()
                elif response_id == "readiness-retry":
                    if (
                        record.get("command") != "set_auto_retry"
                        or record.get("success") is not True
                    ):
                        raise RpcProtocolError(
                            "RPC_AUTO_RETRY_DISABLE_FAILED",
                            "Prime auto-retry was not disabled.",
                        )
                    popen.stdin.write(
                        _rpc_command("readiness-models", "get_available_models")
                    )
                    popen.stdin.flush()
                elif (
                    record.get("command") != "get_available_models"
                    or record.get("success") is not True
                ):
                    raise RpcProtocolError(
                        "RPC_MODEL_CATALOG_FAILED", "Prime model catalog request failed."
                    )
                elif not popen.stdin.closed:
                    popen.stdin.close()
        if error_code is not None:
            terminate_owned()
        else:
            try:
                popen.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                error_code = "RPC_TIMEOUT"
                terminate_owned()
    except RpcProtocolError as exc:
        error_code = exc.error_code
        terminate_owned()
    except (BrokenPipeError, OSError):
        error_code = "RPC_PREMATURE_EOF"
        terminate_owned()
    finally:
        if not popen.stdin.closed:
            with contextlib.suppress(OSError):
                popen.stdin.close()
        owned.finish_output(timeout=5.0)
        reader_stop.set()
        reader.join(5.0)
        if reader.is_alive():
            with contextlib.suppress(OSError):
                popen.stdout.close()
            reader.join(1.0)
        if reader.is_alive() and error_code is None:
            error_code = "RPC_READER_DID_NOT_STOP"

    if error_code is not None:
        return _readiness_failure(error_code, host_terminated=host_terminated), argv
    state = responses[0].get("data")
    catalog = responses[2].get("data")
    model = state.get("model") if isinstance(state, dict) else None
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(model, dict) or not isinstance(models, list):
        return _readiness_failure("RPC_READINESS_INVALID"), argv
    provider = model.get("provider")
    model_id = model.get("id")
    available = tuple(
        (item.get("provider"), item.get("id"))
        for item in models
        if isinstance(item, dict)
        and isinstance(item.get("provider"), str)
        and isinstance(item.get("id"), str)
    )
    if not isinstance(provider, str) or not isinstance(model_id, str):
        return _readiness_failure("RPC_MODEL_IDENTITY_INVALID"), argv
    return (
        {
            "stream_valid": True,
            "error_code": None,
            "provider": provider,
            "model": model_id,
            "available_models": available,
            "auto_retry_disabled": True,
            "host_terminated": False,
        },
        argv,
    )


def run_prime_rpc(
    command_prefix: list[str],
    layout,
    timeout_seconds: int,
    *,
    env: dict | None = None,
) -> tuple[PrimeObservation, list[str]]:
    """Run one strict ephemeral Prime RPC session with no automatic retry."""
    try:
        argv = build_rpc_argv(command_prefix, os.fspath(layout.candidate))
    except (FileNotFoundError, ValueError):
        return (
            PrimeObservation(
                launched=False,
                exit_code=None,
                session_id=None,
                saw_agent_start=False,
                saw_agent_end=False,
                event_count=0,
                error_code="PRIME_COMMAND_NOT_FOUND",
            ),
            [],
        )
    try:
        prompt = _read_task_envelope(layout.prime_task_md)
    except (OSError, RpcProtocolError) as exc:
        error_code = (
            exc.error_code if isinstance(exc, RpcProtocolError) else "RPC_TASK_UNAVAILABLE"
        )
        return (
            PrimeObservation(
                launched=False,
                exit_code=None,
                session_id=None,
                saw_agent_start=False,
                saw_agent_end=False,
                event_count=0,
                stream_valid=False,
                error_code=error_code,
            ),
            argv,
        )
    child_env = (
        {**env, "PI_SKIP_VERSION_CHECK": "1", "PI_OFFLINE": "1"}
        if env is not None
        else build_rpc_environment(dict(os.environ))
    )
    Path(layout.prime_stderr).parent.mkdir(parents=True, exist_ok=True)
    Path(layout.prime_stderr).write_bytes(b"")
    try:
        popen = subprocess.Popen(  # noqa: S603 - resolved argv, shell=False
            argv,
            cwd=os.fspath(layout.candidate),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_env,
            bufsize=0,
            **popen_kwargs_for_platform(),
        )
    except OSError:
        Path(layout.prime_events).touch(exist_ok=True)
        Path(layout.prime_stderr).touch(exist_ok=True)
        return (
            PrimeObservation(
                launched=False,
                exit_code=None,
                session_id=None,
                saw_agent_start=False,
                saw_agent_end=False,
                event_count=0,
                error_code="PRIME_LAUNCH_FAILED",
            ),
            argv,
        )
    if popen.stdin is None or popen.stdout is None:
        owned = OwnedProcess(popen, argv, [])
        owned.terminate_tree_best_effort()
        raise RuntimeError("Prime RPC process pipes were not created")
    owned = OwnedProcess(popen, argv, [])
    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=4)
    reader_stop = threading.Event()

    def enqueue_stdout(item: bytes | None) -> bool:
        while not reader_stop.is_set():
            try:
                chunks.put(item, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def read_stdout() -> None:
        try:
            while True:
                chunk = popen.stdout.read(64 * 1024)
                if not chunk:
                    break
                if not enqueue_stdout(chunk):
                    return
        finally:
            if not reader_stop.is_set():
                enqueue_stdout(None)

    reader = threading.Thread(
        target=read_stdout,
        name=f"prime-rpc-stdout-{popen.pid}",
        daemon=True,
    )
    reader.start()
    framer = RpcJsonlFramer(
        max_record_bytes=RPC_MAX_RECORD_BYTES,
        max_total_bytes=RPC_MAX_STREAM_BYTES,
    )
    validator = RpcLifecycleValidator(candidate_path=os.fspath(layout.candidate))
    followups_sent = False
    stats_seen = False
    final_state_seen = False
    deadline = time.monotonic() + float(timeout_seconds)
    protocol_error: RpcProtocolError | None = None
    timed_out = False
    host_terminated = False

    def terminate_owned() -> None:
        nonlocal host_terminated
        reader_stop.set()
        if popen.poll() is None:
            host_terminated = True
        owned.terminate_tree_best_effort()

    try:
        popen.stdin.write(_rpc_command("handshake-state", "get_state"))
        popen.stdin.flush()
        with open(layout.prime_events, "wb") as evidence:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    chunk = chunks.get(timeout=remaining)
                except queue.Empty:
                    timed_out = True
                    break
                if chunk is None:
                    framer.finish()
                    break
                records = framer.feed(chunk)
                evidence.write(chunk)
                for record in records:
                    validator.consume(record)
                    if record.get("type") == "response":
                        response_id = record.get("id")
                        if response_id == "handshake-state":
                            popen.stdin.write(
                                _rpc_command(
                                    "disable-retry", "set_auto_retry", enabled=False
                                )
                            )
                            popen.stdin.flush()
                        elif response_id == "disable-retry":
                            popen.stdin.write(
                                _rpc_command(
                                    "available-models", "get_available_models"
                                )
                            )
                            popen.stdin.flush()
                        elif response_id == "available-models":
                            popen.stdin.write(
                                _rpc_command("prompt", "prompt", message=prompt)
                            )
                            popen.stdin.flush()
                        elif response_id == "stats":
                            popen.stdin.write(
                                _rpc_command("final-state", "get_state")
                            )
                            popen.stdin.flush()
                    if record.get("type") == "agent_end" and not followups_sent:
                        popen.stdin.write(_rpc_command("stats", "get_session_stats"))
                        popen.stdin.flush()
                        followups_sent = True
                    if record.get("type") == "response":
                        stats_seen = stats_seen or record.get("id") == "stats"
                        final_state_seen = final_state_seen or record.get("id") == "final-state"
                    if (
                        followups_sent
                        and stats_seen
                        and final_state_seen
                        and not popen.stdin.closed
                    ):
                        popen.stdin.close()
            evidence.flush()
            os.fsync(evidence.fileno())
        if timed_out:
            terminate_owned()
        else:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                popen.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_owned()
    except RpcProtocolError as exc:
        protocol_error = exc
        terminate_owned()
    except (BrokenPipeError, OSError) as exc:
        protocol_error = RpcProtocolError("RPC_PREMATURE_EOF", str(exc))
        terminate_owned()
    finally:
        if not popen.stdin.closed:
            with contextlib.suppress(OSError):
                popen.stdin.close()
        owned.finish_output(timeout=5.0)
        reader_stop.set()
        reader.join(5.0)
        if reader.is_alive():
            with contextlib.suppress(OSError):
                popen.stdout.close()
            reader.join(1.0)
        if reader.is_alive() and protocol_error is None:
            protocol_error = RpcProtocolError(
                "RPC_READER_DID_NOT_STOP", "Prime RPC stdout reader did not stop."
            )

    if timed_out:
        return _failure_observation(
            validator,
            exit_code=None,
            error_code="RPC_TIMEOUT",
            timed_out=True,
            host_terminated=host_terminated,
        ), argv
    if protocol_error is not None:
        return _failure_observation(
            validator,
            exit_code=popen.poll(),
            error_code=protocol_error.error_code,
            host_terminated=host_terminated,
        ), argv
    try:
        result = validator.finish()
    except RpcProtocolError as exc:
        return _failure_observation(
            validator,
            exit_code=popen.poll(),
            error_code=exc.error_code,
        ), argv
    return (
        PrimeObservation(
            launched=True,
            exit_code=popen.returncode,
            session_id=result.session_id,
            saw_agent_start=True,
            saw_agent_end=True,
            event_count=result.event_count,
            final_text=result.final_text,
            stream_valid=True,
        ),
        argv,
    )


__all__ = ["build_rpc_argv", "build_rpc_environment", "run_prime_rpc"]
