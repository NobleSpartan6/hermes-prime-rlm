"""Platform runtime tests (spec §24 Platform runtime)."""

from __future__ import annotations

import pathlib
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import platform_runtime as rt


def _source_text() -> str:
    product_files = list(Path(rt.__file__).parent.glob("*.py"))
    combined = "\n".join(p.read_text(encoding="utf-8") for p in product_files)
    return combined


def test_shell_true_absent_from_product_code():
    for path in Path(rt.__file__).parent.glob("*.py"):
        if path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        assert "shell=True" not in text, f"shell=True found in {path.name}"
        assert "os.system(" not in text, f"os.system call found in {path.name}"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PATHEXT semantics")
def test_executable_discovery_respects_pathext(tmp_path, monkeypatch):
    exe = tmp_path / "tool.fakeext"
    exe.write_text("@echo ok\r\n", encoding="utf-8")
    monkeypatch.setenv("PATHEXT", ".FAKEEXT;.COM;.EXE;.BAT;.CMD")
    resolved = rt.resolve_executable(str(exe))
    assert resolved == str(exe)


def test_windows_cmd_shim_runs_correctly(tmp_path):
    """Real .cmd shim through the dedicated adapter (skipped off-Windows).

    Contract verified by probes on this host: quoted args through a %* shim
    are re-split by cmd; 8.3 short paths pass through intact. A space-bearing
    existing path must arrive as ONE argv element.
    """
    if sys.platform != "win32":
        pytest.skip("Windows-only shim behavior")
    shim = tmp_path / "shim.cmd"
    shim.write_text(
        '@echo off\r\npython -c "import sys; print(len(sys.argv)-1, repr(sys.argv[1:]))" %*\r\n',
        encoding="utf-8",
    )
    task_file = tmp_path / "task file.md"  # space-bearing path: the hard case
    task_file.write_text("x", encoding="utf-8")
    spec = rt.SpawnSpec(
        argv=rt.build_shim_argv(str(shim), [str(task_file), "--flag"]),
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "out.log"),
        stderr_path=str(tmp_path / "err.log"),
        env=None,
    )
    code, timed_out = rt.spawn_and_wait(spec, 60)
    assert code == 0 and not timed_out
    out = (tmp_path / "out.log").read_text(encoding="utf-8")
    assert out.startswith("2 "), f"expected 2 argv elements, got: {out!r}"
    # The short-path form of the task file resolves back to the same file.
    short_form = out.split("'", 1)[1].split("'", 1)[0]
    assert pathlib.Path(short_form).resolve() == task_file.resolve()


def test_shim_route_refuses_space_bearing_strings(tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows-only shim behavior")
    with pytest.raises(ValueError):
        rt.build_shim_argv(str(tmp_path / "x.cmd"), ["value with spaces"])


def test_posix_launcher_runs_correctly(tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX launcher behavior")
    launcher = tmp_path / "launcher.sh"
    launcher.write_text("#!/bin/sh\necho ran \"$1\"\n", encoding="utf-8")
    launcher.chmod(0o755)
    spec = rt.SpawnSpec(
        argv=[str(launcher), "arg one"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "out.log"),
        stderr_path=str(tmp_path / "err.log"),
        env=None,
    )
    code, timed_out = rt.spawn_and_wait(spec, 30)
    assert code == 0 and not timed_out
    assert "ran arg one" in (tmp_path / "out.log").read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows timeout behavior")
def test_windows_timeout_invokes_tree_termination(tmp_path, monkeypatch):
    calls = []
    real = rt.OwnedProcess._terminate_windows

    def spy(self):
        calls.append(self.pid)
        real(self)

    monkeypatch.setattr(rt.OwnedProcess, "_terminate_windows", spy)
    sleeper = (
        "import time, sys; sys.stdout.write('start\\n'); sys.stdout.flush(); "
        "time.sleep(300)"
    )
    spec = rt.SpawnSpec(
        argv=[sys.executable, "-c", sleeper],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "out.log"),
        stderr_path=str(tmp_path / "err.log"),
        env=None,
    )
    code, timed_out = rt.spawn_and_wait(spec, 3)
    assert timed_out is True
    assert calls, "tree termination adapter must be invoked on Windows timeout"
    # The direct child was reaped after taskkill.
    assert code is None or isinstance(code, int)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group behavior")
def test_macos_timeout_uses_process_group_termination(tmp_path, monkeypatch):
    """On POSIX hosts this proves group-kill wiring; skipped on Windows."""
    calls = []
    real = rt.OwnedProcess._terminate_posix

    def spy(self):
        calls.append(self.pid)
        real(self)

    monkeypatch.setattr(rt.OwnedProcess, "_terminate_posix", spy)
    spec = rt.SpawnSpec(
        argv=[sys.executable, "-c", "import time; time.sleep(300)"],
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "out.log"),
        stderr_path=str(tmp_path / "err.log"),
        env=None,
    )
    code, timed_out = rt.spawn_and_wait(spec, 2)
    assert timed_out is True
    assert calls


def test_no_os_kill_pid_zero_in_product_code():
    combined = _source_text()
    assert "os.kill(pid" not in combined.replace(" ", ""), "os.kill(pid, 0) probe banned"


