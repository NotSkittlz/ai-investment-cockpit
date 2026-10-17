# Guarded fixer reliability (CSL-201)

The workflow installs its embedded pre-push guard outside the PR tree. The
guard consumes one push attempt, verifies the advertised remote head and live
PR identity against the locked checkout, and checks that the candidate descends
from that checkout. A moved head, failed read or second push stops the run.
Git still makes the final ordinary push decision; an external writer can race
the last read. Never fetch, merge, rebase or retry from that stale checkout.

The model must run the existing acceptance gates, copy the full output of
`git rev-parse HEAD`, and verify the live PR head after successful push before
posting an addressed receipt. Short SHA values remain invalid. The guard prints
the full candidate but explicitly does not claim the push has succeeded.
Receipt parsing and authentication are unchanged: model prose is not evidence
of completion, and the workflow audit never creates an addressed receipt.

The terminal audit reconciles actual PR review replies against the captured
per-root context using the workflow-owned copy of the existing receipt consumer.
It reports verified addressed receipts separately from human handoffs, missing
receipts, invalid/superseded receipts and changed roots. Full SHA, authenticated
Claude numeric identity, body fingerprint, captured base and remote ancestry are
required. A reachable push with an abbreviated receipt is reported as invalid;
the workflow does not repair or author that receipt. This is deterministic
reconciliation of observed output, not proof that a model will produce it.

Reconciliation is read-only and does not change eligibility, reservations,
resolution or READY. It checks at most 50 captured roots and five comment pages;
all terminal reads share a 40-call/70-second budget with a final live-head check.
Incomplete reads, duplicate snapshots, changed heads, denied access and exhausted
budgets report unverified. Mention runs without captured context say not
applicable. Results describe the observed context, not an atomic current-thread
decision; future runs still recheck resolved/outdated findings and later replies.

A `!cancelled()` step records a terminal audit after model failure, denied commands,
turn exhaustion or missing execution output. It independently checks the guarded
candidate's remote ancestry and posts a PR notice. If the comment API fails,
the job summary retains the audit and the step fails rather than claiming a
posted handback. A 20-minute model-step timeout reserves room inside the existing
30-minute job. The model limit remains 30 turns. Runner loss, whole-job timeout,
hard cancellation and unavailable GitHub can still prevent remote delivery;
this is not an external durable queue or an atomic writer lock.

Failure notices do not suppress findings or reset reservations. A fresh authorized
run must re-evaluate genuine unresolved/current findings, author permissions,
the three-attempt budget and eight-commit guard. Resolved or outdated findings
are never reopened for qualification. No persistent account access, credentials,
paid provider API or expanded model tool allowlist is introduced. The Git hook
configuration exists only in the disposable runner checkout.

This hook is a reliability control for the authorized fixer, not a security
boundary against hostile code executing as the runner user. Existing acceptance
gates execute PR code with that user's filesystem access; an adversarial test
could alter the hook or Git configuration. Edit/Write denials reduce accidental
model edits but do not isolate shell subprocesses. A separately trusted publisher
would require a broader security design; no such isolation is claimed here.

The handback is a PR-level notice to the author. It does not impersonate a Claude
thread reply or make the stuck-thread watcher classify a no-Claude thread as
stuck. Automatic mediation of workflow failures needs a separate actor-trust and
routing decision. The author must relay such blockers to Linear; transient
failures remain eligible for a fresh bounded retry rather than a needs-human
receipt. One audit is intentional even after model success, which alone cannot
prove every per-root receipt was valid.

## Mediation and implementation remain separate

Devin submitted reviews and authorized human requests drive Claude. Codex
READY requests CI only after live reconciliation; MEDIATE requests adjudication.
Neither is a fix receipt. Codex comments cannot impersonate Claude receipts or
consume Claude attempt reservations. Legacy FIX/timeout dispatch stays disabled.
The new tests retain these distinctions, alongside the existing permission,
same-repository, queue and readiness controls. CSL-89 owns resolution policy;
this change does not enable new thread-resolution authority.

Read-only #116 diagnosis (2026-10-16): run 38944245119 was a Codex event and
returned `mention=false`, `devin=false`; author NotSkittlz had admin permission.
Devin runs 38939609601 and 38936042436 reached preflight and skipped before
reservation/model execution. Their findings 4297383409 and 4297144162 have
`kind: analysis`, which the current automatic policy excludes. This establishes
intake scope, not evidence that mediation displaced or starved Claude.

