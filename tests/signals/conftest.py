"""Signal-sending tests run only as a queued job (the engineering standard's
§3.20): a test here that signals a process is skipped, saying why, unless
REDPROOF_JOBS_ALL is set, as it is in every queued job of this repository.
The light check runs no tests, so it needs no sanction of its own. A gate run
(SUITE_GATE set) that would skip them fails instead of passing: a skip passes
silently, and a gate that lost its sanction on the way would otherwise pass
without running them.

Kept in this directory's conftest, not tests/conftest.py, which every test
reads, so that a change here leaves every other test's recorded reads alone.
"""

import os
from pathlib import Path

import pytest

#: Modules here whose every test signals processes, by file name.
SIGNALLING = frozenset({'worker_test.py'})
REASON = ('it signals processes: the engineering standard\'s §3.20 runs it '
          'only as a queued job (REDPROOF_JOBS_ALL set)')


def sanctioned(env=os.environ):
    """Whether this run may signal processes: a queued job's."""
    return bool(env.get('REDPROOF_JOBS_ALL'))


def guarded(items, env=os.environ):
    """The items skipped here, unless this run is sanctioned. In a gate run
    without the sanction, a usage error instead of any skip."""
    if sanctioned(env):
        return []
    out = [i for i in items if Path(str(i.path)).name in SIGNALLING]
    if out and env.get('SUITE_GATE'):
        raise pytest.UsageError(
                '%d tests that signal processes, in a gate run without '
                'REDPROOF_JOBS_ALL: the engineering standard\'s §3.20 runs '
                'them only as a queued job, and a skip would pass them'
                % len(out))
    for i in out:
        i.add_marker(pytest.mark.skip(reason=REASON))
    return out


def pytest_collection_modifyitems(config, items):
    guarded(items)
