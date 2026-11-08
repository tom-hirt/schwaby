'''No secret reaches any log, at any level, by default.

A whole session runs against local stand-ins for Schwab -- an HTTP server for
the REST and token endpoints and a websocket server for the streamer -- so the
real websockets, httpx2, httpcore and authlib code runs with its own logging,
rather than a mock that logs nothing. Every logger is at DEBUG and websockets'
elision of long frames is turned off, as its documented
``WEBSOCKETS_MAX_LOG_SIZE`` setting does. Everything logged is searched for
each secret in every form it could take.

This test uses nothing but public API and the root logger, so it runs as it
is against an earlier release with the same public API, and fails on one
where the streamer's LOGIN request logs the access token, the bug report
carries it, or websockets logs it whole.

The servers bind ephemeral loopback ports, so two runs of the suite cannot
collide here.
'''
import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import random
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx2
import websockets
import websockets.frames

from schwaby import auth, debug
from schwaby.streaming import StreamClient

from .utils import no_duplicates


# Fresh values for every test. Redaction registers secrets for the life of the
# process, so a value an earlier test registered would be masked here whether
# or not this test's path registers it, and a leak would pass unseen.
SECRETS = {}


def new_secrets():
    # Independent values, none containing another: masking one secret must
    # not hide part of a second, or the second goes unseen while still
    # leaking. Each starts with a letter so none is all digits but the
    # account number, which Schwab sends as one.
    def fresh(prefix):
        return prefix + ''.join(random.choice('abcdefghjkmnpqrstuvwxyz'
                                              '23456789') for _ in range(14))
    SECRETS.clear()
    for name in ('app key', 'app secret', 'stale access token',
                 'stale refresh token', 'refreshed access token',
                 'refreshed refresh token', 'authorization code',
                 'exchanged access token', 'exchanged refresh token',
                 'id token'):
        SECRETS[name] = fresh(chr(ord('A') + len(SECRETS)))
    SECRETS['account number'] = str(random.randrange(10 ** 7, 10 ** 8))
    # Named only by the stream, never by a REST response, so nothing has
    # registered it when websockets logs the frame that brings it.
    while True:
        SECRETS['stream-only account number'] = str(
            random.randrange(10 ** 7, 10 ** 8))
        values = list(SECRETS.values())
        if not any(a != b and a in b for a in values for b in values):
            break


# A fragment this long of any secret but an account number counts as the
# secret: elision can cut one, and what survives is still a leak. Account
# numbers are too short to search by fragment without matching unrelated
# digits.
FRAGMENT = 10
CALLBACK = 'https://127.0.0.1:8182'

# The stand-in streamer's own websockets logger records the frames it
# receives -- Schwab's side of the connection, not this library's. It is the
# one source excluded, by name, and the test checks that the client side of
# the same frames is present.
STAND_IN_LOGGERS = ('websockets.server',)


def forms(value):
    '''Every spelling a secret could reach a log in.'''
    return {value, urllib.parse.quote(value, safe=''),
            urllib.parse.quote_plus(value)}


def needles():
    out = {name: forms(value) for name, value in SECRETS.items()}
    basic = base64.b64encode('{}:{}'.format(
        SECRETS['app key'], SECRETS['app secret']).encode()).decode()
    out['app key and secret as Basic auth'] = {basic}
    for name, value in SECRETS.items():
        if not value.isdigit():
            out[name + ', in part'] = {value[i:i + FRAGMENT] for i in range(
                len(value) - FRAGMENT + 1)}
    return out


