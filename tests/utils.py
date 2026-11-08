from colorama import Fore, Back, Style, init
from unittest.mock import AsyncMock, MagicMock

import asyncio
import contextlib
import difflib
import inspect
import httpx2
import json
import os
import socket


#: Every port free_callback_port and bound_callback_socket have handed out in
#: this process. A login flow given a loopback port that is not here was
#: given one written down, and tests/conftest.py fails the test saying so: a
#: scan of the source for port numbers misses `port = 6969` a line above the
#: URL that uses it.
HANDED_OUT_PORTS = set()


def free_callback_port():
    '''A loopback port nothing is listening on, for a login-flow test's
    callback URL: a fixed one (6969) made any two runs of the suite on one
    machine fail each other. The port is free when this returns and the flow
    binds it a moment later, so another process could take it between the
    two; a test that is not about the flow binding its port serves on
    bound_callback_socket() instead, which leaves no such gap. With a port of
    its own, a login-flow test needs no worker or run to itself: two flows
    side by side do not take each other's server for their own.'''
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    HANDED_OUT_PORTS.add(port)
    return port


@contextlib.contextmanager
def naming_a_port_taken_between_pick_and_bind(test, port):
    '''For the tests of a login flow binding a port by number, which have to
    pick a free one and let it go first. If the flow's server exits and the
    port turns out to be held by something else, that is a collision in the
    gap between the pick and the bind, and the test fails saying so rather
    than as a failure of the flow. A port nobody holds lets the error through
    as it was.'''
    from schwaby.auth import RedirectServerExitedError
    try:
        yield
    except RedirectServerExitedError:
        if _a_child_holds(port):
            # The flow's own server, outliving the flow: a defect of the
            # flow, which must not read as a collision.
            raise
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # A port left in TIME_WAIT by the test's own requests is not
            # held. Not on Windows, where the option lets a bind take a port
            # someone is listening on.
            if os.name != 'nt':
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('127.0.0.1', port))
            except OSError:
                test.fail('port taken between pick and bind: something else '
                          'held port {} when the callback server tried to '
                          'bind it, so this is a collision, not a failure '
                          'of the login flow'.format(port))
        raise


def _a_child_holds(port):
    '''Whether a process this one started holds ``port``. A child that
    cannot be read is taken as not holding it.'''
    import psutil
    try:
        children = psutil.Process().children(recursive=True)
    except psutil.Error:
        return False
    for child in children:
        try:
            # net_connections from psutil 6.0, connections before it.
            connections = getattr(child, 'net_connections',
                                  getattr(child, 'connections', None))(
                                          kind='tcp')
        except psutil.Error:
            continue
        if any(c.laddr and c.laddr.port == port for c in connections):
            return True
    return False


def bound_callback_socket():
    '''A loopback socket bound to a port of its own, held from the moment the
    port is chosen, for a login flow to serve on as its ``callback_socket``.
    Bound and not listening: the flow puts it in the listening state.'''
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    HANDED_OUT_PORTS.add(s.getsockname()[1])
    return s


def bound_datagram_socket():
    '''A loopback UDP socket on a port of its own, handed out like
    bound_callback_socket()'s: for a test that offers a login flow a socket
    of the wrong type, on the port its callback URL names. Bound to a port
    the host chooses for UDP, rather than a TCP port's number, which a host
    may refuse for UDP (Windows: WinError 10013).'''
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 0))
    HANDED_OUT_PORTS.add(s.getsockname()[1])
    return s


# Resolved from this file rather than the working directory, so the tests
# that load it run from anywhere, not only from the repo root.
TESTDATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'testdata')

class AnyStringWith(str):
    '''
    Utility for checking whether a function was called with the given string as 
    a substring.
    '''
    def __eq__(self, other):
        return self in other

def account_preferences():
    with open(os.path.join(TESTDATA, 'preferences.json'), 'r') as f:
        return json.load(f)


def real_order():
    return {
        'session': 'NORMAL',
        'duration': 'DAY',
        'orderType': 'MARKET',
        'complexOrderStrategyType': 'NONE',
        'quantity': 1.0,
        'filledQuantity': 1.0,
        'remainingQuantity': 0.0,
        'destinationLinkName': 'AUTO',
        'price': 58.41,
        'orderLegCollection': [
            {
                'orderLegType': 'EQUITY',
                'legId': 1,
                'instrument': {
                    'assetType': 'EQUITY',
                    'cusip': '126650100',
                    'symbol': 'CVS'
                },
                'instruction': 'BUY',
                'positionEffect': 'OPENING',
                'quantity': 1.0
            }
        ],
        'orderStrategyType': 'SINGLE',
        'orderId': 100001,
        'cancelable': False,
        'editable': False,
        'status': 'FILLED',
        'enteredTime': '2020-03-30T15:36:12+0000',
        'closeTime': '2020-03-30T15:36:12+0000',
        'tag': 'API_TDAM:App',
        'accountId': 100000,
        'orderActivityCollection': [
            {
                'activityType': 'EXECUTION',
                'executionType': 'FILL',
                'quantity': 1.0,
                'orderRemainingQuantity': 0.0,
                'executionLegs': [
                    {
                        'legId': 1,
                        'quantity': 1.0,
                        'mismarkedQuantity': 0.0,
                        'price': 58.1853,
                        'time': '2020-03-30T15:36:12+0000'
                    }
                ]
            }
        ]
    }

