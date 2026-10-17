# CSL-255: staged queue rollout

This change is preparation, not evidence of enforced queue protection. Existing
repository rules remain untouched. The independent review found that the original
#160 Linux aggregate did not require queue Windows or Rust, and its review check
accepted an unrelated app's successful check or an obsolete successful attempt.

## Proposed stable required checks

| Exact check name | PR evidence | Merge-group evidence |
| --- | --- | --- |
| `linux-gates` | Linux 3.11/3.12, or explicit docs noop | Same |
| `queue-gates` | Linux; also Windows for full requests | Linux and Windows 3.11/3.12 |
| `rust-gates` | Every selected Rust cell or explicit irrelevant-path noop | Linux and Windows Rust 3.11/3.12, or explicit irrelevant-path noop |
| `review-gate` | Current genuine Devin success on the exact live PR head | Same for every unambiguously identified constituent PR |

Unknown filter outputs and pending, failed, cancelled, or unexpectedly skipped
mandatory work cannot pass an aggregate. Drafts fail closed. All PRs report the
Rust aggregate; the path filter runs inside the workflow. macOS remains best
effort. Main push Linux, Windows and relevant Rust checks remain enabled until a
separate owner-approved removal after queue enforcement is proven. Local DEC-09
and all product/safety gates remain unchanged.

## Review identity and recovery

The read-only Cockpit #147 evidence on 2026-10-16 established that Devin publishes
commit statuses, not check runs. Head `c2708f2248a1e04bebf511c19007841b0aa18037`
had `Devin Review` success status `56550390641`, after pending `56550267617`, from
creator ID `158243242`, login `devin-ai-integration[bot]`, type `Bot`. It had no
Devin app check run. The sanitized status payload is retained in
`tools/fixtures/devin-cockpit-147-statuses.json`. App metadata alone did not prove
the original check-run consumer could observe the provider.

The gate requests every page of statuses on the exact commit SHA. Context and all
three creator identity fields must match; the canonical API status URL must bind
to the requested repository and SHA. The largest authenticated status ID is the
current update, independent of response order. Identical repeated IDs deduplicate;
conflicting copies, invalid IDs and unknown states fail closed. Current pending,
failure or error blocks old success. Check runs are not an alternative success
source. Display names, descriptions and target URLs never authorize passage.
Before accepting success, the gate rereads the first page and requires it to match
the scan; an insertion while traversing pages returns pending for the bounded poll
to retry. Status reads are not an atomic snapshot and cannot exclude a new update
after the final read. Direct Devin protection and the activation hold remain.

Polling has a 120-second deadline, 30-second per-API timeout, and a five-minute job
timeout. A failed status can be replaced by a new successful retry within the poll
bound. A submitted PR review starts another bounded attempt. A completion after
the bound requires the owner to rerun the failed `review-gate` job, or manually
dispatch this workflow on the exact live PR branch with its PR number. Dispatch
validates that the selected SHA still equals the live PR head. A new commit is not
required. There is no claim of automatic recovery for every late status completion:
`check_run` workflows attach to the default-branch SHA and are deliberately not
used to impersonate a successful PR status.

The review workflow keeps #160's read-only `contents: read` and `checks: read`
permissions; it adds no write token or permission. `checks: read` was itself new
in original #160 and remains part of the owner's review of this proposal.
[GitHub documents](https://docs.github.com/en/rest/commits/statuses#list-commit-statuses-for-a-reference)
that public status reads need no additional statuses permission. This public repo
uses the existing token without adding permissions or falling back to other
credentials. API authorization, rate-limit or transport failures block. No
private-repository support or hosted-token qualification is inferred from the
local read-only API probe; permissions are never silently escalated.
The policy script executes from the PR/queue base. The workflow YAML itself still
comes from the PR/queue/dispatch ref, so a PR could replace that step or its check
name. The platform aggregates also execute head code. Base checkout alone is not
an independent protection boundary. Keep direct `Devin Review` required; do not
replace it with `review-gate` under this proposal. A separately approved protected
required workflow or equivalent owner-enforced control over workflow/policy edits
is necessary before considering replacement. This change creates no such control
and changes no ruleset, CODEOWNERS, or permissions.

On a PR head, a later Linux-scope dispatch can report a narrower successful
`queue-gates` result after a full-scope failure. Inspect the actual full platform
checks when assessing PR diagnostics. An enforced merge-group run must independently
require both Windows cells on its distinct merge-group SHA; PR dispatch results
are not evidence for queue enforcement.

Queue discovery paginates the base-to-group comparison and each constituent commit's
associated PRs. Every constituent commit must map uniquely to an open non-draft PR
on the target base whose exact live head is included in the group. Queue-created
merges are exempt only when their SHA is the exact merge-group head attested by
the GitHub event and they have multiple parents. PR-authored merge heads require
review coverage; intermediate merges without a unique PR mapping block. Parent
count alone never identifies a queue-created commit. Missing, stale, ambiguous, truncated or
over-limit evidence blocks. This conservative mapping still needs a live canary
with the repository's actual queue merge strategy; synthetic tests are not proof
that every GitHub queue topology maps successfully.

## Activation sequence and owner decisions

1. Integrate replacement #162, #163, #164, #165, then #166; run the combined tree's
   tests and all changed workflows through actionlint. Preserve the #164 settle
   dependency in every aggregate, including intentional docs noops.
2. Land the policy under the existing protection (currently `linux-gates` and
   `Devin Review`). Do not require the new base-loaded `review-gate` before its
   policy script exists on main; its deliberate missing-script failure is not a
   bootstrap success.
3. The owner reviews read permissions, the exact check names above, app provenance,
   base policy loading and review/queue diagnostics. Preserve current direct Devin
   protection. Replacing it remains blocked on a separately approved protected
   workflow/policy control; a green canary alone cannot establish that control.
   No role, receipt, trust or fixer
   policy is replaced by these aggregates.
4. Queue activation is blocked until there is evidence that the currently required
   direct `Devin Review` reports on the merge-group SHA, or the owner separately
   approves a protected replacement that establishes the trust boundary described
   above. Do not disable direct Devin protection to make a canary pass. Only after
   resolving that prerequisite and obtaining explicit owner approval may the owner
   configure required checks/queue enforcement and run a controlled live queue
   canary. Confirm the required checks bind to the
   merge-group SHA; demonstrate Windows/Rust pending, failure and cancellation
   blocking, docs noops, multiple PRs, and late review recovery. Live negative
   tests and repository settings changes are not authorized by this draft.
5. Only after that evidence may the owner approve a separate proposal removing
   redundant main push jobs. Keep the fallback throughout this staged rollout.

The public repository's standard Ubuntu control jobs are unbilled but consume
runner capacity. Neither polling changes nor queue preparation guarantees savings.
P2 budget and cadence remain open; do not treat the historical budget as newly
ratified or trade validation for it.

# Candidate follow-on (CSL-259)

The [exact candidate coordinator](ci-candidate-readiness.md) adds staged full
ordinary-candidate evidence for all five Python and five Rust cells, including
docs-only candidates, after independent READY and before human approval.
`candidate-ci-gates` is an additional staged aggregate; no existing required name
or repository protection changes here. The protected-policy and live-canary
holds above still apply.
