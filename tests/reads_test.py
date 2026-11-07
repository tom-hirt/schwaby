'''tools/reads.py: the map of what each test module reads, which chooses the
tests a change reaches.'''

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, 'tools', name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reads = load('reads')

TEST = b'import unittest\n\n\nclass T(unittest.TestCase):\n\n' \
       b'    def test_x(self):\n        pass\n'


class MapIsCurrentTest(unittest.TestCase):

    def test_the_committed_map_is_what_the_tree_reads(self):
        files = reads.tracked(ROOT)

        def read(path):
            try:
                with open(os.path.join(ROOT, path), 'rb') as f:
                    return f.read()
            except OSError:
                return None
        built = reads.build(read, files)
        with open(os.path.join(ROOT, reads.MAP), encoding='utf-8') as f:
            committed = json.load(f)
        stale = sorted(m for m in set(built) | set(committed)
                       if built.get(m) != committed.get(m))
        self.assertEqual([], stale, '%s is out of date for these; run '
                         'python -I -B tools/reads.py' % reads.MAP)
        self.assertIn('schwaby/auth.py', committed['tests/auth_test.py'],
                      'the map has a test module that reads nothing')


class MapAgainstEntriesTest(unittest.TestCase):
    '''The map against what the red-proof entries already say: an entry
    that proves fails its test when the file it mutates breaks, so the test
    reads that file. A miss is a read the scan cannot see, or an entry
    naming the wrong test or path. It cannot fail for a test module that
    declares every file read ('*' in DECLARED).'''

    def test_each_entry_s_tests_read_the_file_it_mutates(self):
        redproof = reads._redproof()
        with open(os.path.join(ROOT, reads.MAP), encoding='utf-8') as f:
            committed = json.load(f)
        missed, checked = [], 0
        for case in redproof.load_cases(ROOT):
            if case.removed or case.superseded:
                continue
            modules = [t.split('::')[0] for t in redproof.tests_of(case)]
            checked += 1
            if not any(redproof.reads_match(entry, case.path)
                       for m in modules for entry in committed.get(m, ())):
                missed.append('%s: %s does not read %s' % (
                    case.name, ' or '.join(modules), case.path))
        self.assertGreater(checked, 500, 'the entries were not read')
        self.assertEqual([], missed, 'the map misses a read an entry shows: '
                         'declare it in tools/reads.py DECLARED, or correct '
                         "the entry's test or path")

    def test_each_declaration_is_a_mapped_test_module(self):
        with open(os.path.join(ROOT, reads.MAP), encoding='utf-8') as f:
            committed = json.load(f)
        self.assertEqual([], sorted(set(reads.DECLARED) - set(committed)),
                         'a declaration names no test module in the map')