class AsyncResync:
    """
    Re-synchronizes every async function on a given object.
    NOTE: Every method runs on a new loop
    """

    class _AsyncResyncMethod:
        def __init__(self, func):
            self.func = func
        def __call__(self, *args, **kwargs):
            coroutine = self.func(*args, **kwargs)
            loop = asyncio.new_event_loop()
            retval = loop.run_until_complete(coroutine)
            loop.close()
            return retval

    def __getattr__(self, attr):
        retval = super().__getattribute__(attr)
        if inspect.iscoroutinefunction(retval):
            return self._AsyncResyncMethod(retval)
        return retval
    __getattribute__ = __getattr__


class ResyncProxy:
    """
    Proxies the underlying class, replacing coroutine methods
    with an auto-executing one
    """
    def __init__(self, cls):
        self.cls = cls

    def __call__(self, *args, **kwargs):
        class DynamicResync(AsyncResync, self.cls):
            """
            Forces a mixin of the underlying class and the AsyncResync
            class
            """
            pass
        return DynamicResync(*args, **kwargs)

    def __getattr__(self, key):
        if key == 'cls':
            return super().__getattribute__(key)
        return getattr(self.cls, key)


# TODO: Figure out if httpx2 supports cleaner response mocking, because this is 
#       pretty janky.
class MockResponse(httpx2.Response):
    def __init__(self, json, status_code, headers=None):
        resp_args = {
            'status_code': status_code,
            'json': json,
        }
        if headers:
            resp_args['headers'] = headers
        httpx2.Response.__init__(
                self, **resp_args)


# TODO: This was necessary back before AsyncMock was a thing, but is it still 
#       necessary now?
class AsyncMagicMock:
    """
    Simple mock that returns an AsyncMock instance for every attribute. Useful
    for mocking async libraries.
    """
    def __init__(self):
        self.__attr_cache = {}

    def __getattr__(self, key):
        attr_cache = super().__getattribute__('_AsyncMagicMock__attr_cache')
        if key == '_AsyncMagicMock__attr_cache': return attr_cache

        try:
            return super().__getattribute__(key)
        except AttributeError:
            if key not in attr_cache:
                attr_cache[key] = AsyncMock()
            return attr_cache[key]

    def __setattr__(self, key, val):
        if key == '_AsyncMagicMock__attr_cache':
            return super().__setattr__(key, val)
        attr_cache = super().__getattribute__('_AsyncMagicMock__attr_cache')
        attr_cache[key] = val

    def __hasattr__(self, key):
        attr_cache = super().__getattribute__('_AsyncMagicMock__attr_cache')
        return attr_cache.has_key(key)

    def reset_mock(self):
        self.__attr_cache.clear()

def has_diff(old, new):
    '''False when old and new are the same JSON. When they differ it raises
    AssertionError carrying the diff, so the difference is in the failure
    itself, not only in the test's captured output with a bare "True is not
    false". Every caller asserts it False.'''
    old_out = json.dumps(old, indent=4, sort_keys=True).splitlines()
    new_out = json.dumps(new, indent=4, sort_keys=True).splitlines()
    diff = list(difflib.ndiff(old_out, new_out))
    _, has_diff = color_diff(diff)

    if has_diff:
        raise AssertionError('the JSON differs:\n' + '\n'.join(
            line for line in diff if not line.startswith('?')))
    return has_diff


def color_diff(diff):
    has_diff = False
    output = []
    for line in diff:
        if line.startswith('+'):
            output.append(Fore.GREEN + line + Fore.RESET)
            has_diff = True
        elif line.startswith('-'):
            output.append(Fore.RED + line + Fore.RESET)
            has_diff = True
        elif line.startswith('^'):
            output.append(Fore.BLUE + line + Fore.RESET)
            has_diff = True
        else:
            output.append(line)
    return output, has_diff


__NO_DUPLICATES_DEFINED_NAMES = set()


def no_duplicates(f):
    name = f.__qualname__
    if name in __NO_DUPLICATES_DEFINED_NAMES:
        raise AttributeError('duplicate definition of {}'.format(name))
    __NO_DUPLICATES_DEFINED_NAMES.add(name)
    return f


class MockOAuthClient(MagicMock):
    def __call__(self, *args, **kwargs):
        if 'update_token' in kwargs:
            assert not inspect.iscoroutinefunction(kwargs['update_token']), (
                'a sync OAuth client was handed a coroutine token writer')
        return MagicMock.__call__(self, *args, **kwargs)


class MockAsyncOAuthClient(MagicMock):
    def __call__(self, *args, **kwargs):
        if 'update_token' in kwargs:
            assert inspect.iscoroutinefunction(kwargs['update_token']), (
                'an async OAuth client was handed a plain token writer')
        return MagicMock.__call__(self, *args, **kwargs)
