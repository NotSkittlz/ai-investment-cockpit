"""Exercise actual workflow aggregate scripts and path filter offline."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.required_ci_gate import gate, settled

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.load((ROOT / '.github/workflows' / name).read_text(encoding='utf-8'), Loader=yaml.BaseLoader)


def execute(script, env):
    source = script.split("<<'PY'", 1)[1].rsplit('PY', 1)[0]
    return subprocess.run([sys.executable, '-c', source],
                          env={**os.environ, **env},
                          cwd=ROOT, capture_output=True, text=True)


class QueueGateTests(unittest.TestCase):
    def test_exhaustive_aggregate_states(self):
        states = ['success', 'failure', 'cancelled', 'skipped', '', 'pending', 'in_progress']
        for filter_result in states:
            for required in ['true', 'false', '', 'unknown']:
                for first in states:
                    for second in states:
                        expected = (filter_result == 'success' and
                                    ((required == 'true' and first == second == 'success') or
                                     (required == 'false' and first == second == 'skipped')))
                        self.assertEqual(gate(filter_result, required, [first, second]), expected)

    def test_actual_queue_aggregate_code(self):
        doc = workflow('ci.yml')['jobs']['queue-gates']
        self.assertEqual(doc['needs'], ['filter', 'settle', 'linux', 'windows'])
        self.assertIn('always()', doc['if'])
        script = doc['steps'][-2]['run']
        for required, win_required, linux, windows, expected in [
            ('true', 'true', 'success', 'success', True),
            ('true', 'true', 'success', 'failure', False),
            ('true', 'true', 'success', 'cancelled', False),
            ('true', 'true', 'success', 'skipped', False),
            ('true', 'true', 'success', 'pending', False),
            ('true', 'false', 'success', 'skipped', True),
            ('false', 'true', 'skipped', 'skipped', True),
            ('', 'true', 'skipped', 'skipped', False),
        ]:
            with self.subTest(required=required, linux=linux, windows=windows):
                result = execute(script, {'FILTER_RESULT': 'success', 'REQUIRED': required,
                                 'WINDOWS_REQUIRED': win_required, 'LINUX_RESULT': linux,
                                 'WINDOWS_RESULT': windows, 'SETTLE_RESULT': 'skipped',
                                 'SETTLE_EXPECTED': 'false'})
                self.assertEqual(result.returncode == 0, expected, result.stderr)

    def test_actual_rust_aggregate_code(self):
        doc = workflow('rust.yml')
        self.assertNotIn('paths', doc['on']['pull_request'])
        job = doc['jobs']['rust-gates']
        self.assertEqual(job['needs'], ['filter', 'settle', 'rust-book'])
        self.assertEqual(job['if'], 'always()')
        for required, result, expected in [('true', 'success', True), ('true', 'skipped', False),
                                            ('true', 'cancelled', False), ('true', 'failure', False),
                                            ('false', 'skipped', True), ('', 'skipped', False)]:
            actual = execute(job['steps'][-2]['run'], {'FILTER_RESULT': 'success',
                             'REQUIRED': required, 'RESULTS': json.dumps(result),
                             'SETTLE_RESULT': 'skipped', 'SETTLE_EXPECTED': 'false'})
            self.assertEqual(actual.returncode == 0, expected, actual.stderr)

    def test_settle_combines_with_actual_queue_and_rust_gates(self):
        for result in ('success', 'failure', 'cancelled', 'skipped', '', 'pending'):
            for expected in ('true', 'false', ''):
                accept = expected in ('true', 'false') and (
                    result == 'success' or (result == 'skipped' and expected == 'false'))
                self.assertEqual(settled(result, expected), accept)
                for required in ('true', 'false'):
                    work = 'success' if required == 'true' else 'skipped'
                    env = {'FILTER_RESULT': 'success', 'REQUIRED': required,
                           'WINDOWS_REQUIRED': 'true', 'LINUX_RESULT': work,
                           'WINDOWS_RESULT': work, 'RESULTS': json.dumps(work),
                           'SETTLE_RESULT': result, 'SETTLE_EXPECTED': expected}
                    for filename, job in [('ci.yml', 'queue-gates'), ('rust.yml', 'rust-gates')]:
                        script = workflow(filename)['jobs'][job]['steps'][-2]['run']
                        with self.subTest(job=job, settle=result, expected=expected, required=required):
                            self.assertEqual(execute(script, env).returncode == 0, accept)

    def test_cancelled_workflow_cannot_pass_even_after_successful_dependencies(self):
        from tools.test_ci_dispatch_scope import evaluate
        from unittest.mock import patch
        for filename, job in [('ci.yml', 'queue-gates'), ('rust.yml', 'rust-gates')]:
            doc = workflow(filename)['jobs'][job]
            with patch('tools.test_ci_dispatch_scope.eval', wraps=eval) as evaluator:
                evaluate(doc['if'])
                expression, environment = evaluator.call_args.args
            environment['cancelled'] = lambda: True
            self.assertTrue(eval(expression, environment))
            cancellation = doc['steps'][-1]
            with patch('tools.test_ci_dispatch_scope.eval', wraps=eval) as evaluator:
                evaluate(cancellation['if'])
                expression, environment = evaluator.call_args.args
            environment['cancelled'] = lambda: True
            self.assertTrue(eval(expression, environment))
            result = subprocess.run(['bash', '-c', cancellation['run']], capture_output=True)
            self.assertEqual(result.returncode, 1)

    def test_actual_rust_path_and_platform_matrix(self):
        step = next(s for s in workflow('rust.yml')['jobs']['filter']['steps'] if s.get('id') == 'gate')
        for path, windows, expected in [('docs/readme.md', 'true', []),
                                       ('rust/src/lib.rs', 'true', ['linux', 'linux', 'windows', 'windows']),
                                       ('.github/workflows/review-gate.yml', 'true', ['linux', 'linux', 'windows', 'windows']),
                                       ('app/quant/pnl.py', 'false', ['linux', 'linux']),
                                       # Native signals/normalization paths added on main select cells;
                                       # sibling ingest/quant files that have no Rust twin stay a noop.
                                       ('app/quant/signals.py', 'false', ['linux', 'linux']),
                                       ('app/quant/signals.py', 'true', ['linux', 'linux', 'windows', 'windows']),
                                       ('app/ingest/normalize.py', 'false', ['linux', 'linux']),
                                       ('app/tests/quant/test_signals.py', 'false', ['linux', 'linux']),
                                       ('app/tests/quant/test_signals_rust_parity.py', 'false', ['linux', 'linux']),
                                       ('app/tests/ingest/test_normalize.py', 'false', ['linux', 'linux']),
                                       ('app/tests/ingest/test_normalize_rust_parity.py', 'false', ['linux', 'linux']),
                                       ('app/tests/ingest/test_normalize_rust_parity.py', 'true',
                                        ['linux', 'linux', 'windows', 'windows']),
                                       ('docs/notes.md\napp/ingest/normalize.py', 'false', ['linux', 'linux']),
                                       ('app/ingest/edgar.py', 'true', []),
                                       ('app/quant/signals.py.bak', 'true', []),
                                       ('app/tests/ingest/test_edgar_brew.py', 'true', [])]:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / 'output'
                result = execute(step['run'], {'SKIP_CELLS': 'false', 'CHECK_PATHS': 'true',
                                 'DIFF_FILES': path, 'RUN_WINDOWS': windows, 'RUN_MACOS': 'false',
                                 'GITHUB_OUTPUT': str(output)})
                self.assertEqual(result.returncode, 0, result.stderr)
                matrix = json.loads(output.read_text().splitlines()[1])['include']
                self.assertEqual([cell['os-name'] for cell in matrix], expected)


if __name__ == '__main__':
    unittest.main()