class ScanTest(unittest.TestCase):
    '''What the scan finds, on a small tree held in memory.'''

    FILES = {
        'setup.cfg': b'[tool:pytest]\npython_files = *test*.py\n',
        'README.md': b'',
        'docs/a.rst': b'',
        'docs/b/c.rst': b'',
        'schwaby/__init__.py': b'',
        'schwaby/auth.py': b'from .utils import f\n',
        'schwaby/utils.py': b'def f():\n    pass\n',
        'schwaby/streaming.py': b"NAME = 'README.md'\n",
        'tools/redproof.py': b'',
        'tests/__init__.py': b'',
        'tests/conftest.py': b'',
        'tests/utils.py': b'',
        'tests/orders/__init__.py': b'',
        'tests/orders/conftest.py': b'',
    }

    def reads_of(self, path, source, **files):
        tree = dict(self.FILES, **{path: source}, **files)
        return reads.reads_of(path, tree.get, sorted(tree))

    def test_an_import_is_followed_to_its_end(self):
        got = self.reads_of('tests/a_test.py', b'import schwaby.auth\n')
        self.assertIn('schwaby/__init__.py', got)
        self.assertIn('schwaby/auth.py', got)
        self.assertIn('schwaby/utils.py', got,
                      'what an imported module imports was not followed')
        self.assertNotIn('schwaby/streaming.py', got)

    def test_from_a_package_import_a_module(self):
        got = self.reads_of('tests/a_test.py', b'from schwaby import auth\n')
        self.assertIn('schwaby/auth.py', got,
                      '`from package import module` did not read the module')

    def test_a_relative_import_from_the_test_package(self):
        got = self.reads_of('tests/a_test.py', b'from .utils import x\n')
        self.assertIn('tests/utils.py', got,
                      'a relative import was not read')
        got = self.reads_of('tests/orders/a_test.py',
                            b'from ..utils import x\n')
        self.assertIn('tests/utils.py', got,
                      'a relative import two levels up was not read')

    def test_a_file_named_in_a_string(self):
        for shape, source, path in (
                ('its path', b"P = 'docs/a.rst'\n", 'docs/a.rst'),
                ('its name', b"P = 'README.md'\n", 'README.md'),
                ('its name without .py', b"load('redproof')\n",
                 'tools/redproof.py')):
            with self.subTest(shape=shape):
                self.assertIn(path, self.reads_of('tests/a_test.py', source),
                              'a file named by %s was not read' % shape)

    def test_a_directory_named_in_a_string_is_kept_as_the_directory(self):
        # So a file added under it later is read too.
        got = self.reads_of('tests/a_test.py',
                            b"for f in os.walk('docs'):\n    pass\n")
        self.assertIn('docs/', got,
                      'a named directory was not kept as the directory')
        redproof = reads._redproof()
        self.assertTrue(redproof.reads_any(got, {'docs/new/page.rst'}),
                        'a file added under a named directory was not read')

    def test_a_file_named_inside_a_string(self):
        got = self.reads_of(
            'tests/a_test.py',
            b"BY = 'redproof.py::T::test_x'\n"
            b"WHY = 'the docs and the tests say README.md'\n"
            b"HOW = 'see the part:docs'\n")
        self.assertIn('tools/redproof.py', got,
                      'a file named in a piece of a string was not read')
        self.assertNotIn('README.md', got,
                         'a word of prose was taken for a file')
        self.assertNotIn('docs/', got,
                         'a piece of a sentence was taken for a file')

    def test_only_a_test_module_or_helper_names_files(self):
        # schwaby/streaming.py names README.md; a test importing it does not
        # read README.md for that.
        got = self.reads_of('tests/a_test.py', b'import schwaby.streaming\n')
        self.assertIn('schwaby/streaming.py', got)
        self.assertNotIn('README.md', got,
                         "a library module's string was taken for a read")

    def test_what_pytest_loads_for_a_test_module(self):
        got = self.reads_of('tests/orders/a_test.py', b'')
        for path in ('setup.cfg', 'tests/conftest.py', 'tests/__init__.py',
                     'tests/orders/conftest.py', 'tests/orders/__init__.py'):
            self.assertIn(path, got, 'pytest loads %s for it' % path)
        # One that does not exist yet would be loaded once added.
        self.assertIn('tests/b/conftest.py',
                      self.reads_of('tests/b/a_test.py', b''),
                      'a conftest.py added later would not be read')

    def test_what_a_conftest_imports_is_read(self):
        got = self.reads_of('tests/a_test.py', b'',
                            **{'tests/conftest.py': b'from . import utils\n'})
        self.assertIn('tests/utils.py', got,
                      "what pytest's conftest.py imports was not read")

    def test_a_declared_read_is_added(self):
        with patch.dict(reads.DECLARED,
                        {'tests/a_test.py': {'docs/*': 'it walks docs'}}):
            got = self.reads_of('tests/a_test.py', b'')
        self.assertIn('docs/*', got, 'a declared read was not added')

    def test_the_map_holds_test_modules_only(self):
        tree = dict(self.FILES, **{
            'tests/a_test.py': TEST,
            'tests/test_helpers.py': b'def helper():\n    pass\n'})
        built = reads.build(tree.get, sorted(tree))
        self.assertEqual(['tests/a_test.py'], sorted(built),
                         'a helper or a conftest.py was mapped as a test')


