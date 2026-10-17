# CLAUDE.md — operating manual for the AI Investment Cockpit

> Auto-loaded each session. Keep it tight and high-signal. The detailed playbooks
> and the full design corpus live in `docs/` — start at `docs/README.md`.

## Product target and current implementation

The ratified [PRODUCT.md](PRODUCT.md) owns purpose, scope and acceptance for the
integrated autonomous research-to-paper product. [PLAN](docs/PLAN.md) owns delivery
order and the reviewed migration from the current implementation described below.
The nine invariants and runtime rules here remain in force until their separately
scoped, reviewed implementation amendments; ratification alone grants no new authority.

Current implementation:
A solo-developer, subscription-powered, **human-gated** AI investing copilot.
**Paper-first** — no live capital moves until the pre-live gates pass. Think of it
as **two engines**:
1. **Awareness + discipline** — the reliable value (time saved, not missing things,
   enforced discipline). Powered by the AI's real strength: information work.
2. **Execution** — mechanically reliable, financially *experimental*, capped, and
   gated. Engine 1 makes Engine 2 safer and better-informed; it never makes it
   "reliable growth."

The reliable ROI is **time, awareness, and discipline — not alpha.** Any plan that
leads with "the AI will beat the market" is wrong; the corpus documents why.

## The 9 invariants — NEVER weaken silently
1. **Single order path** — an order reaches a broker ONLY via risk-pass → mode →
   approval → `OrderGateway`. This includes **human-initiated** trades.
2. **Decision path pinned to Claude in auto modes** — Codex/other runtimes are
   barred from the decision/order path.
3. **Schema-validate every model output** (repair-then-quarantine).
4. **Secrets only in the OS credential store** (`keyring`) — Windows Credential
   Manager, macOS Keychain, or Linux SecretService; never in repo, prompts, chat,
   or logs.
5. **`risk/` is pure deterministic functions** — NO LLM, NO I/O, NO subprocess.
   Enforced by the no-LLM-in-risk CI gate.
6. **Human gate is client-side; hash-chained append-only audit** — every
   state change is audited.
7. **Fail closed** — vendor-limit / stale-data / hung orchestrator → `watch_only`,
   never "trade without fresh analysis."
8. **Raw inputs stored separately from summaries** — enables replay + interrogation.
9. **Post-PDT risk model** — Reg-T $2,000 min equity, no day-trade counts.

**PAPER-FIRST** overrides all: no live capital until the P0 pre-live gates pass —
chief among them SEC-11 (`docs/security-hardening-backlog.md`).

## Current load-bearing rules
- **New data feeds are UNTRUSTED input** — wrap in `<untrusted-content>`,
  canary-scan (SEC-02). Inv 3 validates model *output*; this governs tool/feed *input*.
- **The current operator surface is Discord + loopback API.** A general
  `OperatorSurface` abstraction is planned, not merged; do not create new order authority or
  assume another adapter already exists.
- **The current order universe is a static operator-approved allowlist.** Dynamic discovery is
  future awareness scope only; no model or API may mutate execution eligibility at runtime.
- **Execution stays model-free.** Models serve awareness/reasoning only.
- **Research/dev host is Cursor Cloud Agent Linux VM.** Cursor-native fresh
  sessions are preferred; Codex/OpenCode development routes require exact-host
  qualification and admitted budgets. Devin is retired. Muse synthesis requires
  a different model family; OpenRouter/Hive are capped fallback synthesis only.
  Current process: `docs/research/agent-swarm-rebase-2026-09-14.md`.
  Cloud handoff: `docs/research/cursor-cloud-rdt-handoff-2026-09-14.md`.
  No swarm broker tools, secrets or soak starts. Product authority is unchanged.

## Session workflow
1. Read `docs/README.md`, `PRODUCT.md` for product scope/acceptance, then
   `docs/PLAN.md` for current delivery order and `docs/REGISTER.md` for claims.
   The adopted product contracts supply required behavior; subsystem/reference
   documents do not independently change delivery order or current runtime authority.
2. Pick **ONE** work package (RW-xx refactor, or W-xx production). Confirm its
   scope and its deterministic gate before writing code. Decision or direction
   work: see `AGENTS.md` Linear-first (assignee Parallel Task API brief; Grok Bots
   do not research or resolve those issues).
