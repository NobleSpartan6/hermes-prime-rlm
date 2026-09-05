"""Desktop -> unchanged Hermes handler -> real Git/fake Prime/host verifier.

Runs with the existing cross-platform CI fixtures, never a live provider.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import _load, receipt_mod, validation

desktop = _load("desktop")


@pytest.mark.parametrize("check_kind, expected", [
    ("pass", "VERIFIED"), ("fail", "FAILED_VERIFICATION"), ("none", "COMPLETED_UNVERIFIED"),
])
def test_desktop_preserves_real_handler_and_receipt_semantics(
    fake_ctx, clean_repo, passing_check, failing_check, check_kind, expected,
):
    fake_ctx._settings["prime_agent_command"] += ["--fake-scenario", "success_tracked_change"]
    checks = {"pass": [passing_check()], "fail": [failing_check()], "none": []}[check_kind]
    controller = desktop.DesktopRunController(fake_ctx)
    key = str(uuid4())
    args = {
        "goal": "Add a greeting module.", "repository_path": str(clean_repo),
        "checks": checks, "runtime_timeout_seconds": 120,
    }
    try:
        controller.submit(controller.session_id, key, args)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            snapshot = controller.snapshot(controller.session_id, key)
            if snapshot["state"] == "finished":
                break
            time.sleep(0.01)
        else:
            pytest.fail("fake Prime desktop run failed to finish within 30 seconds")
        assert snapshot["status"] == expected
        assert snapshot["verified"] is (check_kind == "pass")
        assert snapshot["automatic_retry_allowed"] is False
        references = controller.review_references(controller.session_id, key)
        envelope = json.loads(Path(references["receipt_path"]).read_text(encoding="utf-8"))
        assert receipt_mod.digest_of(envelope["receipt"]) == snapshot["receipt_sha256"]
        assert Path(references["candidate_path"]).exists()
        assert validation.require_clean_repository(str(clean_repo)) is None
        assert "Candidate edit" not in (clean_repo / "README.md").read_text(encoding="utf-8")
        before = list((fake_ctx.state.data_dir / "runs").iterdir())
        controller.submit(controller.session_id, key, args)
        assert list((fake_ctx.state.data_dir / "runs").iterdir()) == before
    finally:
        controller.close(wait=True)
