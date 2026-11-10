'''The red-proof harness's worker processes: a dead or stopped one is
reported, not waited for, what it left running is killed, and the rest of
the work goes on.

These tests send signals, to processes they start and record, so they live
in a directory of their own: its conftest skips them outside a queued job,
as the engineering standard's rule for signal-sending tests says.'''

import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest

from ..redproof_tool_test import case, redproof


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

# This script's pid and session, taken before any worker exists.
SCENARIO, SESSION = os.getpid(), os.getsid(0)

# Each worker the pool forks is recorded as it starts, by the fork that
# starts it, so a worker signals only pids the scenario started and wrote
# down, never ones it went looking for.
real_fork = os.fork

def recording_fork():
    pid = real_fork()
    if pid:
        with open(MARK + 'workers', 'a') as f:
            f.write('%d\\n' % pid)
    return pid

os.fork = recording_fork

# The recorded workers, other than this one, that are still the scenario's
# children in its session, each with a pidfd opened before it is checked: a
# recorded pid whose worker is gone may belong to another process now, and
# then the check fails and nothing is signalled.
def siblings():
    with open(MARK + 'workers') as f:
        recorded = {int(line) for line in f if line.strip()}
    out = []
    for pid in sorted(recorded - {os.getpid()}):
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            continue
        try:
            with open('/proc/%d/stat' % pid) as f:
                fields = f.read().rsplit(')', 1)[1].split()
            if int(fields[1]) == SCENARIO and int(fields[3]) == SESSION:
                out.append(fd)
                continue
        except (OSError, ValueError, IndexError):
            pass
        os.close(fd)
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
        for fd in found:
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                # Gone already: nothing to signal, which is the point.
                pass
            os.close(fd)
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
    # Each pool's own workers only: the last pool's are gone.
    open(MARK + 'workers', 'w').close()
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
                    os.path.dirname(__file__), '..', '..', 'tools',
                    'redproof.py')],
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
        self.assertEqual('SKIP', result[1])
        self.assertEqual('', result[4], 'an entry that never ran printed '
                         'another run\'s time split')
