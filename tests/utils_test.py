from unittest.mock import MagicMock


class PicklableResponse:
    '''Stands in for a response where the test really pickles.

    MagicMock is not picklable, so a round-trip test built on one can only ever
    call copy() -- which is in-process, and therefore not the thing the fix is
    about. Module level so pickle can find it by name.
    '''

    def __init__(self, marker='response'):
        self.marker = marker

    def __eq__(self, other):
        return (isinstance(other, PicklableResponse)
                and other.marker == self.marker)

from schwaby.utils import (
    AccountHashMismatchException,
    HTTPStatusError,
    MissingLocationHeaderError,
    OrderIdNotFoundError,
    SchwabError,
    UnrecognizedLocationError,
    UnsuccessfulOrderException,
    Utils,
)
from schwaby.utils import EnumEnforcer, _describe_error
from .utils import no_duplicates, MockResponse

import decimal
import enum
import unittest


class HTTPStatusErrorTest(unittest.TestCase):
    """The name a consumer catches must be the class a real call raises.

    `httpx2` shares no exception hierarchy with `httpx`, so a handler written
    against the wrong package does not match and says nothing. The alias only
    helps if it is the class that actually escapes -- so this drives a real
    client through its real session stack rather than asserting a name.
    """

    @staticmethod
    def _client(status):
        import httpx2
        import time
        from schwaby.auth import client_from_access_functions

        # An access token with an hour left, so no call refreshes it. An
        # expires_at centuries off is refreshed on the first call instead.
        token = {'access_token': 'a', 'refresh_token': 'r',
                 'token_type': 'Bearer', 'expires_in': 3600,
                 'expires_at': int(time.time()) + 3600}
        client = client_from_access_functions(
                'api-key', 'app-secret',
                lambda: {'creation_timestamp': 9999999999, 'token': token},
                lambda *args, **kwargs: None)
        client.session._transport = httpx2.MockTransport(
                lambda request: httpx2.Response(status, request=request))
        # A proxy variable routes requests through mounts, not `_transport`,
        # which would send this test to the network.
        client.session._mounts = {}
        return client

    @staticmethod
    def _refreshing_client(token_response, writes=None, asynchronous=False):
        # An access token that has already expired, so the session refreshes
        # it on the way past -- through the real authlib path, which is what
        # the documented behaviour depends on.
        import httpx2
        from schwaby.auth import client_from_access_functions

        token = {'access_token': 'a', 'refresh_token': 'r',
                 'token_type': 'Bearer', 'expires_in': 3600, 'expires_at': 1}
        requests = []

        def handler(request):
            requests.append(request.url.path)
            if request.url.path.endswith('/oauth/token'):
                return token_response(request)
            return httpx2.Response(200, json={}, request=request)

        client = client_from_access_functions(
                'api-key', 'app-secret',
                lambda: {'creation_timestamp': 9999999999, 'token': token},
                (lambda *args, **kwargs: None) if writes is None
                else (lambda written, *args, **kwargs: writes.append(written)),
                asyncio=asynchronous)
        client.session._transport = httpx2.MockTransport(handler)
        client.session._mounts = {}   # see `_client`
        return client, requests

    @no_duplicates
    def test_a_server_error_refreshing_the_token_raises_out_of_the_call(self):
        # Documented: a 5xx from the token endpoint escapes the call itself,
        # before any response is handed back, and describes the token request
        # rather than the one that was made.
        import httpx2
        client, requests = self._refreshing_client(
                lambda request: httpx2.Response(503, request=request))
        with self.assertRaises(HTTPStatusError) as caught:
            client.get_quote('F')
        self.assertTrue(
                str(caught.exception.response.url).endswith('/oauth/token'))
        self.assertEqual(['/v1/oauth/token'], requests)

    @no_duplicates
    def test_a_refresh_rejected_with_an_error_body_is_a_refresh_error(self):
        import httpx2
        from schwaby.utils import TokenRefreshError
        client, requests = self._refreshing_client(
                lambda request: httpx2.Response(
                    400, json={'error': 'invalid_grant'}, request=request))
        with self.assertRaises(TokenRefreshError) as caught:
            client.get_quote('F')
        # Schwab's rejection, translated -- not a token that failed locally,
        # and not a call that never reached the endpoint.
        self.assertTrue(caught.exception.refresh_token_invalid)
        self.assertEqual(['/v1/oauth/token'], requests)

    @staticmethod
    def _json_response(status, body, request):
        # The JSON bytes themselves: `json=None` would send no body at all,
        # which is the not-JSON case rather than JSON `null`.
        import httpx2
        import json
        return httpx2.Response(status, content=json.dumps(body).encode(),
                               headers={'Content-Type': 'application/json'},
                               request=request)

    GOOD_TOKEN = {'access_token': 'NEW', 'refresh_token': 'r2',
                  'token_type': 'Bearer', 'expires_in': 1800}

    @no_duplicates
    def test_a_refresh_response_that_is_not_a_token_is_not_stored(self):
        # authlib takes any JSON object without an `error` key as the new
        # token, and keeps JSON that is not an object before failing on it.
        # {"message": "Unauthorized"} was written to the token file, and a
        # list or a string was kept in memory; either way every call after it
        # failed without contacting Schwab. Now none is stored: the call
        # raises, and the next one refreshes again and recovers once the
        # endpoint does. Each body is sent at a success as well as at a
        # refusal: a token is kept only from a success, so at a refusal a
        # body wrongly judged usable is refused anyway, and only the success
        # proves the judgement.
        import time
        from schwaby.utils import TokenRefreshError
        usable = {'access_token': 'NEW', 'token_type': 'Bearer',
                  'expires_in': 1800}
        for body in ({'message': 'Unauthorized'}, ['a'], [], 'x', '', None, 7,
                     dict(usable, token_type='mac'),
                     dict(usable, token_type=['Bearer']),
                     dict(usable, refresh_token=None),
                     dict(usable, refresh_token=''),
                     {'access_token': 'NEW', 'token_type': 'Bearer'},
                     dict(usable, expires_in=0),
                     dict(usable, expires_in='abc'),
                     dict(usable, expires_in=float('inf')),
                     dict(usable, expires_at=int(time.time()) * 1000),
                     # Milliseconds sent as expires_in: 20.8 days of 401s.
                     dict(usable, expires_in=1800000)):
            for status in (200, 401):
                with self.subTest(body=body, status=status):
                    answers = [(status, body), (200, self.GOOD_TOKEN)]
                    writes = []
                    client, requests = self._refreshing_client(
                            lambda request: self._json_response(
                                *answers.pop(0), request), writes=writes)
                    with self.assertRaises(TokenRefreshError) as caught:
                        client.get_quote('F')
                    self.assertFalse(caught.exception.refresh_token_invalid)
                    self.assertEqual([], writes)
                    self.assertEqual(200, client.get_quote('F').status_code)
                    self.assertEqual(['/v1/oauth/token', '/v1/oauth/token',
                                      '/marketdata/v1/F/quotes'], requests)
                    self.assertEqual('NEW', writes[0]['token']['access_token'])

    @no_duplicates
    def test_a_server_error_with_a_json_body_is_still_a_server_error(self):
        # The pre-parse check leaves a 5xx to authlib whatever its body. The
        # empty 503 above passes through the not-JSON branch anyway, so it
        # proved nothing about this one.
        client, requests = self._refreshing_client(
                lambda request: self._json_response(
                    503, {'message': 'Service Unavailable'}, request))
        with self.assertRaises(HTTPStatusError):
            client.get_quote('F')
        self.assertEqual(['/v1/oauth/token'], requests)

    @no_duplicates
    def test_a_login_does_not_store_a_token_that_is_not_usable(self):
        # The token a login exchanges its code for was written unchecked:
        # {"message": "Unauthorized"} became the token file, and every call
        # after it failed without contacting Schwab.
        import httpx2
        from unittest.mock import patch
        from authlib.integrations.base_client.errors import OAuthError
        from authlib.integrations.httpx_client import OAuth2Client
        from schwaby import auth
        context = auth.AuthContext('https://127.0.0.1:8182',
                                   'https://example.invalid/authorize',
                                   'state')
        request = httpx2.Request('POST',
                                 'https://api.schwabapi.com/v1/oauth/token')

        def log_in(body, writes):
            with patch.object(OAuth2Client, 'post',
                              return_value=self._json_response(
                                  200, body, request)):
                return auth.client_from_received_url(
                        'api-key', 'app-secret', context,
                        'https://127.0.0.1:8182/?code=c&state=state',
                        lambda written, *args, **kwargs: writes.append(
                            written))

        writes = []
        with self.assertRaises(OAuthError):
            log_in({'message': 'Unauthorized'}, writes)
        self.assertEqual([], writes)
        # Positive control: a usable token logs in and is written once.
        log_in(dict(self.GOOD_TOKEN), writes)
        self.assertEqual(1, len(writes))
        self.assertEqual('NEW', writes[0]['token']['access_token'])

    @no_duplicates
    def test_a_stored_token_that_cannot_be_sent_needs_a_new_login(self):
        # authlib raises UnsupportedTokenTypeError locally, before any
        # request, when the stored token has no access token of a type it can
        # send -- a token file damaged before refreshes were checked, say.
        # Without a refresh token retrying never helps, so it says to log in.
        # With one, the token has no expiry authlib acts on, so it is
        # refreshed first, and a refresh that works replaces it. Schwab's own
        # unsupported_token_type rejection is a different class and stays
        # retryable; one has been seen to recover.
        import httpx2
        from schwaby.auth import client_from_access_functions
        from schwaby.utils import TokenRefreshError

        def build(token, requests):
            def handler(request):
                requests.append(request.url.path)
                if request.url.path.endswith('/oauth/token'):
                    return self._json_response(200, self.GOOD_TOKEN, request)
                return httpx2.Response(200, json={}, request=request)
            client = client_from_access_functions(
                    'api-key', 'app-secret',
                    lambda: {'creation_timestamp': 9999999999, 'token': token},
                    lambda *args, **kwargs: None)
            client.session._transport = httpx2.MockTransport(handler)
            client.session._mounts = {}
            return client

        requests = []
        client = build({'message': 'Unauthorized'}, requests)
        with self.assertRaises(TokenRefreshError) as caught:
            client.get_quote('F')
        self.assertTrue(caught.exception.refresh_token_invalid)
        self.assertEqual([], requests)

        requests = []
        client = build({'message': 'Unauthorized', 'refresh_token': 'r'},
                       requests)
        self.assertEqual(200, client.get_quote('F').status_code)
        self.assertEqual(1, sum(p.endswith('/oauth/token') for p in requests))

        client, _ = self._refreshing_client(
                lambda request: self._json_response(
                    400, {'error': 'unsupported_token_type',
                          'error_description': '400 Bad Request'}, request))
        with self.assertRaises(TokenRefreshError) as caught:
            client.get_quote('F')
        self.assertFalse(caught.exception.refresh_token_invalid)

    @no_duplicates
    def test_the_async_client_does_not_store_a_bad_refresh_response(self):
        import asyncio
        from schwaby.utils import TokenRefreshError
        answers = [(401, ['a']), (200, self.GOOD_TOKEN)]
        writes = []
        client, requests = self._refreshing_client(
                lambda request: self._json_response(*answers.pop(0), request),
                writes=writes, asynchronous=True)

        async def calls():
            with self.assertRaises(TokenRefreshError):
                await client.get_quote('F')
            self.assertEqual([], writes)
            return (await client.get_quote('F')).status_code

        self.assertEqual(200, asyncio.run(calls()))
        self.assertEqual('NEW', writes[0]['token']['access_token'])

    @no_duplicates
    def test_a_client_from_a_login_does_not_store_a_bad_refresh_response(self):
        # The check was first added where token files and access functions
        # build their session, and a client returned by a login built its
        # own, so it went on storing whatever the token endpoint returned.
        import httpx2
        from unittest.mock import patch
        from authlib.integrations.httpx_client import OAuth2Client
        from schwaby import auth
        from schwaby.utils import TokenRefreshError
        expired = {'access_token': 'a', 'refresh_token': 'r',
                   'token_type': 'Bearer', 'expires_in': 3600,
                   'expires_at': 1}
        writes = []
        context = auth.AuthContext('https://127.0.0.1:8182',
                                   'https://example.invalid/authorize',
                                   'state')
        with patch.object(OAuth2Client, 'fetch_token', return_value=expired):
            client = auth.client_from_received_url(
                    'api-key', 'app-secret', context,
                    'https://127.0.0.1:8182/?code=c&state=state',
                    lambda written, *args, **kwargs: writes.append(written))
        self.assertEqual(1, len(writes))   # the login's own write

        answers = [(401, {'message': 'Unauthorized'}), (200, self.GOOD_TOKEN)]
        client.session._transport = httpx2.MockTransport(
                lambda request: self._json_response(*answers.pop(0), request)
                if request.url.path.endswith('/oauth/token')
                else httpx2.Response(200, json={}, request=request))
        client.session._mounts = {}
        with self.assertRaises(TokenRefreshError):
            client.get_quote('F')
        self.assertEqual(1, len(writes))
        self.assertEqual(200, client.get_quote('F').status_code)
        self.assertEqual('NEW', writes[-1]['token']['access_token'])

    @no_duplicates
    def test_a_refusal_without_a_json_body_is_a_token_refresh_error(self):
        # authlib changed what this raised at 1.9 -- json.JSONDecodeError
        # before, HTTPStatusError after -- so the class callers were told to
        # catch depended on which authlib a fresh install resolved. Refused
        # here instead, so it is TokenRefreshError under either; this test
        # runs against whichever authlib is installed, and CI installs fresh.
        import httpx2
        from schwaby.utils import TokenRefreshError
        for status, body in ((401, b''), (403, b'<html>denied</html>'),
                             (302, b'')):
            with self.subTest(status=status):
                client, _ = self._refreshing_client(
                        lambda request: httpx2.Response(
                            status, content=body, request=request))
                with self.assertRaises(Exception) as caught:
                    client.get_quote('F')
                self.assertIsInstance(caught.exception, TokenRefreshError)
                self.assertIn('HTTP {}'.format(status),
                              str(caught.exception))
                # Not known to be terminal, so retryable: the conservative
                # direction the class documents.
                self.assertFalse(caught.exception.refresh_token_invalid)

    @no_duplicates
    def test_a_success_without_a_json_body_is_still_a_value_error(self):
        # A 200 that is not JSON is not a refusal, and is left to authlib,
        # which raises a ValueError for it under both 1.8 and 1.9.
        import httpx2
        from schwaby.utils import TokenRefreshError
        client, _ = self._refreshing_client(
                lambda request: httpx2.Response(
                    200, content=b'not json', request=request))
        with self.assertRaises(Exception) as caught:
            client.get_quote('F')
        self.assertIsInstance(caught.exception, ValueError)
        self.assertNotIsInstance(caught.exception, TokenRefreshError)

    @no_duplicates
    def test_a_server_error_without_a_json_body_is_still_an_http_error(self):
        # Positive control for the refusal above: a 5xx is not refused by this
        # library, and stays the HTTPStatusError it is documented as.
        import httpx2
        from schwaby.utils import TokenRefreshError
        client, _ = self._refreshing_client(
                lambda request: httpx2.Response(
                    503, content=b'', request=request))
        with self.assertRaises(Exception) as caught:
            client.get_quote('F')
        self.assertIsInstance(caught.exception, HTTPStatusError)
        self.assertNotIsInstance(caught.exception, TokenRefreshError)

    @no_duplicates
    def test_it_is_the_class_the_http_package_defines(self):
        import httpx2
        self.assertIs(httpx2.HTTPStatusError, HTTPStatusError)

    @no_duplicates
    def test_it_catches_what_a_real_client_call_raises(self):
        response = self._client(429).get_quote('F')
        with self.assertRaises(HTTPStatusError) as caught:
            response.raise_for_status()
        self.assertEqual(429, caught.exception.response.status_code)

    @no_duplicates
    def test_a_successful_call_does_not_raise(self):
        # Positive control: the same stub answering 200 does not raise, so the
        # refusal above comes from the status and not from the stub itself.
        self._client(200).get_quote('F').raise_for_status()

    @no_duplicates
    def test_it_is_not_a_schwab_error(self):
        # Stated in the docs, because `except SchwabError` is documented as
        # one name for everything this library defines, and this is not one.
        self.assertFalse(issubclass(HTTPStatusError, SchwabError))


