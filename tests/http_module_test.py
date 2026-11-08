'''The HTTP module this library guards against must be the one authlib built
its OAuth2 client on.

authlib 1.8 stopped importing httpx directly. It now imports httpx2 when that
package is installed and falls back to httpx when it is not, so which of the two
this library is actually running against is decided by the environment rather
than by anything here.

That matters because the two are not interchangeable and do not share a
hierarchy. httpx2.Response is not httpx.Response, and httpx2.ConnectError is not
a subclass of httpx.ConnectError. So a mismatch does not fail loudly -- an
isinstance guard simply stops matching and an except clause simply stops
catching. Both do so only in production, where the response comes from authlib's
session; a test which builds its own httpx.Response never notices, which is why
the rest of the suite passes either way.

These tests deliberately ask which module object was imported rather than what
it was bound as, so they say the same thing before and after a rename and fail
only on a genuine mismatch.
'''

from authlib.integrations.httpx_client import OAuth2Client
from unittest.mock import patch, MagicMock

import importlib
import os
import tempfile
import types
import unittest

from schwaby import auth, debug, streaming
from schwaby.orders import generic
from schwaby.orders.generic import OrderBuilder

from .utils import (MockAsyncOAuthClient, MockOAuthClient,
                    bound_callback_socket,
                    no_duplicates)


HTTP_MODULE_NAMES = ('httpx', 'httpx2')

# Every module here builds a type guard, an except clause or a status
# comparison against the HTTP module it imports.
MODULES_WHICH_GUARD_ON_HTTP = (auth, debug, generic, streaming)

API_KEY = 'APIKEY'
APP_SECRET = '0x5EC07'


def http_module_authlib_resolved():
    '''The module authlib built ``OAuth2Client`` on, read off the class itself.

    Asking the class rather than importing something is what makes this work on
    both sides of the change: authlib below 1.8 imports httpx directly and
    authlib 1.8 and up goes through its ``_compat`` shim, but either way the
    client it produced inherits from one concrete ``Client``.
    '''
    for base in OAuth2Client.__mro__:
        top_level = base.__module__.split('.')[0]
        if base.__name__ == 'Client' and top_level in HTTP_MODULE_NAMES:
            return importlib.import_module(top_level)

    raise AssertionError(
            'OAuth2Client is built on neither httpx nor httpx2, so this test '
            'no longer knows what to compare against: '
            + repr([b.__module__ + '.' + b.__name__
                    for b in OAuth2Client.__mro__]))


def http_modules_used_by(module):
    '''Every httpx-or-httpx2 module object ``module`` imported, regardless of
    the name it was bound to.'''
    return {
            value for value in vars(module).values()
            if isinstance(value, types.ModuleType)
            and value.__name__ in HTTP_MODULE_NAMES}


def the_http_module_used_by(module):
    '''The single HTTP module ``module`` imported.

    Behaviour tests patch and raise through this rather than through whatever
    authlib resolved, so that they exercise the code path under test instead of
    silently patching a module the code never calls -- which does not fail, it
    lets a real connection attempt run. Whether this agrees with authlib is
    :class:`HttpModuleResolutionTest`'s job, and keeping the two separate is
    what stops a mismatch turning every test in the file red at once.
    '''
    used = http_modules_used_by(module)
    assert len(used) == 1, (
            f'{module.__name__} imports {sorted(m.__name__ for m in used)}, '
            f'so there is no single module to exercise')
    return next(iter(used))


class HttpModuleResolutionTest(unittest.TestCase):

    @no_duplicates
    def test_every_module_agrees_with_the_one_authlib_resolved(self):
        resolved = http_module_authlib_resolved()

        for module in MODULES_WHICH_GUARD_ON_HTTP:
            used = http_modules_used_by(module)

            # A module which imports nothing HTTP-shaped has nothing to
            # disagree about. Asserting it imports something would just pin the
            # current file layout.
            if not used:
                continue

            self.assertEqual(
                    {resolved}, used,
                    f'{module.__name__} builds its guards against '
                    f'{sorted(m.__name__ for m in used)} while authlib built '
                    f'OAuth2Client on {resolved.__name__}. Those types are '
                    f'unrelated, so the guards in {module.__name__} cannot '
                    f'match what the client actually returns.')

    @no_duplicates
    def test_no_module_imports_both(self):
        # Importing both is how a half-finished migration hides: the file still
        # resolves every name, and only the guard which happens to use the
        # wrong one goes quiet.
        for module in MODULES_WHICH_GUARD_ON_HTTP:
            used = http_modules_used_by(module)
            self.assertLessEqual(
                    len(used), 1,
                    f'{module.__name__} imports both '
                    f'{sorted(m.__name__ for m in used)}')


