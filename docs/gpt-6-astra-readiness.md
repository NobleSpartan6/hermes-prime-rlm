# GPT-6 Astra readiness: verify the provider adapter, not just the model name

Reviewed 2026-09-04. **Not a claim of a successful Astra run.** This PR makes no
live provider calls, changes no authentication, and does not switch the default
model or relax the exact Prime 0.8.1 version gate.

## Verified external requirement

OpenAI's supplied model guide says Astra tool calling requires the Responses
API; Chat Completions only supports its non-tool use. The migration also removes
`temperature`, `top_p`, and `top_logprobs`. `none`/`minimal` reasoning settings
must move to a supported setting such as `low`. Asynchronous tool execution and
steering require their own protocol handling, not a model-string substitution.
Source: [OpenAI model guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra).

## What this repository actually controls

Hermes passes the bounded task to a Prime-native runtime. Prime owns the provider
adapter, native login, recursive kernels and model selection. The existing RPC
admission proves an active model is in the runtime's available-model catalog;
it does not prove that model's remote tool-call transport is compatible.

The correct integration point is therefore the **Prime provider adapter**, not
a second OpenAI client embedded in a desktop renderer or this stdlib plugin.
Do not copy Hermes credentials into Prime or start inheriting ambient provider
keys. Preserve explicit operator model selection and the digest-bound runtime
plan. No renderer or model-facing field may override command, credentials,
provider transport or runtime pin.

## Release gate

| Gate | Evidence required | This change |
|---|---|---|
| Runtime identity | Exact pinned version plus protocol fixtures | Existing 0.8.1 contract preserved |
| Model discovery | Native catalog and correlated state show the selected Astra identity | Not exercised live |
| Tool transport | Prime adapter uses Responses with matching call/result identities | Not verified for the pinned runtime |
| Parameters | Supported reasoning setting and no removed sampling parameters | Documented requirement; no silent rewrite |
| Recursive accounting | Root and descendant model identity, usage and limits recorded | Future versioned runtime work |
| Host outcome | Detached worktree unchanged-source check, passing recorded check, receipt | Covered by fake-runtime integration tests, not Astra inference |
| Native lifecycle | Windows/macOS/Linux start, exit, timeout and disconnect evidence | PR CI must be observed; no claim from configuration alone |

Keep Astra behind an operator-enabled compatibility gate until those runtime
and live inference checks pass. An absent catalog entry, unknown transport or
mismatched runtime is a diagnostic failure, not a reason to auto-fallback,
retry an admitted task, or upgrade globally.

## Opt-in canary procedure

Use a tiny non-secret temporary repository and a meaningful host-owned check.
Have the operator explicitly select an available Astra model via Prime's native
configuration, establish native login, and approve paid inference. Freeze the
runtime/model/settings and start **one** bounded request. Record the provider
and actual model, transport, one lifecycle, root/descendant usage when available,
source identity, check outcome and unsigned receipt. Do not describe a host
wall-clock timeout as an enforced global spend cap; descendant accounting and
hard token/dollar limits need separately verified runtime support.

On uncertainty, retain the candidate and evidence and stop. No automatic replay.
Only after that canary and native lifecycle checks pass should the desktop UI
advertise Astra as a supported choice. Low-effort interactive work versus deeper
analysis can then be evaluated under explicit operator budgets; changing effort
is not a substitute for testing tool compatibility or task correctness.
