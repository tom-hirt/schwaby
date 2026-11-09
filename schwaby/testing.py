'''Support for testing code that uses schwaby, without Schwab's servers.

Nothing here touches the network. A client built with ``transport=`` sends
every request, the token endpoint's included, to the transport given, and
:class:`FakeTokenEndpoint` is one that answers refreshes and logins as the
test says. :func:`block_network` makes sure nothing else gets out.

This module is imported only by tests: ``import schwaby.testing as st``.
'''

import errno
import functools
import inspect
import ipaddress
import itertools
import json
import math
import os
import socket
import threading
import time
import urllib.parse

import httpx2

from .auth import TOKEN_ENDPOINT, token_file_writer
from .utils import _rebuild_exception


__all__ = [
    'FakeTokenEndpoint', 'NetworkBlocked', 'UnexpectedRequest', 'answer',
    'block_network', 'failing_writer', 'no_network', 'raises',
    'rate_limited', 'received_url', 'refusal', 'token', 'write_token_file',
]


################################################################################
# Blocking the network


class NetworkBlocked(AssertionError):
    '''A connection, or a name lookup, was attempted inside
    :func:`block_network`. An ``AssertionError``, so a test reports it as a
    failure, and not an ``OSError``, so nothing that handles a network error
    takes it for one.'''


# Every active block_network, and the guards they installed.
_block_lock = threading.Lock()
_active_blocks = []
_installed = []
_saved_environ = {}

# Names that are this machine, so a test's own server still works.
_LOCAL_NAMES = frozenset({None, '', 'localhost', 'localhost.'})