class TokenEndpointStatusTest(unittest.TestCase):
    """What a failed refresh says about the token endpoint's answer: its HTTP
    status, whether it asked for fewer requests, and whether it answered at
    all. Driven through the real session stack, since the status is taken
    from the response on the way through."""

    _refreshing_client = staticmethod(HTTPStatusErrorTest._refreshing_client)
    _json_response = staticmethod(HTTPStatusErrorTest._json_response)

    def refused(self, token_response, asynchronous=False):
        import asyncio
        from schwaby.utils import TokenRefreshError
        client, requests = self._refreshing_client(
                token_response, asynchronous=asynchronous)
        with self.assertRaises(TokenRefreshError) as caught:
            if asynchronous:
                asyncio.run(client.get_quote('F'))
            else:
                client.get_quote('F')
        return caught.exception, requests

    @no_duplicates
    def test_a_token_at_a_refusal_is_refused_on_either_session(self):
        # The async session runs the same hook through authlib's async
        # refresh; a token is kept only from a success there too.
        import asyncio
        from schwaby.utils import TokenRefreshError
        good = HTTPStatusErrorTest.GOOD_TOKEN
        for asynchronous in (False, True):
            for status in (302, 401, 429, 200):
                with self.subTest(status=status, asynchronous=asynchronous):
                    writes = []
                    client, requests = self._refreshing_client(
                            lambda request: self._json_response(
                                status, good, request),
                            writes=writes, asynchronous=asynchronous)
                    try:
                        if asynchronous:
                            asyncio.run(client.get_quote('F'))
                        else:
                            client.get_quote('F')
                    except TokenRefreshError as e:
                        refusal = e
                    else:
                        refusal = None
                    if status == 200:
                        self.assertIsNone(refusal)
                        self.assertEqual(
                                ['NEW'],
                                [w['token']['access_token'] for w in writes])
                        continue
                    self.assertIsNotNone(refusal)
                    self.assertEqual('http_{}'.format(status),
                                     refusal.__cause__.error)
                    self.assertEqual(status, refusal.status_code)
                    self.assertEqual(status == 429, refusal.rate_limited)
                    self.assertEqual([], writes)
                    self.assertEqual(['/v1/oauth/token'], requests)

    @no_duplicates
    def test_a_refusal_without_a_json_body_carries_its_status(self):
        import httpx2
        for asynchronous in (False, True):
            for status in (429, 400, 401):
                with self.subTest(status=status, asynchronous=asynchronous):
                    error, requests = self.refused(
                            lambda request: httpx2.Response(
                                status, content=b'', request=request),
                            asynchronous)
                    self.assertEqual(['/v1/oauth/token'], requests)
                    self.assertEqual(status, error.status_code)
                    self.assertTrue(error.endpoint_answered)

    @no_duplicates
    def test_a_refusal_with_a_json_body_carries_its_status(self):
        # Raised here rather than left to authlib, which puts no status on
        # its error, so an implementation reading it from there would leave
        # this None.
        for asynchronous in (False, True):
            for status in (400, 429):
                with self.subTest(status=status, asynchronous=asynchronous):
                    error, _ = self.refused(
                            lambda request: self._json_response(
                                status, {'error': 'invalid_grant',
                                         'error_description': 'expired'},
                                request),
                            asynchronous)
                    self.assertEqual(status, error.status_code)
                    self.assertTrue(error.endpoint_answered)
                    # Still the endpoint's own refusal, as authlib reports
                    # one: terminal, with its code and text as the cause.
                    self.assertTrue(error.refresh_token_invalid)
                    self.assertEqual('invalid_grant', error.__cause__.error)
                    self.assertEqual('expired', error.__cause__.description)

    @no_duplicates
    def test_an_unusable_token_response_carries_its_status(self):
        error, _ = self.refused(
                lambda request: self._json_response(
                    200, {'message': 'Unauthorized'}, request))
        self.assertEqual('unusable_token_response', error.__cause__.error)
        self.assertEqual(200, error.status_code)
        self.assertTrue(error.endpoint_answered)

    @no_duplicates
    def test_only_a_429_is_rate_limited(self):
        import httpx2
        # Above 429 as well as below it, so a range is not mistaken for 429.
        for status in (429, 400, 401, 403, 431, 499):
            for json_body in (False, True):
                with self.subTest(status=status, json_body=json_body):
                    error, _ = self.refused(
                            (lambda request: self._json_response(
                                status, {'error': 'x'}, request))
                            if json_body else
                            (lambda request: httpx2.Response(
                                status, content=b'', request=request)))
                    self.assertEqual(status == 429, error.rate_limited)

    @no_duplicates
    def test_a_token_that_fails_before_anything_is_sent_says_so(self):
        # authlib refuses a stored token with no access token of a type it
        # can send, UnsupportedTokenTypeError, before asking the endpoint
        # anything.
        import httpx2
        from schwaby.auth import client_from_access_functions
        from schwaby.utils import TokenRefreshError

        def build(token, requests):
            def handler(request):
                requests.append(request.url.path)
                if request.url.path.endswith('/oauth/token'):
                    return httpx2.Response(401, content=b'', request=request)
                return httpx2.Response(200, json={}, request=request)
            client = client_from_access_functions(
                    'api-key', 'app-secret',
                    lambda: {'creation_timestamp': 9999999999, 'token': token},
                    lambda *args, **kwargs: None)
            client.session._transport = httpx2.MockTransport(handler)
            client.session._mounts = {}
            return client

        requests = []
        with self.assertRaises(TokenRefreshError) as caught:
            build({'message': 'Unauthorized'}, requests).get_quote('F')
        self.assertEqual([], requests)
        self.assertFalse(caught.exception.endpoint_answered)
        self.assertIsNone(caught.exception.status_code)
        self.assertFalse(caught.exception.rate_limited)

        # Positive control: the same client, asking the endpoint, says it
        # answered.
        requests = []
        with self.assertRaises(TokenRefreshError) as caught:
            build({'access_token': 'a', 'refresh_token': 'r',
                   'token_type': 'Bearer', 'expires_at': 1},
                  requests).get_quote('F')
        self.assertEqual(['/v1/oauth/token'], requests)
        self.assertTrue(caught.exception.endpoint_answered)
        self.assertEqual(401, caught.exception.status_code)

    @no_duplicates
    def test_the_attributes_survive_a_copy_and_a_pickle(self):
        import copy, pickle
        from schwaby.utils import LoginExchangeError, TokenRefreshError
        for exc in (TokenRefreshError('m', status_code=429,
                                      endpoint_answered=True),
                    LoginExchangeError('http_429', 'd', status_code=429,
                                       endpoint_answered=True)):
            for how, rebuild in (
                    ('copy', copy.copy), ('deepcopy', copy.deepcopy),
                    ('pickle', lambda e: pickle.loads(pickle.dumps(e)))):
                rebuilt = rebuild(exc)
                with self.subTest(exception=type(exc).__name__, how=how):
                    self.assertEqual(429, rebuilt.status_code)
                    self.assertTrue(rebuilt.rate_limited)
                    self.assertTrue(rebuilt.endpoint_answered)