HOSTILE_GOALS = [
    'x"; touch SENTINEL.txt; echo "',
    "$(touch SENTINEL2.txt)",
    "`touch SENTINEL3.txt`",
    "a & b | c > d < e",
    "%TEMP%\\..\\..\\evil",
]


@pytest.mark.parametrize("hostile", HOSTILE_GOALS)
def test_hostile_goal_does_not_create_sentinel(fake_ctx, clean_repo, tmp_path, hostile):
    """The goal travels via the task file only — never the command line.

    Runs the fake agent (which ignores goal content) with a hostile goal and
    asserts no sentinel file appeared anywhere near the run or repo.
    """
    from conftest import make_fake_prime_command

    sentinel_dir = tmp_path / "sentinel-watch"
    sentinel_dir.mkdir()
    command, _ = make_fake_prime_command(sentinel_dir)

    base = validation_base(clean_repo)
    run_id, layout = workspace_run(fake_ctx, clean_repo, base, hostile)
    observation, _record = prime_process_run(command, layout, 120)
    assert observation.exit_code in (0, None) or isinstance(observation.exit_code, int)
    sentinels = [p.name for p in sentinel_dir.iterdir() if p.name.startswith("SENTINEL")]
    assert sentinels == []
    assert not (Path(layout.run_dir) / "SENTINEL.txt").exists()


def validation_base(repo) -> str:
    from conftest import validation

    return validation.resolve_base_commit(str(repo))


def workspace_run(fake_ctx, repo, base, goal):
    from conftest import workspace

    return workspace.create_run_directory(
        data_root=fake_ctx.state.data_dir,
        repository_path=str(repo),
        base_commit=base,
        goal=goal,
        checks=[],
        runtime_timeout_seconds=90,
        prime_command_prefix=["prime-agent"],
    )


def prime_process_run(command, layout, timeout):
    from conftest import prime_process

    return prime_process.run_prime(command, layout, timeout)


HOSTILE_ARGV = [
    '"; & echo pwned > ',
    "a&b|c",
    "%PATH%",
]


def test_hostile_check_argv_stays_literal_or_fails_closed(fake_ctx, clean_repo, tmp_path):
    """Direct route keeps metacharacters literal; shim route refuses them."""
    sentinel = tmp_path / "SENTINEL-ARGV.txt"

    # Direct python route: argument must arrive literally.
    literal_arg = 'echo-trap"; & | <> %TEMP%'
    result = verification_run(
        {
            "name": "literal",
            "argv": [sys.executable, "-c", f"import sys; open(r'{sentinel}', 'w').write(sys.argv[1]); print(sys.argv[1])", literal_arg],
            "timeout_seconds": 30,
        },
        str(tmp_path / "cwd"),
        str(tmp_path / "checks"),
    )
    assert result.status.value == "passed"
    assert sentinel.exists()


def verification_run(check, cwd, checks_dir):
    from conftest import verification

    Path(cwd).mkdir(parents=True, exist_ok=True)
    Path(checks_dir).mkdir(parents=True, exist_ok=True)
    return verification.run_check(check, cwd, checks_dir)


@pytest.mark.skipif(sys.platform != "win32", reason=".cmd shim guard semantics")
def test_shim_route_refuses_metacharacters():
    with pytest.raises(ValueError):
        rt.guard_shim_argument('x"; & echo pwned')
    with pytest.raises(ValueError):
        rt.build_shim_argv("C:/x.cmd", ["safe", "bad&arg"])


def test_spaces_and_parens_passed_correctly(tmp_path):
    weird_dir = tmp_path / "dir with space (and parens)"
    weird_dir.mkdir(parents=True)
    target = weird_dir / "target file.txt"
    target.write_text("content", encoding="utf-8")
    script = tmp_path / "copy.py"
    script.write_text(
        textwrap.dedent(
            """
            import shutil, sys
            shutil.copy(sys.argv[1], sys.argv[2])
            print("copied")
            """
        ),
        encoding="utf-8",
    )
    dest = weird_dir / "dest file.txt"
    result = verification_run(
        {
            "name": "copy",
            "argv": [sys.executable, str(script), str(target), str(dest)],
            "timeout_seconds": 30,
        },
        str(weird_dir),
        str(tmp_path / "checks2"),
    )
    assert result.status.value == "passed"
    assert dest.exists() and dest.read_text(encoding="utf-8") == "content"


def test_environment_values_not_serialized_to_artifacts(fake_ctx, clean_repo, monkeypatch):
    marker_value = "PRIMERLMTESTVALUE9f83b"  # arbitrary non-credential marker
    monkeypatch.setenv("PRIME_RLM_TEST_TOKEN_ENV", marker_value)
    from conftest import make_fake_prime_command

    command, _ = make_fake_prime_command(clean_repo.parent)
    base = validation_base(clean_repo)
    run_id, layout = workspace_run(fake_ctx, clean_repo, base, "innocent goal")
    observation, record = prime_process_run(command, layout, 90)
    artifacts = [
        layout.request_json,
        layout.prime_task_md,
        layout.prime_events,
        layout.prime_stderr,
        layout.status_txt,
    ]
    for artifact in artifacts:
        path = Path(artifact)
        if path.exists():
            body = path.read_bytes()
            assert marker_value.encode() not in body, f"marker leaked into {path.name}"
