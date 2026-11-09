'''Tests for schwaby.testing, and for the transport= every entry point takes.

Every test that builds a client runs inside block_network(), so a request
that misses the transport fails it instead of reaching Schwab.
'''

import asyncio
import errno
import inspect
import json
import os
import socket
import sys
import tempfile
import time
import unittest
import urllib.parse
from unittest.mock import patch

import httpx2

import schwaby
import schwaby.testing as st
from schwaby import auth

# Where pytest finds the fixture: a name in the test module. Assigned, not
# imported, so pyflakes does not take the parameter for a shadowed import.
no_network = st.no_network

from .utils import bound_callback_socket, no_duplicates


API_KEY = 'a' * 32
APP_SECRET = 'b' * 16
CALLBACK_URL = 'https://127.0.0.1:8182'

# As auth_test skips its login flows.
_skip_on_macos_runner = unittest.skipIf(
        sys.platform == 'darwin' and os.environ.get('CI') == 'true',
        'the callback server does not answer on loopback on macOS CI runners')
QUOTES = '/marketdata/v1/AAPL/quotes'


class Base(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, 'token.json')
        self.endpoint = st.FakeTokenEndpoint()
        self.endpoint.on_api(lambda request: {'path': request.url.path})
        self.blocked = st.block_network()
        self.blocked.__enter__()
        self.addCleanup(self.blocked.__exit__, None, None, None)

    def quote(self, client):
        result = client.get_quote('AAPL')
        if inspect.isawaitable(result):
            async def wait():
                return await result
            return asyncio.run(wait())
        return result

    def paths(self):
        return [request.url.path for request in self.endpoint.requests]


