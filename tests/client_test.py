import decimal
import asyncio
import datetime
import httpx2
import inspect
import logging
import os
from types import SimpleNamespace
import pytest
import pytz
import sys
import time
import unittest
import warnings
from unittest.mock import ANY, MagicMock, Mock, patch

from authlib.integrations.base_client.errors import OAuthError
import schwaby.client.base
from schwaby.client import AsyncClient, Client
from schwaby.orders.generic import OrderBuilder
from schwaby.utils import TokenRefreshError

from .utils import (AsyncMagicMock, MockResponse, ResyncProxy,
                    no_duplicates)

# Constants

API_KEY = '1234567890'
ACCOUNT_ID = 100000
ACCOUNT_HASH = '0x0x0x0x10000'
ORDER_ID = 200000
SAVED_ORDER_ID = 300000
CUSIP = '000919239'
MARKET = 'EQUITY'
INDEX = '$SPX.X'
SYMBOL = 'AAPL'
TRANSACTION_ID = 400000
WATCHLIST_ID = 5000000

# The library's default start date, which is UTC. A naive value here and in
# the library would both be read as local time and agree, so the expected
# value would depend on the timezone of whatever machine ran the suite.
# Pinning it to UTC on both sides makes the assertion mean the same thing
# everywhere.
MIN_DATETIME = datetime.datetime(year=1971, month=1, day=1,
                                 tzinfo=datetime.timezone.utc)
MIN_ISO = '1971-01-01T00:00:00+0000'
MIN_TIMESTAMP_MILLIS = int(MIN_DATETIME.timestamp()) * 1000

NOW_DATETIME = datetime.datetime(2020, 1, 2, 3, 4, 5)
NOW_DATE = datetime.date(2020, 1, 2)
NOW_DATETIME_ISO = '2020-01-02T03:04:05Z'
NOW_DATETIME_TRUNCATED_ISO = '2020-01-02T00:00:00Z'
NOW_DATE_ISO = '2020-01-02'

NOW_DATETIME_MINUS_60_DAYS = NOW_DATE - datetime.timedelta(days=60)
NOW_DATETIME_MINUS_60_DAYS_ISO = '2019-11-03T03:04:05Z'

NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS = \
        int((NOW_DATETIME + datetime.timedelta(days=7)).timestamp()) * 1000


class mockdatetime(datetime.datetime):
    @classmethod
    def utcnow(cls):
        return NOW_DATETIME

    @classmethod
    def now(cls, timezone):
        return NOW_DATETIME


EARLIER_DATETIME = datetime.datetime(2001, 1, 2, 3, 4, 5,
                                     tzinfo=pytz.timezone('America/New_York'))
EARLIER_ISO = '2001-01-02T03:04:05-0456'
EARLIER_MILLIS = 978422405000
EARLIER_DATE_STR = '2001-01-02'