class _StandIn(BaseHTTPRequestHandler):
    '''Schwab's REST and token endpoints, enough of them for one session.'''

    refuse_refresh = False
    websocket_port = None

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if form.get('grant_type') == ['refresh_token']:
            if type(self).refuse_refresh:
                return self._json(400, {
                    'error': 'invalid_grant',
                    'error_description': 'refresh token invalid'})
            access = SECRETS['refreshed access token']
            refresh = SECRETS['refreshed refresh token']
        else:
            access = SECRETS['exchanged access token']
            refresh = SECRETS['exchanged refresh token']
        self._json(200, {'access_token': access, 'refresh_token': refresh,
                         'token_type': 'Bearer', 'expires_in': 1800,
                         'scope': 'api', 'id_token': SECRETS['id token']})

    def do_GET(self):
        if self.path.startswith('/trader/v1/userPreference'):
            self._json(200, {'streamerInfo': [{
                'streamerSocketUrl': 'ws://127.0.0.1:{}'.format(
                    type(self).websocket_port),
                'schwabClientCustomerId': 'CUSTOMER',
                'schwabClientCorrelId': 'CORREL',
                'schwabClientChannel': 'N9',
                'schwabClientFunctionId': 'APIAPP'}]})
        elif self.path.startswith('/trader/v1/accounts/accountNumbers'):
            self._json(200, [{'accountNumber': SECRETS['account number'],
                              'hashValue': 'HASHVALUE'}])
        elif self.path.startswith('/marketdata/v1/quotes'):
            self._json(200, {'AAPL': {'quote': {'lastPrice': 1.5}}})
        else:
            self._json(401, {'error': 'unauthorized'})


async def _streamer(ws):
    '''Answers every request, and pushes a quote and an account activity.'''
    async for raw in ws:
        for req in json.loads(raw)['requests']:
            await ws.send(json.dumps({'response': [{
                'service': req['service'], 'command': req['command'],
                'requestid': req['requestid'],
                'SchwabClientCorrelId': 'CORREL', 'timestamp': 1,
                'content': {'code': 0, 'msg': 'ok'}}]}))
            if req['command'] == 'SUBS' and req['service'] == 'ACCT_ACTIVITY':
                await ws.send(json.dumps({'data': [{
                    'service': 'ACCT_ACTIVITY', 'timestamp': 1,
                    'command': 'SUBS', 'content': [{
                        'seq': 1, 'key': 'SUBSCRIPTIONKEY',
                        '1': SECRETS['account number'],
                        '2': 'OrderCreated',
                        '3': json.dumps({'AccountNumber':
                                         SECRETS['account number']})}, {
                        'seq': 2, 'key': 'SUBSCRIPTIONKEY',
                        '1': SECRETS['stream-only account number'],
                        '2': 'OrderAccepted',
                        '3': json.dumps({'AccountNumber': SECRETS[
                            'stream-only account number']})}]}]}))


