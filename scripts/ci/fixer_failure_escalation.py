#!/usr/bin/env python3
"""Read-only evidence for exhausted, failed fixers; never dispatch or resolve.

Reservation comments establish budget only. GitHub's attempt-specific job
metadata independently binds the failing action to the workflow-emitted key.
No terminal prose, model statement or Actions-authored result is failure proof.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "escalation_receipts", Path(__file__).with_name("claude_fixer_receipts.py")
)
receipts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(receipts)
CONTEXT_PREFIX = "Fixer attempt "
WORKFLOW = ".github/workflows/claude.yml"
ACTION_USES = "anthropics/claude-code-action@v1"
FAILURES = {"failure", "timed_out"}


class Unverified(RuntimeError):
    """Incomplete evidence must not admit or close an escalation."""


class ReadUnavailable(Unverified):
    def __init__(self, status=None):
        super().__init__("evidence read unavailable")
        self.status = status


class Reader:
    def __init__(self):
        self.deadline = time.monotonic() + 60
        self.calls = 0
        self.public_anonymous = False

    def read(self, path, body=None, *, public=False, retry=True, anonymous=False):
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        if self.calls > 60 or remaining <= 0:
            raise Unverified("evidence read budget exhausted")
        timeout = min(10, remaining)
        try:
            if public and (anonymous or self.public_anonymous or not os.environ.get("GH_TOKEN")):
                # Only public Actions metadata, after repository visibility check.
                # No PAT, credential forwarding, permission change or log download.
                request = urllib.request.Request(
                    "https://api.github.com/" + path,
                    headers={"Accept": "application/vnd.github+json", "User-Agent": "fixer-failure-watch"},
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read(4_000_001)
                if len(raw) > 4_000_000:
                    raise Unverified("oversized evidence response")
            else:
                args = ["gh", "api", path, "--method", "POST" if body else "GET"]
                if body:
                    args += ["--input", "-"]
                result = subprocess.run(args, input=json.dumps(body) if body else None,
                                        capture_output=True, text=True, timeout=timeout, check=True)
                raw = result.stdout
            if len(raw) > 4_000_000:
                raise Unverified("oversized evidence response")
            return json.loads(raw)
        except subprocess.CalledProcessError as error:
            match = re.search(r"\(HTTP ([0-9]{3})\)", str(error.stderr or ""))
            status = int(match[1]) if match else None
            if public and status == 403:
                # The repository was independently verified public. Read only
                # this public metadata without a token if the scoped token
                # cannot read it; never change scopes or select another PAT.
                self.public_anonymous = True
                return self.read(path, body, public=True, retry=retry, anonymous=True)
            if retry and (status == 429 or status in {500, 502, 503, 504}):
                time.sleep(min(1, max(0, self.deadline - time.monotonic())))
                return self.read(path, body, public=public, retry=False, anonymous=anonymous)
            raise ReadUnavailable(status) from error
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            status = getattr(error, "code", None)
            if retry and (status == 429 or status in {500, 502, 503, 504}):
                time.sleep(min(1, max(0, self.deadline - time.monotonic())))
                return self.read(path, body, public=public, retry=False, anonymous=anonymous)
            raise ReadUnavailable(status) from error


def listing(read, path):
    result, seen = [], set()
    for page in range(1, 6):
        batch = read(f"{path}?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise Unverified("invalid comment listing")
        for item in batch:
            identity = item.get("id")
            if type(identity) is not int or identity <= 0 or identity in seen:
                raise Unverified("ambiguous comment listing")
            seen.add(identity)
            result.append(item)
        if len(batch) < 100:
            return result
    raise Unverified("comment page limit")


def thread_snapshot(read, repo, number):
    owner, name = repo.split("/")
    result, cursors, ids = [], set(), set()
    cursor = None
    for _ in range(5):
        data = read("graphql", {"query": receipts.QUERY, "variables": {
            "owner": owner, "name": name, "number": number, "cursor": cursor}})
        if data.get("errors"):
            raise Unverified("thread read failed")
        page = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        for thread in page["nodes"]:
            tid = thread.get("id")
            if not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9_=-]{1,128}", tid) or tid in ids:
                raise Unverified("ambiguous thread listing")
            ids.add(tid)
            result.append(thread)
        if page["pageInfo"]["hasNextPage"] is False:
            return result
        cursor = page["pageInfo"]["endCursor"]
        if not cursor or cursor in cursors:
            raise Unverified("invalid thread cursor")
        cursors.add(cursor)
    raise Unverified("thread page limit")


def pr_identity(pr, repo, number):
    head = pr.get("head") or {}
    user = pr.get("user") or {}
    return (pr.get("number") == number and pr.get("state") == "open"
            and pr.get("draft") is False and user.get("type") == "User"
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", str(user.get("login") or ""))
            and (head.get("repo") or {}).get("full_name") == repo
            and receipts.SHA_RE.fullmatch(str(head.get("sha") or ""))
            and head.get("ref") not in {"main", "master"})


def snapshot(read, repo, number):
    pr = read(f"repos/{repo}/pulls/{number}")
    if not pr_identity(pr, repo, number):
        return None
    head = pr["head"]["sha"]
    permissions = {}

    def permission(login):
        if login not in permissions:
            permissions[login] = read(f"repos/{repo}/collaborators/{login}/permission")["permission"]
        return permissions[login]

    if permission(pr["user"]["login"]) not in {"write", "maintain", "admin"}:
        return None
    history = listing(read, f"repos/{repo}/issues/{number}/comments")
    # Cheap exit before GraphQL/model-independent receipt reconciliation.
    candidates = [r for c in history
                  if receipts.bot(c.get("user") or {}, "github-actions[bot]", receipts.ACTIONS_ID)
                  for r in receipts.records(str(c.get("body") or ""), "attempt")
                  if r.get("head") == head]
    if len(candidates) < receipts.MAX_ATTEMPTS:
        return None
    threads = thread_snapshot(read, repo, number)
    comments = listing(read, f"repos/{repo}/pulls/{number}/comments")
    if hasattr(receipts, "review_snapshot"):
        threads, comments = receipts.review_snapshot(threads, comments)
    items = (receipts.findings(threads, comments) if hasattr(receipts, "review_snapshot")
             else receipts.findings(threads, comments, strict_snapshot=True))
    if hasattr(receipts, "prepare_observations"):
        states = receipts.observation_roots(threads, comments, history, items)

        def refuse_write(_body):
            raise Unverified("observation history needs the locked fixer")

        observed = receipts.prepare_observations(states, comments, history, permission, refuse_write)
        eligible = {item["root"] for item in items}
        items = [item for item in observed if item["root"] in eligible]

    comparisons = {}
    def compare(base, tip):
        key = base, tip
        if key not in comparisons:
            try:
                comparisons[key] = read(f"repos/{repo}/compare/{base}...{tip}")["status"]
            except ReadUnavailable as error:
                if error.status not in {404, 422}:
                    raise
                comparisons[key] = "invalid"
        return comparisons[key]

    items = receipts.pending_findings(items, comments, head, compare, permission)
    key = receipts.attempt_key(head, items)
    if not items or receipts.attempt_count(history, key) < receipts.MAX_ATTEMPTS:
        return None
    attempts = set()
    for record in candidates:
        run, attempt = str(record.get("run") or ""), str(record.get("attempt") or "")
        if (all(record.get(k) == v for k, v in key.items())
                and re.fullmatch(r"[1-9][0-9]{0,19}", run)
                and re.fullmatch(r"[1-9][0-9]{0,5}", attempt)):
            attempts.add((int(run), int(attempt)))
    if len(attempts) < receipts.MAX_ATTEMPTS:
        return None
    return {"key": key, "items": items, "attempts": sorted(attempts), "pr": pr}


def failed_run(read, repo, number, state, run_id, attempt, workflow_id, default_branch, workflow_sha):
    run = read(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}", public=True)
    pr, key = state["pr"], state["key"]
    # Actions attempt metadata may qualify the path with @ref. The workflow ID,
    # repository, head and attempt-specific job context remain independently bound.
    path = run.get("path")
    path_matches = isinstance(path, str) and re.fullmatch(re.escape(WORKFLOW) + r"(?:@[^\s]+)?", path)
    if not (run.get("id") == run_id and run.get("run_attempt") == attempt
            and run.get("workflow_id") == workflow_id and path_matches
            and run.get("event") in {"pull_request_review", "repository_dispatch"}
            and run.get("status") == "completed" and run.get("conclusion") in FAILURES
            and (run.get("repository") or {}).get("full_name") == repo
            and (run.get("head_repository") or {}).get("full_name") == repo):
        return None
    if run["event"] == "repository_dispatch":
        # Dispatch metadata identifies the default branch, not the target PR.
        # Bind its immutable workflow blob before trusting the reserved run's
        # successful head/fingerprint context step below as the target proof.
        dispatch_sha = run.get("head_sha")
        if (not isinstance(dispatch_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", dispatch_sha)
                or run.get("head_branch") != default_branch):
            return None
        executed = read(f"repos/{repo}/contents/{WORKFLOW}?ref={dispatch_sha}")
        if not workflow_sha or executed.get("sha") != workflow_sha:
            return None
    elif (run.get("head_sha") != key["head"] or run.get("head_branch") != pr["head"]["ref"]
          or not any(p.get("number") == number for p in run.get("pull_requests", []))):
        return None
    jobs = read(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", public=True)
    if jobs.get("total_count") != len(jobs.get("jobs", [])) or len(jobs["jobs"]) > 100:
        raise Unverified("incomplete job listing")
    matching = [j for j in jobs["jobs"] if j.get("name") == "devin-review-fix"]
    if len(matching) != 1:
        return None
    job = matching[0]
    if (job.get("run_id") != run_id or job.get("run_attempt") != attempt or job.get("status") != "completed"
            or job.get("conclusion") not in FAILURES or type(job.get("id")) is not int):
        return None
    steps = job.get("steps") or []
    context = [s for s in steps if s.get("name") == CONTEXT_PREFIX + key["head"] + ":" + key["fingerprint"]]
    action = [s for s in steps if s.get("name") == f"Run {ACTION_USES}"]
    action_failed = len(action) == 1 and (action[0].get("conclusion") in FAILURES or (
        action[0].get("conclusion") == "cancelled" and job.get("conclusion") == "timed_out"
        and run.get("conclusion") == "timed_out"))
    if (len(context) != 1 or context[0].get("conclusion") != "success"
            or not action_failed
            or context[0].get("number", 0) >= action[0].get("number", 0)):
        return None
    return f"https://github.com/{repo}/actions/runs/{run_id}/attempts/{attempt}#job-{job['id']}"


class Evidence:
    def __init__(self, repo, number, read):
        self.repo, self.number, self.read = repo, number, read
        self.state = None
        self.failure = ""

    def collect(self):
        try:
            return self._collect()
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
            raise Unverified("incomplete failure evidence") from error

    def _collect(self):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo) or type(self.number) is not int or self.number <= 0:
            raise Unverified("unsafe target")
        self.state = snapshot(self.read, self.repo, self.number)
        if self.state is None:
            return {}
        metadata = self.read(f"repos/{self.repo}")
        if metadata.get("private") is not False:
            raise Unverified("public metadata access not established")
        # Only the current trusted workflow definition can produce the step key.
        from urllib.parse import quote
        head = self.state["key"]["head"]
        default = quote(metadata["default_branch"], safe="")
        current = self.read(f"repos/{self.repo}/contents/{WORKFLOW}?ref={head}")
        trusted = self.read(f"repos/{self.repo}/contents/{WORKFLOW}?ref={default}")
        if not current.get("sha") or current.get("sha") != trusted.get("sha"):
            raise Unverified("fixer workflow differs from default branch")
        workflow = self.read(f"repos/{self.repo}/actions/workflows/claude.yml", public=True)
        if workflow.get("path") != WORKFLOW or type(workflow.get("id")) is not int:
            raise Unverified("workflow identity unavailable")
        # Three is the unchanged automatic budget; never scan unbounded history.
        for run_id, attempt in self.state["attempts"][-receipts.MAX_ATTEMPTS:]:
            self.failure = failed_run(self.read, self.repo, self.number, self.state, run_id, attempt,
                                      workflow["id"], metadata["default_branch"], trusted["sha"]) or ""
            if self.failure:
                break
        if not self.failure:
            return {}
        return {item["thread"]: {**item, "description": self.description()}
                for item in self.state["items"]}

    def description(self):
        return ("Automatic attempt budget exhausted on unchanged current findings; "
                "a context-bound fixer action failed. Reservations are not three model failures. "
                f"Head: {self.state['key']['head']}. Evidence: {self.failure}")

    def fresh(self):
        if not self.state:
            return False
        # The watcher never enters the writer queue or dispatches a replacement.
        # Refuse observed active/pending writers; these reads are not an atomic lock.
        for status in ("queued", "in_progress", "waiting", "pending", "requested"):
            # issue_comment and manual dispatch writers may carry the default
            # branch in metadata, so branch-filtered reads cannot prove quiet.
            runs = self.read(f"repos/{self.repo}/actions/workflows/claude.yml/runs?status={status}&per_page=1", public=True)
            if runs.get("total_count") != 0:
                return False
        current = snapshot(self.read, self.repo, self.number)
        if not (current and current["key"] == self.state["key"]
                and current["attempts"] == self.state["attempts"]
                and current["pr"]["head"]["ref"] == self.state["pr"]["head"]["ref"]):
            return False
        final = self.read(f"repos/{self.repo}/pulls/{self.number}")
        return bool(pr_identity(final, self.repo, self.number)
                    and final["head"]["sha"] == current["key"]["head"]
                    and final["head"]["ref"] == current["pr"]["head"]["ref"])


def collect(repo, number):
    evidence = Evidence(repo, number, Reader().read)
    return evidence, evidence.collect()
