"""Optional native Tk launcher. Tk is imported ONLY on an explicit UI launch.

The trusted local operator sees the exact request and confirms it. Work and
readiness probes stay off the Tk thread. No HTTP server, telemetry, repository
scan, credential collection, configuration edits, or automatic retries live here.
"""

from __future__ import annotations

import queue
import sys
import threading
from uuid import uuid4

from .desktop_workflow import (
    PRESETS,
    FormError,
    build_request,
    demo_runner,
    error_help,
    readiness_text,
    result_text,
    submit_with_consent,
)

MISSING_TK = (
    "The graphical launcher needs Python's optional Tk support. On macOS/Windows, "
    "use a Python installation that includes Tcl/Tk. On Linux, install your "
    "distribution's matching python3-tk package. Check with: python -m tkinter\n"
    "The Hermes chat tool and hermes prime doctor still work without the GUI."
)
NO_DISPLAY = (
    "A desktop display could not be opened. Launch this command from a graphical "
    "desktop session, not a headless SSH session. Use hermes prime doctor --json "
    "or the Hermes chat tool on headless machines."
)


class Launcher:
    """UI-thread-only widgets; one bounded controller per launcher instance."""

    def __init__(self, root, tk, ttk, dialogs, scrolled_text, ctx, *, demo=False,
                 controller=None, inspect_fn=None):
        from .desktop import DesktopRunController

        self.root, self.tk, self.dialogs = root, tk, dialogs
        self.ctx, self.demo = ctx, demo
        self.controller = controller or DesktopRunController(
            ctx, runner=demo_runner if demo else None,
        )
        self.inspect_fn = inspect_fn
        self.active = False
        self.request_id = None
        self.references = {}
        self.readiness_running = False
        self.probe_results = queue.Queue(maxsize=1)
        self.editable = []
        self._form_state = {}
        root.title("Hermes Prime — " + ("Safe demo" if demo else "Guided launcher"))
        root.minsize(650, 600)
        root.geometry("820x760")
        root.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(3, weight=1)
        frame.rowconfigure(10, weight=1)
        ttk.Label(frame, text="Hermes Prime", font=("TkDefaultFont", 22, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w",
        )
        banner = (
            "DEMO — no model calls, no repository changes, no charges."
            if demo else "Describe a task. Review a candidate. Keep control of your project."
            # No inference starts when the window opens.
        )
        ttk.Label(frame, text=banner, wraplength=760).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 16),
        )
        self.folder = tk.StringVar()
        self.goal = scrolled_text(
            frame, height=5, wrap="word", undo=True, font="TkDefaultFont",
        )
        self.preset = tk.StringVar(value="Python: pytest")
        self.minutes = tk.StringVar(value="20")
        self.python_path = tk.StringVar()
        ttk.Label(frame, text="1  Repository folder").grid(
            row=2, column=0, sticky="w", padx=(0, 12),
        )
        folder_entry = ttk.Entry(frame, textvariable=self.folder)
        folder_entry.grid(row=2, column=1, sticky="ew")
        browse = ttk.Button(frame, text="Browse…", command=self.browse_folder)
        browse.grid(row=2, column=2, padx=(8, 0))
        ttk.Label(frame, text="2  What should change?").grid(row=3, column=0, sticky="nw", pady=12)
        self.goal.grid(row=3, column=1, columnspan=2, sticky="nsew", pady=12)
        ttk.Label(frame, text="3  Verification").grid(row=4, column=0, sticky="w")
        combo = ttk.Combobox(
            frame, values=tuple(PRESETS), textvariable=self.preset, state="readonly",
        )
        combo.grid(row=4, column=1, columnspan=2, sticky="ew")
        combo.bind("<<ComboboxSelected>>", self.update_preset)
        self.preset_hint = tk.StringVar()
        ttk.Label(frame, textvariable=self.preset_hint, wraplength=520).grid(
            row=5, column=1, columnspan=2, sticky="w", pady=(4, 10),
        )
        ttk.Label(frame, text="Python executable").grid(row=6, column=0, sticky="w")
        self.python_entry = ttk.Entry(frame, textvariable=self.python_path)
        self.python_entry.grid(row=6, column=1, sticky="ew")
        self.python_browse = ttk.Button(
            frame, text="Browse…", command=self.browse_python,
        )
        self.python_browse.grid(row=6, column=2, padx=(8, 0))
        ttk.Label(
            frame, text="Optional: choose the project's virtual-environment Python; "
            "blank uses Hermes Python.", wraplength=520,
        ).grid(row=7, column=1, columnspan=2, sticky="w", pady=(4, 8))
        ttk.Label(frame, text="Prime limit (minutes)").grid(row=8, column=0, sticky="w")
        minutes = ttk.Spinbox(frame, from_=1, to=60, textvariable=self.minutes, width=6)
        minutes.grid(row=8, column=1, sticky="w")
        actions = ttk.Frame(frame)
        actions.grid(row=9, column=0, columnspan=3, sticky="ew", pady=12)
        self.setup_button = ttk.Button(actions, text="Check setup", command=self.check_setup)
        self.setup_button.pack(side="left")
        self.run_button = ttk.Button(
            actions, text="Try demo" if demo else "Review and run…", command=self.start,
        )
        self.run_button.pack(side="right")
        self.output = scrolled_text(
            frame, height=8, wrap="word", state="disabled", font="TkDefaultFont",
        )
        self.output.grid(row=10, column=0, columnspan=3, sticky="nsew")
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        self.status = tk.StringVar(value="Ready. Nothing starts until you choose Run.")
        ttk.Label(frame, textvariable=self.status, wraplength=760).grid(
            row=12, column=0, columnspan=3, sticky="w",
        )
        footer = ttk.Frame(frame)
        footer.grid(row=13, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.copy_buttons = {}
        for key, label in (
            ("candidate_path", "Copy candidate folder"), ("receipt_path", "Copy receipt path"),
        ):
            button = ttk.Button(
                footer, text=label, state="disabled", command=lambda k=key: self.copy_reference(k),
            )
            button.pack(side="left", padx=(0, 8))
            self.copy_buttons[key] = button
        self.editable = [folder_entry, browse, self.goal, combo, minutes,
                         self.python_entry, self.python_browse]
        self._form_state[combo] = "readonly"
        self.update_preset()
        self.set_output(
            "Start with Check setup. Select the top-level folder of a clean Git repository.\n\n"
            "Real runs execute generated code and repository tests with your user permissions. "
            "This is not a sandbox. Provider fees may apply. "
            "Changes are never automatically applied."
        )
        if demo:
            self.folder.set("Demo only — no folder will be opened")
            self.goal.insert("1.0", "Show me how a proposed fix is reviewed.")
            self.preset.set("No verification")
            self.update_preset()
            for widget in self.editable:
                widget.configure(state="disabled")
            self.setup_button.configure(state="disabled")
            self.set_output(
                "This demo illustrates the controls only. It does not run an agent or create files."
            )

    def set_output(self, text):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")

    def update_preset(self, _event=None):
        kind, hint = PRESETS[self.preset.get()]
        self.preset_hint.set(hint)
        state = "normal" if kind in {"pytest", "unittest"} and not self.active else "disabled"
        self.python_entry.configure(state=state)
        self.python_browse.configure(state=state)

    def browse_folder(self):
        chosen = self.dialogs[0].askdirectory(
            parent=self.root, title="Choose the Git repository root", mustexist=True,
        )
        if chosen:
            self.folder.set(chosen)

    def browse_python(self):
        chosen = self.dialogs[0].askopenfilename(
            parent=self.root, title="Choose the project's Python executable",
        )
        if chosen:
            self.python_path.set(chosen)

    def check_setup(self):
        if self.active or self.readiness_running or self.demo:
            return
        self.readiness_running = True
        self.setup_button.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self.status.set("Checking local setup without a model request…")

        def probe():
            try:
                if self.inspect_fn is None:
                    from .setup_gate import inspect_readiness

                    report = inspect_readiness(self.ctx)
                else:
                    report = self.inspect_fn(self.ctx)
                text = readiness_text(report)
            except Exception:
                text = error_help("READINESS_FAILED")
            self.probe_results.put(text)

        try:
            threading.Thread(target=probe, name="hermes-prime-ui-readiness", daemon=False).start()
        except RuntimeError:
            self.probe_results.put(error_help("READINESS_FAILED"))
        self.root.after(100, self.poll_setup)

    def poll_setup(self):
        try:
            text = self.probe_results.get_nowait()
        except queue.Empty:
            self.root.after(100, self.poll_setup)
            return
        self.readiness_running = False
        self.setup_button.configure(state="normal")
        self.run_button.configure(state="normal")
        self.status.set("Setup check finished. No configuration was changed.")
        self.set_output(
            text + "\n\nFor guided repairs, open a terminal and run: hermes prime setup"
        )

    def confirm(self, text):
        """Scrollable exact-request review; approval is default-negative."""
        from tkinter import scrolledtext, ttk

        window = self.tk.Toplevel(self.root)
        window.title("Review before running")
        window.geometry("700x540")
        window.transient(self.root)
        accepted = [False]
        view = scrolledtext.ScrolledText(window, wrap="word", height=18)
        view.pack(fill="both", expand=True, padx=16, pady=16)
        view.insert("1.0", text)
        view.configure(state="disabled")
        bar = ttk.Frame(window, padding=16)
        bar.pack(fill="x")

        def approve():
            accepted[0] = True
            window.destroy()

        cancel = ttk.Button(bar, text="Go back", command=window.destroy)
        cancel.pack(side="left")
        ttk.Button(bar, text="Approve and run once", command=approve).pack(side="right")
        window.bind("<Escape>", lambda _event: window.destroy())
        window.grab_set()
        cancel.focus_set()
        self.root.wait_window(window)
        return accepted[0]

    def start(self):
        if self.active or self.readiness_running:
            return
        try:
            if self.demo:
                request_id = str(uuid4())
                self.controller.submit(self.controller.session_id, request_id, {
                    "action": "run", "goal": "Demonstrate controls only", "checks": [],
                })
            else:
                args = build_request(self.folder.get(), self.goal.get("1.0", "end-1c"),
                                     self.preset.get(), self.minutes.get(), self.python_path.get())
                request_id = submit_with_consent(self.controller, args, self.confirm)
                if request_id is None:
                    self.status.set("Not started. You can edit the request.")
                    return
        except FormError as exc:
            self.dialogs[1].showerror("Check the form", str(exc), parent=self.root)
            return
        except Exception as exc:
            self.set_output(error_help(getattr(exc, "code", None)))
            return
        self.request_id, self.active = request_id, True
        self.references = {}
        for button in self.copy_buttons.values():
            button.configure(state="disabled")
        for widget in self.editable:
            widget.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self.setup_button.configure(state="disabled")
        self.progress.start(80)
        self.set_output(
            "The request is running. This window does not yet show individual agent steps."
        )
        self.root.after(100, self.poll_run)

    def poll_run(self):
        try:
            snapshot = self.controller.snapshot(self.controller.session_id, self.request_id)
        except Exception:
            # Never free the run slot or offer replay when observation is lost.
            self.status.set(
                "Cannot read run status. Keep this window open; automatic retry is disabled."
            )
            self.root.after(1000, self.poll_run)
            return
        elapsed = int(snapshot["elapsed_ms"] / 1000)
        if snapshot["state"] not in {"finished", "rejected"}:
            self.status.set(
                f"Running — {elapsed // 60}m {elapsed % 60:02d}s. No automatic retries."
            )
            self.root.after(150, self.poll_run)
            return
        self.active = False
        self.progress.stop()
        self.progress["value"] = 0
        self.status.set("Demo finished." if self.demo else "Finished. Review the result below.")
        self.set_output(result_text(snapshot, demo=self.demo))
        self.references = self.controller.review_references(
            self.controller.session_id, self.request_id,
        )
        for key, button in self.copy_buttons.items():
            button.configure(state="normal" if key in self.references else "disabled")
        self.run_button.configure(state="normal")
        if not self.demo:
            self.setup_button.configure(state="normal")
            for widget in self.editable:
                widget.configure(state=self._form_state.get(widget, "normal"))
            self.update_preset()

    def copy_reference(self, key):
        value = self.references.get(key)
        if value:
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self.status.set("Copied local path. This does not revalidate its contents.")

    def close(self):
        if self.active or self.readiness_running:
            self.dialogs[1].showinfo(
                "Work is still running",
                "Keep this window open until the run or setup probe finishes. "
                "Cancellation is not available yet. The Prime timeout does not include "
                "host checks and evidence collection. No request will be retried automatically.",
                parent=self.root,
            )
            return
        self.controller.close()
        self.root.destroy()


def launch_ui(ctx=None, *, demo=False) -> int:
    if ctx is None and not demo:
        print("Launch real work through your Hermes profile: hermes prime --ui", file=sys.stderr)
        return 2
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except ImportError:
        print(MISSING_TK, file=sys.stderr)
        return 2
    try:
        root = tk.Tk()
    except tk.TclError:
        print(NO_DISPLAY, file=sys.stderr)
        return 2
    Launcher(root, tk, ttk, (filedialog, messagebox), scrolledtext.ScrolledText, ctx, demo=demo)
    root.mainloop()
    return 0
