# RFC: managed runtime v1

Status: proposed

This document defines the first managed-runtime slice for
`hermes-prime-rlm`. It does not change runtime behavior. The current release
remains `EXTERNAL_OBSERVED` with a
`PLUGIN_MANAGED_NOT_FULLY_LOCKED` kernel. Nothing described here may emit
`MANAGED_LOCKED` until every acceptance gate in this RFC passes.

## Why this exists

`runtime_lock.json` pins four Prime Agent v0.8.1 release archives. Those hashes
do not identify the complete process that executes on a user machine. Node,
the materialized npm tree, uv, CPython, kernel wheels, and any required shell
can still vary while Prime reports the same upstream version.

The first implementation should import a complete prebuilt bundle offline. It
should not download packages or resolve dependencies on the user machine. This
keeps network policy, package-manager behavior, and runtime activation out of
the same initial change.

## Authority boundary

Managed setup is an operator control-plane transaction. It must not add a
model-facing tool or alter `prime_agent(action="run")`.

Hermes continues to own:

- phase-specific consent;
- the immutable setup plan;
- runtime admission and activation;
- setup journals and receipts;
- host verification and final acceptance.

Prime continues to own native provider authentication and model execution. The
plugin must not read, copy, translate, log, or roll back Prime credentials.

A managed runtime remains same-user software. It is not a sandbox.

## Scope

The first vertical slice adds:

```text
hermes prime setup --bundle <path>
```

The command imports one local, prebuilt, target-specific bundle into a
content-addressed plugin store. Acquisition and activation require separate
consent. Declining activation may leave a verified inert bundle in the cache
when the approved plan says so.

This slice does not add:

- network acquisition;
- live npm or Python package installation;
- source builds;
- shell installers;
- elevation;
- persistent Prime sessions;
- automatic retries;
- automatic candidate application;
- paper-derived behavior.

## Runtime classifications

`EXTERNAL_OBSERVED`
: An operator-supplied Prime command passed the current probes. Its dependency
  closure is not owned or completely identified by the plugin.

`PLUGIN_MANAGED_NOT_FULLY_LOCKED`
: The plugin owns part of the runtime, such as the Windows kernel, but has not
  proved the complete executable closure.

`MANAGED_LOCKED`
: The plugin admitted a complete target lock, verified the bundle and installed
  tree, passed zero-model probes, atomically activated the version, and read
  back the matching plugin configuration.

Presence on disk is never enough to move between these states.

## Complete closure

Each target entry must account for every runtime dependency needed before the
model call. Each dependency is either a bundled byte covered by the installed-
tree manifest or a named entry in the closed host-substrate contract:

- exact Node executable;
- exact materialized Prime npm application tree;
- package-lock or shrinkwrap source identity and per-package integrity data;
- exact Prime entrypoint;
- exact uv executable, if retained in the shipped runtime;
- exact CPython interpreter and ABI;
- exact kernel wheel set and materialized environment tree;
- an explicit Bash decision: absent, or a complete target-specific locked
  closure;
- fixed argv prefix and non-secret environment policy;
- zero-model probe contracts;
- source ledger, SBOM, and license manifest.

Managed mode must not consult ambient `PATH`, npm prefixes, user-site packages,
`PYTHONPATH`, conda, pyenv, Homebrew, Git Bash, WSL, or a user shell.

### Host substrate boundary

`MANAGED_LOCKED` means a complete application/runtime closure running on an
explicitly admitted host substrate. It does not mean that the bundle contains
an operating-system kernel or every system library.

Each target lock divides dependencies into two closed sets:

1. bundled files, each covered by the installed-tree manifest; and
2. host-provided components, each named by a closed allowlist and an admission
   rule.

The host set includes only operating-system facilities that cannot reasonably
ship inside the bundle, such as the kernel, loader, platform frameworks, and a
declared trust-store policy. A build-time dependency scan records every dynamic
library or framework edge. Any edge that is neither bundled nor in the target's
host allowlist rejects the lock.

Target identity binds the observable host contract rather than pretending OS
updates have stable file hashes. Examples include:

- Windows architecture, minimum/maximum admitted build policy, API-set/UCRT
  requirements, and whether the Visual C++ runtime is bundled;
- macOS architecture, minimum OS version, dyld/system-framework allowlist,
  code-signing policy, and Gatekeeper observation contract;
