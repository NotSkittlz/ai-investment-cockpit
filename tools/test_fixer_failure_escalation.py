#!/usr/bin/env python3
"""Provider-free failure provenance regressions, not live qualification."""
import copy
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("escalation", ROOT / "scripts/ci/fixer_failure_escalation.py")
f = load("fixtures", ROOT / "tools/test_claude_fixer_receipts.py")
REPO, HEAD = "acme/example", "a" * 40


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.threads = [f.thread()]
        self.comments = f.root_comments(self.threads)
        self.history = []
        self.items = f.findings(self.threads)
        if hasattr(f, "prepare"):
            self.items = f.prepare(self.items, history=self.history)
        self.key = m.receipts.attempt_key(HEAD, self.items)
        for run in (10, 11, 12):
            self.history.append({"id": 100 + run,
                                 "user": f.actor("github-actions[bot]", f.m.ACTIONS_ID),
                                 "body": f.m.marker("attempt", {**self.key, "run": str(run), "attempt": "1"})})
        self.pr = {"number": 7, "state": "open", "draft": False,
                   "user": {"login": "owner", "type": "User"},
                   "head": {"sha": HEAD, "ref": "fix", "repo": {"full_name": REPO}}}
        self.run = {"id": 10, "run_attempt": 1, "workflow_id": 99, "path": m.WORKFLOW,
                    "event": "pull_request_review", "status": "completed", "conclusion": "failure",
                    "head_sha": HEAD, "head_branch": "fix", "repository": {"full_name": REPO},
                    "head_repository": {"full_name": REPO}, "pull_requests": [{"number": 7}]}
        self.job = {"id": 123, "run_id": 10, "run_attempt": 1, "name": "devin-review-fix", "status": "completed", "conclusion": "failure",
                    "steps": [{"number": 8, "name": m.CONTEXT_PREFIX + HEAD + ":" + self.key["fingerprint"], "conclusion": "success"},
                              {"number": 9, "name": "Run anthropics/claude-code-action@v1", "conclusion": "failure"}]}
        self.calls = []
        self.active = 0
        self.private = False
        self.default_sha = "trusted"

    def read(self, path, body=None, *, public=False):
        self.calls.append((path, body, public))
        if path == f"repos/{REPO}/pulls/7":
            return copy.deepcopy(self.pr)
        if path.endswith("/permission"):
            return {"permission": "write"}
        if "/issues/7/comments?" in path:
            return copy.deepcopy(self.history)
        if "/pulls/7/comments?" in path:
            return copy.deepcopy(self.comments)
        if path == "graphql":
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": copy.deepcopy(self.threads), "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}}
        if path == f"repos/{REPO}":
            return {"private": self.private, "default_branch": "main"}
        if "/contents/" in path:
            return {"sha": self.default_sha if path.endswith("ref=main") else "trusted"}
        if path.endswith("/actions/workflows/claude.yml"):
            return {"id": 99, "path": m.WORKFLOW}
        if "/actions/workflows/claude.yml/runs?" in path:
            return {"total_count": self.active, "workflow_runs": []}
        if "/jobs?" in path:
            return {"total_count": 1, "jobs": [copy.deepcopy(self.job)]}
        if "/actions/runs/10/" in path:
            return copy.deepcopy(self.run)
        if "/actions/runs/" in path:
            return copy.deepcopy(self.run) | {"id": int(path.split("/runs/")[1].split("/")[0]), "conclusion": "success"}
        if "/compare/" in path:
            return {"status": "ahead"}
        raise AssertionError(path)

    def collect(self, read=None):
        evidence = m.Evidence(REPO, 7, read or self.read)
        result = evidence.collect()
        return result if not result or evidence.fresh() else {}

    def test_exhaustion_plus_one_independently_bound_failure_admits_only_real_root(self):
        result = self.collect()
        self.assertEqual(set(result), {"thread1"})
        self.assertIn("not three model failures", result["thread1"]["description"])
        self.assertIn("/attempts/1#job-123", result["thread1"]["description"])
        self.assertTrue(all(body is None or path == "graphql" for path, body, _ in self.calls))
        self.assertTrue(all("logs" not in path for path, _, _ in self.calls))

    def test_repository_dispatch_binds_default_workflow_and_reserved_pr_context(self):
        self.run.update(event="repository_dispatch", head_sha="d" * 40,
                        head_branch="main", pull_requests=[])
        self.assertEqual(set(self.collect()), {"thread1"})
        self.assertTrue(any(path.endswith("ref=" + "d" * 40) for path, _, _ in self.calls))
        original = copy.deepcopy(self.run)
        for key, value in (("head_sha", "bad"), ("head_branch", "fix"),
                           ("head_repository", {"full_name": "evil/repo"}),
                           ("event", "workflow_dispatch"), ("workflow_id", 100)):
            with self.subTest(key=key):
                self.run = original | {key: value}
                self.assertEqual(self.collect(), {})
        self.run = original
        def old_workflow(path, *args, **kwargs):
            if path.endswith("ref=" + "d" * 40):
                return {"sha": "old-untrusted-workflow"}
            return self.read(path, *args, **kwargs)
        self.assertEqual(self.collect(old_workflow), {})
        self.job["steps"][0]["name"] = m.CONTEXT_PREFIX + HEAD + ":" + "f" * 64
        self.assertEqual(self.collect(), {})
        self.job["steps"][0]["name"] = m.CONTEXT_PREFIX + HEAD + ":" + self.key["fingerprint"]
        self.history.clear()
        self.assertEqual(self.collect(), {})

    def test_review_comment_event_cannot_prove_the_review_fixer_job(self):
        self.run["event"] = "pull_request_review_comment"
        self.assertEqual(self.collect(), {})

    def test_reservations_success_skip_and_diagnostic_prose_are_not_failures(self):
        self.history.append({"id": 999, "body": "Claude fixer terminal audit: turn-limit. needs human",
                             "user": f.actor("github-actions[bot]", f.m.ACTIONS_ID)})
        for status in ("success", "skipped", "neutral", "startup_failure"):
            with self.subTest(status=status):
                self.run["conclusion"] = status
                self.assertEqual(self.collect(), {})

    def test_ref_qualified_attempt_path_preserves_all_other_provenance_checks(self):
        for suffix in ("", "@main", "@refs/heads/fix", "@refs/pull/7/merge", "@" + HEAD):
            with self.subTest(suffix=suffix):
                self.run["path"] = m.WORKFLOW + suffix
                self.assertEqual(set(self.collect()), {"thread1"})
                self.run["workflow_id"] = 100
                self.assertEqual(self.collect(), {})
                self.run["workflow_id"] = 99
        for path in (None, "", m.WORKFLOW + "@", m.WORKFLOW + "@main\n", "other.yml@main",
                     m.WORKFLOW + ".evil@main", "evil/" + m.WORKFLOW + "@main"):
            with self.subTest(path=path):
                self.run["path"] = path
                self.assertEqual(self.collect(), {})

    def test_no_exhaustion_and_forged_reservation_identity_refused(self):
        for user in (f.actor("claude[bot]", f.m.CLAUDE_ID),
                     f.actor("github-actions[bot]", 1),
                     {"login": "github-actions[bot]", "id": f.m.ACTIONS_ID, "type": "User"}):
            previous = copy.deepcopy(self.history)
            self.history[-1]["user"] = user
            self.assertEqual(self.collect(), {})
            self.history = previous
        self.history.pop()
        self.assertEqual(self.collect(), {})

    def test_wrong_run_attempt_workflow_event_repo_head_pr_or_branch_refused(self):
        for key, value in {"id": 15, "run_attempt": 2, "workflow_id": 100, "path": "other.yml",
                           "event": "workflow_dispatch", "status": "in_progress", "head_sha": "b" * 40,
                           "head_branch": "other", "repository": {"full_name": "evil/repo"},
                           "head_repository": {"full_name": "evil/repo"}, "pull_requests": [{"number": 8}]}.items():
            with self.subTest(key=key):
                old = self.run[key]
                self.run[key] = value
                self.assertEqual(self.collect(), {})
                self.run[key] = old

    def test_wrong_job_context_terminal_only_failure_and_noop_action_refused(self):
        for change in ({"name": "claude-mention"}, {"run_id": 44}, {"run_attempt": 2}, {"status": "in_progress"}, {"conclusion": "success"}):
            old = copy.deepcopy(self.job)
            self.job.update(change)
            self.assertEqual(self.collect(), {})
            self.job = old
        for index, field, value in ((0, "name", m.CONTEXT_PREFIX + HEAD + ":" + "f" * 64),
                                     (0, "conclusion", "failure"), (0, "number", 10),
                                     (1, "conclusion", "skipped"), (1, "conclusion", "success")):
            old = copy.deepcopy(self.job)
            self.job["steps"][index][field] = value
            self.assertEqual(self.collect(), {})
            self.job = old

    def test_timeout_is_failure_but_human_or_concurrency_cancellation_is_not(self):
        self.run["conclusion"] = self.job["conclusion"] = self.job["steps"][1]["conclusion"] = "timed_out"
        self.assertEqual(set(self.collect()), {"thread1"})
        self.job["steps"][1]["conclusion"] = "cancelled"
        self.assertEqual(set(self.collect()), {"thread1"})  # parent job/run independently prove timeout
        self.run["conclusion"] = self.job["conclusion"] = self.job["steps"][1]["conclusion"] = "cancelled"
        self.assertEqual(self.collect(), {})

    def test_closed_draft_fork_bot_author_and_revoked_author_permission_refused(self):
        for key, value in (("state", "closed"), ("draft", True), ("user", {"login": "bot", "type": "Bot"}),
                           ("head", self.pr["head"] | {"repo": {"full_name": "fork/repo"}})):
            old = copy.deepcopy(self.pr)
            self.pr[key] = value
            self.assertEqual(self.collect(), {})
            self.pr = old
        self.assertEqual(self.collect(lambda path, *a, **kw: {"permission": "read"} if path.endswith("/permission") else self.read(path, *a, **kw)), {})

    def test_changed_resolved_outdated_or_analysis_roots_do_not_reuse_budget(self):
        for field in ("isResolved", "isOutdated"):
            original = copy.deepcopy(self.threads)
            self.threads[0][field] = True
            try:
                self.assertEqual(self.collect(), {})
            except m.Unverified:
                self.assertTrue(hasattr(f, "prepare"))  # immutable observation needs locked journal writer
            self.threads = original
        self.threads = [f.thread('<!-- devin-review-comment {"kind":"analysis"} -->\nBug')]
        self.comments = f.root_comments(self.threads)
        try:
            self.assertEqual(self.collect(), {})
        except m.Unverified:
            self.assertTrue(hasattr(f, "prepare"))

    def test_snapshot_change_after_provenance_read_prevents_delivery(self):
        def race(path, *args, **kwargs):
            result = self.read(path, *args, **kwargs)
            if "/jobs?" in path:
                self.pr["head"]["sha"] = "b" * 40
            return result
        self.assertEqual(self.collect(race), {})

    def test_active_or_queued_writer_refuses_escalation(self):
        self.active = 1
        self.assertEqual(self.collect(), {})
        self.assertFalse(any("branch=" in path for path, _, _ in self.calls))

    def test_foreign_workflow_or_nonpublic_repo_refuses_without_permission_expansion(self):
        self.default_sha = "different"
        with self.assertRaises(m.Unverified):
            self.collect()
        self.default_sha, self.private = "trusted", True
        with self.assertRaises(m.Unverified):
            self.collect()
        self.assertFalse(any(public for _, _, public in self.calls))

    def test_denied_reads_and_duplicate_or_capped_snapshots_fail_closed(self):
        def denied(path, *args, **kwargs):
            if "/jobs?" in path:
                raise m.Unverified("denied")
            return self.read(path, *args, **kwargs)
        with self.assertRaises(m.Unverified):
            self.collect(denied)
        self.history.append(copy.deepcopy(self.history[-1]))
        with self.assertRaises(m.Unverified):
            self.collect()
        with self.assertRaises(m.Unverified):
            m.listing(lambda _: [{"id": n} for n in range(1, 101)], "fixture")

    def test_read_budget_is_bounded(self):
        with patch.object(m.time, "monotonic", side_effect=[0, 61]), self.assertRaises(m.Unverified):
            m.Reader().read("fixture")
        reader = m.Reader()
        reader.calls = 60
        with self.assertRaises(m.Unverified):
            reader.read("fixture")

    def test_valid_existing_receipts_exclude_handled_and_human_handoff_roots(self):
        for status in ("addressed", "needs-human"):
            receipt = {**self.items[0], "base": "b" * 40, "commit": HEAD, "status": status}
            self.comments = f.root_comments(self.threads) + [f.reply(receipt) | {"id": 900}]
            def read(path, *args, **kwargs):
                if path.endswith(f"{HEAD}...{HEAD}"):
                    return {"status": "identical"}
                return self.read(path, *args, **kwargs)
            self.assertEqual(self.collect(read), {})

    def test_invalid_receipt_compare_is_not_a_global_read_failure(self):
        receipt = {**self.items[0], "base": "b" * 40, "commit": "c" * 40, "status": "addressed"}
        self.comments += [f.reply(receipt) | {"id": 900}]
        for status in (404, 422):
            def read(path, *args, status=status, **kwargs):
                if "/compare/" in path:
                    raise m.ReadUnavailable(status)
                return self.read(path, *args, **kwargs)
            self.assertEqual(set(self.collect(read)), {"thread1"})

    def test_real_reader_budget_allows_initial_evidence_and_four_fresh_rechecks(self):
        import io
        import subprocess
        from types import SimpleNamespace
        for denied_public in (False, True):
            def transport(args, denied_public=denied_public, **kwargs):
                if denied_public and "/actions/" in args[2]:
                    raise subprocess.CalledProcessError(1, "gh", stderr="(HTTP 403)")
                body = json.loads(kwargs["input"]) if kwargs.get("input") else None
                return SimpleNamespace(stdout=json.dumps(self.read(args[2], body)))
            def public_transport(request, **_kwargs):
                self.assertIsNone(request.get_header("Authorization"))
                return io.BytesIO(json.dumps(self.read(request.full_url.removeprefix("https://api.github.com/"))).encode())
            with patch.dict(m.os.environ, {"GH_TOKEN": "fixture"}), patch.object(m.subprocess, "run", side_effect=transport), patch.object(m.urllib.request, "urlopen", side_effect=public_transport):
                reader = m.Reader()
                evidence = m.Evidence(REPO, 7, reader.read)
                self.assertTrue(evidence.collect())
                for _ in range(4):
                    self.assertTrue(evidence.fresh())
                self.assertLessEqual(reader.calls, 60)
                self.assertEqual(reader.public_anonymous, denied_public)

    def test_bounded_retry_and_compare_http_status_preservation(self):
        import subprocess
        from types import SimpleNamespace
        for status in (429, 500, 503):
            error = subprocess.CalledProcessError(1, "gh", stderr=f"(HTTP {status})")
            with patch.object(m.subprocess, "run", side_effect=[error, SimpleNamespace(stdout="{}")]) as run, patch.object(m.time, "sleep"):
                self.assertEqual(m.Reader().read("fixture"), {})
                self.assertEqual(run.call_count, 2)
        error = subprocess.CalledProcessError(1, "gh", stderr="(HTTP 404)")
        with patch.object(m.subprocess, "run", side_effect=error), self.assertRaises(m.ReadUnavailable) as caught:
            m.Reader().read("fixture")
        self.assertEqual(caught.exception.status, 404)

    def test_new_head_keeps_genuine_pending_findings_fresh_without_old_escalation(self):
        self.pr["head"]["sha"] = "b" * 40
        self.assertEqual(self.collect(), {})
        self.assertEqual(len(m.receipts.findings(self.threads, self.comments)), 1)
        self.assertEqual(m.receipts.attempt_count(self.history, m.receipts.attempt_key("b" * 40, self.items)), 0)

    def test_workflow_emits_context_from_actual_locked_eligibility_and_embeds_consumer(self):
        import re
        import textwrap
        text = (ROOT / ".github/workflows/claude.yml").read_text(encoding="utf-8")
        self.assertEqual(text.count('name: "Fixer attempt ${{ steps.eligibility.outputs.attempt_key }}"'), 1)
        embedded = textwrap.dedent(text.split("<<'CLAUDE_FIXER_RECEIPTS'\n")[1].split("          CLAUDE_FIXER_RECEIPTS")[0]).rstrip()
        self.assertEqual(embedded, (ROOT / "scripts/ci/claude_fixer_receipts.py").read_text().rstrip())
        # Parse the supported indentation/step schema without adding a YAML
        # dependency to the existing standard-library-only event-path job.
        job = re.split(r"\n  [A-Za-z0-9_-]+:\n", text.split("\n  devin-review-fix:\n", 1)[1])[0]
        header, steps_text = job.split("    steps:\n", 1)
        self.assertNotRegex(header, r"(?m)^    (name|strategy|uses):")
        steps = re.split(r"(?m)^      - ", steps_text)[1:]
        action = [(i, s) for i, s in enumerate(steps) if f"        uses: {m.ACTION_USES}\n" in s]
        context = [(i, s) for i, s in enumerate(steps) if s.startswith('name: "Fixer attempt ${{ steps.eligibility.outputs.attempt_key }}"\n')]
        eligibility = [s for s in steps if "        id: eligibility\n" in s or s.startswith("id: eligibility\n")]
        self.assertEqual(len(action), 1)
        self.assertEqual(len(context), 1)
        self.assertEqual(len(eligibility), 1)
        self.assertTrue(action[0][1].startswith("id: claude\n"))
        self.assertNotRegex(action[0][1], r"(?m)^        name:")
        self.assertIn("        id: push_guard\n", context[0][1])
        self.assertLess(context[0][0], action[0][0])
        watcher = (ROOT / ".github/workflows/stuck-thread-watch.yml").read_text()
        self.assertIn("contents: read", watcher)
        self.assertIn("GH_TOKEN: ${{ github.token }}", watcher)
        self.assertIn("uses: actions/checkout@v4", watcher)
        if "sparse-checkout:" in watcher:
            sparse = watcher.split("          sparse-checkout: |\n", 1)[1].split("          sparse-checkout-cone-mode:", 1)[0]
            for source in (".github/scripts/stuck_thread_watch.py", "scripts/ci/fixer_failure_escalation.py", "scripts/ci/claude_fixer_receipts.py"):
                self.assertIn("            " + source + "\n", sparse)
            self.assertIn("ref: ${{ github.event.repository.default_branch }}", watcher)
            self.assertIn("persist-credentials: false", watcher)


if __name__ == "__main__":
    unittest.main()
