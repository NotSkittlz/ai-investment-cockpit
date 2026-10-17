"""Exact candidate identity and evidence checks; all network operations are read-only.

Coordinator owns request/dispatch authentication, holds, writer exclusion and retry
reservations. Neither labels nor this helper's process exit constitutes readiness.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime

CODEX_ID = 199175422  # Verified from genuine Cockpit #147 reviews on 2026-10-16.
CODEX_LOGIN = 'chatgpt-codex-connector[bot]'
SHA = re.compile(r'[0-9a-f]{40}')
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
NONCE = re.compile(r'[0-9a-f]{32}')
REQUIRED_JOBS = {
    'ci': frozenset({'filter', 'linux (3.11)', 'linux (3.12)', 'windows (3.11)',
                     'windows (3.12)', 'macos', 'linux-gates', 'queue-gates', 'candidate-ci-gates'}),
    'rust': frozenset({'filter', 'rust-gates', 'rust-book (linux / py3.11)',
                       'rust-book (linux / py3.12)', 'rust-book (windows / py3.11)',
                       'rust-book (windows / py3.12)', 'rust-book (macos / py3.12)'}),
}
# Candidate-side files the trusted default-branch YAML executes or imports after
# the candidate checkout: the local composite action with its installer and pin
# set, the aggregate helper, this validator and the orchestrating workflows. Each
# must be byte-identical to the policy commit's copy staged before the candidate
# checkout; a candidate that changes orchestration needs a separately approved
# policy landing first. Closed trees admit no entry beyond the listed files.
# Tested application and test code (app/, rust/, tools/test_*.py) is deliberately
# not covered: this proves control-file identity, not test semantics.
CONTROL_FILES = (
    '.github/actions/run-gates/action.yml',
    '.github/actions/run-gates/install.py',
    '.github/actions/run-gates/requirements.txt',
    '.github/workflows/ci.yml',
    '.github/workflows/rust.yml',
    'scripts/ci/merge_candidate.py',
    'tools/required_ci_gate.py',
)
CONTROL_TREES = ('.github/actions/run-gates',)
# The aggregates import tools.required_ci_gate, json and os with the candidate
# workspace first on sys.path. These paths would shadow those imports and must
# not exist in any form (file, directory or symlink).
CONTROL_ABSENT = (
    'tools.py', 'tools.pyc', 'tools/__init__.py', 'tools/__init__.pyc',
    'tools/__pycache__', 'tools/required_ci_gate', 'tools/required_ci_gate.pyc',
    'json.py', 'json.pyc', 'json', 'os.py', 'os.pyc', 'os',
    'sitecustomize.py', 'sitecustomize.pyc', 'sitecustomize',
    'usercustomize.py', 'usercustomize.pyc', 'usercustomize',
)
# Actions run metadata may qualify the workflow file with "@<ref or sha>", e.g.
# ".github/workflows/ci.yml@main" or "@refs/heads/<branch>". Only ref/SHA-safe
# characters are admitted after "@"; the file name itself must match exactly.
WORKFLOW_PATH_SUFFIX = r'(?:@[A-Za-z0-9._/-]+)?'


def workflow_path_matches(path, kind):
    """True when path names exactly this kind's workflow file, bare or @ref-qualified.

    The suffix is tolerated, never trusted: numeric workflow ID, repository ID,
    exact head/branch, actor, nonce, attempt and job completeness stay bound by
    the callers. A split on "@" would admit a different file; this does not.
    """
    if kind not in REQUIRED_JOBS or not isinstance(path, str):
        return False
    pattern = re.escape(f'.github/workflows/{kind}.yml') + WORKFLOW_PATH_SUFFIX
    return re.fullmatch(pattern, path) is not None


class CandidateError(ValueError):
    """Incomplete, stale or ambiguous evidence; no readiness assertion is allowed."""


def positive(value):
    return type(value) is int and value > 0


def sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def safe_ref(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}', value):
        return False
    return '..' not in value and all(part and not part.startswith(('.', '-'))
                                    and not part.endswith(('.', '.lock')) for part in value.split('/'))


@dataclass(frozen=True)
class Candidate:
    repository: str
    repository_id: int
    number: int
    pr_id: int
    head_ref: str
    base_ref: str
    head: str
    base: str
    merge: str
    tree: str

    def record(self):
        return asdict(self)

    @property
    def key(self):
        payload = json.dumps(self.record(), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(payload.encode()).hexdigest()

    def fields(self):
        return {'pr_number': str(self.number), 'pr_head_sha': self.head,
                'pr_base_sha': self.base, 'merge_sha': self.merge,
                'merge_tree_sha': self.tree, 'candidate_id': self.key}


@dataclass(frozen=True)
class Policy:
    """Trusted workflow/validator identity: exact default-branch commit and branch name.

    Candidate workflow runs execute this commit's YAML and validator, never the
    candidate head. The candidate tuple stays bound separately through the exact
    nonce-bearing run title and validated dispatch inputs.
    """
    sha: str
    branch: str

    def __post_init__(self):
        if not sha(self.sha) or not safe_ref(self.branch):
            raise CandidateError('Invalid trusted policy identity')


def policy_binding(policy, candidate):
    """Refuse policy/candidate pairs whose roles could be confused."""
    if not isinstance(policy, Policy) or not isinstance(candidate, Candidate):
        raise CandidateError('Missing policy or candidate identity')
    if (policy.sha in (candidate.head, candidate.merge) or candidate.head_ref == policy.branch
            or (candidate.base_ref == policy.branch and candidate.base != policy.sha)):
        raise CandidateError('Trusted policy commit does not bind to this candidate')


def repository_default_branch(get, repository, repository_id=None):
    """Live repository default branch; identity and shape are strict."""
    data = get(f'repos/{repository}')
    if (not isinstance(data, dict) or data.get('full_name') != repository or not positive(data.get('id'))
            or (repository_id is not None and data['id'] != repository_id)
            or not safe_ref(data.get('default_branch'))):
        raise CandidateError('Repository default branch unavailable')
    return data['default_branch']


def policy_witness(witness, default_branch):
    """Bind the runner's GitHub SHA, workflow SHA and recorded policy witness into one Policy.

    For a default-branch workflow_dispatch all three SHAs name the same commit:
    the trusted YAML, the trusted validator copy and the checked-out policy.
    Any disagreement, or a ref other than the live default branch, fails closed.
    """
    if not isinstance(witness, dict) or not safe_ref(default_branch):
        raise CandidateError('Malformed policy witness')
    values = [witness.get(key) for key in ('sha', 'workflow_sha', 'policy_sha')]
    if any(not sha(value) for value in values) or len(set(values)) != 1:
        raise CandidateError('Workflow, checkout and policy witness SHAs disagree')
    if (witness.get('ref') != f'refs/heads/{default_branch}' or witness.get('ref_name') != default_branch
            or witness.get('branch') != default_branch):
        raise CandidateError('Candidate workflow was not dispatched on the default branch')
    return Policy(values[0], default_branch)


def pr_identity(repository, number, pr):
    if not isinstance(pr, dict) or not REPO.fullmatch(repository):
        raise CandidateError('Malformed repository or PR')
    if (not positive(number) or not positive(pr.get('number')) or pr['number'] != number or not positive(pr.get('id'))
            or pr.get('state') != 'open' or pr.get('draft') is not False
            or pr.get('mergeable') is not True or not sha(pr.get('merge_commit_sha'))):
        raise CandidateError('PR is not an open, ready, mergeable candidate')
    sides = []
    for name in ('head', 'base'):
        side = pr.get(name)
        if not isinstance(side, dict) or not isinstance(side.get('repo'), dict):
            raise CandidateError('Missing candidate branch/repository identity')
        repo = side['repo']
        if (repo.get('full_name') != repository or not positive(repo.get('id'))
                or not sha(side.get('sha')) or not safe_ref(side.get('ref'))
                or (name == 'head' and side['ref'] in ('main', 'master', 'HEAD'))):
            raise CandidateError('Invalid same-repository candidate identity')
        sides.append((repo['id'], side['ref'], side['sha']))
    if sides[0][0] != sides[1][0]:
        raise CandidateError('Candidate repository IDs differ')
    return {'repository': repository, 'repository_id': sides[0][0], 'number': number,
            'pr_id': pr['id'], 'head_ref': sides[0][1], 'base_ref': sides[1][1],
            'head': sides[0][2], 'base': sides[1][2], 'merge': pr['merge_commit_sha']}


def verify_branch_refs(get, identity):
    """Triangulate PR metadata with the actual named head and base refs."""
    for side in ('base', 'head'):
        name = identity[f'{side}_ref']
        value = get(f'repos/{identity["repository"]}/git/ref/heads/{name}')
        if (not isinstance(value, dict) or value.get('ref') != f'refs/heads/{name}'
                or not isinstance(value.get('object'), dict)
                or value['object'].get('type') != 'commit'
                or value['object'].get('sha') != identity[side]):
            raise CandidateError(f'Live {side} ref does not match candidate PR metadata')


def resolve_candidate(get, repository, number):
    """Bind PR, live branch refs and merge tree; reread refs and PR before return.

    These reads are not atomic. Later movement invalidates readiness at the
    coordinator's next reconciliation; no cached PR merge data alone qualifies.
    """
    if not isinstance(repository, str) or not REPO.fullmatch(repository) or not positive(number):
        raise CandidateError('Invalid repository/PR lookup')
    path = f'repos/{repository}/pulls/{number}'
    identity = pr_identity(repository, number, get(path))
    verify_branch_refs(get, identity)
    commit = get(f'repos/{repository}/git/commits/{identity["merge"]}')
    if (not isinstance(commit, dict) or commit.get('sha') != identity['merge']
            or not isinstance(commit.get('parents'), list)
            or not isinstance(commit.get('tree'), dict) or not sha(commit['tree'].get('sha'))):
        raise CandidateError('Malformed merge commit evidence')
    parents = commit['parents']
    if (len(parents) != 2 or any(not isinstance(p, dict) for p in parents)
            or [p.get('sha') for p in parents] != [identity['base'], identity['head']]):
        raise CandidateError('Merge parents must be exactly current base then head')
    verify_branch_refs(get, identity)
    if pr_identity(repository, number, get(path)) != identity:
        raise CandidateError('Candidate changed during resolution')
    return Candidate(**identity, tree=commit['tree']['sha'])


def timestamp(value):
    if not isinstance(value, str):
        raise CandidateError('Missing evidence timestamp')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise CandidateError('Invalid evidence timestamp') from error
    if stamp.tzinfo is None:
        raise CandidateError('Ambiguous evidence timezone')
    return stamp


def _latest_head_review(reviews, head):
    """Select latest genuine review first, without falling back to an older verdict."""
    if not sha(head):
        raise CandidateError('Invalid review head')
    if not isinstance(reviews, list):
        raise CandidateError('Incomplete review listing')
    trusted = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise CandidateError('Malformed review')
        user = review.get('user')
        if not isinstance(user, dict) or (user.get('id'), user.get('login'), user.get('type')) != (
                CODEX_ID, CODEX_LOGIN, 'Bot'):
            continue
        if review.get('commit_id') != head:
            continue
        identifier = review.get('id')
        if not positive(identifier):
            raise CandidateError('Invalid Codex review ID')
        timestamp(review.get('submitted_at'))
        if identifier in trusted and trusted[identifier] != review:
            raise CandidateError('Conflicting Codex review records')
        trusted[identifier] = review
    if not trusted:
        return None
    # Select latest first, never search backwards for a successful older verdict.
    latest = max(trusted.values(), key=lambda r: (timestamp(r['submitted_at']), r['id']))
    if sum(timestamp(r['submitted_at']) == timestamp(latest['submitted_at'])
           for r in trusted.values()) != 1:
        return None  # Same-second independent submissions have no proven causal order.
    return latest


def _terminal_verdict(body):
    """Return plain marker lines and an exact, unquoted terminal verdict.

    The verdict must be the unique plain CODEX-GATE line, and the final nonblank
    line of the body must itself be that admitted plain line. A final copy that
    is indented code, quoted, a fence delimiter, or inside an open or closed
    fence is not a verdict, and an earlier plain line cannot lend it that status.
    """
    if not isinstance(body, str):
        return [], ''
    machine, fence, terminal = [], None, None
    for raw in body.splitlines():
        if not raw.strip():
            continue  # Trailing blank lines are legitimate; they carry no verdict.
        terminal = None  # Reset per nonblank line; only an admitted plain line sets it.
        # At most three spaces may precede a Markdown fence or plain marker.
        # A tab/four spaces denotes code, never an independent review verdict.
        indent = raw[:len(raw) - len(raw.lstrip())]
        if len(indent) > 3 or any(char != ' ' for char in indent):
            continue
        line = raw.lstrip(' ')
        delimiter = re.match(r'(`{3,}|~{3,})(.*)$', line)
        if fence is not None:
            char, length = fence
            if re.fullmatch(re.escape(char) + '{' + str(length) + r',}[ \t]*', line):
                fence = None
            continue
        if delimiter and (delimiter[1][0] != '`' or '`' not in delimiter[2]):
            fence = (delimiter[1][0], len(delimiter[1]))
        elif not line.startswith('>'):
            terminal = line.strip()
            machine.append(terminal)
    verdicts = [line for line in machine if line.startswith('CODEX-GATE:')]
    if (len(verdicts) != 1 or verdicts[0] not in ('CODEX-GATE: READY', 'CODEX-GATE: MEDIATE')
            or terminal != verdicts[0]):
        return machine, ''
    return machine, verdicts[0].split(': ', 1)[1]


def _candidate_review(reviews, candidate, request):
    """Return latest genuine candidate verdict; request provenance is caller-owned.

    Request is an authenticated coordinator record with id, candidate, nonce and
    created_at. A new nonce is required after invalidation/retry. Same-second
    request/review timestamps are ambiguous and cannot establish causality.
    """
    if (not isinstance(request, dict) or not positive(request.get('id'))
            or request.get('candidate') != candidate.key
            or not isinstance(request.get('nonce'), str) or not NONCE.fullmatch(request['nonce'])):
        raise CandidateError('Invalid candidate review request')
    requested_at = timestamp(request.get('created_at'))
    latest = _latest_head_review(reviews, candidate.head)
    if (latest is None or latest.get('state') not in ('COMMENTED', 'APPROVED')
            or timestamp(latest['submitted_at']) <= requested_at):
        return None
    machine, verdict = _terminal_verdict(latest.get('body'))
    expected = [f'CODEX-CANDIDATE: {candidate.key}', f'CODEX-REQUEST: {request["nonce"]}']
    if not verdict or any([line for line in machine if line.startswith(prefix)] != [wanted]
           for prefix, wanted in zip(('CODEX-CANDIDATE:', 'CODEX-REQUEST:'), expected, strict=True)):
        return None
    return latest


def head_mediate_review(reviews, head):
    """Validate a current-head human handoff, never READY or CI authorization.

    MEDIATE does not require a mergeable synthetic candidate or a request nonce.
    The caller verifies live PR identity, owner holds, pause generation, review
    edit provenance and side-effect reservations before acting.
    """
    latest = _latest_head_review(reviews, head)
    if (latest is None or latest.get('state') not in ('COMMENTED', 'APPROVED', 'CHANGES_REQUESTED')
            or _terminal_verdict(latest.get('body'))[1] != 'MEDIATE'):
        return None
    return latest


def candidate_review(reviews, candidate, request):
    """Return the exact validated READY/MEDIATE review for caller freshness checks."""
    return _candidate_review(reviews, candidate, request)


def candidate_verdict(reviews, candidate, request):
    """Authenticated candidate verdict string READY/MEDIATE, otherwise empty."""
    review = _candidate_review(reviews, candidate, request)
    return _terminal_verdict(review['body'])[1] if review else ''


def ready_review(reviews, candidate, request):
    """Return the exact current READY review; MEDIATE cannot qualify CI."""
    review = _candidate_review(reviews, candidate, request)
    return review if review and _terminal_verdict(review['body'])[1] == 'READY' else None


def full_run_success(run, jobs, candidate, kind, workflow_id, dispatch_nonce, policy):
    """Validate one coordinator-owned latest run/attempt with complete job pages.

    Caller must authenticate dispatch ownership and select/revalidate the latest
    owned run/attempt. A run name is correlation data, not dispatch authority.
    Run head_sha/head_branch name the trusted policy commit and default branch;
    a run on the candidate head executed candidate YAML and never qualifies.
    """
    if (kind not in REQUIRED_JOBS or not positive(workflow_id)
            or not isinstance(dispatch_nonce, str) or not NONCE.fullmatch(dispatch_nonce)):
        raise CandidateError('Unknown candidate workflow')
    policy_binding(policy, candidate)
    if not isinstance(run, dict) or not isinstance(jobs, list):
        raise CandidateError('Incomplete run/job evidence')
    repo = run.get('repository')
    head_repo = run.get('head_repository')
    if (not isinstance(head_repo, dict) or head_repo.get('id') != candidate.repository_id
            or head_repo.get('full_name') != candidate.repository
            or not isinstance(repo, dict) or repo.get('id') != candidate.repository_id
            or repo.get('full_name') != candidate.repository
            or run.get('event') != 'workflow_dispatch' or run.get('workflow_id') != workflow_id
            or not workflow_path_matches(run.get('path'), kind)
            or run.get('display_title') != f'candidate:{candidate.key}:{kind}:{dispatch_nonce}'
            or run.get('head_sha') != policy.sha or run.get('head_branch') != policy.branch
            or not positive(run.get('id')) or not positive(run.get('run_attempt'))
            or run.get('status') != 'completed' or run.get('conclusion') != 'success'):
        return False
    found = {}
    for job in jobs:
        if (not isinstance(job, dict) or not positive(job.get('id'))
                or not positive(job.get('run_id')) or not positive(job.get('run_attempt'))
                or job.get('run_id') != run['id'] or job.get('run_attempt') != run['run_attempt']
                or job.get('head_sha') != policy.sha):
            raise CandidateError('Job does not bind to this run/attempt')
        name = job.get('name')
        if not isinstance(name, str) or name in found:
            raise CandidateError('Ambiguous job identity')
        found[name] = job
    return all(name in found and found[name].get('status') == 'completed'
               and found[name].get('conclusion') == 'success' for name in REQUIRED_JOBS[kind])


def validate_checkout(candidate, fields, checkout):
    """Pure validation of supplied inputs and checked-out git object identity.

    The workflow SHA is deliberately not compared here: it names the trusted
    policy commit (see policy_witness), never the candidate head.
    """
    if fields != candidate.fields():
        raise CandidateError('Dispatch tuple does not match the live candidate')
    if checkout != {'sha': candidate.merge, 'parents': [candidate.base, candidate.head],
                    'tree': candidate.tree}:
        raise CandidateError('Checkout is not the exact candidate merge tree')


def inside(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False  # Different drives: certainly outside the workspace.


def _components(path):
    return path.split('/')


def _workspace_path(workspace, path, directory=False):
    """Resolve a control path without crossing any symlink below the workspace."""
    current = os.path.abspath(workspace)
    for part in _components(path):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise CandidateError(f'Candidate control path is a symlink: {path}')
    present = os.path.isdir(current) if directory else os.path.isfile(current)
    if not present:
        raise CandidateError(f'Candidate control path is missing: {path}')
    return current


def _read(path):
    try:
        with open(path, 'rb') as handle:
            return handle.read()
    except OSError as error:
        raise CandidateError(f'Unreadable control file: {path}') from error


def stage_policy(stage, policy_sha, workspace, git):
    """Copy the policy commit's control files into the isolated stage, bound to its blob IDs.

    Runs in the trusted policy checkout before any candidate file exists. The
    working-tree bytes are staged (the candidate checkout uses the same checkout
    machinery, so a later byte comparison is exact), and every tree entry is
    required to be a regular blob: symlinks, gitlinks and missing files refuse.
    """
    if not sha(policy_sha) or git('rev-parse', 'HEAD') != policy_sha:
        raise CandidateError('Policy checkout does not name the trusted policy commit')
    if not stage or inside(os.path.abspath(stage), os.path.abspath(workspace)):
        raise CandidateError('Policy stage must lie outside the workspace')
    entries = {}
    for line in git('ls-tree', '-r', policy_sha, '--', *CONTROL_TREES, *CONTROL_FILES).splitlines():
        meta, _, path = line.partition('\t')
        parts = meta.split()
        if len(parts) != 3 or not path:
            raise CandidateError('Unreadable policy tree listing')
        entries[path] = tuple(parts)
    for path, (mode, kind, oid) in entries.items():
        if path not in CONTROL_FILES:
            raise CandidateError(f'Policy control tree has an unlisted entry: {path}')
        if kind != 'blob' or mode not in ('100644', '100755') or not sha(oid):
            raise CandidateError(f'Policy control file is not a regular blob: {path}')
    if set(entries) != set(CONTROL_FILES):
        raise CandidateError('Policy commit lacks a control file; nothing is staged')
    manifest = {'policy_sha': policy_sha, 'files': {}}
    for path in CONTROL_FILES:
        data = _read(_workspace_path(workspace, path))
        target = os.path.join(stage, 'controls', *_components(path))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as handle:
            handle.write(data)
        manifest['files'][path] = {'oid': entries[path][2], 'sha256': hashlib.sha256(data).hexdigest()}
    with open(os.path.join(stage, 'manifest.json'), 'w', encoding='utf-8') as handle:
        json.dump(manifest, handle, sort_keys=True)


def verify_controls(workspace, stage, policy_sha):
    """Every control file the trusted YAML will execute equals the staged policy copy.

    Compares candidate working-tree bytes against the isolated stage; no Git
    command, import path or helper output from the candidate tree is consulted.
    """
    if not workspace or not stage or inside(os.path.abspath(stage), os.path.abspath(workspace)):
        raise CandidateError('Policy stage is not isolated from the candidate workspace')
    try:
        with open(os.path.join(stage, 'manifest.json'), encoding='utf-8') as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as error:
        raise CandidateError('Policy control manifest unavailable') from error
    files = manifest.get('files') if isinstance(manifest, dict) else None
    if (not sha(policy_sha) or manifest.get('policy_sha') != policy_sha
            or not isinstance(files, dict) or set(files) != set(CONTROL_FILES)):
        raise CandidateError('Policy control manifest does not name this policy commit')
    for path in CONTROL_FILES:
        staged = _read(os.path.join(stage, 'controls', *_components(path)))
        record = files[path]
        if not isinstance(record, dict) or hashlib.sha256(staged).hexdigest() != record.get('sha256'):
            raise CandidateError(f'Staged policy copy does not match its manifest: {path}')
        if _read(_workspace_path(workspace, path)) != staged:
            raise CandidateError(f'Candidate modifies orchestration control file {path}; '
                                 'land it through a separately approved policy change first')
    for tree in CONTROL_TREES:
        root = _workspace_path(workspace, tree, directory=True)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                relative = os.path.relpath(full, os.path.abspath(workspace)).replace(os.sep, '/')
                if os.path.islink(full) or name in dirnames or relative not in CONTROL_FILES:
                    raise CandidateError(f'Candidate adds an unlisted entry to control tree {tree}: {relative}')
    for path in CONTROL_ABSENT:
        if os.path.lexists(os.path.join(os.path.abspath(workspace), *_components(path))):
            raise CandidateError(f'Candidate adds a module path that would shadow aggregate imports: {path}')


def trusted_location(script, expected, workspace):
    """The running validator must be the staged policy copy outside the candidate workspace.

    The candidate checkout owns everything under the workspace, including any
    symlink or relative path that could shadow scripts/ci/merge_candidate.py.
    """
    if not expected or not workspace:
        raise CandidateError('Trusted validator location is not recorded')
    try:
        if os.path.islink(expected):
            raise CandidateError('Staged validator path is a symlink')
        # Both the recorded path as written and its resolved target must lie
        # outside the workspace: a workspace directory symlink is not trusted.
        if inside(os.path.abspath(expected), os.path.abspath(workspace)):
            raise CandidateError('Validator must not run from the candidate workspace')
        script, expected, workspace = (os.path.realpath(path) for path in (script, expected, workspace))
    except (TypeError, ValueError) as error:
        raise CandidateError('Unreadable validator location') from error
    if script != expected or not os.path.isfile(script):
        raise CandidateError('Validator is not the staged trusted policy copy')
    if inside(script, workspace):
        raise CandidateError('Validator must not run from the candidate workspace')


def api(path):
    result = subprocess.run(['gh', 'api', path], check=True, capture_output=True,
                            text=True, timeout=30)
    return json.loads(result.stdout)


def git(*args):
    return subprocess.check_output(['git', *args], text=True, timeout=10).strip()


def main():
    env = os.environ
    command = sys.argv[1:]
    if len(command) == 2 and command[0] == 'stage-policy':
        # Trusted policy checkout is cwd; the staged validator copy must be the
        # one running, and the stage must lie outside that workspace.
        stage = command[1]
        workspace = os.getcwd()
        trusted_location(__file__, os.path.join(stage, 'merge_candidate.py'), workspace)
        if env.get('GITHUB_SHA') != env.get('GITHUB_WORKFLOW_SHA'):
            raise CandidateError('Workflow and checkout SHAs disagree')
        stage_policy(stage, env.get('GITHUB_SHA', ''), workspace, git)
        print(f'Staged {len(CONTROL_FILES)} policy control files from {env["GITHUB_SHA"]}')
        return
    if command != ['validate-dispatch']:
        raise CandidateError('Expected validate-dispatch or stage-policy command')
    if env.get('CANDIDATE_SCOPE') != 'full' or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        raise CandidateError('Candidate validation requires a full dispatch')
    validator, workspace = env.get('CODEX_POLICY_VALIDATOR', ''), env.get('GITHUB_WORKSPACE', '')
    trusted_location(__file__, validator, workspace)
    witness = {'sha': env.get('GITHUB_SHA'), 'workflow_sha': env.get('GITHUB_WORKFLOW_SHA'),
               'policy_sha': env.get('CODEX_POLICY_SHA'), 'ref': env.get('GITHUB_REF'),
               'ref_name': env.get('GITHUB_REF_NAME'), 'branch': env.get('CODEX_POLICY_BRANCH')}
    # Before any candidate-side control file can execute: the local action,
    # installer, pins, aggregate helper and workflows must equal the policy copy.
    verify_controls(workspace, os.path.dirname(os.path.abspath(validator)), witness['policy_sha'])
    # cwd is the candidate workspace: the exact merge commit, read with live Git.
    checkout = {'sha': git('rev-parse', 'HEAD'), 'parents': git('show', '-s', '--format=%P', 'HEAD').split(),
                'tree': git('rev-parse', 'HEAD^{tree}')}
    if not NONCE.fullmatch(env.get('DISPATCH_NONCE', '')):
        raise CandidateError('Missing or invalid dispatch nonce')
    number = env.get('PR_NUMBER', '')
    if not re.fullmatch(r'[1-9][0-9]*', number):
        raise CandidateError('Invalid dispatched PR number')
    repository = env.get('GITHUB_REPOSITORY', '')
    candidate = resolve_candidate(api, repository, int(number))
    default_branch = repository_default_branch(api, repository, candidate.repository_id)
    policy = policy_witness(witness, default_branch)
    policy_binding(policy, candidate)
    fields = {key: env.get(key.upper(), '') for key in candidate.fields()}
    validate_checkout(candidate, fields, checkout)
    print(f'Validated merge candidate {candidate.key} under policy {policy.sha}')


if __name__ == '__main__':
    main()
