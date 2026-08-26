"""Shared fixtures: repo factory, fake-prime launchers, plugin data isolation.

The suite runs the REAL handler with the REAL fake process (no Popen mocks).
A temporary HERMES-style plugin-data root is injected through the fake
context so tests never touch a real Hermes profile.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

# The tests directory ships an __init__.py (spec §6 layout), so pytest imports
# this file as ``tests.conftest`` while test modules' ``from conftest import
# ...`` may create a second copy named ``conftest``. Two copies would each run
# _load() and produce distinct product-module objects, splitting registration
# state. Alias both names to ONE module object and make _load idempotent.
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

sys.modules.setdefault("conftest", sys.modules[__name__])
sys.modules.setdefault("tests.conftest", sys.modules[__name__])

REPO_ROOT = _TESTS_DIR.parent

# Import the plugin package under its hyphenated directory via a stable alias.
_spec = importlib.util.spec_from_file_location(
    "prime_rlm_pkg",
    REPO_ROOT / "__init__.py",
    submodule_search_locations=[str(REPO_ROOT)],
)
prime_rlm_pkg = importlib.util.module_from_spec(_spec)
sys.modules["prime_rlm_pkg"] = prime_rlm_pkg
_spec.loader.exec_module(prime_rlm_pkg)

FAKE_PRIME_AGENT = REPO_ROOT / "tests" / "fake_prime_agent.py"


def _load(name: str):
    module_key = f"prime_rlm_pkg.{name}"
    existing = sys.modules.get(module_key)
    if existing is not None and getattr(existing, "__file__", None):
        return existing  # already loaded by whichever conftest copy ran first
    module_path = REPO_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(module_key, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = module
    spec.loader.exec_module(module)
    return module


schemas = _load("schemas")
validation = _load("validation")
workspace = _load("workspace")
platform_runtime = _load("platform_runtime")
prime_protocol = _load("prime_protocol")
prime_process = _load("prime_process")
verification = _load("verification")
evidence = _load("evidence")
receipt_mod = _load("receipt")
receipt = receipt_mod  # alias matching test-module import names
tools = _load("tools")


class FakeState:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class FakeContext:
    """Registration/handler context double mirroring PluginContext's surface."""

    def __init__(self, data_root: Path, settings: dict | None = None) -> None:
        self.state = FakeState(data_root)
        self._settings = dict(settings or {})
        self.registered_tools: list[dict] = []

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def register_tool(self, *, name, toolset, schema, handler, description="", **_extra):
        self.registered_tools.append(
            {
                "name": name,
                "toolset": toolset,
                "schema": schema,
                "handler": handler,
                "description": description,
            }
        )


@pytest.fixture()
def plugin_data_root(tmp_path) -> Path:
    root = tmp_path / "plugin-data" / "prime-rlm"
    root.mkdir(parents=True)
    return root


@pytest.fixture()
def fake_ctx(plugin_data_root, tmp_path):
    ctx = FakeContext(plugin_data_root)
    # Default operator config: the direct fake invocation (no live prime-agent
    # required). Individual tests may override ctx._settings.
    command, _shim = make_fake_prime_command(tmp_path)
    ctx._settings["prime_agent_command"] = list(command)
    return ctx


def make_fake_prime_command(tmp_path: Path) -> tuple[list[str], Path]:
    """Build an operator-style command prefix that runs the real fake.

    Windows: a temporary ``.cmd`` shim so the real PATHEXT/shim resolution
    path is exercised. POSIX: an executable stub script.
    """
    if sys.platform == "win32":
        shim = tmp_path / "bin" / "fake-prime-agent.cmd"
        shim.parent.mkdir(parents=True, exist_ok=True)
        python = sys.executable
        shim.write_text(
            "@echo off\r\n"
            f'"{python}" "{FAKE_PRIME_AGENT}" %*\r\n',
            encoding="utf-8",
        )
        # NOTE: the .cmd route is exercised by platform-runtime-specific
        # tests. For end-to-end runs the direct python invocation keeps the
        # goal-free argv contract intact on every developer machine:
        return [sys.executable, str(FAKE_PRIME_AGENT)], shim
    launcher = tmp_path / "bin" / "fake-prime-agent"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" "{FAKE_PRIME_AGENT}" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return [str(launcher)], launcher


@pytest.fixture()
def fake_prime_direct(tmp_path):
    """Direct (non-shim) command prefix for the fake agent."""
    command, _shim = make_fake_prime_command(tmp_path)
    return command


@pytest.fixture()
def clean_repo(tmp_path):
    """A clean temporary Git repository with one commit and README."""
    repo = tmp_path / "source-repo"
    repo.mkdir()
    env = {**os.environ}
    git = _git_exe()
    def run(*args: str, cwd: Path = repo) -> None:
        subprocess.run(
            [git, *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            env=env,
        )

    run("init", "-b", "main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test Runner")
    (repo / "README.md").write_text("# sample\n", encoding="utf-8")
    (repo / "src.py").write_text("print('hello')\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "init")
    return repo


def _git_exe() -> str:
    import shutil

    resolved = shutil.which("git")
    assert resolved, "git must be on PATH for the test suite"
    return resolved


@pytest.fixture()
def passing_check():
    """Check argv that passes inside any candidate: python -c exit 0."""

    def build(name: str = "sanity") -> dict:
        return {
            "name": name,
            "argv": [sys.executable, "-c", "import sys; sys.exit(0)"],
            "timeout_seconds": 60,
        }

    return build


@pytest.fixture()
def failing_check():
    def build(name: str = "must_fail") -> dict:
        return {
            "name": name,
            "argv": [sys.executable, "-c", "import sys; sys.exit(3)"],
            "timeout_seconds": 60,
        }

    return build
