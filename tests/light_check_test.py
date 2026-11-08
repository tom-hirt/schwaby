'''tools/light_check.py, against a throwaway repository: a commit that adds a
warning is refused, and one that only moves code or keeps an old warning is
not.'''

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from venv import EnvBuilder

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK = os.path.join(ROOT, 'tools', 'light_check.py')
HOOKS = os.path.join(ROOT, 'tools', 'hooks')


#: The pieces of a blind kill, kept apart so that no tracked file carries
#: one whole; the tree scan below would find it if one did.
PARENT, KILL, UP = 'pp' + 'id', 'k' + 'ill', 'par' + 'ent'

#: A word for the tests to forbid, made up and spelled in pieces so that no
#: tracked file carries it whole; the tree scan below would find it if one
#: did.
PROBE = 'qz' + 'xprobe'


def light_check():
    spec = importlib.util.spec_from_file_location('light_check', CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def forbidden():
    # This clone's own list, read by the check's own function, so the two
    # cannot disagree about the words.
    return light_check().forbidden_words()


# What a walk of a copy skips: what is not the repository's own.
NOT_THE_TREE = {'.git', '.venv', 'venv', '.uv-python', 'build', 'dist',
                '__pycache__', '.pytest_cache', 'htmlcov', '.tox'}


def the_tree(root):
    '''Every path the repository at root holds, relative to it: what git
    tracks in a checkout, so nothing a developer keeps beside it is read. A
    red-proof copy is not a checkout: it holds what is tracked and what git
    does not ignore, and is walked, without what is not the repository's
    own.'''
    # Nothing from a surrounding git process, which would point ls-files at
    # another repository.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    try:
        out = subprocess.run(['git', 'ls-files', '-z'], cwd=root, env=env,
                             capture_output=True)
    except FileNotFoundError:
        out = None
    if out is not None and out.returncode == 0 and out.stdout:
        return [os.fsdecode(p) for p in out.stdout.split(b'\0') if p]
    paths = []
    for top, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in NOT_THE_TREE
                   and not d.endswith('.egg-info')]
        paths += [os.path.relpath(os.path.join(top, f), root) for f in files]
    return paths


def carrying(root, words):
    '''(path, word) for each word a file under root carries, in its name or
    its content, in any case; and how many files were read.'''
    found, seen = [], 0
    for rel in the_tree(root):
        path = os.path.join(root, rel)
        if os.path.islink(path):
            # A link carries its target, not what the target holds.
            content = os.fsencode(os.readlink(path)).lower()
        elif os.path.isfile(path):
            with open(path, 'rb') as f:
                content = f.read().lower()
        else:
            continue
        seen += 1
        rel = rel.replace(os.sep, '/')
        found += [(rel, w) for w in words
                  if w in rel.lower() or w.encode('utf-8') in content]
    return found, seen


class ForbiddenWordsTest(unittest.TestCase):

    def test_no_file_in_the_tree_carries_a_forbidden_word(self):
        # This clone's words, if it configures any, and the probe, which a
        # clone with none still scans for.
        words = sorted(set(forbidden()) | {PROBE})
        found, seen = carrying(ROOT, words)
        # Positive control: the walk reached the files it is for.
        self.assertGreater(seen, 50, 'the walk read almost nothing')
        self.assertEqual([], found)

    def test_a_checkout_is_read_as_git_tracks_it(self):
        word = PROBE
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')

        def git(*args):
            subprocess.run(['git', *args], cwd=root, env=env,
                           capture_output=True, check=True)

        def write(rel, text):
            os.makedirs(os.path.dirname(os.path.join(root, rel)),
                        exist_ok=True)
            with open(os.path.join(root, rel), 'w', newline='\n') as f:
                f.write(text)

        git('init', '-q')
        write('clean.md', 'nothing\n')
        write('docs/deep.md', 'a %s job\n' % word.upper())
        expected = [('docs/deep.md', word)]
        if os.name != 'nt':
            os.symlink('%s-target' % word, os.path.join(root, 'link'))
            expected.append(('link', word))
        git('add', '-A')
        # Beside the checkout, untracked, as a developer's own files are.
        write('env/kept.dat', 'a %s job\n' % word)
        found, seen = carrying(root, [word])
        self.assertEqual(sorted(expected), sorted(found),
                         'an untracked file was read, or a tracked one or a '
                         "link's target was missed")
        # Positive control: outside a checkout the same files are walked,
        # untracked ones too. Moved, not deleted: git leaves its objects
        # read-only, which a plain rmtree cannot delete on Windows, and a
        # TemporaryDirectory's cleanup can.
        aside = tempfile.TemporaryDirectory()
        self.addCleanup(aside.cleanup)
        os.rename(os.path.join(root, '.git'), os.path.join(aside.name, 'git'))
        found, seen = carrying(root, [word])
        self.assertIn(('env/kept.dat', word), found,
                      'the walk outside a checkout missed a file')


