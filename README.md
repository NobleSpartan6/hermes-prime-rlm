# Hermes Prime RLM

A standalone [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin
that runs a bounded coding goal through **Prime Agent** (RLM) inside a detached
Git worktree — and returns **independently executed verification evidence**.

> Hermes Prime RLM is an independent community integration. It is not an
> official Nous Research or Prime Intellect product.

## What it does

`prime_rlm_run` takes a goal, a clean Git repository, and explicit
verification commands, then:

1. Freezes the repository's exact current commit.
2. Creates a **detached candidate worktree** from that commit.
3. Runs Prime Agent (JSON event-stream mode) inside the candidate only.
4. **Independently validates** Prime's protocol (schema 3, one `agent_start`,
   one `agent_end`).
5. **Independently runs** your exact verification commands inside the candidate.
6. Writes a deterministic receipt (hashes of events, stderr, diff, tree) and
   returns a compact result.

The active source checkout is never modified. The candidate is never applied,
committed, merged, pushed, retried, or deleted — acceptance is always a human
(or host) decision.

## Why standalone

This ships as its own plugin repository rather than a core change: it is a
third-party integration (Prime Agent), it carries its own release cadence, and
Hermes' core stays narrow. It installs into the user plugin directory and uses
only public plugin APIs.

## Authority boundary

- **Hermes** owns the conversation, the decision to invoke the tool, and the
  final accept/discard decision.
- **Prime Agent** owns its RLM trajectory, IPython environment, recursive
  subagents, and its own model/provider configuration (the plugin never selects
  a model).
- **The plugin** owns admission, worktree creation, protocol validation, host
  verification, evidence, and receipts.

Prime's textual claims have **no verification authority**. "All tests passed"
is never evidence unless the plugin independently ran the recorded check and
observed exit code `0`.

## Status semantics

- `VERIFIED` — Prime exited 0 with a valid stream **and** every supplied host
  check independently exited 0. It means only that the exact recorded commands
  exited successfully against the recorded candidate. It does not prove
  semantic correctness, security, absence of hidden defects, reproducible
  builds, or production readiness.
- `COMPLETED_UNVERIFIED` — Prime finished cleanly but **no checks were
  supplied**. Never treated as success.
- `FAILED_VERIFICATION` — Prime finished but a check failed, timed out, or
  could not launch.
- `FAILED` — known failure (nonzero exit, launch failure, worktree failure).
  The candidate is preserved and may contain partial changes.
- `UNCERTAIN` — no reliable terminal boundary (runtime timeout, malformed or
  incomplete stream, duplicate/missing lifecycle events). No checks run, the
  candidate is preserved, `automatic_retry_allowed` is always `false`.

## No-replay behavior

One invocation = one Prime process. There is no automatic retry, ever. If a
run ends `UNCERTAIN`, inspect the preserved candidate and evidence and start a
new run explicitly if you choose to.

## Platform support

- **Windows 10/11 native** — first-class. Command resolution respects
  `PATHEXT`; `.cmd`/`.bat` shims go through a dedicated, injection-hardened
  cmd.exe adapter (verified empirically: quoted args through `%*` shims are
  re-split by cmd, so space-bearing paths are passed as 8.3 short paths, and
  cmd-active metacharacters are refused before spawn). Tree termination via
  `taskkill /PID <pid> /T /F` (argv list, no shell).
- **macOS native** — `start_new_session=True`; timeout escalates
  SIGTERM→SIGKILL against the owned process group.
- Linux may work but is not an acceptance target for v0.1.

## Requirements

- Python >= 3.11
- Git on PATH
- **Prime Agent `>=0.8.0,<0.9.0`** (the plugin probes `--version` and refuses
  anything else)
- Prime JSON event-stream schema **version 3** (checked independently of the
  version gate)
- On native Windows, Prime Agent itself may require Git Bash; the plugin does
  not require WSL.

## Installation

Clone or copy this repository, then link it into your Hermes user plugin
directory (Windows shown; on macOS use a symlink):

