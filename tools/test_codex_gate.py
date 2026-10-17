#!/usr/bin/env python3
"""Tests for the Codex CI/fixer gate.

    python -X utf8 -B tools/test_codex_gate.py
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "ci" / "codex_gate.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "codex-gate.yml"
CLAUDE_WORKFLOW = ROOT / ".github" / "workflows" / "claude.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RUST_WORKFLOW = ROOT / ".github" / "workflows" / "rust.yml"
PR_LINT_WORKFLOW = ROOT / ".github" / "workflows" / "pr-lint.yml"
RETRIGGER_WORKFLOW = ROOT / ".github" / "workflows" / "ci-label-retrigger.yml"
SCAN_WORKFLOW = ROOT / ".github" / "workflows" / "conflict-scan.yml"
ACTIONLINT = ROOT / ".github" / "actionlint.yaml"
AGENTS = ROOT / "AGENTS.md"
REPO = "acme/example"
SHA = "a" * 40


def load_gate():
    spec = importlib.util.spec_from_file_location("codex_gate", GATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = load_gate()


# These router tests model a clean remote snapshot unless a case explicitly
# injects pending findings. Real receipt/GraphQL behavior has its own suite.
_remote_state = patch.object(mod.FIXER_RECEIPTS, "live_state", return_value=([], []))


_sleep = patch.object(mod.FIXER_RECEIPTS.time, "sleep")


def setUpModule():
    _remote_state.start()
    _sleep.start()


def tearDownModule():
    _remote_state.stop()
    _sleep.stop()


def pr_payload(
    *,
    number: int = 42,
    state: str = "open",
    draft: bool = False,
    head_repo: str = REPO,
    head_ref: str = "feature/fix",
    head_sha: str = SHA,
) -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "draft": draft,
        "head": {"ref": head_ref, "sha": head_sha, "repo": {"full_name": head_repo}},
    }


def comment(
    body: str,
    created: str | None = None,
    login: str = "github-actions[bot]",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "body": body,
        "user": {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"},
    }
    if created:
        item["created_at"] = created
    return item


def review(body: str, *, login: str = "chatgpt-codex-connector[bot]", sha: str = SHA) -> dict:
    return {"body": body, "user": {"login": login}, "commit_id": sha}


def label_event(
    login: str,
    *,
    unlabeled: bool = False,
    created: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "event": "unlabeled" if unlabeled else "labeled",
        "actor": {"login": login},
        "label": {"name": "ci:full"},
    }
    if created:
        item["created_at"] = created
    return item


class FakeGH:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.dispatches: list[tuple[str, dict[str, Any], str]] = []
        self.workflows: list[tuple[str, dict[str, str]]] = []
        self.deletes: list[str] = []
        self.comments: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.prs: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.fail_workflow = False
        self.writer = "github-actions[bot]"

    def writer_login(self) -> str:
        return self.writer

    def get(self, path: str) -> Any:
        if path.rstrip("/") in {"user", "/user"} or path.endswith("/user"):
            return {"login": "github-actions[bot]"}
        if "/events" in path:
            return list(self.events)
        if "/comments" in path:
            return list(self.comments)
        if "/reviews" in path:
            return list(self.reviews)
        if path.startswith(f"repos/{REPO}/pulls?") or path.startswith(f"repos/{REPO}/pulls&"):
            return list(self.prs)
        if "/pulls/" in path and path.rstrip("0123456789").endswith("/pulls/"):
            number = path.rsplit("/", 1)[-1].split("?")[0]
            for item in self.prs:
                if str(item.get("number")) == number:
                    return item
            return pr_payload(number=int(number) if number.isdigit() else 42)
        return []

    def post(self, path: str, body: dict[str, Any]) -> Any:
        self.posts.append((path, body))
        if path.endswith("/labels"):
            self.events.append({"id": len(self.events) + 1, "event": "labeled", "label": {"name": "ci:full"}, "actor": {"login": self.writer}})
        return {"id": len(self.posts)}

    def post_read_token(self, path, body):
        self.comments.append({"id": max([1000] + [c.get("id", 0) for c in self.comments]) + 1, "body": body["body"],
            "user": {"login": "github-actions[bot]", "type": "Bot", "id": 41898282}})
        return self.comments[-1]

    def delete(self, path: str) -> None:
        self.deletes.append(path)
        self.events.append({"id": max([0] + [e.get("id", 0) for e in self.events]) + 1,
                            "event": "unlabeled", "label": {"name": "ci:full"}})

    def delete_read_token(self, path):
        self.delete(path)
        self.events[-1]["actor"] = {"login": "github-actions[bot]", "type": "Bot", "id": 41898282}

    def dispatch(self, event_type: str, payload: dict[str, Any], repository: str) -> None:
        self.dispatches.append((event_type, payload, repository))

    def workflow_run(self, workflow: str, fields: dict[str, str]) -> None:
        if self.fail_workflow:
            raise OSError("workflow missing")
        self.workflows.append((workflow, fields))


class WriterIdentityTests(unittest.TestCase):
    def test_writer_login_uses_write_token(self) -> None:
        gh = mod.GitHub("reader", "writer")
        with patch.object(gh, "_run", return_value='{"login": "trigger-author"}') as run:
            self.assertEqual(gh.writer_login(), "trigger-author")
        run.assert_called_once_with(["api", "user"], token="writer")

    def test_failed_writer_lookup_does_not_trust_reader(self) -> None:
        gh = FakeGH()
        buf = io.StringIO()
        with patch.dict(os.environ, {"CODEX_GATE_LOGINS": ""}), \
             patch.object(gh, "writer_login", side_effect=OSError("lookup failed")), \
             patch.object(gh, "get") as get, patch("sys.stdout", buf):
            authors = mod.marker_authors(gh)
        self.assertEqual(authors, frozenset({"github-actions[bot]"}))
        get.assert_not_called()
        self.assertEqual(buf.getvalue(), "")

    def test_pat_request_marker_prevents_duplicate_request(self) -> None:
        gh = FakeGH()
        gh.writer = "trigger-author"
        gh.comments = [comment(mod.build_request_comment(SHA), login=gh.writer)]
        with patch.dict(os.environ, {"CODEX_GATE_LOGINS": ""}):
            rc = mod.run(event_name="pull_request", event={"pull_request": pr_payload()},
                         repository=REPO, pat="not-a-real-secret", gh=gh)
        self.assertEqual(rc, 0)
        self.assertEqual(gh.posts, [])
        self.assertEqual(gh.dispatches, [])

    def test_pat_action_marker_prevents_repeat_fixer(self) -> None:
        gh = FakeGH()
        gh.writer = "trigger-author"
        gh.prs = [pr_payload()]
        gh.comments = [
            comment(mod.build_request_comment(SHA), "2026-10-15T11:00:00Z", gh.writer),
            comment(mod.action_marker(SHA, "FIX"), "2026-10-15T11:30:00Z", gh.writer),
        ]
        with patch.dict(os.environ, {"CODEX_GATE_LOGINS": ""}):
            rc = mod.run(event_name="schedule", event={}, repository=REPO,
                         pat="not-a-real-secret", gh=gh,
                         now=datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(rc, 0)
        self.assertEqual(gh.posts, [])
        self.assertEqual(gh.dispatches, [])


class ParseVerdictTests(unittest.TestCase):
    def test_standalone_last_line_only(self) -> None:
        self.assertEqual(mod.parse_verdict("nits\nCODEX-GATE: MEDIATE\n"), "MEDIATE")
        self.assertEqual(mod.parse_verdict("CODEX-GATE: FIX"), "FIX")
        self.assertEqual(mod.parse_verdict(""), "")
        self.assertEqual(mod.parse_verdict(None), "")

    def test_quoted_or_multiple_lines_are_rejected(self) -> None:
        self.assertEqual(mod.parse_verdict("CODEX-GATE: FIX\nCODEX-GATE: READY"), "")
        self.assertEqual(mod.parse_verdict("see CODEX-GATE: MEDIATE please"), "")
        self.assertEqual(mod.parse_verdict("CODEX-GATE: NOPE"), "")
        self.assertEqual(
            mod.parse_verdict(
                "Options were CODEX-GATE: FIX or CODEX-GATE: READY; more investigation needed."
            ),
            "",
        )
        self.assertEqual(
            mod.parse_verdict("CODEX-GATE: FIX\nmore investigation needed."),
            "",
        )


class RequestDecisionTests(unittest.TestCase):
    def test_posts_once_per_sha(self) -> None:
        pr = pr_payload()
        self.assertEqual(mod.decide_request(pr, REPO, []), "post")
        existing = [comment(mod.gate_marker(SHA) + "\n@codex review")]
        self.assertEqual(mod.decide_request(pr, REPO, existing), "exists")

    def test_skips_fork_draft_protected(self) -> None:
        self.assertEqual(mod.decide_request(pr_payload(head_repo="evil/fork"), REPO, []), "skip-ineligible-pr")
        self.assertEqual(mod.decide_request(pr_payload(draft=True), REPO, []), "skip-ineligible-pr")
        self.assertEqual(mod.decide_request(pr_payload(head_ref="main"), REPO, []), "skip-ineligible-pr")
        self.assertEqual(mod.decide_request(pr_payload(state="closed"), REPO, []), "skip-ineligible-pr")


class VerdictDecisionTests(unittest.TestCase):
    def test_fix_ready_mediate(self) -> None:
        self.assertEqual(
            mod.decide_verdict(sha=SHA, review_body="CODEX-GATE: FIX", reviews=[], comments=[], day="2026-10-15"),
            "skip-retired-fix",
        )
        self.assertEqual(
            mod.decide_verdict(sha=SHA, review_body="CODEX-GATE: READY", reviews=[], comments=[], day="2026-10-15"),
            "READY",
        )
        self.assertEqual(
            mod.decide_verdict(sha=SHA, review_body="nits\nCODEX-GATE: MEDIATE\n", reviews=[], comments=[], day="2026-10-15"),
            "MEDIATE",
        )

    def test_already_acted_and_no_daily_cap(self) -> None:
        acted = [comment(mod.action_marker(SHA, "FIX"))]
        self.assertEqual(
            mod.decide_verdict(sha=SHA, review_body="CODEX-GATE: FIX", reviews=[], comments=acted, day="2026-10-15"),
            "skip-retired-fix",
        )
        cap = [
            comment(mod.fixer_run_marker("2026-10-15", f"{i:040x}"))
            for i in range(3)
        ]
        self.assertEqual(
            mod.decide_verdict(sha=SHA, review_body="CODEX-GATE: FIX", reviews=[], comments=cap, day="2026-10-15"),
            "skip-retired-fix",
        )
        self.assertEqual(
            mod.decide_verdict(sha=SHA, review_body="CODEX-GATE: READY", reviews=[], comments=cap, day="2026-10-15"),
            "READY",
        )

    def test_legacy_fix_marker_cannot_suppress_ready_or_mediate(self) -> None:
        comments = [comment(mod.action_marker(SHA, "FIX"))]
        for verdict in ("READY", "MEDIATE"):
            self.assertEqual(mod.decide_verdict(sha=SHA,
                review_body="CODEX-GATE: " + verdict, reviews=[],
                comments=comments, day="2026-10-15"), verdict)
            self.assertEqual(mod.decide_recovery(sha=SHA,
                reviews=[review("CODEX-GATE: " + verdict)], comments=comments,
                now=datetime(2026, 10, 15, tzinfo=timezone.utc), day="2026-10-15"), verdict)

    def test_mediation_review_without_gate_line_is_skipped(self) -> None:
        comments = [
            comment("<!-- codex-mediation:abc -->\n@codex review\nmediate", created="2026-10-15T12:00:00Z"),
        ]
        self.assertEqual(
            mod.decide_verdict(
                sha=SHA,
                review_body="advisory notes, no gate line",
                reviews=[],
                comments=comments,
                day="2026-10-15",
            ),
            "skip-mediation-review",
        )


class TimeoutDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    def _request(self, age_min: int, sha: str = SHA) -> dict[str, Any]:
        created = (self.now - timedelta(minutes=age_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return comment(mod.gate_marker(sha) + "\n@codex review", created)

    def test_waiting_under_20_minutes(self) -> None:
        self.assertEqual(
            mod.decide_recovery(
                sha=SHA, comments=[self._request(19)], reviews=[], now=self.now, day="2026-10-15"
            ),
            "skip-no-verdict",
        )

    def test_old_request_without_verdict_never_fixes(self) -> None:
        self.assertEqual(
            mod.decide_recovery(
                sha=SHA, comments=[self._request(20)], reviews=[], now=self.now, day="2026-10-15"
            ),
            "skip-no-verdict",
        )

    def test_elapsed_time_never_authorizes_fixer(self) -> None:
        for minutes in (0, 19, 20, 60, 1440, 10080):
            for reviews in ([], [review("CODEX-GATE: FIX")]):
                result = mod.decide_recovery(sha=SHA, comments=[self._request(minutes)],
                    reviews=reviews, now=self.now, day="2026-10-15")
                self.assertIn(result, {"skip-no-verdict", "skip-retired-fix"})

    def test_timeout_respects_codex_verdict(self) -> None:
        reviews = [review("looks good\nCODEX-GATE: READY")]
        self.assertEqual(
            mod.decide_recovery(
                sha=SHA, comments=[self._request(30)], reviews=reviews, now=self.now, day="2026-10-15"
            ),
            "READY",
        )

    def test_timeout_has_no_daily_cap(self) -> None:
        comments = [self._request(30)] + [
            comment(mod.fixer_run_marker("2026-10-15", f"{i:040x}")) for i in range(3)
        ]
        self.assertEqual(
            mod.decide_recovery(sha=SHA, comments=comments, reviews=[], now=self.now, day="2026-10-15"),
            "skip-no-verdict",
        )

    def test_old_request_with_mediation_prompt_never_fixes(self) -> None:
        comments = [
            self._request(30),
            comment(
                "<!-- codex-mediation:abc -->\n@codex review\nmediate this thread",
                created=(self.now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        ]
        self.assertEqual(
            mod.decide_recovery(
                sha=SHA, comments=comments, reviews=[], now=self.now, day="2026-10-15"
            ),
            "skip-no-verdict",
        )


class ApplyVerdictTests(unittest.TestCase):
    def _apply(self, verdict: str) -> tuple[list[str], list[str], list[str], list[str]]:
        posted: list[str] = []
        labels: list[str] = []
        dispatched: list[str] = []
        mediated: list[str] = []
        logs = mod.apply_verdict(
            verdict,
            repository=REPO,
            pr_number="42",
            sha=SHA,
            comments=[],
            day="2026-10-15",
            post_comment=posted.append,
            add_label=labels.append,
            file_decision=lambda: (mediated.append("linear") or "https://linear.app/issue/CSL-1"),
        )
        return logs, posted, labels, dispatched, mediated  # type: ignore[return-value]

    def test_fix_never_dispatches_or_posts_terminal_marker(self) -> None:
        logs, posted, labels, dispatched, mediated = self._apply("FIX")
        self.assertTrue(any("skip-retired-fix" in line for line in logs))
        self.assertEqual(dispatched, [])
        self.assertEqual(posted, [])
        self.assertEqual(labels, [])
        self.assertEqual(mediated, [])

    def test_ready_labels(self) -> None:
        _, posted, labels, dispatched, mediated = self._apply("READY")
        self.assertEqual(labels, ["ci:full"])
        self.assertEqual(dispatched, [])
        self.assertEqual(mediated, [])
        self.assertIn("READY", posted[0])

    def test_mediate_handoff(self) -> None:
        _, posted, labels, dispatched, mediated = self._apply("MEDIATE")
        self.assertEqual(mediated, ["linear"])
        self.assertEqual(dispatched, [])
        self.assertEqual(labels, [])
        self.assertIn("codex-gate-mediate:42:" + SHA, posted[0])
        self.assertIn("needs a human", posted[0])
        self.assertIn("https://linear.app/issue/CSL-1", posted[0])

    def test_mediate_skips_approved_pr(self) -> None:
        posted: list[str] = []
        mediated: list[str] = []
        logs = mod.apply_verdict(
            "MEDIATE",
            repository=REPO,
            pr_number="42",
            sha=SHA,
            comments=[],
            day="2026-10-15",
            post_comment=posted.append,
            add_label=lambda _n: None,
            file_decision=lambda: (mediated.append("linear") or "https://linear.app/issue/CSL-1"),
            pr_approved=True,
        )
        self.assertEqual(mediated, [])
        self.assertEqual(len(posted), 1)
        self.assertIn("already approved", posted[0])
        self.assertIn("codex-gate-action:" + SHA + ":MEDIATE", posted[0])
        self.assertTrue(any("mediate=skip-approved" in line for line in logs))

    def test_mediate_without_linear_url_does_not_mark(self) -> None:
        posted: list[str] = []
        logs = mod.apply_verdict(
            "MEDIATE",
            repository=REPO,
            pr_number="42",
            sha=SHA,
            comments=[],
            day="2026-10-15",
            post_comment=posted.append,
            add_label=lambda _n: None,
            file_decision=lambda: "",
        )
        self.assertEqual(len(posted), 1)
        self.assertIn("codex-gate-mediate-pending:42:" + SHA, posted[0])
        self.assertNotIn("codex-gate-action:", posted[0])
        self.assertTrue(any("mediate=retry" in line for line in logs))

        posted2: list[str] = []
        mod.apply_verdict(
            "MEDIATE",
            repository=REPO,
            pr_number="42",
            sha=SHA,
            comments=[comment(posted[0])],
            day="2026-10-15",
            post_comment=posted2.append,
            add_label=lambda _n: None,
            file_decision=lambda: "",
        )
        self.assertEqual(posted2, [])

    def test_skip_is_noop(self) -> None:
        logs, posted, labels, dispatched, mediated = self._apply("skip-waiting")
        self.assertEqual(posted, [])
        self.assertEqual(labels, [])
        self.assertEqual(dispatched, [])
        self.assertEqual(mediated, [])
        self.assertTrue(logs)

    def test_failed_side_effect_does_not_mark(self) -> None:
        posted: list[str] = []

        def boom(_label: str) -> None:
            raise OSError("dispatch failed")

        with self.assertRaises(OSError):
            mod.apply_verdict(
                "READY",
                repository=REPO,
                pr_number="42",
                sha=SHA,
                comments=[],
                day="2026-10-15",
                post_comment=posted.append,
                add_label=boom,
                file_decision=lambda: "",
            )
        self.assertEqual(posted, [])


class RunTests(unittest.TestCase):
    def test_missing_secret_notices_and_skips(self) -> None:
        gh = FakeGH()
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = mod.run(
                event_name="pull_request",
                event={"pull_request": pr_payload()},
                repository=REPO,
                pat="",
                gh=gh,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(gh.posts, [])
        out = buf.getvalue()
        self.assertIn("::notice::", out)
        self.assertIn("CODEX_TRIGGER_PAT", out)
        self.assertNotIn("ghp_", out)

    def test_request_posts_once(self) -> None:
        gh = FakeGH()
        rc = mod.run(
            event_name="pull_request",
            event={"pull_request": pr_payload()},
            repository=REPO,
            pat="not-a-real-secret",
            gh=gh,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(gh.posts), 1)
        body = gh.posts[0][1]["body"]
        self.assertIn(mod.gate_marker(SHA), body)
        self.assertIn("@codex review", body)
        self.assertNotIn("@claude", body)
        self.assertNotIn("CODEX-GATE: FIX", body)
        self.assertIn("Linear needs-decision", body)
        self.assertNotIn("stuck-thread-watch", body)

        gh.comments.append(comment(body))
        rc = mod.run(
            event_name="pull_request",
            event={"pull_request": pr_payload()},
            repository=REPO,
            pat="not-a-real-secret",
            gh=gh,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(gh.posts), 1)

    def test_codex_review_fix_is_ignored(self) -> None:
        gh = FakeGH()
        event = {
            "review": review("findings remain\nCODEX-GATE: FIX"),
            "pull_request": pr_payload(),
        }
        rc = mod.run(
            event_name="pull_request_review",
            event=event,
            repository=REPO,
            pat="not-a-real-secret",
            gh=gh,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(gh.dispatches, [])
        self.assertFalse(any("/labels" in path for path, _ in gh.posts))

    def test_retired_fix_and_old_request_never_post_or_dispatch(self) -> None:
        gh = FakeGH()
        gh.comments = [comment(mod.gate_marker(SHA), created="2026-10-01T00:00:00Z")]
        for event_name, event in (
            ("pull_request_review", {"review": review("CODEX-GATE: FIX"), "pull_request": pr_payload()}),
            ("workflow_dispatch", {"inputs": {"pr_number": "42"}}),
        ):
            self.assertEqual(mod.run(event_name=event_name, event=event, repository=REPO,
                pat="stub-token", gh=gh, now=datetime(2026, 10, 15, tzinfo=timezone.utc)), 0)
            self.assertEqual(gh.dispatches, [])
            self.assertEqual(gh.posts, [])

    def test_stale_or_unpinned_codex_review_cannot_trigger_ready(self) -> None:
        for sha in ("b" * 40, ""):
            gh = FakeGH()
            self.assertEqual(mod.run(event_name="pull_request_review",
                event={"review": review("CODEX-GATE: READY", sha=sha), "pull_request": pr_payload()},
                repository=REPO, pat="stub-token", gh=gh), 0)
            self.assertEqual(gh.posts, [])
            self.assertEqual(gh.dispatches, [])
            self.assertEqual(mod.verdict_from_reviews([review("CODEX-GATE: READY", sha=sha)], SHA), "")

    def test_non_codex_review_is_ignored(self) -> None:
        gh = FakeGH()
        event = {
            "review": review("CODEX-GATE: FIX", login="devin-ai-integration[bot]"),
            "pull_request": pr_payload(),
        }
        rc = mod.run(
            event_name="pull_request_review",
            event=event,
            repository=REPO,
            pat="not-a-real-secret",
            gh=gh,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(gh.dispatches, [])

    def test_mediate_missing_linear_notices(self) -> None:
        gh = FakeGH()
        buf = io.StringIO()
        with patch("sys.stdout", buf), patch.dict(os.environ, {"LINEAR_API_KEY": ""}, clear=False):
            rc = mod.run(
                event_name="pull_request_review",
                event={
                    "review": review("CODEX-GATE: MEDIATE"),
                    "pull_request": pr_payload(),
                },
                repository=REPO,
                pat="not-a-real-secret",
                gh=gh,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(gh.dispatches, [])
        self.assertIn("::notice::", buf.getvalue())
        self.assertIn("LINEAR_API_KEY", buf.getvalue())
        self.assertFalse(any("codex-gate-action:" in (body.get("body") or "") for _, body in gh.posts))
        self.assertTrue(any("codex-gate-mediate-pending:" in (body.get("body") or "") for _, body in gh.posts))

    def test_mediate_run_skips_linear_when_pr_approved(self) -> None:
        gh = FakeGH()
        gh.reviews = [
            {
                "body": "",
                "user": {"login": "alice"},
                "state": "APPROVED",
                "commit_id": SHA,
                "submitted_at": "2026-10-15T15:00:00Z",
            }
        ]
        with patch.dict(os.environ, {"LINEAR_API_KEY": "lin_api_not_real"}, clear=False):
            rc = mod.run(
                event_name="pull_request_review",
                event={
                    "review": review("nits\nCODEX-GATE: MEDIATE"),
                    "pull_request": pr_payload(),
                },
                repository=REPO,
                pat="not-a-real-secret",
                gh=gh,
            )
        self.assertEqual(rc, 0)
        bodies = [str(body.get("body") or "") for _, body in gh.posts]
        self.assertTrue(any("already approved" in item for item in bodies))
        self.assertTrue(any("codex-gate-action:" + SHA + ":MEDIATE" in item for item in bodies))
        self.assertFalse(any("Linear:" in item for item in bodies))

    def test_review_skips_fork_and_draft(self) -> None:
        gh = FakeGH()
        for pr in (
            pr_payload(head_repo="evil/fork"),
            pr_payload(draft=True),
            pr_payload(state="closed"),
        ):
            gh.dispatches.clear()
            rc = mod.run(
                event_name="pull_request_review",
                event={"review": review("CODEX-GATE: READY"), "pull_request": pr},
                repository=REPO,
                pat="not-a-real-secret",
                gh=gh,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(gh.dispatches, [])
            self.assertFalse(any("/labels" in path for path, _ in gh.posts))

    def test_forged_markers_do_not_count(self) -> None:
        gh = FakeGH()
        gh.comments = [
            comment(mod.action_marker(SHA, "FIX"), login="mallory"),
            comment(mod.fixer_run_marker("2026-10-15", SHA), login="mallory"),
        ]
        rc = mod.run(
            event_name="pull_request_review",
            event={
                "review": review("looks unfinished\nCODEX-GATE: FIX"),
                "pull_request": pr_payload(),
            },
            repository=REPO,
            pat="not-a-real-secret",
            gh=gh,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(gh.dispatches, [])

    def test_new_sha_preserves_legacy_unowned_label(self) -> None:
        gh = FakeGH()
        gh.comments = [comment(mod.action_marker("c" * 40, "READY"))]
        rc = mod.run(
            event_name="pull_request",
            event={"pull_request": pr_payload()},
            repository=REPO,
            pat="not-a-real-secret",
            gh=gh,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(gh.deletes, [])

    def test_current_ready_keeps_label_when_older_sha_also_ready(self) -> None:
        gh = FakeGH()
        gh.comments = [
            comment(mod.action_marker("c" * 40, "READY")),
            comment(mod.action_marker(SHA, "READY")),
            comment(mod.gate_marker(SHA) + "\n@codex review"),
        ]
        rc = mod.run(
            event_name="pull_request",
            event={"pull_request": pr_payload()},
            repository=REPO,
            pat="not-a-real-secret",
            gh=gh,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(gh.deletes, [])
        self.assertFalse(any("/labels" in path for path, _ in gh.posts))

    def test_schedule_posts_missing_request(self) -> None:
        gh = FakeGH()
        gh.prs = [pr_payload()]
        rc = mod.run(
            event_name="schedule",
            event={},
            repository=REPO,
            pat="not-a-real-secret",
            gh=gh,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(gh.posts), 1)
        self.assertIn(mod.gate_marker(SHA), gh.posts[0][1]["body"])
        self.assertEqual(gh.dispatches, [])

    def test_schedule_does_not_strip_manual_ci_full(self) -> None:
        gh = FakeGH()
        gh.prs = [pr_payload()]
        gh.comments = [
            comment(mod.action_marker("c" * 40, "READY"), created="2026-10-15T15:00:00Z"),
            comment(mod.gate_marker(SHA) + "\n@codex review", created="2026-10-15T15:10:00Z"),
        ]
        gh.events = [
            label_event("github-actions[bot]", created="2026-10-15T15:00:00Z"),
            label_event("lucas", created="2026-10-15T15:12:00Z"),
        ]
        rc = mod.run(
            event_name="schedule",
            event={},
            repository=REPO,
            pat="not-a-real-secret",
            gh=gh,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(gh.deletes, [])

    def test_schedule_keeps_pat_owner_manual_label(self) -> None:
        gh = FakeGH()
        gh.prs = [pr_payload()]
        gh.comments = [
            comment(mod.action_marker("c" * 40, "READY"), created="2026-10-15T15:00:00Z"),
            comment(mod.gate_marker(SHA) + "\n@codex review", created="2026-10-15T15:10:00Z"),
        ]
        # Same actor as the gate PAT owner; later timestamp is the override.
        gh.events = [
            label_event("github-actions[bot]", created="2026-10-15T15:00:00Z"),
            label_event("github-actions[bot]", created="2026-10-15T15:12:00Z"),
        ]
        rc = mod.run(
            event_name="schedule",
            event={},
            repository=REPO,
            pat="not-a-real-secret",
            gh=gh,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(gh.deletes, [])

    def test_schedule_clears_gate_owned_stale_label(self) -> None:
        gh = FakeGH()
        gh.prs = [pr_payload()]
        gh.comments = [
            comment(mod.gate_marker(SHA) + "\n@codex review", created="2026-10-15T15:10:00Z"),
        ]
        gh.events = [label_event("github-actions[bot]", created="2026-10-15T15:00:00Z") | {"id": 20}]
        gh.comments.append({"body": mod.FIXER_RECEIPTS.marker("ci-owned", {"head": "c" * 40, "event": 20}),
                            "user": {"login": "github-actions[bot]", "id": 41898282, "type": "Bot"}})
        rc = mod.run(
            event_name="schedule",
            event={},
            repository=REPO,
            pat="not-a-real-secret",
            gh=gh,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(any("labels/ci:full" in path for path in gh.deletes))

    def test_gate_ready_helper_requires_current_sha_not_ready(self) -> None:
        older = [comment(mod.action_marker("c" * 40, "READY"))]
        both = older + [comment(mod.action_marker(SHA, "READY"))]
        self.assertTrue(mod.gate_ready_on_other_sha(older, SHA))
        self.assertFalse(mod.gate_ready_on_other_sha(both, SHA))
        self.assertFalse(mod.gate_ready_on_other_sha([], SHA))

    def test_create_needs_decision_issue_updates_rollup(self) -> None:
        updates: list[dict[str, Any]] = []
        existing = (
            f"{mod.stuck_pr_marker(REPO, '42')}\n"
            f"{mod.GATE_BEGIN}\n"
            f"**PR:** https://github.com/{REPO}/pull/42\n"
            f"**SHA:** `{SHA}`\n\n"
            "Codex gate MEDIATE: Devin and Claude disagree or a thread needs a "
            "human. Auto-filed by `.github/workflows/codex-gate.yml`. Humans decide.\n"
            f"{mod.GATE_END}\n"
            "Wait for Alice's risk decision\n"
        )

        def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            if "issues(" in query:
                return {
                    "issues": {
                        "nodes": [
                            {
                                "id": "issue-1",
                                "url": "https://linear.app/csl-laboratories/issue/CSL-9",
                                "description": existing,
                            }
                        ]
                    }
                }
            if "issueUpdate" in query:
                updates.append(variables)
                return {
                    "issueUpdate": {
                        "success": True,
                        "issue": {"url": "https://linear.app/csl-laboratories/issue/CSL-9"},
                    }
                }
            raise AssertionError("should update, not create")

        url = mod.create_needs_decision_issue(
            graphql, repository=REPO, pr_number="42", sha="b" * 40
        )
        self.assertEqual(url, "https://linear.app/csl-laboratories/issue/CSL-9")
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["id"], "issue-1")
        self.assertEqual(updates[0]["input"]["title"], "Stuck PR threads: example#42")
        body = updates[0]["input"]["description"]
        self.assertIn(mod.stuck_pr_marker(REPO, "42"), body)
        self.assertIn("b" * 40, body)
        self.assertIn("Wait for Alice's risk decision", body)
        self.assertIn(mod.GATE_BEGIN, body)
        self.assertEqual(body.count(mod.stuck_pr_marker(REPO, "42")), 1)

    def test_create_needs_decision_issue_rollup_title(self) -> None:
        created: list[dict[str, Any]] = []

        def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            if "issues(" in query:
                return {"issues": {"nodes": []}}
            if "teams(" in query:
                return {
                    "teams": {
                        "nodes": [
                            {
                                "id": "t1",
                                "states": {"nodes": [{"id": "s1", "type": "triage"}]},
                            }
                        ]
                    }
                }
            if "issueLabels" in query:
                name = str(variables.get("name") or "")
                return {"issueLabels": {"nodes": [{"id": name, "name": name}]}}
            if "projects(" in query:
                return {
                    "projects": {
                        "nodes": [{"id": "p1", "name": "ai-investment-cockpit"}]
                    }
                }
            if "issueCreate" in query:
                created.append(variables)
                return {
                    "issueCreate": {
                        "success": True,
                        "issue": {"url": "https://linear.app/csl-laboratories/issue/CSL-10"},
                    }
                }
            raise AssertionError(query[:80])

        url = mod.create_needs_decision_issue(
            graphql, repository=REPO, pr_number="42", sha=SHA
        )
        self.assertEqual(url, "https://linear.app/csl-laboratories/issue/CSL-10")
        self.assertEqual(created[0]["input"]["title"], "Stuck PR threads: example#42")
        self.assertIn(mod.stuck_pr_marker(REPO, "42"), created[0]["input"]["description"])
        self.assertIn(mod.GATE_BEGIN, created[0]["input"]["description"])
        self.assertIn(mod.mediate_issue_end_marker(), created[0]["input"]["description"])
        self.assertNotIn(f"codex-gate-mediate:42:{SHA}", created[0]["input"]["description"])

    def test_create_needs_decision_issue_ignores_other_repo(self) -> None:
        created: list[dict[str, Any]] = []
        other = (
            f"{mod.stuck_pr_marker('acme/other', '42')}\n"
            f"{mod.WATCHER_BEGIN}\n"
            "**PR:** https://github.com/acme/other/pull/42\n"
            f"{mod.WATCHER_END}\n"
        )

        def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            if "issues(" in query:
                return {
                    "issues": {
                        "nodes": [
                            {
                                "id": "other-1",
                                "url": "https://linear.app/csl-laboratories/issue/CSL-8",
                                "description": other,
                            }
                        ]
                    }
                }
            if "teams(" in query:
                return {
                    "teams": {
                        "nodes": [
                            {
                                "id": "t1",
                                "states": {"nodes": [{"id": "s1", "type": "triage"}]},
                            }
                        ]
                    }
                }
            if "issueLabels" in query:
                name = str(variables.get("name") or "")
                return {"issueLabels": {"nodes": [{"id": name, "name": name}]}}
            if "projects(" in query:
                return {
                    "projects": {
                        "nodes": [{"id": "p1", "name": "ai-investment-cockpit"}]
                    }
                }
            if "issueCreate" in query:
                created.append(variables)
                return {
                    "issueCreate": {
                        "success": True,
                        "issue": {"url": "https://linear.app/csl-laboratories/issue/CSL-11"},
                    }
                }
            if "issueUpdate" in query:
                raise AssertionError("must not update another repository's issue")
            raise AssertionError(query[:80])

        url = mod.create_needs_decision_issue(
            graphql, repository=REPO, pr_number="42", sha=SHA
        )
        self.assertEqual(url, "https://linear.app/csl-laboratories/issue/CSL-11")
        self.assertEqual(len(created), 1)

    def test_create_needs_decision_issue_updates_legacy_twice(self) -> None:
        updates: list[dict[str, Any]] = []
        issue = {
            "id": "legacy-1",
            "url": "https://linear.app/csl-laboratories/issue/CSL-20",
            "title": "Decision on risk limits",
            "description": (
                f"{mod.mediate_issue_marker_legacy('42')}\n"
                f"**PR:** https://github.com/{REPO}/pull/42\n"
            ),
            "state": {"name": "Triage", "type": "triage"},
        }

        def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            if "title: { eq" in query:
                return {"issues": {"nodes": []}}
            if "issues(" in query:
                contains = str(variables.get("contains") or "")
                if contains in {
                    "codex-gate-mediate:42 -->",
                    f"https://github.com/{REPO}/pull/42",
                } or "stuck-pr-threads:" in contains:
                    return {"issues": {"nodes": [issue]}}
                return {"issues": {"nodes": []}}
            if "issueUpdate" in query:
                updates.append(variables)
                issue["description"] = str(variables["input"]["description"])
                issue["title"] = str(variables["input"]["title"])
                return {
                    "issueUpdate": {
                        "success": True,
                        "issue": {"url": issue["url"]},
                    }
                }
            raise AssertionError("must update the legacy issue, not create")

        first = mod.create_needs_decision_issue(
            graphql, repository=REPO, pr_number="42", sha=SHA
        )
        second = mod.create_needs_decision_issue(
            graphql, repository=REPO, pr_number="42", sha="b" * 40
        )
        self.assertEqual(first, issue["url"])
        self.assertEqual(second, issue["url"])
        self.assertEqual(len(updates), 2)
        self.assertIn(mod.stuck_pr_marker(REPO, "42"), updates[-1]["input"]["description"])
        self.assertIn("b" * 40, updates[-1]["input"]["description"])

    def test_create_needs_decision_issue_skips_unqualified_overwrite(self) -> None:
        created: list[dict[str, Any]] = []
        foreign = (
            "<!-- stuck-pr-threads:example#42 -->\n"
            "**PR:** https://github.com/other/example/pull/42\n"
        )

        def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            if "title: { eq" in query:
                return {"issues": {"nodes": []}}
            if "issues(" in query:
                return {
                    "issues": {
                        "nodes": [
                            {
                                "id": "foreign-1",
                                "url": "https://linear.app/csl-laboratories/issue/CSL-99",
                                "description": foreign,
                            }
                        ]
                    }
                }
            if "teams(" in query:
                return {
                    "teams": {
                        "nodes": [
                            {
                                "id": "t1",
                                "states": {"nodes": [{"id": "s1", "type": "triage"}]},
                            }
                        ]
                    }
                }
            if "issueLabels" in query:
                name = str(variables.get("name") or "")
                return {"issueLabels": {"nodes": [{"id": name, "name": name}]}}
            if "projects(" in query:
                return {
                    "projects": {
                        "nodes": [{"id": "p1", "name": "ai-investment-cockpit"}]
                    }
                }
            if "issueCreate" in query:
                created.append(variables)
                return {
                    "issueCreate": {
                        "success": True,
                        "issue": {"url": "https://linear.app/csl-laboratories/issue/CSL-100"},
                    }
                }
            if "issueUpdate" in query:
                raise AssertionError("must not overwrite a roll-up without a repo-qualified marker")
            raise AssertionError(query[:80])

        url = mod.create_needs_decision_issue(
            graphql, repository=REPO, pr_number="42", sha=SHA
        )
        self.assertEqual(url, "https://linear.app/csl-laboratories/issue/CSL-100")
        self.assertEqual(len(created), 1)
        self.assertIn(mod.stuck_pr_marker(REPO, "42"), created[0]["input"]["description"])

    def test_create_needs_decision_issue_ignores_related_link_in_pr_field(self) -> None:
        created: list[dict[str, Any]] = []
        stolen = (
            f"{mod.mediate_issue_marker_legacy('42')}\n"
            "**PR:** [other PR](https://github.com/other/example/pull/42) "
            f"[related](https://github.com/{REPO}/pull/42)\n"
        )

        def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            if "title: { eq" in query:
                return {"issues": {"nodes": []}}
            if "issues(" in query:
                return {
                    "issues": {
                        "nodes": [
                            {
                                "id": "stolen-1",
                                "url": "https://linear.app/csl-laboratories/issue/CSL-101",
                                "description": stolen,
                            }
                        ]
                    }
                }
            if "teams(" in query:
                return {
                    "teams": {
                        "nodes": [
                            {
                                "id": "t1",
                                "states": {"nodes": [{"id": "s1", "type": "triage"}]},
                            }
                        ]
                    }
                }
            if "issueLabels" in query:
                name = str(variables.get("name") or "")
                return {"issueLabels": {"nodes": [{"id": name, "name": name}]}}
            if "projects(" in query:
                return {
                    "projects": {
                        "nodes": [{"id": "p1", "name": "ai-investment-cockpit"}]
                    }
                }
            if "issueCreate" in query:
                created.append(variables)
                return {
                    "issueCreate": {
                        "success": True,
                        "issue": {"url": "https://linear.app/csl-laboratories/issue/CSL-102"},
                    }
                }
            if "issueUpdate" in query:
                raise AssertionError("must not steal a roll-up via a related PR link")
            raise AssertionError(query[:80])

        url = mod.create_needs_decision_issue(
            graphql, repository=REPO, pr_number="42", sha=SHA
        )
        self.assertEqual(url, "https://linear.app/csl-laboratories/issue/CSL-102")
        self.assertEqual(len(created), 1)

    def test_create_needs_decision_issue_updates_watcher_marker(self) -> None:
        updates: list[dict[str, Any]] = []
        watcher = (
            f"{mod.stuck_pr_marker(REPO, '42')}\n"
            f"{mod.WATCHER_BEGIN}\n"
            "watcher threads\n"
            f"{mod.WATCHER_END}\n"
        )

        def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            if "issues(" in query:
                return {
                    "issues": {
                        "nodes": [
                            {
                                "id": "watch-1",
                                "url": "https://linear.app/csl-laboratories/issue/CSL-86",
                                "description": watcher,
                            }
                        ]
                    }
                }
            if "issueUpdate" in query:
                updates.append(variables)
                return {
                    "issueUpdate": {
                        "success": True,
                        "issue": {"url": "https://linear.app/csl-laboratories/issue/CSL-86"},
                    }
                }
            raise AssertionError("should update the #124 roll-up, not create")

        url = mod.create_needs_decision_issue(
            graphql, repository=REPO, pr_number="42", sha=SHA
        )
        self.assertEqual(url, "https://linear.app/csl-laboratories/issue/CSL-86")
        self.assertEqual(len(updates), 1)
        body = updates[0]["input"]["description"]
        self.assertIn(mod.stuck_pr_marker(REPO, "42"), body)
        self.assertIn(mod.WATCHER_BEGIN, body)
        self.assertIn("watcher threads", body)
        self.assertIn(mod.GATE_BEGIN, body)

    def test_create_needs_decision_issue_adopts_title_and_skips_done(self) -> None:
        updates: list[dict[str, Any]] = []
        created: list[dict[str, Any]] = []
        unmarked = {
            "id": "hand-1",
            "url": "https://linear.app/csl-laboratories/issue/CSL-167",
            "title": "Stuck PR threads: example#42",
            "description": "Hand-made roll-up",
            "state": {"name": "Triage", "type": "triage"},
        }
        done = {
            "id": "done-1",
            "url": "https://linear.app/csl-laboratories/issue/CSL-169",
            "title": "Stuck PR threads: example#7",
            "description": "",
            "state": {"name": "Done", "type": "completed"},
        }

        def graphql_open(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            if "title: { eq" in query:
                return {"issues": {"nodes": [unmarked]}}
            if "issues(" in query:
                return {"issues": {"nodes": []}}
            if "issueUpdate" in query:
                updates.append(variables)
                return {
                    "issueUpdate": {
                        "success": True,
                        "issue": {"url": unmarked["url"]},
                    }
                }
            raise AssertionError(query[:80])

        url = mod.create_needs_decision_issue(
            graphql_open, repository=REPO, pr_number="42", sha=SHA
        )
        self.assertEqual(url, unmarked["url"])
        self.assertEqual(len(updates), 1)
        self.assertIn("Hand-made roll-up", updates[0]["input"]["description"])
        self.assertIn(mod.stuck_pr_marker(REPO, "42"), updates[0]["input"]["description"])

        def graphql_done(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            if "title: { eq" in query:
                return {"issues": {"nodes": [done]}}
            if "issues(" in query:
                return {"issues": {"nodes": []}}
            if "teams(" in query:
                return {
                    "teams": {
                        "nodes": [
                            {
                                "id": "t1",
                                "states": {"nodes": [{"id": "s1", "type": "triage"}]},
                            }
                        ]
                    }
                }
            if "issueLabels" in query:
                name = str(variables.get("name") or "")
                return {"issueLabels": {"nodes": [{"id": name, "name": name}]}}
            if "projects(" in query:
                return {
                    "projects": {
                        "nodes": [{"id": "p1", "name": "ai-investment-cockpit"}]
                    }
                }
            if "issueCreate" in query:
                created.append(variables)
                return {
                    "issueCreate": {
                        "success": True,
                        "issue": {"url": "https://linear.app/csl-laboratories/issue/CSL-12"},
                    }
                }
            if "issueUpdate" in query:
                raise AssertionError("must not update a Done issue")
            raise AssertionError(query[:80])

        url = mod.create_needs_decision_issue(
            graphql_done, repository=REPO, pr_number="7", sha=SHA
        )
        self.assertEqual(url, "https://linear.app/csl-laboratories/issue/CSL-12")
        self.assertEqual(len(created), 1)

    def test_merge_keeps_watcher_block(self) -> None:
        existing = (
            f"{mod.stuck_pr_marker(REPO, '42')}\n"
            f"{mod.WATCHER_BEGIN}\n"
            "watcher threads\n"
            f"{mod.WATCHER_END}\n"
            "Wait for Alice's risk decision\n"
        )
        merged = mod.merge_mediate_description(existing, REPO, "42", SHA)
        self.assertIn(mod.WATCHER_BEGIN, merged)
        self.assertIn("watcher threads", merged)
        self.assertIn(mod.WATCHER_END, merged)
        self.assertIn(mod.GATE_BEGIN, merged)
        self.assertIn("Wait for Alice's risk decision", merged)
        self.assertLess(merged.index(mod.WATCHER_END), merged.index(mod.GATE_BEGIN))

    def test_merge_mediate_description_keeps_notes_on_repeat(self) -> None:
        first = mod.merge_mediate_description(
            "Wait for Alice's risk decision", REPO, "42", SHA
        )
        self.assertIn("Wait for Alice's risk decision", first)
        second = mod.merge_mediate_description(first, REPO, "42", "b" * 40)
        self.assertEqual(second.count("Wait for Alice's risk decision"), 1)
        self.assertIn("b" * 40, second)
        self.assertNotIn(SHA, second.split(mod.mediate_issue_end_marker())[0])

    def test_mediate_description_does_not_match_other_pr(self) -> None:
        own = mod.stuck_pr_marker(REPO, "42")
        self.assertTrue(mod.mediate_description_matches(own, REPO, "42"))
        self.assertTrue(mod.has_repo_qualified_marker(own, REPO, "42"))
        self.assertTrue(mod.rollup_may_update(own, REPO, "42"))
        short = "<!-- stuck-pr-threads:example#42 -->"
        self.assertTrue(mod.mediate_description_matches(short, REPO, "42"))
        self.assertFalse(mod.has_repo_qualified_marker(short, REPO, "42"))
        self.assertFalse(mod.rollup_may_update(short, REPO, "42"))
        self.assertFalse(mod.has_repo_qualified_marker(own, "acme/other", "42"))
        legacy = (
            f"{mod.mediate_marker('42', SHA)}\n**PR:** https://github.com/{REPO}/pull/42\n"
        )
        self.assertTrue(mod.mediate_description_matches(legacy, REPO, "42"))
        self.assertTrue(mod.rollup_may_update(legacy, REPO, "42"))
        self.assertFalse(mod.mediate_description_matches(own, REPO, "4"))
        other = (
            f"{mod.mediate_issue_marker_legacy('42')}\n"
            "**PR:** https://github.com/acme/other/pull/42\n"
        )
        self.assertFalse(mod.mediate_description_matches(other, REPO, "42"))
        self.assertFalse(
            mod.mediate_description_matches(mod.stuck_pr_marker("acme/other", "42"), REPO, "42")
        )
        markdown = (
            f"{mod.mediate_issue_marker_legacy('42')}\n"
            f"**PR:** [#42](https://github.com/{REPO}/pull/42)\n"
        )
        self.assertTrue(mod.mediate_description_matches(markdown, REPO, "42"))
        numbered = (
            f"{mod.mediate_issue_marker_legacy('42')}\n"
            f"**PR:** https://github.com/{REPO}/pull/420\n"
        )
        self.assertFalse(mod.mediate_description_matches(numbered, REPO, "42"))
        related = (
            f"{mod.mediate_issue_marker_legacy('42')}\n"
            "**PR:** [#42](https://github.com/other/project/pull/42)\n"
            f"[discussion](https://github.com/{REPO}/pull/42)\n"
        )
        self.assertFalse(mod.mediate_description_matches(related, REPO, "42"))
        related_field = (
            f"{mod.mediate_issue_marker_legacy('42')}\n"
            f"**PR:** [other PR](https://github.com/other/example/pull/42) "
            f"[related](https://github.com/{REPO}/pull/42)\n"
        )
        self.assertFalse(mod.mediate_description_matches(related_field, REPO, "42"))
        self.assertFalse(mod.rollup_may_update(related_field, REPO, "42"))
        self.assertEqual(
            mod.primary_pr_field_url(related_field),
            "https://github.com/other/example/pull/42",
        )

    def test_pr_is_approved_ignores_bots_and_changes(self) -> None:
        human_ok = {
            "user": {"login": "alice"},
            "state": "APPROVED",
            "submitted_at": "2026-10-15T15:00:00Z",
        }
        self.assertTrue(mod.pr_is_approved([human_ok]))
        self.assertFalse(
            mod.pr_is_approved(
                [
                    human_ok,
                    {
                        "user": {"login": "bob"},
                        "state": "CHANGES_REQUESTED",
                        "submitted_at": "2026-10-15T16:00:00Z",
                    },
                ]
            )
        )
        self.assertTrue(
            mod.pr_is_approved(
                [
                    human_ok,
                    {
                        "user": {"login": "devin-ai-integration[bot]"},
                        "state": "CHANGES_REQUESTED",
                        "submitted_at": "2026-10-15T16:00:00Z",
                    },
                ]
            )
        )
        self.assertFalse(
            mod.pr_is_approved(
                [
                    {
                        "user": {"login": "chatgpt-codex-connector[bot]"},
                        "state": "APPROVED",
                        "submitted_at": "2026-10-15T15:00:00Z",
                    }
                ]
            )
        )
        self.assertFalse(
            mod.pr_is_approved(
                [
                    {
                        "user": {"login": "chatgpt-codex-connector", "type": "Bot"},
                        "state": "APPROVED",
                        "submitted_at": "2026-10-15T15:00:00Z",
                    }
                ]
            )
        )
        self.assertFalse(
            mod.pr_is_approved(
                [
                    {
                        "user": {"login": "chatgpt-codex-connector"},
                        "state": "APPROVED",
                        "submitted_at": "2026-10-15T15:00:00Z",
                    }
                ]
            )
        )
        self.assertFalse(mod.pr_is_approved([]))

    def test_label_predates_last_ready_ignores_actor(self) -> None:
        ready = [comment(mod.action_marker("c" * 40, "READY"), created="2026-10-15T15:00:00Z")]
        self.assertTrue(
            mod.label_predates_last_ready(
                ready,
                [label_event("github-actions[bot]", created="2026-10-15T15:00:00Z")],
            )
        )
        self.assertFalse(
            mod.label_predates_last_ready(
                ready,
                [label_event("github-actions[bot]", created="2026-10-15T15:12:00Z")],
            )
        )
        self.assertFalse(
            mod.label_predates_last_ready(
                ready,
                [
                    label_event("github-actions[bot]", created="2026-10-15T15:00:00Z"),
                    label_event("github-actions[bot]", unlabeled=True, created="2026-10-15T15:01:00Z"),
                ],
            )
        )


class WorkflowStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.claude = CLAUDE_WORKFLOW.read_text(encoding="utf-8")
        cls.ci = CI_WORKFLOW.read_text(encoding="utf-8")
        cls.rust = RUST_WORKFLOW.read_text(encoding="utf-8")
        cls.pr_lint = PR_LINT_WORKFLOW.read_text(encoding="utf-8")
        cls.retrigger = RETRIGGER_WORKFLOW.read_text(encoding="utf-8")
        cls.scan = SCAN_WORKFLOW.read_text(encoding="utf-8")
        cls.actionlint = ACTIONLINT.read_text(encoding="utf-8")
        cls.agents = AGENTS.read_text(encoding="utf-8")

    def test_runs_on_ubuntu_latest_not_blacksmith(self) -> None:
        self.assertIn("runs-on: ubuntu-latest", self.text)
        self.assertNotIn("blacksmith", self.text)

    def test_notice_on_missing_secret(self) -> None:
        self.assertIn("::notice::CODEX_TRIGGER_PAT is not configured", self.text)
        self.assertIn("secrets.CODEX_TRIGGER_PAT", self.text)
        self.assertIn("scripts/ci/codex_gate.py", self.text)
        source = GATE_PATH.read_text(encoding="utf-8")
        self.assertIn("<!-- codex-gate:", source)
        self.assertIn("CODEX-GATE:", source)

    def test_candidate_coordinator_uses_shared_writer_lock_and_trusted_policy(self) -> None:
        doc = yaml.load(self.text, Loader=yaml.BaseLoader)
        self.assertEqual(doc['on']['pull_request_review']['types'], ['submitted'])
        # No repo-wide lock: the read-only scan of PR B must not wait behind a
        # gate of PR A that is queued on a busy Claude writer lock. The per-PR
        # gate lock below is the only serialization of candidate side effects.
        self.assertNotIn('concurrency', doc)
        scan, gate, tests = (doc['jobs'][name] for name in ('scan', 'gate', 'event-paths'))
        self.assertNotIn('concurrency', scan)
        self.assertNotIn('concurrency', tests)
        self.assertEqual(gate['needs'], 'scan')
        self.assertEqual(gate['concurrency'], {'group': 'claude-${{ matrix.pr }}',
                                               'queue': 'max', 'cancel-in-progress': 'false'})
        # Trusted policy identity flows from the gate job's own checkout.
        policy = next(s for s in gate['steps'] if s.get('id') == 'policy')
        self.assertEqual(policy['if'], "steps.pat.outputs.skip != 'true'")
        self.assertEqual(policy['env'], {'DEFAULT_BRANCH': '${{ github.event.repository.default_branch }}'})
        self.assertIn('git rev-parse HEAD', policy['run'])
        names = [s.get('id') or s.get('name') or s.get('uses') for s in gate['steps']]
        self.assertLess(names.index('actions/checkout@v4'), names.index('policy'))
        self.assertLess(names.index('policy'), names.index('Run Codex gate'))
        self.assertEqual(gate['strategy']['matrix']['pr'],
                         "${{ fromJSON(needs.scan.outputs.prs || '[]') }}")
        # Human review changes must wake reconciliation, including hold release.
        # Exact reviewer identity is authenticated by the trusted helper.
        self.assertEqual(' '.join(scan['if'].split()),
                         "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch' || "
                         'github.event.pull_request.head.repo.full_name == github.repository')
        self.assertEqual(scan.get('permissions', doc['permissions']),
                         {'contents': 'read', 'pull-requests': 'read', 'issues': 'read'})
        self.assertEqual(scan['steps'][1]['env'],
                         {'GH_TOKEN': '${{ github.token }}', 'CANDIDATE_SCAN': '${{ vars.CANDIDATE_COORDINATOR }}'})
        self.assertEqual(gate['permissions'],
                         {'contents': 'read', 'pull-requests': 'read', 'issues': 'write'})
        for job in (scan, gate):
            checkouts = [s for s in job['steps'] if s.get('uses') == 'actions/checkout@v4']
            self.assertEqual(len(checkouts), 1)
            self.assertEqual(checkouts[0]['with']['ref'], '${{ github.event.repository.default_branch }}')
            self.assertEqual(checkouts[0]['with']['persist-credentials'], 'false')
        execute = next(s for s in gate['steps'] if s.get('name') == 'Run Codex gate')
        self.assertEqual(execute['if'], "steps.pat.outputs.skip != 'true'")
        self.assertEqual(execute['env']['CODEX_CANDIDATE_PR'], '${{ matrix.pr }}')
        self.assertEqual(execute['env']['CODEX_POLICY_SHA'], '${{ steps.policy.outputs.sha }}')
        self.assertEqual(execute['env']['CODEX_POLICY_BRANCH'], '${{ steps.policy.outputs.branch }}')
        self.assertEqual(execute['env']['CODEX_TRIGGER_PAT'], '${{ secrets.CODEX_TRIGGER_PAT }}')
        self.assertEqual(execute['env']['GH_TOKEN'], '${{ github.token }}')
        # Same-repository tests may load PR code, but have no privileged secrets.
        self.assertEqual(tests['permissions'], {'contents': 'read'})
        self.assertEqual(' '.join(tests['if'].split()),
                         "github.event_name == 'pull_request' && "
                         'github.event.pull_request.head.repo.full_name == github.repository')
        self.assertNotIn('secrets.', yaml.dump(tests))

    def test_legacy_route_runs_only_when_coordinator_is_off(self) -> None:
        doc = yaml.load(self.text, Loader=yaml.BaseLoader)
        legacy = doc['jobs']['legacy']
        condition = ' '.join(legacy['if'].split())
        self.assertTrue(condition.startswith("vars.CANDIDATE_COORDINATOR != 'true' && ("), condition)
        # Every clause of main's pre-CSL-259 gate condition, unchanged.
        for clause in ("github.event_name == 'schedule'",
                       "github.event_name == 'workflow_dispatch'",
                       "github.event_name == 'pull_request' &&",
                       ("github.event_name == 'pull_request_review' && "
                        "github.event.review.user.login == 'chatgpt-codex-connector[bot]' && "
                        "github.event.pull_request.head.repo.full_name == github.repository")):
            self.assertIn(clause, condition)
        self.assertEqual(condition.count('github.event.pull_request.head.repo.full_name == github.repository'), 2)
        # Only main's four pull_request actions, on non-draft PRs, reach the legacy handlers.
        self.assertIn("github.event.pull_request.draft == false", legacy['if'])
        self.assertIn('contains(fromJSON(\'["opened","synchronize","reopened","ready_for_review"]\'), '
                      'github.event.action)', condition)
        self.assertNotIn('needs', legacy)
        self.assertEqual(legacy['concurrency'], {'group': 'codex-gate', 'cancel-in-progress': 'false'})
        self.assertEqual(legacy['permissions'], {'contents': 'read', 'pull-requests': 'read', 'issues': 'write'})
        self.assertEqual(legacy['runs-on'], 'ubuntu-latest')
        self.assertEqual(legacy['timeout-minutes'], '10')
        self.assertEqual([s.get('name') or s.get('uses') for s in legacy['steps']],
                         ['Check trigger PAT', 'actions/checkout@v4', 'Run Codex gate'])
        checkout = legacy['steps'][1]
        self.assertEqual(checkout['if'], "steps.pat.outputs.skip != 'true'")
        self.assertEqual(checkout['with'], {'ref': '${{ github.event.repository.default_branch }}',
                                            'persist-credentials': 'false', 'fetch-depth': '1'})
        execute = legacy['steps'][2]
        self.assertEqual(execute['if'], "steps.pat.outputs.skip != 'true'")
        self.assertEqual(execute['env'], {
            'GH_TOKEN': '${{ github.token }}', 'CODEX_TRIGGER_PAT': '${{ secrets.CODEX_TRIGGER_PAT }}',
            'LINEAR_API_KEY': '${{ secrets.LINEAR_API_KEY }}', 'GITHUB_REPOSITORY': '${{ github.repository }}',
            'GITHUB_EVENT_NAME': '${{ github.event_name }}', 'GITHUB_EVENT_PATH': '${{ github.event_path }}'})
        self.assertNotIn('CODEX_CANDIDATE_PR', yaml.dump(legacy))
        self.assertIn('python3 -X utf8 -B scripts/ci/codex_gate.py', execute['run'])

    def test_coordinator_route_runs_only_when_coordinator_is_on(self) -> None:
        doc = yaml.load(self.text, Loader=yaml.BaseLoader)
        gate, scan = doc['jobs']['gate'], doc['jobs']['scan']
        self.assertEqual(' '.join(gate['if'].split()),
                         "vars.CANDIDATE_COORDINATOR == 'true' && needs.scan.outputs.prs != '[]' && "
                         "needs.scan.outputs.prs != ''")
        self.assertEqual(scan['steps'][1]['env']['CANDIDATE_SCAN'], '${{ vars.CANDIDATE_COORDINATOR }}')

    def test_only_gate_jobs_see_trigger_pat(self) -> None:
        doc = yaml.load(self.text, Loader=yaml.BaseLoader)
        with_pat = {name for name, job in doc['jobs'].items() if 'CODEX_TRIGGER_PAT' in yaml.dump(job)}
        self.assertEqual(with_pat, {'legacy', 'gate'})
        self.assertNotIn('CODEX_TRIGGER_PAT', yaml.dump({k: v for k, v in doc.items() if k != 'jobs'}))

    def test_claude_dispatch_triggers(self) -> None:
        self.assertIn("repository_dispatch:", self.claude)
        self.assertIn("types: [devin-review-fix]", self.claude)
        self.assertIn("job:", self.claude)
        self.assertIn("devin-review-fix", self.claude)

    def test_windows_runner_is_2vcpu(self) -> None:
        self.assertIn("blacksmith-2vcpu-windows-2025", self.ci)
        self.assertNotIn("blacksmith-4vcpu-windows-2025", self.ci)
        self.assertIn("blacksmith-2vcpu-windows-2025", self.rust)
        self.assertNotIn("blacksmith-4vcpu-windows-2025", self.rust)
        self.assertIn("blacksmith-2vcpu-windows-2025", self.actionlint)
        self.assertNotIn("blacksmith-4vcpu-windows-2025", self.actionlint)

    def test_rust_cache_and_label_skip(self) -> None:
        self.assertIn("Swatinem/rust-cache@v2", self.rust)
        self.assertIn("workspaces: rust", self.rust)
        self.assertIn("ci:full", self.rust)
        self.assertIn("github.event.label.name == 'enhancement'", self.retrigger)

    def test_ci_label_events_do_not_start_required_check(self) -> None:
        on = self.ci.split("jobs:", 1)[0]
        self.assertIn("types: [opened, synchronize, reopened, ready_for_review]", on)
        self.assertNotIn("labeled, unlabeled", on)
        rust_on = self.rust.split("jobs:", 1)[0]
        self.assertIn("types: [opened, synchronize, reopened, ready_for_review]", rust_on)
        self.assertNotIn("labeled, unlabeled", rust_on)
        self.assertIn("github.event.pull_request.draft != true", self.ci)
        self.assertIn("&& 'draft'", self.ci)
        self.assertIn("format('label-{0}-{1}', inputs.pr_head_sha", self.ci)
        gates = self.ci.split("\n  linux-gates:\n", 1)[1].split("\n  windows:\n", 1)[0]
        header = gates.split("steps:", 1)[0]
        self.assertIn("name: linux-gates", self.ci)
        self.assertIn("always() && github.event_name != 'schedule'", header)
        self.assertNotIn("needs.filter.result != 'skipped'", header)
        self.assertIn('FILTER_RESULT" != "success"', gates)
        self.assertNotIn("required check passes.", self.ci)
        retrigger_jobs = self.retrigger.split("\njobs:\n", 1)[1]
        self.assertNotIn("linux-gates:", retrigger_jobs)
        self.assertNotIn("name: linux-gates", retrigger_jobs)
        self.assertIn("github.event.label.name == 'ci:full'", self.retrigger)
        self.assertIn("github.event.label.name == 'enhancement'", self.retrigger)
        self.assertIn("gh workflow run ci.yml", self.retrigger)
        self.assertIn("runs-on: ubuntu-latest", self.retrigger)
        retrigger_on = self.retrigger.split("jobs:", 1)[0]
        self.assertIn("types: [labeled]", retrigger_on)
        self.assertNotIn("unlabeled", retrigger_on.split("jobs:", 1)[0].split("\non:", 1)[-1])

    def test_non_test_jobs_use_github_hosted(self) -> None:
        filter_job = self.ci.split("\n  linux:\n", 1)[0]
        self.assertIn("runs-on: ubuntu-latest", filter_job)
        gates = self.ci.split("\n  linux-gates:\n", 1)[1].split("\n  windows:\n", 1)[0]
        self.assertIn("runs-on: ubuntu-latest", gates)
        self.assertIn("name: linux-gates", self.ci)
        rust_filter = self.rust.split("\n  rust-book:\n", 1)[0]
        self.assertIn("runs-on: ubuntu-latest", rust_filter)
        self.assertIn("github.event.pull_request.draft != true", self.rust)
        self.assertIn("ready_for_review", self.rust)
        self.assertIn("&& 'draft'", self.rust)
        self.assertIn("format('label-{0}-{1}', inputs.pr_head_sha", self.rust)
        linux = self.ci.split("\n  linux:\n", 1)[1].split("\n  linux-gates:\n", 1)[0]
        self.assertIn("blacksmith-2vcpu-ubuntu-2404", linux)
        claude_runners = [
            line.strip()
            for line in self.claude.splitlines()
            if line.strip().startswith("runs-on:")
        ]
        self.assertTrue(claude_runners)
        self.assertTrue(all(r == "runs-on: ubuntu-latest" for r in claude_runners))
        scan = (ROOT / ".github" / "workflows" / "conflict-scan.yml").read_text(encoding="utf-8")
        scan_runners = [
            line.strip()
            for line in scan.splitlines()
            if line.strip().startswith("runs-on:")
        ]
        self.assertTrue(scan_runners)
        self.assertTrue(all(r == "runs-on: ubuntu-latest" for r in scan_runners))
        self.assertIn('cron: "7 */2 * * *"', scan)
        self.assertIn("workflow_dispatch:", scan)
        scan_job = scan.split("\n  conflict-scan:\n", 1)[1].split("\n  conflict-resolve:\n", 1)[0]
        self.assertIn("github.event_name == 'workflow_dispatch'", scan_job)
        self.assertIn("github.event.inputs.pr_number", scan_job)
        resolve = scan.split("\n  conflict-resolve:\n", 1)[1]
        resolve_header = resolve.split("steps:", 1)[0]
        self.assertIn("github.event_name != 'push'", resolve_header)
        self.assertNotIn("github.event_name == 'schedule'", resolve_header)
        self.assertIn("gh workflow run conflict-scan.yml", self.claude)
        self.assertIn("forward-conflict-scan:", self.claude)

    def test_pr_lint_uses_github_hosted(self) -> None:
        self.assertIn("runs-on: ubuntu-latest", self.pr_lint)
        self.assertNotIn("blacksmith", self.pr_lint)

    def test_agents_code_review_rules(self) -> None:
        self.assertIn("## Code Review Rules", self.agents)
        self.assertIn("CODEX-GATE: FIX", self.agents)
        self.assertIn("CODEX-GATE: READY", self.agents)
        self.assertIn("CODEX-GATE: MEDIATE", self.agents)
        self.assertIn("When you are the assignee", self.agents)
        self.assertIn("Parallel Task API deep research", self.agents)
        self.assertIn("in addition to Search/Extract", self.agents)
        self.assertIn("Grok Bots do not research or resolve decision issues", self.agents)
        self.assertIn("neither a Devin substitute", self.agents)
        self.assertIn("Human-only findings need a human `@claude` mention", self.agents)
        self.assertIn("A founder may have their own bot", self.agents)
        self.assertIn("Stuck-thread watch", self.agents)
        self.assertIn("Stuck PR threads:", self.agents)
        self.assertIn("stuck-pr-threads:", self.agents)
        self.assertIn("already approved", self.agents)
        self.assertIn("codex-mediation:", self.agents)
        self.assertIn("needs-decision", self.agents)
        self.assertIn("do **not** add a `CODEX-GATE:` line", self.agents)
        self.assertIn("aggregator is GitHub-hosted", self.agents)
        self.assertIn("Python 3.11+3.12 matrix it wraps runs on Blacksmith", self.agents)
        self.assertIn("fails closed until `ready_for_review`", self.agents)
        self.assertIn("ci-label-retrigger.yml", self.agents)
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("AGENTS.md", claude)
        self.assertIn("Grok Bots", claude)

    def test_pat_job_token_has_narrow_receipt_scope_and_actionlint_is_checksummed(self) -> None:
        self.assertIn("contents: read", self.text)
        self.assertIn("pull-requests: read", self.text)
        self.assertIn("issues: read", self.text)
        self.assertNotIn("actions: write", self.text)
        # Only the two mutually exclusive PAT jobs may write receipts.
        jobs = yaml.load(self.text, Loader=yaml.BaseLoader)["jobs"]
        self.assertEqual({name for name, job in jobs.items()
                          if (job.get("permissions") or {}).get("issues") == "write"}, {"legacy", "gate"})
        self.assertEqual(self.text.count("issues: write"), 2)
        self.assertNotIn("pull-requests: write", self.text)
        self.assertIn("ACTIONLINT_SHA256", self.text)
        self.assertIn("sha256sum -c", self.text)
        self.assertNotIn("download-actionlint.bash", self.text)
        self.assertIn("LINEAR_API_KEY", self.text)

    def test_does_not_print_secret_names_as_values(self) -> None:
        source = GATE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("print(pat)", source)
        self.assertNotIn("print(os.environ", source)


class ActionlintTests(unittest.TestCase):
    def test_actionlint_when_available(self) -> None:
        import shutil

        exe = shutil.which("actionlint")
        if not exe:
            self.skipTest("actionlint not on PATH")
        result = subprocess.run(
            [exe, "-color", str(WORKFLOW_PATH), str(CLAUDE_WORKFLOW), str(CI_WORKFLOW)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class CliSecretTests(unittest.TestCase):
    def test_cli_missing_event_path_does_not_fail(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "CODEX_TRIGGER_PAT"}
        env.update(
            {
                "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_REPOSITORY": REPO,
                "GITHUB_EVENT_PATH": "/nonexistent/event.json",
            }
        )
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", str(GATE_PATH)],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::notice::", result.stdout)
        self.assertIn("CODEX_TRIGGER_PAT", result.stdout)


class PausedReadinessTests(unittest.TestCase):
    def pause(self):
        helper = mod.FIXER_RECEIPTS
        return {"id": 10, "user": {"login": "github-actions[bot]", "type": "Bot", "id": 41898282},
                "body": helper.marker("paused", {"head": SHA, "fingerprint": "f" * 64})}

    def act(self, gh, verdict="READY"):
        return mod._act(verdict, REPO, "42", SHA, gh.comments, "2026-10-16", gh)

    def test_known_label_rejection_retries_but_transport_stays_uncertain(self):
        for error, settled in [(subprocess.CalledProcessError(1, ["gh"], stderr="gh: denied (HTTP 403)"), True),
                               (subprocess.CalledProcessError(1, ["gh"], stderr="gh: failure (HTTP 500)"), False),
                               (OSError("connection lost"), False)]:
            gh = FakeGH(); original = gh.post
            def fail_label(path, body):
                if path.endswith("/labels"):
                    raise error
                return original(path, body)
            with patch.object(gh, "post", side_effect=fail_label):
                with self.assertRaises(type(error)):
                    self.act(gh)
            self.assertEqual(mod.FIXER_RECEIPTS.pending_ci_intent(gh.events, gh.comments) is None, settled)
            if settled:
                self.act(gh)
                self.assertTrue(mod.FIXER_RECEIPTS.label_event_id(gh.events))

    def test_rejection_receipt_failure_keeps_intent_uncertain(self):
        gh = FakeGH(); original = gh.post_read_token
        def fail_rejection(path, body):
            if "claude-fixer-ci-rejected:" in body.get("body", ""):
                raise OSError("receipt endpoint unavailable")
            return original(path, body)
        with patch.object(gh, "post", side_effect=subprocess.CalledProcessError(1, "gh", stderr="(HTTP 403)")), patch.object(gh, "post_read_token", side_effect=fail_rejection):
            with self.assertRaises(OSError):
                self.act(gh)
        self.assertTrue(mod.FIXER_RECEIPTS.pending_ci_intent(gh.events, gh.comments))

    def test_human_removal_preserved_only_for_observed_head(self):
        gh = FakeGH(); self.act(gh); gh.delete(f"repos/{REPO}/issues/42/labels/ci:full")
        self.assertIn("preserve-human", self.act(gh)[0])
        newsha = "b" * 40
        gh.prs = [pr_payload(head_sha=newsha)]
        mod._act("READY", REPO, "42", newsha, gh.comments, "x", gh)
        self.assertTrue(mod.FIXER_RECEIPTS.label_event_id(gh.events))

    def test_recovery_isolates_first_pr_api_error(self):
        gh = FakeGH(); gh.prs = [pr_payload(number=42), pr_payload(number=43)]
        original = gh.get
        def fail_first(path):
            if "/issues/42/comments" in path:
                raise OSError("first PR unavailable")
            return original(path)
        with patch.object(gh, "get", side_effect=fail_first):
            logs = mod._handle_recovery({}, "schedule", REPO, gh, datetime.now(timezone.utc), "x")
        self.assertTrue(any("pr=42 recovery=api-error" in line for line in logs))
        self.assertTrue(any("pr=43" in line for line in logs))

    def test_stale_label_listing_error_does_not_prevent_review_request(self):
        gh = FakeGH(); original = gh.get
        def fail_events(path):
            if "/events" in path:
                raise ValueError("bad events")
            return original(path)
        with patch.object(gh, "get", side_effect=fail_events):
            logs = mod._post_request_if_needed(gh, REPO, pr_payload(), [], clear_stale_ready=True)
        self.assertTrue(any("clear-label=error" in line for line in logs))
        self.assertTrue(any("request=posted" in line for line in logs))

    def test_mediate_then_human_clears_allows_samehead_ready(self):
        gh = FakeGH()
        gh.comments = [comment(mod.action_marker(SHA, "MEDIATE"))]
        ready_review = review("CODEX-GATE: READY")
        verdict = mod.decide_verdict(sha=SHA, review_body="CODEX-GATE: READY", reviews=[ready_review], comments=gh.comments, day="x")
        self.assertEqual(verdict, "READY")
        self.assertEqual(mod.decide_recovery(sha=SHA, reviews=[ready_review], comments=gh.comments,
                         now=datetime.now(timezone.utc), day="x"), "READY")
        with patch.object(mod.FIXER_RECEIPTS, "live_state", return_value=([], [])):
            self.act(gh, verdict)
        self.assertTrue(mod.FIXER_RECEIPTS.label_event_id(gh.events))
        self.assertEqual(mod.decide_verdict(sha=SHA, review_body="CODEX-GATE: MEDIATE", reviews=[],
                         comments=[comment(mod.action_marker(SHA, "MEDIATE"))], day="x"), "skip-already-mediate")

    def test_pause_in_final_audit_gap_recovers_without_terminal_deadend(self):
        gh = FakeGH(); original = gh.post; helper = mod.FIXER_RECEIPTS
        def paused_at_audit(path, body):
            if "codex-gate-action:" in body.get("body", ""):
                helper.revoke_owned_ci(REPO, "42", SHA, lambda p: mod.strict_items(gh, p),
                    lambda repo, num: gh.delete_read_token(f"repos/{repo}/issues/{num}/labels/ci:full"),
                    lambda repo, num, text: gh.post_read_token(f"repos/{repo}/issues/{num}/comments", {"body": text}))
            return original(path, body)
        with patch.object(gh, "post", side_effect=paused_at_audit):
            self.act(gh)
        self.assertEqual(helper.label_event_id(gh.events), 0)
        verdict = mod.decide_verdict(sha=SHA, review_body="CODEX-GATE: READY", reviews=[], comments=gh.comments, day="x")
        self.assertEqual(verdict, "READY")
        self.act(gh, verdict)
        self.assertTrue(helper.label_event_id(gh.events))

    def test_uncertain_event_lag_persists_across_restart(self):
        gh = FakeGH(); original = gh.get
        def lag(path):
            if "/events" in path:
                return []
            return original(path)
        with patch.object(gh, "get", side_effect=lag):
            with self.assertRaises(ValueError):
                self.act(gh)
        self.assertTrue(mod.FIXER_RECEIPTS.label_event_id(gh.events))
        before = len(gh.posts)
        self.assertIn("unknown-label-ownership", self.act(gh)[0])
        self.assertEqual(len(gh.posts), before)
        self.assertFalse(any("codex-gate-action:" in b.get("body", "") for _, b in gh.posts))

    def test_intent_failure_never_mutates_label(self):
        gh = FakeGH()
        with patch.object(gh, "post_read_token", side_effect=OSError("intent unavailable")):
            with self.assertRaises(OSError):
                self.act(gh)
        self.assertFalse(gh.events)

    def test_live_findings_always_block_and_handoff_does_not_mean_ready(self):
        gh = FakeGH(); unresolved = [{"root": 1}]
        with patch.object(mod.FIXER_RECEIPTS, "live_state", return_value=([], unresolved)):
            self.assertIn("pending", self.act(gh)[0])
        self.assertFalse(gh.posts)

    def test_clean_present_label_is_idempotent_and_manual_removal_preserved(self):
        gh = FakeGH(); self.act(gh)
        before = len(gh.posts)
        self.assertIn("already-present", self.act(gh)[0])
        self.assertEqual(len(gh.posts), before)
        gh.delete(f"repos/{REPO}/issues/42/labels/ci:full")
        self.assertIn("preserve-human", self.act(gh)[0])
        self.assertEqual(len(gh.posts), before)
        observations = sum("claude-fixer-ci-removal:" in c["body"] for c in gh.comments)
        self.assertEqual(observations, 1)
        self.act(gh)
        self.assertEqual(sum("claude-fixer-ci-removal:" in c["body"] for c in gh.comments), 1)

    def test_stale_event_cannot_revoke_new_head_label(self):
        gh = FakeGH(); self.act(gh)
        gh.prs = [pr_payload() | {"head": {"sha": "b" * 40, "repo": {"full_name": REPO}}}]
        self.assertIn("stale", self.act(gh)[0])
        self.assertFalse(gh.deletes)

    def test_ownership_write_permission_is_gate_job_only(self):
        text = WORKFLOW_PATH.read_text()
        top = text.split("jobs:", 1)[0]
        self.assertIn("issues: read", top)
        self.assertNotIn("issues: write", top)
        gate = text.split("  gate:", 1)[1].split("  event-paths:", 1)[0]
        self.assertIn("issues: write", gate)
        self.assertIn("contents: read", gate)
        self.assertIn("pull-requests: read", gate)
        self.assertNotIn("issues: write", text.split("  event-paths:", 1)[1])

    def test_ownership_receipt_uses_actions_token_not_pat(self):
        gh = mod.GitHub(read_token="read-token-fixture", write_token="write-token-fixture")
        with patch.object(gh, "_run", return_value='{"id":1}') as run:
            gh.post_read_token("repos/acme/example/issues/42/comments", {"body": "receipt"})
        self.assertEqual(run.call_args.kwargs["token"], "read-token-fixture")

    def test_ready_old_marker_after_pause_is_not_terminal_without_checked_receipt(self):
        comments = [self.pause(), {"id": 11, "body": mod.action_marker(SHA, "READY"), "user": {"login": "github-actions[bot]"}}]
        self.assertEqual(mod.decide_verdict(sha=SHA, review_body="CODEX-GATE: READY", reviews=[], comments=comments, day="x"), "READY")
        checked = {"id": 12, "body": mod.FIXER_RECEIPTS.marker("ci-checked", {"head": SHA, "pause": 10}),
                   "user": {"login": "github-actions[bot]", "type": "Bot", "id": 41898282}}
        comments += [checked, comments[1] | {"id": 13}]
        self.assertEqual(mod.decide_verdict(sha=SHA, review_body="CODEX-GATE: READY", reviews=[], comments=comments, day="x"), "READY")

    def test_pause_before_ready_blocks_without_terminal_marker(self):
        gh = FakeGH(); gh.comments = [self.pause()]
        with patch.object(mod.FIXER_RECEIPTS, "readiness_blocked", return_value=True):
            logs = self.act(gh)
        self.assertIn("fixer-pending", logs[0])
        self.assertFalse(gh.posts)

    def test_pause_during_label_write_revokes_and_never_posts_ready(self):
        gh = FakeGH()
        with patch.object(mod.FIXER_RECEIPTS, "readiness_blocked", side_effect=[False, True]):
            with self.assertRaises(ValueError):
                self.act(gh)
        self.assertEqual(len(gh.deletes), 1)
        self.assertFalse(any("codex-gate-action:" in body.get("body", "") for _, body in gh.posts))

    def test_pause_after_ready_revokes_owned_label_then_samehead_can_resume(self):
        gh = FakeGH()
        self.act(gh)
        self.assertTrue(any("codex-gate-action:" in body.get("body", "") for _, body in gh.posts))
        gh.comments.append(self.pause() | {"id": 2000})
        with patch.object(mod.FIXER_RECEIPTS, "readiness_blocked", return_value=True):
            self.act(gh, "skip-already-ready")
        self.assertEqual(len(gh.deletes), 1)
        with patch.object(mod.FIXER_RECEIPTS, "readiness_blocked", return_value=False):
            self.act(gh)
        self.assertEqual(sum("codex-gate-action:" in body.get("body", "") for _, body in gh.posts), 2)

    def test_repeated_pause_lifecycle_same_original_marker_and_duplicate_events(self):
        gh = FakeGH(); helper = mod.FIXER_RECEIPTS
        items = [{"root": 1, "fingerprint": "e" * 64, "thread": "t"}]
        pause = self.pause() | {"body": helper.marker("paused", helper.attempt_key(SHA, items))}
        gh.comments = [pause]
        with patch.object(helper, "live_state", return_value=([], [])) as live:
            for cycle in range(2):
                verdict = mod.decide_recovery(sha=SHA, comments=gh.comments,
                    reviews=[{"user": {"login": mod.CODEX_BOT}, "commit_id": SHA, "body": "CODEX-GATE: READY"}], now=datetime.now(timezone.utc), day="x")
                self.assertEqual(verdict, "READY")
                self.act(gh, verdict)
                terminal = {"id": max(c["id"] for c in gh.comments) + 1, "body": mod.action_marker(SHA, "READY"),
                            "user": {"login": "github-actions[bot]"}}
                gh.comments.append(terminal)
                self.assertEqual(mod.decide_recovery(sha=SHA, comments=gh.comments, reviews=[], now=datetime.now(timezone.utc), day="x"), "skip-no-verdict")
                live.return_value = (items, items)
                self.act(gh, "skip-already-ready")
                self.assertEqual(helper.label_event_id(gh.events), 0)
                live.return_value = ([], [])
            self.assertEqual(len(gh.deletes), 2)

    def test_ownership_post_failure_rolls_back_exact_label_event(self):
        gh = FakeGH()
        original = gh.post_read_token
        failed = False
        def fail_once(path, body):
            nonlocal failed
            if not failed and "claude-fixer-ci-owned:" in body.get("body", ""):
                failed = True
                raise OSError("receipt unavailable")
            return original(path, body)
        with patch.object(gh, "post_read_token", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.act(gh)
        self.assertEqual(len(gh.deletes), 1)
        self.assertEqual(mod.FIXER_RECEIPTS.label_event_id(gh.events), 0)
        self.assertFalse(any("codex-gate-action:" in b.get("body", "") for _,b in gh.posts))

    def test_invalidation_post_failure_never_deletes_before_receipt(self):
        gh = FakeGH(); self.act(gh)
        with patch.object(mod.FIXER_RECEIPTS, "readiness_blocked", return_value=True), patch.object(gh, "post_read_token", side_effect=OSError("receipt unavailable")):
            with self.assertRaises(OSError):
                self.act(gh, "skip-already-ready")
        self.assertFalse(gh.deletes)

    def test_stale_head_and_event_pagination_fail_closed(self):
        gh = FakeGH(); gh.prs = [pr_payload() | {"head": {"sha": "b" * 40, "repo": {"full_name": REPO}}}]
        self.assertIn("stale", self.act(gh)[0]); self.assertFalse(gh.posts)
        gh = FakeGH()
        with patch.object(gh, "get", side_effect=OSError("pagination unavailable")):
            with self.assertRaises(OSError):
                self.act(gh)
        self.assertFalse(gh.posts)


if __name__ == "__main__":
    unittest.main()
