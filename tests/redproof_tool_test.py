'''The red-proof harness's own decisions: what a mutation does, which failure
counts, which writes to tracked files are caught, how many runs fit, and what
its worker processes do when one dies. A harness that calls a guard PROVEN
when it is not reports it working after it broke, and nothing else would
ever notice.'''

import argparse
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

# The harness is POSIX only (process groups, os.wait4, fcntl), and these tests
# read paths the way it does, with '/'.
if os.name == 'nt':
    raise unittest.SkipTest('the red-proof harness runs on POSIX only')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, 'tools', name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


redproof = load('redproof')
cases = load('redproof_cases')


def case(old='a = 1', new='a = 2', **kwargs):
    return cases.Case('x/y', 'schwaby/x.py', old, new,
                      'tests/x_test.py::T::test_x', 'expected', **kwargs)


def snapshot(content):
    s = redproof.Snapshot.__new__(redproof.Snapshot)
    s.content = content
    s.tracked = set(content)
    return s


class MutateTest(unittest.TestCase):

    def test_replaces_the_one_occurrence_in_bytes(self):
        original = 'x = "é"\na = 1\n'.encode('utf-8')
        mutated, why = redproof.mutate(case(), original)
        self.assertIsNone(why)
        self.assertEqual('x = "é"\na = 2\n'.encode('utf-8'), mutated)

    def test_applies_the_second_pair_too(self):
        mutated, why = redproof.mutate(
            case(also=('b = 1', 'b = 3')), b'a = 1\nb = 1\n')
        self.assertIsNone(why)
        self.assertEqual(b'a = 2\nb = 3\n', mutated)

    def test_refuses_a_no_op(self):
        mutated, why = redproof.mutate(case(new='a = 1'), b'a = 1\n')
        self.assertIsNone(mutated, 'a no-op mutation was applied')
        self.assertIn('identical', why)

    def test_refuses_text_that_is_missing_or_repeated(self):
        for original, count in ((b'b = 1\n', 0), (b'a = 1\na = 1\n', 2)):
            mutated, why = redproof.mutate(case(), original)
            self.assertIsNone(mutated, 'text appearing %d times was mutated'
                              % count)
            self.assertIn('appears %d times' % count, why)

    def test_refuses_a_mutant_that_does_not_parse(self):
        mutated, why = redproof.mutate(case(new='a = ('), b'a = 1\n')
        self.assertIsNone(mutated, 'a mutant that does not parse was applied')
        self.assertIn('does not parse where the original does', why)

    def test_takes_a_mutant_whose_original_does_not_parse_either(self):
        # Positive control: the refusal is of a mutation that broke parsing,
        # not of a file that never parsed.
        mutated, why = redproof.mutate(case(), b'a = 1\nb = (\n')
        self.assertIsNone(why, 'a file that never parsed was refused')
        self.assertEqual(b'a = 2\nb = (\n', mutated)

    def test_refuses_a_shell_script_mutant_that_does_not_parse(self):
        for shebang in ('#!/bin/sh', '#!/bin/bash', '#!/usr/bin/env bash'):
            with self.subTest(shebang=shebang):
                hook = cases.Case('x/y', 'tools/hooks/x', 'exit 0', 'if',
                                  'tests/x_test.py::T::test_x', 'expected')
                original = ('%s\nexit 0\n' % shebang).encode()
                mutated, why = redproof.mutate(hook, original)
                self.assertIsNone(mutated, 'a shell mutant that does not '
                                  'parse was applied')
                self.assertIn('does not parse where the original does', why)
                # Positive control: the same script, still parsing.
                hook.new = 'exit 1'
                mutated, why = redproof.mutate(hook, original)
                self.assertIsNone(why, 'a shell mutant that parses was '
                                  'refused')

    def test_does_not_check_a_file_it_cannot_parse(self):
        # Asked directly: through mutate() the original fails the same
        # check, so a refusal never shows.
        for path, data in (('docs/x.rst', b'(\n'),
                           ('tools/hooks/x', b'if\n'),
                           ('tools/x', b'#!/usr/bin/perl\nif\n')):
            with self.subTest(path=path):
                self.assertIsNone(redproof.parse_error(path, data),
                                  'a file it cannot parse was refused')


class FakeRun:
    '''Stands in for redproof.Run in judge(): a snapshot, and run() handing
    back a canned result and the tracked files it wrote.'''

    def __init__(self, content, result, changed=(), unchecked=None):
        self.snapshot = snapshot(content)
        self.result, self.changed = result, list(changed)
        self.unchecked_why = unchecked
        self.tracked_writes, self.unchecked = {}, {}
        self.ran = []

    def run(self, nodes, mutation=None):
        self.ran.append((nodes, mutation))
        return self.result, self.changed, self.unchecked_why


def result(code, output='', killed=None):
    return redproof.Result(code, output, 0.1, 1 << 20, killed)


class JudgeTest(unittest.TestCase):

    CONTENT = {'schwaby/x.py': b'a = 1\n'}
    PASSED = result(0, '1 passed')

    # What pytest -q prints for a test whose first assertion failed, with the
    # entry's expect text in the source it echoes and in nothing it says.
    ECHOED = '\n'.join([
        '    def test_x(self):',
        ">       self.assertIn('identical', why)",
        "        self.assertIsNone(mutated, 'expected')",
        "E       AssertionError: 'identical' not found in 'other'",
        'FAILED tests/expected_test.py::T::test_x - AssertionError: '
        "'identical' not found",
        '1 failed in 0.01s'])

    def judge(self, output, expect='expected', baseline=PASSED, code=1,
              **kwargs):
        c = case(**kwargs)
        c.expect = expect
        run = FakeRun(self.CONTENT, result(code, output))
        return redproof.judge(run, c, baseline)

    def test_expect_must_be_in_what_the_failure_says(self):
        verdict, detail = self.judge(self.ECHOED)
        self.assertEqual('RED?', verdict,
                         'a failure on another assertion was PROVEN because '
                         'the expect text was in the echoed source')
        said = self.ECHOED.replace("'identical' not found in",
                                   "'expected' not found in")
        self.assertEqual('PROVEN', self.judge(said)[0])

    SAME = "E       AssertionError: '7521' unexpectedly found in 'xxx'"

    @staticmethod
    def section(title, *said):
        return '\n'.join(['_ %s _' % title, '    def test_x(self):', *said])

    def test_a_subtest_qualifier_picks_the_failing_subtest(self):
        # Every subtest fails with the same message; only the subtest pytest
        # names in each header says which helper it was.
        both = '\n'.join([
            self.section("T.test_x (helper='_safe_name', offset=1)", self.SAME),
            self.section("T.test_x (helper='_safe_str', offset=1)", self.SAME),
            '=== short test summary info ===', '1 failed'])
        self.assertEqual('PROVEN', self.judge(
            both, expect='unexpectedly found in',
            subtest="helper='_safe_str'")[0])
        other = '\n'.join([
            self.section("T.test_x (helper='_safe_name', offset=1)", self.SAME),
            '=== short test summary info ===', '1 failed'])
        self.assertEqual('RED?', self.judge(
            other, expect='unexpectedly found in',
            subtest="helper='_safe_str'")[0],
            'a failure in another subtest was PROVEN')

    def test_a_subtest_label_is_never_the_match(self):
        # The test's own subtest keyword, and a failure that is a crash, not
        # the assertion: matching the label would call this PROVEN with every
        # assertion deleted.
        crashed = '\n'.join([
            self.section('T.test_x (protocol=0)',
                         'E   RecursionError: maximum recursion depth exceeded'),
            "SUBFAILED(protocol=0) tests/redact_test.py::T::test_x",
            '1 failed'])
        self.assertEqual('RED?', self.judge(crashed, expect='protocol=')[0],
                         'an entry matched on a label the test chose')

    def test_a_rewritten_asserts_quoted_expression_is_not_what_failed(self):
        # What pytest prints for `assert r != 0 or 'expected' == ''`: the
        # expression, quoted from the source, with the literal in it.
        bare = '\n'.join([
            self.section('test_other',
                         "E       AssertionError: assert (0 != 0 or "
                         "'expected' == ''",
                         'E         ',
                         "E         + expected)"),
            '1 failed'])
        self.assertEqual('RED?', self.judge(bare)[0],
                         "an assert's quoted expression was taken for what "
                         'failed')
        # The positive control: an assert's own message still counts, and
        # the explanation under it does not.
        with_message = '\n'.join([
            self.section('test_real', 'E       AssertionError: expected',
                         'E       assert 3 == 4'),
            '1 failed'])
        self.assertEqual('PROVEN', self.judge(with_message)[0])
        other = '\n'.join([
            self.section('test_other', 'E       AssertionError: the count',
                         "E       assert (0 == 1 and 'expected' != '')"),
            '1 failed'])
        self.assertEqual('RED?', self.judge(other)[0])

    def test_a_subtest_failure_message_counts(self):
        out = '\n'.join([self.section('T.test_x (i=1)',
                                       'E   AssertionError: expected 3'),
                          '1 failed'])
        self.assertEqual('PROVEN', self.judge(out)[0])

    def test_captured_output_and_the_summary_are_not_what_failed(self):
        # A captured log line and the short summary both echo text a test can
        # choose; only the failure's own E lines say what failed.
        out = '\n'.join([
            self.section('T.test_x', 'E   AssertionError: something else'),
            '----- Captured stdout call -----',
            'E   AssertionError: expected, printed by the test itself',
            '----- Captured log call -----',
            'ERROR    schwaby.streaming:streaming.py:812 expected',
            '=== short test summary info ===',
            'FAILED tests/x_test.py::T::test_x - AssertionError: expected',
            '1 failed'])
        self.assertEqual('RED?', self.judge(out)[0],
                         'captured output was taken for what failed')

    def test_a_baseline_that_fails_is_not_judged(self):
        verdict, detail = self.judge(self.ECHOED,
                                        baseline=result(1, 'FAILED'))
        self.assertEqual('SPEC', verdict, 'an entry was judged on a test that '
                         'fails unmutated')
        self.assertIn('does not pass before the mutation', detail)

    def test_a_run_with_no_exit_status_proves_nothing(self):
        # Killed after printing the very failure the entry expects: still no
        # verdict, because a run that did not finish proves nothing.
        verdict, detail = self.judge('E   AssertionError: expected',
                                        code=None)
        self.assertEqual('RED?', verdict, 'a timed-out run was judged')

    def test_a_pass_is_green_unless_superseded(self):
        self.assertEqual('GREEN', self.judge('1 passed', code=0)[0])
        self.assertEqual('SUPERSEDED', self.judge(
            '1 passed', code=0, superseded='covered elsewhere')[0])

    def test_a_run_that_could_not_be_checked_is_reported(self):
        run = FakeRun(self.CONTENT, result(1, 'E   expected'),
                      unchecked='it left no record of its writes')
        redproof.judge(run, case(), self.PASSED)
        self.assertEqual({'x/y': 'it left no record of its writes'},
                         run.unchecked, 'a run whose writes went unchecked '
                         'was not reported')

    def test_a_tracked_write_is_reported_against_the_entry(self):
        run = FakeRun(self.CONTENT, result(1, 'E   expected'),
                      changed=['schwaby/x.py'])
        self.assertEqual('PROVEN', redproof.judge(run, case(), self.PASSED)[0])
        self.assertEqual({'x/y': ['schwaby/x.py']}, run.tracked_writes,
                         'a tracked write went unreported')


