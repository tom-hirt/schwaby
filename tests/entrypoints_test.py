'''Each tool run the way it is used: as a script, under ``python -I`` so
neither the working directory nor PYTHONPATH is on the path, from a working
directory outside the checkout. A tool's own tests import it by path, which
proves its functions and not that the file starts: a tool that works only
with the repository root on the path passes every one of them.'''

import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, 'tools')


class EntrypointTest(unittest.TestCase):

    # Each tool with a main, and each hook, and the test that runs it.
    RUN_BY = {
        'light_check.py': 'light_check_test.py::LightCheckTest::'
        'test_a_commit_or_merge_through_the_hooks_is_refused_and_named',
        'redproof.py': 'entrypoints_test.py::EntrypointTest::'
        'test_redproof_runs_as_a_script',
        'redproof_audit.py': 'entrypoints_test.py::EntrypointTest::'
        'test_redproof_audit_runs_as_a_script',
        'repeat.py': 'entrypoints_test.py::EntrypointTest::'
        'test_repeat_runs_as_a_script',
        'reads.py': 'entrypoints_test.py::EntrypointTest::'
        'test_reads_runs_as_a_script',
        'hooks/pre-commit': 'light_check_test.py::LightCheckTest::'
        'test_a_commit_or_merge_through_the_hooks_is_refused_and_named',
        'hooks/pre-merge-commit': 'light_check_test.py::LightCheckTest::'
        'test_a_commit_or_merge_through_the_hooks_is_refused_and_named',
    }
    # The tools that run on POSIX only, whose tests skip on Windows. Every
    # other test above runs everywhere. A skip is seen by its decorator, on
    # the method or its class; one taken inside the test is not.
    POSIX_ONLY = {'redproof.py', 'repeat.py'}
    # The ones whose test runs the light check, which refuses every commit
    # without pyflakes: that test skips where [dev] is not installed.
    NEED_PYFLAKES = {'light_check.py', 'hooks/pre-commit',
                     'hooks/pre-merge-commit'}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name

    def run_tool(self, path, *args, env=None):
        return subprocess.run(
            [sys.executable, '-I', '-B', path, *args], cwd=self.cwd, env=env,
            capture_output=True, text=True, timeout=120)

    def test_every_tool_with_a_main_is_run_as_a_script(self):
        entrypoints = ['hooks/' + h
                       for h in os.listdir(os.path.join(TOOLS, 'hooks'))]
        for name in os.listdir(TOOLS):
            if name.endswith('.py'):
                with open(os.path.join(TOOLS, name), encoding='utf-8') as f:
                    if re.search(r'^if __name__ == .__main__.:', f.read(),
                                 re.M):
                        entrypoints.append(name)
        self.assertEqual(sorted(self.RUN_BY), sorted(entrypoints),
                         'a tool with a main, or a hook, that no test runs '
                         'as it is used, or one listed here that is gone')
        for name, by in self.RUN_BY.items():
            path, cls, test = by.split('::')
            module = importlib.import_module(
                'tests.' + path[:-len('.py')])
            self.assertTrue(hasattr(module, cls), '%s: no %s' % (name, by))
            method = vars(vars(module)[cls]).get(test)
            self.assertTrue(callable(method), '%s: no %s' % (name, by))
            skipped = (getattr(vars(module)[cls], '__unittest_skip__', False)
                       or getattr(method, '__unittest_skip__', False))
            expected = ((os.name == 'nt' and name in self.POSIX_ONLY)
                        or (importlib.util.find_spec('pyflakes') is None
                            and name in self.NEED_PYFLAKES))
            self.assertEqual(expected, skipped,
                             '%s: %s %s here' % (
                                 name, by,
                                 'is skipped' if skipped else 'runs'))

    @unittest.skipIf(os.name == 'nt', 'redproof.py runs on POSIX only')
    def test_redproof_runs_as_a_script(self):
        # From a checkout of its own, since it snapshots the git checkout it
        # sits in: a red-proof's copy of this one is not a checkout. Past its
        # imports, the snapshot and the cases file, to the first thing it
        # can refuse without running an entry.
        checkout = os.path.join(self.cwd, 'checkout')
        os.makedirs(os.path.join(checkout, 'tools'))
        for name in ('redproof.py', 'redproof_cases.py'):
            shutil.copy2(os.path.join(TOOLS, name),
                         os.path.join(checkout, 'tools', name))
        # Nothing from a surrounding git process: run inside a hook, git
        # exports GIT_INDEX_FILE and friends.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        # Its own identity too, though nothing here commits: never the host's.
        for args in (['init', '-q'], ['config', 'user.name', 'test'],
                     ['config', 'user.email', 'test@example.invalid'],
                     ['add', 'tools']):
            subprocess.run(['git', *args], cwd=checkout, env=env,
                           capture_output=True, check=True)
        run = self.run_tool(os.path.join(checkout, 'tools', 'redproof.py'),
                            '--name', 'no-entry-is-called-this', env=env)
        self.assertEqual(2, run.returncode, run.stdout + run.stderr)
        self.assertIn("no entry name contains 'no-entry-is-called-this'",
                      run.stdout, run.stderr)

    def test_redproof_audit_runs_as_a_script(self):
        # Run from the root of a copy, as the harness runs it: the copy's
        # package is the one it reports.
        package = os.path.join(self.cwd, 'schwaby')
        os.mkdir(package)
        open(os.path.join(package, '__init__.py'), 'w').close()
        run = self.run_tool(os.path.join(TOOLS, 'redproof_audit.py'),
                            '--where')
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertEqual(
            os.path.realpath(os.path.join(package, '__init__.py')),
            run.stdout.strip())

    @unittest.skipIf(os.name == 'nt', 'only redproof.py runs it this way, '
                     'and that runs on POSIX only')
    def test_redproof_audit_runs_pytest_as_a_script(self):
        # Its other way in, as the harness runs each entry: pytest, with the
        # audit hook installed and its record written.
        with open(os.path.join(self.cwd, 'test_one.py'), 'w') as f:
            f.write('def test_one():\n    open("made", "w").close()\n')
        record = os.path.join(self.cwd, 'record.json')
        env = {k: v for k, v in os.environ.items()
               if k != 'PYTEST_ADDOPTS'}
        env['REDPROOF_AUDIT'] = record
        run = self.run_tool(os.path.join(TOOLS, 'redproof_audit.py'),
                            '-q', '-p', 'no:cacheprovider', 'test_one.py',
                            env=env)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        with open(record) as f:
            written = json.load(f)['written']
        self.assertIn(os.path.join(os.path.realpath(self.cwd), 'made'),
                      [os.path.realpath(w) for w in written])

    def test_reads_runs_as_a_script(self):
        # Its check, on this checkout: past its imports and its walk of the
        # tree, to the map's answer.
        run = self.run_tool(os.path.join(TOOLS, 'reads.py'), '--check')
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)

    @unittest.skipIf(os.name == 'nt', 'repeat.py runs on POSIX only')
    def test_repeat_runs_as_a_script(self):
        test = os.path.join(self.cwd, 'test_one.py')
        with open(test, 'w') as f:
            f.write('def test_one():\n    pass\n')
        env = {k: v for k, v in os.environ.items()
               if k != 'PYTEST_ADDOPTS'}
        run = self.run_tool(os.path.join(TOOLS, 'repeat.py'),
                            '--runs', '1', test, env=env)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertIn('[1/1] passed', run.stdout)
        self.assertIn('repeat: 0 of 1 runs failed', run.stdout)
