# Trust Boundaries for `hermes-prime-rlm`

*Design notes for v0.1.1*

`prime_rlm_run` is a local review harness. Prime proposes changes in a detached
Git worktree; the plugin records Prime's event stream, runs recorded checks, and
writes an unsigned receipt. The candidate remains inert until a human or
separate host policy accepts it.

This design improves reviewability. It is not a sandbox, custody boundary, or
tamper-resistant attestation system.

## Actual authority boundary

Hermes, Prime, the check processes, and the receipt writer normally run as the
same OS user. That user can read and write the source checkout, candidate,
plugin data, credentials, and receipt artifacts. A detached worktree separates
ordinary Git edits; it does not restrict process, filesystem, network, or
credential access.

Consequences:

- A malicious or compromised Prime process may reach the active checkout or
  evidence store despite being launched with the candidate as its working
  directory.
- Direct-child exit does not prove that every descendant stopped. Receipts say
  `candidate_status: UNQUIESCED`.
- The receipt self-hash proves only that its canonical payload matches its
  embedded digest. Receipts say `authenticity_status: UNSIGNED`.
- Artifact digests are recorded, but v0.1.1 does not revalidate every referenced
  artifact under separate custody. Receipts say
  `integrity_status: RECORDED_NOT_REVALIDATED`.

Stronger claims require a worker/coordinator/evidence privilege split, process-
tree fencing, and signed source-bound receipts whose signing key is unavailable
to Prime.

## Pipeline

```text
admission → candidate run → protocol observation → recorded checks → receipt
```

### Admission

Before creating a run directory, the plugin validates the request, resolves the
current commit, requires a clean source checkout, and probes a supported Prime
Agent version. The Prime command comes from operator plugin settings, not the
tool request.

Admission is a consistency gate, not a security boundary. A same-user process
can still change the checkout after admission.

### Candidate run

The plugin creates a detached worktree from the admitted commit and launches one
Prime process with bounded stdout/stderr sinks. It never retries automatically
and never auto-applies, commits, merges, pushes, or deletes the candidate.

Output files are bounded while the child runs. On timeout the plugin attempts
tree termination using the platform's available process-group mechanism. It
does not claim control over independently daemonized descendants.

The admission-time version probe and Git helpers use the same bounded-drain
model. Tracked patches stream directly to their evidence file; candidate-tree
content streams into the digest. Any truncation or size overrun fails closed.

### Protocol observation

Prime's prose has no verification authority. The plugin parses the bounded JSON
event stream and requires an unambiguous lifecycle. Malformed, duplicated,
incomplete, or truncated lifecycle evidence degrades to `UNCERTAIN`; checks do
not run.

### Recorded checks

After a valid Prime terminal event, the plugin hashes the proposal tree and then
runs the recorded check argv inside the candidate. `VERIFIED` is a convenience
label meaning those checks exited zero. It does not mean the candidate is
semantically correct, safe, quiescent, authentic, or accepted.

In v0.1.1, checks supplied through the tool request are labeled
`verification_authority: MODEL_PROPOSED`; no operator repository policy root is
yet implemented. Empty checks produce `COMPLETED_UNVERIFIED` and
`verification_status: NOT_RUN`.

Checks run in the canonical candidate and may mutate it. The receipt records a
pre-check proposal-tree digest and a post-check candidate-tree digest so that
mutation is visible; it does not pretend checks ran against an immutable copy.

### Receipt

The receipt records:

- admitted source commit and observed post-run checkout state;
- effective Prime argv identity and executable/script digests;
- bounded Prime event/stderr digests;
- tracked patch and candidate-tree digests;
- per-check status, duration, and output digests;
- explicit execution, candidate, verification, authority, integrity,
  authenticity, and acceptance axes.

`receipt_sha256` is a canonical payload self-hash. It is not a signature and
does not establish who wrote the receipt.

Raw Prime argv tokens are not written to the receipt. Operator command-prefix
arguments may contain credentials, so only a canonical argv digest and
allowlisted executable/script basenames and file digests are retained.

## Status semantics

| Field | v0.1.1 meaning |
|---|---|
| `execution_status` | Whether the observed run completed, failed, or remained uncertain |
| `candidate_status` | `UNQUIESCED`; descendant quiescence is not proven |
| `verification_status` | Recorded checks passed, failed, or did not run |
| `verification_authority` | `MODEL_PROPOSED` for request checks, or `NONE` |
| `integrity_status` | `RECORDED_NOT_REVALIDATED` |
| `authenticity_status` | `UNSIGNED` |
| `acceptance_status` | `PENDING` |

The legacy `status` field remains for compatibility:

- `VERIFIED`: valid terminal stream and every recorded check exited zero.
- `COMPLETED_UNVERIFIED`: valid terminal stream and no checks.
- `FAILED_VERIFICATION`: at least one check failed, timed out, or failed to launch.
- `FAILED`: a known failure; partial candidate changes may exist.
- `UNCERTAIN`: no reliable terminal/proposal boundary; no checks run.

## Acceptance

Every outcome preserves the candidate and evidence. Acceptance is external to
this plugin. Review the candidate, recorded check authority, pre/post tree
identity, and receipt limitations before applying anything.

## v0.1.1 invariants

1. No automatic candidate application, commit, merge, push, retry, or deletion.
2. No `VERIFIED` label without at least one recorded passing check.
3. Ambiguous lifecycle or missing proposal identity degrades to `UNCERTAIN`.
4. Prime output is bounded during execution.
5. Every admitted terminal path attempts to finalize a receipt.
6. Public claims distinguish host observation from custody and authenticity.
