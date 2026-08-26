"""End-to-end tests: real handler + real fake Prime process (no Popen mocks).

Successful flow proves the ten §24 properties; uncertain flow proves the
eight. Runs identically on Windows CI and macOS CI.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from conftest import tools


def _invoke(fake_ctx, repo, scenario, checks, timeout=120, extra_env=None):
    """Run the real handler against the real fake agent with a scenario.

    Timeout scenarios keep the fake's default 600s sleep, which exceeds every
    legal runtime budget (>=30s), so the handler's timeout path is exercised
    honestly. The UNCERTAIN e2e test therefore takes ~30s.
    """
    old = os.environ.get("FAKE_PRIME_SCENARIO")
    os.environ["FAKE_PRIME_SCENARIO"] = scenario
    try:
        raw = tools.handle_prime_rlm_run(
            {
                "goal": "Add a greeting module.",
                "repository_path": str(repo),
                "checks": checks,
                "runtime_timeout_seconds": timeout,
            },
            _ctx=fake_ctx,
            source="e2e-test",
        )
    finally:
        if old is None:
            os.environ.pop("FAKE_PRIME_SCENARIO", None)
        else:
            os.environ["FAKE_PRIME_SCENARIO"] = old
    return json.loads(raw)


def _passing_check():
    return {
        "name": "tests",
        "argv": [sys.executable, "-c", "import sys; sys.exit(0)"],
        "timeout_seconds": 60,
    }


@pytest.fixture()
def prime_command_ready(monkeypatch):
    """Point the operator config at the direct fake invocation."""
    from conftest import make_fake_prime_command

    holder = {}

    def provide(tmp_path):
        command, _shim = make_fake_prime_command(tmp_path)
        holder["command"] = command

        def fake_get_config(self, key, default=None):
            if key == "prime_agent_command":
                return command
            return default

        monkeypatch.setattr(
            "conftest.FakeContext.get_config",
            lambda self, key, default=None: (
                command if key == "prime_agent_command" else default
            ),
        )
        return command

    return provide


# ---------------------------------------------------------------------------
# Successful flow
# ---------------------------------------------------------------------------


def test_successful_flow_verified(fake_ctx, clean_repo, tmp_path, passing_check):
    """All ten success properties."""
    # 1. Source repository initially clean.
    from conftest import validation

    assert validation.require_clean_repository(str(clean_repo)) is None

    result = _invoke(fake_ctx, clean_repo, "success_tracked_change", [_passing_check()])

    assert result["status"] == "VERIFIED"
    assert result["ok"] is True

    receipt_path = Path(result["receipt_path"])
    # 7. Receipt exists.
    assert receipt_path.exists()
    envelope = json.loads(receipt_path.read_text(encoding="utf-8"))
    # 8. Receipt digest verifies.
    from conftest import receipt_mod

    assert (
        receipt_mod.digest_of(envelope["receipt"]) == envelope["receipt_sha256"]
        == result["receipt_sha256"]
    )
    # 9. Candidate remains inspectable.
    candidate = Path(result["candidate_path"])
    assert candidate.exists()
    # 3. Active checkout unchanged.
    active_readme = clean_repo / "README.md"
    assert "Candidate edit" not in active_readme.read_text(encoding="utf-8")
    status = validation.run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"], cwd=str(clean_repo)
    ).strip()
    assert status == ""

    # 10. No commit created in the candidate.
    base = validation.resolve_base_commit(str(clean_repo))
    candidate_head = validation.run_git(["rev-parse", "HEAD"], cwd=str(candidate)).strip()
    assert candidate_head == base  # still at base: edits are uncommitted

    # 11. No push occurred: no remote configured / no upstream.
    remotes = validation.run_git(["remote"], cwd=str(candidate)).strip()
    assert remotes == ""

    # 2. Fake Prime edited the candidate.
    changed = result["changed_paths"]
    assert changed["modified"] or changed["untracked"]

    # 4./5./6. Host check ran inside the candidate and passed → VERIFIED.
    check_row = result["checks"][0]
    assert check_row["name"] == "tests"
    assert check_row["status"] == "passed"

    # Changed paths recorded in the receipt match the compact result.
    assert envelope["receipt"]["changed_paths"] == changed

    # v0.1.1 separates observed facts instead of overloading VERIFIED.
    assert envelope["receipt"]["execution_status"] == "COMPLETED"
    assert envelope["receipt"]["candidate_status"] == "UNQUIESCED"
    assert envelope["receipt"]["verification_status"] == "PASSED"
    assert envelope["receipt"]["verification_authority"] == "MODEL_PROPOSED"
    assert envelope["receipt"]["integrity_status"] == "RECORDED_NOT_REVALIDATED"
    assert envelope["receipt"]["authenticity_status"] == "UNSIGNED"
    assert envelope["receipt"]["acceptance_status"] == "PENDING"
    assert envelope["receipt"]["candidate_stability"] == "unknown"
    assert envelope["receipt"]["candidate_may_have_partial_changes"] is True

    # v0.1.1: command identity is bound without persisting raw argv tokens.
    assert "prime_argv" not in envelope["receipt"]
    identity = envelope["receipt"].get("prime_command_identity")
    assert identity and len(identity["argv_sha256"]) == 64
    assert len(identity["executable_sha256"]) == 64
    assert identity["executable_name"]
    command_prefix = fake_ctx.get_config("prime_agent_command")
    if len(command_prefix) == 1:
        # POSIX executes the launcher script directly as argv[0], so its bytes
        # are already bound by executable_sha256. There is no separate
        # file-valued script argument to hash.
        assert identity["script_name"] == ""
        assert identity["script_sha256"] == ""
    else:
        # Windows runs python.exe with fake_prime_agent.py as argv[1].
        assert identity["script_name"] == "fake_prime_agent.py"
        assert len(identity["script_sha256"]) == 64

    # v0.1.1: proposal digest (pre-check) present and matches post-check when
    # the check did not mutate anything.
    proposal = envelope["receipt"].get("proposal_tree_sha256")
    assert proposal, "receipt must record pre-check proposal tree digest"
    assert proposal == envelope["receipt"]["candidate_tree_sha256"]

    # v0.1.1: real duration — started_at precedes finished_at by > 0.
    started = envelope["receipt"]["started_at"]
    finished = envelope["receipt"]["finished_at"]
    assert started < finished, f"started_at {started} must precede finished_at {finished}"


def test_receipt_records_source_checkout_unchanged(fake_ctx, clean_repo, passing_check):
    """The active checkout's pre/post identity is bound into the receipt."""
    result = _invoke(fake_ctx, clean_repo, "success_tracked_change", [passing_check()])
    assert result["status"] == "VERIFIED"
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["receipt"]["source_checkout_unchanged"] is True


