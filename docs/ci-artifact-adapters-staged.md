# Candidate artifact adapters (staged, disabled)

Status 2026-10-17 UTC: **offline preparation only, fail-closed.** This slice adds a
producer and a consumer adapter around the unchanged v1 validator
([`scripts/ci/candidate_artifact_journal.py`](../scripts/ci/candidate_artifact_journal.py),
contract [ci-artifact-journal-v1.md](ci-artifact-journal-v1.md)). It changes no
workflow, trigger, permission, secret, label, fixture, shared contract text or
existing gate. There is no activation knob, no network access, no upload, no dispatch
and no credential read. `disabled_entry` rejects every payload and never calls a
callback; `main` returns 1. The shared-journal authentication and protected-policy
holds on #168 and the holds in [rollout limits](ci-queue-rollout.md) stay in force.

Files: [`scripts/ci/candidate_artifact_adapters.py`](../scripts/ci/candidate_artifact_adapters.py),
[`tools/test_candidate_artifact_adapters.py`](../tools/test_candidate_artifact_adapters.py).

## 1. What the adapters are

| Adapter | Functions | Pure? | Reuses validator |
| --- | --- | --- | --- |
| Producer | `validate_producer_context`, `build_journal_document`, `build_archive_bytes`, `produce` | yes | `validate_policy`, `validate_records`, `reconcile`, `verify_archive`, `canonical_*`, `workflow_ref` |
| Consumer | `observe`, `consume` | no; invokes an injected read-only transport | `validate_policy`, `validate_candidate`, `validate_snapshot`, `evaluate_offline` |
| Authority | `disabled_entry`, `main` | yes | `DISABLED` shape |

Every acceptance is the validator's: nothing passes the adapters without passing
`verify_archive`, `validate_snapshot`, `reconcile` and `evaluate_offline`. Some
*rejections* are restated in the adapters rather than reused, and they are listed here
so nobody reads "reuses validator" as "no local rules":

- `_exact` is a local copy of the validator's exact-field-set rule (set comparison,
  so mixed-type keys reject instead of raising from a sort). It is applied to the
  producer context and to each appended event.
- `validate_producer_context` restates the producer-binding rules (attempt 1, trusted
  event, `runtime_sha` = `policy_commit`, qualified `workflow_ref`) from the producer's
  side. The validator checks the same facts from the consumer's side in
  `validate_journal`, against REST metadata the producer does not have.
- `consume` short-circuits the same-repository check before any read; the validator
  repeats it in `evaluate_offline`.
- `_call` and `_listing` own the transport reply grammar (section 3). The validator has
  no notion of a transport.
- `_normalized` turns the shared helpers' `TypeError`/`ValueError`/`KeyError` over
  hostile previous evidence into `JournalError` (section 2). It adds no acceptance.

Anything not listed above is the validator's rule, called unchanged.

## 2. Producer: deterministic assembly

`produce(policy, context, previous, events)`:

- `policy` must pass `validate_policy`. `context` has exactly the envelope `producer`
  fields (`run_id`, `run_attempt`, `event`, `runtime_sha`, `workflow_ref`) and must
  bind the policy: attempt 1, event in `producer_events`, `runtime_sha` equal to
  `policy_commit`, `workflow_ref` equal to the qualified default-branch form. The
  adapter cannot check these against REST metadata; that is the consumer's
  `validate_journal`.
- `previous` follows `reconcile`: `{"kind": "genesis"}` starts sequence 1; a previous
  journal is extended by exactly one sequence with its records as an unchanged prefix;
  unavailable, unsupported or malformed previous evidence rejects. Policy movement does
  not reset the chain.
- The previous document is canonicalized (`canonical_digest`) before the chain check.
  A previous document that passes the shape checks but is outside the canonical
  grammar (NaN or infinity, mixed-type or `None` keys, bytes, sets, arbitrary objects)
  makes the shared serializer raise `ValueError`/`TypeError`; a mixed-key previous
  evidence *container* makes the shared `_keys` sort raise `TypeError`. The adapter
  catches these at its boundary (`_normalized`) and raises `JournalError`
  (`previous evidence: not canonically serializable` / `… not canonically comparable`,
  with the original exception as `__cause__`). Validator `EvidenceError` subclasses pass
  through untouched, nothing is defaulted to empty or genesis, and the same wrapper
  surrounds `evaluate_offline` in `consume`. The shared validator itself still raises
  the bare interpreter exception for such inputs when called directly; that is a
  separate, unaddressed gap in `candidate_artifact_journal.py` (see section 8).
- `events` are `{operation_id, candidate, operation, state}`; sequence numbers and the
  recording `run_id` are assigned here. Transitions, candidate shape, repository and
  `max_records` are enforced by `validate_records`.
- The archive is one stored entry named `journal_entry`, fixed 1980-01-01 timestamp,
  regular-file mode, no comment. The bytes are passed back through `verify_archive`
  with `expected_artifact(archive)` (the size and digest the authenticated REST
  metadata must later show) and must parse to the same document, so the policy bounds
  apply to the produced bytes before anything could be uploaded.
