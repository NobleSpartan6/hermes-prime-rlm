"""Live demo: run prime_rlm_run through the REAL Hermes plugin stack.

Loads the plugin exactly as Hermes does (manifest -> register_tools ->
handler), then executes a full run against a real demo repository using the
operator-configured prime_agent_command. Prints the compact result and the
receipt. This is what a Hermes session would invoke.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "tests"))

# --- Load the plugin the way Hermes does -------------------------------------
import importlib.util

spec = importlib.util.spec_from_file_location(
    "prime_rlm_pkg", REPO / "__init__.py", submodule_search_locations=[str(REPO)]
)
pkg = importlib.util.module_from_spec(spec)
sys.modules["prime_rlm_pkg"] = pkg
spec.loader.exec_module(pkg)

from hermes_cli.plugins import PluginContext, PluginManager, PluginState  # noqa: E402

manager = PluginManager()
manifest = manager._parse_manifest(REPO / "plugin.yaml", REPO, prefix=None, source="user")
ctx = PluginContext(manifest, manager)

registered: list[dict] = []
ctx.register_tool = lambda **kw: registered.append(kw)  # capture, not global reg
pkg.register(ctx)
tool = registered[0]
print(f"[demo] registered tool: {tool['name']} (toolset={tool['toolset']})")

# ctx.state is a read-only property building PluginState(plugin_id,
# skill_namespace) — the same data dir our resolver reads. Show it:
state_dir = PluginState(ctx.plugin_id, manifest.skill_namespace).data_dir
print(f"[demo] plugin-data root: {state_dir}")

# --- Build a real demo repository --------------------------------------------
demo = Path(tempfile.mkdtemp(prefix="prime-rlm-demo-")) / "repo"
demo.mkdir(parents=True)


def git(*args: str) -> None:
    subprocess.run(  # noqa: S603, S607 - fixed git argv, demo helper
        ["git", *args],
        cwd=demo,
        check=True,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


git("init", "-b", "main")
git("config", "user.email", "demo@example.com")
git("config", "user.name", "Demo")
(demo / "app.py").write_text("def greet(name):\n    return f'Hello, {name}!'\n", encoding="utf-8")
(demo / "README.md").write_text("# demo app\n", encoding="utf-8")
git("add", "-A")
git("commit", "-m", "initial commit")
print(f"[demo] repository: {demo}")

# --- Invoke the tool against REAL Prime Agent --------------------------------
# OPENROUTER_API_KEY comes from Hermes' .env — load it manually (no dep).
_env_path = Path(
    os.environ.get("HERMES_HOME", Path.home() / "AppData" / "Local" / "hermes")
) / ".env"
for _line in _env_path.read_text(encoding="utf-8").splitlines():
    if _line.strip() and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip())

result = json.loads(
    tool["handler"](
        {
            "goal": "Add a farewell() function to app.py that returns a goodbye string, and document both greet() and farewell() in README.md. Keep the change minimal.",
            "repository_path": str(demo),
            "checks": [
                {
                    "name": "import-check",
                    "argv": [sys.executable, "-c", "import app; assert callable(app.greet) and callable(app.farewell)"],
                    "timeout_seconds": 60,
                },
            ],
            "runtime_timeout_seconds": 600,
        }
    )
)

print(json.dumps(result, indent=2, ensure_ascii=False))

if not result.get("receipt_path"):
    print("\n[demo] FAILED — no receipt produced.")
    raise SystemExit(1)

receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
print("\n[demo] receipt summary:")
r = receipt["receipt"]
print(f"  status:              {r['status']}")
print(f"  base commit:         {r['base_commit'][:12]}")
print(f"  candidate:           {r['candidate_path']}")
print(f"  changed (tracked):   {r['changed_paths']['modified']}")
print(f"  changed (untracked): {r['changed_paths']['untracked']}")
print(f"  checks passed:       {[c['name'] for c in r['checks'] if c['status'] == 'passed']}")
print(f"  tree digest:         {r['candidate_tree_sha256'][:16]}...")
print(f"  automatic retry:     {r['automatic_retry_allowed']}")
src_clean = (
    subprocess.run(  # noqa: S603, S607 - fixed git argv, demo helper
        ["git", "-C", str(demo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ).stdout.strip()
    == ""
)
print(f"  source repo clean:   {src_clean}")

print("\n[demo] OK — full pipeline executed through the real plugin surface.")