```powershell
# From an elevated-free shell — a junction needs no admin rights
New-Item -ItemType Junction -Path "$env:LOCALAPPDATA\hermes\plugins\prime-rlm" -Target "<repo-path>"
```

Enable it:

```bash
hermes plugins enable prime-rlm
```

Verify:

```bash
hermes plugins doctor <repo-path> --ci
hermes plugins list
```

A new Hermes session (or gateway restart) is needed to load the plugin.

## Configuring `prime_agent_command`

Operator-owned list of command-prefix tokens (never a model-facing argument):

```yaml
plugins:
  entries:
    prime-rlm:
      settings:
        prime_agent_command: ["prime-agent"]           # default
        # or: ["C:\\Program Files\\Git\\bin\\bash.exe", "C:\\path\\to\\prime-agent.sh"]
        # or: ["/opt/homebrew/bin/prime-agent"]
```

## Tool input example

```json
{
  "goal": "Add input validation to the signup form and cover it with tests.",
  "repository_path": "C:\\dev\\my-project",
  "checks": [
    { "name": "tests", "argv": ["python", "-m", "pytest", "-q"], "timeout_seconds": 600 }
  ],
  "runtime_timeout_seconds": 1200
}
```

## Run layout & receipt

```
<hermes home>/plugin-data/prime-rlm/runs/<run-id>/
├── request.json          # canonical request envelope (sha256 in receipt)
├── prime-task.md         # goal + fixed host rules (goal never on a CLI)
├── candidate/            # detached worktree — preserved after every outcome
├── prime-events.jsonl    # Prime stdout (validated independently)
├── prime-stderr.log
├── prime-version.txt
├── tracked.patch         # git diff --binary --full-index vs base
├── status.txt
├── checks/<name>/stdout.log, stderr.log
└── receipt.json          # {"receipt": {...}, "receipt_sha256": "..."}
```

## Inspecting / cleaning up a candidate

The candidate is an ordinary registered worktree:

```bash
cd "<candidate-path>"        # inspect files, run git diff, etc.
git -C <source-repo> worktree list
git -C <source-repo> worktree remove <candidate-path>   # manual cleanup only
```

Nothing is removed automatically.

## Non-goals (v0.1)

Prime Continuim integration, hostd, ACP/RPC, persistent sessions, resume,
background tool execution, live streaming, desktop/dashboard UI, MCP, remote
hosts, model selection, env-var input, `/refine`, goals/schedules/autonomy,
retries, auto-apply/commit/push, submodules, Docker, WSL.

## Tests

```bash
python -m pytest -q          # deterministic; never calls a live model
ruff check .
```

CI runs the suite natively on `windows-latest` and `macos-latest` (Python
3.11 and 3.12) using only the deterministic fake Prime executable.

## Security warning

This plugin is not a sandbox. Prime Agent executes model-generated code with
the current user's permissions. A detached Git worktree protects the active
checkout from ordinary edits, but it does not contain process access,
filesystem access, network access, credentials, malicious code, or
independently daemonized descendants.

## Privacy warning

Do not use the plugin on repositories containing secrets or confidential
material unless the configured Prime Agent model and provider are approved for
that data.

## Troubleshooting

- **`.cmd` shim issues** — the plugin passes space-bearing paths through shims
  as 8.3 short paths and refuses cmd-metacharacter args before spawn. If your
  shim needs richer args, point `prime_agent_command` at the underlying
  executable directly (e.g. `["python", "-m", "prime_agent"]` or a `.exe`).
- **PATH problems** — the first token must resolve via absolute path or
  `shutil.which`; check with `where prime-agent` (Windows) / `which prime-agent`.
- **Git Bash on Windows** — if Prime Agent requires bash, use the two-token
  prefix form shown above.
- **Spaces in Windows paths** — fully supported on the direct route; the shim
  route uses short paths (verify the volume has 8.3 name generation enabled
  with `fsutil 8dot3name query C:`).
- **Version mismatch** — `UNSUPPORTED_PRIME_VERSION` means your prime-agent is
  outside `>=0.8.0,<0.9.0`; `UNPARSEABLE_PRIME_VERSION` means `--version`
  output could not be parsed (check wrappers that print extra text).