class _TestClient:
    """
    Test suite used for both Client and AsyncClient
    """

    def setUp(self):
        self.mock_session = self.magicmock_class()
        self.client = self.client_class(API_KEY, self.mock_session)

        # Set the logging level to DEBUG to force all lazily-evaluated messages
        # to be evaluated
        self.client.logger.setLevel('DEBUG')

    def make_url(self, path):
        path = path.format(
            accountId=ACCOUNT_ID,
            accountHash=ACCOUNT_HASH,
            orderId=ORDER_ID,
            savedOrderId=SAVED_ORDER_ID,
            cusip=CUSIP,
            market=MARKET,
            index=INDEX,
            symbol=SYMBOL,
            transactionId=TRANSACTION_ID,
            watchlistId=WATCHLIST_ID)
        return 'https://api.schwabapi.com' + path


    # Generic functionality


    def test_set_timeout(self):
        timeout = 'dummy'
        self.client.set_timeout(timeout)
        self.assertEqual(timeout, self.client.session.timeout)

    # The kind of a failed refresh, and the code Schwab sent with it.

    @staticmethod
    def hook_refusal(status, body=None, json_body=True):
        # Raised as the token response hook raises it, from a real response.
        import httpx2
        from schwaby import auth
        request = httpx2.Request(
                'POST', 'https://api.schwabapi.com/v1/oauth/token')
        if body is None:
            response = httpx2.Response(status, content=b'', request=request)
        else:
            response = httpx2.Response(status, json=body, request=request)
        try:
            auth._refuse_unusable_token_response(response)
        except OAuthError as e:
            return e
        raise AssertionError('the hook let {} through'.format(status))

    @no_duplicates
    def test_each_refresh_refusal_has_its_kind(self):
        from authlib.integrations.base_client.errors import MissingTokenError
        from schwaby import TokenErrorKind as K
        nested = ('400 Bad Request: {"error_description":"Refresh token is '
                  'invalid, expired or revoked","error":"invalid_grant"}')
        token = {'access_token': 'NEW', 'token_type': 'Bearer',
                 'expires_in': 1800, 'refresh_token': 'r2'}
        cases = (
            ('invalid_grant', self.hook_refusal(
                400, {'error': 'invalid_grant'}), K.DEAD_TOKEN,
             'invalid_grant'),
            ('nested', self.hook_refusal(
                400, {'error': 'unsupported_token_type',
                      'error_description': nested}), K.DEAD_TOKEN,
             'invalid_grant'),
            ('429 naming the app', self.hook_refusal(
                429, {'error': 'invalid_client'}), K.RATE_LIMITED,
             'invalid_client'),
            ('429 with no body', self.hook_refusal(429), K.RATE_LIMITED,
             None),
            ('invalid_client', self.hook_refusal(
                401, {'error': 'invalid_client'}), K.APP_REFUSED,
             'invalid_client'),
            ('unauthorized_client', self.hook_refusal(
                400, {'error': 'unauthorized_client'}), K.APP_REFUSED,
             'unauthorized_client'),
            ('400 with no body', self.hook_refusal(400), K.REFUSED_NO_BODY,
             None),
            ('401 with no body', self.hook_refusal(401), K.REFUSED_NO_BODY,
             None),
            ('403 with no body', self.hook_refusal(403), K.TRANSIENT, None),
            ('a token at 302', self.hook_refusal(302, token), K.TRANSIENT,
             None),
            ('unusable', self.hook_refusal(200, {'message': 'x'}),
             K.TRANSIENT, None),
            ('slow_down', self.hook_refusal(400, {'error': 'slow_down'}),
             K.TRANSIENT, 'slow_down'),
            ('a stored token with none', MissingTokenError(), K.DEAD_TOKEN,
             None),
            # A dead token at 429 is dead: checked first.
            ('429 with invalid_grant', self.hook_refusal(
                429, {'error': 'invalid_grant'}), K.DEAD_TOKEN,
             'invalid_grant'),
            # Only Schwab's nesting is unwrapped; other JSON in a
            # description changes nothing.
            ('invalid_client with JSON in it', self.hook_refusal(
                401, {'error': 'invalid_client', 'error_description':
                      'see {"error":"server_error"}'}), K.APP_REFUSED,
             'invalid_client'),
            ('slow_down naming invalid_client', self.hook_refusal(
                400, {'error': 'slow_down', 'error_description':
                      'x {"error":"invalid_client"}'}), K.TRANSIENT,
             'slow_down'),
            # Dead by refresh_token_invalid's own rule, which reads a nested
            # invalid_grant anywhere; the code is still the one sent.
            ('invalid_client nesting invalid_grant', self.hook_refusal(
                401, {'error': 'invalid_client', 'error_description':
                      'x {"error":"invalid_grant"}'}), K.DEAD_TOKEN,
             'invalid_client'),
            ('a nested code that is not invalid_grant', self.hook_refusal(
                400, {'error': 'unsupported_token_type', 'error_description':
                      '{"error":"invalid_client"}'}), K.TRANSIENT,
             'unsupported_token_type'))
        for name, refusal, kind, code in cases:
            with self.subTest(case=name):
                self.mock_session.get.side_effect = refusal
                with self.assertRaises(TokenRefreshError) as cm:
                    self.client.get_quote(SYMBOL)
                self.assertIs(kind, cm.exception.kind,
                              'a refusal was given the wrong kind')
                self.assertEqual(code, cm.exception.server_error_code)
                self.assertEqual(code,
                                 schwaby.server_error_code(cm.exception))
                # What was there before is as it was.
                self.assertEqual(kind is K.DEAD_TOKEN,
                                 cm.exception.refresh_token_invalid)

    # ensure_token_fresh

    def token(self, seconds_left, refresh_token='r'):
        token = {'access_token': 'a', 'token_type': 'Bearer'}
        if seconds_left is not None:
            token['expires_at'] = int(time.time()) + seconds_left
        if refresh_token is not None:
            token['refresh_token'] = refresh_token
        return token

    @no_duplicates
    def test_a_fresh_token_is_left_alone(self):
        from schwaby import TokenFreshness
        for left, margin in ((3600, 300.0), (100, 0), (900, 300)):
            with self.subTest(left=left, margin=margin):
                self.mock_session.token = self.token(left)
                self.mock_session.refresh_token.reset_mock()
                self.assertIs(TokenFreshness.FRESH,
                              self.client.ensure_token_fresh(margin))
                self.mock_session.refresh_token.assert_not_called()

    @no_duplicates
    def test_a_token_near_its_expiry_is_refreshed_now(self):
        from schwaby import TokenFreshness
        for left in (100, 300, -50, None, 'soon'):
            with self.subTest(left=left):
                token = self.token(left if not isinstance(left, str)
                                   else None)
                if isinstance(left, str):
                    token['expires_at'] = left
                self.mock_session.token = token
                self.mock_session.refresh_token.reset_mock()
                self.assertIs(TokenFreshness.REFRESHED,
                              self.client.ensure_token_fresh(300),
                              'a token near its expiry was not refreshed')
                self.mock_session.refresh_token.assert_called_once_with(
                        refresh_token='r')

    @no_duplicates
    def test_the_sessions_leeway_is_a_margin_too(self):
        # A request would refresh within the leeway anyway.
        from schwaby import TokenFreshness
        self.mock_session.leeway = 300
        self.mock_session.token = self.token(200)
        self.assertIs(TokenFreshness.REFRESHED,
                      self.client.ensure_token_fresh(0))

    @no_duplicates
    def test_ensure_token_fresh_reports_a_refusal_with_its_kind(self):
        from schwaby import TokenErrorKind
        self.mock_session.token = self.token(10)
        self.mock_session.refresh_token.side_effect = self.hook_refusal(
                400, {'error': 'invalid_grant'})
        with self.assertRaises(TokenRefreshError) as cm:
            self.client.ensure_token_fresh()
        self.assertIs(TokenErrorKind.DEAD_TOKEN, cm.exception.kind)

    @no_duplicates
    def test_a_token_with_no_refresh_token_is_dead_without_asking(self):
        from schwaby import TokenErrorKind
        for refresh_token in (None, '', 5):
            with self.subTest(refresh_token=refresh_token):
                self.mock_session.token = self.token(10, refresh_token)
                self.mock_session.refresh_token.reset_mock()
                with self.assertRaises(TokenRefreshError) as cm:
                    self.client.ensure_token_fresh()
                self.assertIs(TokenErrorKind.DEAD_TOKEN, cm.exception.kind)
                self.mock_session.refresh_token.assert_not_called()

    @no_duplicates
    def test_ensure_token_fresh_refuses_a_margin_it_cannot_use(self):
        for margin, error in ((-1, ValueError), (float('inf'), ValueError),
                              (float('nan'), ValueError), ('5', TypeError),
                              (True, TypeError)):
            with self.subTest(margin=repr(margin)):
                with self.assertRaises(error):
                    self.client.ensure_token_fresh(margin)


    # Rate-limit retries. The sleep is replaced, so nothing here waits; what
    # is checked is what was sent, how often, and the waits asked for.

    def patch_sleep(self):
        """Replaces the client's sleep, and returns the stand-in."""
        from unittest.mock import AsyncMock
        if isinstance(self.client, AsyncClient):
            target, stand_in = 'schwaby.client.asynchronous._sleep', AsyncMock()
        else:
            target, stand_in = 'schwaby.client.synchronous._sleep', MagicMock()
        patcher = patch(target, new=stand_in)
        patcher.start()
        self.addCleanup(patcher.stop)
        return stand_in

    def waits(self, sleep):
        # The waits that were awaited: a coroutine sleep that is called and
        # not awaited waits for nothing.
        calls = (sleep.await_args_list if isinstance(self.client, AsyncClient)
                 else sleep.call_args_list)
        return [c.args[0] for c in calls]

    @staticmethod
    def answer(status, headers=None):
        return MockResponse({}, status, headers=headers)

    @staticmethod
    def refresh_refused(status):
        # As the token response hook raises a refusal the endpoint answered.
        error = OAuthError(error='http_%d' % status,
                           description='refused at %d' % status)
        error._token_endpoint_status = status
        return error

    @no_duplicates
    def test_a_rate_limited_get_is_returned_when_no_retry_was_asked(self):
        sleep = self.patch_sleep()
        limited = self.answer(429)
        self.mock_session.get.side_effect = [limited, self.answer(200)]
        self.assertIs(limited, self.client.get_quote(SYMBOL),
                      'a 429 was retried when no retry was asked')
        self.assertEqual(1, self.mock_session.get.call_count)
        self.assertEqual([], self.waits(sleep))

    @no_duplicates
    def test_a_rate_limited_get_is_sent_again_after_a_backoff(self):
        sleep = self.patch_sleep()
        self.client.set_rate_limit_retry(3)
        done = self.answer(200)
        self.mock_session.get.side_effect = [
                self.answer(429), self.answer(429), done]
        self.assertIs(done, self.client.get_quote(SYMBOL))
        self.assertEqual(3, self.mock_session.get.call_count)
        self.assertEqual([1.0, 2.0], self.waits(sleep),
                         'the waits asked for were not the backoff')

    @no_duplicates
    def test_the_backoff_never_waits_longer_than_max_wait(self):
        sleep = self.patch_sleep()
        self.client.set_rate_limit_retry(5, max_wait=3)
        self.mock_session.get.side_effect = (
                [self.answer(429)] * 5 + [self.answer(200)])
        self.assertEqual(200, self.client.get_quote(SYMBOL).status_code)
        self.assertEqual([1.0, 2.0, 3.0, 3.0, 3.0], self.waits(sleep),
                         'a wait was longer than max_wait')

    @no_duplicates
    def test_retry_after_is_the_wait_when_schwab_gives_one(self):
        import email.utils
        long_past = email.utils.format_datetime(
                datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc),
                usegmt=True)
        for header, wait in (('7', 7.0), (' 12 ', 12.0), ('0', 1.0),
                             (long_past, 1.0), ('soon', 1.0), ('-3', 1.0),
                             ('1.5', 1.0), ('00000000005', 5.0),
                             ('5, 10', 10.0)):
            with self.subTest(header=header[:20]):
                sleep = self.patch_sleep()
                self.client.set_rate_limit_retry(1)
                self.mock_session.get.reset_mock()
                self.mock_session.get.side_effect = [
                        self.answer(429, {'Retry-After': header}),
                        self.answer(200)]
                self.assertEqual(200,
                                 self.client.get_quote(SYMBOL).status_code)
                self.assertEqual([wait], self.waits(sleep))

    @no_duplicates
    def test_a_retry_after_beyond_max_wait_returns_the_429(self):
        # Asking again sooner than Schwab said would only be refused again.
        import email.utils
        far = email.utils.format_datetime(
                datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc),
                usegmt=True)
        for header in ('60', far, '9' * 400, '9999999999', '5, 60'):
            with self.subTest(header=header):
                sleep = self.patch_sleep()
                self.client.set_rate_limit_retry(2, max_wait=5)
                self.mock_session.get.reset_mock()
                limited = self.answer(429, {'Retry-After': header})
                self.mock_session.get.side_effect = [limited, self.answer(200)]
                self.assertIs(limited, self.client.get_quote(SYMBOL),
                              'a 429 whose Retry-After is beyond max_wait '
                              'was retried')
                self.assertEqual(1, self.mock_session.get.call_count)
                self.assertEqual([], self.waits(sleep))

    @no_duplicates
    def test_the_last_429_is_returned_when_the_retries_run_out(self):
        sleep = self.patch_sleep()
        self.client.set_rate_limit_retry(2)
        last = self.answer(429)
        self.mock_session.get.side_effect = [
                self.answer(429), self.answer(429), last, self.answer(200)]
        self.assertIs(last, self.client.get_quote(SYMBOL),
                      'the retries did not stop where asked')
        self.assertEqual(3, self.mock_session.get.call_count)
        self.assertEqual([1.0, 2.0], self.waits(sleep))

    @no_duplicates
    def test_an_order_answered_429_is_never_sent_again(self):
        # Placing, replacing or cancelling twice is the harm this guards.
        for name, call, method in (
                ('place', lambda: self.client.place_order(ACCOUNT_HASH, {}),
                 'post'),
                ('replace', lambda: self.client.replace_order(
                    ACCOUNT_HASH, ORDER_ID, {}), 'put'),
                ('cancel', lambda: self.client.cancel_order(
                    ORDER_ID, ACCOUNT_HASH), 'delete')):
            with self.subTest(order=name):
                sleep = self.patch_sleep()
                self.client.set_rate_limit_retry(3)
                send = getattr(self.mock_session, method)
                send.reset_mock()
                limited = self.answer(429, {'Retry-After': '1'})
                send.side_effect = [limited, self.answer(201)]
                self.assertIs(limited, call(),
                              'an order Schwab answered was sent again')
                self.assertEqual(1, send.call_count,
                                 'an order Schwab answered was sent again')
                self.assertEqual([], self.waits(sleep))

    @no_duplicates
    def test_a_rate_limited_refresh_is_retried_for_a_get(self):
        sleep = self.patch_sleep()
        self.client.set_rate_limit_retry(2)
        done = self.answer(200)
        self.mock_session.get.side_effect = [self.refresh_refused(429), done]
        self.assertIs(done, self.client.get_quote(SYMBOL))
        self.assertEqual([1.0], self.waits(sleep))

    @no_duplicates
    def test_an_order_whose_refresh_is_rate_limited_is_not_held_back(self):
        # Sent late, an order could fill at a price its caller never saw,
        # or after the caller gave up and placed it again.
        for name, call, method in (
                ('place', lambda: self.client.place_order(ACCOUNT_HASH, {}),
                 'post'),
                ('replace', lambda: self.client.replace_order(
                    ACCOUNT_HASH, ORDER_ID, {}), 'put'),
                ('cancel', lambda: self.client.cancel_order(
                    ORDER_ID, ACCOUNT_HASH), 'delete')):
            with self.subTest(order=name):
                sleep = self.patch_sleep()
                self.client.set_rate_limit_retry(3)
                send = getattr(self.mock_session, method)
                send.reset_mock()
                send.side_effect = [self.refresh_refused(429),
                                    self.answer(201)]
                with self.assertRaises(TokenRefreshError,
                                       msg='an order was held back for a '
                                           'rate-limited refresh'):
                    call()
                self.assertEqual(1, send.call_count)
                self.assertEqual([], self.waits(sleep))

    @no_duplicates
    def test_a_response_is_not_read_while_the_retry_is_off(self):
        class Unreadable:
            @property
            def status_code(self):
                raise AssertionError('the status was read with the retry '
                                     'off')
        self.assertIsNone(
                self.client._response_retry_wait(Unreadable(), 1, 0.0))

    @no_duplicates
    def test_a_refresh_refused_otherwise_is_raised_at_once(self):
        for refusal in (self.refresh_refused(401), self.refresh_refused(400),
                        OAuthError(error='invalid_grant', description='x')):
            with self.subTest(refusal=refusal.error):
                sleep = self.patch_sleep()
                self.client.set_rate_limit_retry(3)
                self.mock_session.get.reset_mock()
                self.mock_session.get.side_effect = [refusal, self.answer(200)]
                with self.assertRaises(TokenRefreshError,
                                       msg='a refresh refused other than '
                                           'by a rate limit was retried'):
                    self.client.get_quote(SYMBOL)
                self.assertEqual(1, self.mock_session.get.call_count)
                self.assertEqual([], self.waits(sleep))

    @no_duplicates
    def test_a_refresh_still_rate_limited_after_the_retries_raises(self):
        sleep = self.patch_sleep()
        self.client.set_rate_limit_retry(1)
        self.mock_session.get.side_effect = [
                self.refresh_refused(429), self.refresh_refused(429)]
        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)
        self.assertTrue(cm.exception.rate_limited)
        self.assertEqual([1.0], self.waits(sleep))

    @no_duplicates
    def test_a_refresh_429_is_not_retried_when_no_retry_was_asked(self):
        sleep = self.patch_sleep()
        self.mock_session.get.side_effect = [self.refresh_refused(429)]
        with self.assertRaises(TokenRefreshError):
            self.client.get_quote(SYMBOL)
        self.assertEqual([], self.waits(sleep))

    @no_duplicates
    def test_zero_retries_turns_the_retry_off_again(self):
        sleep = self.patch_sleep()
        self.client.set_rate_limit_retry(2)
        self.client.set_rate_limit_retry(0)
        limited = self.answer(429)
        self.mock_session.get.side_effect = [limited, self.answer(200)]
        self.assertIs(limited, self.client.get_quote(SYMBOL))
        self.assertEqual([], self.waits(sleep))

    @no_duplicates
    def test_each_wait_is_logged(self):
        self.patch_sleep()
        self.client.set_rate_limit_retry(2)
        self.mock_session.get.side_effect = [self.answer(429),
                                             self.answer(200)]
        with self.assertLogs(self.client.logger, level='WARNING') as logs:
            self.client.get_quote(SYMBOL)
        self.assertEqual(1, len(logs.records))
        self.assertIn('rate limited (HTTP 429); sending again in 1.0 s, '
                      'retry 1 of 2', logs.output[0])

    # max_sleep, no_rate_limit_retry, and the pace.

    @no_duplicates
    def test_a_call_never_waits_past_max_sleep(self):
        sleep = self.patch_sleep()
        self.client.set_rate_limit_retry(5, max_sleep=2.5)
        limited = self.answer(429)
        self.mock_session.get.side_effect = [self.answer(429), limited,
                                             self.answer(200)]
        # 1 s, then 2 s would make 3: the second 429 is returned instead.
        self.assertIs(limited, self.client.get_quote(SYMBOL),
                      'a call waited past max_sleep')
        self.assertEqual([1.0], self.waits(sleep))
        self.assertEqual(2, self.mock_session.get.call_count)

    @no_duplicates
    def test_a_refresh_is_not_waited_for_past_max_sleep(self):
        sleep = self.patch_sleep()
        self.client.set_rate_limit_retry(5, max_sleep=0.5)
        self.mock_session.get.side_effect = [self.refresh_refused(429),
                                             self.answer(200)]
        with self.assertRaises(TokenRefreshError):
            self.client.get_quote(SYMBOL)
        self.assertEqual([], self.waits(sleep))

    @no_duplicates
    def test_the_paces_time_counts_toward_max_sleep(self):
        import time as real_time
        sleep = self.patch_sleep()
        # Exactly max_sleep is allowed, so the first backoff of 1 s alone
        # would be waited; the pace's time on top of it is not.
        self.client.set_rate_limit_retry(5, max_sleep=1.0)
        self.client.set_pace(lambda request: real_time.sleep(0.05))
        limited = self.answer(429)
        self.mock_session.get.side_effect = [limited, self.answer(200)]
        self.assertIs(limited, self.client.get_quote(SYMBOL),
                      "the pace's time was not counted")
        self.assertEqual([], self.waits(sleep))
        # Positive control: without the pace, the one wait fits.
        self.client.set_pace(None)
        self.mock_session.get.side_effect = [self.answer(429),
                                             self.answer(200)]
        self.assertEqual(200, self.client.get_quote(SYMBOL).status_code)
        self.assertEqual([1.0], self.waits(sleep))

    @no_duplicates
    def test_no_rate_limit_retry_turns_the_retry_off_inside_it(self):
        sleep = self.patch_sleep()
        self.client.set_rate_limit_retry(3)
        limited = self.answer(429)
        with self.client.no_rate_limit_retry():
            self.mock_session.get.side_effect = [limited, self.answer(200)]
            self.assertIs(limited, self.client.get_quote(SYMBOL),
                          'a call inside no_rate_limit_retry was retried')
            # The refresh a call makes too.
            self.mock_session.get.side_effect = [self.refresh_refused(429),
                                                 self.answer(200)]
            with self.assertRaises(TokenRefreshError):
                self.client.get_quote(SYMBOL)
        self.assertEqual([], self.waits(sleep))
        # Outside it, as before.
        self.mock_session.get.side_effect = [self.answer(429),
                                             self.answer(200)]
        self.assertEqual(200, self.client.get_quote(SYMBOL).status_code)
        self.assertEqual([1.0], self.waits(sleep))

    @no_duplicates
    def test_no_rate_limit_retry_is_this_clients_alone(self):
        sleep = self.patch_sleep()
        other = self.client_class(API_KEY, self.magicmock_class())
        for client in (self.client, other):
            client.set_rate_limit_retry(3)
        other.session.get.side_effect = [self.answer(429), self.answer(200)]
        with self.client.no_rate_limit_retry():
            self.assertEqual(200, other.get_quote(SYMBOL).status_code)
        self.assertEqual([1.0], self.waits(sleep))

    def paced(self):
        """Sets a pace that records what it is told, and returns the
        record."""
        told = []
        self.client.set_pace(told.append)
        return told

    @no_duplicates
    def test_the_pace_is_told_each_attempt(self):
        from schwaby import PaceRequest
        self.patch_sleep()
        told = self.paced()
        self.client.set_rate_limit_retry(2)
        self.mock_session.get.side_effect = [self.answer(429),
                                             self.answer(200)]
        self.client.get_quote(SYMBOL)
        path = '/marketdata/v1/{}/quotes'.format(SYMBOL)
        self.assertEqual([PaceRequest('GET', path, 'api', 1, None),
                          PaceRequest('GET', path, 'api', 2, None)], told)

    @no_duplicates
    def test_the_pace_is_told_each_kind_of_request(self):
        told = self.paced()
        self.client.place_order(ACCOUNT_HASH, {})
        self.client.replace_order(ACCOUNT_HASH, ORDER_ID, {})
        self.client.cancel_order(ORDER_ID, ACCOUNT_HASH)
        self.assertEqual(['POST', 'PUT', 'DELETE'],
                         [request.method for request in told])
        self.assertEqual({'api'}, {request.purpose for request in told})

    @no_duplicates
    def test_a_pace_that_raises_sends_nothing(self):
        mine = RuntimeError('mine')

        def pace(request):
            raise mine
        self.client.set_pace(pace)
        for name, call, method in (
                ('get', lambda: self.client.get_quote(SYMBOL), 'get'),
                ('place', lambda: self.client.place_order(ACCOUNT_HASH, {}),
                 'post')):
            with self.subTest(call=name):
                with self.assertRaises(RuntimeError) as cm:
                    call()
                self.assertIs(mine, cm.exception)
                getattr(self.mock_session, method).assert_not_called()

    @no_duplicates
    def test_pace_tag_reaches_the_pace(self):
        told = self.paced()
        other = self.client_class(API_KEY, self.magicmock_class())
        with self.client.pace_tag('urgent'):
            self.client.get_quote(SYMBOL)
            with self.client.pace_tag('inner'):
                self.client.get_quote(SYMBOL)
            # Another client's tag is not this one's.
            with other.pace_tag('theirs'):
                self.client.get_quote(SYMBOL)
        self.client.get_quote(SYMBOL)
        self.assertEqual(['urgent', 'inner', 'urgent', None],
                         [request.tag for request in told])

    @no_duplicates
    def test_set_pace_takes_a_callable_or_none(self):
        for bad in ('pace', 5, object()):
            with self.subTest(pace=repr(bad)):
                with self.assertRaises(TypeError):
                    self.client.set_pace(bad)
        told = self.paced()
        self.client.set_pace(None)
        self.client.get_quote(SYMBOL)
        self.assertEqual([], told)

    def due_token(self, refresh_token='r'):
        self.mock_session.leeway = 300
        self.mock_session.token = {
                'access_token': 'a', 'refresh_token': refresh_token,
                'expires_at': int(time.time()) + 100}

    @no_duplicates
    def test_with_a_pace_a_due_token_is_refreshed_through_it(self):
        told = self.paced()
        self.due_token()
        self.client.get_quote(SYMBOL)
        self.assertEqual([('api', 'GET'), ('token_refresh', 'POST')],
                         [(r.purpose, r.method) for r in told],
                         'a refresh was not paced')
        self.assertEqual('/v1/oauth/token', told[1].path)
        self.mock_session.refresh_token.assert_called_once_with(
                refresh_token='r')

    @no_duplicates
    def test_due_is_authlibs_rule(self):
        self.mock_session.leeway = 300
        for left, due in ((299, True), (-5, True), (310, False),
                          (3600, False)):
            with self.subTest(left=left):
                self.mock_session.token = {
                        'expires_at': int(time.time()) + left}
                self.assertEqual(due, self.client._token_due())

    @no_duplicates
    def test_a_retry_after_too_long_to_read_returns_the_429(self):
        sleep = self.patch_sleep()
        self.client.set_rate_limit_retry(3)
        limited = self.answer(429, {'Retry-After': '9' * 5000})
        self.mock_session.get.side_effect = [limited, self.answer(200)]
        self.assertIs(limited, self.client.get_quote(SYMBOL))
        self.assertEqual([], self.waits(sleep))

    @no_duplicates
    def test_a_long_backoff_does_not_overflow(self):
        self.client.set_rate_limit_retry(2000, max_wait=0.001)
        self.assertEqual(0.001, self.client._retry_wait(1500, 0.0))

    @no_duplicates
    def test_with_a_pace_a_fresh_token_is_left_to_the_session(self):
        told = self.paced()
        self.mock_session.leeway = 300
        for expires_at in (int(time.time()) + 3600, None, 'soon',
                           float(time.time())):
            with self.subTest(expires_at=expires_at):
                told.clear()
                self.mock_session.refresh_token.reset_mock()
                self.mock_session.token = {'refresh_token': 'r',
                                           'expires_at': expires_at}
                self.client.get_quote(SYMBOL)
                self.assertEqual(['api'], [r.purpose for r in told])
                self.mock_session.refresh_token.assert_not_called()

    @no_duplicates
    def test_without_a_pace_the_client_does_not_refresh_itself(self):
        self.due_token()
        self.client.get_quote(SYMBOL)
        self.mock_session.refresh_token.assert_not_called()

    @no_duplicates
    def test_a_paced_refresh_rate_limited_is_retried_for_a_get(self):
        sleep = self.patch_sleep()
        told = self.paced()
        self.due_token()
        self.client.set_rate_limit_retry(2)
        self.mock_session.refresh_token.side_effect = [
                self.refresh_refused(429), None]
        self.client.get_quote(SYMBOL)
        self.assertEqual([('api', 1), ('token_refresh', 1), ('api', 2),
                          ('token_refresh', 2)],
                         [(r.purpose, r.attempt) for r in told])
        self.assertEqual([1.0], self.waits(sleep))
        self.assertEqual(1, self.mock_session.get.call_count)

    @no_duplicates
    def test_an_order_whose_paced_refresh_is_rate_limited_is_not_held(self):
        sleep = self.patch_sleep()
        self.paced()
        self.due_token()
        self.client.set_rate_limit_retry(3)
        self.mock_session.refresh_token.side_effect = [
                self.refresh_refused(429), None]
        with self.assertRaises(TokenRefreshError):
            self.client.place_order(ACCOUNT_HASH, {})
        self.mock_session.post.assert_not_called()
        self.assertEqual([], self.waits(sleep),
                         'an order was held back for a rate-limited refresh')

    @no_duplicates
    def test_a_due_token_with_no_refresh_token_is_dead_before_the_pace(self):
        from schwaby import TokenErrorKind
        told = self.paced()
        self.due_token(refresh_token=None)
        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)
        self.assertIs(TokenErrorKind.DEAD_TOKEN, cm.exception.kind)
        self.assertEqual(['api'], [r.purpose for r in told])
        self.mock_session.get.assert_not_called()

    @no_duplicates
    def test_ensure_token_fresh_refreshes_through_the_pace(self):
        from schwaby import PaceRequest
        told = self.paced()
        self.due_token()
        with self.client.pace_tag('warm'):
            self.client.ensure_token_fresh()
        self.assertEqual([PaceRequest('POST', '/v1/oauth/token',
                                      'token_refresh', 1, 'warm')], told)

    @no_duplicates
    def test_set_rate_limit_retry_refuses_what_it_cannot_use(self):
        for kwargs, error in (({'retries': -1}, ValueError),
                              ({'retries': 1.5}, TypeError),
                              ({'retries': True}, TypeError),
                              ({'retries': '2'}, TypeError),
                              ({'retries': 1, 'max_wait': 0}, ValueError),
                              ({'retries': 1, 'max_wait': -1}, ValueError),
                              ({'retries': 1, 'max_wait': float('inf')},
                               ValueError),
                              ({'retries': 1, 'max_wait': float('nan')},
                               ValueError),
                              ({'retries': 1, 'max_wait': '5'}, TypeError),
                              ({'retries': 1, 'max_wait': True}, TypeError),
                              ({'retries': 1, 'max_sleep': -1}, ValueError),
                              ({'retries': 1, 'max_sleep': float('inf')},
                               ValueError),
                              ({'retries': 1, 'max_sleep': '5'}, TypeError),
                              ({'retries': 1, 'max_sleep': False},
                               TypeError)):
            with self.subTest(**{k: repr(v) for k, v in kwargs.items()}):
                with self.assertRaises(error):
                    self.client.set_rate_limit_retry(**kwargs)
        # Positive control: a sound setting is taken, and what was refused
        # left the retry as it was.
        self.client.set_rate_limit_retry(4, max_wait=2.5, max_sleep=0)
        self.assertEqual((4, 2.5, 0.0), (self.client._rate_limit_retries,
                                         self.client._rate_limit_max_wait,
                                         self.client._rate_limit_max_sleep))


    def test_token_age(self):
        token_metadata = MagicMock()
        token_metadata.token_age.return_value = 1000
        client = self.client_class(
                API_KEY, self.mock_session, token_metadata=token_metadata)

        self.assertEqual(client.token_age(), 1000)



    # get_account


    def test_get_account(self):
        self.client.get_account(ACCOUNT_HASH)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}'), params={})

    
    def test_get_account_fields(self):
        self.client.get_account(ACCOUNT_HASH, fields=[
            self.client_class.Account.Fields.POSITIONS])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}'),
            params={'fields': 'positions'})


    def test_get_account_fields_scalar(self):
        self.client.get_account(
                ACCOUNT_HASH, fields=self.client_class.Account.Fields.POSITIONS)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}'),
            params={'fields': 'positions'})


    def test_get_account_fields_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_account(ACCOUNT_HASH, fields=['positions'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}'),
            params={'fields': 'positions'})


    # get_account_numbers

    def test_get_account_numbers(self):
        self.client.get_account_numbers()
        self.mock_session.get.assert_called_with(
                self.make_url('/trader/v1/accounts/accountNumbers'), params={})


    # get_accounts


    def test_get_accounts(self):
        self.client.get_accounts()
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts'), params={})


    def test_get_accounts_fields(self):
        self.client.get_accounts(fields=[
            self.client_class.Account.Fields.POSITIONS])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts'),
            params={'fields': 'positions'})


    def test_get_accounts_fields_scalar(self):
        self.client.get_accounts(fields=self.client_class.Account.Fields.POSITIONS)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts'),
            params={'fields': 'positions'})


    def test_get_accounts_fields_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_accounts(fields=['positions'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts'),
            params={'fields': 'positions'})

    # get_order

    
    def test_get_order(self):
        self.client.get_order(ORDER_ID, ACCOUNT_HASH)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'),
            params={})

    def test_get_order_str(self):
        self.client.get_order(str(ORDER_ID), str(ACCOUNT_HASH))
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'),
            params={})

    # cancel_order

    def test_cancel_order(self):
        self.client.cancel_order(ORDER_ID, ACCOUNT_HASH)
        self.mock_session.delete.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'))

    def test_cancel_order_str(self):
        self.client.cancel_order(str(ORDER_ID), str(ACCOUNT_HASH))
        self.mock_session.delete.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'))

    # get_orders_for_account

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_vanilla(self):
        self.client.get_orders_for_account(ACCOUNT_HASH)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_from_not_datetime(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_account(
                    ACCOUNT_HASH, from_entered_datetime='2020-01-02')
        self.assertEqual(
                str(cm.exception),
                "expected type in (datetime.date, datetime.datetime) for " +
                "from_entered_datetime, got 'builtins.str'")


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_to_not_datetime(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_account(
                    ACCOUNT_HASH, to_entered_datetime='2020-01-02')
        self.assertEqual(
                str(cm.exception),
                "expected type in (datetime.date, datetime.datetime) for " +
                "to_entered_datetime, got 'builtins.str'")


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_a_wrong_type_whose_name_raises_is_still_refused_by_name(self):
        class Meta(type):
            @property
            def __name__(cls):
                raise RuntimeError('metaclass raised')

        class Odd(metaclass=Meta):
            pass

        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_account(
                    ACCOUNT_HASH, from_entered_datetime=Odd())
        self.assertIn("got 'tests.client_test.Odd'", str(cm.exception))


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_a_wrong_type_whose_module_raises_is_still_refused_by_name(self):
        class Meta(type):
            @property
            def __module__(cls):
                raise RuntimeError('metaclass raised')

        class Odd(metaclass=Meta):
            pass

        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_account(
                    ACCOUNT_HASH, from_entered_datetime=Odd())
        self.assertIn("got 'tests.client_test.Odd'", str(cm.exception))


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_max_results(self):
        self.client.get_orders_for_account(ACCOUNT_HASH, max_results=100)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO,
                'maxResults': 100,
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_from_entered_datetime(self):
        self.client.get_orders_for_account(
                ACCOUNT_HASH, from_entered_datetime=datetime.datetime(
                    year=2024, month=6, day=5, hour=4, minute=3, second=2))
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': '2024-06-05T04:03:02Z',
                'toEnteredTime': NOW_DATETIME_ISO,
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_to_entered_datetime(self):
        self.client.get_orders_for_account(
                ACCOUNT_HASH, to_entered_datetime=datetime.datetime(
                    year=2024, month=6, day=5, hour=4, minute=3, second=2))
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': '2024-06-05T04:03:02Z',
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_status(self):
        self.client.get_orders_for_account(
                ACCOUNT_HASH, status=self.client_class.Order.Status.FILLED)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO,
                'status': 'FILLED'
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_multiple_statuses(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_account(
                    ACCOUNT_HASH,
                    status=[self.client_class.Order.Status.FILLED,
                            self.client_class.Order.Status.REJECTED])
        self.assertIn(
                'expected type "Status", got type "list"',
                str(cm.exception))


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_status_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_orders_for_account(ACCOUNT_HASH, status='NOT_A_STATUS')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO,
                'status': 'NOT_A_STATUS'
            })


    # get_orders_for_all_linked_accounts

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_vanilla(self):
        self.client.get_orders_for_all_linked_accounts()
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_from_not_datetime(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_all_linked_accounts(
                    from_entered_datetime='2020-01-02')
        self.assertEqual(
                str(cm.exception),
                "expected type in (datetime.date, datetime.datetime) for " +
                "from_entered_datetime, got 'builtins.str'")


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_to_not_datetime(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_all_linked_accounts(
                    to_entered_datetime='2020-01-02')
        self.assertEqual(
                str(cm.exception),
                "expected type in (datetime.date, datetime.datetime) for " +
                "to_entered_datetime, got 'builtins.str'")


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_max_results(self):
        self.client.get_orders_for_all_linked_accounts(max_results=100)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO,
                'maxResults': 100,
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_from_entered_datetime(self):
        self.client.get_orders_for_all_linked_accounts(
                from_entered_datetime=datetime.datetime(
                    year=2024, month=6, day=5, hour=4, minute=3, second=2))
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/orders'), params={
                'fromEnteredTime': '2024-06-05T04:03:02Z',
                'toEnteredTime': NOW_DATETIME_ISO,
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_to_entered_datetime(self):
        self.client.get_orders_for_all_linked_accounts(
                to_entered_datetime=datetime.datetime(
                    year=2024, month=6, day=5, hour=4, minute=3, second=2))
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': '2024-06-05T04:03:02Z',
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_status(self):
        self.client.get_orders_for_all_linked_accounts(
                status=self.client_class.Order.Status.FILLED)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO,
                'status': 'FILLED'
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_multiple_statuses(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_all_linked_accounts(
                    status=[self.client_class.Order.Status.FILLED,
                            self.client_class.Order.Status.REJECTED])
        self.assertIn(
                'expected type "Status", got type "list"',
                str(cm.exception))


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_status_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_orders_for_all_linked_accounts(status='NOT_A_STATUS')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO,
                'status': 'NOT_A_STATUS'
            })


    # place_order

    
    def test_place_order(self):
        order_spec = {'order': 'spec'}
        self.client.place_order(ACCOUNT_HASH, order_spec)
        self.mock_session.post.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), json=order_spec)

    
    def test_place_order_order_builder(self):
        order_spec = OrderBuilder(enforce_enums=False).set_order_type('LIMIT')
        expected_spec = {'orderType': 'LIMIT'}
        self.client.place_order(ACCOUNT_HASH, order_spec)
        self.mock_session.post.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'),
            json=expected_spec)

    
    def test_place_order_str(self):
        order_spec = {'order': 'spec'}
        self.client.place_order(str(ACCOUNT_HASH), order_spec)
        self.mock_session.post.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), json=order_spec)

    # replace_order

    
    def test_replace_order(self):
        order_spec = {'order': 'spec'}
        self.client.replace_order(ACCOUNT_HASH, ORDER_ID, order_spec)
        self.mock_session.put.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'),
            json=order_spec)

    
    def test_replace_order_order_builder(self):
        order_spec = OrderBuilder(enforce_enums=False).set_order_type('LIMIT')
        expected_spec = {'orderType': 'LIMIT'}
        self.client.replace_order(ACCOUNT_HASH, ORDER_ID, order_spec)
        self.mock_session.put.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'),
            json=expected_spec)

    
    def test_replace_order_str(self):
        order_spec = {'order': 'spec'}
        self.client.replace_order(str(ACCOUNT_HASH), str(ORDER_ID), order_spec)
        self.mock_session.put.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'),
            json=order_spec)


    # preview_order

    
    def test_preview_order(self):
        order_spec = {'order': 'spec'}
        self.client.preview_order(ACCOUNT_HASH, order_spec)
        self.mock_session.post.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/previewOrder'),
            json=order_spec)

    
    def test_preview_order_order_builder(self):
        order_spec = OrderBuilder(enforce_enums=False).set_order_type('LIMIT')
        expected_spec = {'orderType': 'LIMIT'}
        self.client.preview_order(ACCOUNT_HASH, order_spec)
        self.mock_session.post.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/previewOrder'),
            json=expected_spec)

    
    # get_transactions

    
    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions(self):
        self.client.get_transactions(ACCOUNT_HASH)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': ','.join(t.value for t in self.client.Transactions.TransactionType),
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': NOW_DATETIME_ISO})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_one_type(self):
        self.client.get_transactions(
                ACCOUNT_HASH, 
                transaction_types=self.client.Transactions.TransactionType.TRADE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': 'TRADE',
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': NOW_DATETIME_ISO})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_type_list(self):
        self.client.get_transactions(
                ACCOUNT_HASH, 
                transaction_types=[
                    self.client.Transactions.TransactionType.TRADE,
                    self.client.Transactions.TransactionType.JOURNAL])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': 'TRADE,JOURNAL',
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': NOW_DATETIME_ISO})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_type_list_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_transactions(
                ACCOUNT_HASH, transaction_types=['TRADE', 'JOURNAL'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': 'TRADE,JOURNAL',
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': NOW_DATETIME_ISO})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_symbol(self):
        self.client.get_transactions(ACCOUNT_HASH, symbol='AAPL')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': ','.join(t.value for t in self.client.Transactions.TransactionType),
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': NOW_DATETIME_ISO,
                'symbol': 'AAPL'})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_symbol_start_date_as_datetime(self):
        self.client.get_transactions(
                ACCOUNT_HASH, start_date=NOW_DATETIME)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': ','.join(t.value for t in self.client.Transactions.TransactionType),
                'startDate': NOW_DATETIME_ISO,
                'endDate': NOW_DATETIME_ISO})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_symbol_start_date_as_date(self):
        self.client.get_transactions(
                ACCOUNT_HASH, start_date=NOW_DATE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': ','.join(t.value for t in self.client.Transactions.TransactionType),
                'startDate': NOW_DATETIME_TRUNCATED_ISO,
                'endDate': NOW_DATETIME_ISO})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_symbol_end_date_as_datetime(self):
        self.client.get_transactions(
                ACCOUNT_HASH,
                # NOW_DATETIME is the default, use something different
                end_date=datetime.datetime(2020, 6, 7, 8, 9, 0))
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': ','.join(t.value for t in self.client.Transactions.TransactionType),
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': '2020-06-07T08:09:00Z'})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_symbol_end_date_as_date(self):
        self.client.get_transactions(ACCOUNT_HASH, end_date=NOW_DATE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': ','.join(t.value for t in self.client.Transactions.TransactionType),
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': NOW_DATETIME_TRUNCATED_ISO})


    # get_transaction
    
    def test_get_transaction(self):
        self.client.get_transaction(ACCOUNT_HASH, TRANSACTION_ID)
        self.mock_session.get.assert_called_once_with(
            self.make_url(
                '/trader/v1/accounts/{accountHash}/transactions/{transactionId}'),
            params={})


    # get_user_preference

    def test_get_user_preferences(self):
        self.client.get_user_preferences()
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/userPreference'), params={})


    # get_quote

    def test_get_quote(self):
        self.client.get_quote(SYMBOL)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/{symbol}/quotes'), params={})


    def test_get_quote_fields_single(self):
        self.client.get_quote(SYMBOL, fields=self.client.Quote.Fields.QUOTE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/{symbol}/quotes'),
            params={'fields': 'quote'})


    def test_get_quote_fields_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_quote(SYMBOL, fields=['not-a-field'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/{symbol}/quotes'),
            params={'fields': 'not-a-field'})


    def test_get_quote_fields_multiple(self):
        self.client.get_quote(SYMBOL, fields=[
            self.client.Quote.Fields.QUOTE,
            self.client.Quote.Fields.FUNDAMENTAL])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/{symbol}/quotes'),
            params={'fields': 'quote,fundamental'})


    # get_quotes

    def test_get_quotes(self):
        self.client.get_quotes(['AAPL', 'MSFT'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/quotes'), params={
                'symbols': 'AAPL,MSFT'})

    
    def test_get_quotes_single_symbol(self):
        self.client.get_quotes('AAPL')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/quotes'), params={
                'symbols': 'AAPL'})

    def test_get_quotes_fields(self):
        self.client.get_quotes(
                ['AAPL', 'MSFT'],
                fields=self.client.Quote.Fields.QUOTE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/quotes'), params={
                'symbols': 'AAPL,MSFT',
                'fields': 'quote'})


    def test_get_quotes_fields_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_quotes(['AAPL', 'MSFT'], fields=['quote'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/quotes'), params={
                'symbols': 'AAPL,MSFT',
                'fields': 'quote'})


    def test_get_quotes_indicative(self):
        self.client.get_quotes(['AAPL', 'MSFT'], indicative=True)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/quotes'), params={
                'symbols': 'AAPL,MSFT',
                'indicative': 'true'})


    def test_get_quotes_indicative_not_bool(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_quotes(['AAPL', 'MSFT'], indicative='false')
        self.assertEqual(str(cm.exception),
                         "value of 'indicative' must be either True or False")


    # get_price_history
    
    def test_get_price_history_vanilla(self):
        self.client.get_price_history(SYMBOL)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL})

    
    def test_get_price_history_period_type(self):
        self.client.get_price_history(
            SYMBOL, period_type=self.client_class.PriceHistory.PeriodType.MONTH)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'periodType': 'month'})

    
    def test_get_price_history_period_type_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_price_history(SYMBOL, period_type='month')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'periodType': 'month'})

    
    def test_get_price_history_num_periods(self):
        self.client.get_price_history(
            SYMBOL, period=self.client_class.PriceHistory.Period.TEN_DAYS)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'period': 10})

    
    def test_get_price_history_num_periods_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_price_history(SYMBOL, period=10)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'period': 10})

    
    def test_get_price_history_frequency_type(self):
        self.client.get_price_history(
            SYMBOL,
            frequency_type=self.client_class.PriceHistory.FrequencyType.DAILY)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'frequencyType': 'daily'})

    
    def test_get_price_history_frequency_type_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_price_history(SYMBOL, frequency_type='daily')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'frequencyType': 'daily'})

    
    def test_get_price_history_frequency(self):
        self.client.get_price_history(
        SYMBOL,
        frequency=self.client_class.PriceHistory.Frequency.EVERY_FIVE_MINUTES)
        self.mock_session.get.assert_called_once_with(
        self.make_url('/marketdata/v1/pricehistory'), params={
            'symbol': SYMBOL,
            'frequency': 5})


    def test_get_price_history_frequency_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_price_history(SYMBOL, frequency=5)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'frequency': 5})

    
    def test_get_price_history_start_datetime(self):
        self.client.get_price_history(
            SYMBOL, start_datetime=EARLIER_DATETIME)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'startDate': EARLIER_MILLIS})

    
    def test_get_price_history_start_datetime_str(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_price_history(SYMBOL, start_datetime='2020-01-01')
        self.assertEqual(str(cm.exception),
                         "expected type 'datetime.datetime' for " +
                         "start_datetime, got 'builtins.str'")

    
    def test_get_price_history_end_datetime(self):
        self.client.get_price_history(SYMBOL, end_datetime=EARLIER_DATETIME)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'endDate': EARLIER_MILLIS})

    
    def test_get_price_history_end_datetime_str(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_price_history(SYMBOL, end_datetime='2020-01-01')
        self.assertEqual(str(cm.exception),
                         "expected type 'datetime.datetime' for " +
                         "end_datetime, got 'builtins.str'")

    
    def test_get_price_history_need_extended_hours_data(self):
        self.client.get_price_history(SYMBOL, need_extended_hours_data=True)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'needExtendedHoursData': True})


    def test_get_price_history_need_previous_close(self):
        self.client.get_price_history(SYMBOL, need_previous_close=True)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'needPreviousClose': True})


    # get_option_chain
    
    def test_get_option_chain_vanilla(self):
        self.client.get_option_chain('AAPL')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL'})

    def test_get_option_chain_contract_type(self):
        self.client.get_option_chain(
            'AAPL', contract_type=self.client_class.Options.ContractType.PUT)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'contractType': 'PUT'})

    
    def test_get_option_chain_contract_type_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_option_chain('AAPL', contract_type='PUT')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'contractType': 'PUT'})

    
    def test_get_option_chain_strike_count(self):
        self.client.get_option_chain('AAPL', strike_count=100)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'strikeCount': 100})

    
    def test_get_option_chain_include_underlyingquotes(self):
        self.client.get_option_chain('AAPL', include_underlying_quote=True)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'includeUnderlyingQuote': True})

    
    def test_get_option_chain_strategy(self):
        self.client.get_option_chain(
            'AAPL', strategy=self.client_class.Options.Strategy.STRANGLE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'strategy': 'STRANGLE'})

    
    def test_get_option_chain_strategy_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_option_chain('AAPL', strategy='STRANGLE')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'strategy': 'STRANGLE'})

    
    def test_get_option_chain_interval(self):
        self.client.get_option_chain('AAPL', interval=10.0)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'interval': 10.0})

    
    def test_get_option_chain_strike(self):
        self.client.get_option_chain('AAPL', strike=123)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'strike': 123})

    
    def test_get_option_chain_strike_range(self):
        self.client.get_option_chain(
            'AAPL', strike_range=self.client_class.Options.StrikeRange.IN_THE_MONEY)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'range': 'ITM'})

    
    def test_get_option_chain_strike_range_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_option_chain('AAPL', strike_range='ITM')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'range': 'ITM'})

    
    def test_get_option_chain_from_date_datetime(self):
        self.client.get_option_chain(
            'AAPL', from_date=NOW_DATETIME)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'fromDate': NOW_DATE_ISO})

    
    def test_get_option_chain_from_date_date(self):
        self.client.get_option_chain('AAPL', from_date=NOW_DATE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'fromDate': NOW_DATE_ISO})

    
    def test_get_option_chain_from_date_str(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_option_chain('AAPL', from_date='2020-01-01')
        self.assertEqual(str(cm.exception),
                         "expected type 'datetime.date' for " +
                         "from_date, got 'builtins.str'")

    
    def test_get_option_chain_to_date_datetime(self):
        self.client.get_option_chain('AAPL', to_date=NOW_DATETIME)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'toDate': NOW_DATE_ISO})

    
    def test_get_option_chain_to_date_date(self):
        self.client.get_option_chain('AAPL', to_date=NOW_DATE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'toDate': NOW_DATE_ISO})

    
    def test_get_option_chain_to_date_str(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_option_chain('AAPL', to_date='2020-01-01')
        self.assertEqual(str(cm.exception),
                         "expected type 'datetime.date' for " +
                         "to_date, got 'builtins.str'")

    
    def test_get_option_chain_volatility(self):
        self.client.get_option_chain('AAPL', volatility=40.0)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'volatility': 40.0})

    
    def test_get_option_chain_underlying_price(self):
        self.client.get_option_chain('AAPL', underlying_price=234.0)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'underlyingPrice': 234.0})

    
    def test_get_option_chain_interest_rate(self):
        self.client.get_option_chain('AAPL', interest_rate=0.07)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'interestRate': 0.07})

    
    def test_get_option_chain_days_to_expiration(self):
        self.client.get_option_chain('AAPL', days_to_expiration=12)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'daysToExpiration': 12})

    
    def test_get_option_chain_exp_month(self):
        self.client.get_option_chain(
            'AAPL', exp_month=self.client_class.Options.ExpirationMonth.JANUARY)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'expMonth': 'JAN'})

    
    def test_get_option_chain_exp_month_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_option_chain('AAPL', exp_month='JAN')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'expMonth': 'JAN'})

    
    def test_get_option_chain_option_type(self):
        self.client.get_option_chain(
            'AAPL', option_type=self.client_class.Options.Type.STANDARD)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'optionType': 'S'})

    
    def test_get_option_chain_option_type_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_option_chain('AAPL', option_type='S')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'optionType': 'S'})


    def test_get_option_chain_option_entitlement(self):
        self.client.get_option_chain(
            'AAPL', entitlement=self.client.Options.Entitlement.PAYING_PRO)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'entitlement': 'PP'})


    # get_option_expiration_chain

    def test_get_option_expiration_chain(self):
        self.client.get_option_expiration_chain('AAPL')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/expirationchain'),
            params={'symbol': 'AAPL'})


    # Schwab honours `period` and ignores the date range when both are sent, so
    # a caller who asks for a range and still gets a period's worth of data has
    # been silently ignored. get_price_history's own docstring says period
    # "should not be provided if start_datetime and end_datetime".

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_price_history_helpers_omit_period_when_given_a_range(self):
        helpers = [name for name in dir(self.client)
                   if name.startswith('get_price_history_every')]
        self.assertEqual(7, len(helpers))

        for name in helpers:
            for kwargs in ({'start_datetime': EARLIER_DATETIME},
                           {'end_datetime': EARLIER_DATETIME},
                           {'start_datetime': EARLIER_DATETIME,
                            'end_datetime': NOW_DATETIME}):
                self.mock_session.get.reset_mock()
                getattr(self.client, name)('AAPL', **kwargs)
                params = self.mock_session.get.call_args[1]['params']

                self.assertNotIn(
                        'period', params,
                        '{} sent period alongside {}'.format(
                            name, sorted(kwargs)))
                self.assertIn('startDate', params)
                self.assertIn('endDate', params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_price_history_helpers_keep_period_without_a_range(self):
        # With no range asked for, the synthesized one spans decades and is not
        # something to request. period is what should describe the request.
        helpers = [name for name in dir(self.client)
                   if name.startswith('get_price_history_every')]

        for name in helpers:
            self.mock_session.get.reset_mock()
            getattr(self.client, name)('AAPL')
            params = self.mock_session.get.call_args[1]['params']

            self.assertIn('period', params,
                          '{} dropped period with no range'.format(name))

    # get_price_history_every_minute

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_vanilla(self):
        self.client.get_price_history_every_minute('AAPL')
        params = {
                'symbol': 'AAPL',
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_start_datetime(self):
        self.client.get_price_history_every_minute(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_end_datetime(self):
        self.client.get_price_history_every_minute(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_empty_extendedhours(self):
        self.client.get_price_history_every_minute(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_extendedhours(self):
        self.client.get_price_history_every_minute(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_empty_previous_close(self):
        self.client.get_price_history_every_minute(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_previous_close(self):
        self.client.get_price_history_every_minute(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)



    # get_price_history_every_five_minutes


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_vanilla(self):
        self.client.get_price_history_every_five_minutes('AAPL')
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_start_datetime(self):
        self.client.get_price_history_every_five_minutes(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_end_datetime(self):
        self.client.get_price_history_every_five_minutes(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_empty_extendedhours(self):
        self.client.get_price_history_every_five_minutes(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_extendedhours(self):
        self.client.get_price_history_every_five_minutes(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_empty_previous_close(self):
        self.client.get_price_history_every_five_minutes(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_previous_close(self):
        self.client.get_price_history_every_five_minutes(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    # get_price_history_every_ten_minutes


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_vanilla(self):
        self.client.get_price_history_every_ten_minutes('AAPL')
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_start_datetime(self):
        self.client.get_price_history_every_ten_minutes(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_end_datetime(self):
        self.client.get_price_history_every_ten_minutes(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_empty_extendedhours(self):
        self.client.get_price_history_every_ten_minutes(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_extendedhours(self):
        self.client.get_price_history_every_ten_minutes(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_empty_previous_close(self):
        self.client.get_price_history_every_ten_minutes(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_previous_close(self):
        self.client.get_price_history_every_ten_minutes(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    # get_price_history_every_fifteen_minutes


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_vanilla(self):
        self.client.get_price_history_every_fifteen_minutes('AAPL')
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_start_datetime(self):
        self.client.get_price_history_every_fifteen_minutes(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_end_datetime(self):
        self.client.get_price_history_every_fifteen_minutes(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_empty_extendedhours(self):
        self.client.get_price_history_every_fifteen_minutes(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_extendedhours(self):
        self.client.get_price_history_every_fifteen_minutes(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_empty_previous_close(self):
        self.client.get_price_history_every_fifteen_minutes(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_previous_close(self):
        self.client.get_price_history_every_fifteen_minutes(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    # get_price_history_every_thirty_minutes


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_vanilla(self):
        self.client.get_price_history_every_thirty_minutes('AAPL')
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_start_datetime(self):
        self.client.get_price_history_every_thirty_minutes(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_end_datetime(self):
        self.client.get_price_history_every_thirty_minutes(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_empty_extendedhours(self):
        self.client.get_price_history_every_thirty_minutes(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_extendedhours(self):
        self.client.get_price_history_every_thirty_minutes(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_empty_previous_close(self):
        self.client.get_price_history_every_thirty_minutes(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_previous_close(self):
        self.client.get_price_history_every_thirty_minutes(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    # get_price_history_every_day


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_vanilla(self):
        self.client.get_price_history_every_day('AAPL')
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_start_datetime(self):
        self.client.get_price_history_every_day(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_end_datetime(self):
        self.client.get_price_history_every_day(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_empty_extendedhours(self):
        self.client.get_price_history_every_day(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_extendedhours(self):
        self.client.get_price_history_every_day(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_empty_previous_close(self):
        self.client.get_price_history_every_day(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_previous_close(self):
        self.client.get_price_history_every_day(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    # get_price_history_every_week


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_vanilla(self):
        self.client.get_price_history_every_week('AAPL')
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_start_datetime(self):
        self.client.get_price_history_every_week(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_end_datetime(self):
        self.client.get_price_history_every_week(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_empty_extendedhours(self):
        self.client.get_price_history_every_week(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_extendedhours(self):
        self.client.get_price_history_every_week(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_empty_previous_close(self):
        self.client.get_price_history_every_week(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_previous_close(self):
        self.client.get_price_history_every_week(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    # get_movers

    
    def test_get_movers(self):
        self.client.get_movers(
                self.client.Movers.Index.DJI)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/movers/$DJI'), params={})


    def test_get_movers_index_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_movers('not-an-index')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/movers/not-an-index'), params={})


    def test_get_movers_sort_order(self):
        self.client.get_movers(
                self.client.Movers.Index.DJI,
                sort_order=self.client.Movers.SortOrder.VOLUME)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/movers/$DJI'),
            params={'sort': 'VOLUME'})


    def test_get_movers_sort_order_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_movers(
                self.client.Movers.Index.DJI,
                sort_order='not-a-sort-order')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/movers/$DJI'),
            params={'sort': 'not-a-sort-order'})


    def test_get_movers_frequency(self):
        self.client.get_movers(
                self.client.Movers.Index.DJI,
                frequency=self.client.Movers.Frequency.ZERO)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/movers/$DJI'),
            params={'frequency': '0'})


    def test_get_movers_frequency_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_movers(
                self.client.Movers.Index.DJI,
                frequency='999999')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/movers/$DJI'),
            params={'frequency': '999999'})


    # get_market_hours

    
    def test_get_market_hours_single_market(self):
        self.client.get_market_hours(
                self.client_class.MarketHours.Market.EQUITY)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/markets'), params={
                'markets': 'equity'})


    def test_get_market_hours_market_list(self):
        self.client.get_market_hours(
                [self.client_class.MarketHours.Market.EQUITY,
                 self.client_class.MarketHours.Market.OPTION])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/markets'), params={
                'markets': 'equity,option'})


    def test_get_market_hours_market_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_market_hours(['not-a-market'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/markets'), params={
                'markets': 'not-a-market'})


    def test_get_market_hours_date(self):
        self.client.get_market_hours(
                self.client_class.MarketHours.Market.EQUITY,
                date=NOW_DATE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/markets'), params={
                'markets': 'equity',
                'date': NOW_DATE_ISO})


    def test_get_market_hours_date_str(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_market_hours(
                    self.client_class.MarketHours.Market.EQUITY,
                    date='2020-01-02')
        self.assertEqual(str(cm.exception),
                         "expected type 'datetime.date' for " +
                         "date, got 'builtins.str'")


    # get_instruments
    
    def test_get_instruments(self):
        self.client.get_instruments(
            ['AAPL', 'MSFT'], self.client_class.Instrument.Projection.FUNDAMENTAL)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/instruments'), params={
                'symbol': 'AAPL,MSFT',
                'projection': 'fundamental'})


    def test_get_instruments_string_args(self):
        self.client.get_instruments(
            'AAPL', self.client_class.Instrument.Projection.FUNDAMENTAL)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/instruments'), params={
                'symbol': 'AAPL',
                'projection': 'fundamental'})


    def test_get_instruments_projection_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_instruments(
            ['AAPL', 'MSFT'], 'not-a-projection')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/instruments'), params={
                'symbol': 'AAPL,MSFT',
                'projection': 'not-a-projection'})


    # get_instrument_by_cusip
    
    def test_get_instrument_by_cusip(self):
        self.client.get_instrument_by_cusip('037833100')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/instruments/037833100'), params={})


    def test_get_instrument_by_cusip_cusip_must_be_string(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_instrument_by_cusip(37833100)
        self.assertEqual(str(cm.exception), 'cusip must be passed as str')


    # ISO-8601 datetime parameters


    @no_duplicates
    def test_orders_aware_datetime_is_converted_to_utc(self):
        # Schwab documents fromEnteredTime/toEnteredTime as
        # yyyy-MM-dd'T'HH:mm:ss.SSSZ -- the trailing Z means UTC. A datetime
        # which carries a non-UTC timezone therefore has to be converted, not
        # just formatted, or the wall clock of another zone is sent labelled as
        # UTC.
        eastern = pytz.timezone('America/New_York')
        # 2024-06-05 04:03:02 UTC, expressed in New York as 00:03:02-04:00.
        aware = eastern.localize(
                datetime.datetime(year=2024, month=6, day=5,
                                  hour=0, minute=3, second=2))

        self.client.get_orders_for_account(
                ACCOUNT_HASH,
                from_entered_datetime=aware,
                to_entered_datetime=aware)

        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': '2024-06-05T04:03:02Z',
                'toEnteredTime': '2024-06-05T04:03:02Z',
            })


    @no_duplicates
    def test_transactions_aware_datetime_is_converted_to_utc(self):
        eastern = pytz.timezone('America/New_York')
        aware = eastern.localize(
                datetime.datetime(year=2024, month=6, day=5,
                                  hour=0, minute=3, second=2))

        self.client.get_transactions(
                ACCOUNT_HASH, start_date=aware, end_date=aware)

        self.mock_session.get.assert_called_once_with(
            self.make_url(
                '/trader/v1/accounts/{accountHash}/transactions'), params={
                'types': ','.join(
                    t.value for t in self.client.Transactions.TransactionType),
                'startDate': '2024-06-05T04:03:02Z',
                'endDate': '2024-06-05T04:03:02Z',
            })


    @no_duplicates
    @unittest.skipUnless(hasattr(time, 'tzset'),
                         'needs time.tzset to pin a non-UTC local zone')
    def test_a_naive_datetime_is_sent_as_written_not_as_local_time(self):
        # A naive datetime has no zone to convert from, so its wall clock is
        # sent as written. Converting it anyway reads it as the host's local
        # time, and on a host set to UTC that gives the identical string --
        # which is what CI runners are. So the zone is pinned here to one
        # that is not UTC, or a regression would pass everywhere it runs.
        naive = datetime.datetime(year=2024, month=6, day=5,
                                  hour=4, minute=3, second=2)

        original = os.environ.get('TZ')
        os.environ['TZ'] = 'America/New_York'
        time.tzset()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                self.client.get_orders_for_account(
                        ACCOUNT_HASH,
                        from_entered_datetime=naive,
                        to_entered_datetime=naive)
        finally:
            if original is None:
                del os.environ['TZ']
            else:
                os.environ['TZ'] = original
            time.tzset()

        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': '2024-06-05T04:03:02Z',
                'toEnteredTime': '2024-06-05T04:03:02Z',
            })


    @no_duplicates
    @unittest.skipUnless(hasattr(time, 'tzset'),
                         'needs time.tzset to pin a non-UTC local zone')
    def test_a_tzinfo_with_no_offset_is_naive(self):
        # Python calls a datetime naive when utcoffset() returns None, even
        # with a tzinfo set. Keying on tzinfo alone would skip the warning and
        # then convert from the host's zone, so the same code would send a
        # different instant from each machine. The zone is pinned for the
        # reason the naive test above pins it.
        class NoOffset(datetime.tzinfo):
            def utcoffset(self, dt):
                return None

            def dst(self, dt):
                return None

        value = datetime.datetime(year=2024, month=6, day=5, hour=4,
                                  minute=3, second=2, tzinfo=NoOffset())

        original = os.environ.get('TZ')
        os.environ['TZ'] = 'America/New_York'
        time.tzset()
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                self.client.get_orders_for_account(
                        ACCOUNT_HASH, from_entered_datetime=value)
        finally:
            if original is None:
                del os.environ['TZ']
            else:
                os.environ['TZ'] = original
            time.tzset()

        # Only the parameter under test: the other end of the range defaults
        # to now, so asserting the whole dict would compare against the clock.
        params = self.mock_session.get.call_args[1]['params']
        self.assertEqual('2024-06-05T04:03:02Z', params['fromEnteredTime'])
        messages = [str(w.message) for w in caught]
        self.assertEqual(
                1, len([m for m in messages if 'no timezone' in m]), messages)


    @no_duplicates
    def test_utc_aware_datetime_is_unchanged(self):
        # The same instant already expressed in UTC must serialize identically,
        # so the conversion cannot be doing something zone-specific.
        aware = datetime.datetime(year=2024, month=6, day=5,
                                  hour=4, minute=3, second=2,
                                  tzinfo=datetime.timezone.utc)

        self.client.get_orders_for_account(
                ACCOUNT_HASH,
                from_entered_datetime=aware,
                to_entered_datetime=aware)

        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': '2024-06-05T04:03:02Z',
                'toEnteredTime': '2024-06-05T04:03:02Z',
            })


    @no_duplicates
    def test_naive_datetime_warns_on_iso_parameters(self):
        naive = datetime.datetime(year=2024, month=6, day=5,
                                  hour=4, minute=3, second=2)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.client.get_orders_for_account(
                    ACCOUNT_HASH, from_entered_datetime=naive)

        messages = [str(w.message) for w in caught]
        self.assertTrue(
                any('no timezone' in m for m in messages),
                'expected a naive-datetime warning, got {}'.format(messages))
        self.assertTrue(
                any('from_entered_datetime' in m for m in messages),
                'the warning should name the parameter, got {}'.format(messages))


    @no_duplicates
    def test_naive_datetime_warns_on_millisecond_parameters(self):
        naive = datetime.datetime(year=2024, month=6, day=5,
                                  hour=4, minute=3, second=2)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.client.get_price_history(SYMBOL, start_datetime=naive)

        messages = [str(w.message) for w in caught]
        self.assertTrue(
                any('no timezone' in m for m in messages),
                'expected a naive-datetime warning, got {}'.format(messages))
        self.assertTrue(
                any('start_datetime' in m for m in messages),
                'the warning should name the parameter, got {}'.format(messages))


    @no_duplicates
    def test_naive_datetime_warning_points_at_the_caller(self):
        # A warning which names a line inside this library tells the caller
        # nothing they can act on -- they cannot find their own offending call.
        # The number of frames to skip is not constant: get_price_history_every_day
        # calls get_price_history, and get_orders_for_account goes through
        # _make_order_query, so the wrappers sit one frame deeper than the
        # endpoints they wrap.
        # The exact line matters, not just the file: an off-by-one lands on
        # whatever called this test, which is still this file and would pass a
        # filename-only assertion while being wrong.
        naive = datetime.datetime(year=2024, month=6, day=5, hour=4)
        here = inspect.currentframe()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            line = here.f_lineno + 1
            self.client.get_price_history(SYMBOL, start_datetime=naive)
        self._assert_blamed(caught, line)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            line = here.f_lineno + 1
            self.client.get_price_history_every_day(SYMBOL, start_datetime=naive)
        self._assert_blamed(caught, line)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            line = here.f_lineno + 1
            self.client.get_orders_for_account(ACCOUNT_HASH, from_entered_datetime=naive)
        self._assert_blamed(caught, line)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            line = here.f_lineno + 1
            self.client.get_transactions(ACCOUNT_HASH, start_date=naive)
        self._assert_blamed(caught, line)

    @no_duplicates
    def test_stacklevel_never_blames_the_library_itself(self):
        # warnings.warn treats stacklevel=0 as "blame the frame that called
        # warn", which is base.py -- the failure this machinery exists to
        # avoid. Reachable whenever a frame's co_filename does not resolve
        # under the package root, as a zipimport or a frozen build can manage.
        with patch.object(type(self.client), '_PACKAGE_ROOT',
                          '/nowhere/that/exists'):
            self.assertGreaterEqual(self.client._caller_stacklevel(), 1)

    @no_duplicates
    def test_a_stack_entirely_inside_the_package_still_blames_somebody(self):
        """The fallback after the walk runs out of frames.

        `_caller_stacklevel` never returns 0, because `warnings.warn` reads 0
        as "blame the frame which called warn" -- this library -- which is the
        exact failure the function exists to avoid. A zipimport or a frozen
        build is how a frame's `co_filename` stops resolving under the
        package root.

        Under pytest the stack always contains frames outside the package, so
        the loop always returns early and an ordinary call never reaches the
        fallback. Every filename is made to look internal instead, which is
        the same shape as every frame being unresolvable, and takes the walk
        to the end.
        """
        # Choosing a clever `_PACKAGE_ROOT` or patching `os.path.abspath`
        # does not make every frame look internal, each for its own reason:
        #
        #  * `''` gives a prefix of `\` on Windows, which no absolute path
        #    starts with, so the walk returns at the first frame.
        #  * this file's drive gives `D:\` on a GitHub runner, where the
        #    checkout is on D: and the interpreter under C:\hostedtoolcache,
        #    so the stdlib frames are foreign and the walk stops midway.
        #  * patching `os.path.abspath` reaches `posixpath` itself, since
        #    base.py does `import os` -- so it redirects every call in the
        #    process for the duration, including coverage's own
        #    `canonical_filename`, which would then record a newly traced file
        #    as living under `schwaby/`.
        #
        # The first two are green on Linux and red on `windows-latest`, which
        # runs on pull requests and tags.
        #
        # So supply the frames instead. `_caller_stacklevel` walks whatever
        # `inspect.currentframe()` gives it and reads `co_filename` off each
        # one, so a synthetic chain settles the question with no filesystem,
        # no path semantics, and nothing patched outside this module.
        internal = self.client._PACKAGE_ROOT + os.sep + 'inside.py'
        foreign = os.path.join('elsewhere', 'outside.py')

        def chain(*filenames):
            frame = None
            for name in reversed(filenames):
                frame = SimpleNamespace(
                        f_code=SimpleNamespace(co_filename=name), f_back=frame)
            return frame

        # Every frame internal: the walk runs off the end and takes the
        # fallback, which must never be 0 -- `warnings.warn` reads 0 as
        # "blame the frame which called warn", which is this library.
        with patch('schwaby.client.base.inspect.currentframe',
                   return_value=chain(*([internal] * 12))):
            self.assertEqual(1, self.client._caller_stacklevel())

        # The control. The same walk with a foreign frame in it returns that
        # frame's depth, so the 1 above is the fallback rather than the value
        # any stack happens to produce.
        with patch('schwaby.client.base.inspect.currentframe',
                   return_value=chain(internal, internal, internal, foreign)):
            self.assertEqual(3, self.client._caller_stacklevel())

    @no_duplicates
    def test_a_sibling_package_is_not_mistaken_for_ours(self):
        """A bare prefix test matches '.../schwabytools' against a root of
        '.../schwaby', so frames from an unrelated package are skipped as if
        they were ours and the blame lands a frame too far out.

        The sibling's name has to share the package's own name as a prefix.
        A sibling named after any other prefix collides with nothing, so the
        fixture would go quietly inert and the mutation would stop being
        caught, with the test still passing.

        This needs a real module in a real sibling directory. Patching
        _PACKAGE_ROOT cannot stand it up, because the walk starts at this
        library's own frame -- any root not covering base.py makes that frame
        foreign and the walk stops at once, whichever way the comparison is
        written. So build the layout on disk: symlink the package in as
        'schwaby', put 'schwabytools' beside it, and call from inside that.
        """
        import os
        import subprocess
        import tempfile

        package = os.path.dirname(os.path.dirname(os.path.abspath(
                schwaby.client.base.__file__)))

        with tempfile.TemporaryDirectory() as tmp:
            os.symlink(package, os.path.join(tmp, 'schwaby'))
            sibling = os.path.join(tmp, 'schwabytools')
            os.mkdir(sibling)

            with open(os.path.join(sibling, '__init__.py'), 'w') as f:
                f.write('')
            with open(os.path.join(sibling, 'caller.py'), 'w') as f:
                f.write(
                    'import datetime\n'
                    'def go(client):\n'
                    '    client.get_price_history(\n'
                    '            "AAPL",\n'
                    '            start_datetime=datetime.datetime(2024, 6, 5))\n')

            script = (
                'import sys, warnings\n'
                'sys.path.insert(0, {tmp!r})\n'
                'from schwaby.client import Client\n'
                'import schwabytools.caller as caller\n'
                'class S:\n'
                '    timeout = None\n'
                '    def get(self, *a, **k):\n'
                '        class R:\n'
                '            status_code = 200\n'
                '            text = "{{}}"\n'
                '        return R()\n'
                'with warnings.catch_warnings(record=True) as w:\n'
                '    warnings.simplefilter("always")\n'
                '    caller.go(Client("k", S()))\n'
                'for x in w:\n'
                '    if "no timezone" in str(x.message):\n'
                '        print(x.filename)\n'
            ).format(tmp=tmp)

            blamed = subprocess.run(
                    [sys.executable, '-c', script],
                    capture_output=True, text=True, cwd=tmp).stdout.strip()

        self.assertTrue(
                blamed.endswith(os.path.join('schwabytools', 'caller.py')),
                'the warning should name the sibling package that made the '
                'call, but named {!r}'.format(blamed))

    def _assert_blamed(self, caught, expected_line):
        naive_warnings = [w for w in caught if 'no timezone' in str(w.message)]
        self.assertTrue(naive_warnings, 'expected a naive-datetime warning')
        for w in naive_warnings:
            self.assertEqual(
                    (__file__, expected_line), (w.filename, w.lineno),
                    'warning blamed {}:{} rather than the calling line'.format(
                        w.filename, w.lineno))


    @no_duplicates
    def test_the_librarys_own_start_default_does_not_warn(self):
        # Omitting the dates is the common case. Warning there tells the caller
        # to attach a timezone to a datetime they never wrote, and points at
        # their line to do it.
        #
        # Scoped to start_datetime, the default this test is about. The
        # end_datetime default is timezone-aware as well.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.client.get_price_history_every_day(SYMBOL)
            self.client.get_price_history(SYMBOL)
            self.client.get_orders_for_account(ACCOUNT_HASH)
            self.client.get_transactions(ACCOUNT_HASH)

        blamed = [str(w.message) for w in caught
                  if 'no timezone' in str(w.message)
                  and 'start_datetime' in str(w.message)]
        self.assertEqual([], blamed)


    @no_duplicates
    def test_the_default_start_date_does_not_depend_on_the_host(self):
        # It becomes epoch milliseconds, so a naive default would shift with
        # whatever timezone the machine happens to be set to. Only the
        # per-frequency helpers fill the dates in; raw get_price_history sends
        # what it is given.
        self.client.get_price_history_every_day(SYMBOL)
        params = self.mock_session.get.call_args[1]['params']
        self.assertEqual(
                int(datetime.datetime(
                        1971, 1, 1,
                        tzinfo=datetime.timezone.utc).timestamp() * 1000),
                params['startDate'])


    @no_duplicates
    def test_aware_datetime_does_not_warn(self):
        aware = datetime.datetime(year=2024, month=6, day=5,
                                  hour=4, minute=3, second=2,
                                  tzinfo=datetime.timezone.utc)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.client.get_orders_for_account(
                    ACCOUNT_HASH,
                    from_entered_datetime=aware, to_entered_datetime=aware)
            self.client.get_price_history(
                    SYMBOL, start_datetime=aware, end_datetime=aware)

        naive_warnings = [str(w.message) for w in caught
                          if 'no timezone' in str(w.message)]
        self.assertEqual([], naive_warnings)


    @no_duplicates
    def test_date_object_does_not_warn(self):
        # A date has no time of day to be ambiguous about.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.client.get_orders_for_account(
                    ACCOUNT_HASH,
                    from_entered_datetime=datetime.date(2024, 6, 5))

        naive_warnings = [str(w.message) for w in caught
                          if 'no timezone' in str(w.message)]
        self.assertEqual([], naive_warnings)


    # Token refresh failures


    @no_duplicates
    def test_oauth_error_becomes_a_schwab_exception(self):
        # The refresh happens on the way past an ordinary request, so this is
        # what an application which never touches the token directly sees.
        self.mock_session.get.side_effect = OAuthError(
                error='unsupported_token_type',
                description='Bad refresh_token')

        with self.assertRaises(TokenRefreshError):
            self.client.get_quote(SYMBOL)


    @no_duplicates
    def test_token_refresh_error_preserves_the_original(self):
        original = OAuthError(error='unsupported_token_type',
                              description='Bad refresh_token')
        self.mock_session.get.side_effect = original

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertIs(original, cm.exception.__cause__)
        self.assertIn('Bad refresh_token', str(cm.exception))


    @no_duplicates
    def test_token_refresh_error_escapes_the_endpoints_text(self):
        # The endpoint's code and description reach this message and the
        # chained error's line in a traceback, and both end up in logs. The
        # chained error is the same object, its attributes as authlib set
        # them, since callers classify a refresh by walking the chain.
        import traceback
        description = 'line one\nline two \x1b[31m' + 'y' * 500
        original = OAuthError(error='invalid_grant\nFORGED',
                              description=description)
        self.mock_session.get.side_effect = original

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertIs(original, cm.exception.__cause__)
        self.assertEqual('invalid_grant\nFORGED', original.error)
        self.assertEqual(description, original.description)
        self.assertIn('invalid_grant\\nFORGED', str(cm.exception))
        logged = ''.join(traceback.format_exception(cm.exception))
        for raw in ('\nFORGED', 'line one\nline two', '\x1b', 'y' * 300):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, logged)


    @no_duplicates
    def test_token_refresh_error_keeps_this_librarys_own_description(self):
        # An unusable token response is refused with this library's text,
        # which is longer than the cut and says what a usable token needs.
        import httpx2
        from schwaby import auth
        request = httpx2.Request(
                'POST', 'https://api.schwabapi.com/v1/oauth/token')
        with self.assertRaises(OAuthError) as refused:
            auth._refuse_unusable_token_response(httpx2.Response(
                    200, json={'message': 'Unauthorized'}, request=request))
        self.mock_session.get.side_effect = refused.exception

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertIn('no refresh token that is empty or not a string',
                      str(cm.exception))


    @no_duplicates
    def test_token_refresh_error_reports_token_age(self):
        # The age is the signal worth reasoning about: Schwab documents the
        # seven day term but not what it returns when the term expires.
        eight_days = 8 * 60 * 60 * 24

        metadata = Mock()
        metadata.token_age.return_value = eight_days
        self.client.token_metadata = metadata

        self.mock_session.get.side_effect = OAuthError(
                error='unsupported_token_type',
                description='Bad refresh_token')

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertEqual(eight_days, cm.exception.token_age)
        self.assertIn('8.0 days old', str(cm.exception))


    @no_duplicates
    def test_token_refresh_error_reports_a_decimal_token_age(self):
        # A token store such as DynamoDB returns the creation timestamp as a
        # Decimal, so the age is one too, and the advice must not fail on it.
        eight_days = decimal.Decimal(8 * 60 * 60 * 24)

        metadata = Mock()
        metadata.token_age.return_value = eight_days
        self.client.token_metadata = metadata

        self.mock_session.get.side_effect = OAuthError(
                error='unsupported_token_type',
                description='Bad refresh_token')

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertEqual(eight_days, cm.exception.token_age)
        self.assertIn('8.0 days old', str(cm.exception))


    @no_duplicates
    def test_a_retryable_refusal_past_seven_days_says_to_alert(self):
        # Schwab has not held exactly to its seven days, so the verdict stays
        # retryable, and the message is what says someone should know.
        self.mock_session.get.side_effect = OAuthError(
                error='invalid_client', description='Client not recognized')
        for age, alerts in ((8 * 86400, True),
                            (decimal.Decimal(8 * 86400), True),
                            (7 * 86400, False), (86400, False), (None, False)):
            with self.subTest(age=age):
                if age is None:
                    self.client.token_metadata = None
                else:
                    metadata = Mock()
                    metadata.token_age.return_value = age
                    self.client.token_metadata = metadata

                with self.assertRaises(TokenRefreshError) as cm:
                    self.client.get_quote(SYMBOL)

                self.assertFalse(cm.exception.refresh_token_invalid)
                self.assertEqual(alerts, 'alert someone' in str(cm.exception))

        # A terminal refusal already says to log in again.
        metadata = Mock()
        metadata.token_age.return_value = 8 * 86400
        self.client.token_metadata = metadata
        self.mock_session.get.side_effect = OAuthError(
                error='invalid_grant', description='expired')
        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)
        self.assertTrue(cm.exception.refresh_token_invalid)
        self.assertNotIn('alert someone', str(cm.exception))

    @no_duplicates
    def test_token_refresh_error_without_metadata(self):
        # Clients built directly, as the tests do, have no token metadata.
        self.assertIsNone(self.client.token_metadata)

        self.mock_session.get.side_effect = OAuthError(
                error='unsupported_token_type',
                description='Bad refresh_token')

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertIsNone(cm.exception.token_age)
        self.assertIn('age is unknown', str(cm.exception))


    @no_duplicates
    def test_network_errors_are_not_relabelled_as_token_errors(self):
        # A connection failure while refreshing and one while fetching a quote
        # are the same problem, and are not distinguishable from here. Claiming
        # the token is at fault would be a guess.
        self.mock_session.get.side_effect = httpx2.ConnectError('no route')

        with self.assertRaises(httpx2.ConnectError):
            self.client.get_quote(SYMBOL)


    @no_duplicates
    def test_successful_requests_are_unaffected(self):
        self.client.get_quote(SYMBOL)
        self.mock_session.get.assert_called_once()


    # Is the refresh token dead, or was this just a bad day?


    #: Schwab's answer, observed 2026-08-02, to a refresh token past its
    #: seven day expiry. Note the outer
    #: code is unsupported_token_type, which RFC 7009 defines for the
    #: revocation endpoint and which describes nothing that happened, and that
    #: the real answer is a JSON string nested in the description.
    OBSERVED_DEAD_REFRESH_TOKEN = OAuthError(
            error='unsupported_token_type',
            description='400 Bad Request: {"error_description":"Refresh token '
                        'is invalid, expired or revoked","error":'
                        '"invalid_grant"}')

    @no_duplicates
    def test_observed_dead_refresh_token_is_recognized(self):
        self.mock_session.get.side_effect = self.OBSERVED_DEAD_REFRESH_TOKEN

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertTrue(cm.exception.refresh_token_invalid)
        self.assertIn('retrying will not help', str(cm.exception))


    @no_duplicates
    def test_a_description_that_is_not_a_string_is_not_terminal(self):
        # `description` is whatever the exception carries, and the two reads
        # after this one are `.find('{')` and `.rfind('}')`, which a non-string
        # does not have.
        #
        # False is the right answer rather than an exception, for the reason
        # the docstring gives: a failure wrongly called terminal stops an
        # application that only needed to retry, and this path runs when the
        # application is already in trouble.
        for description in (None, 400, {'error': 'invalid_grant'},
                            ['invalid_grant'], b'invalid_grant'):
            with self.subTest(description=description):
                error = OAuthError(error='some_other_code')
                error.description = description
                self.mock_session.get.side_effect = error

                with self.assertRaises(TokenRefreshError) as cm:
                    self.client.get_quote(SYMBOL)
                self.assertFalse(cm.exception.refresh_token_invalid)

    @no_duplicates
    def test_a_string_description_is_still_read(self):
        # The positive control. Every assertion above is satisfied by a
        # classifier that returns False for everything, including the observed
        # dead-token response the two tests around this one exist for.
        self.mock_session.get.side_effect = self.OBSERVED_DEAD_REFRESH_TOKEN
        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)
        self.assertTrue(cm.exception.refresh_token_invalid)

    @no_duplicates
    def test_standard_placement_is_also_recognized(self):
        # If Schwab ever puts the code where RFC 6749 says it goes.
        self.mock_session.get.side_effect = OAuthError(
                error='invalid_grant',
                description='Refresh token is invalid, expired or revoked')

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertTrue(cm.exception.refresh_token_invalid)


    @no_duplicates
    def test_locally_unusable_tokens_are_not_reported_as_retryable(self):
        # MissingTokenError and InvalidTokenError are OAuthError subclasses
        # raised by authlib itself, before any HTTP call, when the stored token
        # has no refresh_token or is otherwise unusable. Nothing about them
        # improves with time, so an unattended application told to retry would
        # retry forever on a permanent, purely local condition.
        from authlib.integrations.base_client.errors import (
                MissingTokenError, InvalidTokenError)

        for error in (MissingTokenError(), InvalidTokenError()):
            self.setUp()
            self.mock_session.get.side_effect = error

            with self.assertRaises(TokenRefreshError) as cm:
                self.client.get_quote(SYMBOL)

            self.assertTrue(
                    cm.exception.refresh_token_invalid,
                    '{} is permanent and must not be reported as '
                    'retryable'.format(type(error).__name__))
            self.assertIn('cannot be used', str(cm.exception))


    @no_duplicates
    def test_a_locally_raised_unsupported_token_type_is_not_confused(self):
        # authlib raises UnsupportedTokenTypeError locally with the same error
        # code Schwab uses as its outer wrapper. Without the nested body it is
        # not evidence the refresh token is dead, and must not be read as such
        # -- while authlib will replace the stored token by itself: it has an
        # expiry authlib acts on, and a refresh token to refresh with.
        import time
        from authlib.integrations.base_client.errors import (
                UnsupportedTokenTypeError)

        self.mock_session.token = {'access_token': 'a', 'token_type': 'mac',
                                   'expires_at': int(time.time()) + 3600,
                                   'refresh_token': 'r'}
        self.mock_session.get.side_effect = UnsupportedTokenTypeError()

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertFalse(cm.exception.refresh_token_invalid)
        # Nothing reached Schwab, so the message must not say what it said.
        self.assertIn('nothing was sent to Schwab', str(cm.exception))
        self.assertNotIn('Schwab did not say', str(cm.exception))
        # authlib refreshes 300 seconds early, not when the expiry passes.
        self.assertIn('as that expiry nears', str(cm.exception))

    @no_duplicates
    def test_a_local_unsupported_token_type_without_expiry_is_terminal(self):
        # With no expiry the stored token is never refreshed, so a token
        # authlib cannot send fails the same way on every call and nothing is
        # ever sent: retrying cannot help. A token file holding a refusal
        # body rather than a token looks exactly like this.
        from authlib.integrations.base_client.errors import (
                UnsupportedTokenTypeError)

        self.mock_session.get.side_effect = UnsupportedTokenTypeError()

        # authlib checks an expires_at only when it is an int, so a string
        # there is no expiry either -- and nor is one in milliseconds, which is
        # never reached. It refreshes only with a refresh token, so a good
        # expiry without one is never replaced either.
        import time
        for token in ({'message': 'Unauthorized', 'refresh_token': 'r'},
                      {'access_token': 'a', 'token_type': 'mac',
                       'expires_at': '1789073203.0', 'refresh_token': 'r'},
                      {'access_token': 'a', 'token_type': 'mac',
                       'expires_at': 1789073203000, 'refresh_token': 'r'},
                      # Past the seven days a refresh token lasts.
                      {'access_token': 'a', 'token_type': 'mac',
                       'expires_at': int(time.time()) + 8 * 86400,
                       'refresh_token': 'r'},
                      # A refresh token authlib cannot send: empty, as an
                      # erased one is, or not a string at all.
                      {'access_token': 'a', 'token_type': 'mac',
                       'expires_at': int(time.time()) + 3600,
                       'refresh_token': ''},
                      {'access_token': 'a', 'token_type': 'mac',
                       'expires_at': int(time.time()) + 3600,
                       'refresh_token': {'x': 1}},
                      {'access_token': 'a', 'token_type': 'mac',
                       'expires_at': int(time.time()) + 3600}):
            with self.subTest(token=token):
                self.mock_session.token = token
                with self.assertRaises(TokenRefreshError) as cm:
                    self.client.get_quote(SYMBOL)

                self.assertTrue(cm.exception.refresh_token_invalid)
                self.assertIn('login flow has to be completed again',
                              str(cm.exception))

    @no_duplicates
    def test_a_local_unsupported_token_type_with_an_unreadable_token_stays_retryable(self):
        # When the stored token cannot be read, whether it expires is unknown,
        # and calling a failure terminal wrongly is the worse mistake.
        from authlib.integrations.base_client.errors import (
                UnsupportedTokenTypeError)

        self.mock_session.token = None
        self.mock_session.get.side_effect = UnsupportedTokenTypeError()

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertFalse(cm.exception.refresh_token_invalid)


    @no_duplicates
    def test_other_oauth_failures_are_not_called_terminal(self):
        # Claiming a recoverable failure is terminal stops an application which
        # only needed to try again, so anything unrecognized has to come back
        # False. These are the shapes most likely to be mistaken for the above.
        not_terminal = [
            OAuthError(error='server_error',
                       description='500 Internal Server Error'),
            OAuthError(error='temporarily_unavailable', description=''),
            OAuthError(error='invalid_client',
                       description='400 Bad Request: bad app key'),
            # Right words, wrong place: a description mentioning the phrase but
            # carrying no nested error code.
            OAuthError(error='unsupported_token_type',
                       description='400 Bad Request: refresh token trouble'),
            # Nested body, but a different code.
            OAuthError(error='unsupported_token_type',
                       description='400 Bad Request: {"error":"invalid_client"}'),
            # Nested body which is not JSON at all.
            OAuthError(error='unsupported_token_type',
                       description='400 Bad Request: {not json}'),
            OAuthError(error=None, description=None),
        ]

        for error in not_terminal:
            self.setUp()
            self.mock_session.get.side_effect = error

            with self.assertRaises(TokenRefreshError) as cm:
                self.client.get_quote(SYMBOL)

            self.assertFalse(
                    cm.exception.refresh_token_invalid,
                    '{!r} must not be reported as a dead refresh '
                    'token'.format(str(error)))
            self.assertIn('may be transient', str(cm.exception))


class OrderTerminalStatusesTest(unittest.TestCase):
    """The REST order statuses after which an order stops changing."""

    TERMINAL = ('FILLED', 'REJECTED', 'CANCELED', 'EXPIRED', 'REPLACED')

    @no_duplicates
    def test_a_raw_status_string_tests_directly(self):
        # The reason the set holds values. `Status` is a plain Enum, so
        # `Status.FILLED == 'FILLED'` is False, and a set of members would
        # call the string a REST response carries not terminal, silently.
        for status in self.TERMINAL:
            with self.subTest(status=status):
                self.assertIn(status, Client.Order.TERMINAL_STATUSES)

    @no_duplicates
    def test_a_working_order_is_not_terminal(self):
        # The other side. A set that contained everything would pass the test
        # above, so membership has to be shown to discriminate.
        for status in ('WORKING', 'QUEUED', 'ACCEPTED', 'NEW',
                       'PENDING_CANCEL', 'PENDING_REPLACE', 'AWAITING_UR_OUT'):
            with self.subTest(status=status):
                self.assertNotIn(status, Client.Order.TERMINAL_STATUSES)

    @no_duplicates
    def test_it_is_exactly_the_five(self):
        # Pinned on purpose. Unlike a set that mirrors other code, this is a
        # judgement about which statuses end an order, so widening it should
        # be a decision someone makes here rather than a side effect.
        self.assertEqual(set(self.TERMINAL),
                         set(Client.Order.TERMINAL_STATUSES))

    @no_duplicates
    def test_every_value_is_a_status_schwab_defines(self):
        values = {status.value for status in Client.Order.Status}
        self.assertLessEqual(Client.Order.TERMINAL_STATUSES, values)

    @no_duplicates
    def test_one_definition_serves_both_clients(self):
        self.assertIs(Client.Order.TERMINAL_STATUSES,
                      AsyncClient.Order.TERMINAL_STATUSES)


class ClientTest(_TestClient, unittest.TestCase):
    """
    Subclass set to use Client and MagicMock
    """
    client_class    = Client
    magicmock_class = MagicMock

    def test_close_session(self):
        # Without this the synchronous client would hold its connections
        # until it happened to be collected.
        self.client.close_session()
        self.mock_session.close.assert_called_once_with()

    @no_duplicates
    def test_a_coroutine_pace_is_refused_and_sends_nothing(self):
        import warnings
        async def pace(request):
            pass
        self.client.set_pace(pace)
        with warnings.catch_warnings():
            # The coroutine is closed, not left to warn it was never awaited.
            warnings.simplefilter('error')
            with self.assertRaises(TypeError) as cm:
                self.client.get_quote(SYMBOL)
        self.assertIn('AsyncClient', str(cm.exception))
        self.mock_session.get.assert_not_called()

class AsyncClientTest(_TestClient, unittest.TestCase):
    """
    Subclass set to resync AsyncClient and use AsyncMagicMock
    """
    client_class    = ResyncProxy(AsyncClient)
    magicmock_class = AsyncMagicMock

    @no_duplicates
    def test_an_awaitable_from_the_pace_is_awaited(self):
        # A function returning an awaitable: this proxy runs a coroutine
        # attribute of the client itself, so an async def pace is tested on
        # a real client, in auth_test.
        order = []

        async def wait(request):
            await asyncio.sleep(0)
            order.append(request.purpose)

        def pace(request):
            return wait(request)
        self.client.set_pace(pace)
        self.mock_session.get.side_effect = (
                lambda *a, **k: order.append('sent') or MockResponse({}, 200))
        self.client.get_quote(SYMBOL)
        self.assertEqual(['api', 'sent'], order,
                         'the request went before the pace let it')

    @no_duplicates
    def test_no_rate_limit_retry_holds_in_its_own_task_alone(self):
        # Two tasks at once on one client: the scope one opens is not the
        # other's.
        from unittest.mock import AsyncMock
        session = AsyncMagicMock()
        client = AsyncClient(API_KEY, session)
        client.set_rate_limit_retry(3)
        sends = {}

        async def get(url, **kwargs):
            task = asyncio.current_task().get_name()
            sends[task] = sends.get(task, 0) + 1
            await asyncio.sleep(0)
            return MockResponse({}, 429 if sends[task] == 1 else 200)
        session.get = get

        async def scoped():
            with client.no_rate_limit_retry():
                await asyncio.sleep(0)
                return (await client.get_quote(SYMBOL)).status_code

        async def plain():
            return (await client.get_quote(SYMBOL)).status_code

        async def both():
            return await asyncio.gather(
                    asyncio.create_task(scoped(), name='scoped'),
                    asyncio.create_task(plain(), name='plain'))
        with patch('schwaby.client.asynchronous._sleep', new=AsyncMock()):
            self.assertEqual([429, 200], asyncio.run(both()))
        self.assertEqual({'scoped': 1, 'plain': 2}, sends)

    def test_async_close(self):
        self.client.close_async_session()