def test_post_admission_internal_failure_still_writes_failed_receipt(
    fake_ctx, clean_repo, monkeypatch
):
    """Every admitted run gets a receipt, even when evidence finalization crashes."""

    def boom(*_args, **_kwargs):
        raise OSError("simulated evidence failure")

    monkeypatch.setattr(tools, "_collect_evidence", boom)
    result = _invoke(fake_ctx, clean_repo, "success_tracked_change", [_passing_check()])
    assert result["status"] == "FAILED"
    assert result["ok"] is False
    assert result["receipt_path"]
    envelope = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert envelope["receipt"]["status"] == "FAILED"
    assert envelope["receipt"]["candidate_status"] == "UNQUIESCED"
    assert envelope["receipt"]["authenticity_status"] == "UNSIGNED"
    assert envelope["receipt"]["verification_authority"] == "MODEL_PROPOSED"


def test_post_admission_validation_error_still_finalizes_receipt(
    fake_ctx, clean_repo, monkeypatch
):
    from conftest import schemas

    def fail_worktree(*_args, **_kwargs):
        raise schemas.ValidationError("WORKTREE_TEST_FAILURE", "simulated")

    monkeypatch.setattr(tools, "create_detached_worktree", fail_worktree)
    result = _invoke(fake_ctx, clean_repo, "success_tracked_change", [_passing_check()])
    assert result["status"] == "FAILED"
    assert result["error_code"] == "WORKTREE_TEST_FAILURE"
    envelope = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert envelope["receipt"]["status"] == "FAILED"
    assert envelope["receipt"]["error_code"] == "WORKTREE_TEST_FAILURE"
    assert envelope["receipt"]["verification_authority"] == "MODEL_PROPOSED"


def test_missing_proposal_digest_degrades_before_checks(fake_ctx, clean_repo, monkeypatch):
    """No proposal identity means no check execution and no VERIFIED label."""
    monkeypatch.setattr(tools, "candidate_tree_digest_safe", lambda _layout: "")
    result = _invoke(fake_ctx, clean_repo, "success_tracked_change", [_passing_check()])
    assert result["status"] == "UNCERTAIN"
    assert result["checks"] == []
    envelope = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert envelope["receipt"]["verification_status"] == "NOT_RUN"


