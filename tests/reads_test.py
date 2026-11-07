'''tools/reads.py: the map of what each test module reads, which chooses the
tests a change reaches.'''

import importlib.util
import json
import os
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

    def test_a_directory_named_in_a_string_is_every_file_under_it(self):
        got = self.reads_of('tests/a_test.py',
                            b"for f in os.walk('docs'):\n    pass\n")
        self.assertIn('docs/a.rst', got)
        self.assertIn('docs/b/c.rst', got,
                      'a file deeper under a named directory was not read')

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

    def test_a_declared_read_is_added(self):
        with patch.dict(reads.DECLARED,
                        {'tests/a_test.py': {'docs/*': 'it walks docs'}}):
            got = self.reads_of('tests/a_test.py', b'')
        self.assertIn('docs/b/c.rst', got, 'a declared read was not added')

    def test_the_map_holds_test_modules_only(self):
        tree = dict(self.FILES, **{
            'tests/a_test.py': TEST,
            'tests/test_helpers.py': b'def helper():\n    pass\n'})
        built = reads.build(tree.get, sorted(tree))
        self.assertEqual(['tests/a_test.py'], sorted(built),
                         'a helper or a conftest.py was mapped as a test')


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