def _is_local(host):
    if isinstance(host, bytes):
        try:
            host = host.decode('ascii')
        except UnicodeDecodeError:
            return False
    if host in _LOCAL_NAMES:
        return True
    if not isinstance(host, str):
        return False
    try:
        address = ipaddress.ip_address(host.split('%', 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _refuse(what):
    message = ('block_network() refused {}: a test reached for the '
               'network'.format(what))
    with _block_lock:
        for block in _active_blocks:
            block.attempts.append(what)
    raise NetworkBlocked(message)


def _lookup_guard(replaced):
    def guarded(host, *args, **kwargs):
        if _active_blocks and not _is_local(host):
            _refuse('a lookup of {!r}'.format(host))
        return replaced(host, *args, **kwargs)
    return guarded


def _check_address(sock, address):
    family = getattr(socket, 'AF_UNIX', None)
    if family is not None and sock.family == family:
        return
    host = address[0] if isinstance(address, tuple) and address else address
    if not _is_local(host):
        _refuse('a connection to {!r}'.format(host))


def _connect_guard(replaced):
    def guarded(self, address):
        if _active_blocks:
            _check_address(self, address)
        return replaced(self, address)
    return guarded


# What a block replaces, and how each guard is made. Each block makes its
# own guards, each holding what it replaced, and hands a call it lets
# through to that. A guard can outlive its block -- a reference taken
# inside it, or a patch started inside and stopped after, which puts the
# guard back -- and it then lets everything through, to what it replaced.
_GUARDS = (
    (socket, 'getaddrinfo', _lookup_guard),
    (socket, 'gethostbyname', _lookup_guard),
    (socket, 'gethostbyname_ex', _lookup_guard),
    (socket.socket, 'connect', _connect_guard),
    (socket.socket, 'connect_ex', _connect_guard),
)


def _is_proxy_variable(name):
    name = name.lower()
    return name.endswith('_proxy') and name != 'no_proxy'


def _install():
    _installed[:] = []
    for target, name, make in _GUARDS:
        replaced = getattr(target, name)
        guard = make(replaced)
        guard.__name__ = guard.__qualname__ = name
        setattr(target, name, guard)
        _installed.append((target, name, guard, replaced))
    _saved_environ.clear()
    _saved_environ.update({name: value for name, value in os.environ.items()
                           if _is_proxy_variable(name)})
    for name in _saved_environ:
        del os.environ[name]


def _uninstall():
    for target, name, guard, replaced in _installed:
        # Something patched over it inside the block puts it back when it
        # stops, and it then lets everything through.
        if getattr(target, name) is guard:
            setattr(target, name, replaced)
    _installed[:] = []
    for name in [n for n in os.environ if _is_proxy_variable(n)]:
        del os.environ[name]
    os.environ.update(_saved_environ)


class block_network:
    '''Refuses connections and name lookups that leave this machine, as
    httpx2 and websockets make them, for as long as it is active, and
    clears the environment's proxy settings. The note below says what it
    does not stop.

    ::

        with st.block_network():
            ...

    It also decorates a test, a coroutine function's included, and is a
    pytest fixture as :func:`no_network`. An attempt raises
    :class:`NetworkBlocked`. If the code under test catches that and carries
    on, leaving the block raises it again, so an attempt that was swallowed
    still fails the test. ``attempts`` lists what was refused; a test that
    means to provoke one clears it before the block ends.

    Connections to ``localhost`` and loopback addresses, and Unix sockets,
    are let through, so a test's own server still works.

    .. note:: It replaces ``socket.getaddrinfo``, ``gethostbyname``,
       ``gethostbyname_ex``, and ``socket.socket.connect`` and
       ``connect_ex``, for the whole process, every thread included, and
       puts them back when the last active block ends. What reaches the
       network some other way is not stopped: a datagram sent with
       ``sendto``, a resolver of its own, uvloop, which resolves and
       connects in libuv, a C extension that opens sockets itself, or, on
       Windows, asyncio's own connections, made without
       ``socket.connect``. An ``asyncio`` client connecting to an address
       rather than a name may raise the refusal inside an
       ``ExceptionGroup``.
    '''

    def __init__(self):
        self.attempts = []

    def __enter__(self):
        with _block_lock:
            if not _active_blocks:
                _install()
            _active_blocks.append(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        with _block_lock:
            _active_blocks.remove(self)
            if not _active_blocks:
                _uninstall()
        if exc_type is None and self.attempts:
            raise NetworkBlocked(
                    'block_network() refused {} attempt(s) to reach the '
                    'network, the first {}, and the block ended without the '
                    'refusal: the code under test caught it, or the test '
                    'failed on it already'.format(
                        len(self.attempts), self.attempts[0]))
        return False

    def __call__(self, func):
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def blocked(*args, **kwargs):
                with block_network():
                    return await func(*args, **kwargs)
        else:
            @functools.wraps(func)
            def blocked(*args, **kwargs):
                with block_network():
                    return func(*args, **kwargs)
        return blocked


try:
    import pytest
except ImportError:  # pragma: no cover
    def no_network():
        '''A pytest fixture, when pytest is installed.'''
        raise RuntimeError('no_network is a pytest fixture, and pytest is '
                           'not installed')
else:
    @pytest.fixture
    def no_network():
        '''A pytest fixture that runs the test inside :func:`block_network`
        and gives it the block, whose ``attempts`` lists what was refused.
        Imported into a test module or a ``conftest.py`` to be used::

            from schwaby.testing import no_network
        '''
        with block_network() as blocked:
            yield blocked


################################################################################
# Answers


class UnexpectedRequest(AssertionError):
    '''A :class:`FakeTokenEndpoint` was sent a request it had no answer for.
    ``request`` is that request.'''

    def __init__(self, message, request):
        super().__init__(message)
        self.request = request

    def __reduce__(self):
        # As SchwabError's: rebuilt without __init__, which takes the
        # request as well as the message.
        return (_rebuild_exception, (type(self), self.args, self.__dict__))


class _Answer:
    '''One answer to one request: a response, or an exception raised in its
    place.'''

    def __init__(self, status=200, body=None, headers=None, exception=None):
        self.status = status
        self.headers = dict(headers or {})
        self.exception = exception
        if body is None:
            self.content = b''
        elif isinstance(body, bytes):
            self.content = body
        elif isinstance(body, str):
            self.content = body.encode('utf-8')
        else:
            self.content = json.dumps(body).encode('utf-8')
            self.headers.setdefault('Content-Type', 'application/json')

    def __repr__(self):
        if self.exception is not None:
            return 'raises({!r})'.format(self.exception)
        return 'answer({}, {!r})'.format(self.status, self.content[:60])

    def respond(self, request):
        if self.exception is not None:
            raise self.exception
        return httpx2.Response(self.status, headers=self.headers,
                               content=self.content, request=request)


def _as_answer(value):
    if isinstance(value, _Answer):
        return value
    if isinstance(value, dict):
        return _Answer(200, value)
    raise TypeError('an answer is a token, from token(), or one of '
                    'answer(), refusal(), rate_limited() or raises(), not '
                    '{}'.format(type(value).__name__))


_serial = itertools.count(1)


def token(expires_in=1800, *, access_token=None, refresh_token=None,
          **fields):
    '''A token as Schwab's token endpoint sends it, a ``dict``: given to
    :meth:`FakeTokenEndpoint.answer_refresh` or
    :meth:`~FakeTokenEndpoint.answer_exchange`, it is answered with status
    200. The tokens are made up and differ from one call to the next unless
    given. Any other ``fields`` are added to it.'''
    n = next(_serial)
    body = {
        'expires_in': expires_in,
        'token_type': 'Bearer',
        'scope': 'api',
        'refresh_token': (refresh_token if refresh_token is not None
                          else 'fake-refresh-token-{}'.format(n)),
        'access_token': (access_token if access_token is not None
                         else 'fake-access-token-{}'.format(n)),
        'id_token': 'fake-id-token-{}'.format(n),
    }
    body.update(fields)
    return body


# For the functions whose parameter of the same name hides it.
_token = token


_NOT_GIVEN = object()


def refusal(status, error='invalid_request', *, description=None,
            body=_NOT_GIVEN):
    '''The token endpoint refusing, with ``{"error": error}`` as the body,
    and ``error_description`` too when ``description`` is given. ``body``
    replaces that: ``None`` sends no body at all, as Schwab sometimes
    does.'''
    if body is _NOT_GIVEN:
        body = {'error': error}
        if description is not None:
            body['error_description'] = description
    return _Answer(status, body)


def answer(status, body=None, *, headers=None, token=False):
    '''Any answer: ``body`` is a ``dict`` or ``list`` sent as JSON, ``bytes``
    or a ``str`` sent as they are, or ``None`` for no body. ``token=True``
    sends a token as the body, at whatever ``status`` is.'''
    if token:
        if body is not None:
            raise ValueError('give a body or token=True, not both')
        body = _token()
    return _Answer(status, body, headers)


def rate_limited(retry_after=None, *, body=None):
    '''A 429, with a ``Retry-After`` header of ``retry_after`` seconds when
    given.'''
    headers = {}
    if retry_after is not None:
        headers['Retry-After'] = str(retry_after)
    return _Answer(429, body, headers)


def raises(exception):
    '''An answer that raises ``exception`` where a response would come back,
    as a dropped connection does: ``raises(httpx2.ConnectError('down'))``.'''
    if not isinstance(exception, BaseException):
        raise TypeError('raises() takes an exception')
    return _Answer(exception=exception)


################################################################################
# The fake endpoint


_TOKEN_URL = urllib.parse.urlsplit(TOKEN_ENDPOINT)


class FakeTokenEndpoint(httpx2.MockTransport):
    '''An ``httpx2`` transport that answers the token endpoint as a test says,
    and hands every other request to a handler. Given as ``transport=`` to a
    ``client_from_*`` function or :func:`~schwaby.auth.easy_client`, it
    serves both the client and, for a login, the exchange of its code::

        endpoint = st.FakeTokenEndpoint()
        endpoint.answer_refresh(st.refusal(400, error='invalid_grant'))
        client = schwaby.auth.client_from_token_file(
                path, api_key, app_secret, transport=endpoint)

    Answers are given in order, and each is used once. One given with
    ``repeat=True`` answers every request that finds no other answer
    waiting, and a later one replaces it. A
    request with no answer waiting raises :class:`UnexpectedRequest`, which
    reaches the code under test as the exception it sees. It serves a
    synchronous and an ``asyncio`` client alike, and is safe across threads.
    '''

    def __init__(self):
        super().__init__(self._handle)
        self._lock = threading.Lock()
        self._answers = {'refresh_token': [], 'authorization_code': []}
        self._repeat = {}
        self._api = None
        #: Every request it was sent, in order: ``httpx2.Request`` objects,
        #: with their bodies read.
        self.requests = []

    def _queue(self, grant_type, value, repeat):
        value = _as_answer(value)
        with self._lock:
            if repeat:
                self._repeat[grant_type] = value
            else:
                self._answers[grant_type].append(value)

    def answer_refresh(self, value, *, repeat=False):
        '''Answers the next refresh, a POST of ``grant_type=refresh_token``,
        with ``value``: a token, from :func:`token`, or an answer from
        :func:`answer`, :func:`refusal`, :func:`rate_limited` or
        :func:`raises`.'''
        self._queue('refresh_token', value, repeat)

    def answer_exchange(self, value, *, repeat=False):
        '''Answers the next login's exchange of its code, a POST of
        ``grant_type=authorization_code``, as :meth:`answer_refresh`
        answers a refresh.'''
        self._queue('authorization_code', value, repeat)

    def on_api(self, handler):
        '''Hands every request that is not to the token endpoint to
        ``handler``, a function that takes the ``httpx2.Request`` and returns
        an ``httpx2.Response``, a ``dict`` to send as JSON with status 200, or
        an answer from :func:`answer` and the rest. For an ``asyncio``
        client it may be a coroutine function. Without one, such a request
        raises :class:`UnexpectedRequest`.'''
        self._api = handler

    def posts(self, grant_type=None):
        '''How many POSTs the token endpoint was sent, of ``grant_type`` when
        given: ``'refresh_token'`` or ``'authorization_code'``.'''
        with self._lock:
            requests = list(self.requests)
        return sum(1 for request in requests
                   if _is_token_post(request) and (
                       grant_type is None
                       or _grant_type(request) == grant_type))

    def _handle(self, request):
        with self._lock:
            self.requests.append(request)
        if _is_token_post(request):
            grant_type = _grant_type(request)
            with self._lock:
                queue = self._answers.get(grant_type)
                if queue:
                    value = queue.pop(0)
                else:
                    value = self._repeat.get(grant_type)
            if value is None:
                raise UnexpectedRequest(
                        'FakeTokenEndpoint had no answer for a {} request to '
                        'the token endpoint; give one with {}'.format(
                            grant_type, {
                                'refresh_token': 'answer_refresh()',
                                'authorization_code': 'answer_exchange()',
                            }.get(grant_type, 'neither answer_refresh() nor '
                                  'answer_exchange(): it is not a grant '
                                  'this library sends')), request)
            return value.respond(request)
        if self._api is None:
            raise UnexpectedRequest(
                    'FakeTokenEndpoint had no handler for {} {}; give one '
                    'with on_api()'.format(request.method, request.url.path),
                    request)
        result = self._api(request)
        if inspect.isawaitable(result):
            # httpx2 waits for this on an asyncio client, and refuses it on
            # a synchronous one.
            async def finish():
                return _api_response(await result, request)
            return finish()
        return _api_response(result, request)


def _api_response(result, request):
    if isinstance(result, httpx2.Response):
        return result
    return _as_answer(result).respond(request)


def _is_token_post(request):
    url = request.url
    return (request.method == 'POST' and url.scheme == _TOKEN_URL.scheme
            and url.host == _TOKEN_URL.hostname
            and url.path == _TOKEN_URL.path)


def _grant_type(request):
    try:
        form = urllib.parse.parse_qs(request.content.decode('utf-8'))
    except UnicodeDecodeError:
        return None
    values = form.get('grant_type')
    return values[0] if values else None


################################################################################
# Token files and logins


def write_token_file(path, *, expires_in=1800, created_days_ago=0.0,
                     token=None):
    '''Writes a token file at ``path`` as this library writes one, for
    :func:`~schwaby.auth.client_from_token_file` to read.

    :param expires_in: seconds the access token has left from now; negative
                       for one that has already expired.
    :param created_days_ago: how long ago the login that made it was, in
                             days, for :meth:`~schwaby.client.Client.token_age`
                             and ``max_token_age``.
    :param token: the token to write, from :func:`token` by default.
    :return: what was written, as a ``dict``.
    '''
    for name, value in (('expires_in', expires_in),
                        ('created_days_ago', created_days_ago)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError('{} is a number of seconds or days'.format(name))
        if not math.isfinite(value):
            raise ValueError('{} must be finite'.format(name))
    if created_days_ago < 0:
        raise ValueError('created_days_ago cannot be in the future')
    now = time.time()
    body = dict(token if token is not None else _token())
    body['expires_at'] = int(now + expires_in)
    written = {'creation_timestamp': int(now - created_days_ago * 86400),
               'token': body}
    token_file_writer(path)(written)
    return written


def received_url(auth_context, code='fake-authorization-code', *,
                 session='fake-session', state=None):
    '''The URL Schwab redirects a login to, for an
    :class:`~schwaby.auth.AuthContext` from
    :func:`~schwaby.auth.get_auth_context`: its callback URL with ``code``,
    ``session`` and the context's ``state``, or ``state`` when given, for a
    test of one that does not match.'''
    query = urllib.parse.urlencode({
        'code': code, 'session': session,
        'state': auth_context.state if state is None else state})
    url = auth_context.callback_url
    return url + ('&' if '?' in url else '?') + query


class _FailingWriter:
    def __init__(self, exception):
        self.exception = exception
        self.calls = []

    def __call__(self, token, *args, **kwargs):
        self.calls.append(token)
        raise self.exception


def failing_writer(exception=None):
    '''A ``token_write_func`` that raises ``exception``, a full disk's
    ``OSError`` by default, every time it is called. ``calls`` lists the
    tokens it was given.'''
    if exception is None:
        exception = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
    return _FailingWriter(exception)
