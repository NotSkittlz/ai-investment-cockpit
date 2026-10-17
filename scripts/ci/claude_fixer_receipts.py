#!/usr/bin/env python3
"""Locked fixer eligibility, explicit receipts, and per-head attempt budget."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
import time

DEVIN_ID = 158243242
CLAUDE_ID = 209825114
ACTIONS_ID = 41898282
MAX_ATTEMPTS = 3
SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
METADATA_RE = re.compile(r"<!-- devin-review-comment (\{.*?\}) -->")
QUERY = """query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
 repository(owner:$owner, name:$name) { pullRequest(number:$number) {
  reviewThreads(first:100, after:$cursor) {
   pageInfo { hasNextPage endCursor }
   nodes { id isResolved isOutdated comments(first:1) { nodes {
    databaseId author { login __typename } body
   } } }
  }
 } }
}"""


def digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def marker(kind: str, data: dict) -> str:
    return f"<!-- claude-fixer-{kind}:" + json.dumps(data, sort_keys=True, separators=(",", ":")) + " -->"


def records(body: str, kind: str) -> list[dict]:
    found = []
    prefix = f"<!-- claude-fixer-{kind}:"
    for match in re.finditer(re.escape(prefix) + r"(.*?)\s*-->", body, re.DOTALL):
        try:
            value = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def bot(user: dict, login: str, identity: int) -> bool:
    return user.get("login") == login and user.get("type") == "Bot" and user.get("id") == identity


def findings(threads: list[dict], comments: list[dict], strict_snapshot: bool = False) -> list[dict]:
    found = []
    for thread in threads:
        if thread.get("isResolved") is not False or thread.get("isOutdated") is not False:
            continue
        roots = (thread.get("comments") or {}).get("nodes") or []
        if not roots:
            continue
        root = roots[0]
        author = root.get("author") or {}
        if author.get("__typename") != "Bot" or author.get("login") not in {"devin-ai-integration", "devin-ai-integration[bot]"}:
            continue
        body = str(root.get("body") or "")
        matches = METADATA_RE.findall(body)
        if len(matches) != 1:
            continue
        try:
            metadata = json.loads(matches[0])
        except ValueError:
            continue
        if not isinstance(metadata, dict) or metadata.get("kind") != "bug":
            continue
        # REST supplies immutable numeric bot identity, absent from Actor above.
        corroborated = [c for c in comments if c.get("id") == root.get("databaseId")
                        and not c.get("in_reply_to_id")
                        and bot(c.get("user") or {}, "devin-ai-integration[bot]", DEVIN_ID)
                        and c.get("body") == body]
        if len(corroborated) != 1:
            if strict_snapshot:
                raise ValueError("inconsistent GraphQL/REST finding snapshot")
            continue
        root_id = root.get("databaseId")
        if type(root_id) is int and root_id > 0:
            found.append({"root": root_id, "fingerprint": digest(body), "thread": str(thread.get("id") or "")})
    return sorted(found, key=lambda item: item["root"])


def substantive_followup(comment: dict, permission) -> bool:
    body = str(comment.get("body") or "").strip()
    # Quoting a prior receipt must not hide a new substantive correction.
    body = re.sub(r"<!-- claude-fixer-result:.*?-->", "", body, flags=re.DOTALL).strip()
    if not body or re.fullmatch(r"[\s>`]*", body):
        return False
    user = comment.get("user") or {}
    if bot(user, "devin-ai-integration[bot]", DEVIN_ID):
        # Exclude known analysis metadata; a new bug report or nonempty direct
        # Devin follow-up can reopen an explicit handoff, never mark success.
        matches = METADATA_RE.findall(body)
        if matches:
            try:
                return any(json.loads(value).get("kind") == "bug" for value in matches)
            except (ValueError, AttributeError):
                return False
        return not body.startswith("<!--")
    if user.get("type") != "User":
        return False
    login = str(user.get("login") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login):
        return False
    return permission(login) in {"write", "maintain", "admin"}


def newer_comment(comment, receipt):
    stamp = str(comment.get("updated_at") or comment.get("created_at") or "")
    previous = str(receipt.get("created_at") or "")
    if stamp != previous:
        return stamp > previous
    cid, rid = comment.get("id"), receipt.get("id")
    edited = str(comment.get("updated_at") or "") > str(comment.get("created_at") or "")
    # An edit to an older reply can share the receipt's second. IDs order
    # creations only, so conservatively rearm a demonstrably edited reply.
    return edited or (type(cid) is int and type(rid) is int and cid > rid)


def pending_findings(items: list[dict], comments: list[dict], head: str, compare, permission, manual: bool = False) -> list[dict]:
    pending = []
    for item in items:
        replies = [c for c in comments if c.get("in_reply_to_id") == item["root"]]
        terminal = False
        generations = set()
        for reply in replies:
            if not bot(reply.get("user") or {}, "claude[bot]", CLAUDE_ID):
                continue
            for receipt in records(str(reply.get("body") or ""), "result"):
                if type(receipt.get("root")) is not int or receipt.get("root") != item["root"] or receipt.get("fingerprint") != item["fingerprint"]:
                    continue
                base = str(receipt.get("base") or "")
                if not SHA_RE.fullmatch(base):
                    continue
                stamp = str(reply.get("created_at") or "")
                if not stamp:
                    continue
                status = receipt.get("status")
                valid = False
                if status == "addressed":
                    commit = str(receipt.get("commit") or "")
                    valid = bool(SHA_RE.fullmatch(commit)) and compare(base, commit) == "ahead" and compare(commit, head) in {"ahead", "identical"}
                elif status == "needs-human":
                    valid = compare(base, head) in {"ahead", "identical"}
                if not valid:
                    continue
                followups = [c for c in replies if newer_comment(c, reply) and substantive_followup(c, permission)]
                if followups:
                    generations.update(digest(str((c.get("user") or {}).get("login")) + "\n" + str(c.get("body") or "")) for c in followups)
                    continue
                if status == "addressed" or not manual:
                    terminal = True
        if not terminal:
            pending.append({**item, **({"generation": digest(json.dumps(sorted(generations)))} if generations else {})})
    return pending


def terminal_receipts(items, comments, head, compare, permission):
    """Read-only evidence for captured roots; never changes eligibility or journals."""
    reports = []
    for item in items:
        root = item["root"]
        roots = [c for c in comments if c.get("id") == root and not c.get("in_reply_to_id")]
        if (len(roots) != 1 or not bot(roots[0].get("user") or {}, "devin-ai-integration[bot]", DEVIN_ID)
                or digest(str(roots[0].get("body") or "")) != item["fingerprint"]):
            reports.append({"root": root, "status": "root-changed-or-unverified"})
            continue
        replies = [c for c in comments if c.get("in_reply_to_id") == root]

        # Restrict this audit to the workflow-captured base. Reuse the existing
        # consumer for identity, schema, full SHA, ancestry and follow-up rules.
        scoped = []
        for comment in comments:
            if bot(comment.get("user") or {}, "claude[bot]", CLAUDE_ID):
                receipts = [r for r in records(str(comment.get("body") or ""), "result")
                            if r.get("base") == item["base"]]
                comment = {**comment, "body": "\n".join(marker("result", r) for r in receipts)}
            scoped.append(comment)
        if not pending_findings([item], scoped, head, compare, permission, manual=True):
            status = "verified-addressed"
        elif not pending_findings([item], scoped, head, compare, permission):
            status = "verified-needs-human"
        elif any("<!-- claude-fixer-result:" in str(c.get("body") or "") for c in replies):
            status = "invalid-or-superseded"
        else:
            status = "missing"
        reports.append({"root": root, "status": status})
    return reports


def attempt_key(head: str, items: list[dict]) -> dict:
    canonical = json.dumps(sorted(items, key=lambda item: item["root"]), sort_keys=True, separators=(",", ":"))
    return {"head": head, "fingerprint": digest(canonical)}


def attempt_count(comments: list[dict], key: dict) -> int:
    seen = set()
    for comment in comments:
        if not bot(comment.get("user") or {}, "github-actions[bot]", ACTIONS_ID):
            continue
        for record in records(str(comment.get("body") or ""), "attempt"):
            if any(record.get(k) != value for k, value in key.items()):
                continue
            run, attempt = str(record.get("run") or ""), str(record.get("attempt") or "")
            if run.isdigit() and int(run) > 0 and attempt.isdigit() and int(attempt) > 0:
                seen.add((run, attempt))
    return len(seen)


def api(path: str):
    return json.loads(subprocess.check_output(["gh", "api", path], text=True, stderr=subprocess.PIPE))


def pages(path: str) -> list[dict]:
    result, page = [], 1
    while True:
        batch = api(f"{path}?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("invalid GitHub listing")
        result.extend(batch)
        if len(batch) < 100:
            return result
        page += 1


def post(repo: str, number: str, body: str) -> None:
    subprocess.check_call(["gh", "api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}"], stdout=subprocess.DEVNULL)


def emit(items: list[dict], head: str, reason: str = "") -> None:
    context = []
    for item in items:
        context.append({**item, "base": head})
    ids = [item["root"] for item in items]
    thread_ids = [item["thread"] for item in items]
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"attempt_key={head}:{attempt_key(head, items)['fingerprint']}\n")
        output.write(f"skip={'false' if items else 'true'}\ncomment_ids={json.dumps(ids)}\nthread_ids={json.dumps(thread_ids)}\nreceipt_context={json.dumps(context, separators=(',', ':'))}\nreason={reason}\n")
    print(json.dumps(ids))


def live_state(repo: str, number: str, head: str) -> tuple[list[dict], list[dict]]:
    owner, name = repo.split("/")
    threads, cursor = [], None
    while True:
        args = ["gh", "api", "graphql", "-f", f"query={QUERY}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        data = json.loads(subprocess.check_output(args, text=True))
        page = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        threads.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        next_cursor = page["pageInfo"]["endCursor"]
        if not next_cursor or next_cursor == cursor:
            raise ValueError("invalid thread cursor")
        cursor = next_cursor
    comparisons, permissions = {}, {}

    def compare(base, tip):
        key = (base, tip)
        if key not in comparisons:
            try:
                comparisons[key] = api(f"repos/{repo}/compare/{base}...{tip}")["status"]
            except subprocess.CalledProcessError as error:
                # A nonexistent/non-comparable SHA invalidates only this receipt.
                # Authentication, rate-limit, transport and server errors still abort.
                if not re.search(r"\(HTTP (?:404|422)\)", str(error.stderr or "")):
                    raise
                comparisons[key] = "invalid"
        return comparisons[key]

    def permission(login):
        if login not in permissions:
            permissions[login] = api(f"repos/{repo}/collaborators/{login}/permission")["permission"]
        return permissions[login]

    review_comments = pages(f"repos/{repo}/pulls/{number}/comments")
    roots = findings(threads, review_comments, strict_snapshot=True)
    return (pending_findings(roots, review_comments, head, compare, permission, False),
            pending_findings(roots, review_comments, head, compare, permission, True))


def label_event_id(events: list[dict]) -> int:
    relevant = [e for e in events if e.get("event") in {"labeled", "unlabeled"}
                and (e.get("label") or {}).get("name") == "ci:full"
                and type(e.get("id")) is int]
    latest = max(relevant, key=lambda e: e["id"], default={})
    return latest.get("id", 0) if latest.get("event") == "labeled" else 0


def ci_label_decision(events, comments, head=None):
    relevant = sorted((e for e in events if e.get("event") in {"labeled", "unlabeled"}
        and (e.get("label") or {}).get("name") == "ci:full" and type(e.get("id")) is int), key=lambda e: e["id"])
    if not relevant:
        return "allow"
    latest = relevant[-1]
    if latest["event"] == "labeled":
        return "present"
    prior = next((e for e in reversed(relevant[:-1]) if e["event"] == "labeled"), {})
    proven = bot(latest.get("actor") or {}, "github-actions[bot]", ACTIONS_ID) and any(
        bot(c.get("user") or {}, "github-actions[bot]", ACTIONS_ID)
        and any(type(r.get("event")) is int and r["event"] == prior.get("id")
                for r in records(str(c.get("body") or ""), "ci-invalidated")) for c in comments)
    observed = [r for c in comments if bot(c.get("user") or {}, "github-actions[bot]", ACTIONS_ID)
                for r in records(str(c.get("body") or ""), "ci-removal")
                if r.get("event") == latest["id"] and SHA_RE.fullmatch(str(r.get("head") or ""))]
    if head and observed and all(r["head"] != head for r in observed):
        return "allow"
    return "allow" if proven else "preserve-human-removal"


def known_label_rejection(error):
    """Only an explicit server rejection proves this POST did not mutate."""
    if not isinstance(error, subprocess.CalledProcessError):
        return None
    match = re.search(r"\(HTTP (400|401|403|404|409|422|429)\)", str(error.stderr or ""))
    return int(match.group(1)) if match else None


def begin_ci_intent(repo, number, head, events, post_comment):
    """Persist uncertainty before mutation; intent alone is never ownership."""
    after = max((e["id"] for e in events if type(e.get("id")) is int
                 and e.get("event") in {"labeled", "unlabeled"}
                 and (e.get("label") or {}).get("name") == "ci:full"), default=0)
    intent = {"head": head, "nonce": uuid.uuid4().hex, "after": after}
    post_comment(repo, number, marker("ci-intent", intent))
    return intent


def pending_ci_intent(events, comments):
    trusted = [c for c in comments if bot(c.get("user") or {}, "github-actions[bot]", ACTIONS_ID)]
    rejected = {r.get("nonce") for c in trusted for r in records(str(c.get("body") or ""), "ci-rejected")
                if r.get("status") in {400, 401, 403, 404, 409, 422, 429}}
    owned = {r.get("nonce") for c in trusted for r in records(str(c.get("body") or ""), "ci-owned")
             if SHA_RE.fullmatch(str(r.get("head") or "")) and type(r.get("event")) is int and r["event"] > 0}
    for comment in trusted:
        for intent in records(str(comment.get("body") or ""), "ci-intent"):
            nonce, after = intent.get("nonce"), intent.get("after")
            if not re.fullmatch(r"[0-9a-f]{32}", str(nonce or "")) or not SHA_RE.fullmatch(str(intent.get("head") or "")) or type(after) is not int or after < 0:
                continue
            superseded = any(type(e.get("id")) is int and e["id"] > after
                             and e.get("event") == "unlabeled"
                             and bool(comment.get("created_at"))
                             and str(e.get("created_at") or "") > str(comment["created_at"])
                             and (e.get("label") or {}).get("name") == "ci:full" for e in events)
            rollback_events = {r["event"] for c in trusted for r in records(str(c.get("body") or ""), "ci-invalidated")
                               if r.get("nonce") == nonce and type(r.get("event")) is int and r["event"] > after}
            rolled_back = any(type(e.get("id")) is int and e.get("event") == "unlabeled"
                              and (e.get("label") or {}).get("name") == "ci:full"
                              and bot(e.get("actor") or {}, "github-actions[bot]", ACTIONS_ID)
                              and any(e["id"] > event_id for event_id in rollback_events) for e in events)
            if nonce not in owned and nonce not in rejected and not superseded and not rolled_back:
                return nonce
    return None


def record_ci_ownership(repo, number, head, before_id, writer, get_pages, post_comment, proof=None, intent=None, sleep=None):
    # Bound immediate event visibility retries. A later invocation must not
    # infer attribution from a PAT actor: its durable intent stays uncertain.
    sleep = sleep or time.sleep
    for attempt in range(3):
        if attempt:
            sleep(1 << (attempt - 1))
        events = get_pages(f"repos/{repo}/issues/{number}/events")
        current = label_event_id(events)
        if current:
            break
    if current and current == before_id:
        return False  # Pre-existing labels are not ours.
    event = next((e for e in events if e.get("id") == current), {})
    if not current or (event.get("actor") or {}).get("login") != writer:
        raise ValueError("ci:full ownership unknown; manually remove/re-add ci:full to reconcile")
    if proof is not None:
        proof.append(current)
    receipt = {"head": head, "event": current}
    if intent is not None:
        receipt["nonce"] = intent["nonce"]
    post_comment(repo, number, marker("ci-owned", receipt))
    return True


def rollback_ci_event(repo, number, event_id, get_pages, delete):
    if event_id and label_event_id(get_pages(f"repos/{repo}/issues/{number}/events")) == event_id:
        delete(repo, number)
        return True
    return False


def delete_label(repo, number):
    subprocess.check_call(["gh", "api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/ci:full"], stdout=subprocess.DEVNULL)


def revoke_owned_ci(repo, number, head, get_pages=None, delete=None, post_comment=None):
    get_pages, delete = get_pages or pages, delete or delete_label
    comments = get_pages(f"repos/{repo}/issues/{number}/comments")
    current = label_event_id(get_pages(f"repos/{repo}/issues/{number}/events"))
    if not current:
        return False
    owned = any(bot(c.get("user") or {}, "github-actions[bot]", ACTIONS_ID)
                and any(SHA_RE.fullmatch(str(r.get("head") or "")) and type(r.get("event")) is int
                        and r["event"] == current for r in records(str(c.get("body") or ""), "ci-owned"))
                for c in comments)
    if owned:
        # GitHub has no compare-and-delete: a human relabel between this check
        # and DELETE is an unavoidable micro-race, not an atomic guarantee.
        # Invalidate before deletion: a failed delete remains retryable and a
        # later clear cannot get stuck behind a stale checked READY generation.
        (post_comment or post)(repo, number, marker("ci-invalidated", {"head": head, "event": current}))
        delete(repo, number)
    return owned


def exhausted_attempt_id(comments, head):
    groups = {}
    for comment in comments:
        cid = comment.get("id")
        if type(cid) is not int or cid <= 0 or not bot(comment.get("user") or {}, "github-actions[bot]", ACTIONS_ID):
            continue
        for record in records(str(comment.get("body") or ""), "attempt"):
            fingerprint = str(record.get("fingerprint") or "")
            run, attempt = str(record.get("run") or ""), str(record.get("attempt") or "")
            if record.get("head") != head or not DIGEST_RE.fullmatch(fingerprint) or not run.isdigit() or int(run) <= 0 or not attempt.isdigit() or int(attempt) <= 0:
                continue
            group = groups.setdefault(fingerprint, {})
            key = (run, attempt)
            group[key] = min(cid, group.get(key, cid))
    return max((sorted(group.values())[MAX_ATTEMPTS - 1] for group in groups.values()
                if len(group) >= MAX_ATTEMPTS), default=0)


def latest_pause_id(comments: list[dict], head: str) -> int:
    ids = []
    for comment in comments:
        if not bot(comment.get("user") or {}, "github-actions[bot]", ACTIONS_ID):
            continue
        body = str(comment.get("body") or "")
        if (any(r.get("head") == head and DIGEST_RE.fullmatch(str(r.get("fingerprint") or ""))
                for r in records(body, "paused"))
                or any(r.get("head") == head and type(r.get("event")) is int and r["event"] > 0
                       for r in records(body, "ci-invalidated"))
                or f"<!-- claude-fixer-loop-cap:{head} -->" in body):
            value = comment.get("id")
            if type(value) is int and value > 0:
                ids.append(value)
    return max([exhausted_attempt_id(comments, head), *ids])


def latest_ready_check(comments, head, pause):
    return max((c["id"] for c in comments
        if type(c.get("id")) is int and c["id"] > pause
        and bot(c.get("user") or {}, "github-actions[bot]", ACTIONS_ID)
        and any(r.get("head") == head and r.get("pause") == pause
                for r in records(str(c.get("body") or ""), "ci-checked"))), default=0)


def readiness_blocked(repo: str, number: str, head: str, comments: list[dict], read_api=None) -> bool:
    """Read-only reconciliation; an obsolete fingerprint alone never blocks READY."""
    pr = (read_api or api)(f"repos/{repo}/pulls/{number}")
    if (pr.get("state") != "open" or pr.get("draft") is not False
            or (pr.get("head") or {}).get("sha") != head
            or ((pr.get("head") or {}).get("repo") or {}).get("full_name") != repo):
        return True
    # READY concerns all unsettled findings, not merely automatic retry budget.
    # The second view retains needs-human handoffs but excludes verified fixes.
    _, unresolved = live_state(repo, number, head)
    return bool(unresolved)


def main() -> None:
    repo, number, head = (os.environ[k] for k in ("REPO", "PR_NUMBER", "FIXER_HEAD_SHA"))
    if not SHA_RE.fullmatch(head):
        raise ValueError("unsafe head")
    manual = os.environ.get("MANUAL_RETRY") == "true"
    automatic, unresolved = live_state(repo, number, head)
    if unresolved:
        pr = api(f"repos/{repo}/pulls/{number}")
        if (pr.get("state") != "open" or (pr.get("head") or {}).get("sha") != head
                or ((pr.get("head") or {}).get("repo") or {}).get("full_name") != repo):
            emit([], head, "stale-head")
            return
        revoke_owned_ci(repo, number, head)
    items = unresolved if manual else automatic
    if not items:
        emit([], head, "no-pending-findings")
        return
    count = os.environ["LOOP_COMMIT_COUNT"]
    if not re.fullmatch(r"0|[1-9][0-9]*", count):
        raise ValueError("invalid loop commit count")
    comments = pages(f"repos/{repo}/issues/{number}/comments")
    if int(count) >= 8:
        pause = f"<!-- claude-fixer-loop-cap:{head} -->"
        if not any(bot(c.get("user") or {}, "github-actions[bot]", ACTIONS_ID) and pause in str(c.get("body") or "") for c in comments):
            post(repo, number, pause + "\nClaude PR Fixer paused: eight consecutive Claude commits and pending Devin findings. A human needs to inspect them; a human commit resets this guard. Manual retry does not bypass it.")
        emit([], head, "loop-commit-limit")
        return
    key = attempt_key(head, items)
    if not manual and attempt_count(comments, key) >= MAX_ATTEMPTS:
        pause = marker("paused", key)
        if not any(bot(c.get("user") or {}, "github-actions[bot]", ACTIONS_ID) and pause in str(c.get("body") or "") for c in comments):
            post(repo, number, pause + "\nClaude PR Fixer paused after three automatic attempts on this head and unchanged findings. Attempts include runs stopped by workflow validation before model execution. After activation, an authorized manual devin-review-fix workflow dispatch can retry; a new head or changed findings resets this budget. The eight-commit guard still applies.")
        emit([], head, "attempt-limit")
        return
    if not manual:
        run, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
        if not run.isdigit() or not attempt.isdigit():
            raise ValueError("invalid run identity")
        # This is an attempt reservation, not success. Under the shared PR lock,
        # a failed post aborts before any model work and no later step can run.
        post(repo, number, marker("attempt", {**key, "run": run, "attempt": attempt}))
    emit(items, head)


if __name__ == "__main__":
    main()