class BlindKillTest(unittest.TestCase):

    def test_no_file_in_the_tree_signals_what_it_did_not_start(self):
        # By the check's own pattern, so the two cannot disagree.
        pattern = light_check().BLIND_KILL
        found, seen = [], 0
        for rel in the_tree(ROOT):
            path = os.path.join(ROOT, rel)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            seen += 1
            with open(path, 'rb') as f:
                content = f.read()
            found += [(rel, content.count(b'\n', 0, m.start()) + 1)
                      for m in pattern.finditer(content)]
        # Positive control: the walk reached the files it is for.
        self.assertGreater(seen, 50, 'the walk read almost nothing')
        self.assertEqual([], found)


class HooksTest(unittest.TestCase):
    '''The hooks as files, which needs no pyflakes to read.'''

    def test_both_hooks_run_the_check(self):
        for hook in ('pre-commit', 'pre-merge-commit'):
            with open(os.path.join(HOOKS, hook)) as f:
                self.assertIn('tools/light_check.py', f.read(), hook)
            self.assertTrue(os.access(os.path.join(HOOKS, hook), os.X_OK),
                            '%s is not executable' % hook)


# The check refuses every commit without pyflakes, so each of these would
# fail on that alone. [dev] installs it, and a check that it does keeps
# this skip from hiding anything where the suite is meant to run.
@unittest.skipIf(importlib.util.find_spec('pyflakes') is None,
                 'pyflakes not installed in this venv; [dev] installs it')
class LightCheckTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        # Nothing from a surrounding git process: run inside a hook, git
        # exports GIT_INDEX_FILE and friends, and they would point these
        # commands at the outer repository.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith('GIT_')}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_NOSYSTEM='1')
        self.git('init', '-q')
        self.git('config', 'user.name', 'test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', '--add', 'lightcheck.forbidden', PROBE)
        self.write('mod.py', 'import os\n\n\ndef f():\n    return 1\n')
        self.git('add', 'mod.py')
        self.git('commit', '-q', '-m', 'start')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, env=self.env,
                              capture_output=True, check=True)

    def write(self, name, text):
        # LF on every platform: written in text mode on Windows, each line
        # ends in CRLF, which `git diff --check` reports as trailing
        # whitespace.
        with open(os.path.join(self.repo, name), 'w', newline='\n') as f:
            f.write(text)

    def check(self):
        out = subprocess.run([sys.executable, CHECK], cwd=self.repo,
                             env=self.env, capture_output=True, text=True)
        return out.returncode, out.stdout

    def stage(self, name, text):
        self.write(name, text)
        self.git('add', name)

    def test_a_new_warning_is_refused_and_named(self):
        self.stage('mod.py', 'import os\nimport sys\n\n\ndef f():\n'
                   '    return 1\n')
        code, out = self.check()
        self.assertEqual(1, code, 'a new warning was let through: ' + out)
        self.assertIn("mod.py: 'sys' imported but unused", out)
        self.assertNotIn("'os'", out, 'a warning HEAD already had was blamed')

    def test_a_warning_moved_or_kept_is_not_new(self):
        self.stage('mod.py', '\n\nimport os\n\n\ndef f():\n    return 2\n')
        code, out = self.check()
        self.assertEqual(0, code, 'an old warning was blamed on this commit: '
                         + out)

    def test_a_new_file_is_checked_whole(self):
        self.stage('new.py', 'def g():\n    return undefined_name\n')
        code, out = self.check()
        self.assertEqual(1, code, 'a new file was not checked: ' + out)
        self.assertIn("new.py: undefined name 'undefined_name'", out)

    def test_a_file_that_does_not_parse_is_refused(self):
        self.stage('mod.py', 'def f(:\n')
        code, out = self.check()
        self.assertEqual(1, code, 'a file that does not parse was let '
                         'through: ' + out)
        self.assertIn('mod.py: syntax error', out)

    def test_a_forbidden_word_is_refused_and_named(self):
        word = PROBE
        cases = [('notes.md', 'a %s job\n' % word),
                 ('mod.py', 'import os\n\n\ndef f():\n    return %r\n'
                  % (word.upper() + '_CORES')),
                 ('notes-%s.txt' % word, 'nothing\n'),
                 (word.upper() + '.TXT', 'nothing\n')]
        if os.name != 'nt':
            # A name git could read as a stage number: ':0:x' is not ':x'.
            cases.append(('0:notes.md', 'a %s job\n' % word))
        for name, text in cases:
            with self.subTest(name=name):
                self.stage(name, text)
                code, out = self.check()
                self.assertEqual(1, code, 'a file carrying a forbidden word '
                                 'was let through: ' + out)
                self.assertIn('%s: carries %r' % (name, word), out)
                self.git('reset', '-q', '--hard')
        # Positive control: the same files without the word are let through.
        self.stage('notes.md', 'a job\n')
        code, out = self.check()
        self.assertEqual(0, code, out)

    def test_a_blind_kill_is_refused_and_named(self):
        # Each on the third line, so the line named is the one carrying it.
        cases = [('mod.py', 'os.get%s()' % PARENT),
                 ('mod.py', 'psutil.Process().%s()' % PARENT),
                 ('mod.py', 'psutil.Process().%s()' % UP),
                 ('mod.py', "open('/proc/1/status')  # PP%s:" % PARENT[2:]),
                 ('mod.py', 'os.%s(-1, signal.SIGKILL)' % KILL),
                 ('mod.py', "subprocess.run('%s -9 -1', shell=True)" % KILL),
                 ('mod.py', 'os.system("%s -9 -1")' % KILL),
                 ('mod.py', "subprocess.run(['%s', '-9', '-1'])" % KILL),
                 ('mod.py', 'subprocess.run(["%s", "--", "-1"])' % KILL),
                 ('mod.py', 'os.%s(0, signal.SIGTERM)' % KILL),
                 ('mod.py', 'os.%spg(0, 9)' % KILL),
                 ('mod.py', 'os.%spg(os.getpgrp(), 9)' % KILL),
                 ('mod.py', 'os.%spg(os.getpgid(0), 9)' % KILL),
                 ('mod.py', 'os.%spg(os.getpgid(os.getpid()), 9)' % KILL),
                 ('mod.py', 'os.%s(-os.getpgrp(), 9)' % KILL),
                 ('mod.py', 'os.%s(-os.getpid(), 9)' % KILL),
                 ('mod.py', "subprocess.run(['%s', '-s', 'TERM', '0'])" % KILL),
                 ('mod.py', "subprocess.run(['%s', '-s', 'KILL', '-1'])"
                  % KILL),
                 ('mod.py', "subprocess.run(['/bin/%s', '-9', '0'])" % KILL),
                 ('mod.py', "subprocess.run(['%s', '-9', '0'])" % KILL),
                 ('mod.py', 'os.system("%s -9 0")' % KILL),
                 ('mod.py', 'x = os.get%s() == 2' % PARENT),
                 ('run.sh', 'echo $%s' % PARENT.upper()),
                 ('run.sh', 'echo ${%s}' % PARENT.upper()),
                 ('run.sh', 'ps -o %s= -p "$pid"' % PARENT),
                 ('run.sh', '%s -9 -1' % KILL),
                 ('run.sh', '%s -s KILL -1 2>/dev/null' % KILL),
                 ('run.sh', '%s -9 -1>/dev/null' % KILL),
                 ('run.sh', 'echo `%s -9 -1`' % KILL),
                 ('run.sh', '%s 0' % KILL),
                 ('run.sh', '%s -TERM 0 2>/dev/null' % KILL),
                 ('run.sh', '%s -KILL 0  # all of it' % KILL),
                 ('run.sh', "trap '%s -- -$$' EXIT" % KILL),
                 ('run.sh', '%s -9 -$$' % KILL),
                 ('run.sh', 'p%s -g 0' % KILL),
                 ('run.sh', '%s -- -1' % KILL)]
        for name, line in cases:
            with self.subTest(line=line):
                self.stage(name, 'import os\n\n%s\n' % line)
                code, out = self.check()
                self.assertEqual(1, code, 'a blind kill was let through: '
                                 + out)
                self.assertIn("%s:3: looks up its parent's pid" % name, out)
                self.git('reset', '-q', '--hard')
        # Positive control: a hangup sent to one pid, a kill of a group
        # the caller started, one whose number only starts with 1, a probe
        # of whether a pid exists, a list of the signals, a kill of itself
        # alone, a parent pid read for a pid it recorded, an orphan's test
        # of whether init adopted it, and another object's method of the
        # same name are let through.
        self.stage('run.sh', '%s -1 "$pid"\n%s -9 -"$pgid"\n%s -9 -12\n'
                   '%s -0 "$pid"\n%s -s 0 "$pid"\n%s -0 0\n%s -l 0\n'
                   '%s -9 $$\np%s -g 12\n'
                   % ((KILL,) * 9))
        self.stage('mod.py', 'import os\nimport subprocess\n\n'
                   '%s, pgid = 1, 2\nos.%s(-pgid, 9)\nos.%s(-12, 9)\n'
                   "subprocess.run(['%s', '-1', str(pgid)])\n"
                   "subprocess.run(['%s', '-9', '-12'])\n"
                   'os.%s(pgid, 0)\nos.%spg(os.getpgid(pgid), 9)\n'
                   'if os.get%s() == 1:\n    pass\n'
                   'if os.get%s() != 1:\n    pass\n'
                   'subprocess.%s(0, 1)\n'
                   % (PARENT, KILL, KILL, KILL, KILL, KILL, KILL, PARENT,
                      PARENT, KILL))
        code, out = self.check()
        self.assertEqual(0, code, out)

    @unittest.skipIf(os.name == 'nt', 'no file name holds a colon there')
    def test_a_python_file_named_like_a_stage_is_checked(self):
        # ':0:x' is not ':x': read as the latter, '0:bad.py' would be stage 0
        # of 'bad.py', which does not exist, and pass unread.
        self.stage('0:bad.py', 'def f(:\n')
        code, out = self.check()
        self.assertEqual(1, code, 'a file named like a stage went unchecked: '
                         + out)
        self.assertIn('0:bad.py: syntax error', out)

    def test_a_word_configured_in_any_case_is_forbidden(self):
        # The list is read in lower case, as names and contents are.
        self.git('config', '--unset-all', 'lightcheck.forbidden')
        self.git('config', '--add', 'lightcheck.forbidden', PROBE.upper())
        self.stage('notes.md', 'a %s job\n' % PROBE)
        code, out = self.check()
        self.assertEqual(1, code, 'a word configured in upper case forbade '
                         'nothing: ' + out)
        self.assertIn('notes.md: carries %r' % PROBE, out)

    def test_a_forbidden_word_already_at_head_still_fails(self):
        # The whole file is read, not the lines a change adds: a commit
        # touching a file that carries the word carries it again.
        word = PROBE
        self.stage('notes.md', 'a %s job\nfirst\n' % word)
        self.git('commit', '-q', '-m', 'carries it')
        self.stage('notes.md', 'a %s job\nsecond\n' % word)
        code, out = self.check()
        self.assertEqual(1, code, 'a word already at HEAD was let through '
                         'again: ' + out)
        self.assertIn('notes.md: carries', out)

    def changed_set_checkout(self):
        # Two entries, one per test file, in the checkout's own index.
        os.makedirs(os.path.join(self.repo, 'tools'))
        self.stage('tools/redproof_cases.py', self.entries('a/one', 'b/two'))
        os.makedirs(os.path.join(self.repo, 'tests'))
        self.stage('tests/a_test.py', 'def test_a():\n    pass\n')
        self.stage('tests/b_test.py', 'def test_b():\n    pass\n')
        self.git('commit', '-q', '-m', 'entries')

    @staticmethod
    def entries(a, b):
        return ('class Case:\n'
                '    def __init__(self, name, test):\n'
                '        self.name, self.path, self.test = name, "x.py", test\n'
                'CASES = [Case(%r, "tests/a_test.py::T::t"),\n'
                '         Case(%r, "tests/b_test.py::T::t")]\n' % (a, b))

    def test_a_changed_test_file_names_its_entries(self):
        self.changed_set_checkout()
        self.stage('mod.py', 'import os\n\n\ndef f():\n    return 2\n')
        code, out = self.check()
        self.assertEqual(0, code, out)
        self.assertNotIn('red-proof', out, 'a commit with no test change '
                         'named entries')

        self.stage('tests/a_test.py', 'def test_a():\n    return\n')
        code, out = self.check()
        self.assertEqual(0, code, 'naming the changed set failed the '
                         'commit: ' + out)
        self.assertIn('1 red-proof entry reaches a file this commit '
                      'changes', out, out)
        self.assertIn('light check:   a/one\n', out)
        self.assertNotIn('b/two', out, 'an entry whose test is unchanged '
                         'was named')

    def test_a_moved_test_file_names_the_entries_of_its_old_name(self):
        self.changed_set_checkout()
        self.git('mv', 'tests/b_test.py', 'tests/c_test.py')
        code, out = self.check()
        self.assertIn('light check:   b/two\n', out,
                      'a moved test file did not name its entries')

    def test_the_entries_named_are_the_index_s(self):
        self.changed_set_checkout()
        self.stage('tests/a_test.py', 'def test_a():\n    return\n')
        self.write('tools/redproof_cases.py',
                   self.entries('a/not-staged', 'b/two'))
        code, out = self.check()
        self.assertIn('light check:   a/one\n', out,
                      'the entries named were not the index\'s')
        self.assertNotIn('not-staged', out)

    def test_entries_that_cannot_be_read_do_not_refuse_the_commit(self):
        self.changed_set_checkout()
        self.stage('tools/redproof_cases.py', 'raise ValueError("bad")\n')
        self.stage('tests/a_test.py', 'def test_a():\n    return\n')
        code, out = self.check()
        self.assertEqual(0, code, 'entries that could not be read refused '
                         'the commit: ' + out)
        self.assertIn('could not name the red-proof entries', out, out)
        self.assertIn('ValueError: bad', out)

    def test_entries_that_exit_do_not_decide_the_check(self):
        self.changed_set_checkout()
        self.stage('tools/redproof_cases.py', 'import sys\nsys.exit(0)\n')
        self.stage('tests/a_test.py', 'def test_a():\n    return\n')
        self.stage('mod.py', 'import os\nimport sys\n\n\ndef f():\n'
                   '    return 1\n')
        code, out = self.check()
        self.assertEqual(1, code, 'entries that exited let a new warning '
                         'through: ' + out)
        self.assertIn("mod.py: 'sys' imported but unused", out)
        self.assertIn('could not name the red-proof entries', out, out)

    def test_a_checkout_without_entries_names_none(self):
        # A checkout with the tool but no entries in its index: nothing to
        # name, and nothing to say about it.
        os.makedirs(os.path.join(self.repo, 'tests'))
        self.stage('tests/a_test.py', 'def test_a():\n    pass\n')
        code, out = self.check()
        self.assertEqual(0, code, out)
        self.assertNotIn('red-proof', out, 'a checkout with no entries said '
                         'something about them')
        # And with entries, the same change names them.
        os.makedirs(os.path.join(self.repo, 'tools'))
        self.stage('tools/redproof_cases.py', self.entries('a/one', 'b/two'))
        code, out = self.check()
        self.assertIn('light check:   a/one\n', out)

    def test_a_staged_helper_named_like_a_test_is_a_helper(self):
        # Judged by what the index holds, which defines no test, not the
        # working tree, which does.
        self.changed_set_checkout()
        self.stage('tests/test_helpers.py', 'def helper():\n    pass\n')
        self.write('tests/test_helpers.py', 'def test_x():\n    pass\n')
        code, out = self.check()
        self.assertIn('this commit changes a shared test helper '
                      '(tests/test_helpers.py)', out,
                      'a staged helper named like a test was taken for a '
                      'test: ' + out)

    def test_the_tests_a_commit_reaches_are_named_from_the_map(self):
        os.makedirs(os.path.join(self.repo, 'tools'))
        os.makedirs(os.path.join(self.repo, 'tests'))
        for name in ('a', 'b'):
            self.stage('tests/%s_test.py' % name,
                       'def test_%s():\n    pass\n' % name)
        self.stage('tools/reads.json', json.dumps({
            'tests/a_test.py': ['tests/a_test.py', 'mod.py'],
            'tests/b_test.py': ['tests/b_test.py']}))
        self.git('commit', '-q', '-m', 'map')
        self.stage('mod.py', 'import os\n\n\ndef f():\n    return 2\n')
        code, out = self.check()
        self.assertEqual(0, code, out)
        self.assertIn('1 test module read a file this commit changes; run '
                      'them before the change merges: python -m pytest '
                      'tests/a_test.py\n', out,
                      'the tests a commit reaches were not named: ' + out)
        self.assertNotIn('tests/b_test.py', out)

    def test_a_map_that_is_not_a_map_says_so(self):
        os.makedirs(os.path.join(self.repo, 'tools'))
        for shape, text in (('cut short', '{"tests/a_test.py": ['),
                            ('a list', '[]')):
            with self.subTest(shape=shape):
                self.stage('tools/reads.json', text)
                self.stage('mod.py', 'import os\n\n\ndef f():\n'
                           '    return %d\n' % len(text))
                code, out = self.check()
                self.assertEqual(0, code, out)
                self.assertIn('tools/reads.json is missing from the index '
                              'or is not a map, so no tests were named', out,
                              'a broken map named nothing, silently: ' + out)

    def test_a_deleted_test_module_is_not_named(self):
        os.makedirs(os.path.join(self.repo, 'tools'))
        os.makedirs(os.path.join(self.repo, 'tests'))
        self.stage('tests/a_test.py', 'def test_a():\n    pass\n')
        self.stage('tools/reads.json', json.dumps({
            'tests/a_test.py': ['tests/a_test.py', 'mod.py']}))
        self.git('commit', '-q', '-m', 'map')
        self.git('rm', '-q', 'tests/a_test.py')
        self.stage('mod.py', 'import os\n\n\ndef f():\n    return 2\n')
        code, out = self.check()
        self.assertEqual(0, code, out)
        self.assertNotIn('python -m pytest tests/a_test.py', out,
                         'a test module the commit deletes was named to run')

    def test_a_changed_test_helper_says_the_set_is_release_sized(self):
        self.changed_set_checkout()
        self.stage('tests/utils.py', '# shared\n')
        code, out = self.check()
        self.assertEqual(0, code, out)
        self.assertIn('this commit changes a shared test helper '
                      '(tests/utils.py)', out, out)
        self.assertIn('release-sized', out)

    def test_whitespace_errors_are_refused(self):
        self.stage('notes.txt', 'trailing   \n')
        code, out = self.check()
        self.assertEqual(1, code, 'a whitespace error was let through: ' + out)
        self.assertIn('trailing whitespace', out)

    def test_the_index_is_checked_not_the_working_tree(self):
        self.stage('mod.py', 'import os\nimport sys\n\n\ndef f():\n'
                   '    return 1\n')
        # Fixed on disk but not staged: the commit would still carry it.
        self.write('mod.py', 'import os\n\n\ndef f():\n    return 1\n')
        code, out = self.check()
        self.assertEqual(1, code, 'the working tree was checked instead of '
                         'the index: ' + out)
        self.assertIn("'sys' imported but unused", out)

    def test_a_renamed_file_keeps_its_old_warnings(self):
        self.git('mv', 'mod.py', 'renamed.py')
        code, out = self.check()
        self.assertEqual(0, code, 'a rename was blamed for the warnings the '
                         'file already had: ' + out)
        # The positive control: a rename that adds one is still caught.
        self.write('renamed.py', 'import os\nimport sys\n\n\ndef f():\n'
                   '    return 1\n')
        self.git('add', 'renamed.py')
        code, out = self.check()
        self.assertEqual(1, code, out)
        self.assertIn("'sys' imported but unused", out)

    def test_bytes_that_are_not_utf8_do_not_parse(self):
        with open(os.path.join(self.repo, 'mod.py'), 'wb') as f:
            f.write(b'x = "caf\xe9"\n')
        self.git('add', 'mod.py')
        code, out = self.check()
        self.assertEqual(1, code, 'a file Python cannot decode was let '
                         'through: ' + out)
        self.assertIn('syntax error', out)

    def test_code_that_parses_but_does_not_compile_is_refused(self):
        # pyflakes parses; the compiler refuses this, and so would import.
        self.stage('mod.py', 'import os\n\n\ndef f():\n    await os.g()\n')
        code, out = self.check()
        self.assertEqual(1, code, 'a file Python cannot compile was let '
                         'through: ' + out)
        self.assertIn("'await' outside async function", out)

    def test_a_syntax_error_already_at_head_still_fails(self):
        self.write('broken.py', 'def f(:\n')
        self.git('add', 'broken.py')
        self.git('commit', '-q', '--no-verify', '-m', 'broken')
        self.stage('broken.py', 'def f(:\n\n# edited\n')
        code, out = self.check()
        self.assertEqual(1, code, 'a file that still does not parse was let '
                         'through: ' + out)
        self.assertIn('broken.py: syntax error', out)

    def test_a_warning_that_names_a_line_is_the_same_when_moved(self):
        self.stage('dup.py', 'import os\nimport os\n')
        self.git('commit', '-q', '--no-verify', '-m', 'dup')
        self.stage('dup.py', '# a line above it\nimport os\nimport os\n')
        code, out = self.check()
        self.assertEqual(0, code, 'a warning naming a line was blamed on the '
                         'edit that moved it: ' + out)

    @unittest.skipIf(os.name == 'nt', 'a symlink on Windows needs a '
                     'privilege and core.symlinks, which these repos turn off')
    def test_a_symlink_replaced_by_a_file_is_checked(self):
        os.symlink('mod.py', os.path.join(self.repo, 'link.py'))
        self.git('add', 'link.py')
        self.git('commit', '-q', '--no-verify', '-m', 'link')
        os.remove(os.path.join(self.repo, 'link.py'))
        self.stage('link.py', 'import sys\n')
        code, out = self.check()
        self.assertEqual(1, code, 'a file that was a symlink went unchecked: '
                         + out)
        self.assertIn("'sys' imported but unused", out)

    @unittest.skipIf(os.name == 'nt', 'a symlink on Windows needs a '
                     'privilege and core.symlinks, which these repos turn off')
    def test_a_symlink_is_not_checked_as_code(self):
        os.symlink('mod.py', os.path.join(self.repo, 'link.py'))
        self.git('add', 'link.py')
        code, out = self.check()
        self.assertEqual(0, code, 'a symlink was checked as code: ' + out)

    def test_a_warning_only_in_the_working_tree_is_not_the_commits(self):
        # The other direction from the test above: staged clean, dirty on
        # disk. The commit carries the clean one.
        self.stage('mod.py', 'import os\n\n\ndef f():\n    return 2\n')
        self.write('mod.py', 'import os\nimport sys\n\n\ndef f():\n'
                   '    return 2\n')
        code, out = self.check()
        self.assertEqual(0, code, 'the working tree was checked instead of '
                         'the index: ' + out)

    def hook_checkout(self):
        # The hooks as git runs them, from a checkout's tools/.
        os.makedirs(os.path.join(self.repo, 'tools', 'hooks'))
        for name in ['light_check.py'] + ['hooks/' + h
                                          for h in os.listdir(HOOKS)]:
            shutil.copy2(os.path.join(ROOT, 'tools', name),
                         os.path.join(self.repo, 'tools', name))
        # Empty, so a hook that misses every stand-in fails by name.
        self.ran = os.path.join(self.repo, '.git', 'interpreters')
        open(self.ran, 'w').close()

    def stand_in(self, where, label):
        # A virtualenv, laid out as this platform lays one out (bin/python;
        # Scripts\python.exe on Windows), under the name a hook looks for.
        # Its interpreter notes each time it is the one that ran, and finds
        # this test's pyflakes: a hook that missed it would find another
        # python3, which may have pyflakes too and pass the same way.
        # Imported here, not at the top, so an environment without pyflakes
        # still collects the rest of the suite.
        import pyflakes
        EnvBuilder(symlinks=os.name != 'nt').create(where)
        site = sysconfig.get_path(
            'purelib', 'nt' if os.name == 'nt' else 'posix_prefix',
            vars={'base': where, 'platbase': where})
        with open(os.path.join(site, 'stand_in.pth'), 'w') as f:
            f.write('import sys; f = open(%r, "a"); f.write(%r); f.close()'
                    '\n%s\n' % (
                self.ran, label + '\n',
                os.path.dirname(os.path.dirname(pyflakes.__file__))))

    def last_ran(self):
        with open(self.ran) as f:
            ran = f.read().split()
        return ran[-1] if ran else None

    def test_each_hook_takes_the_interpreter_it_names(self):
        # The last one present wins: venv, then .venv at the checkout's root,
        # then the active virtualenv.
        self.hook_checkout()
        active = tempfile.TemporaryDirectory()
        self.addCleanup(active.cleanup)
        env = dict(self.env)
        env.pop('VIRTUAL_ENV', None)
        for hook in sorted(os.listdir(HOOKS)):
            for name in ('venv', '.venv'):
                shutil.rmtree(os.path.join(self.repo, name),
                              ignore_errors=True)
            steps = (('venv', os.path.join(self.repo, 'venv'), None),
                     ('.venv', os.path.join(self.repo, '.venv'), None),
                     ('active', active.name, active.name))
            for label, where, venv in steps:
                with self.subTest(hook=hook, interpreter=label):
                    self.stand_in(where, label)
                    if venv:
                        env['VIRTUAL_ENV'] = venv
                    # The hook itself; on Windows, through git, since only
                    # git starts its own sh there (`git hook run`, git 2.36
                    # or later).
                    run = ([os.path.join(self.repo, 'tools', 'hooks', hook)]
                           if os.name != 'nt' else
                           ['git', '-c', 'core.hooksPath=tools/hooks',
                            'hook', 'run', hook])
                    out = subprocess.run(run, cwd=self.repo, env=env,
                                         capture_output=True, text=True)
                    self.assertEqual(0, out.returncode,
                                     out.stdout + out.stderr)
                    self.assertEqual(label, self.last_ran(),
                                     '%s ran another interpreter' % hook)
            env.pop('VIRTUAL_ENV')

    def test_a_commit_or_merge_through_the_hooks_is_refused_and_named(self):
        # Through git itself: the check's answer is the commit's or the
        # merge's, refusing one that adds a warning and letting a clean one
        # through.
        self.hook_checkout()
        venv = tempfile.TemporaryDirectory()
        self.addCleanup(venv.cleanup)
        self.stand_in(venv.name, 'active')
        self.env['VIRTUAL_ENV'] = venv.name

        # Two side branches, one adding a warning, committed before the
        # hooks are on.
        for branch, text in (('side', 'import sys\n'),
                             ('clean', 'def g():\n    return 4\n')):
            self.git('checkout', '-q', '-b', branch)
            self.stage(branch + '.py', text)
            self.git('commit', '-q', '-m', branch)
            self.git('checkout', '-q', '-')
        self.stage('other.py', 'def h():\n    return 3\n')
        self.git('commit', '-q', '-m', 'other')
        self.git('config', 'core.hooksPath', 'tools/hooks')
        head = self.git('rev-parse', 'HEAD').stdout

        def run(*args):
            out = subprocess.run(['git', *args], cwd=self.repo, env=self.env,
                                 capture_output=True, text=True)
            return out.returncode, out.stdout + out.stderr

        self.stage('mod.py', 'import os\nimport sys\n\n\ndef f():\n'
                   '    return 1\n')
        code, out = run('commit', '-q', '-m', 'warning')
        self.assertNotEqual(0, code, 'pre-commit let a warning through: '
                            + out)
        self.assertIn("mod.py: 'sys' imported but unused", out)
        self.assertEqual(head, self.git('rev-parse', 'HEAD').stdout)
        self.assertEqual('active', self.last_ran())

        # A clean commit goes through: the refusal above was the check's,
        # not a hook that refuses everything.
        self.stage('mod.py', 'import os\n\n\ndef f():\n    return 2\n')
        code, out = run('commit', '-q', '-m', 'clean')
        self.assertEqual(0, code, 'pre-commit refused a clean commit: ' + out)
        head = self.git('rev-parse', 'HEAD').stdout

        code, out = run('merge', '-q', '--no-ff', '-m', 'merge', 'side')
        self.assertNotEqual(0, code, 'pre-merge-commit let a warning '
                            'through: ' + out)
        self.assertIn("side.py: 'sys' imported but unused", out)
        self.assertEqual(head, self.git('rev-parse', 'HEAD').stdout)
        open(self.ran, 'w').close()
        self.git('merge', '--abort')

        # And a clean merge goes through, by way of the same hook.
        code, out = run('merge', '-q', '--no-ff', '-m', 'merge', 'clean')
        self.assertEqual(0, code, 'pre-merge-commit refused a clean merge: '
                         + out)
        self.assertEqual('active', self.last_ran())