- Linux architecture, kernel floor, glibc symbol-version ceiling or exact musl
  target, dynamic-loader path, and CA trust-store policy.

Doctor records the observed host contract and fails `NOT_READY` on an
unsupported version, ABI, loader, framework, trust policy, or undeclared
dynamic dependency. The receipt binds those observations. Deterministic tests
must substitute each admitted host fact independently and prove that a mismatch
prevents `MANAGED_LOCKED`.

## Lock schema v2

The parser is closed and canonical. It must reject:

- duplicate JSON keys;
- unknown fields;
- non-finite numbers;
- booleans in integer fields;
- malformed or non-lowercase digests;
- target aliases;
- incomplete target/component closures;
- unapproved origins;
- inconsistent declared lengths or file counts.

The canonical lock digest binds:

- schema and policy versions;
- exact target triple;
- complete bundled-versus-host dependency declaration and host admission
  contract;
- bundle origin, media type, byte length, and SHA-256;
- signatures and expected publisher identities when available;
- every source component and materialized-tree manifest;
- download and extraction ceilings;
- launch and environment policy;
- probe contracts;
- builder provenance, SBOM, license manifest, and reproducibility evidence.

Integrity, authenticity, and provenance remain separate fields. A matching
SHA-256 does not prove who produced the bundle.

## Bundle policy

The production host must not run any of these during setup:

```text
npm install
npm update
pip install from an index
uv pip install from an index
source compilation
curl | shell
```

A controlled builder may use exact package-manager locks to construct the
bundle. Setup admits the resulting immutable bytes and complete file manifest.

The importer streams and counts actual bytes. It rejects:

- absolute, drive-qualified, UNC, or parent-traversal paths;
- NULs, Windows alternate data streams, reserved names, and trailing dots or
  spaces;
- duplicate paths, Unicode-normalization collisions, and case-fold collisions;
- symlinks, hardlinks, junctions, reparse points, devices, FIFOs, sockets,
  sparse files, and encrypted members;
- member, depth, path-length, file-size, or total-size limit violations;
- pre-existing linked/reparse parents;
- archive-controlled ownership or executable modes.

Extraction occurs in a new plugin-owned directory on the same volume as the
final store. The importer sets only manifest-declared modes after digest
verification.

## Runtime store

Versions are immutable and content-addressed:

```text
<plugin-data>/managed-runtime/
├── artifacts/<bundle-sha256>
├── versions/<target>/<tree-sha256>/
├── transactions/<transaction-id>/
└── active.json
```

`active.json` is an ordinary JSON file replaced atomically. It is not a
symlink, junction, or mutable `current` directory.

Promotion never merges into an existing version. An existing version is either
reverified and reused or rejected. Rollback changes only `active.json` and the
plugin configuration to a previously proved value. It never recursively
deletes unknown files, credentials, candidates, or external Prime state.

## Durable transaction

Each journal event contains the transaction ID, sequence number, previous
event digest, plan digest, phase, and non-secret observations. The journal is
written before each externally visible side effect.

Required phases:

```text
PLAN_WRITTEN
CONSENT_ACQUIRE_RECORDED
LOCK_ACQUIRED
INPUTS_REVALIDATED
BUNDLE_VERIFIED
STAGING_CREATED
EXTRACTION_COMPLETED
TREE_VERIFIED
PROBES_PASSED
PROMOTION_INTENT_RECORDED
VERSION_PROMOTED
CONSENT_ACTIVATE_RECORDED
ACTIVE_POINTER_INTENT_RECORDED
ACTIVE_POINTER_WRITTEN
CONFIG_INTENT_RECORDED
CONFIG_WRITE_RETURNED
CONFIG_READ_BACK_VERIFIED
RECEIPT_WRITTEN
COMPLETE
```

The setup lock is an OS advisory lock (`msvcrt` on Windows, `fcntl` on POSIX),
not a stale PID-file convention. PID and start time may be diagnostic fields;
they never authorize breaking the lock.

After consent and lock acquisition, setup revalidates every plan-bound input.
A mismatch invalidates consent and requires a new plan.

## Reconciliation

`NOT_READY` is valid when setup can prove that no unconsented visible state
remains. Examples include an unsupported target, lock contention, rejected
archive, pre-promotion probe failure, or managed-config refusal.

