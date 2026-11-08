'''Runs the same tests many times and reports which failed, and how often.

    python -I -B tools/repeat.py --runs 40 tests/auth_test.py::ClientFromLoginFlowTest
    python -I -B tools/repeat.py --runs 40 --load tests/auth_test.py

A test that fails one run in forty is a defect, not bad luck: something in it
or under it depends on timing or on what else the machine is doing. This is
how such a failure is measured, and how a fix for one is shown to work: the
same count before and after, on the same tests, under the same load. A
measurement like this is a proof: one-off evidence for one diagnosis, never
a gate or a check a change has to pass.

Each run is a fresh `python -m pytest` in one process, from the repository
root, with the interpreter this runs under. The runs are one after another,
never side by side, so each failure belongs to one run and the only other
thing on the machine is what --load puts there.

--load keeps every usable core busy for the whole measurement with spinning
processes, one per core, started before the first run and stopped after the
last. A failure that appears only under load is still a failure; that is the
condition a busy machine creates on its own.

Every run is bounded (RUN_LIMIT) and a run that passes it is counted, by
name, as having run out of time. Exit status 0 only when every run passed.
'''

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_LIMIT = 900

# Loaded into each pytest run with -p: it writes the node id of every failing
# report, collection errors and failing subTests included, to the file
# REPEAT_FAILURES names. Asked of pytest rather than read from its summary,
# whose lines a subTest label, a parametrize id or a test's own captured
# output can each make ambiguous.
PLUGIN = '''import os


def _note(report):
    if report.failed:
        with open(os.environ['REPEAT_FAILURES'], 'a', encoding='utf-8') as f:
            f.write(report.nodeid + '\\n')


def pytest_runtest_logreport(report):
    _note(report)


def pytest_collectreport(report):
    _note(report)
'''
PLUGIN_NAME = '_repeat_failures'

# The pytest run under way, so a signal can stop it too: it is in a session
# of its own, so neither a terminal's Ctrl-C nor the load's cleanup reaches it.
_running = []


def usable_cpus():
    if hasattr(os, 'process_cpu_count'):
        return os.process_cpu_count() or 1
    if hasattr(os, 'sched_getaffinity'):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def failures(path):
    '''The node ids the plugin wrote: every failing test once.'''
    try:
        with open(path, encoding='utf-8') as f:
            return sorted(set(line.rstrip('\n') for line in f if line.strip()))
    except FileNotFoundError:
        return []


def run_once(nodes, workdir):
    '''(passed, failed node ids, seconds, peak bytes, why it has no status).
    workdir holds the plugin and this run's list of failures.'''
    found = os.path.join(workdir, 'failures.txt')
    if os.path.exists(found):
        os.remove(found)
    argv = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
            '-p', PLUGIN_NAME] + nodes
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', REPEAT_FAILURES=found,
               PYTHONPATH=os.pathsep.join(
                   [workdir] + ([os.environ['PYTHONPATH']]
                                if os.environ.get('PYTHONPATH') else [])))
    started = time.monotonic()
    proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    _running[:] = [proc]
    expired = threading.Event()

    def expire():
        expired.set()
        os.killpg(proc.pid, signal.SIGKILL)

    timer = threading.Timer(RUN_LIMIT, expire)
    timer.start()
    try:
        output = proc.stdout.read().decode('utf-8', 'replace')
        _, status, usage = os.wait4(proc.pid, 0)
    finally:
        timer.cancel()
        # Reaped: its pid may be reused, so a signal must not be sent to it.
        _running.clear()
    proc.returncode = os.waitstatus_to_exitcode(status)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    seconds = time.monotonic() - started
    peak = usage.ru_maxrss * 1024
    if expired.is_set():
        return False, nodes, seconds, peak, 'ran past %d s' % RUN_LIMIT
    if proc.returncode < 0:
        return False, nodes, seconds, peak, 'killed by %s' % (
            signal.Signals(-proc.returncode).name)
    failed = failures(found)
    if proc.returncode != 0 and not failed:
        # Failed without naming a test: a collection error, a crash. Counted,
        # and the end of the output says what it was.
        tail = ' | '.join(line for line in output.splitlines()[-3:] if line)
        return False, [], seconds, peak, 'exit %d: %s' % (proc.returncode,
                                                          tail)
    return proc.returncode == 0, failed, seconds, peak, None


def start_load(n):
    return [subprocess.Popen([sys.executable, '-c', 'while True: pass'],
                             stdin=subprocess.DEVNULL, start_new_session=True)
            for _ in range(n)]


def stop_load(procs):
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--runs', type=int, default=20)
    parser.add_argument('--load', action='store_true',
                        help='keep every usable core busy throughout')
    parser.add_argument('nodes', nargs='+', help='pytest node ids or paths')
    args = parser.parse_args(argv)

    load = []

    def stop(signum, frame):
        for proc in list(_running):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stop_load(load)
        raise SystemExit(128 + signum)

    # Before the load starts: a signal while it starts must stop what has.
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, stop)

    workdir = tempfile.mkdtemp(prefix='schwaby-repeat-')
    with open(os.path.join(workdir, PLUGIN_NAME + '.py'), 'w',
              encoding='utf-8') as f:
        f.write(PLUGIN)
    if args.load:
        for _ in range(usable_cpus()):
            load.extend(start_load(1))
    print('repeat: %d runs of %s on Python %s, %s' % (
        args.runs, ' '.join(args.nodes), sys.version.split()[0],
        '%d cores kept busy' % len(load) if load else 'no added load'),
        flush=True)

    counts = {}
    failed_runs = 0
    peak = 0
    try:
        for i in range(1, args.runs + 1):
            ok, failed, seconds, run_peak, why = run_once(args.nodes, workdir)
            peak = max(peak, run_peak)
            if ok:
                print('[%d/%d] passed  (%.1f s)' % (i, args.runs, seconds),
                      flush=True)
                continue
            failed_runs += 1
            for node in failed or ['(no test named)']:
                counts[node] = counts.get(node, 0) + 1
            print('[%d/%d] FAILED  (%.1f s) %s%s' % (
                i, args.runs, seconds, ', '.join(failed),
                ('  [%s]' % why) if why else ''), flush=True)
    finally:
        stop_load(load)
        shutil.rmtree(workdir, ignore_errors=True)

    print('repeat: largest run %d MiB' % (peak >> 20))
    print('repeat: %d of %d runs failed%s' % (
        failed_runs, args.runs, ''.join(
            '\n  %3d  %s' % (n, node)
            for node, n in sorted(counts.items(), key=lambda kv: -kv[1]))),
        flush=True)
    return 1 if failed_runs else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