- The result is inert data: `document`, `archive`, `journal_digest`,
  `expected_artifact`, `uploaded: false`. Nothing uploads.

The synthetic fixture document is reproduced byte-for-byte from its own `producer`
field and records, which is the test's proof that assembly is deterministic.

## 3. Consumer: sequencing over an injected read-only transport

The transport is whatever object the caller injects. The consumer calls only these
five methods and nothing else. `_call` checks the method name against `READ_METHODS`
at runtime before any attribute lookup and reports anything else as unavailable
(`adapter refused a method outside the read allowlist`) without touching the
transport; the suite exercises this with every write/fallback method name and with a
transport whose attribute lookup raises. The additional AST check in the suite only
shows that the module's single `getattr` call takes the variable `method`; it is a
structural hint, not the proof.

| Method | Arguments | Reply `ok` value |
| --- | --- | --- |
| `get_ref` | repository, default branch | commit SHA of the ref |
| `list_workflow_runs` | repository, workflow ID, page | `{"items": [REST runs], "has_next": bool}` |
| `get_workflow_run` | repository, run ID | REST run |
| `list_artifacts` | repository, run ID, page | `{"items": [REST artifacts], "has_next": bool}` |
| `download_artifact` | repository, artifact ID, `max_bytes` | archive bytes, at most `max_bytes` long |

Each call returns `{"ok": value}` or `{"unavailable": "<reason>"}`. Any other reply
shape (including a dict with mixed-type keys, which is compared as a key *set* so it
can never raise from a sort), any exception, a missing method, a page with more than
100 items or a page without exactly `items` and `has_next` is unavailable evidence.
Reasons outside the validator's reason grammar become `unspecified`. There is no
retry of any call, including after a malformed reply or page.

**Download byte cap.** `max_bytes` is the artifact's authenticated `size_in_bytes`,
which `validate_snapshot` has already bounded by the policy's `max_archive_bytes`. A
real transport must enforce it *while streaming*, before allocating the body; the
adapter cannot do that for an injected object. What the adapter does is reject a
returned body longer than the cap immediately after the download, as
`IntegrityError: archive: downloaded bytes exceed the authenticated size cap`, before
the after-snapshot and without a second download. A transport implemented for the old
two-argument signature fails with `TypeError`, which is unavailable evidence, so the
cap cannot be silently dropped. The suite's fake transport is a deliberate passthrough
that records the cap and ignores it, so that the adapter-side rejection is observable;
it is not a model of the required streaming behaviour.

`consume(transport, policy, candidate, previous, observed_before, observed_after)`:

1. `validate_policy`, `validate_candidate`, same-repository guard. No read yet.
2. **Before snapshot** (`observe`): ref, bounded run listing (at most 8 pages), the
   newest listed run by ID, bounded artifact listing for that run. REST objects are
   projected to the validator's exact field sets; unknown fields are dropped, missing
   fields stay missing so the exact-set rule rejects them. A listing that does not
   finish inside the bound yields `complete: false`; nothing is downloaded.
3. `validate_snapshot(before)`: the validator selects the one authenticated journal
   artifact or rejects (missing, deleted, duplicate, expired, size, digest, unauthoritative
   or failed run, candidate-controlled event in the listing, moved default branch).
4. **Download** exactly that artifact ID, once, with `max_bytes` = its authenticated
   size. An unavailable download (403, 404, 410, rate limit, timeout) is
   `ProvenanceError: evidence unavailable: download <reason>`. A body longer than the
   cap is an `IntegrityError` here, before anything else is read.
5. **After snapshot**, same procedure.
6. `evaluate_offline(policy, before, after, archive, candidate, previous)`: before/after
   stability, bytes against the authenticated digest and size, bounded ZIP, strict JSON,
   envelope binding, chain and semantic state. Wrapped in `_normalized` so a hostile
   previous document rejects as `JournalError` (section 2).

`observed_at` for both snapshots is supplied by the caller. The adapter has no clock,
so it cannot forge an observation time either way; the validator still requires
timezone-qualified timestamps with after not earlier than before.

Call order on the happy path, as the suite asserts it:
`get_ref, list_workflow_runs…, get_workflow_run, list_artifacts…, download_artifact,
get_ref, list_workflow_runs…, get_workflow_run, list_artifacts…`.

Preserved semantics, exercised end to end through the transport: PR-level unresolved
POST holds survive head/base/merge/tree movement (`pr-unresolved`, reconciliation
required); a chained successor for a moved head keeps the old identity's unknown
outcome; lost previous evidence is operator reconciliation with no fresh genesis and
no re-download; policy or source-closure movement rejects before or at the first
snapshot; archive bounds apply at provenance before download and at integrity after.

## 4. What a passing adapter does not establish

- The transport, policy, previous evidence and timestamps are injected. The adapter
  checks mutual consistency only. A fully forged, self-consistent fake state validates
  diagnostically, which is why the result never carries authority:
  `admitted=false`, `dispatch_allowed=false`, `side_effects="rejected"` always.
