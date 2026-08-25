# Trust Boundaries for Agent-Produced Code

*Design rationale for hermes-prime-rlm v0.1*

An agent that edits code is only useful if its output can be trusted without
trusting the agent. This document records the trust decisions baked into
`prime_rlm_run` — why the pipeline is shaped the way it is, and in particular
why ambiguous runs degrade to `UNCERTAIN` instead of being salvaged.

## The pipeline

```
admission → run → verify → evidence → receipt
```

Every stage has one job, and no stage may borrow authority from another.

### 1. Admission — refuse before touching anything

The request is validated before any filesystem side effect:

- **Clean-tree gate.** The source repository must have zero tracked or
  untracked changes (`DIRTY_REPOSITORY` otherwise). A dirty base would make
  the diff-vs-base meaningless and could silently mix operator work with
  agent output. (In practice this gate earns its keep: stale `__pycache__/`
  directories rejected two of our own live runs before anything was created.)
- **Version pinning.** Prime Agent must be `>=0.8.0,<0.9.0`, probed by running
  `--version` — not taken from config text. The JSON event-stream schema is
  pinned to version 3 *independently* of the version gate, so a wrapper that
  lies about its version still can't feed us an unparsed stream.
- **Operator-owned command.** The Prime executable comes from plugin settings,
  never from model input. The model can request a goal; it can never choose
  what binary runs or with which flags.

### 2. Run — isolate, don't sandbox

Prime works inside a detached worktree created from the frozen commit. The
active checkout cannot be touched by ordinary edits; the candidate is preserved
after every outcome, including failures.

This is deliberately *not* called a sandbox. Model-generated code runs with the
user's permissions: process, network, and credential access are uncontained.
Honesty about this boundary shapes every other decision — since we can't
contain the agent at runtime, we contain its *claims* at verification time.

### 3. Verify — evidence over assertion

The single most important rule: **agent textual claims carry zero verification
authority.** "All tests passed" in Prime's final message is prose, not proof.
Proof is only ever:

```
host executed recorded argv inside candidate → observed exit code 0
```

Consequences that fall out of this rule:

- **Empty checks ⇒ COMPLETED_UNVERIFIED, never success.** If the caller
  supplies no verification commands, there is nothing to observe. A run with
  no checks can never be `VERIFIED`, no matter how confident the agent sounds.
- **Checks are host-executed, verbatim.** The argv list recorded in the request
  is what runs, in the candidate directory, with the host as the witness. The
  agent cannot choose its own tests at verification time.
- **`VERIFIED` is deliberately weak.** It means exactly "the recorded commands
  exited 0 against the recorded candidate" — not semantic correctness, not
  security review, not reproducibility. Overclaiming a green status is how
  verification theater starts; the receipt's limitations field says so
  explicitly on every run.

### 4. Evidence & receipt — deterministic, hashable

Each run directory preserves the full chain: canonical request envelope, task
envelope, the complete event stream, stderr, the tracked diff vs base, per-check
logs, and a final receipt whose content hash (`receipt_sha256`) covers all of
it. Receipts are built deterministically (sorted keys, stable ordering) so two
runs with identical outcomes produce identical bytes — a reviewer can recompute
any hash without trusting our serialization.

## Why UNCERTAIN exists (and why it never auto-recovers)

The hard design case is ambiguity. During live testing we hit this exact
failure mode (`run 820c4f4e`):

> Prime did the work correctly — the candidate passed the oracle test when we
> checked by hand afterward. But its event stream contained an `agent_end`
> without a matching `agent_start`. The protocol validator couldn't establish
> a reliable terminal boundary.

Three options existed:

1. **Trust the outcome anyway** ("the work looks done"). Rejected: once you
   accept a malformed lifecycle stream because the *result looks right*, you've
   re-created the assertion-as-evidence problem one level up. A corrupted
   stream could equally mean truncated tool output, a replayed segment, or a
   tampered log — none of which "looks wrong."
2. **Retry automatically.** Rejected twice over. A retry burns money on a
   non-deterministic process to paper over an unknown; and if the anomaly was
   caused by something environmental (a wrapper emitting extra stdout, a disk
   hiccup), the retry reproduces it. One invocation = one Prime process, always.
3. **Degrade to `UNCERTAIN` and preserve everything.** Chosen.

`UNCERTAIN` is honest ignorance, encoded:

- No checks are executed (running checks against a candidate of unknown
  provenance manufactures false confidence).
- `candidate_stability` is `"unknown"` and `candidate_may_have_partial_changes`
  is `true` — the receipt never pretends the tree is coherent when the stream
  wasn't.
- `automatic_retry_allowed` is always `false`. Recovery is a human decision:
  read the evidence, decide whether the candidate is worth keeping, start a new
  run explicitly if desired.

The cost is occasional friction — a correct fix arrives wrapped in `UNCERTAIN`
and someone must look at it manually, as we did. That friction is the product.
A status system that quietly promotes ambiguous runs to success will eventually
promote a wrong one, and nobody will know which runs to distrust.

## What acceptance means

Because candidates are never applied automatically, every run ends the same
way regardless of status: a human (or a downstream host policy) compares the
receipt against the preserved candidate and decides. The tool's job is to make
that decision cheap and evidence-backed — not to make it for you.

## Summary of invariants

1. Never modify the active checkout; candidates live in detached worktrees.
2. Never substitute agent claims for host-executed evidence.
3. Never mark success without at least one passing host check.
4. Never retry automatically; ambiguity degrades to `UNCERTAIN`.
5. Never weaken receipt semantics to make a run look better than it was.
6. Never claim sandboxing; the worktree isolates edits, not execution.
