'''The guard on signal-sending tests: outside a queued job they are skipped,
saying why, and a gate run without the sanction fails rather than skip them.
Each case runs pytest on the signalling module in a child, so nothing here
sends a signal: unsanctioned, the module's tests never run.'''

import os
import subprocess
import sys
import unittest

from ..utils import no_duplicates

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def run_unsanctioned(**extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ('REDPROOF_JOBS_ALL', 'SUITE_GATE')}
    env.update(extra)
    return subprocess.run(
            [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '-rs',
             '-q', '-o', 'addopts=', 'tests/signals/worker_test.py'],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


@unittest.skipIf(os.name == 'nt', 'the harness the module tests is POSIX only')
class GuardTest(unittest.TestCase):

    @no_duplicates
    def test_outside_a_queued_job_they_are_skipped_saying_why(self):
        run = run_unsanctioned()
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertNotIn(' passed', run.stdout,
                         'a signal-sending test ran outside a queued job')
        self.assertIn('skipped', run.stdout)
        self.assertIn('§3.20', run.stdout,
                      "the skip's reason does not name the rule")

    @no_duplicates
    def test_a_gate_run_without_the_sanction_fails(self):
        run = run_unsanctioned(SUITE_GATE='1')
        self.assertNotEqual(0, run.returncode,
                            'a gate run passed by skipping signal-sending '
                            'tests')
        self.assertIn('REDPROOF_JOBS_ALL', run.stdout + run.stderr)
