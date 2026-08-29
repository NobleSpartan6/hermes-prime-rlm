# RFC: WikiSkill integration gate

Status: research gate only

This document records the admission criteria for any future experiment based
on WikiSkill. It changes no Hermes or Prime behavior. It does not claim a
faithful implementation, benchmark improvement, or production readiness.

## Pinned source

```text
Title:      WikiSkill: Compiling Agent Experience into Persistent Knowledge
            for Skill Evolution
Authors:    Liyan Tang, Cyrus Rashtchian, Chun-Sung Ferng, Andrew Tomkins,
            Da-Cheng Juan, Tu Vu
arXiv:      2608.27454v1
Submitted:  2026-08-27
Subjects:   cs.AI, cs.CL
DOI:        10.48550/arXiv.2608.27454
PDF URL:    https://arxiv.org/pdf/2608.27454v1
PDF bytes:  1177706
PDF SHA-256: 65afc6e12f6f707483fe1b79a97ab67c03abf4b4992f82fde03eb7b8d9ad4a69
License:    CC BY 4.0, as reported by arXiv for v1
```

A reproduction must use this versioned source or record a new version and
digest. The unversioned arXiv URL is not an immutable research identity.

## Placement at the authority boundary

WikiSkill separates agent state into three layers:

- Raw Layer: logically write-once, content-addressed execution traces (the
  paper calls this layer immutable);
- Wiki Layer: persistent consolidated knowledge;
- Skill Layer: executable procedural skills.

It then uses inference rollouts, a wiki maintainer, a skill proposer, and
validation gating/rollback to evolve skills.

That loop belongs on the Prime/research side of the boundary. Hermes must not
turn model-authored wiki pages or skill proposals into verification or
acceptance authority.

Hermes may:

1. admit one versioned, immutable research profile;
2. bind its model-call, token, time, process, storage, and iteration ceilings;
3. observe the profile identity and resource accounting;
4. preserve raw traces and proposed skill changes as evidence;
5. run an external validation harness;
6. accept or reject the proposed profile/skill version.

Hermes must not:

- let the wiki or skill proposer approve its own update;
- interpret training success as host verification;
- hide additional retries, refinement rounds, or processes inside an ordinary
  `prime_agent(action="run")` receipt;
- write paper-derived knowledge into the user's normal Hermes memory or skill
  store without a separate explicit workflow;
- change the bounded production architecture as part of managed-runtime setup.

## Runtime identity

A modified Prime build must not continue to identify itself as exact upstream
Prime Agent v0.8.1. Use a distinct immutable identity, for example:

```text
prime-agent-0.8.1+paper2608.27454.1
```

The identity binds:

- exact Prime base revision;
- paper version and PDF digest;
- source revision of the implementation;
- algorithm mapping document;
- deliberate deviations;
- profile/config digest;
- model/provider and budget policy;
- evaluation harness revision.

Waiting for an upstream Prime release with an explicit feature identity is also
valid.

## Reproduction before integration

The first implementation lives outside the production plugin path. It must
reproduce the paper's components under a fixed harness:

```text
Raw Layer
Wiki Layer
Skill Layer
Inference Agent
Wiki Maintainer
Skill Proposer
Validation Gate and Rollback
```

For each component, record:

- paper section and algorithm step;
- implementation symbol;
- input/output schema;
- persistent state written;
- model calls and prompts;
- nondeterminism source;
- deliberate deviation and reason.

No production claim is allowed when a component is omitted or replaced without
being named.

## State separation

Research artifacts use a dedicated, profile-scoped root:

```text
<plugin-data>/research/wikiskill/<profile-id>/
├── raw/
├── wiki/
├── skills/
├── proposals/
├── evaluations/
└── receipts/
```

The application writes raw traces once to exclusive, content-addressed paths
and does not mutate them by policy. Wiki and skill revisions are likewise
content-addressed. Accepted skill revisions point to the exact wiki and raw
evidence used to propose them.

This is logical write-once history, not independent custody. Prime, Hermes,
and the evidence writer run as the same OS user, so a compromised same-user
process can rewrite traces and digests. Receipts therefore report recorded
integrity, signer authenticity, source provenance, and custody separately and
do not call the research store tamper-resistant.

This store is not Hermes's ordinary memory or skill directory. Nothing is
loaded into a normal session until a separate operator review and activation
step accepts a specific content-addressed skill version.

## Research profile schema

Before implementation, define a closed profile schema with duplicate-key and
non-finite-number rejection. Canonical JSON encoding produces the
`profile_digest`. The schema binds the paper/runtime identity, component
mapping, prompts, model/provider expectations, persistence policy, iteration
and resource ceilings, split/evaluator identities, and activation policy.
Unknown fields or a mismatched canonical digest fail admission.

## Validation and rollback

The proposer never evaluates itself. A separate harness runs disjoint training,
validation, and test splits where the benchmark permits them.

A proposal receipt records:

- parent skill and wiki revisions;
- proposed diff;
- exact training traces sampled;
- validation task set identity;
- score function identity;
- model/provider and resource accounting;
- baseline and proposal results;
- acceptance decision and authority;
- rollback target.

Rollback changes the active skill pointer. It does not delete raw traces or
rewrite history. The wiki may persist across rejected skill proposals only when
that behavior is part of the pinned profile and evaluation design.

## Required ablations

Use equal model-call, token, wall-time, and tool budgets. At minimum compare:

```text
no skill
static hand-authored skill
skill evolution without persistent wiki
persistent wiki without skill updates
full WikiSkill profile
```

Where practical, also test:

- self-evolved versus cross-model-evolved skills;
- wiki reset versus persistent wiki;
- accepted-only versus accepted-and-rejected trace consolidation;
- different retrieval strategies when the active skill set grows.

Report negative and neutral results. Do not select only tasks where the evolved
skill improved.

## Production admission gate

A paper-derived profile may enter the bounded plugin only after:

1. the pinned source and mapping are complete;
2. the reproduction and ablations are published with exact budgets;
3. the modified Prime runtime has a distinct locked identity;
4. one admitted invocation still maps to one bounded Prime process unless a
   deliberate versioned architecture changes that rule;
5. every refinement/iteration is visible in the plan and receipt;
6. persistent research state is disabled by default;
7. no new Hermes model-facing tool is added;
8. automatic retry remains disabled;
9. host checks remain the only verification authority;
10. candidate apply/commit/merge/push/delete behavior remains manual;
11. native Windows, macOS, and Linux canaries pass with equal semantics;
12. an independent review finds no authority-boundary regression.

## Receipt additions

An admitted research-profile run records, without raw secrets:

```text
research_profile_id
paper_arxiv_id
paper_version
paper_pdf_sha256
prime_base_revision
modified_runtime_identity
profile_digest
raw_revision
wiki_revision
skill_revision
iteration_count
model_call_count
token_accounting
wall_time_seconds
validation_harness_revision
persistence_enabled
```

These fields describe the experiment. They do not imply verification or
acceptance.

## Separate workstreams

Managed-runtime acquisition and WikiSkill experimentation must remain separate
pull requests. The managed-runtime work changes supply-chain, filesystem,
transaction, and process risks. WikiSkill changes model behavior, persistence,
evaluation, and skill-governance risks. Combining them would make failures and
performance gains difficult to attribute.

The managed runtime should land first. Research starts from a known immutable
runtime identity rather than becoming part of the mechanism used to establish
that identity.
