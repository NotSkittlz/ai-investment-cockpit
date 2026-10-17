#!/usr/bin/env python3
"""Workflow-owned, single-attempt pre-push guard and non-success terminal audit.

Installed outside the PR tree from the workflow's embedded, tested source.
No fetch, retry, history repair, new credential, or handled-result receipt.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
REF = re.compile(r"(?!.*\.\.)[A-Za-z0-9._/-]+\Z")


def command(args, **kwargs):
    kwargs.setdefault("timeout", 30)
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, **kwargs).strip()


def api(path, timeout=30):
    return json.loads(command(["gh", "api", path], timeout=timeout))


class AuditReadError(RuntimeError):
    """Read failures must not be confused with observation validation."""


def bounded_reader():
    deadline = time.monotonic() + 70
    calls = 0
    def read(path):
        nonlocal calls
        remaining = deadline - time.monotonic()
        calls += 1
        if remaining <= 0 or calls > 40:
            raise AuditReadError("terminal read budget exhausted")
        try:
            return api(path, timeout=min(10, remaining))
        except ValueError as error:
            raise AuditReadError("invalid terminal API response") from error
    return read


def receipt_audit(config, read, consumer):
    items = config.get("receipts", [])
    if isinstance(items, list) and not items:
        return "Per-root receipt audit: not applicable (no captured receipt context)."
    try:
        if consumer is None or not isinstance(items, list) or len(items) > 50:
            raise ValueError("receipt context unavailable or over limit")
        roots = set()
        for item in items:
            rid = item.get("root")
            if (type(rid) is not int or rid <= 0 or rid in roots
                    or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("fingerprint") or ""))
                    or item.get("base") != config["base"]):
                raise ValueError("invalid captured receipt context")
            roots.add(rid)
        head = current_pr(config, read)
        comments = []
        for page in range(1, 6):
            batch = read(f"repos/{config['repo']}/pulls/{config['number']}/comments?per_page=100&page={page}")
            if not isinstance(batch, list) or any(not isinstance(c, dict) for c in batch):
                raise ValueError("invalid comment snapshot")
            comments.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise ValueError("comment page limit reached")
        ids = [c.get("id") for c in comments]
        if any(type(cid) is not int or cid <= 0 for cid in ids) or len(set(ids)) != len(ids):
            raise ValueError("inconsistent comment snapshot")
        cache = {}
        def cached(path):
            if path not in cache:
                cache[path] = read(path)
            return cache[path]
        def compare(base, tip):
            try:
                return cached(f"repos/{config['repo']}/compare/{base}...{tip}")["status"]
            except subprocess.CalledProcessError as error:
                if re.search(r"\(HTTP (?:404|422)\)", str(error.stderr or "")):
                    return "invalid"
                raise
        def permission(login):
            return cached(f"repos/{config['repo']}/collaborators/{login}/permission")["permission"]
        reports = consumer.terminal_receipts(items, comments, head, compare, permission)
        if current_pr(config, read) != head:
            raise ValueError("head changed during receipt audit")
        details = "; ".join(f"{r['root']}={r['status']}" for r in reports)
        return f"Per-root receipt audit at `{head}`: {details}."
    except (AuditReadError, OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        return "Per-root receipt audit: unverified (read, context, snapshot or bounded-budget failure)."


def validate(config):
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config["repo"])
            or not re.fullmatch(r"[1-9][0-9]*", config["number"])
            or not SHA.fullmatch(config["base"])
            or not REF.fullmatch(config["ref"])
            or config["ref"] in {"main", "master", "HEAD"}):
        raise ValueError("invalid guard context")


def current_pr(config, read=api):
    pr = read(f"repos/{config['repo']}/pulls/{config['number']}")
    head = pr.get("head") or {}
    if (pr.get("state") != "open" or head.get("ref") != config["ref"]
            or (head.get("repo") or {}).get("full_name") != config["repo"]
            or not SHA.fullmatch(str(head.get("sha") or ""))):
        raise ValueError("PR identity changed")
    return head["sha"]


def expected_remote(config, remote_url):
    """Accept only canonical GitHub transports; never interpret/log credentials."""
    if not isinstance(remote_url, str):
        return False
    # Limit userinfo to URL-unreserved token characters. A second @, encoded
    # delimiter, empty port or control byte must not reach a different parser.
    repo = re.escape(config["repo"])
    return bool(re.fullmatch(
        rf"(?:https://(?:x-access-token:[A-Za-z0-9._~-]+@)?(?i:github\.com)/(?i:{repo})(?:\.git)?"
        rf"|ssh://git@(?i:github\.com)/(?i:{repo})(?:\.git)?"
        rf"|git@(?i:github\.com):(?i:{repo})\.git)", remote_url, flags=re.ASCII))


def guard(config, updates, remote, state_path, read=api, git=command, remote_url=""):
    """One ordinary push at most; consume the attempt even on a rejected check."""
    validate(config)
    # Exclusive creation prevents retry after either a hook refusal or a later
    # server rejection. A fresh authorized workflow run gets a fresh directory.
    with state_path.open("x", encoding="utf-8") as handle:
        handle.write("{}")
    if not expected_remote(config, remote_url):
        raise ValueError("unexpected remote URL")
    rows = [line.split() for line in updates.splitlines() if line.strip()]
    if remote != "origin" or len(rows) != 1 or len(rows[0]) != 4:
        raise ValueError("unexpected push target")
    local_ref, candidate, remote_ref, advertised = rows[0]
    if (local_ref != "HEAD" or remote_ref != "refs/heads/" + config["ref"]
            or not SHA.fullmatch(candidate) or candidate == config["base"]
            or advertised != config["base"]):
        raise ValueError("remote moved or invalid update")
    if git(["git", "rev-parse", "HEAD"]) != candidate:
        raise ValueError("local head changed")
    if git(["git", "merge-base", config["base"], candidate]) != config["base"]:
        raise ValueError("fix does not descend from checkout")
    # The API check is deliberately last. Git's ordinary server-side rejection
    # remains authoritative if an external writer wins the race after this read.
    if current_pr(config, read) != config["base"]:
        raise ValueError("remote moved")
    state_path.write_text(json.dumps({"candidate": candidate}), encoding="utf-8")
    print(f"Guard checked candidate {candidate}; push is not yet confirmed. "
          "Use the full commit ID only after successful push and live-head verification.")


def execution_reason(path, outcome):
    """Read only coarse result fields; never echo model text or command inputs."""
    fallback = "execution-result-unavailable" if outcome == "success" else "model-step-failed-or-timed-out"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
        results = [r for r in items if isinstance(r, dict) and r.get("type") == "result"]
        if not results:
            return fallback
        for result in results:
            turns = result.get("num_turns")
            if (type(turns) is int and turns > 30) or result.get("subtype") == "error_max_turns":
                return "turn-limit"
            if result.get("permission_denials"):
                return "command-denied"
            if type(turns) is not int or turns < 0:
                return fallback
            if result.get("is_error") or result.get("subtype") != "success":
                return "model-error"
        return "model-completed" if outcome == "success" else fallback
    except (OSError, ValueError, TypeError):
        return fallback


def terminal(config, state_path, execution, outcome, read=api, consumer=None):
    validate(config)
    reason = execution_reason(execution, outcome)
    pushed = "unverified"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        candidate = state.get("candidate", "")
        live = current_pr(config, read)
        if SHA.fullmatch(candidate) and candidate != config["base"]:
            base_compare = read(f"repos/{config['repo']}/compare/{config['base']}...{candidate}")
            live_compare = read(f"repos/{config['repo']}/compare/{candidate}...{live}")
            if base_compare.get("status") == "ahead" and live_compare.get("status") in {"ahead", "identical"}:
                pushed = candidate
    except (AuditReadError, OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        pass  # Unknown is not successful, and never a needs-human result receipt.
    return (f"Claude fixer terminal audit: {reason}. Checkout `{config['base']}`. "
            f"Guarded candidate reachable on the live PR: `{pushed}`.\n\n"
            + receipt_audit(config, read, consumer) + "\n\n"
            "This audit is not an addressed or needs-human finding receipt. "
            "Receipt evidence is a current snapshot for captured roots and may predate this run; "
            "it does not prove this run fixed them or establish current eligibility or READY. "
            "A fresh authorized run must recheck genuine unresolved, current findings "
            "and existing author permissions, three-attempt budget and eight-commit guard. "
            "Do not reopen resolved findings or retry this checkout. No fetch, rebase or merge recovery. "
            "The PR author should relay any remaining blocker to its Linear issue.")


def model_api(config, args, run=command):
    """Fixed same-PR API operations. No model-supplied endpoint, method or flags."""
    validate(config)
    root = f"repos/{config['repo']}"
    pull = f"{root}/pulls/{config['number']}"
    reads = {"read": pull, "comments": f"{root}/issues/{config['number']}/comments",
             "review-comments": f"{pull}/comments", "reviews": f"{pull}/reviews"}
    if args == ["threads"]:
        owner, name = config["repo"].split("/")
        # Fixed read-only query. The root maps REST review comments to a current
        # thread; pagination covers all thread states without accepting a query.
        query = """query($owner:String!,$name:String!,$number:Int!,$endCursor:String){
          repository(owner:$owner,name:$name){pullRequest(number:$number){
            reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor}
              nodes{id isResolved isOutdated path line comments(first:1){nodes{databaseId body url}}}}}}}"""
        return run(["gh", "api", "graphql", "--paginate", "-f", "query=" + query,
                    "-f", "owner=" + owner, "-f", "name=" + name, "-F", "number=" + config["number"]])
    if len(args) == 1 and args[0] in reads:
        paging = [] if args[0] == "read" else ["--paginate"]
        return run(["gh", "api", "--method", "GET", reads[args[0]], *paging])
    if len(args) == 2 and args[0] == "comment":
        endpoint, body_path = f"{root}/issues/{config['number']}/comments", args[1]
    elif len(args) == 3 and args[0] == "reply" and re.fullmatch(r"[1-9][0-9]*", args[1]):
        endpoint, body_path = f"{pull}/comments/{args[1]}/replies", args[2]
    else:
        raise ValueError("unsupported PR API operation")
    # Do not let a comment-file read bypass the model's denied .env/secret reads.
    if not re.fullmatch(r"\.claude-fixer-replies/[A-Za-z0-9_-]+\.txt", body_path):
        raise ValueError("invalid comment file")
    workspace = Path.cwd().resolve()
    directory = workspace / ".claude-fixer-replies"
    file = workspace / body_path
    if directory.is_symlink() or file.is_symlink() or file.resolve().parent != directory:
        raise ValueError("comment file escapes reply directory")
    # Nonblocking open plus descriptor type check rejects FIFOs before reading,
    # including a file replaced between the earlier path check and open.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(file, flags), "r", encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("comment file is not regular")
        body = handle.read(65001)
    if not body.strip() or len(body) > 65000:
        raise ValueError("invalid comment body")
    if args[0] == "reply":
        # Do not rely on server routing to bind a repository-wide comment ID.
        target = json.loads(run(["gh", "api", "--method", "GET", f"{root}/pulls/comments/{args[1]}"]))
        if (not isinstance(target, dict) or type(target.get("id")) is not int
                or target["id"] != int(args[1]) or target.get("in_reply_to_id") is not None
                or str(target.get("pull_request_url") or "").casefold() != ("https://api.github.com/" + pull).casefold()):
            raise ValueError("reply target is not a root on this PR")
    return run(["gh", "api", "--method", "POST", endpoint, "--input", "-"], input=json.dumps({"body": body}))


def main():
    directory = Path(__file__).resolve().parent
    config_path, state_path = directory / "context.json", directory / "attempt.json"
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "install" and len(sys.argv) == 2:
        config = {key: os.environ[env] for key, env in (
            ("repo", "REPO"), ("number", "PR_NUMBER"),
            ("base", "HEAD_SHA"), ("ref", "HEAD_REF"))}
        validate(config)
        config["receipts"] = json.loads(os.environ.get("RECEIPT_CONTEXT") or "[]")
        config_path.write_text(json.dumps(config), encoding="utf-8")
        Path(__file__).chmod(0o700)
        command(["git", "config", "--local", "core.hooksPath", str(directory)])
        return 0
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if mode == "model-api":
        try:
            if len(sys.argv) < 4 or sys.argv[2] != "--":
                raise ValueError("invalid model-api invocation")
            print(model_api(config, sys.argv[3:]))
        except (OSError, ValueError, subprocess.SubprocessError):
            print("PR API operation failed or is not permitted.", file=sys.stderr)
            return 1
        return 0
    if mode == "terminal" and len(sys.argv) == 2:
        consumer = None
        try:
            spec = importlib.util.spec_from_file_location("workflow_receipts", directory / "receipts.py")
            consumer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(consumer)
        except (OSError, ImportError, ValueError, AttributeError, SyntaxError, TypeError):
            pass
        body = terminal(config, state_path, os.environ.get("EXECUTION_FILE", ""),
                        os.environ.get("MODEL_OUTCOME", ""), read=bounded_reader(), consumer=consumer)
        # Summary survives a denied/unavailable comment API. Do not log its error
        # payload or silently claim the remote handback was posted.
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(body + "\n")
        print(body)
        try:
            command(["gh", "api", "--method", "POST",
                     f"repos/{config['repo']}/issues/{config['number']}/comments", "--input", "-"],
                    input=json.dumps({"body": body}))
        except (OSError, subprocess.SubprocessError):
            print("::error::Terminal audit saved to job summary; PR handback could not be confirmed.")
            return 1
        return 0
    try:
        if len(sys.argv) != 3:
            raise ValueError("invalid hook invocation")
        guard(config, sys.stdin.read(), mode, state_path, remote_url=sys.argv[2])
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Claude fixer push stopped: stale/invalid head, unavailable read, or attempt already used. "
              "Stop this run; no fetch, rebase, merge or second push.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