class TrackedWritesTest(unittest.TestCase):

    def test_a_write_the_run_put_back_is_found(self):
        # The comparison after the run cannot see it: only the record can.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        copy = os.path.join(tmp.name, 'run1', 'copy')
        os.makedirs(copy)
        record = {'written': [os.path.join(copy, 'schwaby', 'x.py'),
                              os.path.join(copy, 'untracked.log'),
                              os.path.join(tmp.name, 'run1', 'home', 'x.py')],
                  'unresolved': []}
        self.assertEqual(['schwaby/x.py'], redproof.tracked_writes_in(
            record, copy, {'schwaby/x.py'}),
            'a write the run restored went unreported')

    def test_a_write_to_the_file_under_any_name_is_found(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        copy = os.path.join(tmp.name, 'copy')
        os.makedirs(os.path.join(copy, 'schwaby'))
        open(os.path.join(copy, 'schwaby', 'x.py'), 'w').close()
        inodes = redproof.tracked_inodes(copy, {'schwaby/x.py'})
        st = os.stat(os.path.join(copy, 'schwaby', 'x.py'))
        record = {'written': [os.path.join(tmp.name, 'elsewhere.py')],
                  'inodes': [[st.st_dev, st.st_ino]]}
        self.assertEqual(['schwaby/x.py'], redproof.tracked_writes_in(
            record, copy, {'schwaby/x.py'}, inodes),
            'a write to a tracked file under another name went unreported')

    def test_a_write_through_a_linked_path_is_found(self):
        # A copy under a linked temporary directory is written under either
        # spelling.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Resolved: the temporary directory may itself be under a link
        # (/var is one on macOS), and the write must name the real path.
        real = os.path.join(os.path.realpath(tmp.name), 'real')
        os.makedirs(os.path.join(real, 'copy'))
        os.symlink(real, os.path.join(tmp.name, 'link'))
        copy = os.path.join(tmp.name, 'link', 'copy')
        record = {'written': [os.path.join(real, 'copy', 'schwaby', 'x.py')]}
        self.assertEqual(['schwaby/x.py'], redproof.tracked_writes_in(
            record, copy, {'schwaby/x.py'}),
            'a write by the copy\'s real path went unreported')


class AuditTest(unittest.TestCase):
    '''The audit hook itself, under a real pytest: the one thing that sees
    a tracked file written and put back before the run ended.'''

    TEST = '''
import os

def test_writes_and_puts_back():
    with open('tracked.py', 'rb') as f:
        old = f.read()
    with open('tracked.py', 'wb') as f:
        f.write(b'changed')
    with open('tracked.py', 'wb') as f:
        f.write(old)
    os.rename('moved.py', 'moved2.py')
    os.rename('moved2.py', 'moved.py')
    with open('only_read.py') as f:
        f.read()
    # One path per kind of write, so each kind is seen on its own.
    os.remove('removed.py')
    os.chmod('chmodded.py', 0o644)
    os.chown('chowned.py', os.getuid(), os.getgid())
    os.utime('touched.py')
    os.truncate('truncated.py', 0)
    fd = os.open('sub', os.O_RDONLY)
    try:
        os.close(os.open('inner.py', os.O_WRONLY, dir_fd=fd))
        # Made in sub, not here: nothing may be recorded at ./decoy.py.
        os.close(os.open('decoy.py', os.O_WRONLY | os.O_CREAT, dir_fd=fd))
    finally:
        os.close(fd)
    # Through other names for a file, and through a descriptor opened only
    # for reading: the file itself is what was written.
    os.symlink('sym_target.py', 'sym_alias.py')
    with open('sym_alias.py', 'w') as f:
        f.write('through a symlink')
    os.link('hard_target.py', 'hard_alias.py')
    with open('hard_alias.py', 'w') as f:
        f.write('through a hard link')
    fd = os.open('fd_target.py', os.O_RDONLY)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    # What is not a write: a directory opened to read it, a file with no
    # name, a hard link made and removed again, a link's own times, and an
    # open that could only fail.
    os.close(os.open('sub', os.O_RDONLY))
    os.link('kept_a.py', 'kept_a_link.py')
    os.remove('kept_a_link.py')
    os.symlink('kept_b.py', 'kept_b_link.py')
    os.utime('kept_b_link.py', ns=(0, 0), follow_symlinks=False)
    try:
        open('kept_c.py', 'x')
    except FileExistsError:
        pass
    if hasattr(os, 'O_TMPFILE'):
        os.close(os.open('.', os.O_TMPFILE | os.O_WRONLY))
'''

    KINDS = (('removed.py', 'removal'), ('chmodded.py', 'chmod'),
             ('chowned.py', 'chown'), ('touched.py', 'utime'),
             ('truncated.py', 'truncation'),
             ('sub/inner.py', 'write relative to a directory descriptor'))
    IDENTITIES = (('sym_target.py', 'write through a symlink'),
                  ('hard_target.py', 'write through a hard link'),
                  ('fd_target.py', 'change through a read-only descriptor'))

    def test_a_write_put_back_is_recorded_and_a_read_is_not(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.realpath(tmp.name)
        os.makedirs(os.path.join(root, 'sub'))
        for name, data in (('test_x.py', self.TEST), ('tracked.py', 'a = 1\n'),
                           ('moved.py', ''), ('only_read.py', ''),
                           ('removed.py', ''), ('chmodded.py', ''),
                           ('chowned.py', ''), ('touched.py', ''),
                           ('truncated.py', 'x'), ('sub/inner.py', ''),
                           ('sym_target.py', ''), ('hard_target.py', ''),
                           ('fd_target.py', ''), ('kept_a.py', ''),
                           ('kept_b.py', ''), ('kept_c.py', '')):
            with open(os.path.join(root, name), 'w') as f:
                f.write(data)
        out = os.path.join(root, 'record.json')
        env = dict(os.environ, REDPROOF_AUDIT=out)
        run = subprocess.run(
            [sys.executable, '-I', '-B',
             os.path.join(ROOT, redproof.AUDIT), '-q', '-p',
             'no:cacheprovider', 'test_x.py'], cwd=root, env=env,
            capture_output=True, text=True, timeout=120)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        with open(out) as f:
            record = json.load(f)
        written = set(record['written'])
        inodes = {tuple(i) for i in record['inodes']}
        for name in ('tracked.py', 'moved.py', 'moved2.py'):
            self.assertIn(os.path.join(root, name), written,
                          'a write the test put back was not recorded '
                          '(%s)' % name)
        for name, kind in self.KINDS:
            self.assertIn(os.path.join(root, name), written,
                          'a %s was not recorded' % kind)
        for name, kind in self.IDENTITIES:
            st = os.stat(os.path.join(root, name))
            self.assertIn((st.st_dev, st.st_ino), inodes,
                          'a %s was not recorded' % kind)
        self.assertNotIn(os.path.join(root, 'decoy.py'), written,
                         'a write was recorded where it was not made')
        for name, what in (
                ('kept_a.py', 'a removed link was taken for its file'),
                ('kept_b.py', "a change to a link itself was taken for its "
                 "file's"),
                ('kept_c.py', 'an open that could only fail was taken for a '
                 'write')):
            st = os.stat(os.path.join(root, name))
            self.assertNotIn((st.st_dev, st.st_ino), inodes, what)
        self.assertNotIn(os.path.join(root, 'kept_c.py'), written,
                         'an open that could only fail was taken for a write')
        self.assertNotIn(os.path.join(root, 'sub'), written,
                         'a directory opened to read was recorded as a write')
        if hasattr(os, 'O_TMPFILE'):
            self.assertNotIn(root, written,
                             'a file with no name was recorded as a write')
        # The positive control: what was only read is not a write.
        self.assertNotIn(os.path.join(root, 'only_read.py'), written,
                         'a read was recorded as a write')


class RunRecordTest(unittest.TestCase):

    def test_a_write_the_record_names_is_one_the_run_made(self):
        # Run.run merges what the hook saw with the comparison after the
        # run, which cannot see a file put back.
        import threading
        from unittest import mock
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        content = {'schwaby/x.py': b'a = 1\n'}
        snap = snapshot(content)
        snap.copy = lambda dest: [
            os.makedirs(os.path.join(dest, 'schwaby'), exist_ok=True),
            open(os.path.join(dest, 'schwaby', 'x.py'), 'wb').write(b'a = 1\n')]
        run = redproof.Run.__new__(redproof.Run)
        run.tmp, run.snapshot, run.audit = tmp.name, snap, 'audit.py'
        run.largest, run.lowest_available = 0, None
        run.timing = threading.local()

        def run_tests(work, copy, nodes, audit, record, **kwargs):
            with open(record, 'w') as f:
                json.dump({'written': [os.path.join(copy, 'schwaby', 'x.py')],
                           'unresolved': []}, f)
            return result(1, 'E   expected')


        with mock.patch.object(redproof, 'run_tests', run_tests):
            _, changed, unchecked = run.run(['tests/x_test.py'])
        self.assertEqual(['schwaby/x.py'], changed,
                         'a write the hook recorded was dropped')
        self.assertIsNone(unchecked)

        def by_identity(work, copy, nodes, audit, record, **kwargs):
            # Written under another name: only the file's identity says so.
            st = os.stat(os.path.join(copy, 'schwaby', 'x.py'))
            with open(record, 'w') as f:
                json.dump({'written': [], 'inodes': [[st.st_dev, st.st_ino]],
                           'unresolved': []}, f)
            return result(1, 'E   expected')

        with mock.patch.object(redproof, 'run_tests', by_identity):
            self.assertEqual(['schwaby/x.py'], run.run(['tests/x_test.py'])[1],
                             'a write the hook knew by identity was dropped')

        # A run that left no record, and one whose writes the hook could
        # not all place: neither is a run with no writes.
        def no_record(work, copy, nodes, audit, record, **kwargs):
            return result(1, 'E   expected')

        def unresolved(work, copy, nodes, audit, record, **kwargs):
            with open(record, 'w') as f:
                json.dump({'written': [], 'unresolved': ['os.rename']}, f)
            return result(1, 'E   expected')

        for fake, why in ((no_record, 'it left no record of its writes'),
                          (unresolved, 'the audit could not place 1 of its '
                           'writes (os.rename)')):
            with self.subTest(fake.__name__):
                with mock.patch.object(redproof, 'run_tests', fake):
                    self.assertEqual(why, run.run(['tests/x_test.py'])[2],
                                     'a run whose writes went unchecked '
                                     'passed as having none')


class ReportTest(unittest.TestCase):

    def test_a_run_that_could_not_be_checked_fails_the_whole_run(self):
        import contextlib
        import io
        run = types.SimpleNamespace(tracked_writes={}, unchecked={})
        selected = [case()]
        verdicts = {'x/y': ('PROVEN', '', 0.1)}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, redproof.report(run, selected, verdicts, 0))
        run.unchecked['x/y'] = 'it left no record of its writes'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = redproof.report(run, selected, verdicts, 0)
        self.assertEqual(1, code, 'a run whose writes went unchecked passed')
        self.assertIn('x/y could not be checked for tracked writes: it left '
                      'no record of its writes', out.getvalue(),
                      'an unchecked run was not named')

    def test_what_a_worker_found_reaches_the_run(self):
        worker = types.SimpleNamespace(
            tracked_writes={'a': ['x.py']}, unchecked={'b': 'why'},
            largest=5, lowest_available=None)
        handed = redproof._worker_result(worker)
        self.assertEqual(({}, {}), (worker.tracked_writes, worker.unchecked))
        parent = types.SimpleNamespace(tracked_writes={}, unchecked={},
                                       largest=0, lowest_available=None)
        redproof._merge(parent, ('result',) + handed)
        self.assertEqual({'b': 'why'}, parent.unchecked,
                         'a worker\'s unchecked run did not reach the run')
        self.assertEqual({'a': ['x.py']}, parent.tracked_writes)


class BaselineTest(unittest.TestCase):

    def test_a_baselines_unchecked_run_is_reported(self):
        run = FakeRun(JudgeTest.CONTENT, result(0, '1 passed'),
                      unchecked='it left no record of its writes')
        redproof.run_baseline(run, ['tests/x_test.py'])
        self.assertEqual(
            {'the baseline of tests/x_test.py':
             'it left no record of its writes'}, run.unchecked,
            'a baseline whose writes went unchecked was not reported')

    def test_a_baselines_tracked_write_is_reported(self):
        run = FakeRun(JudgeTest.CONTENT, result(0, '1 passed'),
                      changed=['schwaby/x.py'])
        self.assertEqual(0, redproof.run_baseline(
            run, ['tests/x_test.py']).code)
        self.assertEqual(
            {'the baseline of tests/x_test.py': ['schwaby/x.py']},
            run.tracked_writes, 'a baseline that wrote a tracked file went '
            'unreported')


class SnapshotTest(unittest.TestCase):

    def test_secret_directories_at_the_root_are_left_out(self):
        files = ['.config/gh/hosts.yml', '.ssh/known_hosts',
                 '.gnupg/pubring.kbx', 'docs/config.rst']
        self.assertEqual(files[:3], redproof.secret_untracked(files, set()),
                         'a secret directory at the root was copied')

    def test_a_copy_keeps_each_files_mode(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src, dest = os.path.join(tmp.name, 's'), os.path.join(tmp.name, 'd')
        os.makedirs(os.path.join(src, 'tools'))
        hook = os.path.join(src, 'tools', 'hook')
        open(hook, 'w').close()
        os.chmod(hook, 0o755)
        redproof.copy_tree(['tools/hook'], src, dest)
        self.assertEqual(0o755, os.stat(os.path.join(dest, 'tools', 'hook'))
                         .st_mode & 0o777, 'a copy lost a file\'s mode')

    def test_untracked_secret_names_are_left_out(self):
        files = ['README.md', '.env.local', 'docs/api-credentials.rst',
                 'notes/token-notes.md', 'tests/secret_logging_test.py']
        tracked = {'README.md', 'tests/secret_logging_test.py'}
        self.assertEqual(
            ['.env.local', 'docs/api-credentials.rst', 'notes/token-notes.md'],
            redproof.secret_untracked(files, tracked),
            'an untracked secret-named file would be copied')


def group_and_start(pid):
    '''The process group of pid, and when it started, in clock ticks since
    boot.'''
    with open('/proc/%d/stat' % pid) as f:
        fields = f.read().rsplit(')', 1)[1].split()
    return int(fields[2]), int(fields[19])


def members(pgid, start):
    '''The pids in group pgid, zombies too, while it is still the group
    whose leader started at start; none once the leader's pid belongs to
    another process. A group's number cannot be given to a new process while
    any member lives, so with its leader gone the members left are its own.'''
    try:
        if group_and_start(pgid)[1] != start:
            return []
    except (OSError, ValueError, IndexError):
        pass
    found = []
    for d in os.listdir('/proc'):
        if not d.isdigit():
            continue
        try:
            group, _ = group_and_start(int(d))
        except (OSError, ValueError, IndexError):
            continue
        if group == pgid:
            found.append(int(d))
    return found


def kill_recorded_group(pgid, start):
    '''SIGKILL the members of group pgid, while its leader is still the one
    that started at start. Each is signalled through a pidfd opened before it
    is checked again: if its pid changed hands in between, the pidfd is the
    old process's, and the signal reaches no one.'''
    for pid in members(pgid, start):
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            continue
        try:
            group, began = group_and_start(pid)
            if group == pgid and (pid != pgid or began == start):
                signal.pidfd_send_signal(fd, signal.SIGKILL)
        except (OSError, ValueError, IndexError):
            pass
        finally:
            os.close(fd)


def alive(pid, start):
    '''Whether the process that started at start is still running at pid,
    not a zombie and not another process given its pid since.'''
    try:
        with open('/proc/%d/stat' % pid) as f:
            fields = f.read().rsplit(')', 1)[1].split()
        return fields[0] != 'Z' and int(fields[19]) == start
    except (OSError, ValueError, IndexError):
        return False


@unittest.skipUnless(os.path.isdir('/proc'), 'the scenarios find processes '
                     'through /proc; without it every check of one passes '
                     'for the wrong reason')
class WorkerTest(unittest.TestCase):
    '''The worker processes: a dead or stopped one is reported, not waited
    for, what it left running is killed, and the rest of the work goes on.'''

    SCRIPT = '''
import importlib.util, os, signal, subprocess, sys, time
spec = importlib.util.spec_from_file_location('redproof', sys.argv[1])
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)
for s in (signal.SIGTERM, signal.SIGINT):
    signal.signal(s, rp.stop)

def group(name):
    # A pytest, as run_tests starts one and tells the parent about it.
    p = subprocess.Popen(['sleep', '120'], start_new_session=True,
                         stdout=subprocess.DEVNULL)
    rp._track(p.pid)
    # When it started, before its pid: a pid can be given to another
    # process once this one is gone, and the start tells them apart.
    with open('/proc/%d/stat' % p.pid) as f:
        start = f.read().rsplit(')', 1)[1].split()[19]
    print(name + '@', start, flush=True)
    print(name, p.pid, flush=True)
    return p

# This script's pid and session, taken before any worker exists. A worker
# finds its siblings by them rather than by its own parent: orphaned, a
# worker's parent is whatever subreaper adopted it, often the user's
# session manager, whose children are the whole desktop.
SCENARIO, SESSION = os.getpid(), os.getsid(0)

def siblings():
    out = []
    for d in os.listdir('/proc'):
        if not d.isdigit():
            continue
        try:
            with open('/proc/%s/stat' % d) as f:
                fields = f.read().rsplit(')', 1)[1].split()
            ppid, session = int(fields[1]), int(fields[3])
        except (OSError, ValueError, IndexError):
            continue
        if (ppid == SCENARIO and session == SESSION
                and int(d) != os.getpid()):
            out.append(int(d))
    return out

def task(item):
    if item == 'dies':
        group('orphan')
        time.sleep(0.5)
        os._exit(1)
    if item == 'stopped':
        group('stopped-orphan')
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(30)
    if item == 'slow':
        # Its own group must outlive a sibling's death.
        p = group('slow-group')
        time.sleep(2)
        alive = p.poll() is None
        rp.kill_group(p.pid)
        p.wait()
        rp._untrack(p.pid)
        return ('done', item, alive)
    if item == 'kills-idle':
        time.sleep(0.5)
        found = siblings()
        for pid in found:
            os.kill(pid, signal.SIGKILL)
        # Whether it killed any: a search that finds none proves nothing.
        return ('done', item, bool(found))
    if item == 'raises':
        raise SystemExit(143)
    if item == 'big':
        # Larger than one read of the pipe: its frame arrives in pieces.
        return ('big', 'x' * 300000)
    if item == 'reaps-then-dies':
        p = group('reaped-group')
        rp.kill_group(p.pid)
        p.wait()
        rp._untrack(p.pid)
        os._exit(1)
    if item == 'dies-after':
        # Gone a moment after its result, while the parent is still busy
        # with that result: the next item meets a pipe nobody reads.
        signal.setitimer(signal.ITIMER_REAL, 0.05)
    if item == 'unpicklable':
        return lambda: None
    if item == 'hangs':
        time.sleep(20)
    if item in ('busy', 'wedged', 'wedged2', 'own'):
        if item in ('wedged', 'wedged2'):
            # Stuck: told to stop, it starts more tests instead, which it
            # reports after the parent has stopped reading.
            late = 'late-group' if item == 'wedged' else 'late2-group'
            signal.signal(signal.SIGTERM, lambda *a: group(late))
        group(item + '-group')
        open(MARK + item, 'w').close()
        time.sleep(30)
    if item.startswith('quick-'):
        # Done only once the busy one has started its tests. Bounded by
        # TASK_LIMIT, set below before any of these run: past it the parent
        # kills a worker and reports it failed, and the test fails on what
        # came first.
        while not os.path.exists(MARK + item[6:]):
            time.sleep(0.01)
    return ('done', item)

MARK = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), 'mark-')

failed = lambda item, why: ('failed', item, why)
# Every group the parent kills, so a reaped one killed again shows.
killed = []
real_kill_group = rp.kill_group
rp.kill_group = lambda pgid: (killed.append(pgid), real_kill_group(pgid))
for items, size in ((['a', 'b'], 2), (['dies', 'slow'], 2),
                    (['stopped', 'after'], 1), (['kills-idle', 'x'], 3),
                    (['raises', 'a'], 1), (['unpicklable'], 1),
                    (['reaps-then-dies'], 1)):
    got = sorted(rp.parallel(task, items, size, failed))
    print(items[0], got, flush=True)
print('killed', killed, flush=True)
got = [(r[0], len(r[1])) for r in rp.parallel(task, ['big'], 1, failed)]
print('big', got, flush=True)
rp.TASK_LIMIT = 1
print('hangs', sorted(rp.parallel(task, ['hangs'], 1, failed)), flush=True)
rp.WORKER_STOP_LIMIT = 1
# What each abandoned run below got first: its quick item, done.
firsts = []
for first in ('busy', 'wedged'):
    # The run ends early, as on a signal, with a worker still busy.
    started = time.monotonic()
    for result in rp.parallel(task, [first, 'quick-' + first], 2, failed):
        firsts.append(result)
        break
    print('abandoned-' + first, round(time.monotonic() - started, 1),
          flush=True)
# As if the parent's cleanup were cut short before it reaped: a stopped
# worker's own kill is then all there is.
real_reap = rp.reap
rp.reap = lambda pgid: None
for result in rp.parallel(task, ['own', 'quick-own'], 2, failed):
    firsts.append(result)
    break
rp.reap = real_reap
# A wait cut short, as by a signal landing in it: the worker is killed, and
# what it reported last is still read and its tests killed.
real_wait_gone = rp._wait_gone
cut = [False]
def cut_short(pid, deadline):
    if not cut[0]:
        cut[0] = True
        time.sleep(0.3)
        raise RuntimeError('a wait cut short')
    return real_wait_gone(pid, deadline)
rp._wait_gone = cut_short
try:
    for result in rp.parallel(task, ['wedged2', 'quick-wedged2'], 2, failed):
        firsts.append(result)
        break
except RuntimeError:
    pass
rp._wait_gone = real_wait_gone
print('firsts', firsts, flush=True)
# A cleanup that fails says so, not silently.
real_wait_gone = rp._wait_gone
def broken(pid, deadline):
    raise ValueError('cleanup broke')
rp._wait_gone = broken
pool = rp.parallel(task, ['a', 'b'], 2, failed)
next(pool)
try:
    pool.close()
    print('cleanup-error swallowed', flush=True)
except ValueError:
    print('cleanup-error raised', flush=True)
rp._wait_gone = real_wait_gone
# That pool never waited for its workers: reap them here, or they are this
# script's zombies until it exits, and then systemd's, which the check for
# a worker outliving its pool raced.
while True:
    try:
        os.waitpid(-1, 0)
    except ChildProcessError:
        break
got = []
for result in rp.parallel(task, ['dies-after', 'b', 'c'], 1, failed):
    got.append(result)
    time.sleep(0.3)
print('dies-after', sorted(got), flush=True)
'''

    def run_script(self):
        tmp = self.tmp()
        script = os.path.join(tmp, 'workers.py')
        with open(script, 'w') as f:
            f.write(self.SCRIPT)
        # To a file, not a pipe: a pipe is held open by whatever the script
        # leaves running, and reading it would wait on that, not the pool.
        with open(os.path.join(tmp, 'out'), 'w') as out, \
                open(os.path.join(tmp, 'err'), 'w') as err:
            proc = subprocess.Popen(
                [sys.executable, '-I', script, os.path.join(
                    os.path.dirname(__file__), '..', 'tools', 'redproof.py')],
                stdout=out, stderr=err, start_new_session=True)
        # Read while the script is this process's unreaped child, so the
        # pid is still its own.
        self.started = group_and_start(proc.pid)[1]

        def kill_what_it_started():
            # Each test group runs in a session of its own, so killing the
            # script's group does not reach them; the script names each as it
            # starts it, with when it started.
            with open(os.path.join(tmp, 'out')) as f:
                said = dict(line.partition(' ')[::2]
                            for line in f.read().splitlines())
            for name, pid in said.items():
                start = said.get(name + '@', '')
                if (name.endswith(('-group', 'orphan')) and pid.isdigit()
                        and start.isdigit()):
                    kill_recorded_group(int(pid), int(start))

        try:
            proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            # Not yet waited for, so its pid is still its own.
            os.killpg(proc.pid, 9)
            proc.wait()
            kill_what_it_started()
            self.stderr = ''
            self.fail('the pool waited on a dead or overdue worker')
        # A worker the script never waited for is reaped by init once the
        # script exits, which takes a moment: allow it one.
        deadline = time.monotonic() + 2
        while members(proc.pid, self.started):
            if time.monotonic() > deadline:
                kill_recorded_group(proc.pid, self.started)
                kill_what_it_started()
                self.stderr = ''
                self.fail('a worker outlived its pool')
            time.sleep(0.05)
        with open(os.path.join(tmp, 'err')) as f:
            self.stderr = f.read()
        self.returncode = proc.returncode
        with open(os.path.join(tmp, 'out')) as f:
            return dict(line.split(' ', 1) for line in f.read().splitlines())

    def say(self, message):
        # The script's own error, when it crashed, is the first thing a
        # reader of any failure here needs.
        return message + ('\nthe scenario script said:\n' + self.stderr[-2000:]
                          if self.stderr.strip() else '')

    def test_a_dead_worker_is_reported_and_its_tests_killed(self):
        lines = self.run_script()
        # Whatever the script started is killed however the test ends: an
        # assertion failing first would leave them running for two minutes.
        for name in ('orphan', 'stopped-orphan', 'busy-group', 'wedged-group',
                     'late-group', 'own-group', 'reaped-group', 'slow-group',
                     'wedged2-group', 'late2-group'):
            if lines.get(name, '').strip().isdigit():
                start = lines.get(name + '@', '').strip()
                if start.isdigit():
                    self.addCleanup(kill_recorded_group, int(lines[name]),
                                    int(start))
        # The positive control: workers that live return what they made.
        self.assertEqual("[('done', 'a'), ('done', 'b')]", lines.get('a'))
        self.assertEqual("[('done', 'slow', True), ('failed', 'dies', 'its "
                         "worker process stopped (exit 1)')]",
                         lines.get('dies'), 'a dead worker was not reported, '
                         'or took a sibling\'s tests with it')
        self.assertEqual("[('done', 'after'), ('failed', 'stopped', 'its "
                         "worker process stopped (exit 143)')]",
                         lines.get('stopped'), 'the work after a stopped '
                         'worker did not go on')
        self.assertEqual("[('done', 'kills-idle', True), ('done', 'x')]",
                         lines.get('kills-idle'), 'an idle worker\'s death '
                         'lost work, or no idle worker was killed')
        self.assertEqual("[('done', 'a'), ('failed', 'raises', 'the harness "
                         "failed: SystemExit: 143')]", lines.get('raises'),
                         'a task that raised ended its worker\'s work')
        self.assertRegex(lines.get('unpicklable', ''),
                         r"\('failed', 'unpicklable', .the harness failed: ",
                         'a result that cannot '
                         'cross to the parent was not reported as such')
        killed = lines.get('killed', '').replace('[', ' ').replace(
            ']', ' ').replace(',', ' ').split()
        # The positive control: the spy saw the dead worker's group killed,
        # so its silence about the reaped one means something.
        self.assertIn(lines.get('orphan'), killed, self.say(
            'the kill spy saw nothing, so it cannot say what was not killed'))
        self.assertIn('reaped-group', lines, self.say('the scenario never '
                      'reaped a group'))
        self.assertNotIn(lines['reaped-group'], killed,
                         self.say('a reaped group was killed again'))
        self.assertEqual("[('big', 300000)]", lines.get('big'),
                         'a result larger than one read was lost')
        self.assertEqual("[('failed', 'hangs', 'its worker process ran past "
                         "1 s and was killed')]", lines.get('hangs'),
                         'a task past its bound was not stopped')
        # Each abandoned run ended on its quick item, once its busy one had
        # started: anything else, and there was nothing busy to abandon.
        self.assertEqual(
            "[('done', 'quick-busy'), ('done', 'quick-wedged'), ('done', "
            "'quick-own'), ('done', 'quick-wedged2')]", lines.get('firsts'),
            self.say('an abandoned run did not end on its quick item'))
        # Told to stop, a busy worker stops at once; one that cannot is
        # killed at the bound, 1 s here.
        self.assertLess(float(lines.get('abandoned-busy', 99)), 0.9,
                        'an abandoned pool\'s busy worker was not told to '
                        'stop')
        # Left alive, a stuck worker keeps its killed tests as zombies, so
        # reaping them waits out REAP_LIMIT: 30 s, not about 1.
        self.assertLess(float(lines.get('abandoned-wedged', 99)), 5,
                        self.say('a stuck worker was not killed at the bound'))
        self.assertEqual('raised', lines.get('cleanup-error'),
                         self.say('a failing cleanup was swallowed'))
        self.assertEqual("[('done', 'b'), ('done', 'c'), ('done', "
                         "'dies-after')]", lines.get('dies-after'),
                         'a worker that died after its result lost the run')
        for name in ('orphan', 'stopped-orphan', 'busy-group',
                     'wedged-group', 'late-group', 'own-group', 'late2-group'):
            self.assertIn(name, lines, self.say('the scenario never started '
                          '%s' % name))
            pid, start = int(lines[name]), int(lines[name + '@'])
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and alive(pid, start):
                time.sleep(0.1)
            if alive(pid, start):
                kill_recorded_group(pid, start)
                self.fail('a dead worker\'s tests were left running (%s)'
                          % name)
        self.assertEqual(0, self.returncode, 'the scenario script failed: '
                         + self.stderr[-2000:])

    def test_a_second_signal_does_not_cut_the_first_ones_cleanup_short(self):
        import threading
        from unittest import mock
        with mock.patch.object(redproof, '_stopping', threading.Event()), \
                mock.patch.object(redproof, '_live', set()):
            with self.assertRaises(SystemExit):
                redproof.stop(15, None)
            try:
                second = redproof.stop(15, None)
            except SystemExit:
                second = 'raised'
            self.assertIsNone(second, 'a second signal raised into the '
                              'first one\'s cleanup')

    def tmp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def test_a_kill_spares_a_group_whose_leader_is_another_process(self):
        later = subprocess.Popen(['sleep', '60'], start_new_session=True)
        self.addCleanup(later.wait)
        self.addCleanup(later.kill)
        start = group_and_start(later.pid)[1]
        # Recorded as starting at another time: its pid now belongs to
        # another process, as a reused one would.
        self.assertEqual([], members(later.pid, start + 1),
                         'a group led by another process was found')
        kill_recorded_group(later.pid, start + 1)
        # A SIGKILL that landed ends a sleeping process at once; half a
        # second is only what a wrong pass would have to outlast.
        with self.assertRaises(subprocess.TimeoutExpired,
                               msg='a group led by another process was '
                                   'killed'):
            later.wait(timeout=0.5)
        # Positive control: recorded as it started, it is killed.
        self.assertEqual([later.pid], members(later.pid, start))
        kill_recorded_group(later.pid, start)
        self.assertEqual(-signal.SIGKILL, later.wait(timeout=10),
                         'a group the scenario recorded was not killed')

    def test_a_group_whose_leader_is_gone_is_still_killed(self):
        leader = subprocess.Popen(
            ['sh', '-c', 'sleep 60 >/dev/null 2>&1 & echo $!'],
            start_new_session=True, stdout=subprocess.PIPE, text=True)
        start = group_and_start(leader.pid)[1]
        self.addCleanup(kill_recorded_group, leader.pid, start)
        child = int(leader.stdout.readline())
        leader.stdout.close()
        leader.wait()
        child_start = group_and_start(child)[1]
        self.assertEqual([child], members(leader.pid, start),
                         'a group whose leader had gone was not found')
        kill_recorded_group(leader.pid, start)
        # Adopted when its leader went, so another process reaps it.
        deadline = time.monotonic() + 10
        while alive(child, child_start):
            self.assertLess(time.monotonic(), deadline, 'a group whose '
                            'leader had gone was not killed')
            time.sleep(0.05)

    def test_an_entry_judged_without_running_has_no_split(self):
        # A worker is forked with whatever the parent's first baseline left
        # in the timing, and would print it for an entry that never ran.
        import threading
        from unittest import mock
        run = types.SimpleNamespace(
            snapshot=types.SimpleNamespace(content={}), timing=threading.local(),
            tracked_writes={}, unchecked={}, largest=0, lowest_available=None)
        run.timing.copy, run.timing.pytest = 0.1, 0.5
        entry = case()
        with mock.patch.object(redproof, '_job', {
                'run': run, 'cases': {entry.name: entry},
                'baselines': {tuple(redproof.tests_of(entry)): None},
                }):
            result = redproof._entry_task(entry.name)
        self.assertEqual('SPEC', result[1])
        self.assertEqual('', result[4], 'an entry that never ran printed '
                         'another run\'s time split')


class ChangedTrackedTest(unittest.TestCase):

    def test_a_tracked_file_left_changed_is_named(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, data in (('a.py', b'one\n'), ('b.py', b'two\n')):
            with open(os.path.join(tmp.name, name), 'wb') as f:
                f.write(data)
        s = snapshot({'a.py': b'one\n', 'b.py': b'TWO\n'})
        run = types.SimpleNamespace(snapshot=s)
        self.assertEqual(['b.py'], redproof.Run.changed_tracked(
            run, tmp.name, None), 'a changed tracked file was not named')
        # The mutated file is expected to differ, by exactly its mutation.
        self.assertEqual([], redproof.Run.changed_tracked(
            run, tmp.name, ('b.py', b'two\n')))


class ShardTest(unittest.TestCase):

    def cases(self):
        return [cases.Case('area/entry-%d' % i, 'schwaby/x.py', 'a', 'b',
                           'tests/x_test.py', 'e') for i in range(200)]

    def test_the_slices_of_a_run_cover_every_entry_once(self):
        everything = self.cases()
        for n in (1, 2, 3, 7):
            seen = []
            for k in range(1, n + 1):
                seen += [c.name for c in redproof.select(everything, [], (k, n))]
            self.assertEqual(sorted(c.name for c in everything), sorted(seen),
                             'the slices of %d missed or repeated an entry' % n)

    def test_an_entry_keeps_its_slice_whatever_else_is_selected(self):
        everything = self.cases()
        for name in [c.name for c in everything]:
            # --name matches by substring, so entry-1 also selects entry-1x:
            # the narrowing is what varies here, and the answer must not.
            narrow = redproof.select(everything, [name], (2, 3))
            among = redproof.select(everything, [], (2, 3))
            self.assertEqual(
                name in [c.name for c in among],
                name in [c.name for c in narrow],
                'which slice %s is in depends on the others' % name)

    def test_the_shard_line_names_the_slice_and_the_whole(self):
        everything = self.cases()
        mine = redproof.select(everything, [], (2, 3))
        self.assertEqual(
            'redproof: shard 2/3: %d of 200 entries' % len(mine),
            redproof.shard_line((2, 3), len(mine), len(everything)),
            'the shard line misstates the slice or the whole')

    def test_an_empty_slice_is_not_a_failed_name(self):
        # One entry, three slices: two of them hold nothing.
        one = self.cases()[:1]
        found = [redproof.select(one, ['area/entry-0'], (k, 3))
                 for k in (1, 2, 3)]
        self.assertNotIn(None, found, 'an empty slice was reported as a name '
                         'matching nothing')
        self.assertEqual([0, 0, 1], sorted(len(f) for f in found))
        self.assertIsNone(redproof.select(one, ['nothing-like-it'], (1, 3)))


class ChangedSetTest(unittest.TestCase):
    '''--touching: the entries a branch's test changes reach, test file or
    file mutated.'''

    def entries(self):
        return [
            cases.Case('a/by-its-test', 'schwaby/x.py', 'a', 'b',
                       'tests/a_test.py::T::test_a', 'e'),
            cases.Case('b/by-what-it-mutates', 'tests/b_test.py', 'a', 'b',
                       'tests/c_test.py::T::test_c', 'e'),
            cases.Case('d/by-either-test', 'schwaby/y.py', 'a', 'b',
                       ['tests/c_test.py::T::test_c',
                        'tests/d_test.py::T::test_d'], 'e'),
            cases.Case('e/untouched', 'schwaby/x.py', 'a', 'b',
                       'tests/e_test.py::T::test_e', 'e'),
        ]

    # What a file holds, by path; any other is a test module shaped as this
    # repository's are, a TestCase with a test method.
    files = {'setup.cfg': b'[tool:pytest]\npython_files = *test*.py\n'}
    TEST_MODULE = (b'import unittest\n\n\nclass T(unittest.TestCase):\n\n'
                   b'    def test_x(self):\n        pass\n')

    def read(self, path):
        return self.files.get(path, self.TEST_MODULE)

    def chosen(self, *changed, files=None):
        read = self.read if files is None else dict(self.files, **files).get
        selected, helpers = redproof.changed_set(self.entries(), set(changed),
                                                 read)
        return [c.name for c in selected], helpers

    def test_an_entry_is_chosen_by_its_test_file(self):
        self.assertEqual((['a/by-its-test'], []),
                         self.chosen('tests/a_test.py', 'schwaby/w.py'),
                         'an entry was not chosen by its test file')

    def test_an_entry_is_chosen_by_the_test_file_it_mutates(self):
        self.assertEqual((['b/by-what-it-mutates'], []),
                         self.chosen('tests/b_test.py'),
                         'an entry was not chosen by the test file it '
                         'mutates')

    def test_an_entry_is_chosen_by_any_of_its_tests(self):
        self.assertEqual((['d/by-either-test'], []),
                         self.chosen('tests/d_test.py'),
                         'an entry was not chosen by its second test')

    def test_an_entry_is_chosen_by_what_its_test_reads(self):
        # By tools/reads.json: e_test reads schwaby/z.py, which nothing
        # else names.
        reads = json.dumps({'tests/e_test.py': ['tests/e_test.py',
                                                'schwaby/z.py']}).encode()
        self.assertEqual((['e/untouched'], []), self.chosen(
            'schwaby/z.py', files={'tools/reads.json': reads}),
            'an entry was not chosen by a file its test reads')
        self.assertEqual(([], []), self.chosen('schwaby/z.py'),
                         'an entry was chosen by a read with no map')

    def test_a_changed_helper_chooses_every_entry(self):
        for helper in ('tests/utils.py', 'tests/conftest.py',
                       'tests/orders/__init__.py',
                       'tests/testdata/preferences.json',
                       'tests/orders/data.json'):
            with self.subTest(helper=helper):
                selected, helpers = self.chosen(helper, 'tests/a_test.py')
                self.assertEqual(len(self.entries()), len(selected),
                                 'a changed helper did not choose every '
                                 'entry')
                self.assertEqual([helper], helpers)

    def test_a_helper_named_like_a_test_is_a_helper(self):
        # pytest collects it, and finds nothing to run in it.
        selected, helpers = self.chosen(
            'tests/test_helpers.py',
            files={'tests/test_helpers.py': b'def helper():\n    pass\n'})
        self.assertEqual(['tests/test_helpers.py'], helpers,
                         'a helper named like a test was taken for a test')

    def test_a_test_is_one_pytest_would_run(self):
        tests = {
            'a function': b'def test_x():\n    pass\n',
            'a TestCase method': self.TEST_MODULE,
            'a bare TestCase base': (b'from unittest import TestCase\n\n\n'
                                     b'class T(TestCase):\n\n'
                                     b'    def test_x(self):\n'
                                     b'        pass\n'),
            'a method of a Test class': (b'class TestX:\n\n'
                                         b'    def test_x(self):\n'
                                         b'        pass\n'),
            'an async method': (b'class TestX:\n\n'
                                b'    async def test_x(self):\n'
                                b'        pass\n'),
            'what does not parse': b'def test_x(:\n',
        }
        helpers = {
            'a function inside another': (b'def helper():\n\n'
                                          b'    def test_inner():\n'
                                          b'        pass\n'),
            'a method of another class': (b'class FakeServer:\n\n'
                                          b'    def test_connection(self):\n'
                                          b'        pass\n'),
            'tests all inherited': (b'from .base import Base\n\n\n'
                                    b'class TestX(Base):\n    pass\n'),
        }
        for shape, source in tests.items():
            with self.subTest(shape=shape):
                self.assertEqual([], self.chosen(
                    'tests/x_test.py', files={'tests/x_test.py': source})[1],
                    'a test pytest would run was taken for a helper')
        for shape, source in helpers.items():
            with self.subTest(shape=shape):
                self.assertEqual(['tests/x_test.py'], self.chosen(
                    'tests/x_test.py', files={'tests/x_test.py': source})[1],
                    'a module pytest runs nothing in was taken for a test')

    def test_a_test_module_is_what_setup_cfg_says(self):
        cfg = {'setup.cfg': b'[tool:pytest]\npython_files = check_*.py\n'}
        self.assertEqual((['a/by-its-test'], []), self.chosen(
            'tests/a_test.py', files={}),
            'a test module named as setup.cfg says was taken for a helper')
        self.assertEqual(['tests/a_test.py'], self.chosen(
            'tests/a_test.py', files=cfg)[1],
            "setup.cfg's python_files was not read")
        self.assertEqual((['a/by-its-test'], []), self.chosen(
            'tests/a_test.py', files={'setup.cfg': None}),
            "pytest's default was not taken without setup.cfg")
        self.assertEqual(['tests/atest.py'], self.chosen(
            'tests/atest.py', files={'setup.cfg': None})[1],
            "pytest's default, test_*.py and *_test.py, was not the one "
            'taken without setup.cfg')

    def test_a_change_no_entry_reads_or_mutates_chooses_nothing(self):
        self.assertEqual(([], []),
                         self.chosen('schwaby/z.py', 'tools/redproof.py'),
                         'a change outside tests/ chose an entry')

    def test_an_entry_is_chosen_by_the_code_it_mutates(self):
        self.assertEqual((['a/by-its-test', 'e/untouched'], []),
                         self.chosen('schwaby/x.py'),
                         'an entry was not chosen by the code it mutates')

    def test_a_test_module_no_entry_names_chooses_nothing(self):
        # tests/test.py, a test module, and one that is gone.
        self.assertEqual(([], []), self.chosen(
            'tests/test.py', 'tests/gone_test.py',
            files={'tests/gone_test.py': None}),
            'a test module no entry names chose entries')

    def test_the_line_says_what_was_chosen(self):
        one = self.entries()[:1]
        self.assertEqual(
            'redproof: touching main: 1 entry reaches a file the branch '
            'changes', redproof.touching_line('main', one, []))
        self.assertEqual(
            'redproof: touching main: a shared test helper changed '
            '(tests/utils.py), so the changed set is every entry, a '
            'release-sized run',
            redproof.touching_line('main', one, ['tests/utils.py']))

    def git_repo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')

        def git(*args):
            return subprocess.run(['git', *args], cwd=tmp.name, env=env,
                                  capture_output=True, check=True)
        for args in (['init', '-q', '-b', 'main'],
                     ['config', 'user.name', 'test'],
                     ['config', 'user.email', 'test@example.invalid']):
            git(*args)
        return tmp.name, env, git

    def write(self, repo, path, text):
        os.makedirs(os.path.dirname(os.path.join(repo, path)), exist_ok=True)
        with open(os.path.join(repo, path), 'w') as f:
            f.write(text)

    def test_the_branch_is_what_changed_since_it_left_its_base(self):
        repo, env, git = self.git_repo()
        for name in ('a', 'b', 'm', 'u'):
            self.write(repo, 'tests/%s_test.py' % name, '# %s\n' % name)
        git('add', '.')
        git('commit', '-q', '-m', 'start')
        git('switch', '-q', '-c', 'branch')
        git('mv', 'tests/b_test.py', 'tests/c_test.py')
        git('commit', '-q', '-m', 'moved')
        # Changed and not committed; new and not tracked; and what a merge
        # tool and an editor leave, and one more that git's configuration,
        # passed by environment, says to ignore.
        self.write(repo, 'tests/a_test.py', '# a, changed\n')
        self.write(repo, 'tests/new_test.py', '# new\n')
        self.write(repo, 'tests/a_test.py.orig', '# a, before\n')
        self.write(repo, 'tests/.a_test.py.swp', '')
        self.write(repo, 'tests/ignored_test.py', '# ignored\n')
        self.write(repo, 'ignore', 'tests/ignored_test.py\n')
        # And main moves on, in a file the branch never touched.
        git('stash', '-q', '-u')
        git('switch', '-q', 'main')
        self.write(repo, 'tests/m_test.py', '# m, on main\n')
        git('commit', '-q', '-am', 'main moves')
        git('switch', '-q', 'branch')
        git('stash', 'pop', '-q')
        # As if from inside a git process with an index of its own.
        with patch.dict(os.environ, dict(
                env, GIT_INDEX_FILE=os.path.join(repo, 'no-such-index'),
                GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.excludesFile',
                GIT_CONFIG_VALUE_0=os.path.join(repo, 'ignore')),
                clear=True):
            changed = redproof.changed_since('main', cwd=repo)
        self.assertEqual(
            {'tests/a_test.py', 'tests/b_test.py', 'tests/c_test.py',
             'tests/new_test.py'}, changed,
            'the branch\'s changes since it left main were misread')

    def test_every_variable_that_points_git_elsewhere_is_dropped(self):
        # git's own list of them, less the three that carry configuration
        # rather than a place.
        out = subprocess.run(['git', 'rev-parse', '--local-env-vars'],
                             capture_output=True, text=True, check=True)
        config = {'GIT_CONFIG', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT'}
        self.assertIn('GIT_DIR', out.stdout.split(),
                      'git listed none of the variables it reads')
        missing = set(out.stdout.split()) - config - set(redproof.GIT_LOCATION)
        self.assertEqual(set(), missing,
                         'git points itself elsewhere by a variable a run '
                         'keeps')

    def test_the_snapshot_reads_this_checkout_whatever_git_exports(self):
        # Started inside a git process: its index and repository are not
        # this checkout's.
        repo, env, git = self.git_repo()
        self.write(repo, 'tests/a_test.py', '# a\n')
        git('add', '.')
        git('commit', '-q', '-m', 'start')
        with patch.object(redproof, 'ROOT', repo):
            with patch.dict(os.environ, dict(
                    env, GIT_INDEX_FILE=os.path.join(repo, 'no-such-index')),
                    clear=True):
                tracked = redproof.tracked_files()
            with patch.dict(os.environ, dict(
                    env, GIT_DIR=os.path.join(repo, 'no-such-repository')),
                    clear=True):
                try:
                    files = redproof.repo_files()
                except subprocess.CalledProcessError as e:
                    files = e.stderr.decode('utf-8', 'replace')
        self.assertEqual({'tests/a_test.py'}, tracked,
                         'the snapshot read another index')
        self.assertEqual(['tests/a_test.py'], files,
                         'the snapshot read another repository')

    def test_the_tool_runs_only_the_changed_set(self):
        # As a script, from a checkout of its own: the selection reaches
        # the run, a branch that changes no test file runs nothing, and a
        # base git cannot find is refused by name.
        repo, env, git = self.git_repo()
        os.makedirs(os.path.join(repo, 'tools'))
        shutil.copy2(os.path.join(ROOT, 'tools', 'redproof.py'),
                     os.path.join(repo, 'tools', 'redproof.py'))
        self.write(repo, 'tools/redproof_cases.py',
                   'class Case:\n'
                   '    def __init__(self, name, test):\n'
                   '        self.name, self.path, self.test = name, '
                   '"schwaby/x.py", test\n'
                   '        self.removed = None\n'
                   'CASES = [Case("a/one", "tests/a_test.py::T::t"),\n'
                   '         Case("b/two", "tests/b_test.py::T::t")]\n')
        self.write(repo, 'tests/a_test.py', 'def test_a():\n    pass\n')
        git('add', '.')
        git('commit', '-q', '-m', 'start')

        def run(*args):
            out = subprocess.run(
                [sys.executable, '-I', '-B',
                 os.path.join(repo, 'tools', 'redproof.py'), *args],
                cwd=repo, env=env, capture_output=True, text=True,
                timeout=120)
            return out.returncode, out.stdout + out.stderr

        code, out = run('--touching', 'main')
        self.assertIn('touching main: 0 entries reach a file the branch '
                      'changes', out,
                      'a branch with no test change chose entries')
        self.assertEqual(0, code, 'a branch with no test change did not '
                         'stop at the selection: ' + out)
        self.assertIn('redproof: 0 selected, 0 removed, 0 to run\n', out,
                      'an empty changed set printed no count to run')
        self.assertNotIn('baseline', out, 'a branch with no test change '
                         'ran entries')
        code, out = run('--touching', 'main', '--shard', '1/3')
        self.assertIn('redproof: 0 selected, 0 removed, 0 to run (shard '
                      '1/3)\nredproof: shard 1/3: 0 of 0 entries\n', out,
                      'an empty changed set printed no shard line')

        git('switch', '-q', '-c', 'branch')
        self.write(repo, 'tests/a_test.py', 'def test_a():\n    pass\n\n'
                   '# changed\n')
        # Past the selection to the first thing it can refuse without
        # running an entry.
        code, out = run('--touching', 'main', '--name', 'two')
        self.assertIn('touching main: 1 entry reaches a file the branch '
                      'changes', out,
                      'the changed set was not chosen')
        self.assertIn("no entry of the changed set has a name that contains "
                      "'two'", out,
                      'an entry outside the changed set was selected, or '
                      'the refusal did not say where it looked')
        self.assertEqual(2, code, out)

        # Judged by what the checkout holds: a helper named like a test.
        self.write(repo, 'tests/test_helpers.py', 'def helper():\n    pass\n')
        code, out = run('--touching', 'main', '--name', 'two')
        self.assertIn('a shared test helper changed (tests/test_helpers.py)',
                      out, 'the run did not judge a file by what the '
                      'checkout holds')

        code, out = run('--touching', 'no-such-base')
        self.assertEqual(2, code, out)
        self.assertIn('redproof: --touching no-such-base:', out)


class PoolSizeTest(unittest.TestCase):

    def args(self, jobs=None):
        return argparse.Namespace(jobs=jobs)

    def test_two_at_a_time_unless_told_to_use_every_core(self):
        # A host of 20, so neither the machine running the suite nor a
        # mutation counting every core can come out at two by accident.
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(redproof, 'host_cpus', lambda: 20), \
                patch.object(redproof, 'available_memory', lambda: None):
            self.assertEqual(2, redproof.pool_size(self.args(), 0)[0])
        with patch.dict(os.environ, {'REDPROOF_JOBS_ALL': '1'}), \
                patch.object(redproof, 'usable_cpus', lambda: 20), \
                patch.object(redproof, 'available_memory', lambda: None):
            size, reason = redproof.pool_size(self.args(), 0)
        self.assertEqual(20, size)
        self.assertIn('REDPROOF_JOBS_ALL=1', reason)

    def test_a_jobs_core_cap_bounds_the_count(self):
        # A run under a CPU quota may use fewer cores than
        # process_cpu_count() shows; SUITE_CORES says how many.
        for value, expected in (('4', 4), (' 4 ', 4),
                                # Above the host's count it does not raise it.
                                ('16', 7),
                                ('', 7), ('four', 7), ('4.5', 7), ('0', 7),
                                ('-2', 7)):
            with self.subTest(SUITE_CORES=value), \
                    patch.dict(os.environ, {'SUITE_CORES': value}), \
                    patch.object(redproof, 'host_cpus', lambda: 7):
                self.assertEqual(expected, redproof.usable_cpus())

    def test_the_pool_is_sized_to_the_core_cap_and_says_so(self):
        def pool(env):
            with patch.dict(os.environ, env), \
                    patch.object(redproof, 'host_cpus', lambda: 20), \
                    patch.object(redproof, 'available_memory', lambda: None):
                for name in ('SUITE_CORES', 'REDPROOF_JOBS_ALL'):
                    if name not in env:
                        os.environ.pop(name, None)
                return redproof.pool_size(self.args(), 0)

        size, reason = pool({'REDPROOF_JOBS_ALL': '1', 'SUITE_CORES': '4'})
        self.assertEqual(4, size)
        self.assertIn('4 usable cores, SUITE_CORES caps them', reason)
        # Two at a time is two only where two cores are allowed.
        size, reason = pool({'SUITE_CORES': '1'})
        self.assertEqual(1, size)
        self.assertIn('1 usable cores, SUITE_CORES caps them', reason)
        size, reason = pool({'REDPROOF_JOBS_ALL': '1', 'SUITE_CORES': '4c'})
        self.assertEqual(20, size)
        self.assertIn("SUITE_CORES='4c' not understood", reason)
        # Positive controls: unset, blank, equal to the host's count or above
        # it, the host decides and SUITE_CORES is not named.
        for env in ({'REDPROOF_JOBS_ALL': '1'},
                    {'REDPROOF_JOBS_ALL': '1', 'SUITE_CORES': '  '},
                    {'REDPROOF_JOBS_ALL': '1', 'SUITE_CORES': '20'},
                    {'REDPROOF_JOBS_ALL': '1', 'SUITE_CORES': '64'}):
            with self.subTest(env=env):
                size, reason = pool(env)
                self.assertEqual(20, size)
                self.assertNotIn('SUITE_CORES', reason)

    def test_memory_bounds_the_pool_and_says_so(self):
        with patch.dict(os.environ, {'REDPROOF_JOBS_ALL': '1'}), \
                patch.object(redproof, 'usable_cpus', lambda: 20), \
                patch.object(redproof, 'available_memory',
                             lambda: redproof.FLOOR + (6 << 30)):
            size, reason = redproof.pool_size(self.args(), 1 << 30)
        self.assertEqual(6, size)
        self.assertIn('memory holds 6', reason)

    def test_a_jobs_memory_cap_bounds_what_is_available(self):
        # /proc/meminfo describes the host; a run may be held to a memory
        # limit below it, and says so in SUITE_MEMORY_GB.
        host = 30 << 30
        for value, expected in (('8', 8 << 30),
                                ('0.5', 1 << 29),
                                # A cap above what the host has free does
                                # not raise it.
                                ('64', host),
                                ('', host), ('eight', host), ('0', host),
                                ('-8', host), ('nan', host), ('inf', host),
                                ('1e400', host)):
            with self.subTest(SUITE_MEMORY_GB=value), \
                    patch.dict(os.environ, {'SUITE_MEMORY_GB': value}), \
                    patch.object(redproof, 'meminfo_available',
                                 lambda: host):
                self.assertEqual(expected, redproof.available_memory())
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(redproof, 'meminfo_available', lambda: host):
            self.assertEqual(host, redproof.available_memory())
        # Where the kernel says nothing, the cap is all there is.
        with patch.dict(os.environ, {'SUITE_MEMORY_GB': '8'}), \
                patch.object(redproof, 'meminfo_available', lambda: None):
            self.assertEqual(8 << 30, redproof.available_memory())
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(redproof, 'meminfo_available', lambda: None):
            self.assertIsNone(redproof.available_memory())

    def test_the_pool_is_sized_to_a_memory_cap_and_says_so(self):
        # 8 GiB less the floor holds six runs of a GiB, though the host has
        # room for 28.
        with patch.dict(os.environ, {'REDPROOF_JOBS_ALL': '1',
                                     'SUITE_MEMORY_GB': '8'}), \
                patch.object(redproof, 'usable_cpus', lambda: 20), \
                patch.object(redproof, 'meminfo_available',
                             lambda: 30 << 30):
            size, reason = redproof.pool_size(self.args(), 1 << 30)
        self.assertEqual(6, size)
        self.assertIn('8.0 GiB available, SUITE_MEMORY_GB caps it', reason)
        # Positive control: without the cap, or with one above what the host
        # has free, the host's memory decides, and the cap is not named.
        for env in ({}, {'SUITE_MEMORY_GB': '64'}):
            with self.subTest(env=env), \
                    patch.dict(os.environ, dict(env, REDPROOF_JOBS_ALL='1')), \
                    patch.object(redproof, 'usable_cpus', lambda: 20), \
                    patch.object(redproof, 'meminfo_available',
                                 lambda: 30 << 30):
                if not env:
                    os.environ.pop('SUITE_MEMORY_GB', None)
                size, reason = redproof.pool_size(self.args(), 1 << 30)
                self.assertEqual(20, size)
                self.assertNotIn('SUITE_MEMORY_GB', reason)

    def test_a_cap_it_cannot_read_is_named_not_dropped(self):
        # systemd spells a budget 8G; read as no budget at all, the pool
        # would be sized from the host with nothing said.
        with patch.dict(os.environ, {'REDPROOF_JOBS_ALL': '1',
                                     'SUITE_MEMORY_GB': '8G'}), \
                patch.object(redproof, 'usable_cpus', lambda: 20), \
                patch.object(redproof, 'meminfo_available',
                             lambda: 30 << 30):
            size, reason = redproof.pool_size(self.args(), 1 << 30)
        self.assertEqual(20, size)
        self.assertIn("SUITE_MEMORY_GB='8G' not understood", reason)

    def test_the_memory_watch_reads_the_host_not_the_cap(self):
        # Under a cap below the host's free memory, available_memory() is
        # the cap, every time: a watch on it would report a constant as the
        # lowest the run saw.
        host = [30 << 30]

        class Snapshot:
            def __init__(self, path):
                self.path = path
        with patch.dict(os.environ, {'SUITE_MEMORY_GB': '8'}), \
                patch.object(redproof, 'Snapshot', Snapshot), \
                patch.object(redproof, 'meminfo_available',
                             lambda: host[0]):
            run = redproof.Run(argparse.Namespace())
            self.addCleanup(shutil.rmtree, run.tmp, True)
            self.assertEqual(30 << 30, run.lowest_available)
            host[0] = 20 << 30
            run.note_memory(types.SimpleNamespace(peak=0))
            self.assertEqual(20 << 30, run.lowest_available)
            # Positive control: the cap is in force, so the two differ.
            self.assertEqual(8 << 30, redproof.available_memory())


class CasesTest(unittest.TestCase):

    def test_every_entry_has_a_unique_name_and_an_expectation(self):
        names = [c.name for c in cases.CASES]
        self.assertGreater(len(names), 600)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual([], [c.name for c in cases.CASES if not c.expect])