`CANCELLED` records declined consent. A transaction may retain a verified,
inactive bundle only when the approved acquisition plan allows it.

`UNCERTAIN_SETUP` is required when promotion, active-pointer mutation, config
mutation, rollback, journal durability, or receipt durability may have occurred
but cannot be proved. It never triggers automatic replay.

Read-only Doctor reconciles fresh filesystem and configuration observations
with the journal. It does not infer success from the last recorded phase alone.

## Cross-platform rules

### Windows

- Invoke locked `node.exe` directly; do not depend on `.cmd`, PowerShell,
  PATHEXT, Git Bash, or WSL.
- Reject every reparse point in the staging and version paths.
- Test reserved names, alternate data streams, path spaces, Unicode, long
  paths, and antivirus sharing failures.
- Use a Job Object with kill-on-close for production process containment. Job
  assignment failure must fail closed or retain an honest unproved-quiescence
  classification.
- Test direct children, grandchildren, inherited pipe/handle retention, and
  attempted escape from the Job Object. Failure to prove tree quiescence makes
  verification ineligible and the run `UNCERTAIN`.
- Keep noninteractive child processes windowless.

### macOS

- Invoke locked executables directly; do not use Homebrew or system Python.
- Test case-folding and Unicode-normalization collisions.
- Do not silently remove quarantine attributes or bypass Gatekeeper.
- Record code-signing, notarization, and Gatekeeper observations separately
  from bundle integrity.
- Launch in a new session/process group. On termination, send `TERM`, wait one
  bounded grace interval, then send `KILL` to the group and drain owned readers.
  A daemonized or otherwise escaped descendant leaves quiescence unproved,
  verification ineligible, and the run `UNCERTAIN`.

### Linux

- Distinguish architecture and libc target identities.
- Do not use distro Python or ambient shells.
- Set only lock-declared executable modes.
- Reject wrong ABI/libc targets as `NOT_READY`.
- Use the same new-session/process-group `TERM` → bounded grace → `KILL`
  lifecycle as macOS. Escaped descendants receive the same honest
  unproved-quiescence and `UNCERTAIN` classification.

All platforms use the same status semantics even when containment and
filesystem implementations differ.

## Frozen run plan

After activation, one immutable `ResolvedRunPlan` must bind:

- exact runtime/tree identity;
- executable and fixed argv prefix;
- kernel interpreter;
- expected provider/model and protocol;
- candidate/base commit identity;
- resource ceilings;
- check plan;
- non-secret environment policy.

Spawn, protocol parser, verifier, evidence writer, and receipt builder consume
that same object. Configuration changes after consent cannot mix generations.

## Acceptance tests

The implementation is incomplete until deterministic tests cover:

- strict lock parsing and canonical digest stability;
- every hostile archive class listed above;
- actual decompressed-byte ceilings;
- pre-existing link/reparse parents;
- two concurrent setup processes;
- process death before and after every journal edge;
- immutable promotion and existing-version reuse/rejection;
- active pointer and config read-back mismatch;
- managed-config refusal;
- cancellation after acquisition;
- transaction-owned cleanup only;
- offline cache reuse with full reverification;
- zero model calls and zero credential reads during setup;
- unchanged active checkout and candidate lifecycle;
- host-substrate mismatches for every declared OS/ABI/loader/trust component;
- direct-child, grandchild, inherited-handle/pipe, timeout, and descendant-
  escape lifecycle tests on Windows, macOS, and Linux;
- no host verification eligibility until process-tree quiescence is proved;
- native target tests on Windows, macOS, and Linux with ambient tools masked.

The full project gates remain:

```text
ruff check .
pytest -q
git diff --check
hermes plugins doctor . --ci
```

Build and install the exact wheel from a foreign working directory before any
release claim.

## Completion criteria

The first slice is complete when a fresh user profile with no ambient
Prime/Node/uv/Bash/kernel can import a local bundle offline, survive every
hostile archive and crash fixture, atomically activate through `ctx.set_config`,
and return `READY` with a receipt that separates provenance, authenticity,
integrity, promotion, config read-back, readiness, and the admitted host
substrate. Every runtime dependency must be either present in the verified
bundle manifest or declared and observed through the closed host contract.

Only then may the runtime classification become `MANAGED_LOCKED`.