class MatchTest(unittest.TestCase):

    def test_an_entry_is_a_file_a_directory_or_a_pattern(self):
        match = reads._redproof().reads_match
        for entry, path, expected in (
                ('docs/a.rst', 'docs/a.rst', True),
                ('docs/a.rst', 'docs/a.rst.orig', False),
                ('docs/', 'docs/b/c.rst', True),
                ('docs/', 'docsx/a.rst', False),
                ('*.md', 'SECURITY.md', True),
                ('*.md', 'SECURITY.txt', False)):
            with self.subTest(entry=entry, path=path):
                self.assertEqual(expected, match(entry, path),
                                 'an entry covered the wrong paths')


class ReachedTest(unittest.TestCase):

    READS = {'tests/a_test.py': ['tests/a_test.py', 'schwaby/auth.py'],
             'tests/b_test.py': ['tests/b_test.py']}

    def test_a_test_module_is_reached_by_what_it_reads(self):
        self.assertEqual(['tests/a_test.py'], reads.reached(
            self.READS, {'schwaby/auth.py'}, ['*test*.py']),
            'a test reading a changed file was not reached')

    def test_a_new_test_module_is_reached(self):
        self.assertEqual(['tests/new_test.py'], reads.reached(
            self.READS, {'tests/new_test.py', 'tests/conftest.py'},
            ['*test*.py']),
            'a new test module, or only it, was not reached')

    def test_a_new_file_under_a_read_directory_is_reached(self):
        self.assertEqual(['tests/a_test.py'], reads.reached(
            {'tests/a_test.py': ['schwaby/'], 'tests/b_test.py': []},
            {'schwaby/newmod.py'}, ['*test*.py']),
            'a new file under a directory a test reads reached nothing')


class TouchingTest(unittest.TestCase):
    '''--touching, as a script in a checkout of its own: the tests a
    branch reaches since it left a base.'''

    def test_the_tests_a_branch_reaches_are_named(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = tmp.name
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')

        def git(*args):
            subprocess.run(['git', *args], cwd=repo, env=env,
                           capture_output=True, check=True)

        def write(path, data):
            os.makedirs(os.path.dirname(os.path.join(repo, path)),
                        exist_ok=True)
            with open(os.path.join(repo, path), 'wb') as f:
                f.write(data)
        os.makedirs(os.path.join(repo, 'tools'))
        for name in ('reads.py', 'redproof.py'):
            shutil.copy2(os.path.join(ROOT, 'tools', name),
                         os.path.join(repo, 'tools', name))
        write('mod.py', b'X = 1\n')
        write('tests/a_test.py', b'import mod\n' + TEST)
        write('tests/b_test.py', TEST)
        for args in (['init', '-q', '-b', 'main'],
                     ['config', 'user.name', 'test'],
                     ['config', 'user.email', 'test@example.invalid'],
                     ['add', '.']):
            git(*args)

        def run(*args):
            out = subprocess.run(
                [sys.executable, '-I', '-B',
                 os.path.join(repo, 'tools', 'reads.py'), *args],
                cwd=repo, env=env, capture_output=True, text=True,
                timeout=120)
            return out.returncode, out.stdout + out.stderr
        self.assertEqual(0, run()[0])
        git('add', '.')
        git('commit', '-q', '-m', 'start')
        git('switch', '-q', '-c', 'branch')
        write('mod.py', b'X = 2\n')
        code, out = run('--touching', 'main')
        self.assertEqual(0, code, out)
        self.assertEqual('python -m pytest tests/a_test.py\n', out,
                         'the tests the branch reaches were not named')