class EnumEnforcerTest(unittest.TestCase):

    class TestClass(EnumEnforcer):
        def test_enforcement(self, value):
            self.convert_enum(value, EnumEnforcerTest.TestEnum)


    class TestEnum(enum.Enum):
        VALUE_1 = 1
        VALUE_2 = 2


    def test_valid_enum(self):
        t = self.TestClass(enforce_enums=True)
        t.test_enforcement(self.TestEnum.VALUE_1)

    def test_invalid_enum_passed_as_string(self):
        t = self.TestClass(enforce_enums=True)
        with self.assertRaisesRegex(
                ValueError, 'tests.utils_test.TestEnum.VALUE_1'):
            t.test_enforcement('VALUE_1')

    def test_invalid_enum_passed_as_not_string(self):
        t = self.TestClass(enforce_enums=True)
        with self.assertRaises(ValueError):
            t.test_enforcement(123)


class ConvertEnumIterableTest(unittest.TestCase):
    """`convert_enum_iterable`, which had no test for the rejecting branch.

    `convert_enum` next to it was covered both ways. This one was covered only
    where every element was already the right enum, so the arm that refuses a
    wrong element -- the reason `enforce_enums` exists -- was never taken.
    """

    class TestClass(EnumEnforcer):
        def convert(self, values):
            return self.convert_enum_iterable(
                    values, ConvertEnumIterableTest.TestEnum)

    class TestEnum(enum.Enum):
        VALUE_1 = 1
        VALUE_2 = 2

    @no_duplicates
    def test_a_wrong_element_is_refused_and_the_message_suggests_the_member(
            self):
        t = self.TestClass(enforce_enums=True)
        with self.assertRaisesRegex(
                ValueError, 'tests.utils_test.TestEnum.VALUE_1'):
            t.convert([self.TestEnum.VALUE_2, 'VALUE_1'])

    @no_duplicates
    def test_a_wrong_element_is_refused_even_without_a_suggestion(self):
        t = self.TestClass(enforce_enums=True)
        with self.assertRaises(ValueError):
            t.convert([123])

    @no_duplicates
    def test_a_type_whose_name_raises_is_still_refused_by_name(self):
        # type(value).__name__ consults the metaclass, which can raise inside
        # the message that was meant to say what was wrong.
        class Meta(type):
            @property
            def __name__(cls):
                raise RuntimeError('metaclass raised')

        class Odd(metaclass=Meta):
            pass

        t = self.TestClass(enforce_enums=True)
        with self.assertRaisesRegex(ValueError, 'got type "Odd"'):
            t.convert([Odd()])

    @no_duplicates
    def test_a_string_matching_no_member_gets_no_did_you_mean(self):
        # `type_error` only offers a suggestion when the string appears in
        # some member's full name. Every existing test passed a string that
        # did, so the branch where nothing matches -- which is the common case
        # for a genuine typo -- was never taken, and a broken suggestion
        # builder would have gone unnoticed for exactly those callers.
        t = self.TestClass(enforce_enums=True)
        with self.assertRaises(ValueError) as ctx:
            t.convert(['nothing_like_a_member_name'])

        self.assertNotIn('Did you mean', str(ctx.exception))
        # Still says what was wrong, which is the part that has to survive.
        self.assertIn('TestEnum', str(ctx.exception))
        self.assertIn('str', str(ctx.exception))

    @no_duplicates
    def test_the_same_element_passes_through_when_enforcement_is_off(self):
        # The control that makes the two above mean something: they must fail
        # because enforcement rejected the value, not because the value could
        # never get through at all.
        t = self.TestClass(enforce_enums=False)
        self.assertEqual([2, 'VALUE_1'],
                         t.convert([self.TestEnum.VALUE_2, 'VALUE_1']))

    @no_duplicates
    def test_correct_elements_are_converted(self):
        t = self.TestClass(enforce_enums=True)
        self.assertEqual([1, 2],
                         t.convert([self.TestEnum.VALUE_1,
                                    self.TestEnum.VALUE_2]))


class DescribeErrorTest(unittest.TestCase):
    """`_describe_error` against a body that is not an object.

    It runs on the failure path, where a formatter that raises would replace a
    useful exception with a useless one -- so it yields no suffix rather than
    an error of its own. `[1,2,3]`, `"a string"` and `null` are all valid JSON
    and all raise AttributeError on `.get`, which is the shape the guard is
    for and which nothing sent it.
    """

    @no_duplicates
    def test_a_body_that_is_not_an_object_yields_no_suffix(self):
        for payload in ([1, 2, 3], 'a string', None, 42, True):
            with self.subTest(payload=payload):
                self.assertEqual(
                        '', _describe_error(MockResponse(payload, 400)))

    @no_duplicates
    def test_an_object_body_still_yields_its_message(self):
        # Positive control. Every assertion above is satisfied by a function
        # that returns '' unconditionally.
        described = _describe_error(
                MockResponse({'message': 'Account not found'}, 400))
        self.assertIn('Account not found', described)