3. Implement. Preserve the invariants. Add tests alongside.
4. Run the gates (below). Green before you stop.
5. **Do not re-litigate settled decisions** — the corpus is a one-way pipeline
   (research → architecture → build → security → enhancements). Recorded
   architecture and direction live in `docs/adr/` and the linked Linear
   `decision` issues.
6. Commit only when the operator asks. Message convention:
   `feat(RW-xx): …` / `fix(RW-xx): …` / `docs(RW-xx): …`.
7. **CI etiquette (CSL-255):** open PRs as drafts; batch pushes; mark ready
   once after validation and only when no owner hold remains. Never merge a
   PR, rebase, or force-push. Avoid routine base merges; a normal base merge
   is allowed for a real conflict or an owner-approved dependency integration,
   even without a text conflict. Record the reason and rerun the combined gates.
   Never edit `.github/workflows/**` unless a Linear issue
   explicitly assigned by a human asks for it; bots mark ready with their own
   identity, never `GITHUB_TOKEN`.

## Hard rules
- Never put an LLM on the order or risk path.
- Never move live capital or unlock an auto rung without its gate.
- **Never weaken a gate or invariant to make a test pass — fix the code.**
- Prompts live in git; fail any soak/replay run on a dirty working tree.
- Any change touching the order path gets an **adversarial-review round** (the
  attack→fix→regression pattern; see `docs/ultracode-build-playbook.md`).
- When unsure whether something is settled, grep the corpus before deciding.

## Run the gates

`DEC-09` defines the release-blocking matrix: run the full suite and Ruff locally on native
Windows and Ubuntu/WSL2, each with Python 3.11 and 3.12. macOS is best-effort. Hosted Actions
do not replace that matrix: Linux `linux-gates` is the cheap required PR check; Windows runs
on push to `main`, `merge_group`, the `ci:full` label, or full `workflow_dispatch`; macOS runs nightly on `main` (07:17 UTC,
skipped if idle 24h), on `ci:full`, or on `workflow_dispatch`. Hetzner is not part of
current package validation. This changes platform evidence only and does not waive any online,
broker, witness, paper-day, or pre-live gate.

The staged [CSL-259 candidate coordinator](docs/ci-candidate-readiness.md) requests
all five Python and five Rust cells, including docs-only candidates, after an
authenticated exact head/base/merge/tree independent READY and before human
approval. It preserves human holds and manual label removal; persistent labels
and legacy head-only review/CI are not proof. No repository protections or queue
settings are activated by this code.

```bash
cd app
python -m pip install -U pip pydantic pydantic-settings keyring pyyaml jsonschema sqlalchemy pyarrow duckdb fastapi "uvicorn[standard]" httpx websockets apscheduler boto3 discord.py pytest ruff
python -m pytest -q      # incl. Invariant-1 call-site / no-LLM-in-risk / secrets-grep gates
ruff check .
```

Install the full runtime set in every matrix cell, including `discord.py`. The Discord
command-tree tests run offline with the real dependency; a separate isolated subprocess
forces its import to fail and covers the fallback without skipping either path.

## Where things are
- **Core engines (solid — change with care + adversarial review):** `app/risk/`,
  `app/brokers/` (gateway, state machine, capability), `app/orchestrator/`,
  `app/storage/audit.py`.
- **Data feeds:** `app/ingest/`. **LLM pipelines:** `app/workflows/`.
- **Operator surfaces:** `app/bridge/` (Discord) and `app/orchestrator/app.py` (loopback API).
  A general `app/surfaces/` abstraction is not merged.
- **Design corpus:** `docs/` — index at `docs/README.md`. Current Cloud process:
  `docs/research/agent-swarm-rebase-2026-09-14.md`.

## Review loop

Devin reviews → Claude fixes (`claude.yml`) → stuck threads are picked up by
[`.github/workflows/stuck-thread-watch.yml`](.github/workflows/stuck-thread-watch.yml),
which asks Codex to mediate (`@codex review`) and records one Linear Triage
`needs-decision` roll-up per unapproved PR (`Stuck PR threads: <repo>#<PR>`),
updated in place. Shared keys with the Codex CI/fixer gate:
`<!-- stuck-pr-threads:owner/repo#N -->` and
`<!-- codex-gate-mediate:owner/repo:N -->`. Adopt an existing roll-up only by
the `stuck-pr-threads` HTML marker, never by title. Write Linear only when at
least one thread is actionable after the per-PR cap and `CODEX_TRIGGER_PAT`
checks, or to create a missing roll-up after an earlier Linear outage. Humans
decide. The watcher never resolves threads, pushes, or merges.