class ChildOrderStrategyGuardTest(unittest.TestCase):
    '''``add_child_order_strategy`` exists to catch a caller passing the
    response from ``get_order`` instead of its parsed body. The response it has
    to catch is whatever authlib's session returns, so that is what this builds
    rather than naming a module.'''

    @no_duplicates
    def test_a_response_from_the_client_is_rejected(self):
        response = http_module_authlib_resolved().Response(200)

        with self.assertRaisesRegex(
                ValueError, 'Child order cannot be a response'):
            OrderBuilder().add_child_order_strategy(response)


class ConnectErrorSiblingsAreAbsorbedTest(unittest.TestCase):
    '''The readiness wait in the login flow catches ConnectError and
    ConnectTimeout, which are siblings rather than parent and child. Both are
    raised here from whichever module ``auth`` imported, so the pair keeps
    being checked against the real type after a rename rather than against a
    module name written down once.'''

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.token_path = os.path.join(self.tmp_dir.name, 'token.json')

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _drive_login_flow_until_the_wait_gives_up(self, exception_name):
        '''Run the flow far enough to exercise the readiness wait, patching
        ``get`` on the module object itself so the patch names no module.'''
        http = the_http_module_used_by(auth)

        with patch.object(http, 'get') as mock_get:
            mock_get.side_effect = getattr(http, exception_name)('nope')

            held = bound_callback_socket()
            self.addCleanup(held.close)
            with patch('schwaby.auth.SERVER_STARTUP_TIMEOUT', 0.3):
                with self.assertRaises(auth.RedirectServerExitedError):
                    auth.client_from_login_flow(
                            API_KEY, APP_SECRET,
                            'https://127.0.0.1:%d/callback'
                            % held.getsockname()[1],
                            self.token_path, callback_socket=held)

        # Without this the test would also pass if the patch never intercepted
        # anything and the wait gave up for some unrelated reason.
        self.assertTrue(
                mock_get.called,
                f'{http.__name__}.get was never called, so nothing raised '
                f'{exception_name} and this test proved nothing')

    @no_duplicates
    @patch('schwaby.auth.Client')
    @patch('schwaby.auth.OAuth2Client', new_callable=MockOAuthClient)
    @patch('schwaby.auth.AsyncOAuth2Client', new_callable=MockAsyncOAuthClient)
    @patch('schwaby.auth.webbrowser.get', new_callable=MagicMock)
    @patch('schwaby.auth.input', MagicMock(return_value=''))
    def test_connect_error_is_absorbed(
            self, mock_webbrowser_get, async_session, sync_session, client):
        sync_session.return_value = sync_session
        sync_session.create_authorization_url.return_value = 'https://x', None

        # Absorbed rather than propagated: the flow ends in
        # RedirectServerExitedError, which is this library giving up, not in
        # the ConnectError escaping.
        self._drive_login_flow_until_the_wait_gives_up('ConnectError')

    @no_duplicates
    @patch('schwaby.auth.Client')
    @patch('schwaby.auth.OAuth2Client', new_callable=MockOAuthClient)
    @patch('schwaby.auth.AsyncOAuth2Client', new_callable=MockAsyncOAuthClient)
    @patch('schwaby.auth.webbrowser.get', new_callable=MagicMock)
    @patch('schwaby.auth.input', MagicMock(return_value=''))
    def test_connect_timeout_is_absorbed(
            self, mock_webbrowser_get, async_session, sync_session, client):
        sync_session.return_value = sync_session
        sync_session.create_authorization_url.return_value = 'https://x', None

        # The sibling case. ConnectTimeout is not a subclass of ConnectError,
        # so catching only the latter would let this one end the login flow
        # while the server was still coming up.
        self._drive_login_flow_until_the_wait_gives_up('ConnectTimeout')