def test_second_invocation_creates_distinct_run(fake_ctx, clean_repo, passing_check):
    first = _invoke(fake_ctx, clean_repo, "success_no_changes", [])
    second = _invoke(fake_ctx, clean_repo, "success_no_changes", [])
    assert first["run_id"] != second["run_id"]
    assert Path(first["candidate_path"]) != Path(second["candidate_path"])
    assert first["status"] == second["status"] == "COMPLETED_UNVERIFIED"


def test_empty_checks_yield_completed_unverified(fake_ctx, clean_repo):
    result = _invoke(fake_ctx, clean_repo, "success_tracked_change", [])
    assert result["status"] == "COMPLETED_UNVERIFIED"
    # ok now means VERIFIED ONLY — an unverified completion is not ok=true,
    # but the run itself did complete (explicit `completed` boolean).
    assert result["ok"] is False
    assert result["completed"] is True
    assert result["verified"] is False
    assert result["status"] != "VERIFIED"
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))["receipt"]
    assert receipt["execution_status"] == "COMPLETED"
    assert receipt["verification_status"] == "NOT_RUN"
    assert receipt["verification_authority"] == "NONE"


def test_failing_check_yields_failed_verification(fake_ctx, clean_repo):
    failing = {
        "name": "must_fail",
        "argv": [sys.executable, "-c", "import sys; sys.exit(1)"],
        "timeout_seconds": 60,
    }
    result = _invoke(fake_ctx, clean_repo, "success_tracked_change", [failing])
    assert result["status"] == "FAILED_VERIFICATION"
    assert result["checks"][0]["status"] == "failed"


def test_prime_claim_cannot_make_verified(fake_ctx, clean_repo):
    """Fake claims 'all tests passed' textually; NO host checks supplied."""
    result = _invoke(fake_ctx, clean_repo, "success_claims_tests_passed", [])
    assert result["status"] == "COMPLETED_UNVERIFIED"
    # The claim text is preserved but carries no authority either way.
    final_text = result["prime_final_text"].lower()
    assert "tests passed" in final_text or "candidate" in final_text


def test_prime_claim_with_passing_check_is_verified(fake_ctx, clean_repo):
    """Claims plus real passing host check → VERIFIED (host evidence rules)."""
    result = _invoke(
        fake_ctx, clean_repo, "success_claims_tests_passed", [_passing_check()]
    )
    assert result["status"] == "VERIFIED"


# ---------------------------------------------------------------------------
# Uncertain flow
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_uncertain_flow_timeout(fake_ctx, clean_repo):
    """All eight uncertainty properties (uses a short runtime timeout)."""
    from conftest import validation

    result = _invoke(
        fake_ctx,
        clean_repo,
        "partial_change_then_timeout",
        [_passing_check()],
        timeout=30,
    )

    assert result["status"] == "UNCERTAIN"
    assert result["ok"] is False
    assert result["candidate_stability"] == "unknown"
    assert result["automatic_retry_allowed"] is False
    assert result["checks"] == []  # 4. no checks executed

    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["receipt"]["status"] == "UNCERTAIN"  # 8. evidence preserved
    assert receipt["receipt"]["automatic_retry_allowed"] is False
    assert receipt["receipt"]["verification_authority"] == "MODEL_PROPOSED"

    # 5./6. Candidate remains with the partial edit visible.
    candidate = Path(result["candidate_path"])
    assert candidate.exists()
    readme = candidate / "README.md"
    assert "Candidate edit by fake prime" in readme.read_text(encoding="utf-8")

    # Source checkout untouched.
    status = validation.run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"], cwd=str(clean_repo)
    ).strip()
    assert status == ""


@pytest.mark.slow
def test_malformed_stream_after_zero_exit_is_uncertain(fake_ctx, clean_repo):
    result = _invoke(fake_ctx, clean_repo, "malformed_json", [_passing_check()])
    assert result["status"] == "UNCERTAIN"
    assert result["checks"] == []


def test_nonzero_exit_is_failed_not_uncertain(fake_ctx, clean_repo):
    result = _invoke(fake_ctx, clean_repo, "nonzero_before_terminal", [])
    assert result["status"] == "FAILED"


def test_receipt_written_on_failure_preserves_candidate(fake_ctx, clean_repo):
    result = _invoke(fake_ctx, clean_repo, "nonzero_before_terminal", [])
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["receipt"]["status"] == "FAILED"
    assert Path(result["candidate_path"]).exists()