class TransportTest(Base):
    '''Each entry point sends everything through the transport it is given,
    the token endpoint's requests included.'''

    @no_duplicates
    def test_a_token_file_client_refreshes_and_calls_through_it(self):
        for asyncio_ in (False, True):
            with self.subTest(asyncio=asyncio_):
                self.endpoint = st.FakeTokenEndpoint()
                self.endpoint.on_api(lambda r: {'path': r.url.path})
                self.endpoint.answer_refresh(st.token(access_token='NEW'))
                st.write_token_file(self.path, expires_in=-60)
                client = auth.client_from_token_file(
                        self.path, API_KEY, APP_SECRET, asyncio=asyncio_,
                        transport=self.endpoint)
                self.assertEqual({'path': QUOTES},
                                 self.quote(client).json())
                self.assertEqual(['/v1/oauth/token', QUOTES], self.paths())
                self.assertEqual(
                        'Bearer NEW',
                        self.endpoint.requests[1].headers['Authorization'])
                with open(self.path) as f:
                    self.assertEqual('NEW',
                                     json.load(f)['token']['access_token'])

    @no_duplicates
    def test_an_access_functions_client_uses_it(self):
        written = st.write_token_file(self.path)
        client = auth.client_from_access_functions(
                API_KEY, APP_SECRET, lambda: written, lambda *a, **k: None,
                transport=self.endpoint)
        self.quote(client)
        self.assertEqual([QUOTES], self.paths())

    @no_duplicates
    def test_a_login_exchanges_its_code_through_it(self):
        for asyncio_ in (False, True):
            with self.subTest(asyncio=asyncio_):
                self.endpoint = st.FakeTokenEndpoint()
                self.endpoint.on_api(lambda r: {'path': r.url.path})
                self.endpoint.answer_exchange(st.token(access_token='FIRST'))
                context = auth.get_auth_context(API_KEY, CALLBACK_URL)
                client = auth.client_from_received_url(
                        API_KEY, APP_SECRET, context,
                        st.received_url(context, code='c0de'),
                        auth.token_file_writer(self.path), asyncio=asyncio_,
                        transport=self.endpoint)
                self.quote(client)
                self.assertEqual(['/v1/oauth/token', QUOTES], self.paths())
                self.assertEqual(1, self.endpoint.posts('authorization_code'))
                self.assertIn(b'code=c0de', self.endpoint.requests[0].content)

    @no_duplicates
    def test_a_manual_login_uses_it(self):
        self.endpoint.answer_exchange(st.token())
        context = auth.get_auth_context(API_KEY, CALLBACK_URL)
        with patch('schwaby.auth.get_auth_context', return_value=context), \
                patch('schwaby.auth.input',
                      return_value=st.received_url(context)), \
                patch('schwaby.auth.print'):
            client = auth.client_from_manual_flow(
                    API_KEY, APP_SECRET, CALLBACK_URL, self.path,
                    transport=self.endpoint)
        self.quote(client)
        self.assertEqual(['/v1/oauth/token', QUOTES], self.paths())

    @no_duplicates
    @_skip_on_macos_runner
    def test_the_login_flow_exchanges_its_code_through_it(self):
        # The browser is told to open the login, and goes straight to the
        # callback with a code, as Schwab's redirect does.
        import requests
        listening = bound_callback_socket()
        self.addCleanup(listening.close)
        callback = 'https://127.0.0.1:{}/callback'.format(
                listening.getsockname()[1])

        def open_login(url):
            state = urllib.parse.parse_qs(
                    urllib.parse.urlparse(url).query)['state'][0]
            requests.get(callback + '?code=c0de&state=' + state,
                         verify=False)
        self.endpoint.answer_exchange(st.token())
        with patch('schwaby.auth.webbrowser.get') as browser:
            browser.return_value.open.side_effect = open_login
            client = auth.client_from_login_flow(
                    API_KEY, APP_SECRET, callback, self.path,
                    interactive=False, callback_socket=listening,
                    transport=self.endpoint)
        self.quote(client)
        self.assertEqual(['/v1/oauth/token', QUOTES], self.paths())
        self.assertIn(b'code=c0de', self.endpoint.requests[0].content)

    @no_duplicates
    def test_easy_client_passes_it_on(self):
        st.write_token_file(self.path)
        client = auth.easy_client(API_KEY, APP_SECRET, CALLBACK_URL,
                                  self.path, transport=self.endpoint)
        self.quote(client)
        self.assertEqual([QUOTES], self.paths())
        os.remove(self.path)
        for flow, notebook in (('client_from_manual_flow', True),
                               ('client_from_login_flow', False)):
            with self.subTest(flow=flow), \
                    patch('schwaby.auth.' + flow) as called, \
                    patch('schwaby.auth.__running_in_notebook',
                          return_value=notebook):
                auth.easy_client(API_KEY, APP_SECRET, CALLBACK_URL,
                                 self.path, transport=self.endpoint)
                self.assertIs(self.endpoint,
                              called.call_args.kwargs['transport'])

    @no_duplicates
    def test_the_environments_proxies_are_not_used_with_one(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://10.0.0.1:1'}):
            written = st.write_token_file(self.path)
            client = auth.client_from_access_functions(
                    API_KEY, APP_SECRET, lambda: written,
                    lambda *a, **k: None, transport=self.endpoint)
            self.assertFalse(client.session.trust_env)
            self.assertEqual({}, dict(client.session._mounts))
            self.quote(client)
        self.assertEqual([QUOTES], self.paths())

    @no_duplicates
    def test_without_one_nothing_changes(self):
        written = st.write_token_file(self.path)
        client = auth.client_from_access_functions(
                API_KEY, APP_SECRET, lambda: written, lambda *a, **k: None)
        self.assertTrue(client.session.trust_env)
        self.assertNotIsInstance(client.session._transport,
                                 httpx2.MockTransport)
        # The block stands in for the network a real client would reach.
        with self.assertRaises(st.NetworkBlocked):
            self.quote(client)
        self.blocked.attempts.clear()

    @no_duplicates
    def test_it_is_keyword_only(self):
        for name in ('client_from_login_flow', 'client_from_token_file',
                     'client_from_manual_flow', 'client_from_access_functions',
                     'client_from_received_url', 'easy_client'):
            with self.subTest(name=name):
                parameter = inspect.signature(
                        getattr(auth, name)).parameters['transport']
                self.assertIs(inspect.Parameter.KEYWORD_ONLY, parameter.kind)
                self.assertIsNone(parameter.default)


class FakeTokenEndpointTest(Base):
    '''The fake answers as told, and the library reads its answers as it
    reads Schwab's.'''

    def client(self, asyncio_=False, expires_in=-60):
        st.write_token_file(self.path, expires_in=expires_in)
        return auth.client_from_token_file(
                self.path, API_KEY, APP_SECRET, asyncio=asyncio_,
                transport=self.endpoint)

    def refresh_fails(self, answer, asyncio_=False):
        self.endpoint.answer_refresh(answer)
        with self.assertRaises(Exception) as cm:
            self.quote(self.client(asyncio_))
        return cm.exception

    @no_duplicates
    def test_each_refusal_reaches_the_library_as_schwabs_would(self):
        K = schwaby.TokenErrorKind
        for answer, kind in (
                (st.refusal(400, error='invalid_grant'), K.DEAD_TOKEN),
                (st.refusal(401, body=None), K.REFUSED_NO_BODY),
                (st.refusal(401, error='invalid_client'), K.APP_REFUSED),
                (st.rate_limited(retry_after=5), K.RATE_LIMITED),
                (st.answer(302, headers={'Location': '/x'}, token=True),
                 K.TRANSIENT)):
            for asyncio_ in (False, True):
                with self.subTest(answer=answer, asyncio=asyncio_):
                    error = self.refresh_fails(answer, asyncio_)
                    self.assertIsInstance(error, schwaby.TokenRefreshError)
                    self.assertIs(kind, error.kind)

    @no_duplicates
    def test_what_is_not_a_refusal_stays_as_it_was(self):
        self.assertIsInstance(self.refresh_fails(st.answer(503)),
                              httpx2.HTTPStatusError)
        self.assertIsInstance(
                self.refresh_fails(st.answer(200, body=b'<html>')),
                ValueError)
        down = httpx2.ConnectError('down')
        self.assertIs(down, self.refresh_fails(st.raises(down)))

    @no_duplicates
    def test_answers_are_given_in_order_and_once(self):
        self.endpoint.answer_refresh(st.token(access_token='ONE'))
        self.endpoint.answer_refresh(st.refusal(400, error='invalid_grant'))
        client = self.client()
        self.quote(client)
        client.session.token['expires_at'] = int(time.time()) - 60
        with self.assertRaises(schwaby.TokenRefreshError):
            self.quote(client)
        client.session.token['expires_at'] = int(time.time()) - 60
        with self.assertRaises(st.UnexpectedRequest) as cm:
            self.quote(client)
        self.assertIn('answer_refresh()', str(cm.exception))
        self.assertEqual('/v1/oauth/token', cm.exception.request.url.path)
        self.assertEqual(3, self.endpoint.posts('refresh_token'))
        self.assertEqual(0, self.endpoint.posts('authorization_code'))

    @no_duplicates
    def test_a_repeated_answer_answers_every_one_after_the_queue(self):
        self.endpoint.answer_refresh(st.token(access_token='FIRST'))
        self.endpoint.answer_refresh(st.token(access_token='AGAIN'),
                                     repeat=True)
        client = self.client()
        for expected in ('FIRST', 'AGAIN', 'AGAIN'):
            client.session.token['expires_at'] = int(time.time()) - 60
            self.quote(client)
            self.assertEqual(expected, client.session.token['access_token'])

    @no_duplicates
    def test_the_grants_are_kept_apart(self):
        self.endpoint.answer_exchange(st.token())
        with self.assertRaises(st.UnexpectedRequest):
            self.quote(self.client())
        self.assertEqual(1, self.endpoint.posts())

    @no_duplicates
    def test_an_async_handler_answers_an_asyncio_client(self):
        async def handler(request):
            await asyncio.sleep(0)
            return {'async': True}
        self.endpoint.on_api(handler)
        response = self.quote(self.client(asyncio_=True, expires_in=1800))
        self.assertEqual({'async': True}, response.json())
        # A synchronous client cannot wait for it, and says so.
        with self.assertRaises(TypeError):
            self.quote(self.client(expires_in=1800))

    @no_duplicates
    def test_an_api_request_with_no_handler_is_refused(self):
        endpoint = st.FakeTokenEndpoint()
        written = st.write_token_file(self.path)
        client = auth.client_from_access_functions(
                API_KEY, APP_SECRET, lambda: written, lambda *a, **k: None,
                transport=endpoint)
        with self.assertRaises(st.UnexpectedRequest) as cm:
            client.get_quote('AAPL')
        self.assertIn('GET ' + QUOTES, str(cm.exception))

    @no_duplicates
    def test_the_api_handler_may_answer_in_each_form(self):
        for result, status in (
                (httpx2.Response(201, json={}), 201),
                ({'a': 1}, 200),
                (st.answer(404, {'errors': []}), 404),
                (st.rate_limited(retry_after=2), 429)):
            with self.subTest(result=result):
                self.endpoint.on_api(lambda request: result)
                response = self.quote(self.client(expires_in=1800))
                self.assertEqual(status, response.status_code)
        self.endpoint.on_api(lambda request: 'text')
        with self.assertRaises(TypeError):
            self.quote(self.client(expires_in=1800))


class HelpersTest(unittest.TestCase):

    @no_duplicates
    def test_a_token_is_shaped_as_schwab_sends_one(self):
        one, two = st.token(), st.token(expires_in=60, extra='x')
        self.assertEqual({'expires_in', 'token_type', 'scope',
                          'refresh_token', 'access_token', 'id_token'},
                         set(one))
        self.assertEqual(('Bearer', 'api', 1800),
                         (one['token_type'], one['scope'], one['expires_in']))
        self.assertNotEqual(one['access_token'], two['access_token'])
        self.assertEqual((60, 'x'), (two['expires_in'], two['extra']))

    @no_duplicates
    def test_the_answers_carry_what_they_say(self):
        request = httpx2.Request('POST', auth.TOKEN_ENDPOINT)
        for answer, status, body, headers in (
                (st.refusal(400), 400, b'{"error": "invalid_request"}', {}),
                (st.refusal(400, 'invalid_grant', description='d'), 400,
                 b'{"error": "invalid_grant", "error_description": "d"}', {}),
                (st.refusal(401, body=None), 401, b'', {}),
                (st.answer(500, 'oops'), 500, b'oops', {}),
                (st.rate_limited(), 429, b'', {}),
                (st.rate_limited(retry_after=7), 429, b'',
                 {'retry-after': '7'})):
            with self.subTest(answer=answer):
                response = answer.respond(request)
                self.assertEqual(status, response.status_code)
                self.assertEqual(body, response.content)
                for name, value in headers.items():
                    self.assertEqual(value, response.headers[name])
        self.assertEqual('Bearer', json.loads(st.answer(
                302, token=True).respond(request).content)['token_type'])
        with self.assertRaises(ValueError):
            st.answer(200, {'a': 1}, token=True)
        with self.assertRaises(TypeError):
            st.raises('not an exception')

    @no_duplicates
    def test_a_token_file_reads_back_as_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'token.json')
            written = st.write_token_file(path, expires_in=900,
                                          created_days_ago=6.5)
            self.assertEqual(0o600, os.stat(path).st_mode & 0o777)
            client = auth.client_from_token_file(path, API_KEY, APP_SECRET)
            self.assertAlmostEqual(6.5 * 86400, client.token_age(), delta=5)
            self.assertAlmostEqual(
                    time.time() + 900, client.session.token['expires_at'],
                    delta=5)
            self.assertEqual(written['token']['access_token'],
                             client.session.token['access_token'])
            mine = st.token(access_token='MINE')
            st.write_token_file(path, token=mine)
            with open(path) as f:
                self.assertEqual('MINE',
                                 json.load(f)['token']['access_token'])
            self.assertNotIn('expires_at', mine)
            for kwargs, error in (({'created_days_ago': -1}, ValueError),
                                  ({'expires_in': float('nan')}, ValueError),
                                  ({'expires_in': '60'}, TypeError),
                                  ({'created_days_ago': True}, TypeError)):
                with self.subTest(**kwargs):
                    with self.assertRaises(error):
                        st.write_token_file(path, **kwargs)

    @no_duplicates
    def test_a_received_url_carries_the_contexts_state(self):
        context = auth.get_auth_context(API_KEY, CALLBACK_URL, state='s1')
        self.assertEqual(CALLBACK_URL + '?code=c%400&session=fake-session'
                         '&state=s1', st.received_url(context, 'c@0'))
        self.assertTrue(st.received_url(context, state='other').endswith(
                '&state=other'))
        query = auth.AuthContext('https://x/cb?a=1', None, 's')
        self.assertTrue(st.received_url(query).startswith(
                'https://x/cb?a=1&code='))

    @no_duplicates
    def test_a_failing_writer_fails_every_time(self):
        writer = st.failing_writer()
        for _ in range(2):
            with self.assertRaises(OSError) as cm:
                writer({'t': 1})
            self.assertEqual(errno.ENOSPC, cm.exception.errno)
        self.assertEqual([{'t': 1}, {'t': 1}], writer.calls)
        mine = RuntimeError('mine')
        with self.assertRaises(RuntimeError) as cm:
            st.failing_writer(mine)({})
        self.assertIs(mine, cm.exception)

    @no_duplicates
    def test_a_login_whose_writer_fails_says_so(self):
        endpoint = st.FakeTokenEndpoint()
        endpoint.answer_exchange(st.token())
        context = auth.get_auth_context(API_KEY, CALLBACK_URL)
        writer = st.failing_writer()
        with st.block_network(), self.assertRaises(OSError):
            auth.client_from_received_url(
                    API_KEY, APP_SECRET, context, st.received_url(context),
                    writer, transport=endpoint)
        self.assertEqual(1, len(writer.calls))


