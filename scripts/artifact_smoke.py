#!/usr/bin/env python3
"""Clean-installed wheel smoke: entry point registration and fake-Prime dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import hermes_prime_rlm

ROOT = Path(__file__).resolve().parents[1]
FAKE_PRIME = ROOT / "tests" / "fake_prime_agent.py"


class State:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class Context:
    def __init__(self, data_dir: Path) -> None:
        self.state = State(data_dir)
        self.registered: list[dict] = []
        self.settings = {
            "prime_agent_command": [sys.executable, os.fspath(FAKE_PRIME)],
        }

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **definition):
        self.registered.append(definition)


def run(command: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def main() -> int:
    module_path = Path(hermes_prime_rlm.__file__).resolve()
    if module_path.parent == ROOT:
        raise AssertionError(f"source checkout shadowed installed wheel: {module_path}")

    with tempfile.TemporaryDirectory(prefix="prime-rlm-artifact-") as raw:
        temp = Path(raw)
        repository = temp / "repository"
        repository.mkdir()
        run(["git", "init"], repository)
        run(["git", "config", "user.email", "ci@example.invalid"], repository)
        run(["git", "config", "user.name", "CI"], repository)
        (repository / "README.md").write_text("base\n", encoding="utf-8")
        run(["git", "add", "README.md"], repository)
        run(["git", "commit", "-m", "base"], repository)

        data_dir = temp / "plugin-data"
        data_dir.mkdir()
        context = Context(data_dir)
        hermes_prime_rlm.register(context)
        assert len(context.registered) == 1
        definition = context.registered[0]
        assert definition["name"] == "prime_rlm_run"

        previous = os.environ.get("FAKE_PRIME_SCENARIO")
        os.environ["FAKE_PRIME_SCENARIO"] = "success_tracked_change"
        try:
            result = json.loads(
                definition["handler"](
                    {
                        "goal": "Add a greeting module.",
                        "repository_path": os.fspath(repository),
                        "checks": [
                            {
                                "name": "artifact-smoke",
                                "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                                "timeout_seconds": 60,
                            }
                        ],
                        "runtime_timeout_seconds": 120,
                    },
                    source="artifact-smoke",
                )
            )
        finally:
            if previous is None:
                os.environ.pop("FAKE_PRIME_SCENARIO", None)
            else:
                os.environ["FAKE_PRIME_SCENARIO"] = previous

        assert result["status"] == "VERIFIED", result
        assert result["ok"] is True, result
        assert result["completed"] is True, result
        assert result["verified"] is True, result
        assert Path(result["receipt_path"]).is_file(), result
        assert run(["git", "status", "--porcelain"], repository) == ""

    print(
        "installed-wheel-dispatch PASS",
        hermes_prime_rlm.PLUGIN_VERSION,
        module_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
