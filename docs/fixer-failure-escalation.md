# Exhausted fixer failure handoff

CSL-201 adds a separate intake to the existing stuck-thread watcher. It does
not run another fixer, reset attempts, synthesize a Claude reply/result,
apply READY, push, merge or change the resolver. The existing schedule,
writer queue, delivery identities and mediation markers remain in use.

## Evidence required

1. The PR is open, non-draft, same-repository and authored by a current human
   writer. Its head and eligible Devin bug roots are freshly read. The existing
   receipt consumer excludes valid addressed/needs-human handoffs. Recursion
   additionally validates its authenticated observation history without writes;
   a needed history transition waits for the locked fixer to record it.
2. The existing three authenticated reservations match the exact current head
   and pending-finding fingerprint. These establish exhaustion, not three model
   failures. Changed findings or a new head keep their normal fresh budget.
3. At least one reserved run attempt is a completed failing automatic fixer
   run of `.github/workflows/claude.yml` in this repository, for this PR, branch
   and exact head. Review events bind those fields directly. Automatic
   `repository_dispatch` uses default-branch run metadata: its immutable
   workflow blob must match the current trusted blob, and its authenticated
   PR reservation plus successful context step bind the target PR/head.
   Manual workflow dispatch and review-comment mention jobs do not qualify.
   Its `devin-review-fix` job succeeded at the guard-install step
   named `Fixer attempt <head>:<finding-fingerprint>` before the Claude action
   failed or timed out. A cancelled action step qualifies only when both its
   parent job and run independently timed out. Human/concurrency cancellations
   do not qualify.
   GitHub attempt/job metadata supplies
   this binding independently of comment prose. A terminal-post failure alone,
   successful/skipped validation-only run, wrong attempt, missing context or
   failure before the action step cannot qualify. An action failure can itself
   occur during authentication/setup: this proves a failed fixer action, not
   that the model ran.
4. The workflow blob on that head must match the current default-branch blob.
   Modified PR workflows and older workflow versions are not retroactively
   qualified. This compares current head/default blobs, not the historical
   merge-ref definition executed by the run. The default branch is trusted.
   Public repository visibility is established before Actions
   metadata reads. Prefer the existing read token; if its public-metadata read
   is forbidden, use the independently public endpoint without credentials
   and remember that choice for subsequent public reads in this collection.
   Existing authenticated reads handle PR/finding data;
   no new token, permission, logs, artifacts or model output are needed.
5. Before each new failure Linear create/update, after Linear lookups, and
   before each mediation comment, recheck the current finding set, exhausted
   budget and PR head. A denied or stale check defers that delivery. Refuse observed queued, in-progress, waiting, pending or requested
   Claude workflow runs anywhere in the repository, because mention/manual
   runs can be attributed to the default branch. This conservative refusal
   does not claim that an unrelated PR's writer targets this PR. It is a
   bounded observation, not an
   atomic cross-workflow lock; a writer or review change can race the final
   read. Mediation conveys evidence and requests inspection, never a fix or
   readiness verdict. A mediation comment can itself queue a skipped Claude
   workflow; a later finding then waits for another cycle. Caps are maxima,
   not a promise of three deliveries per cycle. Failure mediation neutralizes
   literal Claude mentions in quoted findings and paths to avoid dispatch.

Each PR adapter has a shared 60-read/60-second ceiling, at most five pages per
comment/thread connection and at most three candidate run attempts. Denial,
public API rate limiting, malformed/duplicate/incomplete snapshots and budget
exhaustion stop new failure admissions for that PR and retry on a later watcher
cycle. A 429 or transient server failure receives at most one bounded retry;
primary public rate limiting defers to a later cycle. Incomplete failure
evidence does not suppress ordinary Claude-thread mediation or Recursion's
existing resolver, even if the collector cannot load. Ordinary Linear routing
continues. Existing failure Linear roll-ups and human notes are preserved
while failure evidence is stale/unavailable; no partial checklist replaces them.
Stored failure sections are retained independently when ordinary items change,
including after a new head rearms the budget. They are removed on a later
roll-up rewrite only when the watcher explicitly observes their thread resolved;
they never admit a mediation action.
Only complete evidence enters existing per-thread deduplication and caps.

