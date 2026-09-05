# Desktop RLM bridge v1

Status: **operator-backend foundation**, not a shipped desktop application.
The model-facing v0.2 contract remains exactly `prime_agent(action="run")`.
The adapter neither registers another model tool nor replaces Prime's RLM.

## Repository assessment

The existing implementation already supplies the expensive, safety-sensitive
parts: exact Prime 0.8.1 admission, a detached candidate, strict RPC lifecycle,
filtered environment, bounded process drains, host-run checks, and preserved
unsigned receipts. `tools.py` synchronously orchestrates that path. There is no
renderer or native desktop shell in this repository. Reimplementing the agent in
a UI would duplicate authority and create a second failure/retry policy.

This bridge instead keeps all of those modules unchanged:

```text
Desktop renderer (no credentials, no arbitrary filesystem/command API)
  | approved request / bounded status snapshots
Trusted Hermes desktop backend (one controller per profile)
  | one non-daemon worker; unchanged handle_prime_agent
Hermes admission -> detached candidate -> Prime RPC / RLM -> host checks
  | unsigned receipt and preserved candidate
Backend review action (separate from status transport; human acceptance)
```

The factory is import-light. No worker, runtime probe, network request, provider
login, or filesystem write occurs when the package or controller is loaded.

## Integration API

The desktop backend must obtain a real, profile-scoped Hermes plugin context and
perform its normal user approvals **before** submission. An ID is not approval.
Do not construct contexts or accept executable/provider configuration from a
renderer. Reuse one controller per profile for the backend's lifetime.

```python
from uuid import uuid4
from hermes_prime_rlm import create_desktop_controller

controller = create_desktop_controller(ctx)  # trusted Hermes PluginContext
session = controller.capabilities()["session_id"]
request_id = str(uuid4())  # persist this with this session token in the UI

accepted = controller.submit(session, request_id, {
    "action": "run",
    "goal": approved_goal,
    "repository_path": approved_repository,
    "checks": approved_checks,
    "runtime_timeout_seconds": 1200,
})
# submit starts a worker; it does not wait for Git/Prime/model/check execution.
page = controller.poll(session, after=0, limit=32)
current = controller.snapshot(session, request_id)
# Repeating the SAME ID and identical arguments reattaches; it never reruns.
```

`list_runs(session, offset=0, limit=32)` rediscovers this backend's retained
requests after a renderer reload. `poll` returns sequence cursors, `has_more`, and
`gap`; refresh snapshots/list pages after a gap. These reads never scan candidate
files, parse Prime logs, or join the worker. Events are only `accepted`, `running`,
`finished`, and `rejected`. There is **no** subagent tree, token stream, or progress
percentage yet; do not invent one from elapsed time.

`review_references(session, request_id)` is a backend-only local path reference,
not a public IPC method or artifact-integrity check. Validate destinations and
receipt integrity before opening anything. Never interpolate paths into a shell.
The independent receipt-inspection work should remain the authority for a future
review surface rather than being duplicated in a progress adapter.

### Private pipe transport

`desktop_transport.serve(controller, binary_reader, binary_writer)` accepts
parent-owned inherited pipes from a trusted host. This is a blocking backend
loop, not a UI-thread function, HTTP listener, daemon, or standalone launcher.
The integration must prevent unrelated processes or untrusted web content from
reaching it. Do not expose it on a socket or an unauthenticated browser bridge.

One UTF-8 JSON object per LF-terminated frame:

```json
{"jsonrpc":"2.0","id":1,"method":"capabilities","params":{}}
```

Allowed methods: `capabilities`, `submit`, `snapshot`, `poll`, `list_runs`.
Other calls, notifications, batches, unknown arguments, duplicate JSON keys,
non-finite numbers, oversized frames, and incomplete final frames are refused.
JSON-RPC IDs are integers from 0 through 2^53-1. Execution request IDs are
canonical UUID strings, separate from transport correlation IDs. Responses use
fixed error codes and never echo exception text. Only the transport thread
writes replies, so worker output cannot interleave with JSON framing.

### Resource and lifetime contract

| Boundary | Default / hard limit |
|---|---|
| Active top-level runs | 1 per controller; host must enforce one per profile |
| Queue | 0; a second new request gets `DESKTOP_BUSY` |
| Retained request IDs | 64 by default, configurable 1–1024; no silent eviction |
| Event ring | 128 by default, configurable 1–4096 |
| Events per poll | Default 64, maximum 256 |
| History page | Default 32, maximum 64 |
| Request | 128 KiB canonical JSON; bounded nodes, depth, and pending traversal |
| Result accepted from handler | 256 KiB; malformed/inconsistent becomes `UNCERTAIN` |
| Pipe frame / reply | 144 KiB / 512 KiB |
| Idle work | No adapter timer, filesystem watcher, or polling worker |

A full ID history rejects new work with `HISTORY_FULL`; it does not erase old
no-replay tombstones. Plan an explicit drained-backend lifecycle rather than
silently rotating controllers. Same-session duplicate submissions remain
readable even after closing admission. Different arguments with a retained ID
return `REQUEST_ID_CONFLICT`.

Idempotency is **in-memory and backend-lifetime-scoped**, not durable exactly-once
execution. A recreated controller has a new session UUID. Old session tokens
produce `SESSION_CHANGED_DO_NOT_REPLAY`. On backend restart/crash, inspect
preserved runs before an operator explicitly starts a new request. The UI must
not acquire the new token and automatically replay old requests.