class SecretLoggingTest(unittest.TestCase):

    def setUp(self):
        new_secrets()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _StandIn)
        self.http_port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        _StandIn.refuse_refresh = False

        # Every Schwab URL is pointed at the stand-in below httpx2, so httpcore
        # really runs.
        real = httpx2.HTTPTransport.handle_request
        areal = httpx2.AsyncHTTPTransport.handle_async_request
        port = self.http_port

        def rewrite(request):
            if request.url.host == 'api.schwabapi.com':
                request.url = request.url.copy_with(
                    scheme='http', host='127.0.0.1', port=port)
                request.headers['host'] = '127.0.0.1:{}'.format(port)

        def handle(transport, request):
            rewrite(request)
            return real(transport, request)

        async def ahandle(transport, request):
            rewrite(request)
            return await areal(transport, request)

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(
            httpx2.HTTPTransport, 'handle_request', handle))
        stack.enter_context(patch.object(
            httpx2.AsyncHTTPTransport, 'handle_async_request', ahandle))
        # websockets' documented WEBSOCKETS_MAX_LOG_SIZE, set high: its
        # default elides a long frame's middle, which can cut the LOGIN token
        # out of the line and so hide a leak from this test.
        stack.enter_context(patch.object(
            websockets.frames.Frame, 'MAX_LOG_SIZE', 1_000_000))

        # Every logger at DEBUG, all of it captured at the root.
        self.records = io.StringIO()
        handler = logging.StreamHandler(self.records)
        handler.setFormatter(logging.Formatter(
            '%(name)s %(levelname)s %(message)s'))
        root = logging.getLogger()
        saved = {'': root.level}
        for name in ('schwaby', 'websockets', 'httpx2', 'httpx', 'httpcore',
                     'authlib', 'werkzeug', 'asyncio', 'urllib3'):
            saved[name] = logging.getLogger(name).level
            logging.getLogger(name).setLevel(logging.DEBUG)
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)

        def restore():
            root.removeHandler(handler)
            for name, level in saved.items():
                logging.getLogger(name).setLevel(level)
            self.server.shutdown()
            self.server.server_close()
        self.addCleanup(restore)

    def logged(self):
        return '\n'.join(
            line for line in self.records.getvalue().splitlines()
            if not line.startswith(STAND_IN_LOGGERS))

    def assertNoSecret(self, text, where):
        found = []
        for name, variants in needles().items():
            for line in text.splitlines():
                if any(v in line for v in variants):
                    found.append('{}: {}'.format(name, line[:160]))
        self.assertEqual([], found, 'secrets in {}'.format(where))

    def run_session(self, tmp, *, reload_after_refresh=True):
        '''A login, a refresh, REST, the stream, and their error paths.

        The last step loads a new client from the refreshed token file. That
        load is itself a way a token gets registered, so a check that runs
        after it cannot tell whether the refresh registered the token too.
        '''
        token_path = os.path.join(tmp, 'token.json')

        async def go():
            async with websockets.serve(_streamer, '127.0.0.1', 0) as ws:
                _StandIn.websocket_port = ws.sockets[0].getsockname()[1]

                # The two-step login: an authorization URL, then the exchange.
                ctx = auth.get_auth_context(SECRETS['app key'], CALLBACK)
                received = '{}/?code={}&session=x&state={}'.format(
                    CALLBACK, SECRETS['authorization code'], ctx.state)
                auth.client_from_received_url(
                    SECRETS['app key'], SECRETS['app secret'], ctx, received,
                    auth.token_file_writer(token_path))

                # A login refused for its state, so its exception is logged.
                try:
                    auth.client_from_received_url(
                        SECRETS['app key'], SECRETS['app secret'], ctx,
                        received.replace('state=', 'state=WRONG'),
                        lambda *args, **kwargs: None)
                except Exception:
                    logging.getLogger('session').exception('login refused')

                # A stale token on disk, so the first call refreshes it.
                with open(token_path) as f:
                    stored = json.load(f)
                stored['token'].update({
                    'access_token': SECRETS['stale access token'],
                    'refresh_token': SECRETS['stale refresh token'],
                    'expires_at': 1})
                with open(token_path, 'w') as f:
                    json.dump(stored, f)
                client = auth.client_from_token_file(
                    token_path, SECRETS['app key'], SECRETS['app secret'])

                client.get_quotes(['AAPL'])
                client.get_account_numbers()
                failed = client.get_accounts()
                try:
                    failed.raise_for_status()
                except Exception:
                    logging.getLogger('session').exception('REST error')
                logging.getLogger('session').debug(
                    'reprs: %r %r %r', client, client.token_metadata,
                    client.session)

                stream = StreamClient(client)
                stream.add_level_one_equity_handler(lambda msg: None)
                stream.add_account_activity_handler(lambda msg: None)
                await stream.login()
                await stream.level_one_equity_subs(['AAPL'])
                await stream.account_activity_sub()
                await stream.handle_message()
                await stream.logout()

            if not reload_after_refresh:
                return

            # A refresh Schwab refuses.
            _StandIn.refuse_refresh = True
            with open(token_path) as f:
                stored = json.load(f)
            stored['token']['expires_at'] = 1
            with open(token_path, 'w') as f:
                json.dump(stored, f)
            refused = auth.client_from_token_file(
                token_path, SECRETS['app key'], SECRETS['app secret'])
            try:
                refused.get_quotes(['AAPL'])
            except Exception:
                logging.getLogger('session').exception('refresh refused')

        asyncio.run(go())

    @no_duplicates
    def test_a_session_logs_no_secret_at_any_level(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                contextlib.redirect_stdout(stdout):
            self.run_session(tmp)
        logged = self.logged()

        self.assertNoSecret(logged, 'the logs')

        # Positive controls: the lines that carried secrets were logged, with
        # the secrets masked. Without these, an empty capture would pass.
        login = [line for line in logged.splitlines()
                 if '"command": "LOGIN"' in line or 'LOGIN' in line]
        self.assertTrue(any('Send' in line for line in
                            logged.splitlines()), 'no streamer Send line')
        self.assertIn('"Authorization": "<REDACTED', logged)
        self.assertTrue(login)
        frames = [line for line in logged.splitlines()
                  if line.startswith('websockets.client') and '> TEXT' in line]
        self.assertTrue(any('LOGIN' in f for f in frames),
                        'websockets logged no LOGIN frame')
        self.assertIn('<REDACTED ACCOUNT_NUMBER>', logged)
        self.assertIn('OrderCreated', logged)
        # The frame bringing the stream-only account, as websockets logged it
        # on arrival -- before this library had decoded it.
        self.assertTrue(any(
            line.startswith('websockets.client') and '< TEXT' in line
            and 'OrderAccepted' in line for line in logged.splitlines()),
            'websockets logged no account activity frame')

    @no_duplicates
    def test_the_bug_report_is_free_of_secrets(self):
        report = io.StringIO()
        before = {n: list(logging.getLogger(n).handlers) for n in (
            'schwaby.auth', 'schwaby.client.base', 'schwaby.streaming',
            'schwaby.contrib.util', 'schwaby.debug')}
        write = debug._enable_bug_report_logging(output=report)

        def undo():
            import atexit
            atexit.unregister(write)
            debug._COLLECT_RESPONSE_REDACTIONS = False
            for name, handlers in before.items():
                logging.getLogger(name).handlers[:] = handlers
        self.addCleanup(undo)

        # The report is written before anything reloads the refreshed token.
        # Loading a token from a file registers it, so a session that reloads
        # afterwards would register the refreshed token in time for the
        # report to redact it retroactively, and this test would pass even if
        # a refresh registered nothing. A process that refreshes and carries
        # on never reloads.
        with tempfile.TemporaryDirectory() as tmp, \
                contextlib.redirect_stdout(io.StringIO()):
            self.run_session(tmp, reload_after_refresh=False)
        write()

        self.assertIn('BEGIN REDACTED LOGS', report.getvalue())
        self.assertIn('LOGIN', report.getvalue())
        self.assertNoSecret(report.getvalue(), 'the bug report')

    @no_duplicates
    def test_the_manual_flow_prints_its_url_and_logs_no_secret(self):
        # The authorization URL carries the app key, and the user has to open
        # it, so it is printed -- to the terminal, never to a log.
        printed = io.StringIO()
        state = {}

        def answer(prompt=''):
            url = next(line.strip() for line in printed.getvalue().splitlines()
                       if 'oauth/authorize' in line)
            state.update(urllib.parse.parse_qs(
                urllib.parse.urlparse(url).query))
            return '{}/?code={}&session=x&state={}'.format(
                CALLBACK, SECRETS['authorization code'], state['state'][0])

        with tempfile.TemporaryDirectory() as tmp, \
                contextlib.redirect_stdout(printed), \
                patch('builtins.input', answer):
            auth.client_from_manual_flow(
                SECRETS['app key'], SECRETS['app secret'], CALLBACK,
                os.path.join(tmp, 'token.json'))

        self.assertIn('client_id=' + SECRETS['app key'], printed.getvalue())
        self.assertNoSecret(self.logged(), 'the manual flow\'s logs')
        self.assertIn('schwaby.auth', self.logged())


if __name__ == '__main__':
    unittest.main()
