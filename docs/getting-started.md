# Getting started with the guided launcher

The launcher turns the existing Prime RLM tool into a form. It does not replace
Hermes, add a second model tool, install a model, or create a sandbox.

## Try it before setting anything up

Obtain a complete source checkout of the desktop integration branch. Open a
terminal in that folder and run `python scripts/desktop_demo.py`.

You will see a clearly marked **DEMO** window. Click **Try demo** to see how a
result is displayed. It is a simulation of the controls: no model call, repository
access, tests, credentials, generated candidate, or verification receipt.

This preview is not a double-click installer. It requires Python 3.11 or newer.
When your system uses `python3` or `py` instead of `python`, substitute that name.

## First-time setup

Real runs require an existing Hermes installation, Git, and **Prime Agent 0.8.1**.
Prime must have its own provider/model login. The plugin cannot borrow a Hermes
credential, and local readiness does not prove that a provider will accept a live
request. GPT-6 Astra compatibility is not established by this launcher.

Install the preview from its source folder using the Python environment that
runs Hermes, not an unrelated system Python:

```text
python -m pip install .
hermes plugins enable prime-rlm
hermes prime setup
hermes prime --ui
```

Stop and restart Hermes after installation or update. Use only one plugin source
(wheel or linked checkout), not both. Existing installation choices are preserved
in [the technical reference](../REFERENCE.md#installation).

`hermes prime setup` checks your current runtime and proposes only supported
configuration repairs. It asks before changing configuration. When dependencies
are missing or the Prime version is wrong, it stops rather than doing a mutable
or global install. There is no complete managed-runtime installer in this preview.
A maintainer or the person who set up Hermes must complete the missing runtime
installation. Do not download arbitrary executables to bypass these checks.

The GUI's **Check setup** is read-only with respect to your profile configuration.
It runs the existing local readiness probes without inference and explains how
to continue. It does not collect credentials or automatically repair configuration.

## Use the form

**Repository folder.** Select the top-level folder of an existing Git repository.
Save any work first. The repository must be clean: commit or stash existing edits
using your normal Git client. Choosing a random documents folder will not work.

**What should change?** Give a specific goal, for example: “Investigate why the
payment parser tests fail, propose a fix, and preserve the existing public API.”
The goal limit is 20,000 characters; large evidence should stay in the repository,
not be pasted into this field.

**Verification.** Choose the test system that the project actually uses:

| Preset | Recorded command |
|---|---|
| Python: pytest | Chosen Python, then `-m pytest -q` |
| Python: unittest | Chosen Python, then `-m unittest discover -v` |
| Node.js: npm test | `npm test` |
| Rust: cargo test | `cargo test` |
| No verification | No command; result cannot be VERIFIED |

The GUI does not guess the test runner by executing repository files. These are
fixed argument lists, not shell text. The test preset may not suit a monorepo or
custom build; use the existing Hermes tool for custom verification commands.

**Python executable.** Python presets use the Python running Hermes unless you
select a different interpreter. Choose the project's virtual-environment Python
when its test dependencies are installed there. This setting is not the separate
Prime RLM kernel interpreter. Non-Python presets ignore this field.

**Time limits.** Choose 1–60 minutes for Prime, default 20. The selected host test
has an additional 120-second timeout. Admission, checks, and evidence collection
mean the total wall-clock duration can exceed the Prime limit. There is no
promised global dollar cap.

**Review and run.** Read the exact goal, repository, command arguments, limits,
and permissions warning. The review is scrollable, and **Go back** is the safe
default. Approving starts one request. A second click does not queue another run.

## Review the result

The status shows whether recorded host checks passed, no checks were supplied,
verification failed, execution failed, or the outcome is uncertain. Model claims
like “all tests passed” are not verification evidence.

Use **Copy candidate folder**, paste the path into your editor/file manager's
folder-opening dialog, and inspect the diff. **Copy receipt path** locates the
machine-readable record. These buttons copy text; they do not launch arbitrary
commands or verify that the files still match a receipt. Paths on the clipboard
are visible to other software that can access your clipboard.

The launcher does not apply a candidate. Accepting changes is a separate,
intentional review decision. Candidates remain on disk until you clean them up
manually. Nothing is retried or silently deleted. Review a previous run before
starting a new one: this window retains only the latest visible result, although
the existing backend retains its bounded session history and run artifacts.

## Common problems

**The command says `--ui` is unknown.** You are using an older plugin. The GUI is
on the desktop integration branch until merged/released. Install that checkout in
the Hermes environment, remove duplicate plugin sources, and restart Hermes.

**Python has no Tk support.** Run `python -m tkinter` with the same interpreter.
On Windows/macOS, use a Python installation with Tcl/Tk. On Linux, install the
distribution's Tk package matching that Python (often `python3-tk`). The package
name varies; do not assume a system Tk package repairs a different custom Python.
The terminal/chat plugin remains usable without Tk.

**No graphical display.** Launch from a normal desktop session. Headless servers
and plain SSH sessions should use Hermes chat or `hermes prime doctor --json`.

**Setup is not ready.** Read the short error code, then run
`hermes prime doctor --json` and `hermes prime setup` in a terminal. Do not share
raw logs or receipts publicly without checking for private repository content.

**Tests cannot run.** Check that the preset matches the project and its dependencies
are installed. For Python, select the right virtual-environment executable. The
GUI does not install project dependencies or run package-manager setup scripts.

**The run is uncertain or timed out.** Inspect the preserved candidate and process
evidence before choosing another run. Do not assume the candidate is stable or
all independently daemonized descendants have stopped.

**The window will not close during work.** This first launcher does not have safe
cancellation. Keep it open until the run or readiness probe ends. Forced process
termination can leave an uncertain outcome. No retry or resume happens on restart.

## Privacy and trust

Opening the window sends no model request. Approving a real run may send repository
content to Prime's configured provider and incur charges. Generated code and host
checks execute with your account's permissions. Worktrees separate normal edits
but do not isolate filesystem, network, credentials, or process access. Receipts
are unsigned; the GUI does not revalidate artifacts. See
[the full trust model](trust-boundaries.md).
