"""Behavioral candidate-coordinator tests. All APIs are in-memory; no dispatches.

Run: python -X utf8 -B -m unittest tools.test_candidate_gate -v
The candidate resolver, READY verifier and full-run verifier remain real. Only
the preexisting receipt subsystem is faked at its established API boundary.
"""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import re
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/ci"))
try:
    SPEC = importlib.util.spec_from_file_location("candidate_gate_under_test", ROOT / "scripts/ci/candidate_gate.py")
    gate = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(gate)
    import codex_gate as legacy_gate
finally:
    sys.path.pop(0)

REPO = "acme/example"
PREFIX = f"repos/{REPO}"
PR = 42
HEAD, BASE, MERGE, TREE = (x * 40 for x in "abcd")
ACTOR = {"id": 17, "login": "dispatch-owner"}
ACTION_USER = {"id": 41898282, "login": "github-actions[bot]", "type": "Bot"}
# Trusted policy identity recorded by the gate job: the default-branch commit
# it checked out. With the fixture PR based on main, that commit is BASE.
POLICY = gate.candidate_api.Policy(BASE, "main")
MOVED = "e" * 40
# Later default-branch commits: the policy that supersedes POLICY, and the next.
NEW_POLICY = gate.candidate_api.Policy(MOVED, "main")
THIRD_POLICY = gate.candidate_api.Policy("9" * 40, "main")


class ApiError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}")


class FakeReceipts:
    ACTIONS_ID = ACTION_USER["id"]

    def __init__(self):
        self.fixer_pending = False
        self.label_pending = False
        self.manual_removal = False
        self.pause = 0
        self.revoke_owned_ci = Mock(return_value="nothing-owned")
        self.readiness_blocked = Mock(side_effect=lambda *args: self.fixer_pending)
        self.pending_ci_intent = Mock(side_effect=lambda *args: self.label_pending)
        self.ci_label_decision = Mock(side_effect=lambda *args:
            "preserve-human-removal" if self.manual_removal else "allow")
        self.latest_pause_id = Mock(side_effect=lambda *args: self.pause)

    @staticmethod
    def bot(user, login, identifier):
        return (user.get("id"), user.get("login"), user.get("type")) == (identifier, login, "Bot")

    @staticmethod
    def marker(kind, data):
        return f"<!-- {kind}:{json.dumps(data, sort_keys=True)} -->"

    @staticmethod
    def records(body, kind):
        return [json.loads(x) for x in re.findall(r"<!-- " + re.escape(kind) + r":(.*?) -->", body)]

    @staticmethod
    def known_label_rejection(error):
        status = getattr(error, "status", None)
        return status if status in {400, 401, 403, 404, 409, 422, 429} else None

    @staticmethod
    def label_event_id(events):
        return next((x["id"] for x in reversed(events) if x.get("event") == "labeled"), None)

    def begin_ci_intent(self, repo, number, head, events, post):
        self.label_pending = True
        return {"nonce": "9" * 32}

    def record_ci_ownership(self, *args, **kwargs):
        self.label_pending = False


class FakeGitHub:
    write_token = "fake-trigger-token"
    read_token = "read-token"

    def __init__(self):
        self.pr = {"id": 200, "number": PR, "state": "open", "draft": False,
                   "mergeable": True, "merge_commit_sha": MERGE, "labels": [],
                   "head": {"ref": "feature/owned", "sha": HEAD,
                            "repo": {"id": 10, "full_name": REPO}},
                   "base": {"ref": "main", "sha": BASE,
                            "repo": {"id": 10, "full_name": REPO}}}
        self.commit = {"sha": MERGE, "parents": [{"sha": BASE}, {"sha": HEAD}], "tree": {"sha": TREE}}
        self.comments, self.reviews, self.events = [], [], []
        self.workflow_ids = {"ci": 301, "rust": 302}
        self.workflow_paths = dict(gate.KINDS)
        self.run_lists = {301: [], 302: []}
        self.jobs, self.latest_runs = {}, {}
        self.dispatches, self.mentions, self.label_writes = [], [], []
        self.outcomes = {"ci": [], "rust": []}
        self.next_id = 100
        self.on_record = self.on_dispatch = self.on_pr_get = None
        self.pr_reads = 0
        self.receipt_user = ACTION_USER
        self.live_refs = {}
        self.graphql_node_overrides = {}
        self.graphql_error = None
        self.on_graphql = None
        self.on_merge_get = None
        self.run_queries = []
        # Live repository metadata and default-branch tip, independent of the PR.
        self.repo_id = 10
        self.default_branch = "main"
        self.default_tip = BASE

    def add_comment(self, body, user=None):
        self.next_id += 1
        item = {"id": self.next_id, "body": body, "user": copy.deepcopy(user or ACTION_USER),
                "created_at": "2026-10-16T00:00:00Z", "updated_at": "2026-10-16T00:00:00Z"}
        self.comments.append(item)
        return copy.deepcopy(item)

    @staticmethod
    def page(items, query):
        page = int(parse_qs(query).get("page", ["1"])[0])
        return copy.deepcopy(items[(page - 1) * 100:page * 100])

    def get(self, url):
        parsed = urlsplit(url)
        path = parsed.path
        if path == PREFIX:
            return {"id": self.repo_id, "full_name": REPO, "default_branch": self.default_branch}
        if path == f"{PREFIX}/pulls/{PR}":
            self.pr_reads += 1
            if self.on_pr_get:
                self.on_pr_get(self.pr_reads)
            return copy.deepcopy(self.pr)
        if path.startswith(f"{PREFIX}/git/commits/"):
            if self.on_merge_get:
                self.on_merge_get()
            return copy.deepcopy(self.commit)
        if path.startswith(f"{PREFIX}/git/ref/heads/"):
            ref = path.removeprefix(f"{PREFIX}/git/ref/heads/")
            side = next((side for side in ("base", "head") if (self.pr.get(side) or {}).get("ref") == ref), None)
            if ref in self.live_refs:
                sha = self.live_refs[ref]
            elif side:
                sha = self.pr[side]["sha"]
            elif ref == self.default_branch:
                sha = self.default_tip
            else:
                raise ApiError(404)
            return {"ref": f"refs/heads/{ref}", "object": {"type": "commit", "sha": sha}}
        listing = {f"{PREFIX}/issues/{PR}/comments": self.comments,
                   f"{PREFIX}/issues/{PR}/events": self.events,
                   f"{PREFIX}/pulls/{PR}/reviews": self.reviews}
        if path in listing:
            return self.page(listing[path], parsed.query)
        raise AssertionError(f"Unexpected read: {url}")

    def post_read_token(self, path, payload):
        if path.endswith("/comments"):
            result = self.add_comment(payload["body"], self.receipt_user)
            if self.on_record:
                self.on_record(payload["body"])
            return result
        if path.endswith("/labels"):
            self.label_writes.append(copy.deepcopy(payload))
            self.pr["labels"] = [{"name": "ci:full"}]
            self.events.append({"id": 700, "event": "labeled", "label": {"name": "ci:full"}})
            return self.pr["labels"]
        raise AssertionError(f"Unexpected read-token POST: {path}")

    def post(self, path, payload):
        if path.endswith("/comments"):
            self.mentions.append(copy.deepcopy(payload))
            return self.add_comment(payload["body"], {"id": 17, "login": "dispatch-owner", "type": "User"})
        match = re.fullmatch(re.escape(PREFIX) + r"/actions/workflows/(\d+)/dispatches", path)
        if not match:
            raise AssertionError(f"Unexpected trigger-token POST: {path}")
        kind = next(k for k, value in self.workflow_ids.items() if value == int(match[1]))
        self.dispatches.append((kind, copy.deepcopy(payload)))
        if self.on_dispatch:
            self.on_dispatch(kind, payload)
        if self.outcomes[kind]:
            raise self.outcomes[kind].pop(0)
        return None

    def _run(self, args, token):
        if args[:2] == ["api", "graphql"]:
            if token != self.read_token:
                raise AssertionError("Review provenance must use read token")
            if self.graphql_error:
                raise self.graphql_error
            identifier = next(value.split("=", 1)[1] for value in args if value.startswith("id="))
            review = next(review for review in self.reviews if review.get("node_id") == identifier)
            node = {"__typename": "PullRequestReview", "fullDatabaseId": review["id"],
                    "includesCreatedEdit": False, "lastEditedAt": None}
            node.update(self.graphql_node_overrides)
            if self.on_graphql:
                self.on_graphql()
            return json.dumps({"data": {"node": node}})
        if token != self.write_token or args[0] != "api":
            raise AssertionError("Unexpected actions read authority")
        parsed = urlsplit(args[1])
        path = parsed.path
        if path == "user":
            return json.dumps(ACTOR)
        match = re.fullmatch(re.escape(PREFIX) + r"/actions/workflows/(ci|rust)\.yml", path)
        if match:
            kind = match[1]
            return json.dumps({"id": self.workflow_ids[kind], "path": self.workflow_paths[kind], "state": "active"})
        match = re.fullmatch(re.escape(PREFIX) + r"/actions/workflows/(\d+)/runs", path)
        if match:
            self.run_queries.append(parse_qs(parsed.query))
            head = parse_qs(parsed.query).get("head_sha", [None])[0]
            runs = [run for run in self.run_lists[int(match[1])] if head is None or run.get("head_sha") == head]
            return json.dumps({"workflow_runs": self.page(runs, parsed.query)})
        match = re.fullmatch(re.escape(PREFIX) + r"/actions/runs/(\d+)/attempts/(\d+)/jobs", path)
        if match:
            return json.dumps({"jobs": self.page(self.jobs[(int(match[1]), int(match[2]))], parsed.query)})
        match = re.fullmatch(re.escape(PREFIX) + r"/actions/runs/(\d+)", path)
        if match:
            return json.dumps(self.latest_runs[int(match[1])])
        raise AssertionError(f"Unexpected actions API: {args}")

    def drift(self, side):
        self.pr[side]["sha"] = "e" * 40
        self.pr["merge_commit_sha"] = "f" * 40
        self.commit = {"sha": "f" * 40, "parents": [{"sha": self.pr["base"]["sha"]},
                       {"sha": self.pr["head"]["sha"]}], "tree": {"sha": "1" * 40}}


class CandidateCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.receipts = FakeReceipts()
        self.legacy = SimpleNamespace(FIXER_RECEIPTS=self.receipts,
            SAFE_REPO_RE=re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"),
            safe_pr=lambda value: re.fullmatch(r"[1-9][0-9]*", str(value)) is not None,
            _act=Mock(return_value=["mediation-enqueued"]),
            trusted_comments=Mock(return_value=[]), marker_authors=Mock(return_value={"trusted"}),
            action_for_sha=Mock(return_value=""), ready_comments=lambda comments, head: comments,
            utc_day=Mock(return_value="2026-10-16"), pr_is_approved=legacy_gate.pr_is_approved)
        self.coordinator = gate.Coordinator(self.legacy, self.gh, REPO, PR, POLICY)

    def reconcile(self, policy=POLICY):
        return gate.run(self.legacy, self.gh, REPO, PR, policy=policy)

    def retarget_base(self, ref="release"):
        """PR based on a non-default branch: policy movement is then independent of the candidate."""
        self.gh.pr["base"]["ref"] = ref

    def journal(self, kind):
        return self.coordinator.journal(self.gh.comments, kind)

    def candidate(self):
        return gate.candidate_api.resolve_candidate(self.gh.get, REPO, PR)

    def prime_ready(self):
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        request = self.journal("request")[-1]
        self.gh.reviews.append({"id": 5000 + len(self.gh.reviews), "state": "COMMENTED",
            "node_id": f"PRR_test_{5000 + len(self.gh.reviews)}",
            "user": {"id": gate.candidate_api.CODEX_ID, "login": gate.candidate_api.CODEX_LOGIN, "type": "Bot"},
            "commit_id": self.candidate().head, "submitted_at": "2026-10-16T00:01:00Z",
            "body": f"CODEX-CANDIDATE: {request['candidate']}\nCODEX-REQUEST: {request['nonce']}\nCODEX-GATE: READY"})
        return request

    def prime_head_mediate(self):
        review = {"id": 5000, "node_id": "PRR_test_5000", "state": "COMMENTED",
            "user": {"id": gate.candidate_api.CODEX_ID, "login": gate.candidate_api.CODEX_LOGIN, "type": "Bot"},
            "commit_id": self.gh.pr["head"]["sha"], "submitted_at": "2026-10-16T00:01:00Z",
            "body": "A human decision is needed.\nCODEX-GATE: MEDIATE"}
        self.gh.reviews.append(review)
        return review

    def dispatch(self):
        self.prime_ready()
        result = json.loads(self.reconcile())
        self.assertFalse(result["qualified"])
        self.assertEqual([kind for kind, _ in self.gh.dispatches], ["ci", "rust"])
        return result

    def observe_success(self, policy=POLICY, run_base=9000):
        """Publish one complete successful run for every reservation pinned to policy."""
        candidate = self.candidate()
        for intent in self.journal("dispatch"):
            if (intent.get("policy"), intent.get("policy_branch")) != (policy.sha, policy.branch):
                continue
            kind = intent["kind"]
            identifier = run_base + self.gh.workflow_ids[kind]
            # Candidate runs execute on the default branch at the policy commit.
            run = {"id": identifier, "head_sha": policy.sha, "head_branch": policy.branch,
                "workflow_id": intent["workflow"], "path": gate.KINDS[kind], "event": "workflow_dispatch",
                "repository": {"id": candidate.repository_id, "full_name": REPO}, "actor": ACTOR,
                "head_repository": {"id": candidate.repository_id, "full_name": REPO},
                "display_title": f"candidate:{candidate.key}:{kind}:{intent['nonce']}",
                "run_attempt": 1, "status": "completed", "conclusion": "success"}
            others = [x for x in self.gh.run_lists[intent["workflow"]] if x["id"] != identifier]
            self.gh.run_lists[intent["workflow"]] = others + [run]
            self.gh.latest_runs[identifier] = copy.deepcopy(run)
            self.gh.jobs[(identifier, 1)] = [{"id": identifier * 100 + i, "run_id": identifier,
                "run_attempt": 1, "head_sha": policy.sha, "name": name,
                "status": "completed", "conclusion": "success"}
                for i, name in enumerate(sorted(gate.candidate_api.REQUIRED_JOBS[kind]), 1)]
            self.assertTrue(gate.candidate_api.full_run_success(
                run, self.gh.jobs[(identifier, 1)], candidate, kind, intent["workflow"], intent["nonce"], policy))

    def supersede(self):
        """Deliver and observe a qualified matrix under POLICY, then advance the default branch.

        The PR targets a non-default branch so the candidate is unchanged while
        the trusted policy commit moves from BASE to MOVED.
        """
        self.retarget_base()
        self.dispatch()
        self.observe_success()
        self.assertTrue(json.loads(self.reconcile())["qualified"])
        self.gh.default_tip = NEW_POLICY.sha
        self.gh.run_queries = []
        return {x["kind"]: x for x in self.journal("dispatch")}

    def repin(self, kind, **fields):
        """Rewrite the authenticated dispatch record for kind in place (journal provenance is caller-controlled)."""
        intent = next(x for x in self.journal("dispatch") if x["kind"] == kind)
        comment = next(c for c in self.gh.comments if c["id"] == intent["id"])
        data = {k: v for k, v in intent.items() if k not in ("id", "created_at")}
        data.update(fields)
        data = {k: v for k, v in data.items() if v is not None}
        comment["body"] = self.receipts.marker("candidate-dispatch", data)
        return data

    def dispatched(self, kind):
        return [k for k, _ in self.gh.dispatches].count(kind)

    def queried_shas(self):
        return {query["head_sha"][0] for query in self.gh.run_queries}

    def test_ready_exact_candidate_dispatches_two_full_workflows_once(self):
        self.dispatch()
        for _, payload in self.gh.dispatches:
            # Dispatch on the default branch, never on the candidate head ref.
            self.assertEqual(payload["ref"], POLICY.branch)
            self.assertNotEqual(payload["ref"], self.candidate().head_ref)
            self.assertEqual(payload["inputs"], {**self.candidate().fields(), "scope": "full",
                "dispatch_nonce": payload["inputs"]["dispatch_nonce"]})
        for intent in self.journal("dispatch"):
            self.assertEqual((intent["policy"], intent["policy_branch"]), (POLICY.sha, POLICY.branch))
            self.assertEqual((intent["workflow"], intent["actor"]), (self.gh.workflow_ids[intent["kind"]], ACTOR["id"]))
        for _ in range(3):
            result = json.loads(self.reconcile())
            self.assertFalse(result["qualified"])
            self.assertEqual(result["policy"], POLICY.sha)
        self.assertEqual(len(self.gh.dispatches), 2)
        self.assertEqual(self.gh.label_writes, [])
        self.assertEqual(self.gh.pr["labels"], [])

    def test_coordinator_refuses_without_a_trusted_policy_identity(self):
        self.prime_ready()
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            self.reconcile(policy=None)
        for env in ({"CODEX_POLICY_SHA": BASE}, {"CODEX_POLICY_BRANCH": "main"},
                    {"CODEX_POLICY_SHA": "short", "CODEX_POLICY_BRANCH": "main"},
                    {"CODEX_POLICY_SHA": BASE, "CODEX_POLICY_BRANCH": "-x"},
                    {"CODEX_POLICY_SHA": "", "CODEX_POLICY_BRANCH": ""}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                gate.policy_from_env(env)
        for bad in ("main", {"sha": BASE, "branch": "main"}, (BASE, "main")):
            with self.subTest(policy=bad), self.assertRaises(ValueError):
                gate.Coordinator(self.legacy, self.gh, REPO, PR, bad)
        self.assertEqual(gate.policy_from_env({"CODEX_POLICY_SHA": BASE, "CODEX_POLICY_BRANCH": "main"}), POLICY)
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(self.journal("dispatch"), [])

    def test_default_branch_movement_or_rename_before_reservation_refuses(self):
        for change in ("tip", "rename"):
            with self.subTest(change=change):
                self.setUp()
                self.retarget_base()
                self.prime_ready()
                if change == "tip":
                    self.gh.default_tip = MOVED
                else:
                    self.gh.default_branch = "trunk"
                with self.assertRaises(ValueError):
                    self.reconcile()
                self.assertEqual(self.journal("dispatch"), [])
                self.assertEqual(self.gh.dispatches, [])

    def test_default_branch_movement_after_reservation_aborts_before_dispatch(self):
        self.retarget_base()
        self.prime_ready()
        self.gh.on_record = lambda body: setattr(self.gh, "default_tip", MOVED) if "candidate-dispatch:" in body else None
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(len(self.journal("dispatch")), 1)
        self.assertEqual(self.journal("aborted")[0]["reason"], "pre-dispatch-refused")
        # The superseded policy never retries: every later reconciliation fails closed.
        self.gh.on_record = None
        for _ in range(3):
            with self.assertRaises(ValueError):
                self.reconcile()
        self.assertEqual(self.gh.dispatches, [])

    def test_default_branch_movement_after_dispatch_refuses_qualification(self):
        self.retarget_base()
        self.dispatch()
        self.observe_success()
        self.assertTrue(json.loads(self.reconcile())["qualified"])
        self.gh.default_tip = MOVED
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(len(self.gh.dispatches), 2)

    def test_policy_naming_candidate_head_or_default_head_ref_never_reserves(self):
        self.retarget_base()
        self.prime_ready()
        self.gh.default_tip = HEAD
        with self.assertRaises(ValueError):
            self.reconcile(policy=gate.candidate_api.Policy(HEAD, "main"))
        # A "default branch" that is the candidate head ref is refused even when
        # the live repository metadata and ref agree with that policy.
        self.gh.default_branch = "feature/owned"
        with self.assertRaises(ValueError):
            self.reconcile(policy=gate.candidate_api.Policy(HEAD, "feature/owned"))
        self.assertEqual(self.journal("dispatch"), [])
        self.assertEqual(self.gh.dispatches, [])

    def test_legacy_or_foreign_policy_reservation_never_qualifies_or_redispatches(self):
        for pinned in ({}, {"policy": MOVED, "policy_branch": "main"}, {"policy": BASE, "policy_branch": "trunk"},
                       {"policy": BASE}, {"policy_branch": "main"}, {"policy": HEAD, "policy_branch": "main"}):
            with self.subTest(pinned=pinned):
                self.setUp()
                self.prime_ready()
                nonce = "3" * 32
                self.gh.add_comment(self.receipts.marker("candidate-dispatch", {
                    "candidate": self.candidate().key, "kind": "ci", "nonce": nonce,
                    "workflow": 301, "actor": ACTOR["id"], "before": 0, **pinned}))
                # Even a complete successful run bearing that nonce on the current
                # policy commit cannot be adopted by a reservation that did not pin it.
                run = {"id": 9301, "head_sha": POLICY.sha, "head_branch": POLICY.branch, "workflow_id": 301,
                    "path": gate.KINDS["ci"], "event": "workflow_dispatch", "actor": ACTOR,
                    "repository": {"id": 10, "full_name": REPO}, "head_repository": {"id": 10, "full_name": REPO},
                    "display_title": f"candidate:{self.candidate().key}:ci:{nonce}",
                    "run_attempt": 1, "status": "completed", "conclusion": "success"}
                self.gh.run_lists[301] = [run]
                self.gh.latest_runs[9301] = copy.deepcopy(run)
                self.gh.jobs[(9301, 1)] = [{"id": 1000 + i, "run_id": 9301, "run_attempt": 1, "head_sha": POLICY.sha,
                    "name": name, "status": "completed", "conclusion": "success"}
                    for i, name in enumerate(sorted(gate.candidate_api.REQUIRED_JOBS["ci"]), 1)]
                for _ in range(3):
                    result = json.loads(self.reconcile())
                    self.assertFalse(result["qualified"])
                    self.assertEqual(result["full_matrix"]["ci"], "policy-mismatch-reconcile")
                self.assertEqual([kind for kind, _ in self.gh.dispatches], ["rust"])
                self.assertEqual(len(self.journal("dispatch")), 2)

    def test_resolved_superseded_delivery_permits_exactly_one_fresh_current_reservation(self):
        old = self.supersede()
        result = json.loads(self.reconcile(policy=NEW_POLICY))
        # The old success released its reservation only; it is not current success.
        self.assertFalse(result["qualified"])
        self.assertEqual(result["policy"], NEW_POLICY.sha)
        self.assertEqual(result["full_matrix"], {"ci": "dispatched-awaiting-observation",
                                                 "rust": "dispatched-awaiting-observation"})
        self.assertEqual([kind for kind, _ in self.gh.dispatches], ["ci", "rust", "ci", "rust"])
        # The superseded reservation was actually looked up by its own pinned SHA.
        self.assertEqual(self.queried_shas(), {POLICY.sha, NEW_POLICY.sha})
        fresh = [x for x in self.journal("dispatch") if x["id"] > max(v["id"] for v in old.values())]
        self.assertEqual(len(fresh), 2)
        for intent in fresh:
            self.assertEqual((intent["policy"], intent["policy_branch"]), (NEW_POLICY.sha, NEW_POLICY.branch))
            self.assertNotEqual(intent["nonce"], old[intent["kind"]]["nonce"])
        for _, payload in self.gh.dispatches[2:]:
            self.assertEqual(payload["ref"], NEW_POLICY.branch)
            self.assertIn(payload["inputs"]["dispatch_nonce"], {x["nonce"] for x in fresh})
        # Exactly one fresh reservation: later reconciliations wait, never resend.
        for _ in range(3):
            result = json.loads(self.reconcile(policy=NEW_POLICY))
            self.assertFalse(result["qualified"])
            self.assertEqual(result["full_matrix"], {"ci": "reserved-delivery-uncertain",
                                                     "rust": "reserved-delivery-uncertain"})
        self.assertEqual(len(self.gh.dispatches), 4)
        self.assertEqual(len(self.journal("dispatch")), 4)
        self.assertEqual(self.journal("aborted"), [])
        # Only the current policy's own nonce-bound run qualifies.
        self.observe_success(NEW_POLICY, run_base=9500)
        result = json.loads(self.reconcile(policy=NEW_POLICY))
        self.assertTrue(result["qualified"])
        self.assertEqual(result["policy"], NEW_POLICY.sha)
        self.assertEqual(len(self.gh.dispatches), 4)
        # The superseded policy itself still fails closed against the live branch.
        with self.assertRaises(ValueError):
            self.reconcile(policy=POLICY)

    def test_superseded_success_never_qualifies_without_a_current_policy_run(self):
        self.supersede()
        # Exhaust the shared budget with pre-dispatch aborts so no current
        # delivery can happen; the resolved old success must not fill the gap.
        for _ in range(2):
            self.receipts.fixer_pending = False
            self.gh.on_record = lambda body: setattr(self.receipts, "fixer_pending", True) if "candidate-dispatch:" in body else None
            with self.assertRaises(ValueError):
                self.reconcile(policy=NEW_POLICY)
        self.receipts.fixer_pending = False
        self.gh.on_record = None
        result = json.loads(self.reconcile(policy=NEW_POLICY))
        self.assertFalse(result["qualified"])
        self.assertEqual(result["full_matrix"]["ci"], "rejection-budget-exhausted")
        self.assertNotIn("success", result["full_matrix"].values())
        self.assertEqual(self.dispatched("ci"), 1)

    def test_superseded_evidence_defects_keep_the_reservation_held(self):
        def rerun():
            for store in (self.gh.run_lists[301][0], self.gh.latest_runs[9301]):
                store["run_attempt"] = 2
            self.gh.jobs[(9301, 2)] = [{**job, "run_attempt": 2} for job in self.gh.jobs[(9301, 1)]]

        def duplicate():
            twin = copy.deepcopy(self.gh.run_lists[301][0])
            twin["id"] += 100
            self.gh.run_lists[301].append(twin)

        def both(**fields):
            def apply():
                self.gh.run_lists[301][0].update(copy.deepcopy(fields))
                self.gh.latest_runs[9301].update(copy.deepcopy(fields))
            return apply

        def changing_jobs():
            original_run = self.gh._run
            reads = 0

            def wrapped(args, token):
                nonlocal reads
                if "/actions/runs/9301/attempts/1/jobs" in args[1]:
                    reads += 1
                    if reads == 2:
                        self.gh.jobs[(9301, 1)][0]["conclusion"] = "failure"
                return original_run(args, token)
            self.gh._run = wrapped

        defects = {
            "failure": both(conclusion="failure"),
            "cancelled": both(conclusion="cancelled"),
            "pending": both(status="in_progress", conclusion=None),
            "missing-job": lambda: self.gh.jobs[(9301, 1)].pop(),
            "failed-job": lambda: self.gh.jobs[(9301, 1)][0].update(conclusion="failure"),
            "missing-run": lambda: self.gh.run_lists.__setitem__(301, []),
            "run-not-after-before": lambda: self.repin("ci", before=9301),
            "duplicate-title": duplicate,
            "wrong-actor": both(actor={"id": 999}),
            "wrong-workflow": both(workflow_id=302),
            "wrong-path": both(path=gate.KINDS["rust"]),
            "wrong-branch": both(head_branch="release"),
            "wrong-repo": both(repository={"id": 10, "full_name": "fork/repo"}),
            "wrong-event": both(event="pull_request"),
            "rerun-attempt": rerun,
            "run-changed-between-reads": lambda: self.gh.latest_runs[9301].update(updated_at="later"),
            "attempt-bumped-between-reads": lambda: self.gh.latest_runs[9301].update(run_attempt=2),
            "jobs-changed-between-reads": changing_jobs,
        }
        for name, defect in defects.items():
            with self.subTest(defect=name):
                self.setUp()
                self.supersede()
                defect()
                # The rust reservation, whose old evidence is intact, is released
                # exactly once; the defective ci reservation stays held throughout.
                for expected_rust in ("dispatched-awaiting-observation", "reserved-delivery-uncertain"):
                    result = json.loads(self.reconcile(policy=NEW_POLICY))
                    self.assertFalse(result["qualified"])
                    self.assertEqual(result["full_matrix"]["ci"], "policy-mismatch-reconcile")
                    self.assertEqual(result["full_matrix"]["rust"], expected_rust)
                self.assertIn(POLICY.sha, self.queried_shas())
                self.assertEqual(self.dispatched("ci"), 1)
                self.assertEqual(self.dispatched("rust"), 2)
                self.assertEqual(len([x for x in self.journal("dispatch") if x["kind"] == "ci"]), 1)

    def test_superseded_policy_binding_defects_keep_the_reservation_held(self):
        pins = {"missing-branch": {"policy_branch": None}, "missing-sha": {"policy": None},
                "invalid-sha": {"policy": "short"}, "invalid-branch": {"policy_branch": "-x"},
                "foreign-branch": {"policy_branch": "trunk"}, "candidate-head": {"policy": HEAD},
                "candidate-merge": {"policy": MERGE}, "legacy": {"policy": None, "policy_branch": None}}
        for name, pin in pins.items():
            with self.subTest(pin=name):
                self.setUp()
                self.supersede()
                self.repin("ci", **pin)
                for _ in range(2):
                    result = json.loads(self.reconcile(policy=NEW_POLICY))
                    self.assertFalse(result["qualified"])
                    self.assertEqual(result["full_matrix"]["ci"], "policy-mismatch-reconcile")
                self.assertEqual(self.dispatched("ci"), 1)
                self.assertEqual(self.dispatched("rust"), 2)

    def test_superseded_lookup_is_isolated_to_its_pinned_sha(self):
        self.supersede()
        # The same nonce-bearing successful run relocated to the current policy
        # commit is not that reservation's evidence, and the old SHA is empty.
        run = self.gh.run_lists[301][0]
        run["head_sha"] = NEW_POLICY.sha
        self.gh.latest_runs[run["id"]]["head_sha"] = NEW_POLICY.sha
        for job in self.gh.jobs[(run["id"], 1)]:
            job["head_sha"] = NEW_POLICY.sha
        result = json.loads(self.reconcile(policy=NEW_POLICY))
        self.assertEqual(result["full_matrix"]["ci"], "policy-mismatch-reconcile")
        self.assertEqual(self.dispatched("ci"), 1)
        self.assertEqual(self.queried_shas(), {POLICY.sha, NEW_POLICY.sha})
        ci_queries = [q for q in self.gh.run_queries if q["head_sha"] == [POLICY.sha]]
        self.assertTrue(ci_queries)
        for query in self.gh.run_queries:
            self.assertEqual(query["event"], ["workflow_dispatch"])
        # Conversely, a run on the old SHA bearing the new nonce never serves the
        # current reservation, which only ever queries its own policy commit.
        new_intent = next(x for x in self.journal("dispatch") if x["kind"] == "rust" and x["policy"] == NEW_POLICY.sha)
        stray = copy.deepcopy(self.gh.run_lists[302][0])
        stray.update(id=9777, display_title=f"candidate:{self.candidate().key}:rust:{new_intent['nonce']}")
        self.gh.run_lists[302].append(stray)
        self.gh.latest_runs[9777] = copy.deepcopy(stray)
        self.gh.jobs[(9777, 1)] = copy.deepcopy(self.gh.jobs[(9302, 1)])
        result = json.loads(self.reconcile(policy=NEW_POLICY))
        self.assertEqual(result["full_matrix"]["rust"], "reserved-delivery-uncertain")
        self.assertFalse(result["qualified"])
        self.assertEqual(len(self.gh.dispatches), 3)

    def test_bounded_or_malformed_superseded_listing_never_releases_the_reservation(self):
        for shape in ("unbounded", "non-list", "non-dict-items", "duplicate-ids", "not-json"):
            with self.subTest(shape=shape):
                self.setUp()
                self.supersede()
                original_run = self.gh._run
                if shape == "unbounded":
                    self.gh.run_lists[301] = [{"id": index + 1, "head_sha": POLICY.sha} for index in range(1100)]
                elif shape == "duplicate-ids":
                    twin = copy.deepcopy(self.gh.run_lists[301][0])
                    twin["display_title"] = "other"
                    self.gh.run_lists[301].append(twin)
                else:
                    def malformed(args, token, shape=shape):
                        if "/actions/workflows/301/runs" in args[1] and f"head_sha={POLICY.sha}" in args[1]:
                            if shape == "not-json":
                                return "<html>"
                            return json.dumps({"workflow_runs": "bad" if shape == "non-list" else [1, 2]})
                        return original_run(args, token)
                    self.gh._run = malformed
                result = json.loads(self.reconcile(policy=NEW_POLICY))
                self.assertFalse(result["qualified"])
                self.assertEqual(result["full_matrix"]["ci"], "policy-mismatch-reconcile")
                self.assertEqual(self.dispatched("ci"), 1)
                self.assertEqual(self.dispatched("rust"), 2)

    def test_hold_review_or_pause_change_around_superseded_release_stops_before_dispatch(self):
        for mutation in ("hold", "pause", "review", "fixer", "policy"):
            with self.subTest(mutation=mutation, when="before"):
                self.setUp()
                self.supersede()
                if mutation == "hold":
                    self.gh.pr["labels"] = [{"name": "hold"}]
                    self.assertEqual(self.reconcile(policy=NEW_POLICY), "held-or-fixer-pending")
                elif mutation == "pause":
                    self.receipts.pause += 1
                    self.assertEqual(self.reconcile(policy=NEW_POLICY), "awaiting-independent-candidate-review")
                elif mutation == "review":
                    self.gh.reviews[-1]["body"] = self.gh.reviews[-1]["body"].replace("CODEX-GATE: READY", "CODEX-GATE: MEDIATE")
                    self.assertEqual(self.reconcile(policy=NEW_POLICY), "mediation-enqueued")
                elif mutation == "fixer":
                    self.receipts.fixer_pending = True
                    self.assertEqual(self.reconcile(policy=NEW_POLICY), "held-or-fixer-pending")
                else:
                    self.gh.default_tip = THIRD_POLICY.sha
                    with self.assertRaises(ValueError):
                        self.reconcile(policy=NEW_POLICY)
                self.assertEqual(len(self.gh.dispatches), 2)
                self.assertEqual(len(self.journal("dispatch")), 2)
            if mutation == "review":
                continue
            with self.subTest(mutation=mutation, when="after-reservation"):
                self.setUp()
                self.supersede()

                def mutate(body, mutation=mutation):
                    if "candidate-dispatch:" not in body:
                        return
                    if mutation == "hold":
                        self.gh.pr["labels"] = [{"name": "hold"}]
                    elif mutation == "pause":
                        self.receipts.pause += 1
                    elif mutation == "fixer":
                        self.receipts.fixer_pending = True
                    else:
                        self.gh.default_tip = THIRD_POLICY.sha
                self.gh.on_record = mutate
                with self.assertRaises(ValueError):
                    self.reconcile(policy=NEW_POLICY)
                self.assertEqual(len(self.gh.dispatches), 2)
                self.assertEqual(len(self.journal("dispatch")), 3)
                self.assertEqual(self.journal("aborted")[0]["reason"], "pre-dispatch-refused")

    def test_superseded_evidence_vanishing_between_reservation_and_post_aborts(self):
        self.supersede()
        self.gh.on_record = lambda body: self.gh.run_lists.__setitem__(301, []) if "candidate-dispatch:" in body else None
        with self.assertRaises(ValueError):
            self.reconcile(policy=NEW_POLICY)
        self.assertEqual(len(self.gh.dispatches), 2)
        self.assertEqual(len(self.journal("dispatch")), 3)
        self.assertEqual(len(self.journal("aborted")), 1)

    def test_superseded_evidence_deletion_after_newer_reservation_fails_closed(self):
        for loss in ("deleted", "failed", "rerun"):
            with self.subTest(loss=loss):
                self.setUp()
                self.supersede()
                self.assertFalse(json.loads(self.reconcile(policy=NEW_POLICY))["qualified"])
                self.assertEqual(len(self.gh.dispatches), 4)
                self.observe_success(NEW_POLICY, run_base=9500)
                self.assertTrue(json.loads(self.reconcile(policy=NEW_POLICY))["qualified"])
                old = next(x for x in self.gh.run_lists[301] if x["head_sha"] == POLICY.sha)
                if loss == "deleted":
                    self.gh.run_lists[301].remove(old)
                elif loss == "failed":
                    old["conclusion"] = "failure"
                    self.gh.latest_runs[old["id"]]["conclusion"] = "failure"
                else:
                    old["run_attempt"] = 2
                    self.gh.latest_runs[old["id"]]["run_attempt"] = 2
                    self.gh.jobs[(old["id"], 2)] = [{**job, "run_attempt": 2} for job in self.gh.jobs[(old["id"], 1)]]
                for _ in range(2):
                    with self.assertRaises(ValueError):
                        self.reconcile(policy=NEW_POLICY)
                self.assertEqual(len(self.gh.dispatches), 4)
                self.assertEqual(len(self.journal("dispatch")), 4)

    def test_unresolved_superseded_duplicates_always_block(self):
        self.supersede()
        old = next(x for x in self.journal("dispatch") if x["kind"] == "ci")
        # A second authenticated old-policy reservation with no run of its own.
        self.gh.add_comment(self.receipts.marker("candidate-dispatch", {
            "candidate": old["candidate"], "kind": "ci", "nonce": "3" * 32, "workflow": 301,
            "actor": ACTOR["id"], "before": 0, "policy": POLICY.sha, "policy_branch": POLICY.branch}))
        # One resolved plus one unresolved old: held, not released.
        result = json.loads(self.reconcile(policy=NEW_POLICY))
        self.assertEqual(result["full_matrix"]["ci"], "policy-mismatch-reconcile")
        self.assertEqual(self.dispatched("ci"), 1)
        # Two unresolved olds: refuse outright.
        self.gh.run_lists[301] = []
        with self.assertRaises(ValueError):
            self.reconcile(policy=NEW_POLICY)
        self.assertEqual(self.dispatched("ci"), 1)

    def test_malformed_superseded_nonce_is_held_not_released(self):
        # A superseded reservation whose nonce is missing, wrong-typed or not a
        # 32-hex string cannot name a run title; it is refused as evidence and
        # stays held rather than crashing or being released.
        for name, fields in (("missing", {}), ("wrong-type", {"nonce": 3}), ("list", {"nonce": ["3" * 32]}),
                             ("short", {"nonce": "3" * 31}), ("uppercase", {"nonce": "A" * 32}),
                             ("newline", {"nonce": "3" * 32 + "\n"})):
            with self.subTest(nonce=name):
                self.setUp()
                self.supersede()
                old = next(x for x in self.journal("dispatch") if x["kind"] == "ci")
                self.gh.add_comment(self.receipts.marker("candidate-dispatch", {
                    "candidate": old["candidate"], "kind": "ci", "workflow": 301, "actor": ACTOR["id"],
                    "before": 0, "policy": POLICY.sha, "policy_branch": POLICY.branch, **fields}))
                ci_intents = [x for x in self.journal("dispatch") if x["kind"] == "ci"]
                before = {kind: len(self.journal(kind)) for kind in ("rejected", "aborted")}
                for _ in range(2):
                    result = json.loads(self.reconcile(policy=NEW_POLICY))
                    self.assertFalse(result["qualified"])
                    self.assertEqual(result["full_matrix"]["ci"], "policy-mismatch-reconcile")
                # Only the target workflow is held: no new CI reservation across
                # two reconciliations, and the malformed intent is neither
                # discarded nor treated as rejected/aborted. Rust's independent
                # new-policy dispatch is legitimate and not inspected here.
                self.assertEqual(self.dispatched("ci"), 1)
                self.assertEqual([x for x in self.journal("dispatch") if x["kind"] == "ci"], ci_intents)
                malformed = {**fields, "candidate": old["candidate"], "kind": "ci", "workflow": 301,
                             "actor": ACTOR["id"], "before": 0, "policy": POLICY.sha, "policy_branch": POLICY.branch}
                self.assertIn(malformed, [{k: v for k, v in x.items() if k not in ("id", "created_at")}
                                          for x in ci_intents])
                for kind, count in before.items():
                    self.assertEqual(len(self.journal(kind)), count)

    def test_resolved_superseded_reservations_stay_in_the_three_intent_budget(self):
        self.supersede()
        self.assertFalse(json.loads(self.reconcile(policy=NEW_POLICY))["qualified"])
        self.observe_success(NEW_POLICY, run_base=9500)
        self.assertTrue(json.loads(self.reconcile(policy=NEW_POLICY))["qualified"])
        # Second advancement: two resolved historical reservations plus one current.
        self.gh.default_tip = THIRD_POLICY.sha
        self.gh.run_queries = []
        result = json.loads(self.reconcile(policy=THIRD_POLICY))
        self.assertFalse(result["qualified"])
        self.assertEqual(result["full_matrix"], {"ci": "dispatched-awaiting-observation",
                                                 "rust": "dispatched-awaiting-observation"})
        self.assertEqual(self.queried_shas(), {POLICY.sha, NEW_POLICY.sha, THIRD_POLICY.sha})
        self.assertEqual(len(self.gh.dispatches), 6)
        self.assertEqual(len([x for x in self.journal("dispatch") if x["kind"] == "ci"]), 3)
        self.observe_success(THIRD_POLICY, run_base=9600)
        result = json.loads(self.reconcile(policy=THIRD_POLICY))
        self.assertTrue(result["qualified"])
        self.assertEqual(result["policy"], THIRD_POLICY.sha)
        # Third advancement: three resolved reservations exhaust the budget. No
        # fourth dispatch, and no resolved old success stands in for current.
        fourth = gate.candidate_api.Policy("7" * 40, "main")
        self.gh.default_tip = fourth.sha
        for _ in range(2):
            result = json.loads(self.reconcile(policy=fourth))
            self.assertFalse(result["qualified"])
            self.assertEqual(result["full_matrix"], {"ci": "rejection-budget-exhausted",
                                                     "rust": "rejection-budget-exhausted"})
        self.assertEqual(len(self.gh.dispatches), 6)
        self.assertEqual(len(self.journal("dispatch")), 6)

    def test_superseded_release_leaves_no_persistent_record(self):
        before = {kind: len(self.journal(kind)) for kind in ("dispatch", "rejected", "aborted", "request")}
        self.supersede()
        comments = len(self.gh.comments)
        self.assertFalse(json.loads(self.reconcile(policy=NEW_POLICY))["qualified"])
        # Exactly the two fresh dispatch reservations were written; nothing else.
        self.assertEqual(len(self.gh.comments), comments + 2)
        self.assertEqual(len(self.journal("dispatch")), before["dispatch"] + 4)
        for kind in ("rejected", "aborted"):
            self.assertEqual(len(self.journal(kind)), before[kind])
        self.assertEqual(len(self.journal("request")), 1)

    def test_candidate_head_run_is_never_listed_or_qualified(self):
        self.dispatch()
        self.observe_success()
        candidate = self.candidate()
        run = self.gh.run_lists[301][0]
        run.update(head_sha=candidate.head, head_branch=candidate.head_ref)
        self.gh.latest_runs[run["id"]].update(head_sha=candidate.head, head_branch=candidate.head_ref)
        for job in self.gh.jobs[(run["id"], 1)]:
            job["head_sha"] = candidate.head
        intent = next(x for x in self.journal("dispatch") if x["kind"] == "ci")
        self.assertFalse(gate.candidate_api.full_run_success(
            run, self.gh.jobs[(run["id"], 1)], candidate, "ci", 301, intent["nonce"], POLICY))
        result = json.loads(self.reconcile())
        self.assertFalse(result["qualified"])
        self.assertEqual(result["full_matrix"]["ci"], "reserved-delivery-uncertain")
        self.assertEqual(result["full_matrix"]["rust"], "success")
        self.assertEqual(len(self.gh.dispatches), 2)
        for query in self.gh.run_queries:
            self.assertEqual(query["head_sha"], [POLICY.sha])

    def test_policy_run_on_another_branch_or_different_default_tip_is_unbound(self):
        self.dispatch()
        self.observe_success()
        run = self.gh.run_lists[301][0]
        run["head_branch"] = self.candidate().head_ref
        self.gh.latest_runs[run["id"]]["head_branch"] = run["head_branch"]
        with self.assertRaises(ValueError):
            self.reconcile()
        run["head_branch"] = POLICY.branch
        self.gh.latest_runs[run["id"]]["head_branch"] = POLICY.branch
        self.assertTrue(json.loads(self.reconcile())["qualified"])
        # A run on a later default-branch commit is not this policy's run.
        run["head_sha"] = MOVED
        self.gh.latest_runs[run["id"]]["head_sha"] = MOVED
        result = json.loads(self.reconcile())
        self.assertFalse(result["qualified"])
        self.assertEqual(result["full_matrix"]["ci"], "reserved-delivery-uncertain")
        self.assertEqual(len(self.gh.dispatches), 2)

    def test_verified_mediate_uses_existing_mediation_boundary_without_full_dispatch(self):
        self.prime_ready()
        self.receipts.revoke_owned_ci.reset_mock()
        review = self.gh.reviews[-1]
        review["body"] = review["body"].replace("CODEX-GATE: READY", "CODEX-GATE: MEDIATE")
        expected_comments = copy.deepcopy(self.gh.comments)
        self.assertEqual(self.reconcile(), "mediation-enqueued")
        self.legacy._act.assert_called_once_with("MEDIATE", REPO, str(PR), HEAD, [],
            "2026-10-16", self.gh, self.gh.reviews)
        intent_comment = next(comment for comment in self.gh.comments
            if self.receipts.records(comment["body"], "candidate-mediate-intent"))
        self.legacy.trusted_comments.assert_called_with(expected_comments + [intent_comment], {"trusted"})
        self.receipts.revoke_owned_ci.assert_called_once()
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(len(self.gh.mentions), 1)

    def test_successful_mediate_is_recorded_once_across_reconciliations(self):
        self.prime_ready()
        self.gh.reviews[-1]["body"] = self.gh.reviews[-1]["body"].replace(
            "CODEX-GATE: READY", "CODEX-GATE: MEDIATE")
        self.assertEqual(self.reconcile(), "mediation-enqueued")
        for _ in range(3):
            self.assertEqual(self.reconcile(), "mediate-already-recorded")
        self.legacy._act.assert_called_once()
        records = self.journal("mediate")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["head"], HEAD)
        self.assertEqual(records[0]["review"], self.gh.reviews[-1]["id"])
        self.assertEqual(records[0]["pause"], 0)
        self.assertEqual(records[0]["nonce"], self.journal("mediate-intent")[0]["nonce"])
        self.assertEqual(self.gh.dispatches, [])

    def test_mediate_retry_outcome_is_uncertain_and_never_automatically_repeated(self):
        self.prime_ready()
        self.gh.reviews[-1]["body"] = self.gh.reviews[-1]["body"].replace(
            "CODEX-GATE: READY", "CODEX-GATE: MEDIATE")
        self.legacy._act.side_effect = [
            ["codex-gate: pr=42 mediate=retry"], ["mediation-enqueued"]]
        self.assertEqual(self.reconcile(), "codex-gate: pr=42 mediate=retry")
        self.assertEqual(self.journal("mediate"), [])
        for _ in range(3):
            self.assertEqual(self.reconcile(), "mediate-delivery-uncertain")
        self.assertEqual(self.legacy._act.call_count, 1)
        self.assertEqual(len(self.journal("mediate")), 0)
        self.assertEqual(len(self.journal("mediate-intent")), 1)
        self.assertEqual(self.gh.dispatches, [])

    def test_mediate_cannot_bypass_owner_hold_manual_removal_or_pending_label_intent(self):
        for blocker in ("hold", "manual_removal", "label_pending"):
            with self.subTest(blocker=blocker):
                self.setUp()
                self.prime_ready()
                self.gh.reviews[-1]["body"] = self.gh.reviews[-1]["body"].replace(
                    "CODEX-GATE: READY", "CODEX-GATE: MEDIATE")
                if blocker == "hold":
                    self.gh.pr["labels"] = [{"name": "hold"}]
                else:
                    setattr(self.receipts, blocker, True)
                self.assertEqual(self.reconcile(), "held-or-fixer-pending")
                self.legacy._act.assert_not_called()
                self.assertEqual(self.gh.dispatches, [])

    def test_latest_mediate_supersedes_older_ready(self):
        self.prime_ready()
        latest = copy.deepcopy(self.gh.reviews[-1])
        latest.update(id=6000, submitted_at="2026-10-16T00:02:00Z")
        latest["node_id"] = "PRR_test_6000"
        latest["body"] = latest["body"].replace("CODEX-GATE: READY", "CODEX-GATE: MEDIATE")
        self.gh.reviews.append(latest)
        self.assertEqual(self.reconcile(), "mediation-enqueued")
        self.legacy._act.assert_called_once()
        self.assertEqual(self.gh.dispatches, [])

    def human_review(self, state, identifier, submitted_at):
        return {"id": identifier, "user": {"id": 55, "login": "owner", "type": "User"},
                "state": state, "submitted_at": submitted_at}

    def test_mediate_skips_approved_pr_before_any_side_effect(self):
        self.prime_head_mediate()
        self.gh.reviews.append(self.human_review("APPROVED", 8000, "2026-10-16T00:02:00Z"))
        before = copy.deepcopy(self.gh.comments)
        self.assertEqual(self.reconcile(), "mediate-skip-approved")
        self.receipts.revoke_owned_ci.assert_not_called()
        self.legacy._act.assert_not_called()
        self.assertEqual(self.journal("mediate-intent"), [])
        self.assertEqual(self.gh.comments, before)
        self.assertEqual(self.gh.dispatches, [])

    def test_mediate_still_runs_when_latest_human_review_is_not_approval(self):
        for later in ("CHANGES_REQUESTED", "DISMISSED", "COMMENTED"):
            with self.subTest(later=later):
                self.setUp()
                self.prime_head_mediate()
                approval = self.human_review("APPROVED", 8000, "2026-10-16T00:02:00Z")
                self.gh.reviews.append(approval)
                if later == "DISMISSED":
                    # GitHub rewrites the dismissed review's own state.
                    approval["state"] = "DISMISSED"
                else:
                    self.gh.reviews.append(self.human_review(later, 8001, "2026-10-16T00:03:00Z"))
                result = self.reconcile()
                if later == "CHANGES_REQUESTED":
                    # The human hold wins before mediation, as before.
                    self.assertEqual(result, "held-or-fixer-pending")
                    self.legacy._act.assert_not_called()
                    continue
                # DISMISSED drops the approval; a plain COMMENTED leaves it in force.
                if later == "DISMISSED":
                    self.assertEqual(result, "mediation-enqueued")
                    self.legacy._act.assert_called_once()
                    self.assertEqual(len(self.journal("mediate-intent")), 1)
                else:
                    self.assertEqual(result, "mediate-skip-approved")
                    self.legacy._act.assert_not_called()

    def test_mediate_skips_when_approval_arrives_between_listings(self):
        self.prime_head_mediate()
        approval = self.human_review("APPROVED", 8000, "2026-10-16T00:02:00Z")
        self.receipts.revoke_owned_ci.side_effect = (
            lambda *args, **kwargs: self.gh.reviews.append(approval) or "nothing-owned")
        self.assertEqual(self.reconcile(), "mediate-skip-approved")
        self.receipts.revoke_owned_ci.assert_called_once()
        self.legacy._act.assert_not_called()
        self.assertEqual(self.journal("mediate-intent"), [])
        self.assertEqual(self.journal("mediate"), [])

    def test_forged_mediate_neither_dispatches_nor_starts_mediation(self):
        self.prime_ready()
        self.gh.reviews[-1]["body"] = self.gh.reviews[-1]["body"].replace(
            "CODEX-GATE: READY", "CODEX-GATE: MEDIATE")
        self.gh.reviews[-1]["user"]["id"] += 1
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        self.legacy._act.assert_not_called()
        self.assertEqual(self.gh.dispatches, [])

    def test_persistent_human_label_is_not_coverage_and_does_not_suppress_delivery(self):
        self.gh.pr["labels"] = [{"name": "ci:full"}]
        self.gh.events = [{"id": 70, "event": "labeled", "label": {"name": "ci:full"}}]
        self.dispatch()
        self.assertEqual(self.gh.label_writes, [])

    def test_manual_removal_fixer_pending_and_unsettled_label_intent_block(self):
        for flag in ("manual_removal", "fixer_pending", "label_pending"):
            with self.subTest(flag=flag):
                self.setUp()
                self.prime_ready()
                setattr(self.receipts, flag, True)
                self.assertEqual(self.reconcile(), "held-or-fixer-pending")
                self.assertEqual(self.gh.dispatches, [])
                self.receipts.revoke_owned_ci.assert_called()

    def test_each_owner_hold_label_blocks_ready(self):
        for label in gate.HOLD_LABELS:
            with self.subTest(label=label):
                self.setUp()
                self.prime_ready()
                self.gh.pr["labels"] = [{"name": label.upper()}]
                self.assertEqual(self.reconcile(), "held-or-fixer-pending")
                self.assertEqual(self.gh.dispatches, [])

    def test_human_changes_requested_blocks_until_that_human_releases(self):
        self.prime_ready()
        self.gh.reviews.append({"id": 8000, "user": {"id": 55, "type": "User"}, "state": "CHANGES_REQUESTED",
                               "submitted_at": "2026-10-16T00:02:00Z"})
        self.assertEqual(self.reconcile(), "held-or-fixer-pending")
        self.gh.reviews.append({"id": 8001, "user": {"id": 55, "type": "User"}, "state": "APPROVED",
                               "submitted_at": "2026-10-16T00:03:00Z"})
        self.assertFalse(json.loads(self.reconcile())["qualified"])
        self.assertEqual(len(self.gh.dispatches), 2)

    def test_unknown_dispatch_outcome_never_resends_even_after_restart(self):
        for error in (TimeoutError("uncertain POST"), ApiError(500)):
            with self.subTest(error=error):
                self.setUp()
                self.prime_ready()
                self.gh.outcomes["ci"] = [error]
                with self.assertRaises(type(error)):
                    self.reconcile()
                self.assertEqual(len(self.journal("dispatch")), 1)
                for _ in range(4):
                    result = json.loads(self.reconcile())
                    self.assertFalse(result["qualified"])
                    self.assertEqual(result["full_matrix"]["ci"], "reserved-delivery-uncertain")
                self.assertEqual([k for k, _ in self.gh.dispatches].count("ci"), 1)

    def test_explicit_rejections_retry_at_most_three_times(self):
        self.prime_ready()
        self.gh.outcomes["ci"] = [ApiError(403)] * 4
        for _ in range(3):
            with self.assertRaises(ApiError):
                self.reconcile()
        result = json.loads(self.reconcile())
        self.assertEqual(result["full_matrix"]["ci"], "rejection-budget-exhausted")
        self.assertFalse(result["qualified"])
        self.assertEqual([k for k, _ in self.gh.dispatches].count("ci"), 3)

    def test_explicit_rejection_then_successful_retry_is_not_qualified_until_observed(self):
        self.prime_ready()
        self.gh.outcomes["ci"] = [ApiError(422)]
        with self.assertRaises(ApiError):
            self.reconcile()
        self.assertFalse(json.loads(self.reconcile())["qualified"])
        self.assertEqual([k for k, _ in self.gh.dispatches].count("ci"), 2)

    def test_forged_request_does_not_authorize_ready(self):
        data = {"candidate": self.candidate().key, "pause": 0, "nonce": "2" * 32,
                "identity": self.candidate().record()}
        self.gh.add_comment(self.receipts.marker("candidate-request", data),
                            {**ACTION_USER, "id": ACTION_USER["id"] + 1})
        self.gh.reviews.append({"id": 5000, "state": "COMMENTED", "commit_id": HEAD,
            "submitted_at": "2026-10-16T00:01:00Z",
            "user": {"id": gate.candidate_api.CODEX_ID, "login": gate.candidate_api.CODEX_LOGIN, "type": "Bot"},
            "body": f"CODEX-CANDIDATE: {data['candidate']}\nCODEX-REQUEST: {data['nonce']}\nCODEX-GATE: READY"})
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        self.assertEqual(len(self.journal("request")), 1)
        self.assertEqual(self.gh.dispatches, [])

    def test_forged_dispatch_journal_cannot_suppress_required_delivery(self):
        self.prime_ready()
        self.gh.add_comment(self.receipts.marker("candidate-dispatch", {
            "candidate": self.candidate().key, "kind": "ci", "nonce": "3" * 32,
            "workflow": 301, "actor": ACTOR["id"], "before": 0}),
            {**ACTION_USER, "id": ACTION_USER["id"] + 1})
        self.assertFalse(json.loads(self.reconcile())["qualified"])
        self.assertEqual([kind for kind, _ in self.gh.dispatches], ["ci", "rust"])
        self.assertEqual(len(self.journal("dispatch")), 2)

    def test_uncertain_review_mention_is_reserved_and_never_repeated(self):
        original_post = self.gh.post

        def uncertain_mention(path, payload):
            original_post(path, payload)
            raise TimeoutError("response lost after mention")

        self.gh.post = uncertain_mention
        with self.assertRaises(TimeoutError):
            self.reconcile()
        self.gh.post = original_post
        for _ in range(3):
            self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        self.assertEqual(len(self.journal("request")), 1)
        self.assertEqual(len(self.gh.mentions), 1)
        self.assertEqual(self.gh.dispatches, [])

    def test_forged_or_quoted_rejection_cannot_release_uncertain_reservation(self):
        for forged in ("wrong-id", "wrong-login", "wrong-type", "quoted"):
            with self.subTest(forged=forged):
                self.setUp()
                self.dispatch()
                intent = self.journal("dispatch")[0]
                body = self.receipts.marker("candidate-rejected", {"nonce": intent["nonce"], "status": 403})
                user = copy.deepcopy(ACTION_USER)
                if forged == "quoted":
                    body = "> " + body
                else:
                    user[{"wrong-id": "id", "wrong-login": "login", "wrong-type": "type"}[forged]] = "forged"
                self.gh.add_comment(body, user)
                self.assertFalse(json.loads(self.reconcile())["qualified"])
                self.assertEqual(len(self.gh.dispatches), 2)

    def test_untrusted_receipt_post_stops_before_review_or_dispatch_side_effect(self):
        self.gh.receipt_user = {**ACTION_USER, "id": 123}
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.gh.mentions, [])
        self.assertEqual(self.gh.dispatches, [])

    def test_head_or_base_drift_requires_new_candidate_review(self):
        for side in ("head", "base"):
            with self.subTest(side=side):
                self.setUp()
                request = self.prime_ready()
                self.gh.drift(side)
                self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
                self.assertNotEqual(self.journal("request")[-1]["candidate"], request["candidate"])
                self.assertEqual(self.gh.dispatches, [])

    def test_candidate_drift_during_snapshot_fails_closed(self):
        self.prime_ready()
        self.gh.on_merge_get = lambda: self.gh.drift("base")
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.gh.dispatches, [])

    def test_stale_pr_merge_cache_cannot_hide_live_head_or_base_movement(self):
        for side in ("head", "base"):
            with self.subTest(side=side):
                self.setUp()
                self.prime_ready()
                self.gh.live_refs[self.gh.pr[side]["ref"]] = "e" * 40
                with self.assertRaises(ValueError):
                    self.reconcile()
                self.assertEqual(self.gh.dispatches, [])

    def test_candidate_drift_after_reservation_stops_before_dispatch(self):
        self.prime_ready()
        self.gh.on_record = lambda body: self.gh.drift("head") if "candidate-dispatch:" in body else None
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(len(self.journal("dispatch")), 1)

    def test_fixer_arriving_after_reservation_stops_before_dispatch(self):
        self.prime_ready()
        def start_fixer(body):
            if "candidate-dispatch:" in body:
                self.receipts.fixer_pending = True
        self.gh.on_record = start_fixer
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.gh.dispatches, [])

    def test_old_ready_does_not_cross_fixer_pause_generation(self):
        self.prime_ready()
        self.receipts.pause += 1
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        self.assertEqual(self.gh.dispatches, [])

    def test_complete_observed_full_matrix_qualifies_without_redispatch(self):
        self.dispatch()
        self.observe_success()
        result = json.loads(self.reconcile())
        self.assertTrue(result["qualified"])
        self.assertEqual(result["full_matrix"], {"ci": "success", "rust": "success"})
        self.assertEqual(len(self.gh.dispatches), 2)

    def test_missing_platform_job_does_not_qualify_even_on_successful_run(self):
        for kind in ("ci", "rust"):
            with self.subTest(kind=kind):
                self.setUp()
                self.dispatch()
                self.observe_success()
                identifier = self.gh.run_lists[self.gh.workflow_ids[kind]][0]["id"]
                self.gh.jobs[(identifier, 1)].pop()
                self.assertFalse(json.loads(self.reconcile())["qualified"])
                self.assertEqual(len(self.gh.dispatches), 2)

    def test_pending_failed_cancelled_or_skipped_required_job_does_not_qualify(self):
        for status, conclusion in (("queued", None), ("in_progress", None), ("completed", "failure"),
                                   ("completed", "cancelled"), ("completed", "skipped")):
            with self.subTest(status=status, conclusion=conclusion):
                self.setUp()
                self.dispatch()
                self.observe_success()
                job = next(iter(self.gh.jobs.values()))[0]
                job.update(status=status, conclusion=conclusion)
                self.assertFalse(json.loads(self.reconcile())["qualified"])

    def test_rerun_attempt_changing_during_observation_does_not_qualify(self):
        self.dispatch()
        self.observe_success()
        self.gh.latest_runs[9301]["run_attempt"] = 2
        result = json.loads(self.reconcile())
        self.assertFalse(result["qualified"])
        self.assertEqual(result["full_matrix"]["ci"], "run-changed-reconcile")

    def test_job_change_between_observations_does_not_qualify(self):
        self.dispatch()
        self.observe_success()
        original_run = self.gh._run
        reads = 0

        def changing_jobs(args, token):
            nonlocal reads
            if "/actions/runs/9301/attempts/1/jobs" in args[1]:
                reads += 1
                if reads == 2:
                    self.gh.jobs[(9301, 1)][0]["conclusion"] = "failure"
            return original_run(args, token)

        self.gh._run = changing_jobs
        result = json.loads(self.reconcile())
        self.assertFalse(result["qualified"])
        self.assertEqual(result["full_matrix"]["ci"], "jobs-changed-reconcile")
        self.assertEqual(len(self.gh.dispatches), 2)

    def test_ci_failure_after_rust_observation_is_caught_by_final_matrix_recheck(self):
        self.dispatch()
        self.observe_success()
        original_run = self.gh._run

        def fail_ci_after_rust(args, token):
            result = original_run(args, token)
            if args[1] == f"{PREFIX}/actions/runs/9302":
                self.gh.jobs[(9301, 1)][0]["conclusion"] = "failure"
            return result

        self.gh._run = fail_ci_after_rust
        result = json.loads(self.reconcile())
        self.assertFalse(result["qualified"])
        self.assertEqual(result["full_matrix"]["ci"], "pending-or-failed")
        self.assertEqual(len(self.gh.dispatches), 2)

    def test_unavailable_second_comment_page_never_becomes_empty_success(self):
        for _ in range(100):
            self.gh.add_comment("ordinary", {"id": 90, "login": "writer", "type": "User"})
        original_get = self.gh.get

        def unavailable_page(path):
            if "/comments?" in path and "page=2" in path:
                raise ApiError(500)
            return original_get(path)

        self.gh.get = unavailable_page
        with self.assertRaises(ApiError):
            self.reconcile()
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(self.gh.mentions, [])

    def test_wrong_dispatch_actor_cannot_qualify(self):
        self.dispatch()
        self.observe_success()
        self.gh.run_lists[301][0]["actor"] = {"id": 999}
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(len(self.gh.dispatches), 2)

    def test_ref_qualified_workflow_and_run_paths_qualify_with_every_other_binding_intact(self):
        for suffix in ("@refs/heads/feature/owned", "@feature/owned", "@" + HEAD, "@refs/pull/42/merge"):
            with self.subTest(suffix=suffix):
                self.setUp()
                self.gh.workflow_paths = {kind: path + suffix for kind, path in gate.KINDS.items()}
                self.dispatch()
                self.observe_success()
                for kind, workflow_id in self.gh.workflow_ids.items():
                    run = self.gh.run_lists[workflow_id][0]
                    run["path"] = gate.KINDS[kind] + suffix
                    self.gh.latest_runs[run["id"]]["path"] = run["path"]
                result = json.loads(self.reconcile())
                self.assertTrue(result["qualified"])
                self.assertEqual(result["full_matrix"], {"ci": "success", "rust": "success"})
                self.assertEqual(len(self.gh.dispatches), 2)
                # The suffix tolerates metadata shape only; identity drift still refuses.
                run = self.gh.run_lists[301][0]
                for field, value in (("actor", {"id": 999}), ("workflow_id", 302), ("event", "pull_request"),
                                     ("repository", {"id": 10, "full_name": "fork/repo"})):
                    with self.subTest(suffix=suffix, field=field):
                        original = copy.deepcopy(run[field])
                        run[field] = value
                        self.gh.latest_runs[run["id"]][field] = copy.deepcopy(value)
                        with self.assertRaises(ValueError):
                            self.reconcile()
                        run[field] = original
                        self.gh.latest_runs[run["id"]][field] = copy.deepcopy(original)
                self.assertEqual(len(self.gh.dispatches), 2)

    def test_wrong_or_malformed_workflow_path_never_qualifies_or_dispatches(self):
        malformed = (".github/workflows/rust.yml", ".github/workflows/rust.yml@refs/heads/feature/owned",
                     ".github/workflows/ci.yml@", ".github/workflows/ci.yml@refs/heads/x y",
                     ".github/workflows/ci.yml@refs/heads/x@y", ".github/workflows/ci.yml.bak@main",
                     ".github/workflows/ci.yaml@main", "x.github/workflows/ci.yml@main",
                     "@.github/workflows/ci.yml", ".github/workflows/ci.yml@main@.github/workflows/ci.yml",
                     ".github/workflows/ci.yml@refs/heads/{x}", ".github/workflows/ci.yml@\nmain", "", None)
        for path in malformed:
            with self.subTest(path=path, stage="run"):
                self.setUp()
                self.dispatch()
                self.observe_success()
                run = self.gh.run_lists[301][0]
                run["path"] = path
                self.gh.latest_runs[run["id"]]["path"] = path
                with self.assertRaises(ValueError):
                    self.reconcile()
                self.assertEqual(len(self.gh.dispatches), 2)
            with self.subTest(path=path, stage="workflow"):
                self.setUp()
                self.prime_ready()
                self.gh.workflow_paths["ci"] = path
                with self.assertRaises(ValueError):
                    self.reconcile()
                self.assertEqual(self.gh.dispatches, [])
                self.assertEqual(self.journal("dispatch"), [])

    def test_duplicate_candidate_runs_fail_closed(self):
        self.dispatch()
        self.observe_success()
        duplicate = copy.deepcopy(self.gh.run_lists[301][0])
        duplicate["id"] += 100
        self.gh.run_lists[301].append(duplicate)
        with self.assertRaises(ValueError):
            self.reconcile()

    def test_authenticated_request_on_second_comment_page_is_reused(self):
        for _ in range(100):
            self.gh.add_comment("ordinary comment", {"id": 90, "login": "writer", "type": "User"})
        self.prime_ready()
        self.reconcile()
        self.assertEqual(len(self.journal("request")), 1)
        self.assertEqual(len(self.gh.mentions), 1)
        self.assertEqual(len(self.gh.dispatches), 2)

    def test_cli_main_routes_candidate_environment_to_real_coordinator(self):
        self.prime_ready()
        exited, output = self.run_cli({"CODEX_POLICY_SHA": POLICY.sha, "CODEX_POLICY_BRANCH": POLICY.branch})
        self.assertEqual(exited, 0)
        result = json.loads(output)
        self.assertFalse(result["qualified"])
        self.assertEqual(result["candidate"], self.candidate().key)
        self.assertEqual(result["policy"], POLICY.sha)
        self.assertEqual([kind for kind, _ in self.gh.dispatches], ["ci", "rust"])
        self.assertEqual(self.gh.label_writes, [])
        for _, payload in self.gh.dispatches:
            self.assertEqual(payload["ref"], POLICY.branch)

    def test_cli_main_without_policy_identity_fails_closed_before_side_effects(self):
        self.prime_ready()
        for policy_env in ({}, {"CODEX_POLICY_SHA": POLICY.sha}, {"CODEX_POLICY_BRANCH": POLICY.branch},
                           {"CODEX_POLICY_SHA": "", "CODEX_POLICY_BRANCH": ""},
                           {"CODEX_POLICY_SHA": "not-a-sha", "CODEX_POLICY_BRANCH": POLICY.branch}):
            with self.subTest(policy_env=policy_env):
                exited, output = self.run_cli(policy_env)
                self.assertEqual(exited, 1)
                self.assertIn("::notice::", output)
                self.assertEqual(self.gh.dispatches, [])
                self.assertEqual(self.journal("dispatch"), [])
                self.assertEqual(len(self.gh.mentions), 1)

    def run_cli(self, policy_env):
        receipts = self.receipts
        original_spec = importlib.util.spec_from_file_location

        class ReceiptLoader:
            def create_module(self, spec):
                return None

            def exec_module(self, module):
                # Replace only the established legacy subsystem boundary.
                for name in dir(receipts):
                    if not name.startswith("__"):
                        setattr(module, name, getattr(receipts, name))

        def module_spec(name, *args, **kwargs):
            if name == "claude_fixer_receipts":
                return importlib.util.spec_from_loader(name, ReceiptLoader())
            return original_spec(name, *args, **kwargs)

        def fake_gh_process(args, **kwargs):
            self.assertEqual(args[:2], ["gh", "api"])
            path, token = args[2], kwargs["env"]["GH_TOKEN"]
            if path == "graphql":
                return self.gh._run(args[1:], token)
            if "--method" in args:
                self.assertEqual(args[3:], ["--method", "POST", "--input", "-"])
                payload = json.loads(kwargs["input"])
                result = (self.gh.post_read_token(path, payload) if token == "read-token"
                          else self.gh.post(path, payload))
                return json.dumps(result)
            if token == "read-token":
                return json.dumps(self.gh.get(path))
            return self.gh._run(args[1:], token)

        output = io.StringIO()
        environment = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": REPO,
            "CODEX_CANDIDATE_PR": str(PR), "CODEX_TRIGGER_PAT": self.gh.write_token,
            "GH_TOKEN": "read-token", "GITHUB_EVENT_PATH": "fake-event.json", **policy_env}
        with (patch.dict(os.environ, environment, clear=True),
              patch.dict(sys.modules, {"candidate_gate": gate}),
              patch("importlib.util.spec_from_file_location", side_effect=module_spec),
              patch("builtins.open", mock_open(read_data='{"inputs":{"pr_number":"42"}}')),
              patch("subprocess.check_output", side_effect=fake_gh_process),
              redirect_stdout(output), self.assertRaises(SystemExit) as exited):
            runpy.run_path(str(ROOT / "scripts/ci/codex_gate.py"), run_name="__main__")
        return exited.exception.code, output.getvalue()

    def run_main(self, scan_value, github=None):
        """Run candidate_gate.py as the workflow's scan step does, with fake GitHub boundaries."""
        def refuse(*args, **kwargs):
            raise AssertionError("coordinator off: no GitHub client or event read")
        fake = SimpleNamespace(GitHub=github or refuse, load_event=Mock(return_value={}) if github else refuse)
        environment = {"GH_TOKEN": "read-token", "GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_NAME": "schedule",
                       "GITHUB_EVENT_PATH": "fake-event.json"}
        if scan_value is not None:
            environment["CANDIDATE_SCAN"] = scan_value
        stdout, code = io.StringIO(), None
        with tempfile.TemporaryDirectory() as directory:
            environment["GITHUB_OUTPUT"] = str(Path(directory) / "output")
            with (patch.dict(os.environ, environment, clear=True),
                  patch.dict(sys.modules, {"codex_gate": fake, "merge_candidate": gate.candidate_api}),
                  redirect_stdout(stdout)):
                try:
                    runpy.run_path(str(ROOT / "scripts/ci/candidate_gate.py"), run_name="__main__")
                except SystemExit as exited:
                    code = exited.code
            written = Path(environment["GITHUB_OUTPUT"]).read_text(encoding="utf-8")
        return code, written, stdout.getvalue()

    def assert_inert(self, scan_value):
        code, written, stdout = self.run_main(scan_value)
        self.assertEqual(code, 0)
        self.assertEqual(written, "prs=[]\n")
        self.assertIn('::notice::Candidate coordinator is off (repo variable CANDIDATE_COORDINATOR is not "true").',
                      stdout)

    def test_main_is_inert_when_coordinator_variable_unset(self):
        """An unset repo variable leaves the coordinator off: prs=[] and no GitHub API call."""
        self.assert_inert(None)

    def test_main_is_inert_for_non_true_values(self):
        for value in ("", "false", "TRUE", "1", "yes", " true"):
            with self.subTest(value=value):
                self.assert_inert(value)

    def test_main_scans_only_when_variable_is_exactly_true(self):
        """Exactly "true" runs the real scan() against the read token and writes its result."""
        client = SimpleNamespace(get=Mock(return_value=[{"id": 1, "number": PR, "draft": False,
                                                         "head": {"repo": {"full_name": REPO}}}]))
        github = Mock(return_value=client)
        code, written, stdout = self.run_main("true", github)
        self.assertIsNone(code)
        github.assert_called_once_with("read-token")
        self.assertTrue(client.get.call_args.args[0].startswith(f"repos/{REPO}/pulls?state=open"))
        self.assertEqual(written, f'prs=["{PR}"]\n')
        self.assertNotIn("Candidate coordinator is off", stdout)

    def test_edited_machine_receipt_fails_closed_without_redispatch(self):
        for kind in ("request", "dispatch"):
            with self.subTest(kind=kind):
                self.setUp()
                self.dispatch()
                receipt = next(comment for comment in self.gh.comments
                    if self.receipts.records(comment["body"], "candidate-" + kind))
                receipt["updated_at"] = "2026-10-16T00:03:00Z"
                with self.assertRaises(ValueError):
                    self.reconcile()
                self.assertEqual(len(self.gh.dispatches), 2)
                self.assertEqual(len(self.gh.mentions), 1)

    def test_receipt_without_update_timestamp_does_not_authorize_ready(self):
        self.prime_ready()
        receipt = next(comment for comment in self.gh.comments
            if self.receipts.records(comment["body"], "candidate-request"))
        receipt.pop("updated_at")
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.gh.dispatches, [])

    def test_edited_or_mismatched_review_provenance_cannot_authorize_ready_or_mediate(self):
        invalid_nodes = ({"includesCreatedEdit": True}, {"lastEditedAt": "2026-10-16T00:02:00Z"},
            {"fullDatabaseId": 7777}, {"__typename": "IssueComment"}, {"includesCreatedEdit": None})
        for verdict in ("READY", "MEDIATE"):
            for changes in invalid_nodes:
                with self.subTest(verdict=verdict, changes=changes):
                    self.setUp()
                    self.prime_ready()
                    self.gh.reviews[-1]["body"] = self.gh.reviews[-1]["body"].replace(
                        "CODEX-GATE: READY", "CODEX-GATE: " + verdict)
                    self.gh.graphql_node_overrides = changes
                    with self.assertRaises(ValueError):
                        self.reconcile()
                    self.assertEqual(self.gh.dispatches, [])
                    self.legacy._act.assert_not_called()

    def test_unavailable_review_provenance_cannot_authorize_ready(self):
        self.prime_ready()
        self.gh.graphql_error = OSError("provenance unavailable")
        with self.assertRaises(OSError):
            self.reconcile()
        self.assertEqual(self.gh.dispatches, [])

    def test_review_without_graphql_node_id_cannot_authorize_ready(self):
        self.prime_ready()
        self.gh.reviews[-1].pop("node_id")
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.gh.dispatches, [])

    def test_review_edited_after_dispatch_reservation_stops_before_post(self):
        self.prime_ready()

        def edit_review(body):
            if "candidate-dispatch:" in body:
                self.gh.graphql_node_overrides["includesCreatedEdit"] = True

        self.gh.on_record = edit_review
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(len(self.journal("dispatch")), 1)

    def test_ineligible_candidate_revokes_owned_legacy_ready_before_resolution(self):
        for change, expected in (({"draft": True}, "ineligible-candidate"),
                                 ({"state": "closed"}, "ineligible-candidate"),
                                 ({"mergeable": False}, "ineligible-candidate"),
                                 ({"mergeable": None}, "awaiting-mergeability")):
            with self.subTest(change=change):
                self.setUp()
                self.gh.pr.update(change)
                self.gh.pr["merge_commit_sha"] = None
                self.assertEqual(self.reconcile(), expected)
                self.receipts.revoke_owned_ci.assert_called_once()
                self.assertEqual(self.gh.dispatches, [])
                self.assertEqual(self.gh.mentions, [])

    def test_later_submitted_lower_id_human_change_request_overrides_approval(self):
        self.prime_ready()
        self.gh.reviews.extend([
            {"id": 8000, "user": {"id": 55, "type": "User"}, "state": "APPROVED",
             "submitted_at": "2026-10-16T00:02:00Z"},
            {"id": 7000, "user": {"id": 55, "type": "User"}, "state": "CHANGES_REQUESTED",
             "submitted_at": "2026-10-16T00:03:00Z"}])
        self.assertEqual(self.reconcile(), "held-or-fixer-pending")
        self.assertEqual(self.gh.dispatches, [])

    def test_hold_cleanup_precedes_unavailable_review_provenance(self):
        self.prime_ready()
        self.receipts.revoke_owned_ci.reset_mock()
        self.gh.pr["labels"] = [{"name": "hold"}]
        self.gh.graphql_error = OSError("unavailable")
        self.assertEqual(self.reconcile(), "held-or-fixer-pending")
        self.receipts.revoke_owned_ci.assert_called_once()
        self.assertEqual(self.gh.dispatches, [])

    def test_run_lookup_filters_policy_commit_before_bounded_pagination(self):
        self.prime_ready()
        # Unrelated history on other commits, including the candidate head, is
        # filtered server-side by the policy SHA before the bounded page walk.
        self.gh.run_lists[301] = [{"id": index + 1, "head_sha": ("f" * 40, HEAD)[index % 2]} for index in range(1100)]
        self.assertFalse(json.loads(self.reconcile())["qualified"])
        self.assertTrue(self.gh.run_queries)
        for query in self.gh.run_queries:
            self.assertEqual(query["head_sha"], [POLICY.sha])
            self.assertEqual(query["event"], ["workflow_dispatch"])
        self.assertEqual([kind for kind, _ in self.gh.dispatches], ["ci", "rust"])

    def test_bounded_pagination_still_applies_to_policy_commit_history(self):
        self.prime_ready()
        self.gh.run_lists[301] = [{"id": index + 1, "head_sha": POLICY.sha} for index in range(1100)]
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.gh.dispatches, [])

    def test_known_pre_dispatch_abort_allows_later_safe_retry(self):
        self.prime_ready()

        def block_after_intent(body):
            if "candidate-dispatch:" in body:
                self.receipts.fixer_pending = True

        self.gh.on_record = block_after_intent
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(len(self.journal("aborted")), 1)
        self.gh.on_record = None
        self.receipts.fixer_pending = False
        self.assertFalse(json.loads(self.reconcile())["qualified"])
        self.assertEqual([kind for kind, _ in self.gh.dispatches], ["ci", "rust"])
        self.assertEqual(len(self.journal("dispatch")), 3)

    def test_mediate_exception_keeps_delivery_reserved_without_retry(self):
        self.prime_ready()
        self.gh.reviews[-1]["body"] = self.gh.reviews[-1]["body"].replace(
            "CODEX-GATE: READY", "CODEX-GATE: MEDIATE")
        self.legacy._act.side_effect = TimeoutError("unknown mediation outcome")
        with self.assertRaises(TimeoutError):
            self.reconcile()
        for _ in range(3):
            self.assertEqual(self.reconcile(), "mediate-delivery-uncertain")
        self.legacy._act.assert_called_once()
        self.assertEqual(len(self.journal("mediate-intent")), 1)

    def mutate_state(self, mutation):
        if mutation in ("head", "base"):
            self.gh.drift(mutation)
        elif mutation == "hold":
            self.gh.pr["labels"] = [{"name": "hold"}]
        elif mutation == "pause":
            self.receipts.pause += 1
        else:
            raise AssertionError(mutation)

    def test_graphql_time_state_changes_cannot_reach_ready_or_mediate_side_effect(self):
        for verdict in ("READY", "MEDIATE"):
            for mutation in (("base", "hold", "pause") if verdict == "READY" else ("head", "hold", "pause")):
                with self.subTest(verdict=verdict, mutation=mutation):
                    self.setUp()
                    self.prime_ready()
                    self.gh.reviews[-1]["body"] = self.gh.reviews[-1]["body"].replace(
                        "CODEX-GATE: READY", "CODEX-GATE: " + verdict)

                    def change_during_provenance():
                        # Repeat mutation at every provenance boundary, including
                        # the final fresh check immediately preceding a side effect.
                        self.mutate_state(mutation)

                    self.gh.on_graphql = change_during_provenance
                    with self.assertRaises(ValueError):
                        self.reconcile()
                    self.assertEqual(self.gh.dispatches, [])
                    self.legacy._act.assert_not_called()

    def test_request_record_time_mutation_stops_before_review_mention(self):
        for mutation in ("base", "hold", "pause"):
            with self.subTest(mutation=mutation):
                self.setUp()
                self.gh.on_record = lambda body: self.mutate_state(mutation) if "candidate-request:" in body else None
                with self.assertRaises(ValueError):
                    self.reconcile()
                self.assertEqual(self.gh.mentions, [])
                self.assertEqual(self.gh.dispatches, [])

    def test_final_graphql_after_reservation_is_followed_by_closing_state_check(self):
        for verdict, intent_kind, aborted_kind in (("READY", "dispatch", "aborted"),
                                                   ("MEDIATE", "mediate-intent", "mediate-aborted")):
            for mutation in (("base", "hold", "pause") if verdict == "READY" else ("head", "hold", "pause")):
                with self.subTest(verdict=verdict, mutation=mutation):
                    self.setUp()
                    self.prime_ready()
                    self.gh.reviews[-1]["body"] = self.gh.reviews[-1]["body"].replace(
                        "CODEX-GATE: READY", "CODEX-GATE: " + verdict)

                    def arm_after_intent(body):
                        if self.receipts.records(body, "candidate-" + intent_kind):
                            self.gh.on_graphql = lambda: self.mutate_state(mutation)

                    self.gh.on_record = arm_after_intent
                    with self.assertRaises(ValueError):
                        self.reconcile()
                    self.assertEqual(self.gh.dispatches, [])
                    self.legacy._act.assert_not_called()
                    self.assertEqual(len(self.journal(intent_kind)), 1)
                    self.assertEqual(len(self.journal(aborted_kind)), 1)

    def test_mediate_intent_record_time_mutation_stops_before_action(self):
        for mutation in ("head", "hold", "pause"):
            with self.subTest(mutation=mutation):
                self.setUp()
                self.prime_ready()
                self.gh.reviews[-1]["body"] = self.gh.reviews[-1]["body"].replace(
                    "CODEX-GATE: READY", "CODEX-GATE: MEDIATE")
                self.gh.on_record = lambda body: self.mutate_state(mutation) if "candidate-mediate-intent:" in body else None
                with self.assertRaises(ValueError):
                    self.reconcile()
                self.legacy._act.assert_not_called()
                self.assertEqual(self.gh.dispatches, [])

    def test_proven_pre_mention_abort_allows_a_new_request_after_hold_release(self):
        self.gh.on_record = lambda body: self.mutate_state("hold") if "candidate-request:" in body else None
        with self.assertRaises(ValueError):
            self.reconcile()
        first_request = self.journal("request")[0]
        self.assertEqual(self.journal("request-aborted")[0]["request"], first_request["id"])
        self.assertEqual(self.gh.mentions, [])
        self.gh.on_record = None
        self.gh.pr["labels"] = []
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        self.assertEqual(len(self.gh.mentions), 1)
        self.assertEqual(len(self.journal("request")), 2)
        self.assertNotEqual(self.journal("request")[-1]["nonce"], first_request["nonce"])

    def test_proven_pre_mediate_abort_allows_one_safe_action_after_hold_release(self):
        self.prime_ready()
        self.gh.reviews[-1]["body"] = self.gh.reviews[-1]["body"].replace(
            "CODEX-GATE: READY", "CODEX-GATE: MEDIATE")
        self.gh.on_record = lambda body: self.mutate_state("hold") if "candidate-mediate-intent:" in body else None
        with self.assertRaises(ValueError):
            self.reconcile()
        first_intent = self.journal("mediate-intent")[0]
        self.assertEqual(self.journal("mediate-aborted")[0]["intent"], first_intent["id"])
        self.legacy._act.assert_not_called()
        self.gh.on_record = None
        self.gh.pr["labels"] = []
        self.assertEqual(self.reconcile(), "mediation-enqueued")
        self.assertEqual(self.reconcile(), "mediate-already-recorded")
        self.legacy._act.assert_called_once()
        self.assertEqual(len(self.journal("mediate-intent")), 2)

    def test_repeated_known_pre_dispatch_aborts_obey_retry_budget(self):
        self.prime_ready()
        for _ in range(3):
            self.receipts.fixer_pending = False
            self.gh.on_record = lambda body: setattr(self.receipts, "fixer_pending", True) if "candidate-dispatch:" in body else None
            with self.assertRaises(ValueError):
                self.reconcile()
        self.receipts.fixer_pending = False
        self.gh.on_record = None
        result = json.loads(self.reconcile())
        self.assertEqual(result["full_matrix"]["ci"], "rejection-budget-exhausted")
        self.assertFalse(result["qualified"])
        self.assertEqual([kind for kind, _ in self.gh.dispatches], ["rust"])

    def test_genuine_head_mediate_needs_no_request_or_mergeable_candidate(self):
        for mergeable in (False, None):
            with self.subTest(mergeable=mergeable):
                self.setUp()
                self.gh.pr.update(mergeable=mergeable, merge_commit_sha=None)
                self.prime_head_mediate()
                self.assertEqual(self.reconcile(), "mediation-enqueued")
                self.legacy._act.assert_called_once_with("MEDIATE", REPO, str(PR), HEAD, [],
                    "2026-10-16", self.gh, self.gh.reviews)
                self.assertEqual(self.journal("request"), [])
                self.assertEqual(self.gh.mentions, [])
                self.assertEqual(self.gh.dispatches, [])

    def test_head_mediate_allows_pending_fixer_findings_and_pre_pause_review(self):
        self.prime_head_mediate()
        self.receipts.fixer_pending = True
        self.receipts.pause = 7
        self.gh.pr.update(mergeable=False, merge_commit_sha=None)
        self.assertEqual(self.reconcile(), "mediation-enqueued")
        self.legacy._act.assert_called_once()
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(self.gh.mentions, [])

    def test_head_mediate_does_not_read_candidate_base_or_merge_evidence(self):
        self.prime_head_mediate()
        self.gh.pr.update(base=None, mergeable=None, merge_commit_sha=None)
        self.gh.on_merge_get = Mock(side_effect=AssertionError("MEDIATE must not resolve merge tree"))
        self.assertEqual(self.reconcile(), "mediation-enqueued")
        self.gh.on_merge_get.assert_not_called()
        self.legacy._act.assert_called_once()
        self.assertEqual(self.gh.dispatches, [])

    def test_base_only_drift_does_not_suppress_head_mediate(self):
        for boundary in ("before", "graphql", "intent"):
            with self.subTest(boundary=boundary):
                self.setUp()
                self.prime_head_mediate()
                if boundary == "before":
                    self.gh.drift("base")
                elif boundary == "graphql":
                    self.gh.on_graphql = lambda: self.gh.drift("base")
                else:
                    self.gh.on_record = lambda body: self.gh.drift("base") if "candidate-mediate-intent:" in body else None
                self.assertEqual(self.reconcile(), "mediation-enqueued")
                self.legacy._act.assert_called_once()
                self.assertEqual(self.gh.dispatches, [])

    def test_base_drift_after_completed_head_mediate_does_not_repeat_action(self):
        self.prime_head_mediate()
        self.assertEqual(self.reconcile(), "mediation-enqueued")
        self.gh.drift("base")
        self.gh.pr.update(mergeable=False, merge_commit_sha=None)
        self.assertEqual(self.reconcile(), "mediate-already-recorded")
        self.legacy._act.assert_called_once()

    def test_base_drift_cannot_release_uncertain_head_mediate_delivery(self):
        self.prime_head_mediate()
        self.legacy._act.side_effect = TimeoutError("unknown handoff delivery")
        with self.assertRaises(TimeoutError):
            self.reconcile()
        self.gh.drift("base")
        self.gh.pr.update(mergeable=False, merge_commit_sha=None)
        self.assertEqual(self.reconcile(), "mediate-delivery-uncertain")
        self.legacy._act.assert_called_once()

    def test_stale_cached_head_mediate_refuses_live_branch_head_mismatch(self):
        self.prime_head_mediate()
        self.gh.pr.update(mergeable=False, merge_commit_sha=None)
        self.gh.live_refs[self.gh.pr["head"]["ref"]] = "e" * 40
        with self.assertRaises(ValueError):
            self.reconcile()
        self.legacy._act.assert_not_called()
        self.assertEqual(self.gh.dispatches, [])

    def test_held_malformed_review_still_revokes_owned_ready(self):
        self.prime_head_mediate()
        self.gh.reviews[-1].update(submitted_at=None, node_id=None)
        self.gh.pr["labels"] = [{"name": "hold"}]
        self.gh.graphql_error = OSError("unavailable provenance")
        self.assertEqual(self.reconcile(), "held-or-fixer-pending")
        self.receipts.revoke_owned_ci.assert_called_once()
        self.legacy._act.assert_not_called()
        self.assertEqual(self.gh.dispatches, [])

    def test_closed_pr_with_deleted_head_cleans_up_without_ref_lookup(self):
        self.prime_head_mediate()
        self.gh.pr.update(state="closed", mergeable=False, merge_commit_sha=None)
        original_get = self.gh.get

        def deleted_head(path):
            if "/git/ref/heads/" in path:
                raise AssertionError("Closed PR cleanup must not resolve deleted head")
            return original_get(path)

        self.gh.get = deleted_head
        self.assertEqual(self.reconcile(), "ineligible-candidate")
        self.receipts.revoke_owned_ci.assert_called_once()
        self.legacy._act.assert_not_called()

    def test_nonmergeable_ready_never_inherits_head_only_mediate_authority(self):
        for mergeable in (False, None):
            with self.subTest(mergeable=mergeable):
                self.setUp()
                self.prime_ready()
                self.gh.pr.update(mergeable=mergeable, merge_commit_sha=None)
                expected = "ineligible-candidate" if mergeable is False else "awaiting-mergeability"
                self.assertEqual(self.reconcile(), expected)
                self.legacy._act.assert_not_called()
                self.assertEqual(self.gh.dispatches, [])

    def test_genuine_codex_changes_requested_can_handoff_current_head(self):
        self.prime_head_mediate()["state"] = "CHANGES_REQUESTED"
        self.gh.pr.update(mergeable=False, merge_commit_sha=None)
        self.assertEqual(self.reconcile(), "mediation-enqueued")
        self.legacy._act.assert_called_once()
        self.assertEqual(self.gh.dispatches, [])

    def test_delayed_old_mediate_preserves_latest_ready_and_qualified_candidate(self):
        self.dispatch()
        self.observe_success()
        ready = copy.deepcopy(self.gh.reviews[-1])
        old = copy.deepcopy(ready)
        old.update(id=4999, node_id="PRR_test_4999", submitted_at="2026-10-16T00:00:30Z",
                   body="CODEX-GATE: MEDIATE")
        self.receipts.revoke_owned_ci.reset_mock()
        # API order and an old review event's arrival cannot select the verdict.
        for reviews in ([old, ready], [ready, old]):
            self.gh.reviews = reviews
            for _ in range(2):
                result = json.loads(self.reconcile())
                self.assertTrue(result["qualified"])
        self.assertEqual(len(self.gh.dispatches), 2)
        self.receipts.revoke_owned_ci.assert_not_called()
        self.legacy._act.assert_not_called()
        self.assertEqual(self.journal("mediate-intent"), [])

    def test_legacy_completed_mediate_propagates_without_repeating_handoff(self):
        from scripts.ci import codex_gate as legacy
        self.prime_head_mediate()
        self.gh.pr.update(mergeable=False, merge_commit_sha=None)
        self.legacy.action_for_sha = legacy.action_for_sha
        self.legacy.ready_comments = legacy.ready_comments
        self.legacy.trusted_comments = legacy.trusted_comments
        self.legacy.marker_authors.return_value = {"github-actions[bot]"}
        self.gh.add_comment(legacy.action_marker(HEAD, "MEDIATE"))
        for _ in range(3):
            self.assertEqual(self.reconcile(), "mediate-already-recorded")
        self.legacy._act.assert_not_called()
        self.assertEqual(self.journal("mediate-intent"), [])
        self.assertEqual(self.gh.dispatches, [])

    def test_new_mediate_invalidates_real_owned_label_and_hands_off_once(self):
        real = self.configure_owned_ready_label()
        self.assertEqual(self.reconcile(), "mediation-enqueued")
        self.assertEqual(self.reconcile(), "mediate-already-recorded")
        self.gh.delete_read_token.assert_called_once()
        self.legacy._act.assert_called_once()
        self.assertEqual(self.journal("mediate")[0]["pause"], real.latest_pause_id(self.gh.comments, HEAD))
        self.assertEqual(self.gh.dispatches, [])

    def configure_owned_ready_label(self):
        from scripts.ci import codex_gate as legacy
        real = legacy.FIXER_RECEIPTS
        self.prime_head_mediate()
        self.gh.pr["labels"] = [{"name": "ci:full"}]
        self.gh.events = [{"id": 700, "event": "labeled", "label": {"name": "ci:full"}}]
        self.gh.add_comment(real.marker("ci-owned", {"head": HEAD, "event": 700}))
        self.receipts.revoke_owned_ci = real.revoke_owned_ci
        self.receipts.latest_pause_id = real.latest_pause_id
        self.receipts.ci_label_decision = real.ci_label_decision

        def delete(path):
            self.assertEqual(path, f"{PREFIX}/issues/{PR}/labels/ci:full")
            self.gh.pr["labels"] = []
            self.gh.events.append({"id": 701, "event": "unlabeled", "label": {"name": "ci:full"},
                                   "actor": ACTION_USER})
        self.gh.delete_read_token = Mock(side_effect=delete)
        return real

    def test_own_invalidation_cannot_adopt_concurrent_external_pause(self):
        self.configure_owned_ready_label()
        def pause_after_invalidation(body):
            if "claude-fixer-ci-invalidated:" in body:
                self.gh.add_comment(f"<!-- claude-fixer-loop-cap:{HEAD} -->")
        self.gh.on_record = pause_after_invalidation
        with self.assertRaises(ValueError):
            self.reconcile()
        self.legacy._act.assert_not_called()
        self.assertEqual(self.journal("mediate-intent"), [])
        self.assertEqual(self.gh.dispatches, [])

    def test_unverified_invalidation_response_cannot_delete_or_handoff(self):
        self.configure_owned_ready_label()
        self.gh.receipt_user = {"id": 1, "login": "impostor", "type": "User"}
        with self.assertRaises(ValueError):
            self.reconcile()
        self.gh.delete_read_token.assert_not_called()
        self.legacy._act.assert_not_called()

    def test_own_newer_invalidation_cannot_hide_an_intervening_external_pause(self):
        self.configure_owned_ready_label()
        original_post = self.gh.post_read_token
        def pause_before_own_post(path, payload):
            if "claude-fixer-ci-invalidated:" in payload.get("body", ""):
                self.gh.add_comment(f"<!-- claude-fixer-loop-cap:{HEAD} -->")
            return original_post(path, payload)
        self.gh.post_read_token = pause_before_own_post
        with self.assertRaises(ValueError):
            self.reconcile()
        self.legacy._act.assert_not_called()
        self.assertEqual(self.journal("mediate-intent"), [])

    def test_untrusted_or_old_head_legacy_mediate_does_not_suppress_new_handoff(self):
        from scripts.ci import codex_gate as legacy
        for head, user in ((HEAD, {"id": 1, "login": "impostor", "type": "User"}),
                           ("f" * 40, ACTION_USER)):
            with self.subTest(head=head, user=user):
                self.setUp()
                self.prime_head_mediate()
                self.legacy.action_for_sha = legacy.action_for_sha
                self.legacy.ready_comments = legacy.ready_comments
                self.legacy.trusted_comments = legacy.trusted_comments
                self.legacy.marker_authors.return_value = {"github-actions[bot]"}
                self.gh.add_comment(legacy.action_marker(head, "MEDIATE"), user)
                self.assertEqual(self.reconcile(), "mediation-enqueued")
                self.legacy._act.assert_called_once()

    def test_edited_mediate_cannot_invalidate_owned_ready_before_provenance_check(self):
        self.prime_head_mediate()
        self.gh.graphql_node_overrides = {"includesCreatedEdit": True}
        with self.assertRaises(ValueError):
            self.reconcile()
        self.receipts.revoke_owned_ci.assert_not_called()
        self.legacy._act.assert_not_called()

    # --- Regressions for Dan's 2026-10-17 re-review on f785b732 (CSL-259) ---

    def test_known_rejection_on_review_mention_releases_reservation_and_reposts(self):
        """A definitive 4xx/429 on the @codex review POST records request-aborted and re-requests next time."""
        original = self.gh.post

        def rejected(path, payload):
            if path.endswith("/comments"):
                raise ApiError(429)
            return original(path, payload)

        self.gh.post = rejected
        with self.assertRaises(ApiError):
            self.reconcile()
        requests = self.journal("request")
        aborted = self.journal("request-aborted")
        self.assertEqual(len(requests), 1)
        self.assertEqual([x["request"] for x in aborted], [requests[0]["id"]])
        self.assertEqual(aborted[0]["status"], 429)
        self.assertEqual(self.gh.mentions, [])
        self.gh.post = original
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        self.assertEqual(len(self.gh.mentions), 1)
        self.assertEqual(len(self.journal("request")), 2)
        self.assertNotEqual(self.journal("request")[-1]["nonce"], requests[0]["nonce"])

    def test_unknown_review_mention_outcome_stays_held_without_duplicate(self):
        """An unclassified failure leaves the reservation active: no abort receipt, no second mention."""
        original = self.gh.post

        def unknown(path, payload):
            if path.endswith("/comments"):
                raise RuntimeError("connection reset")
            return original(path, payload)

        self.gh.post = unknown
        with self.assertRaises(RuntimeError):
            self.reconcile()
        self.assertEqual(self.journal("request-aborted"), [])
        self.gh.post = original
        for _ in range(4):
            self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        self.assertEqual(self.gh.mentions, [])
        self.assertEqual(len(self.journal("request")), 1)

    def test_edited_release_record_is_skipped_not_raised(self):
        """A forged-then-edited abort/rejection record no longer halts every reconcile."""
        self.gh.add_comment(FakeReceipts.marker("candidate-request-aborted", {"request": 1}))
        self.gh.comments[-1]["updated_at"] = "2026-10-16T00:00:01Z"
        self.assertEqual(self.journal("request-aborted"), [])
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        self.assertEqual(len(self.gh.mentions), 1)

    def test_edited_reservation_record_still_fails_closed(self):
        """Dropping an edited request/dispatch record could duplicate a dispatch, so it still holds."""
        self.prime_ready()
        record = next(c for c in self.gh.comments if "<!-- candidate-request:" in c["body"])
        record["updated_at"] = "2026-10-16T00:00:01Z"
        with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation"):
            self.reconcile()
        self.assertEqual(self.gh.dispatches, [])

    def test_whole_comment_bot_record_is_still_trusted_by_login_only(self):
        """Documents Major 3 (open): any github-actions[bot] whole-comment record is accepted by journal()."""
        self.gh.add_comment(FakeReceipts.marker("candidate-request-aborted", {"request": 123}))
        self.assertEqual([x["request"] for x in self.journal("request-aborted")], [123])

    # --- Regressions for Devin findings on 8533e522 (CSL-259) ---

    def test_edited_mediate_intent_fails_closed_without_second_handoff(self):
        """An edited mediate-intent fails closed for every head: dropping it could repeat the mediation handoff."""
        review = self.prime_head_mediate()
        self.gh.add_comment(FakeReceipts.marker("candidate-mediate-intent",
            {"head": HEAD, "review": review["id"], "pause": 0, "nonce": "0" * 32}))
        self.gh.comments[-1]["updated_at"] = "2026-10-16T00:00:01Z"
        message = "edited or unversioned candidate reservation; reconcile manually"
        for call in (lambda: self.journal("mediate-intent"), self.coordinator.mediate, self.reconcile):
            with self.assertRaisesRegex(ValueError, message):
                call()
        # Ownership cannot be read from the edited body, so another head holds too.
        self.gh.drift("head")
        review["commit_id"] = self.gh.pr["head"]["sha"]
        with self.assertRaisesRegex(ValueError, message):
            self.reconcile()
        intents = [c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-mediate-intent")]
        self.assertEqual(len(intents), 1)
        self.legacy._act.assert_not_called()
        self.assertEqual(self.gh.mentions, [])

    def reject_mentions(self, status):
        original = self.gh.post

        def rejected(path, payload):
            if path.endswith("/comments"):
                raise ApiError(status)
            return original(path, payload)

        self.gh.post = rejected
        return original

    def test_permanent_review_mention_rejection_exhausts_request_budget(self):
        """A permanent 403 is re-requested at most REQUEST_BUDGET times for one candidate."""
        original = self.reject_mentions(403)
        for _ in range(gate.REQUEST_BUDGET):
            with self.assertRaises(ApiError):
                self.reconcile()
        self.assertEqual(len(self.journal("request")), 3)
        self.assertEqual(len(self.journal("request-aborted")), 3)
        self.gh.post = original
        comments = len(self.gh.comments)
        self.assertEqual(self.reconcile(), "request-budget-exhausted")
        self.assertEqual(len(self.gh.comments), comments)
        self.assertEqual(self.gh.mentions, [])

    def test_request_budget_resets_for_a_new_candidate(self):
        """The budget is per candidate key: a new head records a fresh request."""
        original = self.reject_mentions(403)
        for _ in range(gate.REQUEST_BUDGET):
            with self.assertRaises(ApiError):
                self.reconcile()
        self.gh.post = original
        old_key = self.candidate().key
        self.assertEqual(self.reconcile(), "request-budget-exhausted")
        self.gh.drift("head")
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        latest = self.journal("request")[-1]
        self.assertNotEqual(latest["candidate"], old_key)
        self.assertEqual(latest["candidate"], self.candidate().key)
        self.assertEqual(len(self.gh.mentions), 1)

    def test_active_reservation_is_reused_before_the_request_budget(self):
        """An unaborted matching reservation is returned, not counted out; any pause consumes budget."""
        for pause in range(gate.REQUEST_BUDGET):
            self.receipts.pause = pause
            self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        requests = self.journal("request")
        self.assertEqual(len(requests), 3)
        self.assertEqual(self.coordinator.request(self.candidate(), self.gh.comments), requests[-1])
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        self.assertEqual(len(self.gh.mentions), 3)
        # The budget is per pause generation: a fourth pause records a fresh request.
        self.receipts.pause = gate.REQUEST_BUDGET
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        self.assertEqual(len(self.journal("request")), 4)
        self.assertEqual(len(self.gh.mentions), 4)

    # --- Regressions for Dan's 07:31Z re-review on e49efb89 (CSL-259) ---

    def test_request_budget_resets_for_a_new_pause_on_the_same_candidate(self):
        """A permanent rejection exhausts one pause; a new fixer pause on the same candidate gets a fresh budget."""
        original = self.reject_mentions(403)
        for _ in range(gate.REQUEST_BUDGET):
            with self.assertRaises(ApiError):
                self.reconcile()
        self.gh.post = original
        self.assertEqual(self.reconcile(), "request-budget-exhausted")
        key = self.candidate().key
        self.receipts.pause = 1
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        latest = self.journal("request")[-1]
        self.assertEqual((latest["candidate"], latest["pause"]), (key, 1))
        self.assertEqual(len(self.gh.mentions), 1)

    def test_edited_reservation_holds_every_candidate(self):
        """An edited request/dispatch fails closed for candidate A and for a later candidate B alike."""
        for kind, append in (("request", False), ("dispatch", False), ("request", True), ("dispatch", True)):
            with self.subTest(kind=kind, append=append):
                self.setUp()
                self.dispatch()
                receipt = next(c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-" + kind))
                receipt["updated_at"] = "2026-10-16T00:03:00Z"
                if append:
                    # No longer a whole-comment record, but still an edited reservation.
                    receipt["body"] += "\nedited"
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
                        self.reconcile()
                self.gh.drift("head")
                # Every reservation kind is verified before any side effect, so
                # candidate B gets neither a review request nor a dispatch.
                with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
                    self.reconcile()
                self.assertEqual(len(self.gh.dispatches), 2)
                self.assertEqual(len(self.gh.mentions), 1)
                self.assertEqual(len([c for c in self.gh.comments
                                      if self.receipts.records(c["body"], "candidate-request")]), 1)
                dispatches = [c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-dispatch")]
                self.assertEqual(len(dispatches), 2)

    def test_reservation_edited_between_listings_fails_closed(self):
        """An edit that lands after reconcile's listing is caught by deliver()'s re-scan of its own listing."""
        # A request edit is invisible to deliver()'s dispatch journal; only the
        # all-kinds re-scan of the refreshed listing catches it.
        for kind in ("dispatch", "request"):
            with self.subTest(kind=kind):
                self.setUp()
                self.dispatch()
                original = self.gh._run

                def edit_after_listing(args, token, kind=kind, original=original):
                    if args == ["api", "user"]:
                        receipt = next(c for c in self.gh.comments
                                       if self.receipts.records(c["body"], "candidate-" + kind))
                        receipt["updated_at"] = "2026-10-16T00:03:00Z"
                    return original(args, token)

                self.gh._run = edit_after_listing
                with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
                    self.reconcile()
                self.assertEqual(len(self.gh.dispatches), 2)
                self.gh._run = original
                with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
                    self.reconcile()
                self.assertEqual(len(self.gh.dispatches), 2)

    def edit_on_revoke(self, kind):
        def edit(*args, **kwargs):
            receipt = next(c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-" + kind))
            receipt["updated_at"] = "2026-10-16T00:03:00Z"
            return "nothing-owned"
        self.receipts.revoke_owned_ci.side_effect = edit

    def test_request_edited_after_reconcile_check_is_not_duplicated(self):
        """An edit that lands during revoke is caught on request()'s own listing: no second mention."""
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        self.edit_on_revoke("request")
        with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
            self.reconcile()
        requests = [c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-request")]
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(self.gh.mentions), 1)

    def test_mediate_intent_edited_after_head_check_is_not_handed_off_again(self):
        """An edit that lands during revoke is caught on the listing mediate()'s intent decision uses."""
        review = self.prime_head_mediate()
        self.gh.add_comment(FakeReceipts.marker("candidate-mediate-intent",
            {"head": HEAD, "review": review["id"], "pause": 0, "nonce": "0" * 32}))
        self.edit_on_revoke("mediate-intent")
        with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
            self.reconcile()
        intents = [c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-mediate-intent")]
        self.assertEqual(len(intents), 1)
        self.legacy._act.assert_not_called()

    # --- Global fail-closed on edited reservations (Devin 4301947709, CSL-259) ---

    def rewrite_owner(self, kind, field, value):
        """Edit a whole-comment reservation so its body names a different owner."""
        comment = next(c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-" + kind))
        record = self.receipts.records(comment["body"], "candidate-" + kind)[0]
        comment["body"] = FakeReceipts.marker("candidate-" + kind, {**record, field: value})
        comment["updated_at"] = "2026-10-16T00:03:00Z"

    def test_edited_dispatch_with_rewritten_candidate_blocks_the_original_candidate(self):
        """Rewriting candidate A's dispatch to name B must not free A to dispatch a second full matrix."""
        self.dispatch()
        mentions, comments = len(self.gh.mentions), len(self.gh.comments)
        self.rewrite_owner("dispatch", "candidate", "b" * 64)
        with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
            self.reconcile()
        self.assertEqual(len(self.gh.dispatches), 2)
        self.assertEqual(len(self.gh.mentions), mentions)
        records = [c for c in self.gh.comments[comments:]
                   if self.receipts.records(c["body"], "candidate-dispatch") or self.receipts.records(c["body"], "candidate-request")]
        self.assertEqual(records, [])

    def test_edited_request_with_rewritten_candidate_blocks_the_original_candidate(self):
        """Rewriting candidate A's request to name B must not free A to request a second review."""
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        self.rewrite_owner("request", "candidate", "b" * 64)
        with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
            self.reconcile()
        requests = [c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-request")]
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(self.gh.mentions), 1)

    def test_edited_mediate_intent_with_rewritten_head_blocks_the_original_head(self):
        """Rewriting an unresolved mediate-intent to name another head must not free a second handoff."""
        self.prime_head_mediate()
        self.legacy._act.side_effect = TimeoutError("unknown mediation outcome")
        with self.assertRaises(TimeoutError):
            self.reconcile()
        self.rewrite_owner("mediate-intent", "head", "f" * 40)
        with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
            self.coordinator.mediate()
        with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
            self.reconcile()
        intents = [c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-mediate-intent")]
        self.assertEqual(len(intents), 1)
        self.legacy._act.assert_called_once()

    # --- Verify every reservation kind first; revoke before refusing (Devin 4301979507, 4301979544) ---

    def add_edited(self, kind, record):
        self.gh.add_comment(FakeReceipts.marker("candidate-" + kind, record))
        self.gh.comments[-1]["updated_at"] = "2026-10-16T00:03:00Z"

    def label_ci_full(self, owned):
        """Real receipt ownership: only a ci-owned record for the current label event proves gate ownership."""
        from scripts.ci import codex_gate as legacy
        real = legacy.FIXER_RECEIPTS
        self.gh.pr["labels"] = [{"name": "ci:full"}]
        self.gh.events = [{"id": 700, "event": "labeled", "label": {"name": "ci:full"}}]
        if owned:
            self.gh.add_comment(real.marker("ci-owned", {"head": HEAD, "event": 700}))
        self.receipts.revoke_owned_ci = Mock(side_effect=real.revoke_owned_ci)

        def delete(path):
            self.assertEqual(path, f"{PREFIX}/issues/{PR}/labels/ci:full")
            self.gh.pr["labels"] = []
        self.gh.delete_read_token = Mock(side_effect=delete)

    def test_edited_reservation_revokes_owned_full_ci_label_before_refusing(self):
        """An edited reservation refuses only after removing a gate-owned ci:full; a human label stays."""
        for owned in (True, False):
            with self.subTest(owned=owned):
                self.setUp()
                self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
                self.label_ci_full(owned)
                receipt = next(c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-request"))
                receipt["updated_at"] = "2026-10-16T00:03:00Z"
                with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
                    self.reconcile()
                self.receipts.revoke_owned_ci.assert_called()
                if owned:
                    self.gh.delete_read_token.assert_called_once()
                    self.assertEqual(self.gh.pr["labels"], [])
                    self.assertTrue(any("claude-fixer-ci-invalidated:" in c["body"] for c in self.gh.comments))
                else:
                    self.gh.delete_read_token.assert_not_called()
                    self.assertEqual(self.gh.pr["labels"], [{"name": "ci:full"}])
                self.assertEqual(len(self.gh.mentions), 1)
                self.assertEqual(self.gh.dispatches, [])

    def test_edited_dispatch_blocks_review_request_after_pause_change(self):
        """request() reads only request records; the all-kinds scan stops a new mention on an edited dispatch."""
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        key = self.candidate().key
        self.add_edited("dispatch", {"candidate": key, "kind": "ci", "nonce": "2" * 32})
        self.receipts.pause = 1
        with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
            self.reconcile()
        self.assertEqual(len(self.gh.mentions), 1)
        requests = [c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-request")]
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.gh.dispatches, [])

    def test_edited_mediate_intent_blocks_request_and_dispatch(self):
        """An edited mediate-intent stops a review request and a full dispatch, not only a handoff."""
        for path in ("request", "dispatch"):
            with self.subTest(path=path):
                self.setUp()
                if path == "request":
                    self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
                    self.receipts.pause = 1
                else:
                    self.prime_ready()
                self.add_edited("mediate-intent", {"head": HEAD, "review": 1, "pause": 0, "nonce": "3" * 32})
                with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
                    self.reconcile()
                self.assertEqual(len(self.gh.mentions), 1)
                requests = [c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-request")]
                self.assertEqual(len(requests), 1)
                self.assertEqual(self.gh.dispatches, [])
                self.legacy._act.assert_not_called()

    def test_reservation_edited_after_mediate_check_revokes_before_refusing(self):
        """reconcile()'s own scan, not only mediate()'s, removes the owned label before refusing."""
        self.assertEqual(self.reconcile(), "awaiting-independent-candidate-review")
        self.label_ci_full(owned=True)
        original = gate.Coordinator.ineligible

        def edit_then_check(coordinator):
            receipt = next(c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-request"))
            receipt["updated_at"] = "2026-10-16T00:03:00Z"
            return original(coordinator)

        with (patch.object(gate.Coordinator, "ineligible", edit_then_check),
              self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually")):
            self.reconcile()
        self.gh.delete_read_token.assert_called_once()
        self.assertEqual(self.gh.pr["labels"], [])
        self.assertEqual(len(self.gh.mentions), 1)

    def test_dispatch_edited_before_review_request_blocks_mention(self):
        """The refreshed listing handed to request() is scanned for every kind, not only requests."""
        self.gh.add_comment(FakeReceipts.marker("candidate-dispatch",
            {"candidate": self.candidate().key, "kind": "ci", "nonce": "2" * 32}))
        self.edit_on_revoke("dispatch")
        with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
            self.reconcile()
        self.assertEqual(self.gh.mentions, [])
        self.assertEqual([c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-request")], [])

    def test_request_edited_before_mediate_intent_blocks_handoff(self):
        """mediate() scans its refreshed listing for every kind before recording an intent."""
        self.prime_head_mediate()
        self.gh.add_comment(FakeReceipts.marker("candidate-request",
            {"candidate": self.candidate().key, "nonce": "4" * 32, "pause": 0}))
        self.edit_on_revoke("request")
        with self.assertRaisesRegex(ValueError, "edited or unversioned candidate reservation; reconcile manually"):
            self.reconcile()
        self.assertEqual([c for c in self.gh.comments if self.receipts.records(c["body"], "candidate-mediate-intent")], [])
        self.legacy._act.assert_not_called()


if __name__ == "__main__":
    unittest.main()