class Reached(RuntimeError):
    """The real network function was called: block_network let it past."""


class BlockNetworkTest(unittest.TestCase):

    def refused(self, blocked, reach):
        with self.assertRaises(st.NetworkBlocked):
            reach()
        self.assertTrue(blocked.attempts)
        blocked.attempts.clear()

    def stub_the_network(self):
        # Under the block, so a guard that lets a call past reaches this and
        # fails the test, instead of the network. Not an OSError, which
        # httpx2 would turn into a ConnectError.
        def reached(*args, **kwargs):
            raise Reached('the network was reached')
        for target, name in ((socket, 'getaddrinfo'),
                             (socket, 'gethostbyname'),
                             (socket, 'gethostbyname_ex'),
                             (socket.socket, 'connect'),
                             (socket.socket, 'connect_ex')):
            stub = patch.object(target, name, reached)
            stub.start()
            self.addCleanup(stub.stop)

    @no_duplicates
    def test_a_connection_out_is_refused_every_way(self):
        self.stub_the_network()
        async def async_get():
            async with httpx2.AsyncClient() as client:
                await client.get('https://api.schwabapi.com/')

        def connect(address):
            with socket.socket() as s:
                s.connect(address)

        def connect_ex(address):
            with socket.socket() as s:
                s.connect_ex(address)
        with st.block_network() as blocked:
            for name, reach in (
                    ('httpx2', lambda: httpx2.get('https://api.schwabapi.com')),
                    ('httpx2 async', lambda: asyncio.run(async_get())),
                    ('a lookup', lambda: socket.getaddrinfo('example.com', 80)),
                    ('gethostbyname',
                     lambda: socket.gethostbyname('example.com')),
                    ('gethostbyname_ex',
                     lambda: socket.gethostbyname_ex('example.com')),
                    ('an address', lambda: connect(('192.0.2.1', 80))),
                    ('connect_ex', lambda: connect_ex(('192.0.2.1', 80))),
                    ('a connection', lambda: socket.create_connection(
                        ('192.0.2.1', 80), timeout=1))):
                with self.subTest(name=name):
                    self.refused(blocked, reach)

    @no_duplicates
    def test_this_machine_is_let_through(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            port = server.getsockname()[1]
            with st.block_network() as blocked:
                for host in ('127.0.0.1', 'localhost'):
                    with self.subTest(host=host):
                        socket.create_connection((host, port), 5).close()
                        server.accept()[0].close()
            self.assertEqual([], blocked.attempts)

    @no_duplicates
    def test_the_proxies_are_cleared_and_put_back(self):
        self.stub_the_network()
        saved = {'HTTPS_PROXY': 'http://10.0.0.1:1', 'wss_proxy': 'w',
                 'NO_PROXY': 'n'}
        with patch.dict(os.environ, saved):
            with st.block_network():
                self.assertNotIn('HTTPS_PROXY', os.environ)
                self.assertNotIn('wss_proxy', os.environ)
                self.assertEqual('n', os.environ['NO_PROXY'])
                os.environ['HTTP_PROXY'] = 'set inside'
            for name, value in saved.items():
                self.assertEqual(value, os.environ[name])
            self.assertNotIn('HTTP_PROXY', os.environ)

    @no_duplicates
    def test_everything_is_put_back_once_the_last_block_ends(self):
        before = (socket.getaddrinfo, socket.socket.connect,
                  socket.socket.connect_ex)
        with st.block_network():
            with st.block_network():
                pass
            self.assertIsNot(before[0], socket.getaddrinfo)
        self.assertEqual(before, (socket.getaddrinfo, socket.socket.connect,
                                  socket.socket.connect_ex))

    @no_duplicates
    def test_a_swallowed_attempt_is_raised_when_the_block_ends(self):
        self.stub_the_network()
        with self.assertRaises(st.NetworkBlocked) as cm:
            with st.block_network():
                try:
                    socket.getaddrinfo('example.com', 80)
                except Exception:
                    pass
        self.assertIn('the block ended without the refusal',
                      str(cm.exception))
        self.assertIn("'example.com'", str(cm.exception))
        # An error leaving the block is not replaced by it.
        with self.assertRaises(KeyError):
            with st.block_network():
                try:
                    socket.getaddrinfo('example.com', 80)
                except Exception:
                    pass
                raise KeyError('mine')

    # A guard can outlive its block: a reference taken inside it, or a patch
    # started inside and stopped after, which puts the guard back. None of
    # these reaches the network, mutated or not: each address is loopback,
    # or the call meets a stub.

    def keep_the_socket_functions(self):
        # For the tests after: what these leave in place is put back.
        for target, name in ((socket, 'getaddrinfo'),
                             (socket.socket, 'connect')):
            self.addCleanup(setattr, target, name, getattr(target, name))

    @staticmethod
    def reached(*args, **kwargs):
        raise Reached('the network was reached')

    @no_duplicates
    def test_a_guard_kept_past_its_block_refuses_nothing(self):
        self.keep_the_socket_functions()
        with patch.object(socket, 'getaddrinfo', self.reached):
            with st.block_network():
                held = socket.getaddrinfo
            # Handed on to what it replaced, the stub, and not refused.
            with self.assertRaises(Reached):
                held('192.0.2.1', 80)

    @no_duplicates
    def test_a_guard_put_back_hands_on_to_what_it_replaced(self):
        # A stub started before the block is still there below the guard a
        # patch put back.
        self.keep_the_socket_functions()
        with patch.object(socket.socket, 'connect', self.reached):
            with st.block_network():
                inner = patch.object(socket.socket, 'connect',
                                     socket.socket.connect)
                inner.start()
            inner.stop()
            with socket.socket() as s:
                with self.assertRaises(Reached,
                                       msg='the stub below was skipped'):
                    s.connect(('127.0.0.1', 9))
                # Nor does it refuse, outside a block.
                with self.assertRaises(Reached):
                    s.connect(('192.0.2.1', 80))

    @no_duplicates
    def test_guards_left_one_after_another_do_not_pile_up(self):
        # As a pytest test leaves one when its own patch of connect is
        # undone after its block ends: in a large suite, once per test.
        self.keep_the_socket_functions()

        def depth():
            n, function = 0, socket.socket.connect
            while hasattr(function, '_block_network_replaced'):
                n, function = n + 1, function._block_network_replaced
            return n
        for _ in range(3):
            with st.block_network():
                inner = patch.object(socket.socket, 'connect', self.reached)
                inner.start()
            inner.stop()
        self.assertEqual(1, depth(), 'the guards left piled up')
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            with st.block_network():
                with socket.socket() as s:
                    s.settimeout(5)
                    s.connect(server.getsockname())
                    server.accept()[0].close()

    @no_duplicates
    def test_a_block_over_a_leftover_guard_works(self):
        self.keep_the_socket_functions()
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen(2)
            port = server.getsockname()[1]
            with st.block_network():
                inner = patch.object(socket.socket, 'connect',
                                     socket.socket.connect)
                inner.start()
            inner.stop()
            # A plain block over the guard left on connect.
            with st.block_network() as blocked:
                with socket.socket() as s:
                    s.settimeout(5)
                    s.connect(('127.0.0.1', port))
                    server.accept()[0].close()
            self.assertEqual([], blocked.attempts)
            # And one over a patch wrapping a guard left on getaddrinfo.
            with st.block_network():
                inner = patch.object(socket, 'getaddrinfo',
                                     socket.getaddrinfo)
                inner.start()
            inner.stop()
            with patch.object(socket, 'getaddrinfo',
                              wraps=socket.getaddrinfo):
                with st.block_network():
                    self.assertTrue(socket.getaddrinfo('127.0.0.1', port))

    @no_duplicates
    def test_it_decorates_a_test(self):
        self.stub_the_network()
        @st.block_network()
        def plain():
            socket.getaddrinfo('example.com', 80)

        @st.block_network()
        async def coroutine():
            socket.getaddrinfo('example.com', 80)
        with self.assertRaises(st.NetworkBlocked):
            plain()
        with self.assertRaises(st.NetworkBlocked):
            asyncio.run(coroutine())
        # Each call is its own block.
        with self.assertRaises(st.NetworkBlocked):
            plain()


def test_the_fixture_blocks_the_test(no_network):
    # A pytest fixture, so a pytest test; the suite runs under pytest.
    try:
        socket.getaddrinfo('example.com', 80)
    except st.NetworkBlocked:
        pass
    else:
        raise AssertionError('the fixture let a lookup through')
    assert no_network.attempts == ["a lookup of 'example.com'"]
    no_network.attempts.clear()