## Operational evidence and landing

Historical cockpit #119/#142 pushes lost races to external writers; #142 run
38884600543 also exceeded its configured turn limit. On 2026-10-16, cockpit
#147 run 38944702649/job117120821057 successfully pushed
`d5194ed085712b884a58938d8877926abeed8f75`, parent
`ab7f04fc051800fb0200fbc9ce2d78447d3660b9`. Authenticated Claude reply
4297849933 nevertheless used `commit: d5194ed`, which the strict parser rejects.
That proves a genuine cockpit automatic push, not an accepted machine receipt
or qualification of this new guard, recursion, or contested-writer recovery.

This work uses isolated branches. Cockpit landing is held until #116, #141 and
#147 settle and merge (#153/#151 already merged). Refresh only this branch via
ordinary base merges and repeat checks then; no rebase or force-push. Recursion
has its own verified default base. Also reconcile any overlapping CSL-89 draft
(cockpit #126 / recursion #197) before landing. Never modify another owner's lane.

Sources: [Git pre-push contract](https://git-scm.com/docs/githooks#_pre_push),
[Actions timeout syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idstepstimeout-minutes),
[#116 Codex event](https://github.com/CSL-Laboratories/ai-investment-cockpit/actions/runs/38944245119),
[#147 receipt](https://github.com/CSL-Laboratories/ai-investment-cockpit/pull/147#discussion_r4297849933).

## Independent subscription review follow-up

An independent, tool-disabled Claude Max source review found that failed model
steps masked execution-file reasons and Git remote names could collide with
the hook's maintenance modes. The audit now reads coarse result fields even
after step failure, prioritizes turn exhaustion over recovered denials, and
uses step failure only as a fallback. Install/terminal modes require exactly
one argument; a two-argument Git hook invocation always goes through target
validation. Read-budget and JSON decode failures remain globally unverified
instead of being mistaken for an observation-history change. Regression tests
cover these paths and the exact ordinary-push allowlist (including negative
force/no-verify/config/remote/dry-run controls).

Receipt reconciliation is a current snapshot and can include a valid receipt
that predates this run. It is not attribution of a fix to the current model
invocation. A missing guarded attempt remains unverified; neither local hook
configuration nor the shared runner identity is a hostile-code boundary.
This change does not alter checkout credential wiring or promise resistance
to code that changes hooks or uses the existing runner credential directly.

## Least-privilege API follow-up

The model no longer receives generic `gh api` access: raw API commands are
explicitly denied. The protected workflow helper exposes fixed current-PR
reads (including a fixed paginated thread-state query), issue comments and
review replies. It accepts no arbitrary endpoint, HTTP method, GraphQL query
or mutation, so ref create/update/delete cannot bypass the push guard through
that tool. Comment data comes only from regular `.claude-fixer-replies/NAME.txt`
files under the checkout; traversal, absolute paths and symlinks are rejected.
These files must not be committed. The helper uses the existing credential.

The push hook also validates Git's actual remote URL against this repository's
GitHub URL. Offline tests cover real delete records, empty input, tags, foreign
URLs, fixed API operations and forbidden ref mutations. The terminal step runs
after model success/failure but honors explicit workflow cancellation; runner
loss and cancellation remain delivery limitations. Existing same-user shell
execution is still outside the hook's isolation guarantee.

The URL check accepts the action's existing `x-access-token` HTTPS userinfo
after parsing and binding the actual GitHub host and repository, without logging
credentials. Other hosts/users, queries/fragments and ports remain rejected.
Malformed helper invocations fail without reading stdin or consuming a push.
Reply input opens nonblocking, verifies a regular descriptor and bounds its read;
named pipes and symlinks cannot stall the helper. These cases have offline tests,
including simulated action URL metadata through the real local Git hook fixture.

Remote transport syntax is restricted to canonical GitHub HTTPS/SSH forms.
The existing action token may contain only URL-unreserved characters; multiple
userinfo separators, encoded delimiters, empty ports and non-ASCII host lookalikes
are rejected without printing the URL. This defensive parser restriction is not
proof that a malformed URL previously reached another server.

Before posting a review reply, the helper independently reads the target comment
and requires its numeric ID, current PR URL and top-level root identity. Missing,
foreign, malformed or denied target reads produce no POST. This is local binding,
not a claim that a cross-PR GitHub write was demonstrated.
