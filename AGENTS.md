# AGENTS.md — hermes-prime-rlm

Instructions for AI coding agents working in this repository.

## Hard rules

1. **Exactly one model-facing Hermes tool** (`prime_rlm_run`) unless a
   versioned design change explicitly alters that contract.
2. **Runtime dependencies are standard-library only.** Dev dependencies are
   limited to `pytest` and `ruff`.
3. **Windows and macOS behavior must stay equal.** Every platform-specific
   repair ships with a deterministic test.
4. **Never substitute agent textual claims for host observations.** Verification
   status comes only from recorded checks executed by the host.
5. **Never add automatic retries** for admitted runs. One invocation = one
   Prime process.
6. **Never modify the active checkout.** Candidates live in detached
   worktrees and are never applied, committed, merged, pushed, or deleted.
7. **Never weaken receipt semantics**: `VERIFIED` requires at least one
   passing host check; `UNCERTAIN` always sets
   `automatic_retry_allowed: false` and `candidate_stability: "unknown"`.
8. **Never claim sandboxing.** The worktree is an edit-isolation convenience,
   not a security boundary.
9. Keep private paths, credentials, and environment values out of committed
   fixtures, receipts, and snapshots.

## Before reporting completion

Run all three gates and paste real output:

    ruff check .
    pytest -q
    hermes plugins doctor . --ci

## Layout map

- schemas.py — input bounds, validation, semver gate, schema-3 pin
- models.py — enums/dataclasses (Status, CheckResult, PrimeObservation...)
- validation.py — read-only git admission checks + version probe
- workspace.py — run dirs, atomic writes, detached worktrees, task envelope
- platform_runtime.py — the ONLY module with platform-specific process logic
- prime_process.py — Prime argv construction + lifecycle
- prime_protocol.py — host-side JSONL stream validation
- verification.py — host-side check execution
- evidence.py — NUL-safe porcelain parsing, diffs, tree digest
- receipt.py — canonical, deterministic receipts
- tools.py — the single Hermes tool + orchestration
- tests/fake_prime_agent.py — deterministic Prime stand-in (no live model)
