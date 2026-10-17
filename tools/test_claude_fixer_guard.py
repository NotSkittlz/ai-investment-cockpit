#!/usr/bin/env python3
"""Offline push races and terminal handback; no live/provider qualification."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/ci/claude_fixer_guard.py"
spec = importlib.util.spec_from_file_location("guard", SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
A, B, C = "a" * 40, "b" * 40, "c" * 40
URL = "https://github.com/acme/example.git"
CONFIG = {"repo": "acme/example", "number": "7", "base": A, "ref": "fix"}


def pr(sha=A):
    return {"state": "open", "head": {"sha": sha, "ref": "fix", "repo": {"full_name": "acme/example"}}}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.state = self.path / "attempt.json"
        self.execution = self.path / "execution.json"
        self.execution.write_text(json.dumps([{"type": "result", "subtype": "success", "num_turns": 12}]))

    def push(self, read=lambda _: pr(), updates=None, config=CONFIG, remote="origin"):
        return m.guard(config, f"HEAD {B} refs/heads/fix {A}\n" if updates is None else updates, remote, self.state, remote_url=URL,
                       read=read, git=lambda args: B if args[1] == "rev-parse" else A)

    def test_live_head_movement_stops_and_consumes_attempt(self):
        with self.assertRaisesRegex(ValueError, "remote moved"):
            self.push(lambda _: pr(C))
        with self.assertRaises(FileExistsError):
            self.push()
        self.assertEqual(json.loads(self.state.read_text()), {})

    def test_api_denial_stops_without_retry(self):
        def denied(_):
            raise subprocess.CalledProcessError(1, "gh")
        with self.assertRaises(subprocess.CalledProcessError):
            self.push(denied)
        with self.assertRaises(FileExistsError):
            self.push()

    def test_advertised_remote_movement_stops_even_if_api_is_stale(self):
        with self.assertRaises(ValueError):
            self.push(updates=f"HEAD {B} refs/heads/fix {C}\n")

    def test_valid_push_check_records_candidate_not_success(self):
        self.push()
        self.assertEqual(json.loads(self.state.read_text()), {"candidate": B})
        with self.assertRaises(FileExistsError):
            self.push()

    def test_wrong_target_closed_fork_and_missing_identity_are_rejected(self):
        for bad in [pr() | {"state": "closed"}, {"state": "open", "head": {}},
                    pr() | {"head": pr()["head"] | {"ref": "other"}},
                    pr() | {"head": pr()["head"] | {"repo": {"full_name": "fork/example"}}}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                m.current_pr(CONFIG, lambda _, bad=bad: bad)
        for ref in ("main", "master", "HEAD", "../bad", "fix\nother"):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                m.validate(CONFIG | {"ref": ref})

    def test_delete_multiple_refs_no_change_and_non_origin_rejected(self):
        for rows, remote in [("", "origin"),
                             (f"(delete) {'0'*40} refs/heads/fix {A}", "origin"),
                             (f"HEAD {B} refs/tags/fix {A}", "origin"),
                             (f"HEAD {'0'*40} refs/heads/fix {A}", "origin"),
                             (f"HEAD {A} refs/heads/fix {A}", "origin"),
                             (f"HEAD {B} refs/heads/fix {A}\nHEAD {B} refs/heads/other {A}", "origin"),
                             (f"HEAD {B} refs/heads/fix {A}", "other")]:
            with self.subTest(rows=rows, remote=remote):
                self.state.unlink(missing_ok=True)
                with self.assertRaises(ValueError):
                    self.push(updates=rows, remote=remote)

    def test_remote_authority_rejects_ambiguous_or_encoded_delimiters(self):
        for authority in ("x-access-token:fixture@evil.invalid@github.com",
                          "x-access-token:fixture%40evil.invalid@github.com",
                          "x-access-token:fixture:other@github.com", "github.com:",
                          "x-access-token:fixture@github.com:", "github.com%2f.evil.invalid",
                          "github.com.evil.invalid", "github.com.", "gıthub.com", "gİthub.com", "github.com\x00.evil.invalid"):
            with self.subTest(authority=authority):
                self.assertFalse(m.expected_remote(CONFIG, "https://" + authority + "/acme/example.git"))

    def test_remote_url_must_bind_exact_repository(self):
        for url in ("https://github.com/acme/other.git", "https://evil.invalid/acme/example.git",
                    "https://github.com@evil.invalid/acme/example.git", "https://github.com/acme/example.git/evil",
                    "file:///tmp/remote.git", "", "git@evil.invalid:acme/example.git",
                    "https://other:fixture@github.com/acme/example.git", "https://github.com/acme/example.git?x=1",
                    "https://github.com/acme/example.git#fragment", "https://github.com:444/acme/example.git",
                    "https://github.com/acme/example.git\\evil", "https://github.com/ACME/Example.git\n"):
            with self.subTest(url=url):
                self.state.unlink(missing_ok=True)
                with self.assertRaisesRegex(ValueError, "remote URL"):
                    m.guard(CONFIG, f"HEAD {B} refs/heads/fix {A}", "origin", self.state, remote_url=url)
                self.assertEqual(json.loads(self.state.read_text()), {})

    def test_pr_api_facade_rejects_ref_mutations_methods_and_extra_flags(self):
        from unittest.mock import Mock
        run = Mock()
        for args in (["PATCH", "repos/acme/example/git/refs/heads/fix"],
                     ["DELETE", "repos/acme/example/git/refs/heads/fix"],
                     ["POST", "repos/acme/example/git/refs"], ["graphql"], ["threads", "mutation"],
                     ["read", "--method", "PATCH"], ["comment", "body", "--method", "DELETE"],
                     ["reply", "../git/refs", "body"], ["reply", "1?force=true", "body"],
                     ["reply", "1", "body", "extra"], ["comment", ".env"],
                     ["comment", "../secret"], ["comment", "/tmp/secret"], []):
            with self.subTest(args=args), self.assertRaises(ValueError):
                m.model_api(CONFIG, args, run=run)
        run.assert_not_called()

    def test_pr_api_facade_constructs_only_current_pr_reads_and_posts(self):
        from unittest.mock import Mock
        run = Mock(return_value=json.dumps({"id": 123, "pull_request_url": "https://api.github.com/repos/acme/example/pulls/7"}))
        for operation, endpoint in (("read", "pulls/7"), ("comments", "issues/7/comments"),
                                    ("review-comments", "pulls/7/comments"), ("reviews", "pulls/7/reviews")):
            m.model_api(CONFIG, [operation], run=run)
            self.assertEqual(run.call_args.args[0][:5], ["gh", "api", "--method", "GET", "repos/acme/example/" + endpoint])
        m.model_api(CONFIG, ["threads"], run=run)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["gh", "api", "graphql", "--paginate"])
        self.assertIn("isResolved isOutdated", argv[5])
        self.assertNotIn("mutation", argv[5])
        self.assertEqual(argv[-6:], ["-f", "owner=acme", "-f", "name=example", "-F", "number=7"])
        (self.path / ".claude-fixer-replies").mkdir()
        body = self.path / ".claude-fixer-replies/body.txt"
        body.write_text('Literal --method PATCH and $(secret) are comment data.', encoding="utf-8")
        for args, endpoint in ((["comment", ".claude-fixer-replies/body.txt"], "issues/7/comments"),
                               (["reply", "123", ".claude-fixer-replies/body.txt"], "pulls/7/comments/123/replies")):
            with patch.object(m.Path, "cwd", return_value=self.path):
                m.model_api(CONFIG, args, run=run)
            self.assertEqual(run.call_args.args[0], ["gh", "api", "--method", "POST", "repos/acme/example/" + endpoint, "--input", "-"])
            self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"body": body.read_text()})

    def test_reply_refuses_foreign_missing_malformed_or_non_root_targets(self):
        from unittest.mock import Mock
        (self.path / ".claude-fixer-replies").mkdir()
        (self.path / ".claude-fixer-replies/body.txt").write_text("Fix explanation", encoding="utf-8")
        valid = {"id": 123, "pull_request_url": "https://api.github.com/repos/acme/example/pulls/7"}
        for target in ([], {}, valid | {"id": 124}, valid | {"id": True},
                       valid | {"in_reply_to_id": 1}, valid | {"pull_request_url": valid["pull_request_url"] + "0"},
                       valid | {"pull_request_url": "https://api.github.com/repos/other/repo/pulls/7"}):
            with self.subTest(target=target), patch.object(m.Path, "cwd", return_value=self.path):
                run = Mock(return_value=json.dumps(target))
                with self.assertRaises(ValueError):
                    m.model_api(CONFIG, ["reply", "123", ".claude-fixer-replies/body.txt"], run=run)
                run.assert_called_once_with(["gh", "api", "--method", "GET", "repos/acme/example/pulls/comments/123"])
        for error in (subprocess.CalledProcessError(1, "gh"), ValueError("invalid JSON")):
            run = Mock(side_effect=error)
            with patch.object(m.Path, "cwd", return_value=self.path), self.assertRaises(type(error)):
                m.model_api(CONFIG, ["reply", "123", ".claude-fixer-replies/body.txt"], run=run)
            self.assertEqual(run.call_count, 1)

    def test_action_tokenized_and_case_normalized_urls_pass_without_logging_credentials(self):
        from contextlib import redirect_stdout
        from io import StringIO
        urls = ("https://x-access-token:fixture_only@github.com/acme/example.git",
                "https://GitHub.com/ACME/Example.git", "git@github.com:ACME/Example.git")
        for url in urls:
            with self.subTest(url=url.split("@")[-1]):
                self.state.unlink(missing_ok=True)
                output = StringIO()
                with redirect_stdout(output):
                    m.guard(CONFIG, f"HEAD {B} refs/heads/fix {A}", "origin", self.state,
                            remote_url=url, read=lambda _: pr(), git=lambda args: B if args[1] == "rev-parse" else A)
                self.assertEqual(json.loads(self.state.read_text()), {"candidate": B})
                self.assertNotIn("fixture_only", output.getvalue())

    def test_malformed_api_entry_does_not_read_stdin_or_burn_push_attempt(self):
        (self.path / "context.json").write_text(json.dumps(CONFIG))
        for args in (["model-api"], ["model-api", "--"], ["model-api", "read"]):
            with self.subTest(args=args), patch.object(m, "__file__", str(self.path / "pre-push")), \
                    patch.object(sys, "argv", ["pre-push", *args]), patch.object(sys, "stdin") as stdin:
                self.assertEqual(m.main(), 1)
                stdin.read.assert_not_called()
                self.assertFalse(self.state.exists())

    @unittest.skipIf(os.name == "nt", "POSIX FIFO/symlink fixtures")
    def test_reply_file_fifo_and_symlink_are_rejected_before_read(self):
        from unittest.mock import Mock
        directory = self.path / ".claude-fixer-replies"
        directory.mkdir()
        fifo = directory / "fifo.txt"
        os.mkfifo(fifo)
        secret = self.path / ".env"
        secret.write_text("fixture-only")
        (directory / "link.txt").symlink_to(secret)
        for name in ("fifo", "link"):
            with patch.object(m.Path, "cwd", return_value=self.path), \
                    patch.object(m.Path, "read_text", side_effect=AssertionError("must reject before read")), \
                    self.assertRaises(ValueError):
                m.model_api(CONFIG, ["comment", f".claude-fixer-replies/{name}.txt"], run=Mock())

    def test_local_rewrite_rejected(self):
        with self.assertRaises(ValueError):
            m.guard(CONFIG, f"HEAD {B} refs/heads/fix {A}", "origin", self.state, remote_url=URL,
                    read=lambda _: pr(), git=lambda args: B if args[1] == "rev-parse" else C)

    def test_no_false_handled_receipt_for_unpushed_or_unknown_candidate(self):
        self.push()
        for compare in ("behind", "diverged", None):
            body = m.terminal(CONFIG, self.state, str(self.execution), "success",
                              read=lambda path, compare=compare: pr(C) if "/pulls/" in path else {"status": compare})
            self.assertIn("`unverified`", body)
            self.assertNotIn("claude-fixer-result:", body)
            self.assertNotIn('"status":"addressed"', body)

    def test_reachable_candidate_audit_does_not_claim_findings_handled(self):
        self.push()
        body = m.terminal(CONFIG, self.state, str(self.execution), "success",
                          read=lambda path: pr(B) if "/pulls/" in path else {"status": "ahead"})
        self.assertIn(f"`{B}`", body)
        self.assertIn("not an addressed", body)
        self.assertNotIn("claude-fixer-result:", body)

    def test_denied_command_turn_exhaustion_and_missing_output_have_terminal_audit(self):
        for record, expected in [({"permission_denials": [{"tool_input": "PRIVATE"}]}, "command-denied"),
                                 ({"num_turns": 38}, "turn-limit"),
                                 ({"subtype": "error_max_turns"}, "turn-limit"),
                                 ({"is_error": True}, "model-error")]:
            data = {"type": "result", "subtype": "success", "num_turns": 12} | record
            self.execution.write_text(json.dumps([data]))
            for outcome in ("failure", "cancelled", "success"):
                self.assertEqual(m.execution_reason(str(self.execution), outcome), expected)
            body = m.terminal(CONFIG, self.state, str(self.execution), "failure")
            self.assertIn(expected, body)
            self.assertNotIn("PRIVATE", body)
            self.assertIn("three-attempt", body)
        self.assertEqual(m.execution_reason("", "failure"), "model-step-failed-or-timed-out")
        self.assertEqual(m.execution_reason("", "success"), "execution-result-unavailable")

    def test_remote_named_terminal_is_a_hook_invocation_not_a_terminal_command(self):
        (self.path / "context.json").write_text(json.dumps(CONFIG))
        with patch.object(m, "__file__", str(self.path / "pre-push")), \
                patch.object(sys, "argv", ["pre-push", "terminal", "https://example.invalid/repo"]), \
                patch.object(sys, "stdin") as stdin, patch.object(m, "terminal") as terminal:
            stdin.read.return_value = f"HEAD {B} refs/heads/fix {A}"
            self.assertEqual(m.main(), 1)
            terminal.assert_not_called()
        self.assertEqual(json.loads(self.state.read_text()), {})

    def test_turn_exhaustion_has_priority_over_recovered_denial(self):
        self.execution.write_text(json.dumps({"type": "result", "num_turns": 38,
                                             "permission_denials": ["private"]}))
        self.assertEqual(m.execution_reason(str(self.execution), "failure"), "turn-limit")

    def test_comment_denial_keeps_summary_and_returns_failure(self):
        (self.path / "context.json").write_text(json.dumps(CONFIG))
        summary = self.path / "summary"
        with patch.object(m, "__file__", str(self.path / "pre-push")), \
                patch.object(sys, "argv", ["pre-push", "terminal"]), \
                patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary), "MODEL_OUTCOME": "failure"}), \
                patch.object(m, "command", side_effect=subprocess.CalledProcessError(1, "gh")), \
                patch("builtins.print"):
            self.assertEqual(m.main(), 1)
        self.assertIn("model-step-failed", summary.read_text())


class RealGitRaceTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX runner hook launcher")
    def test_installed_hook_race_after_advertisement_keeps_server_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local, remote, hooks, binaries = [root / name for name in ("local", "remote.git", "hooks", "bin")]
            hooks.mkdir()
            binaries.mkdir()
            def git(*args, cwd=local, check=True, env=None):
                return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check, env=env)
            git("init", "--bare", str(remote), cwd=root)
            git("init", str(local), cwd=root)
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("checkout", "-b", "fix")
            (local / "file").write_text("base")
            git("add", "file")
            git("commit", "-m", "base")
            base = git("rev-parse", "HEAD").stdout.strip()
            git("remote", "add", "origin", str(remote))
            git("push", "origin", "HEAD:refs/heads/fix")
            tree = git("rev-parse", "HEAD^{tree}").stdout.strip()
            other = git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit-tree", tree, "-p", base, "-m", "external", cwd=remote).stdout.strip()
            # The hook receives Git's already-advertised base. During its final
            # API read, a separate writer advances the server. Return the old
            # observation so only the server's ordinary ref comparison can stop it.
            fake_api = binaries / "gh"
            fake_api.write_text("#!/usr/bin/env python3\nimport subprocess\nsubprocess.run(" +
                                repr(["git", "--git-dir", str(remote), "update-ref", "refs/heads/fix", other, base]) +
                                ", check=True)\nprint(" + repr(json.dumps(pr(base))) + ")\n")
            fake_api.chmod(0o700)
            hook = hooks / "pre-push"
            trusted = hooks / "guard.py"
            trusted.write_text(SOURCE.read_text(encoding="utf-8"))
            hook.write_text("#!/bin/sh\nexec " + sys.executable + " " + str(trusted) + ' "$1" ' + URL.replace('https://', 'https://x-access-token:fixture_only@') + "\n")
            hook.chmod(0o700)
            env = os.environ | {"REPO": CONFIG["repo"], "PR_NUMBER": "7", "HEAD_SHA": base,
                                "HEAD_REF": "fix", "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
            subprocess.run([sys.executable, str(trusted), "install"], cwd=local, env=env, check=True)
            (local / "file").write_text("fix")
            git("commit", "-am", "fix")
            candidate = git("rev-parse", "HEAD").stdout.strip()
            result = git("push", "origin", "HEAD:refs/heads/fix", check=False, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("remote rejected", result.stderr)
            self.assertEqual(git("rev-parse", "refs/heads/fix", cwd=remote).stdout.strip(), other)
            self.assertEqual(json.loads((hooks / "attempt.json").read_text()), {"candidate": candidate})
            retry = git("push", "origin", "HEAD:refs/heads/fix", check=False, env=env)
            self.assertNotEqual(retry.returncode, 0)
            self.assertIn("Stop this run", retry.stderr)

    def test_post_check_race_rejected_by_ordinary_push_then_no_second_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, local = root / "remote.git", root / "local"
            def git(*args, cwd=root, check=True):
                return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)
            git("init", "--bare", str(remote))
            git("init", str(local))
            git("config", "user.name", "Fixture", cwd=local)
            git("config", "user.email", "fixture@example.invalid", cwd=local)
            git("checkout", "-b", "fix", cwd=local)
            (local / "file").write_text("base")
            git("add", "file", cwd=local)
            git("commit", "-m", "base", cwd=local)
            base = git("rev-parse", "HEAD", cwd=local).stdout.strip()
            git("remote", "add", "origin", str(remote), cwd=local)
            git("push", "origin", "HEAD:refs/heads/fix", cwd=local)
            (local / "file").write_text("fix")
            git("commit", "-am", "fix", cwd=local)
            candidate = git("rev-parse", "HEAD", cwd=local).stdout.strip()
            config = CONFIG | {"base": base}
            state = root / "attempt.json"
            # Actual candidate, real ancestry, API fixture for the first check.
            m.guard(config, f"HEAD {candidate} refs/heads/fix {base}", "origin", state, remote_url=URL,
                    read=lambda _: pr(base), git=lambda args: git(*args[1:], cwd=local).stdout.strip())
            # External writer advances after the check. Copying candidate objects
            # into the fixture remote allows creating a divergent commit without fetch.
            git("push", "origin", "HEAD:refs/heads/fixture-objects", cwd=local)
            tree = git("rev-parse", f"{base}^{{tree}}", cwd=local).stdout.strip()
            other = subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                    "commit-tree", tree, "-p", base, "-m", "external"],
                                   cwd=remote, text=True, capture_output=True, check=True).stdout.strip()
            git("update-ref", "refs/heads/fix", other, base, cwd=remote)
            result = git("push", "origin", "HEAD:refs/heads/fix", cwd=local, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("rejected", result.stderr)
            self.assertEqual(git("rev-parse", "refs/heads/fix", cwd=remote).stdout.strip(), other)
            with self.assertRaises(FileExistsError):
                m.guard(config, f"HEAD {candidate} refs/heads/fix {other}", "origin", state, remote_url=URL)

    @unittest.skipIf(os.name == "nt", "POSIX runner hook launcher")
    def test_installed_hook_is_invoked_by_git_and_rejects_second_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local, remote, hooks, binaries = [root / name for name in ("local", "remote.git", "hooks", "bin")]
            hooks.mkdir()
            binaries.mkdir()
            def git(*args, cwd=root, check=True, env=None):
                return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check, env=env)
            git("init", "--bare", str(remote))
            git("init", str(local))
            git("config", "user.name", "Fixture", cwd=local)
            git("config", "user.email", "fixture@example.invalid", cwd=local)
            git("checkout", "-b", "fix", cwd=local)
            (local / "file").write_text("base")
            git("add", "file", cwd=local)
            git("commit", "-m", "base", cwd=local)
            base = git("rev-parse", "HEAD", cwd=local).stdout.strip()
            git("remote", "add", "origin", str(remote), cwd=local)
            git("push", "origin", "HEAD:refs/heads/fix", cwd=local)
            hook = hooks / "pre-push"
            trusted = hooks / "guard.py"
            trusted.write_text(SOURCE.read_text(encoding="utf-8"))
            hook.write_text("#!/bin/sh\nexec " + sys.executable + " " + str(trusted) + ' "$1" ' + URL.replace('https://', 'https://x-access-token:fixture_only@') + "\n")
            hook.chmod(0o700)
            fake_api = binaries / "gh"
            fake_api.write_text("#!/usr/bin/env python3\nprint(" + repr(json.dumps(pr(base))) + ")\n")
            fake_api.chmod(0o700)
            env = os.environ | {"REPO": CONFIG["repo"], "PR_NUMBER": "7", "HEAD_SHA": base,
                                "HEAD_REF": "fix", "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
            subprocess.run([sys.executable, str(trusted), "install"], cwd=local, env=env, check=True)
            (local / "file").write_text("fix")
            git("commit", "-am", "fix", cwd=local)
            candidate = git("rev-parse", "HEAD", cwd=local).stdout.strip()
            git("push", "origin", "HEAD:refs/heads/fix", cwd=local, env=env)
            self.assertEqual(json.loads((hooks / "attempt.json").read_text()), {"candidate": candidate})
            (local / "file").write_text("second")
            git("commit", "-am", "second", cwd=local)
            rejected = git("push", "origin", "HEAD:refs/heads/fix", cwd=local, env=env, check=False)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Stop this run", rejected.stderr)
            self.assertEqual(git("rev-parse", "refs/heads/fix", cwd=remote).stdout.strip(), candidate)


class WorkflowTests(unittest.TestCase):
    def test_workflow_embeds_trusted_guard_and_terminal_after_model_failure(self):
        text = (ROOT / ".github/workflows/claude.yml").read_text(encoding="utf-8")
        chunks = text.split("<<'CLAUDE_FIXER_GUARD'\n")[1:]
        self.assertEqual(len(chunks), 2)
        for chunk in chunks:
            embedded = textwrap.dedent(chunk.split("          CLAUDE_FIXER_GUARD", 1)[0]).rstrip()
            self.assertEqual(embedded, SOURCE.read_text(encoding="utf-8").rstrip())
        self.assertEqual(text.count("if: ${{ !cancelled() && steps.push_guard.outcome == 'success'"), 2)
        self.assertEqual(text.count("timeout-minutes: 20"), 2)
        self.assertEqual(text.count("--max-turns 30"), 2)
        for body in text.split("      - id: claude\n")[1:]:
            allow = body.split("--allowedTools", 1)[1].split("--disallowedTools", 1)[0]
            self.assertNotIn("Bash(gh api", allow)
            self.assertIn("Bash(python3 ${{ runner.temp }}/claude-fixer-guard/pre-push model-api --:*)", allow)
            self.assertIn("Bash(gh api:*)", body.split("--disallowedTools", 1)[1])
            for command in ("fetch", "pull", "rebase", "merge", "config"):
                self.assertNotIn(f"Bash(git {command}", allow)
            pushes = re.findall(r"Bash\(git push[^)]*\)", allow)
            self.assertEqual(pushes, ["Bash(git push origin HEAD:refs/heads/${{ steps.preflight.outputs.head_ref }})"])
            for bypass in ("Bash(git -", "Bash(git remote", "--no-verify", "--force", "--dry-run"):
                self.assertNotIn(bypass, "\n".join(re.findall(r"Bash\(git [^)]*\)", allow)))
            self.assertIn("If push is rejected or uncertain, STOP", body)
            self.assertIn("before emitting any addressed receipt", body)
            self.assertIn("has no Linear, Context7 or Parallel tools", body)


class TerminalReceiptIntegrationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("receipt_fixtures", ROOT / "tools/test_claude_fixer_receipts.py")
        self.fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fixtures)
        self.consumer = self.fixtures.m
        self.roots = self.fixtures.root_comments([self.fixtures.thread()])
        items = self.fixtures.findings([self.fixtures.thread()])
        if hasattr(self.fixtures, "prepare"):
            items = self.fixtures.prepare(items)
        self.item = {**items[0], "base": A}
        self.config = CONFIG | {"receipts": [self.item]}
        self.receipt = {**self.item, "status": "addressed", "commit": B}
        self.comments = self.roots + [self.reply(self.receipt)]

    def reply(self, receipt):
        return self.fixtures.reply(receipt) | {"id": 900}

    def read(self, path):
        if "/compare/" in path:
            return {"status": "identical" if path.endswith(f"{B}...{B}") else "ahead"}
        if "/comments?" in path:
            return self.comments
        if "/collaborators/" in path:
            return {"permission": "write"}
        return pr(B)

    def audit(self, read=None):
        return m.receipt_audit(self.config, read or self.read, self.consumer)

    def test_full_sha_producer_shape_reconciles_but_short_output_does_not(self):
        self.assertIn("1=verified-addressed", self.audit())
        self.comments = self.roots + [self.reply(self.receipt | {"commit": "d5194ed"})]
        self.assertIn("1=invalid-or-superseded", self.audit())
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "attempt.json"
            state.write_text(json.dumps({"candidate": B}))
            body = m.terminal(self.config, state, "", "failure", self.read, self.consumer)
        self.assertIn(f"live PR: `{B}`", body)
        self.assertIn("1=invalid-or-superseded", body)
        self.assertNotIn("claude-fixer-result:", body)

    def test_missing_invalid_forged_and_unpushed_receipts_are_not_verified(self):
        self.comments = self.roots
        self.assertIn("1=missing", self.audit())
        for change in ({"commit": A}, {"commit": "z" * 40}, {"base": C}, {"fingerprint": "0" * 64}):
            with self.subTest(change=change):
                self.comments = self.roots + [self.reply(self.receipt | change)]
                # A no-op compare must never be reported as ahead.
                def read(path):
                    if "/compare/" in path and path.endswith(f"{A}...{A}"):
                        return {"status": "identical"}
                    return self.read(path)
                self.assertNotIn("=verified-", self.audit(read))
        for user in (self.fixtures.actor("github-actions[bot]", self.consumer.ACTIONS_ID),
                     self.fixtures.actor("claude[bot]", 1)):
            self.comments = self.roots + [self.reply(self.receipt) | {"user": user}]
            self.assertIn("1=invalid-or-superseded", self.audit())
        self.comments = self.roots + [self.reply(self.receipt)]
        self.assertIn("1=invalid-or-superseded", self.audit(
            lambda p: {"status": "diverged"} if "/compare/" in p else self.read(p)))

    def test_handoff_is_reported_separately_and_root_edits_are_not_verified(self):
        self.comments = self.roots + [self.reply(self.receipt | {"status": "needs-human"})]
        self.assertIn("1=verified-needs-human", self.audit())
        self.comments[0] = self.comments[0] | {"body": "changed"}
        self.assertIn("1=root-changed-or-unverified", self.audit())

    def test_meaningful_followup_invalidates_captured_receipt(self):
        self.comments.append({"id": 901, "in_reply_to_id": 1,
                              "user": {"id": 12, "login": "writer", "type": "User"},
                              "created_at": "2026-10-16T02:00:00Z", "body": "This still fails."})
        self.assertNotIn("=verified-", self.audit())

    def test_read_denial_page_cap_duplicate_snapshot_and_head_race_stay_unverified(self):
        def denied(_):
            raise subprocess.CalledProcessError(1, "gh")
        self.assertIn("audit: unverified", self.audit(denied))
        reads = []
        def pages(path):
            reads.append(path)
            return [{}] * 100 if "/comments?" in path else self.read(path)
        self.assertIn("audit: unverified", self.audit(pages))
        self.assertEqual(sum("/comments?" in p for p in reads), 5)
        self.comments += self.comments
        self.assertIn("audit: unverified", self.audit())
        self.comments = self.roots + [self.reply(self.receipt)]
        heads = iter([B, C])
        def race(path):
            return pr(next(heads)) if path.endswith("/pulls/7") else self.read(path)
        self.assertIn("audit: unverified", self.audit(race))

    def test_invalid_compare_objects_differ_from_access_failure(self):
        for code, expected in ((404, "1=invalid-or-superseded"), (422, "1=invalid-or-superseded"),
                               (403, "audit: unverified"), (500, "audit: unverified")):
            def read(path, code=code):
                if "/compare/" in path:
                    raise subprocess.CalledProcessError(1, "gh", stderr=f"(HTTP {code})")
                return self.read(path)
            self.assertIn(expected, self.audit(read))

    def test_v3_generation_and_observation_remain_required(self):
        if "v" not in self.item:
            return
        for key in ("v", "generation", "observation"):
            self.comments = self.roots + [self.reply(self.receipt | {key: "wrong"})]
            self.assertIn("1=invalid-or-superseded", self.audit())

    def test_mixed_roots_and_complete_pagination_report_each_root(self):
        threads = [self.fixtures.thread(root=2)]
        items = self.fixtures.findings(threads)
        if hasattr(self.fixtures, "prepare"):
            items = self.fixtures.prepare(items)
        self.config["receipts"].append({**items[0], "base": A})
        self.comments += self.fixtures.root_comments(threads)
        def paged(path):
            if "/comments?" in path and path.endswith("page=1"):
                return [{"id": 1000 + i, "body": "unrelated"} for i in range(100)]
            return self.read(path)
        body = self.audit(paged)
        self.assertIn("1=verified-addressed", body)
        self.assertIn("2=missing", body)

    def test_terminal_read_count_and_wall_budget_are_enforced(self):
        with patch.object(m.time, "monotonic", return_value=0), patch.object(m, "api", return_value={}):
            read = m.bounded_reader()
            for _ in range(40):
                read("fixture")
            with self.assertRaisesRegex(m.AuditReadError, "budget"):
                read("fixture")
        with patch.object(m.time, "monotonic", side_effect=[0, 71]), patch.object(m, "api") as api:
            read = m.bounded_reader()
            with self.assertRaisesRegex(m.AuditReadError, "budget"):
                read("fixture")
            api.assert_not_called()

    def test_reader_decode_and_budget_errors_are_global_unverified(self):
        def exhausted(_):
            raise m.AuditReadError("budget")
        self.assertIn("audit: unverified", self.audit(exhausted))
        with patch.object(m, "api", side_effect=ValueError("bad JSON")), self.assertRaises(m.AuditReadError):
            m.bounded_reader()("fixture")

    def test_install_captures_context_and_workflow_uses_trusted_consumer(self):
        text = (ROOT / ".github/workflows/claude.yml").read_text(encoding="utf-8")
        self.assertIn('cat > "$RUNNER_TEMP/claude-fixer-guard/receipts.py"', text)
        self.assertIn('python3 "$RUNNER_TEMP/claude-fixer-guard/receipts.py"', text)
        self.assertEqual(text.count("RECEIPT_CONTEXT: ${{ steps.eligibility.outputs.receipt_context }}"), 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            script = path / "pre-push"
            script.write_text("fixture")
            with patch.object(m, "__file__", str(script)), patch.object(sys, "argv", ["pre-push", "install"]), \
                    patch.object(m, "command"), patch.dict(os.environ, {
                        "REPO": CONFIG["repo"], "PR_NUMBER": "7", "HEAD_SHA": A, "HEAD_REF": "fix",
                        "RECEIPT_CONTEXT": json.dumps([self.item])}):
                self.assertEqual(m.main(), 0)
            self.assertEqual(json.loads((path / "context.json").read_text())["receipts"], [self.item])

    def test_terminal_entry_point_posts_reconciliation_without_handled_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "receipts.py").write_text((ROOT / "scripts/ci/claude_fixer_receipts.py").read_text())
            (path / "context.json").write_text(json.dumps(self.config))
            (path / "attempt.json").write_text(json.dumps({"candidate": B}))
            with patch.object(m, "__file__", str(path / "pre-push")), \
                    patch.object(sys, "argv", ["pre-push", "terminal"]), \
                    patch.object(m, "api", side_effect=lambda p, timeout=30: self.read(p)), \
                    patch.object(m, "command", return_value="") as post, \
                    patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(path / "summary"), "MODEL_OUTCOME": "failure"}), \
                    patch("builtins.print"):
                self.assertEqual(m.main(), 0)
            body = json.loads(post.call_args.kwargs["input"])["body"]
            self.assertIn("1=verified-addressed", body)
            self.assertNotIn("claude-fixer-result:", body)
            self.assertEqual((path / "summary").read_text().strip(), body.strip())


if __name__ == "__main__":
    unittest.main()