class SetAccountHashTest(unittest.TestCase):
    """`Utils.set_account_hash`, which no test called.

    Public, documented on the util page with `automethod`, and reached by
    nothing -- so whether it takes effect on the next call was unverified.
    """

    @no_duplicates
    def test_the_new_hash_is_the_one_used_afterwards(self):
        client = MagicMock()
        u = Utils(client, '0xf1rsth45h')
        self.assertEqual('0xf1rsth45h', u.account_hash)

        u.set_account_hash('0x53c0ndh45h')
        self.assertEqual('0x53c0ndh45h', u.account_hash)

    @no_duplicates
    def test_it_is_the_hash_a_later_call_actually_sends(self):
        # The assertion above only checks the attribute. This checks that the
        # value is the one that leaves the process, which is the thing a
        # caller switching accounts is relying on.
        client = MagicMock()
        u = Utils(client, '0xf1rsth45h')
        u.set_account_hash('0x53c0ndh45h')

        # A real response rather than a MagicMock: `extract_order_id`
        # starts with `if place_order_response.is_error`, and every attribute
        # of a MagicMock is truthy, so a mock takes the rejection path no
        # matter what is set on it.
        response = MockResponse(
                None, 201,
                {'Location':
                 'https://api.schwabapi.com/trader/v1/accounts/'
                 '0x53c0ndh45h/orders/123456789'})
        self.assertEqual(123456789, u.extract_order_id(response))


