# Candidate artifact journal v1 (preparation, disabled)

Status 2026-10-17 UTC: **offline, staged preparation only.** Nothing in this document
activates anything. The existing exact-candidate gate
([`scripts/ci/candidate_gate.py`](../scripts/ci/candidate_gate.py),
[`scripts/ci/merge_candidate.py`](../scripts/ci/merge_candidate.py),
[`.github/workflows/codex-gate.yml`](../.github/workflows/codex-gate.yml)) is
unchanged and remains explicitly unqualified for protected authority, exactly as
recorded in [rollout limits](ci-queue-rollout.md). No workflow, trigger, permission,
setting, secret, label or consumer wiring changes with this document.

The [superseded-policy recovery finding on #168](https://github.com/CSL-Laboratories/ai-investment-cockpit/pull/168#discussion_r4301252507)
was corrected at `aa1eef1` and marked resolved by Devin. That diagnostic recovery
does not authenticate the shared comment journal; journal-origin and protected-policy
holds remain. Removing a reservation or changing a head is not proof of non-delivery.
This offline module preserves uncertainty and grants no live recovery authority.

Contract status: **DRAFT, proposed for peer review.** This is the shared v1 contract
text as proposed; it is not accepted implementation authority for any repository until
each owner has reviewed and accepted it. Each repository owns its implementation from
this text and the neutral fixture
[`tools/fixtures/candidate-artifact-journal-v1.json`](../tools/fixtures/candidate-artifact-journal-v1.json).
Recursion owns the contract wording; each repository owns its implementation.
This Cockpit draft adopts Recursion #226 at `0a482d3eb794016a35a0daef953632fabedef84e`,
with the identical neutral fixture and a portability clarification: the journal key
is separate from Cockpit's runtime key. That clarification is proposed back to the
contract owner; no runtime protocol is changed here.
Reference implementation and tests:
[`scripts/ci/candidate_artifact_journal.py`](../scripts/ci/candidate_artifact_journal.py),
[`tools/test_candidate_artifact_journal.py`](../tools/test_candidate_artifact_journal.py).

## 1. Purpose and non-goals

Today the admission journal lives in editable `github-actions[bot]` PR comments,
which every `issues`/`pull-requests` write workflow can author. v1 prepares a
replacement: a **run-bound artifact** uploaded by a future trusted default-branch
reconciler that runs only on `schedule` or owner-reviewed `workflow_dispatch`.

Non-goals, all of which stay as recorded activation holds:

- No activation switch, environment knob or configuration flag. Activation is a
  separate owner decision delivered as reviewed source.
- No new transport, credential, permission or grant. Artifact metadata and downloads
  need Actions read access. **Access prerequisite:** at activation the owner verifies,
  using existing access only, that this read access is actually available. Insufficient
  access blocks activation; no grant expansion is assumed or requested here.
- No change to human holds, receipt ownership, observation-journal history,
  fingerprints, READY/MEDIATE roles, Devin review-of-record, existing required checks,
  personal sign-off, candidate acceptance, native qualification or landing.
- Public IDs, comments, titles and green status alone are not authentication.
- Native Actions attestation is **held** until the owner accepts a verification route.
  An envelope flag such as `trusted: true` is an unknown field and is rejected; it is
  never cryptographic evidence.
- No live canary, extra full CI or paid provider invocation.

## 2. Four layers, kept separate

| Layer | Function(s) | Answers | Can it grant authority? |
| --- | --- | --- | --- |
| 1 Artifact integrity | `verify_archive`, `parse_strict_json` | Are these bytes the authenticated artifact, and a well-formed single JSON entry? | No |
| 2 Provenance | `validate_policy`, `validate_snapshot`, `validate_run`, `validate_artifact` | Does independently supplied trusted policy match independently obtained Actions metadata? | No |
| 3 Semantic journal | `validate_journal`, `validate_records`, `reconcile`, `journal_status` | What does the journal say about this candidate, and is history intact? | No |
| 4 Side-effect authority | `stage`, `disabled_entry`, `evaluate_offline` | May anything be dispatched? | **Always no in v1** |

A result from `evaluate_offline` always has `admitted=false`,
`dispatch_allowed=false`, `side_effects="rejected"`, even when
`diagnostic_valid=true`. `disabled_entry` rejects every payload, environment and
callback and returns exit status 1 from `main`.

Failures are `EvidenceError` subclasses: `IntegrityError` (layer 1),
`ProvenanceError` (layer 2 and unavailable evidence), `JournalError` (layer 3).
All inherit `ValueError`. Callers fail closed on any of them.

`evaluate_offline` runs the layers in order: policy and candidate, both snapshots
(provenance), archive (integrity), envelope and records, then the chain. The first
failing layer's class is reported. For example, authenticated artifact metadata with
`size_in_bytes: 0` is a `ProvenanceError` before any ZIP check, and an entry larger
than `max_entry_bytes` is an `IntegrityError` before `max_records` is consulted.

## 3. Caller trust statement

The validator cannot authenticate its inputs. It checks that inputs are mutually
consistent and complete. Three things must be true outside the validator:

1. **Trusted policy** is reviewed by the owner and supplied independently of the
   artifact. Policy cannot come from the artifact's own assertions; the envelope must
   *equal* the policy, and a mismatch is a `JournalError`, not a policy update.
2. **Snapshots** come from an authenticated REST transport with existing access (the
   future reconciler's own, not created here), taken twice around the download.
   Arbitrary caller-supplied JSON is not proof. In the offline fixture harness the
   snapshots are synthetic, so a passing fixture proves structural acceptance only.
3. **Source closure** is the owner's reviewed pin of the producer's bytes. A
   successful run alone does not prove trusted YAML or scripts. The validator can only
   check that the closure is pinned to the policy commit and includes the workflow
   file; it cannot observe what the producer executed.

## 4. Trusted policy (`candidate-artifact-journal-policy/v1`)

Exact field set; unknown or missing fields reject.

| Field | Type / rule |
| --- | --- |
| `schema` | `"candidate-artifact-journal-policy/v1"` |
| `repository_id` | positive int (REST numeric repository ID; `true` is not an int) |
| `repository` | `owner/name`; neither segment may be exactly `.` or `..` (same rule for every candidate `repo`) |
| `default_branch` | branch name; no `..`, `//`, trailing `/` or `.lock` |
| `policy_commit` | full lowercase 40-hex SHA; the current immutable default-branch head when the snapshots were taken |
| `workflow_id` | positive int (REST numeric workflow ID) |
| `workflow_path` | **bare** `.github/workflows/<file>.yml`; qualified `owner/repo/path@ref` forms reject here |
| `producer_events` | non-empty unique subset of `["schedule", "workflow_dispatch"]` |
| `dispatch_actor_ids` | unique positive ints of owner-reviewed `workflow_dispatch` users; may be empty (then every dispatch rejects) |
| `source_closure.commit` | must equal `policy_commit` |
| `source_closure.files` | non-empty map of repository-relative normalized paths to sha256 hex; must include `workflow_path`; no `.`/`..` segments or absolute paths. Pins workflow, scripts and local action bytes. The producer must execute only closure files; no candidate-owned local action or script. |
| `artifact_name` | `[A-Za-z0-9][A-Za-z0-9._-]{0,99}` |
| `journal_entry` | single file name ending `.json`; no slash |
| `bounds` | `max_archive_bytes` ≤ 8 MiB, `max_entry_bytes` ≤ `max_archive_bytes`, `max_records` ≤ 10000 |

Policy or default-ref movement **before or after** the observation invalidates the
snapshot (section 5). There is no automatic policy refresh from the run.

## 5. Snapshot (`candidate-artifact-journal-snapshot/v1`)

One complete bounded observation, taken **before** and **after** the archive download.
Exact field set:

| Field | Rule |
| --- | --- |
| `schema` | `"candidate-artifact-journal-snapshot/v1"` |
| `observed_at` | timezone-qualified ISO timestamp (the only field excluded from before/after equality; after ≥ before) |
| `complete` | must be exactly `true`: bounded pagination finished for both listings |
| `default_branch_head` | independent git ref lookup of the default branch; must equal `policy_commit` |
| `run` | selected run, projected to the run fields below |
| `workflow_runs` | complete listing for `workflow_id`, each entry projected to the run fields |
| `artifacts` | complete artifact listing for `run.id` |

Instead of a snapshot, the transport may report `{"unavailable": "<reason>"}` or
`null`. HTTP 403/404/410, rate limits, timeouts and any transport uncertainty are
reported this way and are a `ProvenanceError`. There is **no fallback** to bot
comments, an older successful run or a permissive empty state.

Conservative availability limits, which must never be loosened to keep progressing:

- A complete bounded listing may fail at the bound (too many runs or artifacts to list
  within `bounds`, or pagination that does not finish). That is `complete: false` and
  rejects; a partial listing is never treated as complete.
- Movement of the current policy commit or the default ref invalidates the prior
  artifact as evidence. Nothing is admitted on the old artifact until new trusted
  evidence (a new policy pin and a new run-bound snapshot) exists.
- Expired, deleted or uncertain evidence stays `evidence-unavailable`; waiting for the
  operator is the only progress path.

### Run projection (REST workflow run, exact field set)

`id`, `run_attempt`, `event`, `status`, `conclusion`, `head_sha`, `head_branch`,
`workflow_id`, `path`, `repository{id, full_name}`, `head_repository{id}`,
`actor{id, login, type}`, `triggering_actor{id, login, type}`, `created_at`,
`updated_at`.

Rules for the selected run:

- `run_attempt` must be exactly 1. Reruns are unsupported in v1 because partial reruns
  can reuse prior job outputs.
- `event` in `producer_events`. Every PR, head, fork or push event rejects.
- `status == "completed"` and `conclusion == "success"`.
- `head_sha == policy_commit` and `head_branch == default_branch`. For schedule and
  dispatch runs the REST `head_sha` **is** the default-branch policy commit. It is not
  a candidate merge or head SHA; the envelope's `producer.runtime_sha` (runtime
  `GITHUB_SHA`) must equal the same commit (section 7).
- `workflow_id` and bare `path` equal the policy.
- `repository.id`, `repository.full_name` and `head_repository.id` equal the policy
  (a fork head repository rejects).
- For `workflow_dispatch`, `triggering_actor.type == "User"` and its `id` is in
  `dispatch_actor_ids`. `schedule` does not consult the list.

Rules for the listing: unique IDs; every entry is this workflow and bare path; every
entry's event is `schedule`/`workflow_dispatch` on the default branch (a listing
showing the workflow accepted a candidate-controlled event or branch rejects); no
entry newer than the selected run (newer pending, failed, cancelled **or successful**
runs all mean the selected run is not authoritative); the selected run appears and
equals `run`.

