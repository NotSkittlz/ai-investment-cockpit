"""Execute candidate workflow decisions without dispatching hosted jobs."""
import importlib.util
import itertools
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.test_ci_dispatch_scope import concurrency_group, evaluate, workflow  # noqa: E402 - direct-script entry
import importlib  # noqa: E402

m = importlib.import_module('scripts.ci.merge_candidate')

FIELDS = {'pr_number': '166', 'pr_head_sha': 'a' * 40, 'pr_base_sha': 'b' * 40,
          'merge_sha': 'c' * 40, 'merge_tree_sha': 'd' * 40,
          'candidate_id': 'e' * 64, 'dispatch_nonce': 'f' * 32}
CONTEXT = {'github.event_name': 'workflow_dispatch', 'inputs.scope': 'full',
           'github.event.pull_request.base.sha': 'b' * 40,
           **{f'inputs.{key}': value for key, value in FIELDS.items()}}
VALIDATION = 'Validate exact full candidate'
STAGING = 'Stage trusted candidate validator'
POLICY_SHA = 'b' * 40
CANDIDATE_JOBS = {'ci.yml': ('filter', 'linux', 'windows', 'macos'), 'rust.yml': ('filter', 'rust-book')}
# Resolve bash once: the executable tests below restrict PATH to an interpreter
# directory, and POSIX subprocess resolves the program from the passed PATH.
BASH = shutil.which('bash') or 'bash'


def python_only_path(root):
    """Return a PATH entry exposing `python` but no `python3`, as Windows cells do.

    A real venv launcher, not a shell shim, keeps this executable on native
    Windows, WSL and Linux without trusting any ambient interpreter name.
    """
    target = root / 'interpreter'
    venv.EnvBuilder(with_pip=False, symlinks=False).create(target)
    bindir = next(path for path in (target / 'Scripts', target / 'bin') if path.is_dir())
    for entry in list(bindir.iterdir()):
        if entry.name.lower().startswith('python3'):
            entry.unlink()
    names = {entry.name.lower() for entry in bindir.iterdir()}
    if not names & {'python', 'python.exe'} or any(name.startswith('python3') for name in names):
        raise AssertionError(f'interpreter fixture must expose only python: {sorted(names)}')
    return bindir


def step_env(step, context=None):
    result = {}
    for key, value in step.get('env', {}).items():
        if value.startswith('${{'):
            value = evaluate(value, **(context or CONTEXT), **{
                'github.token': 'fixture-read-only-token'})
            value = str(value).lower() if isinstance(value, bool) else str(value)
        result[key] = value
    return result


class CandidateWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.ci, self.rust = workflow('ci.yml'), workflow('rust.yml')

    def test_candidate_groups_and_delivery_names_do_not_collide(self):
        for name, doc in [('ci', self.ci), ('rust', self.rust)]:
            self.assertEqual(evaluate(doc['run-name'], **CONTEXT),
                             f"candidate:{FIELDS['candidate_id']}:{name}:{FIELDS['dispatch_nonce']}")
            self.assertFalse(evaluate(doc['concurrency']['cancel-in-progress'], **CONTEXT))
            self.assertEqual(evaluate(doc['concurrency']['queue'], **CONTEXT), 'max')
            candidate = concurrency_group(doc, **CONTEXT)
            other = {**CONTEXT, 'inputs.candidate_id': '1' * 64}
            self.assertNotEqual(candidate, concurrency_group(doc, **other))
            retry = {**CONTEXT, 'inputs.dispatch_nonce': '2' * 32}
            self.assertEqual(candidate, concurrency_group(doc, **retry))
            self.assertNotEqual(evaluate(doc['run-name'], **CONTEXT),
                                evaluate(doc['run-name'], **retry))
            label = {**CONTEXT, **{f'inputs.{k}': '' for k in
                                  ('candidate_id', 'pr_base_sha', 'merge_tree_sha', 'dispatch_nonce')}}
            self.assertNotEqual(candidate, concurrency_group(doc, **label))
            self.assertTrue(evaluate(doc['concurrency']['cancel-in-progress'], **label))
            self.assertEqual(evaluate(doc['concurrency']['queue'], **label), 'single')
            self.assertEqual(evaluate(doc['concurrency']['queue'], **{
                'github.event_name': 'workflow_dispatch'}), 'single')
            self.assertEqual(evaluate(doc['concurrency']['queue']), 'max')
            for event in ('pull_request', 'push', 'schedule', 'merge_group'):
                self.assertNotEqual(candidate, concurrency_group(doc, **{'github.event_name': event}))

    def test_partial_candidate_fields_enter_validation_and_cannot_cancel_labels(self):
        new_fields = ('candidate_id', 'pr_base_sha', 'merge_tree_sha', 'dispatch_nonce')
        for doc in (self.ci, self.rust):
            step = next(s for s in doc['jobs']['filter']['steps']
                        if s.get('name') == 'Validate exact full candidate')
            legacy = {'github.event_name': 'workflow_dispatch', 'inputs.pr_number': '166'}
            self.assertFalse(evaluate(step['if'], **legacy))
            for key in new_fields:
                context = {**legacy, f'inputs.{key}': FIELDS[key]}
                with self.subTest(field=key):
                    self.assertTrue(evaluate(step['if'], **context))
                    self.assertFalse(evaluate(doc['concurrency']['cancel-in-progress'], **context))
                    self.assertEqual(evaluate(doc['concurrency']['queue'], **context), 'max')
                    self.assertNotEqual(concurrency_group(doc, **context),
                                        concurrency_group(doc, **legacy))

    def test_delayed_duplicate_delivery_keeps_current_pending_candidate(self):
        for doc in (self.ci, self.rust):
            policy = doc['concurrency']
            # All three deliveries share the candidate digest. The middle
            # approved delivery must survive a delayed duplicate's admission.
            running, pending, canceled = None, [], []
            for delivery in ('active', 'approved-pending', 'delayed-duplicate'):
                if running is None:
                    running = delivery
                elif evaluate(policy['cancel-in-progress'], **CONTEXT):
                    canceled.append(running)
                    running = delivery
                else:
                    if evaluate(policy['queue'], **CONTEXT) == 'single':
                        canceled.extend(pending)
                        pending.clear()
                    pending.append(delivery)
            self.assertEqual(canceled, [])
            self.assertEqual(running, 'active')
            self.assertEqual(pending, ['approved-pending', 'delayed-duplicate'])

    def test_ordinary_candidate_manual_and_label_deliveries_cannot_cancel_each_other(self):
        candidate_fields = ('candidate_id', 'pr_base_sha', 'merge_tree_sha', 'dispatch_nonce')
        common = {'github.event.pull_request.number': 166,
                  'github.ref': 'refs/heads/feature', 'inputs.scope': 'full'}
        ordinary = {**common, 'github.event_name': 'pull_request'}
        manual = {**common, 'github.event_name': 'workflow_dispatch'}
        label = {**manual, 'inputs.pr_number': '166', 'inputs.pr_head_sha': FIELDS['pr_head_sha'],
                 'inputs.merge_sha': FIELDS['merge_sha']}
        for doc in (self.ci, self.rust):
            # Every nonempty subset of candidate-specific fields must isolate
            # even a malformed delivery before its validation step can execute.
            for present in itertools.product((False, True), repeat=len(candidate_fields)):
                if not any(present):
                    continue
                for legacy in (manual, label):
                    candidate = {**legacy, **{f'inputs.{field}': FIELDS[field]
                                             for field, supplied in zip(candidate_fields, present)
                                             if supplied}}
                    policy = doc['concurrency']
                    self.assertFalse(evaluate(policy['cancel-in-progress'], **candidate))
                    self.assertEqual(evaluate(policy['queue'], **candidate), 'max')
                    deliveries = [(name, concurrency_group(doc, **context),
                                   evaluate(policy['cancel-in-progress'], **context))
                                  for name, context in [('ordinary', ordinary), ('candidate', candidate),
                                                        ('manual', manual), ('label', label)]]
                    for arrivals in itertools.permutations(deliveries):
                        running, pending, canceled = {}, {}, []
                        for name, group, cancel in arrivals:
                            if group not in running:
                                running[group] = name
                            elif cancel:
                                canceled.append(running[group])
                                running[group] = name
                            else:
                                pending.setdefault(group, []).append(name)
                        self.assertEqual(canceled, [], arrivals)
                        self.assertEqual(set(running.values()), {'ordinary', 'candidate', 'manual', 'label'})
                        self.assertEqual(pending, {})

    def test_existing_required_check_names_remain_exact(self):
        self.assertEqual(self.ci['jobs']['linux-gates']['name'], 'linux-gates')
        self.assertEqual(self.ci['jobs']['queue-gates']['name'], 'queue-gates')
        self.assertEqual(self.rust['jobs']['rust-gates']['name'], 'rust-gates')
        self.assertEqual(self.ci['jobs']['candidate-ci-gates']['name'], 'candidate-ci-gates')

    def validation_steps(self):
        for name, doc in (('ci.yml', self.ci), ('rust.yml', self.rust)):
            for job in CANDIDATE_JOBS[name]:
                steps = doc['jobs'][job]['steps']
                index = next(i for i, step in enumerate(steps) if step.get('name') == VALIDATION)
                yield name, doc, job, steps, index

    def test_live_validation_runs_after_each_exact_checkout_before_test_installation(self):
        for name, doc, job, steps, index in self.validation_steps():
            with self.subTest(workflow=name, job=job):
                # Trusted policy checkout, trusted interpreter install, policy
                # staging and the exact-merge checkout precede validation; no
                # candidate dependency install, local action or test does.
                self.assertEqual(index, 4)
                policy, setup, stage, checkout, validation = steps[:5]
                self.assertEqual(policy['uses'], 'actions/checkout@v4')
                self.assertEqual(policy['with']['ref'], '${{ github.sha }}')
                self.assertEqual(policy['with']['fetch-depth'], '1')
                self.assertEqual(policy['with']['persist-credentials'], 'false')
                self.assertEqual(policy['if'], validation['if'])
                self.assertEqual(setup['uses'], 'actions/setup-python@v5')
                # The install runs whenever validation runs: unconditional, or the same gate.
                self.assertEqual(setup.get('if', validation['if']), validation['if'])
                self.assertEqual(stage['name'], STAGING)
                self.assertEqual(stage['if'], validation['if'])
                self.assertEqual(stage['shell'], 'bash')
                self.assertIn('stage-policy', stage['run'])
                self.assertEqual(checkout['uses'], 'actions/checkout@v4')
                self.assertNotIn('if', checkout)
                self.assertEqual(evaluate(checkout['with']['ref'], **CONTEXT), FIELDS['merge_sha'])
                self.assertEqual(checkout['with']['fetch-depth'], '2')
                self.assertTrue(evaluate(validation['if'], **CONTEXT))
                self.assertFalse(evaluate(validation['if']))
                # No pip cache: no cache key is derived from candidate files before validation.
                self.assertEqual(set(setup['with']), {'python-version'})
                cell = '${{ matrix.python-version }}' if 'strategy' in doc['jobs'][job] else '3.12'
                self.assertEqual(setup['with']['python-version'], cell)
                self.assertEqual(sum(step.get('uses') == 'actions/setup-python@v5' for step in steps), 1)
                # Nothing before validation runs a local action or candidate script.
                for step in steps[:index]:
                    self.assertFalse(step.get('uses', '').startswith('./'), step)
                    self.assertNotIn('pip install', step.get('run', ''), step)
                self.assertEqual(validation['shell'], 'bash')
                self.assertIn('manifest.json', validation['run'])
                self.assertEqual(step_env(validation)['DISPATCH_NONCE'], FIELDS['dispatch_nonce'])
                self.assertEqual(step_env(validation)['PR_BASE_SHA'], FIELDS['pr_base_sha'])
                self.assertEqual(step_env(validation)['MERGE_TREE_SHA'], FIELDS['merge_tree_sha'])
                if name == 'ci.yml' and job != 'filter':
                    # The pre-validation interpreter is the same cell run-gates installs.
                    self.assertEqual(steps[5]['uses'], './.github/actions/run-gates')
                    self.assertEqual(steps[5]['with']['python-version'], cell)

    def test_actual_validation_step_forwards_tuple_and_stops_on_refusal_or_wrong_scope(self):
        # A subprocess fixture isolates workflow wiring from the helper's separate
        # real-Git/API tests. A refusal must propagate before any following work.
        # PATH exposes only `python`, as native Windows cells do, so a step that
        # still needed a `python3` launcher fails here on every host platform.
        stub = ("import json, os, sys\n"
                "assert sys.argv[1:] == ['validate-dispatch']\n"
                "print(json.dumps({k: os.environ[k] for k in "
                "['PR_NUMBER','PR_HEAD_SHA','PR_BASE_SHA','MERGE_SHA',"
                "'MERGE_TREE_SHA','CANDIDATE_ID','DISPATCH_NONCE']}))\n"
                "raise SystemExit(int(os.environ['REFUSAL']))\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            # The candidate workspace copy must never be the one executed.
            candidate_copy = workspace / 'scripts/ci/merge_candidate.py'
            candidate_copy.parent.mkdir(parents=True)
            candidate_copy.write_text("raise SystemExit('candidate validator copy executed')\n")
            stage = root / 'runner-temp' / 'codex-policy'
            stage.mkdir(parents=True)
            staged = stage / 'merge_candidate.py'
            staged.write_text(stub)
            (stage / 'manifest.json').write_text('{}')
            no_manifest = root / 'runner-temp' / 'no-manifest'
            no_manifest.mkdir()
            (no_manifest / 'merge_candidate.py').write_text(stub)
            try:
                link = root / 'runner-temp' / 'link.py'
                os.symlink(staged, link)
            except (OSError, NotImplementedError):
                link = None
            bindir = python_only_path(root)
            policy_env = {'GITHUB_SHA': POLICY_SHA, 'GITHUB_WORKFLOW_SHA': POLICY_SHA,
                          'CODEX_POLICY_SHA': POLICY_SHA, 'CODEX_POLICY_BRANCH': 'main',
                          # Match the slash-delimited path emitted by the actual
                          # Bash staging step, including on Windows.
                          'CODEX_POLICY_VALIDATOR': staged.as_posix(), 'GITHUB_WORKSPACE': str(workspace)}
            for name, _doc, job, steps, index in self.validation_steps():
                step = steps[index]
                for scope, refusal in [('full', 0), ('linux', 0), ('', 0), ('full', 1)]:
                    with self.subTest(workflow=name, job=job, scope=scope, refusal=refusal):
                        result = subprocess.run([BASH, '-c', step['run']], cwd=workspace,
                                                env={**os.environ, **step_env(step), **policy_env,
                                                     'PATH': str(bindir),
                                                     'CANDIDATE_SCOPE': scope, 'REFUSAL': str(refusal)},
                                                capture_output=True, text=True, check=False)
                        self.assertEqual(result.returncode == 0, scope == 'full' and refusal == 0,
                                         result.stderr)
                        if scope == 'full':
                            self.assertEqual(json.loads(result.stdout),
                                             {key.upper(): value for key, value in FIELDS.items()})
                        else:
                            self.assertEqual(result.stdout, '')
                # Without a staged policy, with a disagreeing witness, a missing
                # manifest or a symlinked validator the step stops before any
                # validator (staged or candidate) can print.
                broken = [{'CODEX_POLICY_VALIDATOR': ''}, {'CODEX_POLICY_SHA': ''},
                          {'CODEX_POLICY_VALIDATOR': str(root / 'runner-temp' / 'absent.py')},
                          {'CODEX_POLICY_VALIDATOR': str(no_manifest / 'merge_candidate.py')},
                          {'GITHUB_SHA': 'a' * 40}, {'GITHUB_WORKFLOW_SHA': 'a' * 40},
                          {'CODEX_POLICY_SHA': 'a' * 40}]
                if link is not None:
                    broken.append({'CODEX_POLICY_VALIDATOR': str(link)})
                for change in broken:
                    with self.subTest(workflow=name, job=job, change=change):
                        result = subprocess.run([BASH, '-c', step['run']], cwd=workspace,
                                                env={**os.environ, **step_env(step), **policy_env, **change,
                                                     'PATH': str(bindir), 'CANDIDATE_SCOPE': 'full',
                                                     'REFUSAL': '0'},
                                                capture_output=True, text=True, check=False)
                        self.assertNotEqual(result.returncode, 0, change)
                        self.assertEqual(result.stdout, '')
                        self.assertNotIn('candidate validator copy executed', result.stderr)

    def policy_repo(self, root):
        """A temporary Git repository holding the real validator and fixture control files."""
        repo = root / 'policy'
        repo.mkdir()

        def git(*args):
            return subprocess.check_output(['git', '-c', 'user.name=CI Test',
                                            '-c', 'user.email=ci@example.invalid',
                                            '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
                                            '-c', 'core.autocrlf=false', *args], cwd=repo,
                                           text=True, stderr=subprocess.DEVNULL).strip()
        git('init', '-b', 'main')
        for path in m.CONTROL_FILES:
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if path == 'scripts/ci/merge_candidate.py':
                target.write_bytes(Path(m.__file__).read_bytes())
            else:
                target.write_text(f'# policy fixture {path}\n')
        (repo / 'app').mkdir()
        (repo / 'app' / 'main.py').write_text('print("candidate application code")\n')
        git('add', '-A')
        git('commit', '-m', 'policy')
        return repo, git

    def run_staging(self, repo, runner_temp, env_file, sha, default_branch='main'):
        stage_step = next(s for s in self.ci['jobs']['filter']['steps'] if s.get('name') == STAGING)
        for doc, jobs in ((self.ci, CANDIDATE_JOBS['ci.yml']), (self.rust, CANDIDATE_JOBS['rust.yml'])):
            for job in jobs:
                other = next(s for s in doc['jobs'][job]['steps'] if s.get('name') == STAGING)
                self.assertEqual(other['run'], stage_step['run'])
        env = {**os.environ, 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'],
               'GITHUB_SHA': sha, 'GITHUB_WORKFLOW_SHA': sha, 'DEFAULT_BRANCH': default_branch,
               'GITHUB_REF': 'refs/heads/main', 'GITHUB_REF_NAME': 'main',
               'RUNNER_TEMP': Path(runner_temp).as_posix(), 'GITHUB_ENV': Path(env_file).as_posix()}
        return subprocess.run([BASH, '-c', stage_step['run']], cwd=repo, env=env,
                              capture_output=True, text=True, check=False)

    def test_actual_staging_binds_policy_controls_and_refuses_candidate_alterations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, git = self.policy_repo(root)
            sha = git('rev-parse', 'HEAD')
            runner_temp, env_file = root / 'runner-temp', root / 'github.env'
            runner_temp.mkdir()
            env_file.touch()
            # Witness disagreement or a wrong branch refuses before any staging.
            for bad in [{'sha': 'a' * 40}, {'default_branch': ''}]:
                result = self.run_staging(repo, runner_temp, env_file, **{**{'sha': sha}, **bad})
                self.assertNotEqual(result.returncode, 0, bad)
                self.assertFalse((runner_temp / 'codex-policy' / 'manifest.json').exists())
            result = self.run_staging(repo, runner_temp, env_file, sha)
            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = dict(line.split('=', 1) for line in env_file.read_text().splitlines() if line)
            self.assertEqual(recorded['CODEX_POLICY_SHA'], sha)
            self.assertEqual(recorded['CODEX_POLICY_BRANCH'], 'main')
            staged = Path(recorded['CODEX_POLICY_VALIDATOR'])
            stage = staged.parent
            self.assertEqual(staged.read_bytes(), Path(m.__file__).read_bytes())
            self.assertFalse(stage.is_relative_to(repo))
            manifest = json.loads((stage / 'manifest.json').read_text())
            self.assertEqual(manifest['policy_sha'], sha)
            self.assertEqual(set(manifest['files']), set(m.CONTROL_FILES))
            for path in m.CONTROL_FILES:
                self.assertEqual(manifest['files'][path]['oid'], git('rev-parse', f'{sha}:{path}'))
                self.assertEqual((stage / 'controls' / path).read_bytes(), (repo / path).read_bytes())

            def candidate(mutate):
                """A candidate working tree: the policy tree plus one alteration."""
                workspace = root / 'candidate'
                if workspace.exists():
                    shutil.rmtree(workspace)
                shutil.copytree(repo, workspace, ignore=shutil.ignore_patterns('.git'), symlinks=True)
                mutate(workspace)
                return workspace

            def symlink(target, link):
                try:
                    os.symlink(target, link)
                except (OSError, NotImplementedError) as error:
                    self.skipTest(f'symlinks unavailable: {error}')

            # Pristine candidate: identical controls pass; the application may differ.
            m.verify_controls(candidate(lambda ws: (ws / 'app' / 'main.py').write_text('changed\n')),
                              stage, sha)
            alterations = {
                'installer': lambda ws: (ws / '.github/actions/run-gates/install.py').write_text(
                    '# policy fixture .github/actions/run-gates/install.py\nimport os\n'),
                'action': lambda ws: (ws / '.github/actions/run-gates/action.yml').write_text('runs: {}\n'),
                'pins': lambda ws: (ws / '.github/actions/run-gates/requirements.txt').write_text('evil==1\n'),
                'aggregate helper': lambda ws: (ws / 'tools/required_ci_gate.py').write_text('gate=lambda *a: True\n'),
                'validator': lambda ws: (ws / 'scripts/ci/merge_candidate.py').write_text('pass\n'),
                'workflow': lambda ws: (ws / '.github/workflows/ci.yml').write_text('jobs: {}\n'),
                'whitespace': lambda ws: (ws / '.github/actions/run-gates/install.py').write_bytes(
                    (ws / '.github/actions/run-gates/install.py').read_bytes().replace(b'\n', b'\r\n')),
                'missing installer': lambda ws: (ws / '.github/actions/run-gates/install.py').unlink(),
                'missing helper': lambda ws: (ws / 'tools/required_ci_gate.py').unlink(),
                'extra action module': lambda ws: (ws / '.github/actions/run-gates/subprocess.py').write_text('x\n'),
                'extra action dir': lambda ws: (ws / '.github/actions/run-gates/lib').mkdir(),
                'shadow tools package': lambda ws: (ws / 'tools/__init__.py').write_text('x\n'),
                'shadow tools module': lambda ws: (ws / 'tools.py').write_text('x\n'),
                'shadow stdlib': lambda ws: (ws / 'json.py').write_text('x\n'),
                'shadow stdlib package': lambda ws: (ws / 'os').mkdir(),
                'shadow helper package': lambda ws: (ws / 'tools/required_ci_gate').mkdir(),
                'sourceless stdlib': lambda ws: (ws / 'json.pyc').write_bytes(b'forged'),
                'interpreter startup': lambda ws: (ws / 'sitecustomize.py').write_text('raise SystemExit(0)\n'),
                'symlinked installer': lambda ws: (
                    (ws / '.github/actions/run-gates/install.py').unlink(),
                    symlink(ws / 'app' / 'main.py', ws / '.github/actions/run-gates/install.py')),
                'symlinked action tree': lambda ws: (
                    shutil.move(ws / '.github/actions/run-gates', ws / 'elsewhere'),
                    symlink(ws / 'elsewhere', ws / '.github/actions/run-gates')),
                'symlinked tools dir': lambda ws: (
                    shutil.move(ws / 'tools', ws / 'tools-real'),
                    symlink(ws / 'tools-real', ws / 'tools')),
            }
            for label, mutate in alterations.items():
                with self.subTest(alteration=label), self.assertRaises(m.CandidateError):
                    m.verify_controls(candidate(mutate), stage, sha)
            # Unavailable or foreign policy objects, and a stage inside the
            # workspace, refuse even a pristine candidate.
            pristine = candidate(lambda ws: None)
            with self.assertRaises(m.CandidateError):
                m.verify_controls(pristine, stage, 'a' * 40)
            with self.assertRaises(m.CandidateError):
                m.verify_controls(pristine, root / 'runner-temp' / 'absent', sha)
            with self.assertRaises(m.CandidateError):
                m.verify_controls(pristine, '', sha)
            inner = pristine / '.stage'
            shutil.copytree(stage, inner)
            with self.assertRaises(m.CandidateError):
                m.verify_controls(pristine, inner, sha)
            corrupt = root / 'corrupt'
            shutil.copytree(stage, corrupt)
            (corrupt / 'controls' / 'tools' / 'required_ci_gate.py').write_text('tampered\n')
            with self.assertRaises(m.CandidateError):
                m.verify_controls(pristine, corrupt, sha)
            (corrupt / 'manifest.json').unlink()
            with self.assertRaises(m.CandidateError):
                m.verify_controls(pristine, corrupt, sha)

    def test_unchecked_bytecode_cannot_replace_an_identical_aggregate_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, git = self.policy_repo(root)
            stage = root / 'stage'
            stage.mkdir()
            policy = git('rev-parse', 'HEAD')
            m.stage_policy(stage, policy, repo, git)
            original = (repo / 'tools/required_ci_gate.py').read_bytes()
            forged = root / 'forged.py'
            forged.write_text('print("FORGED_AGGREGATE_IMPORT")\n')
            cache = Path(importlib.util.cache_from_source(str(repo / 'tools/required_ci_gate.py')))
            cache.parent.mkdir(parents=True)
            py_compile.compile(str(forged), cfile=str(cache),
                               invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
            # The real interpreter uses the unchecked cache even though the
            # allowlisted source is unchanged. Refuse it before any aggregate runs.
            result = subprocess.run([sys.executable, '-c', 'import tools.required_ci_gate'],
                                    cwd=repo, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), 'FORGED_AGGREGATE_IMPORT')
            self.assertEqual((repo / 'tools/required_ci_gate.py').read_bytes(), original)
            with self.assertRaises(m.CandidateError):
                m.verify_controls(repo, stage, policy)

    def test_actual_staging_refuses_policy_commits_without_regular_control_blobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, git = self.policy_repo(root)
            runner_temp, env_file = root / 'runner-temp', root / 'github.env'
            runner_temp.mkdir()
            env_file.touch()
            # Missing control file in the policy commit.
            git('rm', '-q', '.github/actions/run-gates/requirements.txt')
            git('commit', '-m', 'drop pins')
            missing = git('rev-parse', 'HEAD')
            result = self.run_staging(repo, runner_temp, env_file, missing)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((runner_temp / 'codex-policy' / 'manifest.json').exists())
            self.assertEqual(env_file.read_text(), '')
            # A symlink tree entry for the installer, recorded in the index
            # without needing filesystem symlink support.
            git('reset', '-q', '--hard', 'HEAD~1')
            blob = subprocess.run(['git', 'hash-object', '-w', '--stdin'], cwd=repo, input='app/main.py\n',
                                  capture_output=True, text=True, check=True).stdout.strip()
            git('update-index', '--cacheinfo', f'120000,{blob},.github/actions/run-gates/install.py')
            git('commit', '-m', 'symlinked installer')
            linked = git('rev-parse', 'HEAD')
            result = self.run_staging(repo, runner_temp, env_file, linked)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((runner_temp / 'codex-policy' / 'manifest.json').exists())
            self.assertEqual(env_file.read_text(), '')
            # An unlisted entry inside the closed action tree is refused as policy too.
            git('reset', '-q', '--hard', 'HEAD~1')
            (repo / '.github/actions/run-gates/extra.py').write_text('x\n')
            git('add', '-A')
            git('commit', '-m', 'extra action file')
            result = self.run_staging(repo, runner_temp, env_file, git('rev-parse', 'HEAD'))
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((runner_temp / 'codex-policy' / 'manifest.json').exists())

    def test_docs_candidate_executes_all_five_python_and_rust_cells(self):
        for files in ('docs/example.md', '', 'app/main.py'):
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / 'output'
                decide = next(s for s in self.ci['jobs']['filter']['steps'] if s.get('id') == 'decide')
                result = subprocess.run(['bash', '-c', decide['run']], capture_output=True, check=False,
                                        env={**os.environ, **step_env(decide),
                                             'DIFF_FILES': files, 'GITHUB_OUTPUT': str(output)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('run-tests=true', output.read_text())
                for job in ('linux', 'windows', 'macos'):
                    self.assertTrue(evaluate(self.ci['jobs'][job]['if'], **{
                        **CONTEXT, 'needs.settle.result': 'skipped'}))
                self.assertEqual(self.ci['jobs']['linux']['strategy']['matrix']['python-version'],
                                 ['3.11', '3.12'])
                self.assertEqual(self.ci['jobs']['windows']['strategy']['matrix']['python-version'],
                                 ['3.11', '3.12'])
                gate = next(s for s in self.rust['jobs']['filter']['steps'] if s.get('id') == 'gate')
                output.unlink()
                result = subprocess.run(['bash', '-c', gate['run']], capture_output=True, check=False,
                                        env={**os.environ, **step_env(gate),
                                             'DIFF_FILES': files, 'GITHUB_OUTPUT': str(output)})
                self.assertEqual(result.returncode, 0, result.stderr)
                cells = json.loads(output.read_text().splitlines()[1])['include']
                self.assertEqual([(c['os-name'], c['python-version']) for c in cells],
                                 [('linux', '3.11'), ('linux', '3.12'),
                                  ('windows', '3.11'), ('windows', '3.12'), ('macos', '3.12')])

    def test_full_aggregate_rejects_every_partial_platform_result(self):
        gate = self.ci['jobs']['candidate-ci-gates']
        self.assertTrue(evaluate(gate['if'], **CONTEXT))
        self.assertFalse(evaluate(gate['if']))
        self.assertEqual(gate['needs'], ['filter', 'settle', 'linux', 'windows', 'macos'])
        baseline = {'FILTER_RESULT': 'success', 'SETTLE_RESULT': 'skipped',
                    'RUN_TESTS': 'true', 'CANDIDATE_SCOPE': 'full'}
        for states in itertools.product(('success', 'failure', 'skipped', 'cancelled'), repeat=3):
            env = {**os.environ, **baseline, **dict(zip(
                ('LINUX_RESULT', 'WINDOWS_RESULT', 'MACOS_RESULT'), states))}
            result = subprocess.run(['bash', '-c', gate['steps'][0]['run']], env=env,
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode == 0, states == ('success',) * 3)
        for key, value in [('FILTER_RESULT', 'failure'), ('SETTLE_RESULT', 'failure'),
                           ('SETTLE_RESULT', 'cancelled'), ('SETTLE_RESULT', 'success'),
                           ('RUN_TESTS', 'false'), ('RUN_TESTS', ''), ('CANDIDATE_SCOPE', 'linux')]:
            env = {**os.environ, **baseline, 'LINUX_RESULT': 'success',
                   'WINDOWS_RESULT': 'success', 'MACOS_RESULT': 'success', key: value}
            result = subprocess.run(['bash', '-c', gate['steps'][0]['run']], env=env,
                                    capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
        reject = gate['steps'][1]
        self.assertEqual(reject['if'], 'cancelled()')
        self.assertNotEqual(subprocess.run(['bash', '-c', reject['run']], check=False).returncode, 0)


if __name__ == '__main__':
    unittest.main()
