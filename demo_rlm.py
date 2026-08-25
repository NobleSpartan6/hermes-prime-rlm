"""RLM-scale demo: run prime_rlm_run against a 1.5M-token log fixture.

Loads the plugin exactly as Hermes does and invokes the real handler. The
goal requires programming against data too large for any context window.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
FIXTURE = Path(r"C:\Users\ebene\hermes rlm\rlm-fixture\payments-api")
sys.path.insert(0, str(REPO / "tests"))

import importlib.util

spec = importlib.util.spec_from_file_location(
    "prime_rlm_pkg", REPO / "__init__.py", submodule_search_locations=[str(REPO)]
)
pkg = importlib.util.module_from_spec(spec)
sys.modules["prime_rlm_pkg"] = pkg
spec.loader.exec_module(pkg)

from hermes_cli.plugins import PluginContext, PluginManager  # noqa: E402

manager = PluginManager()
manifest = manager._parse_manifest(REPO / "plugin.yaml", REPO, prefix=None, source="user")
ctx = PluginContext(manifest, manager)
registered: list[dict] = []
ctx.register_tool = lambda **kw: registered.append(kw)
pkg.register(ctx)
tool = registered[0]
print(f"[rlm-demo] tool: {tool['name']}")

# env for Prime (kernel python override + provider key)
_env_path = Path(r"C:\Users\ebene\AppData\Local\hermes\.env")
for _line in _env_path.read_text(encoding="utf-8").splitlines():
    if _line.strip() and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip())
os.environ.setdefault(
    "PRIME_AGENT_KERNEL_PYTHON",
    str(Path.home() / ".prime" / "agent" / "kernel-venv" / "Scripts" / "python.exe"),
)

GOAL = (
    "The function error_rate() in app.py is buggy: it counts every physical line of "
    "events.log as a log entry, so stack-trace continuation lines are wrongly counted. "
    "Fix error_rate() so it counts only real log ENTRIES (lines whose second "
    "whitespace-separated field is a level like INFO/WARN/ERROR/DEBUG). Do NOT read "
    "events.log into memory or print it — it is far too large for any context window; "
    "stream it programmatically. Verify by running: python run_tests.py  (it must print PASS)."
)

result = json.loads(
    tool["handler"](
        {
            "goal": GOAL,
            "repository_path": str(FIXTURE),
            "checks": [
                {
                    "name": "analyzer-test",
                    "argv": [sys.executable, "run_tests.py"],
                    "timeout_seconds": 120,
                }
            ],
            "runtime_timeout_seconds": 1200,
        }
    )
)

print()
print(json.dumps(result, indent=2)[:2200])
Path(REPO / "rlm-demo-last-result.json").write_text(
    json.dumps(result, indent=2), encoding="utf-8"
)
if result.get("status") != "VERIFIED":
    print("\n[rlm-demo] NOT VERIFIED — see receipt for evidence.")