class UtilsTest(unittest.TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.account_hash = '0xacc0unth45h'
        self.utils = Utils(self.mock_client, self.account_hash)

        self.order_id = 1

        self.maxDiff = None

    ##########################################################################
    # extract_order_id tests

    @no_duplicates
    def test_extract_order_id_order_not_ok(self):
        # assertRaises' `msg` is the message printed when the *assertion*
        # fails, not a pattern the exception has to match -- so the version of
        # this test that passed `msg='order not successful'` asserted nothing
        # about the exception at all and would have passed on any wording.
        response = MockResponse({}, 403)
        with self.assertRaisesRegex(
                UnsuccessfulOrderException, 'order not successful: status 403'):
            self.utils.extract_order_id(response)

    @no_duplicates
    def test_a_rejection_carries_schwabs_own_explanation(self):
        # A status code does not distinguish a malformed order from one the
        # account cannot afford. Schwab types an error body as
        # {"message": ..., "errors": [...]}, and that is the only place the
        # reason appears.
        response = MockResponse(
                {'message': 'Order validation failed',
                 'errors': ['Insufficient buying power']}, 400)

        with self.assertRaises(UnsuccessfulOrderException) as cm:
            self.utils.extract_order_id(response)

        self.assertIn('Order validation failed', str(cm.exception))
        self.assertIn('Insufficient buying power', str(cm.exception))
        self.assertIs(response, cm.exception.response)

    @no_duplicates
    def test_a_rejection_with_no_readable_body_still_raises(self):
        # The formatter runs on the failure path, so anything it cannot read
        # has to yield no detail rather than an exception of its own --
        # replacing a useful error with a useless one is worse than terse.
        class Unreadable(MockResponse):
            def json(self):
                raise ValueError('not json')

        response = Unreadable({}, 500)
        with self.assertRaisesRegex(
                UnsuccessfulOrderException, 'order not successful: status 500'):
            self.utils.extract_order_id(response)

    @no_duplicates
    def test_a_long_explanation_is_bounded(self):
        # This lands in a log line. The whole body stays reachable on
        # .response for anyone who wants the rest of it.
        response = MockResponse({'message': 'x' * 5000}, 400)

        with self.assertRaises(UnsuccessfulOrderException) as cm:
            self.utils.extract_order_id(response)

        self.assertLess(len(str(cm.exception)), 700)
        self.assertIn('truncated', str(cm.exception))
        self.assertEqual(5000, len(cm.exception.response.json()['message']))

    @no_duplicates
    def test_no_location_header_raises_rather_than_returning_none(self):
        # Neither returns None, which is also what plenty of harmless things
        # return, so `if order_id:` would skip an order Schwab had very likely
        # placed.
        response = MockResponse({}, 200, headers={})

        with self.assertRaises(MissingLocationHeaderError) as cm:
            self.utils.extract_order_id(response)

        self.assertIsNone(cm.exception.location)
        self.assertIs(response, cm.exception.response)
        self.assertIn('may be live', str(cm.exception))

    @no_duplicates
    def test_unparsable_location_raises_and_carries_the_header(self):
        response = MockResponse({}, 200, headers={'Location': 'not-a-match'})

        with self.assertRaises(UnrecognizedLocationError) as cm:
            self.utils.extract_order_id(response)

        # The header is the whole of the evidence for a bug report.
        self.assertEqual('not-a-match', cm.exception.location)
        self.assertIn('not-a-match', str(cm.exception))

    @no_duplicates
    def test_both_are_catchable_as_one_thing(self):
        # A caller that just wants "I have no order id" should not have to name
        # both, and should not accidentally catch a rejection with them.
        for headers in ({}, {'Location': 'not-a-match'}):
            with self.assertRaises(OrderIdNotFoundError):
                self.utils.extract_order_id(MockResponse({}, 200, headers=headers))

        self.assertFalse(
                issubclass(UnsuccessfulOrderException, OrderIdNotFoundError))

    @no_duplicates
    def test_a_broad_except_valueerror_does_not_swallow_it(self):
        # The point of raising was that a live, untracked order must not be
        # silent. Inheriting ValueError would have handed that back: it is the
        # idiom people reach for around int() and float(), and order specs
        # coerce exactly those a few lines from this call.
        self.assertFalse(issubclass(OrderIdNotFoundError, ValueError))

        with self.assertRaises(OrderIdNotFoundError):
            try:
                self.utils.extract_order_id(MockResponse({}, 200, headers={}))
            except ValueError:                       # pragma: no cover
                self.fail('a broad except ValueError swallowed it')

    @no_duplicates
    def test_the_two_siblings_keep_valueerror(self):
        # They had it before SchwabError existed, and code catching them that
        # way predates this release. Dropping it would break that for no gain;
        # they describe a caller mistake, which is what ValueError means.
        self.assertTrue(issubclass(UnsuccessfulOrderException, ValueError))
        self.assertTrue(issubclass(AccountHashMismatchException, ValueError))

    @no_duplicates
    def test_schwab_error_covers_every_exception_the_library_defines(self):
        # A base that covers most of them is worse than none: it invites
        # `except SchwabError` as a complete guard and is quietly not one.
        #
        # Walked, not listed: a list of modules would miss an exception added
        # to any module outside it -- schwaby.debug, say -- and the test would
        # go on passing while the guarantee it states stopped being true.
        import importlib, inspect, pkgutil
        import schwaby

        # Seeded with `schwaby` itself: walk_packages yields only SUBmodules,
        # and schwaby/__init__.py already runs module-level code, so an
        # exception defined there would never be looked at. Verified by hand
        # rather than by mutation, because nothing in __init__.py raises today
        # so a mutation of the seed is green either way: with an exception
        # added there, the seeded walk fails and the unseeded one passes.
        #
        # onerror re-raises rather than defaulting to None, which silently
        # drops a subpackage out of the walk if one of its imports ever fails.
        # That one is defensive and cannot be exercised while every module
        # imports cleanly, which is the point of having it.
        def _boom(name):
            self.fail('could not import %s while walking' % name)

        modules, found, missing = ['schwaby'], {}, []
        for info in pkgutil.walk_packages(schwaby.__path__, 'schwaby.',
                                          onerror=_boom):
            modules.append(info.name)
        for name in modules:
            module = importlib.import_module(name)
            for attr, obj in vars(module).items():
                if (inspect.isclass(obj) and issubclass(obj, BaseException)
                        and obj.__module__ == name):
                    found['%s.%s' % (name, attr)] = obj
                    if not issubclass(obj, SchwabError):
                        missing.append('%s.%s' % (name, attr))

        # Controls for the walk itself, since an empty walk satisfies the
        # assertion below. Name specific classes from three separate modules
        # rather than counting: a count survives a whole module dropping out.
        self.assertGreater(len(modules), 8)
        for expected in ('schwaby.utils.OrderIdNotFoundError',
                         'schwaby.auth.RedirectTimeoutError',
                         'schwaby.streaming.ResponseTimeoutError',
                         'schwaby.orders.common.InvalidOrderException'):
            self.assertIn(expected, found)

        self.assertEqual([], missing)

    @no_duplicates
    def test_schwab_error_is_not_claimed_to_cover_bare_value_errors(self):
        # The library raises plain ValueError for argument validation in about
        # thirty places, so `except SchwabError` is NOT everything it can
        # throw. This pins the BEHAVIOUR; it does not read the docstring, so it
        # cannot stop the wording drifting back on its own -- the assertion
        # below does that part.
        from schwaby.orders.generic import OrderBuilder

        for label, bad in (('set_quantity(-1)',
                            lambda: OrderBuilder().set_quantity(-1)),
                           ('set_price(0.1)',
                            lambda: OrderBuilder().set_price(0.1))):
            with self.subTest(call=label):
                with self.assertRaises(ValueError) as cm:
                    bad()
                self.assertNotIsInstance(cm.exception, SchwabError)

        # Assert the positive statement rather than the absence of one
        # phrasing: "not everything" can be reworded a dozen ways, but the
        # docstring has to keep saying that builtin ValueError is still raised.
        # Skipped under -OO, where docstrings are stripped and __doc__ is None.
        if SchwabError.__doc__ is None:                  # pragma: no cover
            self.skipTest('docstrings stripped (-OO)')
        self.assertIn('ValueError', SchwabError.__doc__)
        self.assertIn('not', SchwabError.__doc__)

    @no_duplicates
    def test_every_exception_survives_a_process_boundary(self):
        # These carry the thing they are about as a leading positional and pass
        # only the message to BaseException, so the default reconstruction
        # called __init__ with the message alone: TypeError for most, and for
        # UnsuccessfulOrderException a copy that bound the message to
        # `response` and lost the message. Nothing in this library crosses a
        # process boundary with an exception; this is for callers who do -- a
        # ProcessPoolExecutor over placements -- where the one saying an order
        # is live on the wrong account must not arrive as a TypeError about
        # argument counts.
        import copy, importlib, inspect, pickle, pkgutil
        import schwaby

        r = PicklableResponse
        samples = {
            'SchwabError': ('m',),          # the base is a class too
            'UnexpectedResponse': (r(), 'm'),
            'UnexpectedResponseCode': (r(), 'm'),
            'UnparsableMessage': ('raw', ValueError('x'), 'm'),
            # ('m',) alone binds to the offending value, not to BaseException,
            # so str() would be '' and the message assertion below would
            # compare '' to '' and pass regardless.
            'UnusableMessage': ('frame', 'm'),
            'ResponseTimeoutError': ('svc', 'cmd', 60, 'm'),
            'UnsuccessfulOrderException': (r(), 'm'),
            'OrderIdNotFoundError': (r(), None, 'm'),
            'MissingLocationHeaderError': (r(), None, 'm'),
            'UnrecognizedLocationError': (r(), 'loc', 'm'),
            'AccountHashMismatchException': (r(), 123, 'BBBB', 'm'),
            'AccountNumberNotFoundError': ('m',),
            'UnusableAccountNumbersError': ('m',),
            'UnusableOrderActivityError': ('m',),
            'TokenRefreshError': ('m',),
            'LoginExchangeError': ('invalid_grant', 'code already used'),
            'RedirectTimeoutError': ('m',),
            'RedirectServerExitedError': ('m',),
            'InvalidOrderException': ('m',),
            'UnusableDecimalScale': ('m',),
            'UnparsableMessageData': ('m',),
        }

        # Seeded with `schwaby` for the same reason as the walk above: an
        # exception defined in schwaby/__init__.py is not a submodule, so it
        # would get no sample, never be round-tripped, and the count control
        # below would still pass because it counts only what the walk found.
        seen, modules = 0, ['schwaby']
        modules.extend(i.name for i in pkgutil.walk_packages(
                schwaby.__path__, 'schwaby.',
                onerror=lambda n: self.fail('could not import %s' % n)))

        for name in modules:
            module = importlib.import_module(name)
            for attr, obj in vars(module).items():
                if not (inspect.isclass(obj) and issubclass(obj, BaseException)
                        and obj.__module__ == name):
                    continue
                self.assertIn(attr, samples, 'new exception, add a sample')
                seen += 1
                exc = obj(*samples[attr])
                with self.subTest(exception=attr):
                    # pickle is the one that matters -- copy is in-process, so
                    # a test built only on it does not cross a boundary at all.
                    for rebuilt in (copy.copy(exc), copy.deepcopy(exc),
                                    pickle.loads(pickle.dumps(exc))):
                        self.assertIs(type(rebuilt), obj)
                        self.assertEqual(str(exc), str(rebuilt))
                        self.assertNotEqual('', str(rebuilt))

        # The walk found them, rather than the loop never running.
        self.assertEqual(len(samples), seen)

    @no_duplicates
    def test_the_attributes_survive_it_too(self):
        # A message that survives while .order_id does not would be the same
        # bug wearing a different face: the handler is told an order is live
        # and cannot reach it.
        import copy, pickle
        response = PicklableResponse('the original')
        exc = AccountHashMismatchException(
                response, 987, 'BBBB', 'm', expected_account_hash='AAAA')

        for rebuilt in (copy.copy(exc), pickle.loads(pickle.dumps(exc))):
            self.assertEqual(987, rebuilt.order_id)
            self.assertEqual('BBBB', rebuilt.account_hash)
            self.assertEqual('AAAA', rebuilt.expected_account_hash)
            # The historical defect was not a LOST response but a WRONG one --
            # the message bound into the response slot. assertIsNotNone cannot
            # see that; equality can.
            self.assertEqual(response, rebuilt.response)
            self.assertEqual('m', str(rebuilt))

    @no_duplicates
    def test_the_two_causes_are_distinguishable(self):
        # Without this, aliasing the two classes together passes every other
        # test here: each raise site names its own class, so both
        # assertRaises calls still match.
        self.assertIsNot(MissingLocationHeaderError, UnrecognizedLocationError)

        missing = MockResponse({}, 200, headers={})
        unparsable = MockResponse({}, 200, headers={'Location': 'nope'})

        # Catching one must not catch the other. A caller reconciling a
        # possibly-live order may want to treat a changed URL format -- which
        # is our bug -- differently from a header Schwab never sent.
        with self.assertRaises(MissingLocationHeaderError):
            self.utils.extract_order_id(missing)
        with self.assertRaises(UnrecognizedLocationError):
            try:
                self.utils.extract_order_id(unparsable)
            except MissingLocationHeaderError:      # pragma: no cover
                self.fail('unparsable Location raised the missing-header type')

    @no_duplicates
    def test_get_order_nonmatching_account_hash(self):
        response = MockResponse({}, 200, headers={
            'Location':
            'https://api.schwabapi.com/trader/v1/accounts/badhash/orders/123'})

        with self.assertRaises(AccountHashMismatchException) as cm:
            self.utils.extract_order_id(response)

        # This fires only after the response came back successful AND an order
        # id parsed out of it, so an order really was placed -- on an account
        # the caller was not expecting to trade. Everything needed to go and
        # cancel it is on the exception rather than left in the message for
        # somebody to re-derive with a regex.
        self.assertEqual(123, cm.exception.order_id)
        self.assertEqual('badhash', cm.exception.account_hash)
        # Both hashes: a handler far from the call site has no Utils left to
        # ask which account it meant, and should not have to parse the message.
        self.assertEqual(self.utils.account_hash,
                         cm.exception.expected_account_hash)
        self.assertNotEqual(cm.exception.account_hash,
                            cm.exception.expected_account_hash)
        self.assertIs(response, cm.exception.response)
        self.assertIn('is live', str(cm.exception))

    @no_duplicates
    def test_the_mismatch_says_the_order_exists(self):
        # The old message was "order request account hash != Utils.account_hash",
        # and the docstring called it a wiring problem "rather than the order".
        # Both read as a configuration complaint. An order had been placed.
        response = MockResponse({}, 200, headers={
            'Location':
            'https://api.schwabapi.com/trader/v1/accounts/badhash/orders/123'})

        with self.assertRaises(AccountHashMismatchException) as cm:
            self.utils.extract_order_id(response)

        message = str(cm.exception)
        self.assertIn('123', message)        # which order
        self.assertIn('badhash', message)    # on which account

    @no_duplicates
    def test_get_order_success_200(self):
        order_id = 123456
        response = MockResponse({}, 200, headers={
            'Location':
            'https://api.schwabapi.com/trader/v1/accounts/{}/orders/{}'.format(
                self.account_hash, order_id)})
        self.assertEqual(order_id, self.utils.extract_order_id(response))

    @no_duplicates
    def test_get_order_success_201(self):
        order_id = 123456
        response = MockResponse({}, 201, headers={
            'Location':
            'https://api.schwabapi.com/trader/v1/accounts/{}/orders/{}'.format(
                self.account_hash, order_id)})
        self.assertEqual(order_id, self.utils.extract_order_id(response))


from schwaby.utils import (
        AccountNumberNotFoundError, SchwabError, UnusableAccountNumbersError,
        find_account_hash)


class FindAccountHashTest(unittest.TestCase):

    ACCOUNTS = [{'accountNumber': '11111111', 'hashValue': 'HASH-ONE'},
                {'accountNumber': '022222222', 'hashValue': 'HASH-TWO'}]

    @no_duplicates
    def test_the_hash_is_the_account_asked_for_not_the_first(self):
        self.assertEqual('HASH-TWO',
                         find_account_hash(self.ACCOUNTS, '022222222'))
        self.assertEqual('HASH-ONE',
                         find_account_hash(self.ACCOUNTS, '11111111'))

    @no_duplicates
    def test_an_account_number_that_is_not_a_str_is_refused(self):
        # As an int, 022222222 is 22222222, which matches nothing and would
        # read as "not linked" rather than as a mistake.
        for number in (22222222, 11111111, 11111111.0, None, b'11111111'):
            with self.subTest(number=number):
                with self.assertRaisesRegex(TypeError, 'must be a str') as cm:
                    find_account_hash(self.ACCOUNTS, number)
                self.assertNotIn('1111111', str(cm.exception))
                self.assertNotIn('2222222', str(cm.exception))

    @no_duplicates
    def test_an_account_number_that_is_not_ascii_digits_is_refused(self):
        # Each of these would otherwise be reported as an account the token
        # does not cover: a number read from a file with its line ending or
        # byte order mark, an unset environment variable, a typo.
        for number in ('11111111\n', ' 11111111', '11111111 ', '11111111\r',
                       '\t11111111', '11111111\xa0', '\ufeff11111111',
                       '11111111\u200b', '11111111\x00', '', '1111-1111',
                       'ABC11111',
                       # Digits, but not ASCII ones.
                       '\u0661\u0661\u0661\u0661\u0661\u0661\u0661\u0661'):
            with self.subTest(number=number):
                with self.assertRaisesRegex(ValueError, 'ASCII digits') as cm:
                    find_account_hash(self.ACCOUNTS, number)
                self.assertNotIn('1111', str(cm.exception))

    @no_duplicates
    def test_a_str_subclass_is_read_as_its_text(self):
        class Number(str):
            def __eq__(self, other):
                return False
            __hash__ = str.__hash__

        self.assertEqual('HASH-ONE',
                         find_account_hash(self.ACCOUNTS, Number('11111111')))

    @no_duplicates
    def test_an_unlinked_account_is_its_own_error(self):
        with self.assertRaises(AccountNumberNotFoundError) as cm:
            find_account_hash(self.ACCOUNTS, '33333333')
        self.assertIsInstance(cm.exception, SchwabError)
        self.assertNotIsInstance(cm.exception, UnusableAccountNumbersError)
        self.assertNotIn('33333333', str(cm.exception))

    @no_duplicates
    def test_an_account_listed_twice_is_refused(self):
        twice = self.ACCOUNTS + [
                {'accountNumber': '11111111', 'hashValue': 'HASH-OTHER'}]
        with self.assertRaises(UnusableAccountNumbersError) as cm:
            find_account_hash(twice, '11111111')
        self.assertNotIsInstance(cm.exception, AccountNumberNotFoundError)
        self.assertNotIn('11111111', str(cm.exception))

    @no_duplicates
    def test_a_response_that_is_not_the_account_list_is_refused(self):
        # Every entry is checked, including those after the one that matches.
        # Each unreadable entry here could be the account asked for.
        good = {'accountNumber': '11111111', 'hashValue': 'HASH-ONE'}
        for accounts in (good, None, 'HASH-ONE',
                         [good, None],
                         [good, ['33333333', 'HASH-THREE']],
                         [good, {'accountNumber': 33333333, 'hashValue': 'H'}],
                         [good, {'accountNumber': None, 'hashValue': 'H'}],
                         [good, {'hashValue': 'H'}],
                         # The account asked for, with a hash that cannot be
                         # used, before or after a readable duplicate.
                         [good, {'accountNumber': '11111111',
                                 'hashValue': None}],
                         [{'accountNumber': '11111111', 'hashValue': ''},
                          good],
                         # And with no readable duplicate to trip the
                         # duplicate check instead.
                         [{'accountNumber': '11111111', 'hashValue': ''},
                          {'accountNumber': '33333333', 'hashValue': 'H'}],
                         # Not ASCII digits, so it could be this number as a
                         # file or a person wrote it.
                         [good, {'accountNumber': ' 11111111',
                                 'hashValue': None}],
                         [good, {'accountNumber': 'ABC33333'}],
                         # Digits, but not ASCII ones: isdigit() alone would
                         # take these for another account and skip them.
                         [good, {'accountNumber': '\uff11' * 8,
                                 'hashValue': None}],
                         [good, {'accountNumber': '\u0661' * 8}]):
            with self.subTest(accounts=accounts):
                with self.assertRaises(UnusableAccountNumbersError) as cm:
                    find_account_hash(accounts, '11111111')
                self.assertNotIn('11111111', str(cm.exception))
                self.assertNotIn('33333333', str(cm.exception))

    @no_duplicates
    def test_an_unusable_entry_for_another_account_is_skipped_once_reported(
            self):
        # An odd linked or closed account must not stop a lookup for a clean
        # one, and it is worth seeing once rather than on every call.
        import logging
        import schwaby.utils as utils_module
        utils_module._reported_account_entry_problems.clear()
        self.addCleanup(utils_module._reported_account_entry_problems.clear)
        # The logger by name, as the docs give it, not whatever get_logger
        # returns.
        logger = logging.getLogger('schwaby.utils')

        class Text(str):
            pass

        good = {'accountNumber': '11111111', 'hashValue': 'HASH-ONE'}
        for other, problem in (
                ({'accountNumber': '33333333'}, 'has no hashValue'),
                ({'accountNumber': '33333333', 'hashValue': None},
                 'not a string'),
                ({'accountNumber': '33333333', 'hashValue': 7},
                 'not a string'),
                ({'accountNumber': '033333333', 'hashValue': ''},
                 'has an empty hashValue'),
                ({'accountNumber': '33333333', 'hashValue': Text('')},
                 'has an empty hashValue')):
            with self.subTest(other=other):
                utils_module._reported_account_entry_problems.clear()
                with self.assertLogs(logger, level='WARNING') as cm:
                    self.assertEqual('HASH-ONE',
                                     find_account_hash([other, good],
                                                       '11111111'))
                self.assertEqual(['WARNING'],
                                 [r.levelname for r in cm.records])
                logged = '\n'.join(cm.output)
                self.assertIn(problem, logged)
                self.assertNotIn('11111111', logged)
                self.assertNotIn('3333333', logged)
                # Once per kind: the same list again logs nothing.
                with self.assertNoLogs(logger, level='WARNING'):
                    self.assertEqual('HASH-ONE',
                                     find_account_hash([good, other],
                                                       '11111111'))

    @no_duplicates
    def test_an_entry_str_subclass_is_read_as_its_text(self):
        class Liar(str):
            def __eq__(self, other):
                return True
            __hash__ = str.__hash__

        accounts = [{'accountNumber': Liar('22222222'), 'hashValue': 'HASH-B'},
                    {'accountNumber': '33333333', 'hashValue': 'HASH-C'}]
        with self.assertRaises(AccountNumberNotFoundError):
            find_account_hash(accounts, '11111111')

    @no_duplicates
    def test_an_entry_dict_subclass_is_read_through_dict_itself(self):
        class Lying(dict):
            def get(self, key, default=None):
                return '11111111' if key == 'accountNumber' else dict.get(
                        self, key, default)

            def __getitem__(self, key):
                return self.get(key)

        accounts = [Lying(accountNumber='22222222', hashValue='HASH-B')]
        with self.assertRaises(AccountNumberNotFoundError):
            find_account_hash(accounts, '11111111')

    @no_duplicates
    def test_a_hash_read_through_dict_itself_too(self):
        class Lying(dict):
            def get(self, key, default=None):
                return 'HASH-OTHER' if key == 'hashValue' else dict.get(
                        self, key, default)

            def __getitem__(self, key):
                return self.get(key)

        accounts = [Lying(accountNumber='11111111', hashValue='HASH-A')]
        self.assertEqual('HASH-A', find_account_hash(accounts, '11111111'))

    @no_duplicates
    def test_a_faked_class_is_not_taken_for_the_real_type(self):
        class FakeDict:
            __class__ = dict

        class FakeStr:
            __class__ = str

        class FakeList:
            __class__ = list

        for accounts in (FakeList(), [FakeDict()],
                         [{'accountNumber': FakeStr(), 'hashValue': 'H'}],
                         [{'accountNumber': '11111111', 'hashValue': FakeStr()}]):
            with self.subTest(accounts=accounts):
                with self.assertRaises(UnusableAccountNumbersError):
                    find_account_hash(accounts, '11111111')
        with self.assertRaisesRegex(TypeError, 'must be a str'):
            find_account_hash(self.ACCOUNTS, FakeStr())

    @no_duplicates
    def test_a_list_subclass_cannot_put_a_count_in_the_message(self):
        class Counting(list):
            def __len__(self):
                return 11111111

        with self.assertRaises(AccountNumberNotFoundError) as cm:
            find_account_hash(Counting(self.ACCOUNTS), '33333333')
        self.assertNotIn('11111111', str(cm.exception))
        self.assertIn('none of the 2 accounts', str(cm.exception))

    @no_duplicates
    def test_a_type_name_is_read_without_trusting_a_metaclass(self):
        class Meta(type):
            @property
            def __name__(cls):
                raise RuntimeError('metaclass raised')

        class Odd(metaclass=Meta):
            pass

        with self.assertRaisesRegex(TypeError, 'must be a str') as cm:
            find_account_hash(self.ACCOUNTS, Odd())
        # The real name, read past the metaclass, not the fallback's 'object'.
        self.assertIn('not a Odd', str(cm.exception))

    @no_duplicates
    def test_the_hash_returned_is_a_plain_str(self):
        class Named(str):
            def __str__(self):
                return 'HASH-B'

        class Padded(str):
            def __len__(self):
                return 8

        result = find_account_hash(
                [{'accountNumber': '11111111', 'hashValue': Named('HASH-A')}],
                '11111111')
        self.assertEqual('HASH-A', result)
        self.assertIs(str, type(result))
        with self.assertRaises(UnusableAccountNumbersError):
            find_account_hash(
                    [{'accountNumber': '11111111', 'hashValue': Padded('')}],
                    '11111111')

    @no_duplicates
    def test_a_list_subclass_is_read_through_list_itself(self):
        class Hiding(list):
            def __iter__(self):
                return iter(list.__getitem__(self, slice(0, 1)))

        accounts = Hiding([
                {'accountNumber': '11111111', 'hashValue': 'HASH-A'},
                {'accountNumber': '11111111', 'hashValue': 'HASH-B'}])
        with self.assertRaises(UnusableAccountNumbersError):
            find_account_hash(accounts, '11111111')


from schwaby.utils import (
        ExecutionTotal, UnusableOrderActivityError, execution_totals)


class ExecutionTotalsTest(unittest.TestCase):
    """Per-leg fill totals from an order's activity, in the shapes measured
    read-only on real orders: ETF orders filled in one or two executions, a
    two-leg vertical in one execution, and canceled or replaced orders whose
    CANCELED execution legs carry quantities never filled."""

    @staticmethod
    def leg(leg_id, quantity, price, **extra):
        leg = {'legId': leg_id, 'quantity': quantity,
               'mismarkedQuantity': 0.0, 'price': price,
               'time': '2026-09-01T14:30:00+0000', 'instrumentId': 7}
        leg.update(extra)
        return leg

    @staticmethod
    def activity(execution_type, *legs, quantity=None):
        return {'activityType': 'EXECUTION', 'activityId': 1,
                'executionType': execution_type,
                'quantity': (quantity if quantity is not None else sum(
                    l['quantity'] for l in legs if type(l) is dict)),
                'orderRemainingQuantity': 0.0, 'executionLegs': list(legs)}

    @staticmethod
    def order(*activities, **fields):
        order = {'status': 'FILLED', 'orderActivityCollection': list(activities)}
        order.update(fields)
        return order

    @no_duplicates
    def test_one_fill_is_its_quantity_and_price(self):
        totals = execution_totals(self.order(
                self.activity('FILL', self.leg(1, 5.0, 58.1853))))
        self.assertEqual({1: ExecutionTotal(decimal.Decimal('5'),
                                            decimal.Decimal('58.1853'))},
                         totals)
        self.assertIs(decimal.Decimal, type(totals[1].quantity))
        self.assertIs(decimal.Decimal, type(totals[1].average_price))

    @no_duplicates
    def test_two_executions_average_by_quantity(self):
        totals = execution_totals(self.order(
                self.activity('FILL', self.leg(1, 3.0, 58.10)),
                self.activity('FILL', self.leg(1, 2.0, 58.20))))
        self.assertEqual(decimal.Decimal('5'), totals[1].quantity)
        self.assertEqual(decimal.Decimal('58.14'), totals[1].average_price)

    @no_duplicates
    def test_a_canceled_execution_is_not_a_fill(self):
        canceled = self.order(
                self.activity('CANCELED', self.leg(1, 10.0, 58.0)),
                status='CANCELED', filledQuantity=0.0)
        self.assertEqual({}, execution_totals(canceled))

        # Positive control: beside a fill, only the fill counts.
        mixed = self.order(self.activity('FILL', self.leg(1, 2.0, 10.0)),
                           self.activity('CANCELED', self.leg(1, 8.0, 10.0)))
        self.assertEqual(decimal.Decimal('2'),
                         execution_totals(mixed)[1].quantity)

    @no_duplicates
    def test_an_activity_that_is_not_an_execution_is_skipped(self):
        other = {'activityType': 'ORDER_ACTION', 'executionLegs': None}
        order = self.order(other, self.activity('FILL', self.leg(1, 1.0, 1.0)))
        self.assertEqual([1], list(execution_totals(order)))

    @no_duplicates
    def test_a_two_leg_order_is_totalled_per_leg(self):
        # One activity carrying one execution leg per leg, its quantity counting
        # spreads rather than the sum across the legs.
        order = self.order(self.activity(
                'FILL', self.leg(1, 1.0, 2.50), self.leg(2, 1.0, 1.10),
                quantity=1.0))
        totals = execution_totals(order)
        self.assertEqual({1, 2}, set(totals))
        self.assertEqual(decimal.Decimal('2.50'), totals[1].average_price)
        self.assertEqual(decimal.Decimal('1.10'), totals[2].average_price)

    @no_duplicates
    def test_an_order_with_no_fills_gives_an_empty_dict(self):
        self.assertEqual({}, execution_totals({'status': 'WORKING'}))
        self.assertEqual({}, execution_totals(self.order()))

    @no_duplicates
    def test_a_float_is_read_from_its_text(self):
        # From the binary expansion, 0.1 and 0.2 would not average to 0.15.
        totals = execution_totals(self.order(
                self.activity('FILL', self.leg(1, 1.0, 0.1)),
                self.activity('FILL', self.leg(1, 1.0, 0.2))))
        self.assertEqual('0.15', str(totals[1].average_price))

    @no_duplicates
    def test_a_leg_whose_fills_add_up_to_nothing_has_no_average_price(self):
        totals = execution_totals(self.order(
                self.activity('FILL', self.leg(1, 0.0, 58.1853))))
        self.assertEqual(ExecutionTotal(decimal.Decimal('0'), None), totals[1])

    @no_duplicates
    def test_a_missing_mismarked_quantity_counts_as_zero(self):
        leg = self.leg(1, 1.0, 1.0)
        del leg['mismarkedQuantity']
        self.assertEqual([1], list(execution_totals(
                self.order(self.activity('FILL', leg)))))

    @no_duplicates
    def test_a_null_mismarked_quantity_counts_as_zero_and_a_negative_is_not(
            self):
        leg = self.leg(1, 1.0, 1.0, mismarkedQuantity=None)
        self.assertEqual([1], list(execution_totals(
                self.order(self.activity('FILL', leg)))))
        with self.assertRaisesRegex(
                UnusableOrderActivityError, 'mismarkedQuantity'):
            execution_totals(self.order(self.activity(
                    'FILL', self.leg(1, 1.0, 1.0, mismarkedQuantity=-1.0))))

    @no_duplicates
    def test_decimal_numbers_give_the_same_totals(self):
        # A caller parsing with parse_float=Decimal sends Decimal, not float.
        import json
        text = json.dumps(self.order(
                self.activity('FILL', self.leg(1, 3.0, 58.10)),
                self.activity('FILL', self.leg(1, 2.0, 58.20))))
        as_float = execution_totals(json.loads(text))
        as_decimal = execution_totals(
                json.loads(text, parse_float=decimal.Decimal))
        self.assertEqual(as_float, as_decimal)
        self.assertEqual(decimal.Decimal('58.14'), as_decimal[1].average_price)

    @no_duplicates
    def test_an_empty_string_is_not_an_absent_collection(self):
        with self.assertRaisesRegex(UnusableOrderActivityError, 'not a list'):
            execution_totals({'orderActivityCollection': ''})

    @no_duplicates
    def test_the_callers_decimal_context_does_not_change_the_totals(self):
        order = self.order(
                self.activity('FILL', self.leg(1, 12345.0, 58.1853)),
                self.activity('FILL', self.leg(1, 1.0, 58.19)))
        expected = execution_totals(order)
        self.assertEqual(decimal.Decimal('12346'), expected[1].quantity)
        with decimal.localcontext(decimal.Context(
                prec=4, traps=[decimal.Inexact, decimal.Rounded])):
            self.assertEqual(expected, execution_totals(order))

    @no_duplicates
    def test_a_number_too_large_to_add_up_is_refused(self):
        huge = decimal.Decimal('1e999990')
        with self.assertRaisesRegex(UnusableOrderActivityError, 'too large'):
            execution_totals(self.order(self.activity(
                    'FILL', self.leg(1, huge, huge), quantity=1.0)))

    @no_duplicates
    def test_a_sum_is_exact_or_refused(self):
        # A real order is nowhere near 60 digits. Past them, or below the
        # smallest exponent, a total would be rounded, so it is refused.
        exact = decimal.Decimal(10 ** 58 + 1)
        totals = execution_totals(self.order(self.activity(
                'FILL', self.leg(1, exact, 1.0), quantity=1.0)))
        self.assertEqual(ExecutionTotal(exact, decimal.Decimal(1)), totals[1])
        for name, quantity, price in (
                ('too long', decimal.Decimal(10 ** 70 + 1), 1.0),
                ('too small', decimal.Decimal('1e-999990'),
                 decimal.Decimal('1e-999990'))):
            with self.subTest(name):
                with self.assertRaisesRegex(
                        UnusableOrderActivityError, 'add up exactly'):
                    execution_totals(self.order(self.activity(
                            'FILL', self.leg(1, quantity, price),
                            quantity=1.0)))

    @no_duplicates
    def test_the_average_is_divided_to_60_digits(self):
        totals = execution_totals(self.order(
                self.activity('FILL', self.leg(1, 1.0, 2.0)),
                self.activity('FILL', self.leg(1, 2.0, 0.0))))
        self.assertEqual(60, len(totals[1].average_price.as_tuple().digits))
        # Two thirds ends in a 7 under any rounding to nearest, which catches
        # truncation; only a tie tells half-even from half-up.
        self.assertEqual('0.' + '6' * 59 + '7', str(totals[1].average_price))
        half = decimal.Decimal('1' + '0' * 59 + '.5')
        tie = execution_totals(self.order(self.activity(
                'FILL', self.leg(1, 2.0, half), quantity=1.0)))
        self.assertEqual(decimal.Decimal(10 ** 59), tie[1].average_price)

    @no_duplicates
    def test_an_average_too_large_or_small_to_hold_is_refused(self):
        # Prices of opposite sign can cancel to an amount far smaller than
        # either, and dividing it would round to zero; a tiny quantity at a
        # huge price divides past the largest exponent.
        huge = decimal.Decimal('1E+999998')
        up = decimal.Decimal('1.' + '0' * 58 + '1E-1000098')
        down = decimal.Decimal('-1E-1000098')
        tiny = decimal.Decimal('1E-100')
        vast = decimal.Decimal('1E+1000050')
        for name, activities in (
                ('too small', (
                    self.activity('FILL', self.leg(1, huge, up), quantity=1.0),
                    self.activity(
                        'FILL', self.leg(1, huge, down), quantity=1.0))),
                ('too large', (self.activity(
                    'FILL', self.leg(1, tiny, vast), quantity=1.0),))):
            with self.subTest(name):
                with self.assertRaisesRegex(
                        UnusableOrderActivityError, 'add up exactly'):
                    execution_totals(self.order(*activities))

    @no_duplicates
    def test_subclasses_are_read_through_the_built_in_types(self):
        # A custom JSON decoder can hand back subclasses, and what is checked
        # must be what is counted.
        class Hiding(list):
            def __iter__(self):
                return iter(())

        class Lying(str):
            def __ne__(self, other):
                return False

            def __eq__(self, other):
                return True

            __hash__ = str.__hash__

        class Noisy(float):
            def __repr__(self):
                return '999.0'

        class Forgetful(dict):
            def get(self, key, default=None):
                return None

        fill = self.activity('FILL', self.leg(1, 1.0, 10.0))
        for name, order in (
                ('activities', {'orderActivityCollection': Hiding([fill])}),
                ('legs', self.order(dict(
                    fill, executionLegs=Hiding(fill['executionLegs'])))),
                ('order', Forgetful(self.order(fill))),
                ('activity', self.order(Forgetful(fill))),
                ('leg', self.order(dict(fill, executionLegs=[
                    Forgetful(fill['executionLegs'][0])])))):
            with self.subTest(read_through=name):
                self.assertEqual(decimal.Decimal('1'),
                                 execution_totals(order)[1].quantity)

        # A str subclass cannot pass another activity off as a fill.
        for field, value in (('activityType', Lying('ORDER_ACTION')),
                             ('executionType', Lying('CANCELED'))):
            with self.subTest(field=field):
                other = self.activity('FILL', self.leg(1, 5.0, 10.0))
                other[field] = value
                totals = execution_totals(self.order(fill, other))
                self.assertEqual(decimal.Decimal('1'), totals[1].quantity)

        # A float subclass is read through float's own repr.
        totals = execution_totals(self.order(
                self.activity('FILL', self.leg(1, 1.0, Noisy(58.1)))))
        self.assertEqual(decimal.Decimal('58.1'), totals[1].average_price)

        # A mismarked quantity the leg's get hides is still refused.
        hiding = Forgetful(self.leg(1, 1.0, 1.0, mismarkedQuantity=3.0))
        with self.assertRaisesRegex(
                UnusableOrderActivityError, 'mismarkedQuantity'):
            execution_totals(self.order(self.activity('FILL', hiding)))

        # An int subclass is read by its value, not through its overrides.
        class Twisted(int):
            def __int__(self):
                return 7

            __index__ = __int__

            def __str__(self):
                return '7'

        totals = execution_totals(self.order(self.activity(
                'FILL', self.leg(1, Twisted(2), Twisted(3)), quantity=1.0)))
        self.assertEqual(
                ExecutionTotal(decimal.Decimal(2), decimal.Decimal(3)),
                totals[1])

        # The leg id comes back a plain int.
        class LegId(int):
            pass
        totals = execution_totals(self.order(
                self.activity('FILL', self.leg(LegId(1), 1.0, 1.0))))
        self.assertIs(int, type(next(iter(totals))))

    @no_duplicates
    def test_a_shape_it_cannot_read_is_refused(self):
        leg, activity, order = self.leg, self.activity, self.order
        for bad, message in (
                (None, 'expected one parsed order'),
                ([], 'expected one parsed order'),
                ({'orderActivityCollection': 'x'}, 'not a list'),
                (order(5), 'activity 1 is not an object'),
                (order({'executionLegs': []}), 'activityType'),
                (order({'activityType': 'EXECUTION'}), 'executionType'),
                (order({'activityType': 'EXECUTION',
                        'executionType': 'FILL'}), 'executionLegs'),
                (order(activity('FILL', 5)), 'execution leg 1 is not'),
                (order(activity('FILL', leg(None, 1.0, 1.0))), 'legId'),
                (order(activity('FILL', leg(True, 1.0, 1.0))), 'legId'),
                (order(activity('FILL', leg('1', 1.0, 1.0))), 'legId'),
                (order(activity('FILL', leg(1, None, 1.0), quantity=1.0)),
                 'quantity'),
                (order(activity('FILL', leg(1, '5', 1.0), quantity=1.0)),
                 'quantity'),
                (order(activity('FILL', leg(1, True, 1.0), quantity=1.0)),
                 'quantity'),
                (order(activity('FILL', leg(1, float('nan'), 1.0),
                                quantity=1.0)), 'quantity'),
                (order(activity('FILL', leg(1, float('inf'), 1.0),
                                quantity=1.0)), 'quantity'),
                (order(activity('FILL', leg(1, -1.0, 1.0))),
                 'negative quantity'),
                (order(activity('FILL', leg(1, 1.0, None))), 'price'),
                (order(activity('FILL', leg(1, 1.0, float('nan')))), 'price'),
                (order(activity('FILL', leg(1, 1.0, 58.1853,
                                            mismarkedQuantity=1.0))),
                 'mismarkedQuantity')):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(
                        UnusableOrderActivityError, message) as cm:
                    execution_totals(bad)
                self.assertIsInstance(cm.exception, ValueError)
                self.assertNotIn('58.1853', str(cm.exception))

    @no_duplicates
    def test_a_faked_class_is_not_taken_for_the_real_type(self):
        class FakeDict:
            __class__ = dict

        class FakeList:
            __class__ = list

        for bad in (FakeDict(), {'orderActivityCollection': FakeList()},
                    self.order(FakeDict()),
                    self.order(self.activity('FILL', FakeDict()))):
            with self.subTest(bad=bad):
                with self.assertRaises(UnusableOrderActivityError):
                    execution_totals(bad)


class HostileClassTest(unittest.TestCase):
    '''A value whose class makes ``__class__`` raise is refused exactly as an
    ordinary instance of the same class is. ``isinstance`` asks ``__class__``
    when the value's real type does not match, so a type check on a caller's
    value let that exception through in place of the refusal.'''

    class Plain:
        def __float__(self):
            return 10.0

    class Hostile(Plain):
        @property
        def __class__(self):
            raise RuntimeError('hostile __class__')

    @staticmethod
    def outcome(call, value):
        try:
            call(value)
        except Exception as e:
            return type(e)
        return None

    @no_duplicates
    def test_a_class_that_raises_is_refused_like_any_other_instance(self):
        import datetime
        from unittest.mock import MagicMock
        from schwaby import auth
        from schwaby.client import Client
        from schwaby.orders.common import Duration
        from schwaby.orders.generic import OrderBuilder
        from schwaby.orders.options import OptionSymbol
        from schwaby.streaming import StreamClient

        # Not place_order and its siblings: an order that is neither a builder
        # nor JSON fails in JSON encoding, which reads __class__ itself.
        client = Client('api-key', MagicMock(), token_metadata=MagicMock())
        enforcer = EnumEnforcer(True)
        # A module-level double-underscore name, read without the mangling a
        # class body would apply.
        normalize = getattr(auth, '__normalize_credential')
        expiry = datetime.date(2027, 1, 15)
        projection = Client.Instrument.Projection.SYMBOL_SEARCH
        calls = (
            ('convert_enum', lambda v: enforcer.convert_enum(v, Duration)),
            ('convert_enum_iterable',
             lambda v: enforcer.convert_enum_iterable(v, Duration)),
            ('convert_enum_iterable member',
             lambda v: enforcer.convert_enum_iterable([v], Duration)),
            ('a datetime argument',
             lambda v: client.get_orders_for_account(
                 'hash', from_entered_datetime=v)),
            ('get_quotes', lambda v: client.get_quotes(v)),
            ('get_instruments',
             lambda v: client.get_instruments(v, projection)),
            ('get_instrument_by_cusip', client.get_instrument_by_cusip),
            ('set_quantity', lambda v: OrderBuilder().set_quantity(v)),
            ('set_price', lambda v: OrderBuilder().set_price(v)),
            ('set_cancel_time', lambda v: OrderBuilder().set_cancel_time(v)),
            ('copy_price and build',
             lambda v: OrderBuilder().copy_price(v).build()),
            ('add_child_order_strategy',
             lambda v: OrderBuilder().add_child_order_strategy(v)),
            ('OptionSymbol expiration',
             lambda v: OptionSymbol('F', v, 'C', '10')),
            ('OptionSymbol strike',
             lambda v: OptionSymbol('F', expiry, 'C', v)),
            ('an app key', lambda v: normalize(v, 'api_key')),
            ('set_json_decoder',
             lambda v: StreamClient(MagicMock()).set_json_decoder(v)),
        )
        for name, call in calls:
            with self.subTest(name):
                ordinary = self.outcome(call, self.Plain())
                self.assertIsNot(RuntimeError, ordinary)
                self.assertEqual(ordinary, self.outcome(call, self.Hostile()))

        # A subclass of an accepted type passes the first check on its real
        # type, so the checks after it see the value too: the date
        # formatters, and set_json_decoder's abstract base, whose
        # isinstance reads __class__ even for a real subclass.
        from schwaby.contrib.util import StreamJsonDecoder

        class PlainDate(datetime.date):
            pass

        class HostileDate(PlainDate):
            @property
            def __class__(self):
                raise RuntimeError('hostile __class__')

        class PlainDecoder(StreamJsonDecoder):
            def decode_json_string(self, raw):
                return {}

        class HostileDecoder(PlainDecoder):
            @property
            def __class__(self):
                raise RuntimeError('hostile __class__')

        subclass_calls = (
            ('get_transactions dates', PlainDate, HostileDate,
             lambda v: client.get_transactions(
                 'hash', start_date=v, end_date=datetime.date(2027, 1, 2))),
            ('get_orders_for_account date', PlainDate, HostileDate,
             lambda v: client.get_orders_for_account(
                 'hash', from_entered_datetime=v)),
            ('set_json_decoder subclass', PlainDecoder, HostileDecoder,
             lambda v: StreamClient(MagicMock()).set_json_decoder(v)),
        )
        for name, plain, hostile, call in subclass_calls:
            with self.subTest(name):
                value = (lambda cls: cls(2027, 1, 1)) if plain is PlainDate \
                        else (lambda cls: cls())
                ordinary = self.outcome(call, value(plain))
                self.assertIsNone(ordinary)
                self.assertEqual(ordinary, self.outcome(call, value(hostile)))

    @no_duplicates
    def test_a_mock_with_a_spec_still_passes(self):
        # The real type answers only when isinstance raises: a spec'd mock
        # reports the class it imitates, and callers' tests rely on it.
        import datetime
        from unittest.mock import MagicMock
        from schwaby.utils import _is_instance
        self.assertTrue(_is_instance(
                MagicMock(spec=datetime.datetime), datetime.datetime))
        self.assertTrue(_is_instance(self.Hostile(), self.Plain))
        self.assertFalse(_is_instance(self.Hostile(), str))