- Public run IDs, artifact IDs, actor logins and green conclusions prove nothing about
  which YAML or scripts the producer executed. Only the owner's reviewed
  `source_closure` pin says that, and the validator can only check that the pin exists
  and names the workflow file.
- An offline pass does not authenticate caller JSON, does not approve the candidate
  contract and does not authorize an active journal.

## 5. Exact blocker: previous evidence cannot be fetched by the consumer

The consumer takes `previous` from the caller. It cannot locate the previous journal
itself because the v1 schema binds `predecessor_digest` to a *document*, not to an
authenticated run or artifact. There is no field that names the previous producer run,
artifact ID or server digest, so any adapter that walked the run listing backwards to
"find" the predecessor would be choosing its own previous evidence from public IDs,
which the contract forbids. Until the owner either accepts an out-of-band pinned
previous-evidence record or amends the shared contract (owned by Recursion) with an
authenticated predecessor locator, previous-evidence retrieval stays unimplemented and
`previous` remains caller-supplied. Nothing else in the consumer sequencing was
unrepresentable within the current schema.

## 6. Smallest owner decisions still required

Each is a reviewed source change or a recorded decision, never a flag:

1. **Trusted producer.** Author the default-branch reconciler workflow that runs only
   on `schedule`/reviewed `workflow_dispatch`, record its numeric `workflow_id`, and pin
   the trusted policy with a `source_closure` over the workflow, these two scripts and
   any local action. Run metadata alone never proves candidate YAML trust.
2. **Authenticated transport.** Decide which existing Actions read access (no new grant)
   backs the five read methods, verify with existing access that artifact metadata and
   downloads are actually readable, and implement that transport as its own reviewed
   adapter that returns the reply shape above. This slice contains no transport.
3. **Bootstrap.** Accept one genesis journal (sequence 1, null predecessor) and record
   its canonical digest as the first `previous` out of band, or resolve the section 5
   blocker so the consumer can be handed authenticated previous evidence.
4. **Activation.** A reviewed source change that wires `consume` into the candidate
   coordinator, states which semantic states it may act on (at most `not-dispatched`
   and `rejected`, and only with `operator_reconciliation_required` false), keeps bot
   comments as mirrors only, and removes `disabled_entry` from the path. Until then
   every entry denies and exits 1.

## 7. Tests

```
python -X utf8 -B tools/test_candidate_artifact_adapters.py
```

Standard `unittest`, stdlib only, in-memory fake transport over the neutral fixture
(REST noise fields included to prove projection). Covered: fixture document reproduced
deterministically and produced bytes verified at layer 1; chain extension preserving
unresolved records; producer context and event rules, including mixed-type and `None`
keys; previous-evidence shapes; hostile previous documents (NaN, infinity, NaN inside a
record, mixed/`None` keys, bytes, sets, opaque objects) and mixed-key previous
containers reject as `JournalError` with the interpreter exception as `__cause__`, in
the producer and, over a genuine sequence-2 chain, in the consumer after exactly one
download, while unsupported and lost previous evidence keep the validator's own
message; the normalizer passes every `EvidenceError` subclass through and does not
swallow non-evidence exceptions; record, entry and archive bounds on produced bytes;
exact consumer call order with and without pagination, including the download cap
argument; incomplete pagination (runs and artifacts) and oversized pages never reach
download; every read method unavailable with 403/404/410/429/timeout/malformed reasons
blocks without retry or fallback, including second-page and after-snapshot failures;
transport exceptions and malformed replies and pages (including mixed-type, `None`,
tuple and integer keys, and an empty dict) fail closed after one call; the download
cap equals the authenticated size, follows the metadata rather than the body, is
within the policy bound, and an over-cap body (bytes or bytearray, by one byte or by
more than the policy bound) is an integrity failure before the after-snapshot, a
non-bytes body is the validator's integrity failure, and a two-argument legacy
transport is unavailable evidence; the runtime read allowlist refuses every
write/fallback, private, dunder, misspelled and empty method name without reaching the
transport, and every adapter-minted reason fits the validator's reason grammar; moving
snapshots (ref, newer run, artifact updated/digest/deleted/expired, rerun) block after
one observation; deleted, expired, oversized, duplicate, unauthoritative and
failed-run evidence is never downloaded; wrong bytes, truncated,
corrupted-with-honest-digest and two-entry archives are integrity failures; PR-level
holds across candidate, policy and evidence movement; disabled entry under valid
inputs, knobs and callbacks with the transport untouched; no activation symbol, clock,
transport module or extraction in the module; existing gate, validator and workflows
not wired to the adapters.

## 8. Shared-validator gap noted, not fixed here

`candidate_artifact_journal.reconcile` canonicalizes the previous document with
`canonical_digest` and compares key sets with `sorted()`. Called directly with a
previous document containing NaN, infinity, mixed-type keys or unserializable values,
or with a mixed-key previous evidence container, it raises a bare `ValueError` or
`TypeError` rather than a `JournalError`. The adapters normalize this at their
boundary (section 2) so no adapter caller sees it, but any *other* caller of the
shared validator still does. Fixing it means editing the shared module, which this
slice does not touch; it is recorded here for the validator's owner.