### Artifact metadata (REST artifact, exact field set)

`id`, `name`, `size_in_bytes`, `expired`, `created_at`, `expires_at`, `updated_at`,
`digest`, `workflow_run{id, repository_id, head_repository_id, head_branch, head_sha}`.
Exactly one artifact named `artifact_name` must exist (missing, deleted or duplicate
rejects). `expired` must be exactly `false` and `expires_at` strictly after
`observed_at`. `digest` must be `sha256:<64 hex>`; `null` rejects. `size_in_bytes`
is positive and within bounds. `workflow_run` must bind the selected run ID, the
policy repository as both repository and head repository, the default branch and the
policy commit. Reference facts checked by the parent: GitHub REST Actions artifacts
expose these fields and downloads are ZIP via 302, with 410 for expired artifacts
(<https://docs.github.com/en/rest/actions/artifacts>,
<https://docs.github.com/en/rest/actions/workflow-runs>). This preparation does not
claim to have observed API behaviour itself.

## 6. Archive integrity

Applied to the downloaded bytes against the authenticated artifact metadata:

- byte length equals `size_in_bytes`; sha256 of the bytes equals `digest`;
- parses as a ZIP; empty archive comment; **exactly one** entry (duplicate paths or
  extra files reject); entry name equals `journal_entry` exactly (traversal, nested,
  absolute and directory names cannot match); not a symlink; no encryption flags;
  stored or deflate only; `0 < file_size ≤ max_entry_bytes`; compressed size within
  the archive; a nonempty entry declaring zero compressed bytes rejects; decompression
  ratio ≤ 32:1 measured against the actual declared compressed size with no minimum
  floor, so small entries are bounded too; CRC/structure check passes; an invalid
  raw deflate stream reported by the decompressor (`zlib.error`) rejects as integrity;
- read bounded in memory; **never extracted to the filesystem**;
- strict JSON: UTF-8, no BOM, a single object, duplicate keys reject, `NaN`/`Infinity`
  reject, size bounded by `max_entry_bytes`;
- nesting bounded by a fixed `MAX_JSON_DEPTH = 64`: the root object is depth 1 and every
  nested object or array (including an empty one) adds 1 along its path; any path deeper
  than 64 containers rejects. The bound is checked by an iterative walk of the parsed
  value after `json.loads`, so the outcome does not depend on the interpreter's
  recursion limit (a parser `RecursionError` rejects as malformed JSON; a document the
  parser accepts but which exceeds the bound rejects as excessive nesting). Sibling
  count does not affect depth; node count is bounded indirectly by `max_entry_bytes`.

The CRC check is independent of the server digest: honest metadata for corrupted
bytes still rejects.

## 7. Journal envelope (`candidate-artifact-journal/v1`)

Exact field set. Every binding field must **equal** the trusted policy and the
authenticated run.

| Field | Rule |
| --- | --- |
| `schema` | `"candidate-artifact-journal/v1"` |
| `repository_id`, `repository`, `policy_commit`, `workflow_id`, `workflow_path` | equal the policy |
| `producer.run_id` | equals `run.id` |
| `producer.run_attempt` | 1, equals `run.run_attempt` |
| `producer.event` | equals `run.event` |
| `producer.runtime_sha` | runtime `GITHUB_SHA`; equals `run.head_sha` and `policy_commit` |
| `producer.workflow_ref` | qualified `owner/name/.github/workflows/<file>.yml@refs/heads/<default_branch>` (as `GITHUB_WORKFLOW_REF` spells it); bare or foreign forms reject |
| `journal_sequence` | positive int; 1 for genesis |
| `predecessor_digest` | `null` exactly when `journal_sequence == 1`; otherwise the canonical digest of the previous journal document |
| `records` | append-only list, ≤ `max_records` |

**Canonical digest**: sha256 of `json.dumps(document, sort_keys=True,
separators=(",", ":"), ensure_ascii=True)`. The archive entry itself need not be
canonical; the chain digest is over the parsed document.

### Records and transitions

Each record has exactly `sequence`, `operation_id`, `candidate`, `operation`, `state`,
`run_id`.

- `sequence` is 1..n contiguous in list order (gaps and reordering reject).
- `operation_id` is 32 lowercase hex characters, unique per side-effect operation.
- `candidate` is `{repo, pr, head, base, merge, tree}` with full lowercase SHAs; its
  canonical digest is the neutral journal key. Runtime gate keys can bind additional
  fields: Cockpit's `Candidate.key` also binds numeric repository/PR IDs and ref names
  and is not interchangeable with this key. No runtime adapter is wired or approved.
  `repo` must equal the journal repository; the envelope separately binds its numeric ID.
- `operation` ∈ `label-post`, `comment-post`, `workflow-dispatch`.
- `run_id` is the recording producer run, ≤ `producer.run_id`.
- States and the only legal transitions per `operation_id`:
  `reserved → attempted → {confirmed | rejected | unknown}`. The first record of an
  operation is `reserved`; nothing follows a terminal state. All records of one
  operation bind the same candidate and operation.

`unknown` is a terminal-uncertain outcome: the POST was issued and its result is
not known. `rejected` is a **confirmed** HTTP rejection. Timestamps are not used for
ordering; list order is.

### Semantic state (`journal_status`)

Two scopes are reported and must not be confused:

- **Candidate state** (`candidate_state`): for the exact candidate identity
  (`repo, pr, head, base, merge, tree`), the latest state of each of its operations is
  reduced, most severe first: `unknown-outcome`, `attempted`, `reserved`, `confirmed`,
  `rejected`, else `not-dispatched`.
- **PR-level reconciliation** (`pr_unresolved_operations`): every operation for the
  same `repo`/`pr`, under **any** candidate identity, whose latest state is `reserved`,
  `attempted` or `unknown`, as a sorted list of operation IDs. Label and comment POSTs
  act on the PR, so a head, base, merge or tree movement does not undo an unresolved
  POST against the old identity.

`semantic_state` is the candidate state, except when the PR has unresolved operations
under another identity and the candidate's own state is not already blocking
(`not-dispatched`, `confirmed` or `rejected`): then it is the explicit `pr-unresolved`.
A moved head therefore never reads `not-dispatched` while its PR has an unresolved
ledger. `evidence-unavailable` is returned for both scopes when the journal is missing
or uncertain. Other fields: `operator_reconciliation_required` (true for
`unknown-outcome`, `evidence-unavailable` and whenever another identity of the same PR
has unresolved operations), `in_flight` (own reserved/attempted), `operations` (own
operation IDs).

Loss semantics:

- A missing, expired, deleted or uncertain artifact **never** means not-dispatched.
- `reserved`, `attempted`, `unknown-outcome` and `pr-unresolved` cannot be redispatched.
- An expired, deleted or lost reservation is operator reconciliation, not a reset.
- Candidate movement (new head/base/merge/tree) and policy movement do not reset the
  unresolved ledger of a PR; only appended terminal records resolve it.
- No field in any result asserts that redispatch is permitted. A future consumer may
  consider only `not-dispatched` and `rejected`, under its own owner-approved rules,
  and only when `operator_reconciliation_required` is false.

### Reconciliation (`reconcile(previous, current)`)

`previous` is one of:

- `{"kind": "genesis"}`: caller-supplied offline bootstrap context. The current journal
  must be sequence 1 with `predecessor_digest: null`. The validator **cannot
  authenticate a bootstrap**; it only checks the shape. In v1 only the test harness
  supplies this value. Real bootstrap and operator acceptance of a genesis remain
  owner-held activation prerequisites.
- `{"kind": "journal", "document": {...}}`: the last validated journal. Current must
  be identical (`unchanged`) or sequence +1 with `predecessor_digest` equal to the
  canonical digest of the previous document and the previous `records` as an exact
  prefix (`extended`). Dropping, resequencing or rewriting any previous record,
  unresolved or terminal, rejects. Repository identity cannot change. Policy movement
  does **not** reset the chain; a sequence-1 document after real history is a
  manufactured genesis and rejects.
- `{"kind": "unavailable", ...}`: lost evidence. Always rejects; there is no fresh
  genesis around uncertainty.

Resolving an open operation appends a new record; it never edits the old one.

## 8. Stage result and disabled entry

`evaluate_offline(trusted_policy, snapshot_before, snapshot_after, archive, candidate,
previous_evidence)` returns:

```
{"stage": "artifact-journal-v1-offline-diagnostic", "diagnostic_valid": true,
 "candidate_key": ..., "run_id": ..., "artifact_id": ..., "artifact_digest": ...,
 "journal_digest": ..., "journal_sequence": ..., "chain": "genesis|unchanged|extended",
 "semantic_state": ..., "candidate_state": ..., "operator_reconciliation_required": ...,
 "in_flight": ..., "operations": [...], "pr_unresolved_operations": [...],
 "admitted": false, "dispatch_allowed": false, "side_effects": "rejected"}
```

`stage` and the last three authority fields are assigned after every diagnostic field
and cannot be overridden by any input, including a diagnostic key named `stage`.
`disabled_entry(...)` ignores all arguments and returns string outputs
`admitted: "false"`, `dispatch_allowed: "false"`, `side_effects: "rejected"`;
`main()` prints a notice and returns 1 so an accidental wiring fails the job.

PR comments remain optional human-readable mirrors. They are never inputs that prove
authority, budget or journal state under this contract.

## 9. Fixture conventions

The fixture is **synthetic and non-authentic**. Its `notice` says so and the suite
asserts it. Archive digest and size on the journal artifact are placeholders
(`sha256:<computed-from-archive-bytes>`, `0`) that the test harness replaces after
building a deterministic single-entry stored ZIP in memory from the canonical bytes of
`journal_document`. No binary blob is checked in; placeholders can never validate.
`snapshot_after_observed_at` gives the second observation time. `expected` states the
diagnostic result and `other_candidates` gives a confirmed and an unrecorded candidate
for the same journal. `previous_evidence` is the harness-supplied genesis context and
proves nothing about a real bootstrap. The peer repository can copy the file and keep
the same materialization rule.

## 10. What is not established, and activation prerequisites

No genuine provenance exists yet, so no diagnostic validity is claimed for real data.
The specific missing inputs are: a reconciler workflow on the default branch and its
numeric `workflow_id`; an owner-reviewed trusted policy with source closure; an
authenticated snapshot transport inside the future reconciler; a real run, artifact
and archive; an owner-held genesis decision; and an accepted native attestation route.
Until those exist, this contract is prepared text plus an offline validator.

Activation prerequisites, all owner-held and outside this slice: trusted producer
workflow authoring and review; bootstrap/genesis; policy pinning procedure; consumer
wiring and the decision on which semantic states a consumer may act on; protected
check and permission review (no expansion assumed); native attestation route;
cross-repository acceptance by each owner; and the existing holds in
[rollout limits](ci-queue-rollout.md), which stay in force. Local tests passing do
not approve the candidate contract or any artifact trust.

## 11. Tests

```
python -X utf8 -B tools/test_candidate_artifact_journal.py
```

Standard `unittest`, stdlib only, in-memory ZIPs. Covered: synthetic valid genesis
(schedule and reviewed dispatch); forged same/foreign run IDs; workflow/path/policy/
closure mismatches; candidate mixups; bad repository/actor/event/attempt; altered
bytes, digest, size and CRC; duplicate JSON keys, non-finite numbers, unknown and
trust-flag fields; missing/deleted/expired/duplicate artifacts; newer pending/failed/
cancelled/successful runs versus older success; reruns; before/after movement; default
branch and policy movement; unknown outcome and lost evidence never authorize
redispatch; moved head/base/merge/tree against old-identity reserved/attempted/unknown
operations reads `pr-unresolved` with reconciliation required, other PRs isolated,
terminal operations not blocking, no reset across policy or candidate movement;
dropped/rewritten/resequenced records; manufactured genesis; repository segments `.`
and `..` rejected at policy, candidate and envelope layers; disabled entry under valid
inputs and activation knobs; `stage` label and authority fields immune to diagnostic
keys; record bound exercised apart from the entry
bound; no activation symbol, transport, environment or extraction call in the module
(checked structurally over the parsed source and the runtime namespace, not by text
search); existing gate and workflows not wired to the module.
