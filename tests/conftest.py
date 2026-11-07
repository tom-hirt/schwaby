import functools
import os

import pytest


def usable_cpus():
    '''The cores this process may run on: its affinity mask, not the
    machine's count, and logical cores rather than physical ones.'''
    if hasattr(os, 'process_cpu_count'):
        return os.process_cpu_count() or 1
    if hasattr(os, 'sched_getaffinity'):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def job_cores():
    '''The core cap, SUITE_CORES: how many cores a run may use, or None when
    it is unset or not a positive whole number. A run under a CPU quota (a
    cgroup or container limit) may use fewer cores than the counts above
    show; whatever starts the run sets this in its environment to say how
    many.'''
    try:
        cores = int(os.environ.get('SUITE_CORES', ''))
    except ValueError:
        return None
    return cores if cores > 0 else None


# `-n auto` asks xdist how many workers to start, and xdist answers with
# psutil's PHYSICAL core count when psutil is installed -- which it always is
# here, since the login flow needs it. On a machine with two threads per core
# that is half of what the suite can use, and it ignores an affinity mask, so
# a run confined to four cores of a large machine starts too many. The
# PYTEST_XDIST_AUTO_NUM_WORKERS override still wins, as it does in xdist.
# Inside a job with a core cap, SUITE_CORES bounds it.
@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config):
    if os.environ.get('PYTEST_XDIST_AUTO_NUM_WORKERS'):
        return None
    cores, cap = usable_cpus(), job_cores()
    return min(cores, cap) if cap is not None else cores


# A fixed callback port made two runs of the suite on one machine fail each
# other, and nothing in one run can see it coming back. The source scan in
# packaging_test finds a port written into a URL; this finds one however it
# was written, by checking the port each callback server is started for.
# Checked where the flow builds its server process, which every way of
# reaching the flow passes through -- a function imported by name included,
# which a patch on the module attribute would miss.
@pytest.fixture(autouse=True)
def login_flows_take_a_port_a_helper_handed_out(monkeypatch):
    guard_login_flow_ports(monkeypatch)


def guard_login_flow_ports(monkeypatch):
    '''What the fixture above does at the start of every test. A function of
    its own, so a test can start "a later test" inside itself.'''
    # Imported here: suite_config_test loads this file on its own, outside
    # the package, where a relative import at the top fails.
    import multiprocess
    from .utils import HANDED_OUT_PORTS

    # Per test: a number handed out to an earlier test and written down in
    # a later one is still written down.
    HANDED_OUT_PORTS.clear()
    real = multiprocess.Process.__init__

    @functools.wraps(real)
    def checked(process, *args, **kwargs):
        target = kwargs.get('target')
        server_args = kwargs.get('args') or ()
        if (getattr(target, '__name__', None)
                == '__run_client_from_login_flow_server'
                and len(server_args) > 1):
            port = server_args[1]
            # 443 is what a URL without a port means, and it is never bound.
            if port != 443 and port not in HANDED_OUT_PORTS:
                pytest.fail('a login flow was given port {}, which neither '
                            'free_callback_port nor bound_callback_socket '
                            'handed out: a port written down is one two runs '
                            'of the suite share'.format(port))
        real(process, *args, **kwargs)

    monkeypatch.setattr(multiprocess.Process, '__init__', checked)