`close(wait=False)` rejects new work without waiting or cancelling. The worker
is non-daemon. `close(wait=True)` waits on the backend shutdown path; never call
it on the UI event loop. Pipe EOF/disconnect closes admission and waits for
already-admitted work; it does not kill or replay it. Renderer detachment only
preserves responsiveness when the trusted backend remains alive. This is not
crash recovery, durable resume, or a daemon adoption protocol.

A slow reader cannot block Prime: snapshots are retained in a bounded ring,
not delivered via worker callbacks. It can block its own pipe responder. As a
renderer design target, poll around four times per second while visible, stop
when all work is terminal, and avoid hidden-window polling. These are suggested
UI policies, not measurements or behavior implemented in this repository.

## Verification and privacy

`VERIFIED` requires the handler's verified status plus nonempty host check rows
all reporting `passed` with integer exit code zero. A model saying tests passed
cannot make that status true. No-check completion stays `COMPLETED_UNVERIFIED`.
Malformed or contradictory outcomes degrade to `UNCERTAIN`; retry remains false.

Snapshots omit goals, command arguments, model output, private paths, raw check
logs, and exception messages. They retain small counts and identifiers. This is
not cryptographic attestation: authenticity stays `UNSIGNED`, acceptance stays
`PENDING`, and this adapter does not revalidate stored artifacts. Worktrees and
threads are not sandboxes. Existing Prime code still runs with the user's OS
authority and native credentials; provider approval for repository data remains
required. No global or cross-process execution budget is introduced here.

## Research-to-product plan

The reference paper's useful distinction is external state rather than a larger
prompt: persistent REPL/subagents and disk-backed history complement active
context. It also describes daemon-owned sessions, asynchronous recursive handles,
a human Agents View, and typed, versioned refinements [1, sections 2.2–2.5].
Those are a roadmap, not capabilities implicitly granted by this adapter.

**Next: observable execution and review.** Add allowlisted host phase events at
admission, candidate creation, RPC start/end, verification, and receipt completion.
Bind all events to the run ID. Add paginated changed-file review and recorded
check summaries, with explicit acceptance separate from verification. Cooperative
cancellation needs the existing platform process owner and honest `UNCERTAIN`
semantics; a decorative Stop button is not sufficient.

**Then: persistent recursive sessions, as a versioned protocol change.** Probe
and pin an upstream runtime supporting daemon ownership, stable parent/child IDs,
attach/detach, recovery and accounting. Add durable host request journals before
reconnect/resume. Verify crash, sleep/wake, pipe loss, PID reuse, migration and
partial-write cases natively on each OS. Do not loosen the current 0.8.1 gate or
silently convert a one-shot run into an autonomous loop.

**Make resource policy explicit before increasing parallelism.** Measure kernels
and active descendants, not just Python adapter threads. Start with a small
operator-selected laptop budget and allow idle kernels to unload. A waiting
parent must not hold all execution permits while its children need those permits
(deadlock). Reserve budget for verification. Root-plus-descendant token/time/cost
accounting and enforced limits require upstream evidence and controls; today's
single-flight adapter does not enforce a dollar cap inside Prime's recursion.

**Finally: reviewed reusable knowledge.** Proposed skills/memories should be
quarantined until independent checks pass; retain provenance, version history,
rollback, and operator promotion. Never let refinement rewrite approvals, base
policy, or receipt semantics. The paper reports a task exploit being retained as
a reusable skill, illustrating why persistence alone is not safe improvement
[1, section 3.5].

## Performance and acceptance

Run the isolated contracts without installing Prime or Hermes:

```bash
python -m pytest --noconftest -q tests/test_desktop.py tests/test_desktop_transport.py
python scripts/benchmark_desktop.py --samples 5000
```

The benchmark uses one blocked synthetic worker and measures snapshot and JSON
framing overhead, with median/p95/p99, payload bytes, OS and Python version.
It makes zero provider calls. It does **not** measure inference speed, UI FPS,
RSS, whole-session cost, or improvement against the old implementation.

The normal CI matrix runs all tests, including real Git + fake Prime + host
verification through the default desktop handler, on Windows/macOS/Linux and
Python 3.11/3.12. It also runs the synthetic benchmark on each matrix entry,
Plugin Doctor and installed-wheel checks. A configured matrix is not a passing
result; inspect the PR's actual checks before release.

For a later product claim, compare the same tasks and verification criteria at
matched model/settings/budget: plain Hermes versus bounded RLM delegation.
Track success rate, time to first useful result, p95 completion latency, total
root-plus-descendant tokens/cost, idle CPU/RSS and sleep/wake recovery. Keep
simple tasks on the ordinary path. Do not extrapolate a paper benchmark score
into a universal speedup or correctness guarantee.

## Sources

1. Karten et al., *Prime Agent: A Self-Improving RLM Harness*, arXiv:2608.23552v1,
   https://arxiv.org/html/2608.23552v1 (reviewed 2026-09-04).
2. [GPT-6 Astra integration gate](gpt-6-astra-readiness.md).
3. Existing contracts: [trust boundaries](trust-boundaries.md),
   [managed runtime](managed-runtime-v1.md), and the root README.