## Repository-specific behavior

Cockpit keeps its rolling 24-hour mediation cap and never resolves threads.
Recursion keeps UTC-day mediation/resolve caps and its existing authenticated
Codex ruling, current full SHA, required-green-check and citation requirements.
Failure evidence does not participate in that resolver. A failure-triggered
request can ask Codex about a no-Claude thread; a later independently qualified
Codex ruling for that requested thread follows the existing policy. No resolver
predicate or authority is relaxed.

## Landing and operational limits

Stack this change on the guarded-fixer reliability draft (#155 in Cockpit,
#211 in Recursion). Cockpit stays after #147 in Lucas's authorized sequence.
Use ordinary dependency/base merges after those owners settle their work;
do not import moving upstream branches or rewrite their history.

CSL-255 owns CI cost/queue changes. This patch does not rely on proposed
merge-queue or `review-gate` behavior, change required checks, or alter cost,
concurrency/debounce policy. It adds only the new source/test paths to existing
lightweight event-path checks. Recursion's default-branch sparse checkout also
includes the adapter and receipt consumer; token grants, checkout ref and
persist-credentials remain unchanged. Compose shared `claude.yml` changes with
that owner and CSL-89 before landing. Cloud bootstrap #156/#212 is separate.

Offline tests cover provenance, races, no false handled receipts, caps,
deduplication and fresh retry eligibility. They are not evidence of an
autonomous fix-to-push or live escalation. Qualification needs an existing
genuine eligible finding, coordinated single writer, deployed matching
workflow and actual failed context-bound run after genuine exhaustion.
Never manufacture findings/events or reopen resolved findings to qualify it.
Historical #142 lacks the new context binding and intentionally cannot be
replayed as new evidence. Existing workflow validation blocks pre-merge live
qualification of a changed workflow. No bypass is authorized.

## Review repair qualification

Mediation bodies neutralize every case-insensitive Claude mention substring,
including Unicode suffixes and longer names, on ordinary and failure routes.
Quoted data never grants a writer trigger. Local fixtures exercise these inputs;
no hostile trigger is posted to GitHub for testing.

Cockpit retains failure blocks only outside rendered text fences, including
legacy bodies. Quoted headings, markers, footer and managed-section sentinels
are neutralized in new output. A current ordinary item supersedes its historical
failure entry by thread identity; quoted marker text cannot suppress other
entries. Resolved real threads do not leave forged quoted entries behind.

Filename backticks are preserved using a longer Markdown code-span delimiter;
control characters are represented as a JSON string on one line. This avoids
silently pointing mediation at a different filename while keeping quoted
structure and writer-trigger sanitization intact.

Dispatch event semantics follow [GitHub’s workflow event reference](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#repository_dispatch).
No genuine repository-dispatch failure was available during this review; its
provenance regressions are offline fixtures, not live qualification.

Linear admission is centralized after its lookups: stale or unavailable failure
evidence removes only new failure items and preserves independently eligible
ordinary delivery and previously recorded handoffs. Recursion applies the same
check to the existing-rollup sweep and refreshes retained entries alongside
ordinary items on approved/green PRs.

Cockpit recomputes admission after that late filter using the remaining cap
and all PAT-eligible thread IDs. Stale failures and failed posts consume no slot,
so a newly eligible ordinary thread receives its Linear record and mediation.
Already-mediated ordinary items alone cannot update an existing issue; they can
still recreate a missing roll-up after an earlier Linear outage.

The 60-read ceiling is unchanged. A real-reader dispatch/Linear fixture posts
two of three comments before budget exhaustion, then posts the remaining one on
a later scan within the same daily cap. Only actual posts consume slots; bounded
deferral is not a lost slot or a handled finding.
