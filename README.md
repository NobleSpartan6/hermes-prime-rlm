# Hermes Prime RLM

**Describe a coding task. Let Prime prepare a separate candidate. Review the result.**

This community plugin connects [Hermes Agent](https://github.com/NousResearch/hermes-agent)
to Prime Agent's Recursive Language Model runtime. It is useful when the evidence
for a coding task is too large to paste into a chat.

## Start here

| What you want | Where to start |
|---|---|
| Try the controls without an account or model charge | [Safe demo](#try-the-controls-for-free) |
| Use an existing Hermes + Prime installation | [Guided launcher](#open-the-guided-launcher) |
| Understand setup or an error | [Beginner guide](docs/getting-started.md) |
| Read the architecture and verification details | [Technical reference](REFERENCE.md) |

> **Preview feature:** the graphical launcher is on the desktop integration branch
> until its PR is merged and released. An older installed plugin will not recognize
> `--ui`. This is a source/plugin preview, not a signed desktop installer.

## Try the controls for free

From a complete checkout of this branch, run:

```bash
python scripts/desktop_demo.py
```

Python 3.11+ with Tk support is sufficient. **Hermes, Prime, Git, API keys, and a
paid model are not needed for this demo.** The clearly marked demo does not
call a model, edit your files, run tests, or produce a verification receipt.
On macOS/Linux your Python command may be `python3`; on Windows it may be `py`.

## Open the guided launcher

For real work you need Hermes, Git, and **Prime Agent 0.8.1 exactly**, with Prime's
own model/provider login already configured. The plugin does not currently ship
a complete installer for those prerequisites.

Install this checked-out preview into the **same Python environment that runs
Hermes**:

```bash
python -m pip install .
hermes plugins enable prime-rlm
hermes prime setup
hermes prime --ui
```

Do not install a wheel and a linked source copy of this plugin side by side.
Restart Hermes after installing or updating it. Setup adopts an existing exact
Prime runtime; it does not silently download missing runtimes or copy credentials.
See the [setup guide](docs/getting-started.md#first-time-setup) when it reports a
missing dependency.

In the window, choose your repository folder, describe the task, select a test
preset, and click **Review and run**. Nothing runs until you approve the request.
The exact task, folder, test arguments, and time limits appear in that review.

The window has a **Check setup** button and clear result messages. Use **Copy
candidate folder** or **Copy receipt path** to inspect the files in your normal
editor or file manager. It does not automatically apply, commit, or retry changes.

Already installed? A no-cost UI demonstration is also available with:

```bash
hermes prime --ui --demo
```

## Prefer chat?

Keep using Hermes normally. For example:

> Use Prime to investigate the failing tests in this repository. Prepare a
> separate candidate, run the tests, and show me the receipt. Do not apply it.

The existing `prime_agent` tool, setup commands, and verification contract are
unchanged. The window is an optional operator interface, not another model tool.

## What the result means

| Result | Meaning |
|---|---|
| Recorded host checks passed | The supplied commands ran and exited zero. Review is still required. |
| Completed without verification | A proposal exists, but no checks were supplied. Not a verified fix. |
| Checks failed / run failed | Inspect the candidate and receipt; the candidate may be partial. |
| Uncertain | The run lacks a reliable terminal boundary. Inspect it before starting another run. |

**This is not a sandbox.** Prime and verification commands run with your user
permissions. A separate Git worktree is not a security boundary. Repository data
may be sent to your configured provider, and provider fees may apply. Receipts
are unsigned and passing tests are not proof of correctness.

## Current limits

The launcher has one active run per window and no queue. Keep it open while work
is running; cancellation, restart/resume, and detailed agent-tree progress are not
yet implemented. The displayed Prime timeout does not include host checks and
evidence collection. Do not use multiple windows to bypass a run already in progress.

Native Windows, macOS, and Linux jobs remain required validation targets. See the
PR's actual CI results before treating this preview as release-ready.

For protocol details, benchmarks, research, and security boundaries, read the
[technical reference](REFERENCE.md), [desktop bridge contract](docs/desktop-rlm-v1.md),
and [trust boundaries](docs/trust-boundaries.md). The technical reference preserves
the previous README and predates the optional graphical launcher.

Independent community integration; not an official Nous Research or Prime
Intellect product. [MIT license](LICENSE).
