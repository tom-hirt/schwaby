import schwaby
import re
import urllib.parse
import asyncio
import contextlib
import io
import json
import os
import logging
import copy
import sys
import warnings
from .utils import (
        account_preferences, has_diff, MockResponse, no_duplicates,
        TESTDATA)
from unittest.mock import ANY, AsyncMock, call, MagicMock, Mock, patch
from unittest import IsolatedAsyncioTestCase
from schwaby import _redact, streaming
from schwaby.streaming import _MAX_REPORTED_SHAPES

StreamClient = streaming.StreamClient


ACCOUNT_ID = 1000
ACCESS_TOKEN = '0xACCE55'
TOKEN_TIMESTAMP = '2020-05-22T02:12:48+0000'
REQUEST_TIMESTAMP = 1590116673258

CLIENT_CUSTOMER_ID = 'client-customer-id'
CLIENT_CORRELATION_ID = 'client-correlation-id'


# For matching calls in which JSON data is passed as a string
class StringMatchesJson:
    def __init__(self, d):
        self.d = d
    def __eq__(self, other):
        return self.d == json.loads(other)
    def __repr__(self):
        return json.dumps(self.d, indent=4)


class StreamClientTest(IsolatedAsyncioTestCase):

    def setUp(self):
        self.http_client = MagicMock()
        self.client = StreamClient(self.http_client)

        self.http_client.token_metadata.token = {'access_token': ACCESS_TOKEN}

        self.maxDiff = None

        with open(os.path.join(TESTDATA, 'preferences.json'), 'r') as f:
            preferences = json.load(f)
            self.pref_customer_id = \
                    preferences['streamerInfo'][0]['schwabClientCustomerId']
            self.pref_correl_id = \
                    preferences['streamerInfo'][0]['schwabClientCorrelId']

    def account(self, index):
        account = account_preferences()['accounts'][0]
        account['accountNumber'] = str(ACCOUNT_ID + index)

        def parsable_as_int(s):
            try:
                int(s)
                return True
            except ValueError:
                return False
        for key, value in list(account.items()):
            if isinstance(value, str) and not parsable_as_int(value):
                account[key] = value + '-' + str(account['accountNumber'])

        return account

    def request_from_socket_mock(self, socket):
        return json.loads(
            socket.send.call_args_list[0][0][0])['requests'][0]

    def success_response(self, request_id, service, command, msg='success'):
        return {
            'response': [
                {
                    'service': service,
                    'requestid': str(request_id),
                    'command': command,
                    'timestamp': REQUEST_TIMESTAMP,
                    'content': {
                        'code': 0,
                        'msg': msg
                    }
                }
            ]
        }

    def streaming_entry(self, service, command, content=None):
        d = {
            'data': [{
                'service': service,
                'command': command,
                'timestamp': REQUEST_TIMESTAMP
            }]
        }

        if content:
            d['data'][0]['content'] = content

        return d

    def assert_handler_called_once_with(self, handler, expected):
        handler.assert_called_once()
        self.assertEqual(len(handler.call_args_list[0]), 2)
        data = handler.call_args_list[0][0][0] # Mock from <- 3.7 has a bad api

        self.assertFalse(has_diff(data, expected))

    async def login_and_get_socket(self, ws_connect):
        preferences = account_preferences()

        self.http_client.get_user_preferences.return_value = MockResponse(
            preferences, 200)
        socket = AsyncMock()
        ws_connect.return_value = socket

        socket.recv.side_effect = [json.dumps(self.success_response(
            0, 'ADMIN', 'LOGIN'))]

        await self.client.login()

        socket.reset_mock()
        return socket


    # TODO: Revive this test once the contrib module comes back.

    '''
    ##########################################################################
    # Custom JSON Decoder


    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_default_parser_invalid_message(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = ['invalid json']

        # No custom parser
        msg = ('Failed to parse message. This often happens with ' +
               'unknown symbols or other error conditions')
        with self.assertRaisesRegex(schwaby.streaming.UnparsableMessage, msg):
            await self.client.level_one_equity_subs(['GOOG', 'MSFT'])


    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_custom_parser_invalid_message(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = ['invalid json']

        class CustomJsonDecoder(schwaby.contrib.util.StreamJsonDecoder):
            def decode_json_string(_, raw):
                self.assertEqual(raw, 'invalid json')
                return self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')

        self.client.set_json_decoder(CustomJsonDecoder())
        await self.client.level_one_equity_subs(['GOOG', 'MSFT'])


    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_custom_parser_wrong_type(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = ['invalid json']

        with self.assertRaises(ValueError):
            self.client.set_json_decoder('')
    '''


    ##########################################################################
    # Login

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_single_account_success(self, ws_connect):
        preferences = account_preferences()
        preferences['accounts'].clear()
        preferences['accounts'].append(self.account(1))

        self.http_client.get_user_preferences.return_value = MockResponse(
            preferences, 200)
        socket = AsyncMock()
        ws_connect.return_value = socket

        socket.recv.side_effect = [json.dumps(self.success_response(
            0, 'ADMIN', 'LOGIN'))]

        await self.client.login()

        socket.send.assert_awaited_once()
        request = self.request_from_socket_mock(socket)
        self.assertEqual(request['parameters']['Authorization'], ACCESS_TOKEN)
        self.assertEqual(request['parameters']['SchwabClientChannel'],
                         'client-channel')
        self.assertEqual(request['parameters']['SchwabClientFunctionId'],
                         'client-function-id')

        self.assertEqual(request['requestid'], '0')
        self.assertEqual(request['service'], 'ADMIN')
        self.assertEqual(request['command'], 'LOGIN')

        self.assertEqual(request['SchwabClientCustomerId'],
                         CLIENT_CUSTOMER_ID)
        self.assertEqual(request['SchwabClientCorrelId'],
                         CLIENT_CORRELATION_ID)


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_single_account_success_async(self, ws_connect):
        '''
        Same as test_login_single_account_success except the underlying client 
        is asynchronous and returns a coroutine for get_user_preferences.
        '''
        preferences = account_preferences()
        preferences['accounts'].clear()
        preferences['accounts'].append(self.account(1))

        async def get_user_preferences(*args, **kwargs):
            return MockResponse(preferences, 200)

        self.http_client.get_user_preferences = get_user_preferences
        socket = AsyncMock()
        ws_connect.return_value = socket

        socket.recv.side_effect = [json.dumps(self.success_response(
            0, 'ADMIN', 'LOGIN'))]

        await self.client.login()

        socket.send.assert_awaited_once()
        request = self.request_from_socket_mock(socket)
        self.assertEqual(request['parameters']['Authorization'], ACCESS_TOKEN)
        self.assertEqual(request['parameters']['SchwabClientChannel'],
                         'client-channel')
        self.assertEqual(request['parameters']['SchwabClientFunctionId'],
                         'client-function-id')

        self.assertEqual(request['requestid'], '0')
        self.assertEqual(request['service'], 'ADMIN')
        self.assertEqual(request['command'], 'LOGIN')

        self.assertEqual(request['SchwabClientCustomerId'],
                         'client-customer-id')
        self.assertEqual(request['SchwabClientCorrelId'],
                         'client-correlation-id')


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_bad_response(self, ws_connect):
        preferences = account_preferences()
        preferences['accounts'].clear()

        self.http_client.get_user_preferences.return_value = MockResponse(
            preferences, 200)
        socket = AsyncMock()
        ws_connect.return_value = socket

        response = self.success_response(0, 'ADMIN', 'LOGIN')
        response['response'][0]['content']['code'] = 21
        response['response'][0]['content']['msg'] = 'failed for some reason'
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.login()


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_ssl_context(self, ws_connect):
        self.client = StreamClient(self.http_client, ssl_context='ssl_context')

        self.http_client.get_user_preferences.return_value = MockResponse(
            account_preferences(), 200)
        socket = AsyncMock()
        ws_connect.return_value = socket

        socket.recv.side_effect = [json.dumps(self.success_response(
            0, 'ADMIN', 'LOGIN'))]

        await self.client.login()

        ws_connect.assert_awaited_once_with(
                ANY, ssl='ssl_context', logger=_redact.websocket_logger())


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_websocket_connect_args(self, ws_connect):
        self.client = StreamClient(self.http_client, ssl_context='ssl_context')

        self.http_client.get_user_preferences.return_value = MockResponse(
            account_preferences(), 200)
        socket = AsyncMock()
        ws_connect.return_value = socket

        socket.recv.side_effect = [json.dumps(self.success_response(
            0, 'ADMIN', 'LOGIN'))]

        await self.client.login(websocket_connect_args={'args': 'yes'})

        ws_connect.assert_awaited_once_with(
                ANY, ssl='ssl_context', args='yes',
                logger=_redact.websocket_logger())


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_websocket_connect_args_extra_headers_refused(
            self, ws_connect):
        # This used to be translated to additional_headers with a
        # DeprecationWarning. websocket_connect_args is documented as a
        # passthrough, and quietly rewriting a name on the way through is not
        # one; the caller is told to change it instead.
        self.http_client.get_user_preferences.return_value = MockResponse(
            account_preferences(), 200)
        socket = AsyncMock()
        ws_connect.return_value = socket

        with self.assertRaises(ValueError) as cm:
            await self.client.login(websocket_connect_args={
                'extra_headers': {'X-Custom-Header': 'value'}})

        # The message has to name the replacement, or it is just a refusal.
        self.assertIn('additional_headers', str(cm.exception))
        ws_connect.assert_not_awaited()


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_does_not_mutate_the_callers_connect_args(
            self, ws_connect):
        # login adds `ssl` to these before handing them to websockets. The
        # dict belongs to the caller, who may well be holding one and reusing
        # it across logins -- it must not come back with our key in it.
        #
        # This guards the invariant rather than any particular change to it:
        # the version this replaced also copied, just at the top of the
        # function instead of the bottom. It is here because the copy is easy
        # to drop while rearranging, and nothing else would notice.
        self.client = StreamClient(self.http_client, ssl_context='ssl_context')

        self.http_client.get_user_preferences.return_value = MockResponse(
            account_preferences(), 200)
        socket = AsyncMock()
        ws_connect.return_value = socket
        socket.recv.side_effect = [json.dumps(self.success_response(
            0, 'ADMIN', 'LOGIN'))]

        connect_args = {'args': 'yes'}
        await self.client.login(websocket_connect_args=connect_args)

        self.assertEqual({'args': 'yes'}, connect_args)
        # ...while websockets did receive the addition.
        ws_connect.assert_awaited_once_with(
                ANY, ssl='ssl_context', args='yes',
                logger=_redact.websocket_logger())


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_websocket_connect_args_both_header_names(
            self, ws_connect):
        # Supplying the new name alongside the old one does not rescue the old
        # one. A caller migrating might reasonably add additional_headers and
        # leave extra_headers behind; the stale key would then be passed
        # straight through to websockets as an unexpected kwarg. Refusing here
        # names it instead.
        self.http_client.get_user_preferences.return_value = MockResponse(
            account_preferences(), 200)
        socket = AsyncMock()
        ws_connect.return_value = socket

        with self.assertRaises(ValueError) as cm:
            await self.client.login(websocket_connect_args={
                'extra_headers': {'X-Custom-Header': 'value'},
                'additional_headers': {'X-Custom-Header': 'value'}})

        # Not the rename message: telling a caller who already passes
        # additional_headers to "pass additional_headers instead" names what
        # they are doing. Assert on the distinguishing word rather than just
        # the field name, or both messages satisfy the test.
        message = str(cm.exception)
        self.assertIn('extra_headers', message)
        self.assertIn('both', message)
        self.assertNotIn('instead', message)
        ws_connect.assert_not_awaited()


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_websocket_connect_args_removed(self, ws_connect):
        self.http_client.get_user_preferences.return_value = MockResponse(
            account_preferences(), 200)
        socket = AsyncMock()
        ws_connect.return_value = socket

        for removed in ('create_protocol', 'read_limit'):
            with self.assertRaises(ValueError):
                await self.client.login(
                    websocket_connect_args={removed: object()})

        ws_connect.assert_not_awaited()


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_websocket_connect_args_unaffected(self, ws_connect):
        self.http_client.get_user_preferences.return_value = MockResponse(
            account_preferences(), 200)
        socket = AsyncMock()
        ws_connect.return_value = socket

        socket.recv.side_effect = [json.dumps(self.success_response(
            0, 'ADMIN', 'LOGIN'))]

        with warnings.catch_warnings():
            warnings.simplefilter('error', DeprecationWarning)
            await self.client.login(
                websocket_connect_args={'additional_headers': {'a': 'b'}})

        ws_connect.assert_awaited_once_with(
            ANY, additional_headers={'a': 'b'},
            logger=_redact.websocket_logger())


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_unexpected_request_id(self, ws_connect):
        preferences = account_preferences()
        preferences['accounts'].clear()
        preferences['accounts'].append(self.account(1))

        self.http_client.get_user_preferences.return_value = MockResponse(
            preferences, 200)
        socket = AsyncMock()
        ws_connect.return_value = socket

        response = self.success_response(0, 'ADMIN', 'LOGIN')
        response['response'][0]['requestid'] = 9999
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaisesRegex(schwaby.streaming.UnexpectedResponse,
                                    'unexpected requestid: 9999'):
            await self.client.login()


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_unexpected_service(self, ws_connect):
        preferences = account_preferences()
        preferences['accounts'].clear()
        preferences['accounts'].append(self.account(1))

        self.http_client.get_user_preferences.return_value = MockResponse(
            preferences, 200)
        socket = AsyncMock()
        ws_connect.return_value = socket

        response = self.success_response(0, 'NOT_ADMIN', 'LOGIN')
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaisesRegex(schwaby.streaming.UnexpectedResponse,
                                    "unexpected service: 'NOT_ADMIN'"):
            await self.client.login()


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_unexpected_command(self, ws_connect):
        preferences = account_preferences()
        preferences['accounts'].clear()
        preferences['accounts'].append(self.account(1))

        self.http_client.get_user_preferences.return_value = MockResponse(
            preferences, 200)
        socket = AsyncMock()
        ws_connect.return_value = socket

        response = self.success_response(0, 'ADMIN', 'NOT_LOGIN')
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaisesRegex(schwaby.streaming.UnexpectedResponse,
                                    "unexpected command: 'NOT_LOGIN'"):
            await self.client.login()


    ##########################################################################
    # Logout

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_logout_success(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'ADMIN', 'LOGOUT'))]

        await self.client.logout()

        socket.send.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'ADMIN',
            'command': 'LOGOUT',
            'requestid': '1',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {}
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_logout_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'ADMIN', 'LOGOUT')
        response['response'][0]['content']['code'] = 9
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.logout()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_logout_closes_socket(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'ADMIN', 'LOGOUT'))]

        await self.client.logout()

        socket.close.assert_awaited_once()
        self.assertIsNone(self.client._socket)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_logout_closes_socket_even_when_rejected(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'ADMIN', 'LOGOUT')
        response['response'][0]['content']['code'] = 9
        socket.recv.side_effect = [json.dumps(response)]

        # The stream is finished either way, so a rejected logout must not
        # leave the connection open.
        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.logout()

        socket.close.assert_awaited_once()
        self.assertIsNone(self.client._socket)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_close(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        await self.client.close()

        socket.close.assert_awaited_once()
        self.assertIsNone(self.client._socket)

        # Closing again is not an error, and does not close twice.
        await self.client.close()
        socket.close.assert_awaited_once()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_logout_close_failure_does_not_mask_logout_error(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'ADMIN', 'LOGOUT')
        response['response'][0]['content']['code'] = 9
        socket.recv.side_effect = [json.dumps(response)]
        socket.close.side_effect = OSError('close failed')

        self.client.logger = MagicMock()

        # Closing is cleanup. If it fails it must not replace the error which
        # actually explains what went wrong.
        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.logout()

        self.client.logger.exception.assert_called_once()

    @no_duplicates
    async def test_close_without_login(self):
        # Tearing down a client which never connected must not raise.
        await self.client.close()
        self.assertIsNone(self.client._socket)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_async_context_manager_closes(self, ws_connect):
        async with self.client as c:
            socket = await self.login_and_get_socket(ws_connect)
            self.assertIs(c, self.client)

        socket.close.assert_awaited_once()
        self.assertIsNone(self.client._socket)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_response_timeout(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)
        self.client._response_timeout = 0.05

        # A stream which accepts the request but never answers it. The
        # connection stays healthy, so no websockets error is raised.
        never = asyncio.Event()

        async def never_responds():
            await never.wait()

        socket.recv.side_effect = never_responds

        with self.assertRaises(schwaby.streaming.ResponseTimeoutError) as cm:
            await self.client.account_activity_sub()

        self.assertEqual(cm.exception.service, 'ACCT_ACTIVITY')
        self.assertEqual(cm.exception.command, 'SUBS')

        # The point of the timeout: no lock is left held, so the client is
        # still usable rather than wedged forever.
        self.assertFalse(self.client._read_lock.locked())
        self.assertFalse(self.client._request_lock.locked())

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_response_timeout_disabled(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)
        self.client._response_timeout = None

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'ACCT_ACTIVITY', 'SUBS'))]

        # None means wait forever, which must still work when a response does
        # arrive.
        await self.client.account_activity_sub()

        self.assertFalse(self.client._read_lock.locked())
        self.assertFalse(self.client._request_lock.locked())

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_subscribe_does_not_block_behind_handle_message(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        # A stream which produces nothing until we say so, so handle_message
        # parks inside recv() exactly as it does on a quiet market.
        inbound = asyncio.Queue()

        async def recv():
            return await inbound.get()

        socket.recv.side_effect = recv
        socket.send.reset_mock()

        listener = asyncio.create_task(self.client.handle_message())
        await asyncio.sleep(0)   # let it reach the read

        subscribe = asyncio.create_task(
                self.client.level_one_equity_subs(['SPY']))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # The whole point: the request goes out immediately rather than waiting
        # for a message to arrive first.
        socket.send.assert_awaited_once()

        # The reader delivers the response to the waiting subscribe, without
        # the subscribe ever needing the socket for itself.
        await inbound.put(json.dumps(
            self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')))
        await asyncio.wait_for(subscribe, timeout=5)

        # ... and handle_message is still waiting for a real message, which it
        # gets when one actually arrives.
        stream_item = self.streaming_entry('LEVELONE_EQUITIES', 'SUBS')
        await inbound.put(json.dumps(stream_item))
        await asyncio.wait_for(listener, timeout=5)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_messages_are_dispatched_in_arrival_order(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        inbound = asyncio.Queue()

        async def recv():
            return await inbound.get()

        socket.recv.side_effect = recv

        seen = []
        self.client.add_chart_equity_handler(
                lambda m: seen.append(m['content'][0]['key']))

        first = self.streaming_entry('CHART_EQUITY', 'SUBS',
                                     [{'key': 'FIRST'}])
        second = self.streaming_entry('CHART_EQUITY', 'SUBS',
                                      [{'key': 'SECOND'}])

        # A request is in flight, so it is the one reading. The message it
        # reads is not its response, so it sets it aside for handle_message.
        subscribe = asyncio.create_task(
                self.client.chart_equity_subs(['GOOG']))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await inbound.put(json.dumps(first))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Now a consumer starts reading too, and a second message arrives
        # before the request has been answered.
        listener = asyncio.create_task(self.client.handle_message())
        await asyncio.sleep(0)
        await inbound.put(json.dumps(second))
        await inbound.put(json.dumps(
            self.success_response(1, 'CHART_EQUITY', 'SUBS')))

        await asyncio.wait_for(subscribe, timeout=5)
        await asyncio.wait_for(listener, timeout=5)
        await asyncio.wait_for(self.client.handle_message(), timeout=5)

        # The message which arrived first must be handled first, even though a
        # request was reading when it arrived and a consumer read the next one.
        self.assertEqual(['FIRST', 'SECOND'], seen)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_cancelling_a_request_does_not_wedge_the_client(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        # A request which is reading when it gets cancelled. The cancellation
        # can arrive after the read lock has been acquired but before the
        # acquiring branch is entered, so releasing it has to happen on every
        # exit rather than only the ones that read something.
        never = asyncio.Event()

        async def never_responds():
            await never.wait()

        socket.recv.side_effect = never_responds

        subscribe = asyncio.create_task(
                self.client.level_one_equity_subs(['SPY']))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        subscribe.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await subscribe
        await asyncio.sleep(0)

        self.assertFalse(self.client._read_lock.locked())
        self.assertFalse(self.client._request_lock.locked())
        self.assertIsNone(self.client._pending_request)

        # And the client still works afterwards, which is the point.
        inbound = asyncio.Queue()

        async def recv():
            return await inbound.get()

        socket.recv.side_effect = recv
        again = asyncio.create_task(
                self.client.level_one_equity_subs(['SPY']))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await inbound.put(json.dumps(
            self.success_response(2, 'LEVELONE_EQUITIES', 'SUBS')))
        await asyncio.wait_for(again, timeout=5)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_read_failure_fails_a_waiting_request(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        # A request is in flight when the connection drops. It must fail rather
        # than wait for a reply which can no longer arrive.
        socket.recv.side_effect = ConnectionError('connection lost')

        with self.assertRaises(ConnectionError):
            await self.client.level_one_equity_subs(['SPY'])

        self.assertFalse(self.client._read_lock.locked())
        self.assertFalse(self.client._request_lock.locked())
        self.assertIsNone(self.client._pending_request)

    @no_duplicates
    async def test_response_timeout_is_configurable(self):
        client = StreamClient(self.http_client, response_timeout=12.5)
        self.assertEqual(client._response_timeout, 12.5)

        default = StreamClient(self.http_client)
        self.assertEqual(default._response_timeout,
                         StreamClient.DEFAULT_RESPONSE_TIMEOUT)


    ##########################################################################
    # ACCT_ACTIVITY

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_account_activity_subs_success(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'ACCT_ACTIVITY', 'SUBS'))]

        await self.client.account_activity_sub()
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'ACCT_ACTIVITY',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': CLIENT_CORRELATION_ID,
                'fields': '0,1,2,3'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_handler_exception_does_not_stop_other_handlers(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'ACCT_ACTIVITY', 'SUBS')),
            json.dumps(self.streaming_entry('ACCT_ACTIVITY', 'SUBS'))
        ]

        failing_handler = Mock(side_effect=ValueError('handler failed'))
        succeeding_handler = Mock()
        self.client.add_account_activity_handler(failing_handler)
        self.client.add_account_activity_handler(succeeding_handler)

        await self.client.account_activity_sub()

        # One handler raising must neither propagate to the caller, whose
        # receive loop cannot tell it apart from the stream failing, nor stop
        # the remaining handlers from seeing the message.
        await self.client.handle_message()

        failing_handler.assert_called_once()
        succeeding_handler.assert_called_once()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_async_handler_exception_is_reported(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'ACCT_ACTIVITY', 'SUBS')),
            json.dumps(self.streaming_entry('ACCT_ACTIVITY', 'SUBS'))
        ]

        async_handler = AsyncMock(side_effect=ValueError('handler failed'))
        self.client.add_account_activity_handler(async_handler)
        self.client.logger = MagicMock()

        await self.client.account_activity_sub()
        await self.client.handle_message()

        # The handler is scheduled rather than awaited, so drive it to
        # completion and let its done callback run.
        tasks = set(self.client._handler_tasks)
        self.assertEqual(len(tasks), 1)
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)

        # An async handler which raises must not fail silently just because
        # nothing awaits it.
        self.client.logger.error.assert_called_once()
        self.assertIsInstance(
                self.client.logger.error.call_args[1]['exc_info'], ValueError)

        # The completed task must not be retained.
        self.assertEqual(len(self.client._handler_tasks), 0)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_unparsable_message_shape_does_not_propagate(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        # 'content' is normally a list. Relabeling reads into it, so a message
        # whose shape differs fails before the handler is ever called.
        malformed = self.streaming_entry('ACCT_ACTIVITY', 'SUBS')
        malformed['data'][0]['content'] = 'not-a-list'

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'ACCT_ACTIVITY', 'SUBS')),
            json.dumps(malformed)
        ]

        handler = Mock()
        self.client.add_account_activity_handler(handler)

        await self.client.account_activity_sub()
        await self.client.handle_message()

        handler.assert_not_called()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_notify_without_service_does_not_propagate(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps({'notify': [{'1': 'OrderFill'}]})]

        handler = Mock()
        self.client.add_account_activity_handler(handler)

        await self.client.handle_message()

        handler.assert_not_called()

        # Nor may looking up an unknown service accrete an entry for it.
        self.assertNotIn(None, self.client._handlers)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_account_activity_unsubs_success(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('ACCT_ACTIVITY', 'SUBS')

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'ACCT_ACTIVITY', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'ACCT_ACTIVITY', 'UNSUBS'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_account_activity_handler(handler)
        self.client.add_account_activity_handler(async_handler)

        await self.client.account_activity_sub()
        await self.client.handle_message()
        await self.client.account_activity_unsubs()

        self.assert_handler_called_once_with(
                handler, {'service': 'ACCT_ACTIVITY',
                          'command': 'SUBS',
                          'timestamp': REQUEST_TIMESTAMP})
        self.assert_handler_called_once_with(
                async_handler, {'service': 'ACCT_ACTIVITY',
                                'command': 'SUBS',
                                'timestamp': REQUEST_TIMESTAMP})
        send_awaited = [
            call(StringMatchesJson({
                "requests": [{
                    "service": "ACCT_ACTIVITY",
                    "requestid": "1",
                    "command": "SUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": CLIENT_CORRELATION_ID,
                        "fields": "0,1,2,3"
                    }
                }]
            })),
            call(StringMatchesJson({
                "requests": [{
                    "service": "ACCT_ACTIVITY",
                    "requestid": "2",
                    "command": "UNSUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": CLIENT_CORRELATION_ID
                    }
                }]
            })),
        ]
        socket.send.assert_has_awaits(send_awaited, any_order=False)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_account_activity_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'ACCT_ACTIVITY', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.account_activity_sub()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_account_activity_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'ACCT_ACTIVITY', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.account_activity_unsubs()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_account_activity_handler(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {
            'data': [
                {
                    'service': 'ACCT_ACTIVITY',
                    'timestamp': 1591754497594,
                    'command': 'SUBS',
                    'content': [
                        {
                            'seq': 1,
                            'key': CLIENT_CORRELATION_ID,
                            '1': '1001',
                            '2': 'OrderEntryRequest',
                            '3': ''
                        }
                    ]
                }
            ]
        }

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'ACCT_ACTIVITY', 'SUBS')),
            json.dumps(stream_item)]
        await self.client.account_activity_sub()

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_account_activity_handler(handler)
        self.client.add_account_activity_handler(async_handler)
        await self.client.handle_message()

        expected_item = {
            'service': 'ACCT_ACTIVITY',
            'timestamp': 1591754497594,
            'command': 'SUBS',
            'content': [
                {
                    'seq': 1,
                    'key': CLIENT_CORRELATION_ID,
                    'ACCOUNT': '1001',
                    'MESSAGE_TYPE': 'OrderEntryRequest',
                    'MESSAGE_DATA': ''
                }
            ]
        }

        self.assert_handler_called_once_with(handler, expected_item)
        self.assert_handler_called_once_with(async_handler, expected_item)


    ##########################################################################
    # CHART_EQUITY

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_chart_equity_subs_and_add_success(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'CHART_EQUITY', 'SUBS'))]

        await self.client.chart_equity_subs(['GOOG', 'MSFT'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'CHART_EQUITY',
            'command': 'SUBS',
            'requestid': '1',
            'SchwabClientCustomerId': self.pref_customer_id,
            'SchwabClientCorrelId': self.pref_correl_id,
            'parameters': {
                'keys': 'GOOG,MSFT',
                'fields': '0,1,2,3,4,5,6,7,8'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'CHART_EQUITY', 'ADD'))]

        await self.client.chart_equity_add(['INTC'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'CHART_EQUITY',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': self.pref_customer_id,
            'SchwabClientCorrelId': self.pref_correl_id,
            'parameters': {
                'keys': 'INTC',
                'fields': '0,1,2,3,4,5,6,7,8'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_chart_equity_unsubs_success(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('CHART_EQUITY', 'SUBS')

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_EQUITY', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'CHART_EQUITY', 'UNSUBS', 'UNSUBS command succeeded'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_chart_equity_handler(handler)
        self.client.add_chart_equity_handler(async_handler)

        await self.client.chart_equity_subs(['GOOG', 'MSFT'])
        await self.client.handle_message()
        await self.client.chart_equity_unsubs(['GOOG', 'MSFT'])

        self.assert_handler_called_once_with(
                handler, {'service': 'CHART_EQUITY',
                          'command': 'SUBS',
                          'timestamp': REQUEST_TIMESTAMP})
        self.assert_handler_called_once_with(
                async_handler, {'service': 'CHART_EQUITY',
                                'command': 'SUBS',
                                'timestamp': REQUEST_TIMESTAMP})

        send_awaited = [
            call(StringMatchesJson({
                "requests": [{
                    "service": "CHART_EQUITY",
                    "requestid": "1",
                    "command": "SUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "GOOG,MSFT",
                        "fields": "0,1,2,3,4,5,6,7,8"
                    }
                }]
            })),
            call(StringMatchesJson(
                {"requests": [{
                    "service": "CHART_EQUITY", 
                    "requestid": "2",
                    "command": "UNSUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "GOOG,MSFT"
                    }
                }]
            })),
        ]
        socket.send.assert_has_awaits(send_awaited, any_order=False)


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_chart_equity_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'CHART_EQUITY', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.chart_equity_subs(['GOOG', 'MSFT'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_chart_equity_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'CHART_EQUITY', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.chart_equity_unsubs(['GOOG', 'MSFT'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_chart_equity_add_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response_subs = self.success_response(1, 'CHART_EQUITY', 'SUBS')

        response_add = self.success_response(2, 'CHART_EQUITY', 'ADD')
        response_add['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [
            json.dumps(response_subs),
            json.dumps(response_add)]

        await self.client.chart_equity_subs(['GOOG', 'MSFT'])

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.chart_equity_add(['INTC'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_chart_equity_handler(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {
            'data': [{
                "service": "CHART_EQUITY",
                "timestamp": 1715908546054,
                "command": "SUBS",
                "content": [
                    {
                        "seq": 0,
                        "key": "MSFT",
                        "1": 779,
                        "2": 421.65,
                        "3": 421.79,
                        "4": 421.65,
                        "5": 421.755,
                        "6": 26.0,
                        "7": 1715903940000,
                        "8": 19859
                    },
                    {
                        "seq": 0,
                        "key": "GOOG",
                        "1": 779,
                        "2": 175.16,
                        "3": 175.21,
                        "4": 175.06,
                        "5": 175.06,
                        "6": 145.0,
                        "7": 1715903940000,
                        "8": 19859
                    }
                ]
            }]
        }

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_EQUITY', 'SUBS')),
            json.dumps(stream_item)]
        await self.client.chart_equity_subs(['GOOG', 'MSFT'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_chart_equity_handler(handler)
        self.client.add_chart_equity_handler(async_handler)
        await self.client.handle_message()

        expected_item = {
            "service": "CHART_EQUITY",
            "timestamp": 1715908546054,
            "command": "SUBS",
            "content": [
                {
                    "seq": 0,
                    "key": "MSFT",
                    "SEQUENCE": 779,
                    "OPEN_PRICE": 421.65,
                    "HIGH_PRICE": 421.79,
                    "LOW_PRICE": 421.65,
                    "CLOSE_PRICE": 421.755,
                    "VOLUME": 26.0,
                    "CHART_TIME_MILLIS": 1715903940000,
                    "CHART_DAY": 19859
                },
                {
                    "seq": 0,
                    "key": "GOOG",
                    "SEQUENCE": 779,
                    "OPEN_PRICE": 175.16,
                    "HIGH_PRICE": 175.21,
                    "LOW_PRICE": 175.06,
                    "CLOSE_PRICE": 175.06,
                    "VOLUME": 145.0,
                    "CHART_TIME_MILLIS": 1715903940000,
                    "CHART_DAY": 19859
                }
            ]
        }

        self.assert_handler_called_once_with(handler, expected_item)
        self.assert_handler_called_once_with(async_handler, expected_item)

    ##########################################################################
    # CHART_FUTURES

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_chart_futures_subs_and_add_success(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'CHART_FUTURES', 'SUBS'))]

        await self.client.chart_futures_subs(['/ES', '/CL'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'CHART_FUTURES',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': '/ES,/CL',
                'fields': '0,1,2,3,4,5,6'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'CHART_FUTURES', 'ADD'))]

        await self.client.chart_futures_add(['/ZC'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'CHART_FUTURES',
            'command': 'ADD',
            'requestid': '2',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': '/ZC',
                'fields': '0,1,2,3,4,5,6'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_chart_futures_unsubs_success(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('CHART_FUTURES', 'SUBS')

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_FUTURES', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'CHART_FUTURES', 'UNSUBS', 'UNSUBS command succeeded'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_chart_futures_handler(handler)
        self.client.add_chart_futures_handler(async_handler)

        await self.client.chart_futures_subs(['/ES', '/CL'])
        await self.client.handle_message()
        await self.client.chart_futures_unsubs(['/ES', '/CL'])

        self.assert_handler_called_once_with(handler, {'service': 'CHART_FUTURES', 'command': 'SUBS',
                                                       'timestamp': REQUEST_TIMESTAMP})
        self.assert_handler_called_once_with(async_handler, {'service': 'CHART_FUTURES', 'command': 'SUBS',
                                                             'timestamp': REQUEST_TIMESTAMP})
        send_awaited = [
            call(StringMatchesJson({
                "requests": [{
                    "service": "CHART_FUTURES",
                    "requestid": "1",
                    "command": "SUBS",
                    'SchwabClientCustomerId': self.pref_customer_id,
                    'SchwabClientCorrelId': self.pref_correl_id,
                    "parameters": {
                        "keys": "/ES,/CL",
                        "fields": "0,1,2,3,4,5,6"
                    }
                }]
            })),
            call(StringMatchesJson({
                "requests": [{
                    "service": "CHART_FUTURES", 
                    "requestid": "2",
                    "command": "UNSUBS",
                    'SchwabClientCustomerId': self.pref_customer_id,
                    'SchwabClientCorrelId': self.pref_correl_id,
                    "parameters": {
                        "keys": 
                        "/ES,/CL"
                    }
                }]
            })),
        ]
        socket.send.assert_has_awaits(send_awaited, any_order=False)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_chart_futures_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'CHART_FUTURES', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.chart_futures_subs(['/ES', '/CL'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_chart_futures_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'CHART_FUTURES', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.chart_futures_unsubs(['/ES', '/CL'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_chart_futures_add_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response_subs = self.success_response(1, 'CHART_FUTURES', 'SUBS')

        response_add = self.success_response(2, 'CHART_FUTURES', 'ADD')
        response_add['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [
            json.dumps(response_subs),
            json.dumps(response_add)]

        await self.client.chart_futures_subs(['/ES', '/CL'])

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.chart_futures_add(['/ZC'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_chart_futures_handler(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {
            'data': [
                {
                    'service': 'CHART_FUTURES',
                    'timestamp': 1590597913941,
                    'command': 'SUBS',
                    'content': [
                        {
                            'seq': 0,
                            'key': '/ES',
                            '1': 1590597840000,
                            '2': 2996.25,
                            '3': 2997.25,
                            '4': 2995.25,
                            '5': 2997.25,
                            '6': 1501.0
                        },
                        {
                            'seq': 0,
                            'key': '/CL',
                            '1': 1590597840000,
                            '2': 33.34,
                            '3': 33.35,
                            '4': 33.32,
                            '5': 33.35,
                            '6': 186.0
                        }
                    ]
                }
            ]
        }

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_FUTURES', 'SUBS')),
            json.dumps(stream_item)]
        await self.client.chart_futures_subs(['/ES', '/CL'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_chart_futures_handler(handler)
        self.client.add_chart_futures_handler(async_handler)
        await self.client.handle_message()

        expected_item = {
            'service': 'CHART_FUTURES',
            'timestamp': 1590597913941,
            'command': 'SUBS',
            'content': [{
                'seq': 0,
                'key': '/ES',
                'CHART_TIME_MILLIS': 1590597840000,
                'OPEN_PRICE': 2996.25,
                'HIGH_PRICE': 2997.25,
                'LOW_PRICE': 2995.25,
                'CLOSE_PRICE': 2997.25,
                'VOLUME': 1501.0
            }, {
                'seq': 0,
                'key': '/CL',
                'CHART_TIME_MILLIS': 1590597840000,
                'OPEN_PRICE': 33.34,
                'HIGH_PRICE': 33.35,
                'LOW_PRICE': 33.32,
                'CLOSE_PRICE': 33.35,
                'VOLUME': 186.0
            }]
        }

        self.assert_handler_called_once_with(handler, expected_item)
        self.assert_handler_called_once_with(async_handler, expected_item)

    ##########################################################################
    # LEVELONE_EQUITIES

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_equity_subs_and_add_success_all_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_EQUITIES', 'SUBS'))]

        await self.client.level_one_equity_subs(['GOOG', 'MSFT'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_EQUITIES',
            'command': 'SUBS',
            'requestid': '1',
            'SchwabClientCustomerId': self.pref_customer_id,
            'SchwabClientCorrelId': self.pref_correl_id,
            'parameters': {
                'keys': 'GOOG,MSFT',
                'fields': ('0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,' +
                           '20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,' +
                           '36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,' +
                           '52,53,54,55,56')
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_EQUITIES', 'ADD'))]

        await self.client.level_one_equity_add(['INTC'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_EQUITIES',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': self.pref_customer_id,
            'SchwabClientCorrelId': self.pref_correl_id,
            'parameters': {
                'keys': 'INTC',
                'fields': ('0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,' +
                           '20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,' +
                           '36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,' +
                           '52,53,54,55,56')
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_equity_unsubs_success(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('LEVELONE_EQUITIES', 'SUBS')

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'LEVELONE_EQUITIES', 'UNSUBS'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_level_one_equity_handler(async_handler)
        self.client.add_level_one_equity_handler(handler)

        await self.client.level_one_equity_subs(['GOOG', 'MSFT'])
        await self.client.handle_message()
        await self.client.level_one_equity_unsubs(['GOOG', 'MSFT'])

        self.assert_handler_called_once_with(handler,
                                             {'service': 'LEVELONE_EQUITIES', 'command': 'SUBS', 'timestamp': REQUEST_TIMESTAMP})
        self.assert_handler_called_once_with(async_handler,
                                             {'service': 'LEVELONE_EQUITIES', 'command': 'SUBS', 'timestamp': REQUEST_TIMESTAMP})
        send_awaited = [
            call(StringMatchesJson({
                "requests": [{
                    "service": "LEVELONE_EQUITIES",
                    "requestid": "1",
                    "command": "SUBS", 
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "GOOG,MSFT", 
                        "fields": 
                        "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,"+
                        "20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,"+
                        "37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,52,53,"+
                        "54,55,56"
                    }
                }]
            })),
            call(StringMatchesJson({
                "requests": [{
                    "service": "LEVELONE_EQUITIES",
                    "requestid": "2",
                    "command": "UNSUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "GOOG,MSFT"
                    }
                }]
            })),
        ]

        socket.send.assert_has_awaits(send_awaited, any_order=False)


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_equity_subs_and_add_success_some_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_EQUITIES', 'SUBS'))]

        await self.client.level_one_equity_subs(['GOOG', 'MSFT'], fields=[
            StreamClient.LevelOneEquityFields.SYMBOL,
            StreamClient.LevelOneEquityFields.BID_PRICE,
            StreamClient.LevelOneEquityFields.ASK_PRICE,
            StreamClient.LevelOneEquityFields.LOW_PRICE,
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_EQUITIES',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'GOOG,MSFT',
                'fields': '0,1,2,11'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_EQUITIES', 'ADD'))]

        await self.client.level_one_equity_add(['INTC'], fields=[
            StreamClient.LevelOneEquityFields.SYMBOL,
            StreamClient.LevelOneEquityFields.BID_PRICE,
            StreamClient.LevelOneEquityFields.ASK_PRICE,
            StreamClient.LevelOneEquityFields.LOW_PRICE,
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_EQUITIES',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'INTC',
                'fields': '0,1,2,11'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_equity_subs_and_add_success_some_fields_no_symbol(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_EQUITIES', 'SUBS'))]

        await self.client.level_one_equity_subs(['GOOG', 'MSFT'], fields=[
            StreamClient.LevelOneEquityFields.BID_PRICE,
            StreamClient.LevelOneEquityFields.ASK_PRICE,
            StreamClient.LevelOneEquityFields.LOW_PRICE,
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_EQUITIES',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'GOOG,MSFT',
                'fields': '0,1,2,11'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_EQUITIES', 'ADD'))]

        await self.client.level_one_equity_add(['INTC'], fields=[
            StreamClient.LevelOneEquityFields.BID_PRICE,
            StreamClient.LevelOneEquityFields.ASK_PRICE,
            StreamClient.LevelOneEquityFields.LOW_PRICE,
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_EQUITIES',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'INTC',
                'fields': '0,1,2,11'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_equity_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_equity_subs(['GOOG', 'MSFT'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_equity_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_EQUITIES', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_equity_unsubs(['GOOG', 'MSFT'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_equity_add_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_EQUITIES', 'ADD')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_equity_add(['GOOG', 'MSFT'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_quote_handler(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {
            'data': [{
                "service": "LEVELONE_EQUITIES",
                "timestamp": 1715777956144,
                "command": "SUBS",
                "content": [
                    {
                        "key": "MSFT",
                        "delayed": True,
                        "assetMainType": "EQUITY",
                        "assetSubType": "COE",
                        "cusip": "594918104",
                        "1": 417.01,
                        "2": 417.08,
                        "3": 417.09,
                        "4": 1,
                        "5": 0,
                        "6": "Z",
                        "7": "Z",
                        "8": 162750,
                        "9": 10,
                        "10": 0,
                        "11": 0,
                        "12": 415.81,
                        "13": "Q",
                        "14": False,
                        "15": "Microsoft Corp",
                        "16": "Q",
                        "17": 0,
                        "18": 1.28,
                        "19": 430.82,
                        "20": 307.59,
                        "21": 36.09605,
                        "22": 3,
                        "23": 0.72513,
                        "24": 0,
                        "25": "NASDAQ",
                        "26": "2024-05-15 00:00:00.0",
                        "27": False,
                        "28": False,
                        "29": 415.81,
                        "30": 408,
                        "31": 0,
                        "32": "Unknown",
                        "33": 417.01,
                        "34": 1715777955574,
                        "35": 1715777953982,
                        "36": 1715716801392,
                        "37": 1715777955574,
                        "38": 1715777955574,
                        "39": "BATS",
                        "40": "BATS",
                        "41": "XBOS",
                        "42": 0.3078329,
                        "43": 0,
                        "44": 1.2,
                        "45": 0.28859335,
                        "46": 95711793,
                        "47": 0,
                        "48": 0,
                        "49": 1,
                        "50": 1.28,
                        "51": 0.3078329
                    },
                    {
                        "key": "GOOG",
                        "delayed": True,
                        "assetMainType": "EQUITY",
                        "assetSubType": "COE",
                        "cusip": "02079K107",
                        "1": 172.75,
                        "2": 172.77,
                        "3": 172.7786,
                        "4": 0,
                        "5": 1,
                        "6": "Q",
                        "7": "Z",
                        "8": 167934,
                        "9": 15,
                        "10": 0,
                        "11": 0,
                        "12": 171.93,
                        "13": "Q",
                        "14": False,
                        "15": "Alphabet Inc. C",
                        "16": "L",
                        "17": 0,
                        "18": 0.8486,
                        "19": 176.42,
                        "20": 115.83,
                        "21": 26.38849,
                        "22": 0.8,
                        "23": 0.46811,
                        "24": 0,
                        "25": "NASDAQ",
                        "26": "2024-06-10 00:00:00.0",
                        "27": False,
                        "28": False,
                        "29": 171.93,
                        "30": 210,
                        "31": 0,
                        "32": "Unknown",
                        "33": 172.75,
                        "34": 1715777954819,
                        "35": 1715777950470,
                        "36": 1715716801295,
                        "37": 1715777954819,
                        "38": 1715777954819,
                        "39": "XBOS",
                        "40": "BATS",
                        "41": "TRFC",
                        "42": 0.49357297,
                        "43": 0,
                        "44": 0.82,
                        "45": 0.47693829,
                        "46": 67295343,
                        "47": 0,
                        "48": 0,
                        "49": 1,
                        "50": 0.8486,
                        "51": 0.49357297
                    }
                ]
            }]
        }

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')),
            json.dumps(stream_item)]
        await self.client.level_one_equity_subs(['GOOG', 'MSFT'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_level_one_equity_handler(handler)
        self.client.add_level_one_equity_handler(async_handler)
        await self.client.handle_message()

        expected_item = {
            "service": "LEVELONE_EQUITIES",
            "timestamp": 1715777956144,
            "command": "SUBS",
            "content": [
                {
                    "key": "MSFT",
                    "delayed": True,
                    "assetMainType": "EQUITY",
                    "assetSubType": "COE",
                    "cusip": "594918104",
                    "BID_PRICE": 417.01,
                    "ASK_PRICE": 417.08,
                    "LAST_PRICE": 417.09,
                    "BID_SIZE": 1,
                    "ASK_SIZE": 0,
                    "ASK_ID": "Z",
                    "BID_ID": "Z",
                    "TOTAL_VOLUME": 162750,
                    "LAST_SIZE": 10,
                    "HIGH_PRICE": 0,
                    "LOW_PRICE": 0,
                    "CLOSE_PRICE": 415.81,
                    "EXCHANGE_ID": "Q",
                    "MARGINABLE": False,
                    "DESCRIPTION": "Microsoft Corp",
                    "LAST_ID": "Q",
                    "OPEN_PRICE": 0,
                    "NET_CHANGE": 1.28,
                    "HIGH_PRICE_52_WEEK": 430.82,
                    "LOW_PRICE_52_WEEK": 307.59,
                    "PE_RATIO": 36.09605,
                    "DIVIDEND_AMOUNT": 3,
                    "DIVIDEND_YIELD": 0.72513,
                    "NAV": 0,
                    "EXCHANGE_NAME": "NASDAQ",
                    "DIVIDEND_DATE": "2024-05-15 00:00:00.0",
                    "REGULAR_MARKET_QUOTE": False,
                    "REGULAR_MARKET_TRADE": False,
                    "REGULAR_MARKET_LAST_PRICE": 415.81,
                    "REGULAR_MARKET_LAST_SIZE": 408,
                    "REGULAR_MARKET_NET_CHANGE": 0,
                    "SECURITY_STATUS": "Unknown",
                    "MARK": 417.01,
                    "QUOTE_TIME_MILLIS": 1715777955574,
                    "TRADE_TIME_MILLIS": 1715777953982,
                    "REGULAR_MARKET_TRADE_MILLIS": 1715716801392,
                    "BID_TIME_MILLIS": 1715777955574,
                    "ASK_TIME_MILLIS": 1715777955574,
                    "ASK_MIC_ID": "BATS",
                    "BID_MIC_ID": "BATS",
                    "LAST_MIC_ID": "XBOS",
                    "NET_CHANGE_PERCENT": 0.3078329,
                    "REGULAR_MARKET_CHANGE_PERCENT": 0,
                    "MARK_CHANGE": 1.2,
                    "MARK_CHANGE_PERCENT": 0.28859335,
                    "HTB_QUANTITY": 95711793,
                    "HTB_RATE": 0,
                    "HARD_TO_BORROW": 0,
                    "IS_SHORTABLE": 1,
                    "POST_MARKET_NET_CHANGE": 1.28,
                    "POST_MARKET_NET_CHANGE_PERCENT": 0.3078329
                },
                {
                    "key": "GOOG",
                    "delayed": True,
                    "assetMainType": "EQUITY",
                    "assetSubType": "COE",
                    "cusip": "02079K107",
                    "BID_PRICE": 172.75,
                    "ASK_PRICE": 172.77,
                    "LAST_PRICE": 172.7786,
                    "BID_SIZE": 0,
                    "ASK_SIZE": 1,
                    "ASK_ID": "Q",
                    "BID_ID": "Z",
                    "TOTAL_VOLUME": 167934,
                    "LAST_SIZE": 15,
                    "HIGH_PRICE": 0,
                    "LOW_PRICE": 0,
                    "CLOSE_PRICE": 171.93,
                    "EXCHANGE_ID": "Q",
                    "MARGINABLE": False,
                    "DESCRIPTION": "Alphabet Inc. C",
                    "LAST_ID": "L",
                    "OPEN_PRICE": 0,
                    "NET_CHANGE": 0.8486,
                    "HIGH_PRICE_52_WEEK": 176.42,
                    "LOW_PRICE_52_WEEK": 115.83,
                    "PE_RATIO": 26.38849,
                    "DIVIDEND_AMOUNT": 0.8,
                    "DIVIDEND_YIELD": 0.46811,
                    "NAV": 0,
                    "EXCHANGE_NAME": "NASDAQ",
                    "DIVIDEND_DATE": "2024-06-10 00:00:00.0",
                    "REGULAR_MARKET_QUOTE": False,
                    "REGULAR_MARKET_TRADE": False,
                    "REGULAR_MARKET_LAST_PRICE": 171.93,
                    "REGULAR_MARKET_LAST_SIZE": 210,
                    "REGULAR_MARKET_NET_CHANGE": 0,
                    "SECURITY_STATUS": "Unknown",
                    "MARK": 172.75,
                    "QUOTE_TIME_MILLIS": 1715777954819,
                    "TRADE_TIME_MILLIS": 1715777950470,
                    "REGULAR_MARKET_TRADE_MILLIS": 1715716801295,
                    "BID_TIME_MILLIS": 1715777954819,
                    "ASK_TIME_MILLIS": 1715777954819,
                    "ASK_MIC_ID": "XBOS",
                    "BID_MIC_ID": "BATS",
                    "LAST_MIC_ID": "TRFC",
                    "NET_CHANGE_PERCENT": 0.49357297,
                    "REGULAR_MARKET_CHANGE_PERCENT": 0,
                    "MARK_CHANGE": 0.82,
                    "MARK_CHANGE_PERCENT": 0.47693829,
                    "HTB_QUANTITY": 67295343,
                    "HTB_RATE": 0,
                    "HARD_TO_BORROW": 0,
                    "IS_SHORTABLE": 1,
                    "POST_MARKET_NET_CHANGE": 0.8486,
                    "POST_MARKET_NET_CHANGE_PERCENT": 0.49357297
                }
            ]
        }

        self.assert_handler_called_once_with(handler, expected_item)
        self.assert_handler_called_once_with(async_handler, expected_item)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_equity_handler_names_the_fields_past_51(
            self, ws_connect):
        # Values from one live item: a print of under one share, which
        # LAST_SIZE reports as 0 and only field 52 carries. Keys are written
        # with double quotes so a failure's repr cannot be read off this
        # source.
        socket = await self.login_and_get_socket(ws_connect)
        stream_item = self.streaming_entry('LEVELONE_EQUITIES', 'SUBS', [{
            "key": "NVDA", "4": 100, "5": 600, "8": 1349842, "9": 0,
            "52": 0.004634, "53": 13466518, "54": 1349842.996459,
            "55": 100, "56": 600}])
        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')),
            json.dumps(stream_item)]

        handler = Mock()
        self.client.add_level_one_equity_handler(handler)
        await self.client.level_one_equity_subs(['NVDA'])
        await self.client.handle_message()

        handler.assert_called_once()
        self.assertEqual(handler.call_args[0][0]['content'], [{
            'key': 'NVDA',
            'BID_SIZE': 100,
            'ASK_SIZE': 600,
            'TOTAL_VOLUME': 1349842,
            'LAST_SIZE': 0,
            'LAST_SIZE_FRACTIONAL': 0.004634,
            'UNKNOWN_53': 13466518,
            'TOTAL_VOLUME_FRACTIONAL': 1349842.996459,
            'BID_SIZE_FRACTIONAL': 100,
            'ASK_SIZE_FRACTIONAL': 600,
        }])

    ##########################################################################
    # LEVELONE_OPTIONS

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_option_subs_and_add_success_all_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_OPTIONS', 'SUBS'))]

        await self.client.level_one_option_subs(
            ['GOOG  240517C00070000', 'MSFT  240517C00160000'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_OPTIONS',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'GOOG  240517C00070000,MSFT  240517C00160000',
                'fields': ('0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,' +
                           '20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,' +
                           '36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,' +
                           '52,53,54,55')
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_OPTIONS', 'ADD'))]

        await self.client.level_one_option_add(['ADBE  240614C00500000'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_OPTIONS',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'ADBE  240614C00500000',
                'fields': ('0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,' +
                           '20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,' +
                           '36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,' +
                           '52,53,54,55')
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_option_unsubs_success_all_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('LEVELONE_OPTIONS', 'SUBS')

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'LEVELONE_OPTIONS', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'LEVELONE_OPTIONS', 'UNSUBS'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_level_one_option_handler(handler)
        self.client.add_level_one_option_handler(async_handler)

        await self.client.level_one_option_subs(
            ['GOOG  240517C00070000', 'MSFT  240517C00160000'])
        await self.client.handle_message()
        await self.client.level_one_option_unsubs(
            ['GOOG  240517C00070000', 'MSFT  240517C00160000'])

        self.assert_handler_called_once_with(
                handler, {'service': 'LEVELONE_OPTIONS',
                          'command': 'SUBS',
                          'timestamp': REQUEST_TIMESTAMP})
        self.assert_handler_called_once_with(
                async_handler, {'service': 'LEVELONE_OPTIONS',
                                'command': 'SUBS',
                                'timestamp': REQUEST_TIMESTAMP})

        send_awaited = [
            call(StringMatchesJson({
                "requests": [{
                    "service": "LEVELONE_OPTIONS",
                    "requestid": "1",
                    "command": "SUBS", 
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "GOOG  240517C00070000,MSFT  240517C00160000",
                        "fields": "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,"+
                        "17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,"+
                        "34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,"+
                        "51,52,53,54,55"
                    }
                }]
            })),
            call(StringMatchesJson({
                "requests": [{
                    "service": "LEVELONE_OPTIONS",
                    "requestid": "2",
                    "command": "UNSUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "GOOG  240517C00070000,MSFT  240517C00160000"
                    }
                }]
            })),
        ]

        socket.send.assert_has_awaits(send_awaited, any_order=False)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_option_subs_and_add_success_some_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_OPTIONS', 'SUBS'))]

        await self.client.level_one_option_subs(
            ['GOOG  240517C00070000', 'MSFT  240517C00160000'], fields=[
                StreamClient.LevelOneOptionFields.SYMBOL,
                StreamClient.LevelOneOptionFields.BID_PRICE,
                StreamClient.LevelOneOptionFields.ASK_PRICE,
                StreamClient.LevelOneOptionFields.VOLATILITY,
            ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_OPTIONS',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'GOOG  240517C00070000,MSFT  240517C00160000',
                'fields': '0,2,3,10'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_OPTIONS', 'ADD'))]

        await self.client.level_one_option_add(
            ['ADBE  240614C00500000'], fields=[
                StreamClient.LevelOneOptionFields.SYMBOL,
                StreamClient.LevelOneOptionFields.BID_PRICE,
                StreamClient.LevelOneOptionFields.ASK_PRICE,
                StreamClient.LevelOneOptionFields.VOLATILITY,
            ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_OPTIONS',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'ADBE  240614C00500000',
                'fields': '0,2,3,10'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_option_subs_and_add_success_some_fields_no_symbol(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_OPTIONS', 'SUBS'))]

        await self.client.level_one_option_subs(
            ['GOOG  240517C00070000', 'MSFT  240517C00160000'], fields=[
                StreamClient.LevelOneOptionFields.BID_PRICE,
                StreamClient.LevelOneOptionFields.ASK_PRICE,
                StreamClient.LevelOneOptionFields.VOLATILITY,
            ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_OPTIONS',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'GOOG  240517C00070000,MSFT  240517C00160000',
                'fields': '0,2,3,10'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_OPTIONS', 'ADD'))]

        await self.client.level_one_option_add(
            ['ADBE  240614C00500000'], fields=[
                StreamClient.LevelOneOptionFields.BID_PRICE,
                StreamClient.LevelOneOptionFields.ASK_PRICE,
                StreamClient.LevelOneOptionFields.VOLATILITY,
            ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_OPTIONS',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'ADBE  240614C00500000',
                'fields': '0,2,3,10'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_option_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_OPTIONS', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_option_subs(
                ['GOOG  240517C00070000', 'MSFT  240517C00160000'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_option_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_OPTIONS', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_option_unsubs(
                ['GOOG  240517C00070000', 'MSFT  240517C00160000'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_option_add_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_OPTIONS', 'ADD')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_option_add(['ADBE  240614C00500000'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_option_handler(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {
            'data': [{
                "service": "LEVELONE_OPTIONS",
                "timestamp": 1715787814441,
                "command": "SUBS",
                "content": [
                    {
                        "key": "GOOG  240517C00070000",
                        "delayed": True,
                        "assetMainType": "OPTION",
                        "cusip": "",
                        "1": "GOOG 05/17/2024 70.00 C",
                        "2": 102.15,
                        "3": 103.75,
                        "4": 103.07,
                        "5": 0,
                        "6": 0,
                        "7": 101.9504,
                        "8": 0,
                        "9": 5,
                        "10": 354.99208367,
                        "11": 102.9299,
                        "12": 2024,
                        "13": 100,
                        "14": 2,
                        "15": 0,
                        "16": 20,
                        "17": 20,
                        "18": 1,
                        "19": 1.1196,
                        "20": 70,
                        "21": "C",
                        "22": "GOOG",
                        "23": 5,
                        "24": "100 GOOG",
                        "25": 0.14009969,
                        "26": 17,
                        "27": 2,
                        "28": 0.99961827,
                        "29": 2.799e-05,
                        "30": -0.01339331,
                        "31": 0.00018508,
                        "32": 0.00434917,
                        "33": "Normal",
                        "34": 103.02414938,
                        "35": 172.9299,
                        "36": "S",
                        "37": 102.95,
                        "38": 1715786878997,
                        "39": 1714140219464,
                        "40": "O",
                        "41": "OPR",
                        "42": 1715990400000,
                        "43": "P",
                        "44": 1.09818108,
                        "45": 0.9996,
                        "46": 0.98047678,
                        "47": 0,
                        "48": True,
                        "49": "GOOG",
                        "50": 103.07,
                        "51": 68.45,
                        "52": 0,
                        "53": 0,
                        "54": 0,
                        "55": "A"
                    },
                    {
                        "key": "MSFT  240517C00160000",
                        "delayed": True,
                        "assetMainType": "OPTION",
                        "cusip": "",
                        "1": "MSFT 05/17/2024 160.00 C",
                        "2": 259.9,
                        "3": 262.1,
                        "4": 255.53,
                        "5": 0,
                        "6": 0,
                        "7": 256.56,
                        "8": 0,
                        "9": 2,
                        "10": 355.24400385,
                        "11": 261.38,
                        "12": 2024,
                        "13": 100,
                        "14": 2,
                        "15": 0,
                        "16": 52,
                        "17": 51,
                        "18": 1,
                        "19": -1.03,
                        "20": 160,
                        "21": "C",
                        "22": "MSFT",
                        "23": 5,
                        "24": "100 MSFT",
                        "25": -5.85000122,
                        "26": 17,
                        "27": 2,
                        "28": 1,
                        "29": 0,
                        "30": 0,
                        "31": 0,
                        "32": 0,
                        "33": "Normal",
                        "34": 261.39,
                        "35": 421.38,
                        "36": "S",
                        "37": 261,
                        "38": 1715786912341,
                        "39": 1715696927227,
                        "40": "O",
                        "41": "OPR",
                        "42": 1715990400000,
                        "43": "P",
                        "44": -0.40146554,
                        "45": 4.44,
                        "46": 1.73058934,
                        "47": 0,
                        "48": True,
                        "49": "MSFT",
                        "50": 271.45,
                        "51": 199.23,
                        "52": 0,
                        "53": 0,
                        "54": 0,
                        "55": "A"
                    }
                ]
            }]
        }

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'LEVELONE_OPTIONS', 'SUBS')),
            json.dumps(stream_item)]
        await self.client.level_one_option_subs(
            ['GOOG  240517C00070000', 'MSFT  240517C00160000'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_level_one_option_handler(handler)
        self.client.add_level_one_option_handler(async_handler)
        await self.client.handle_message()

        expected_item = {
            "service": "LEVELONE_OPTIONS",
            "timestamp": 1715787814441,
            "command": "SUBS",
            "content": [
                {
                    "key": "GOOG  240517C00070000",
                    "delayed": True,
                    "assetMainType": "OPTION",
                    "cusip": "",
                    "DESCRIPTION": "GOOG 05/17/2024 70.00 C",
                    "BID_PRICE": 102.15,
                    "ASK_PRICE": 103.75,
                    "LAST_PRICE": 103.07,
                    "HIGH_PRICE": 0,
                    "LOW_PRICE": 0,
                    "CLOSE_PRICE": 101.9504,
                    "TOTAL_VOLUME": 0,
                    "OPEN_INTEREST": 5,
                    "VOLATILITY": 354.99208367,
                    "MONEY_INTRINSIC_VALUE": 102.9299,
                    "EXPIRATION_YEAR": 2024,
                    "MULTIPLIER": 100,
                    "DIGITS": 2,
                    "OPEN_PRICE": 0,
                    "BID_SIZE": 20,
                    "ASK_SIZE": 20,
                    "LAST_SIZE": 1,
                    "NET_CHANGE": 1.1196,
                    "STRIKE_PRICE": 70,
                    "CONTRACT_TYPE": "C",
                    "UNDERLYING": "GOOG",
                    "EXPIRATION_MONTH": 5,
                    "DELIVERABLES": "100 GOOG",
                    "TIME_VALUE": 0.14009969,
                    "EXPIRATION_DAY": 17,
                    "DAYS_TO_EXPIRATION": 2,
                    "DELTA": 0.99961827,
                    "GAMMA": 2.799e-05,
                    "THETA": -0.01339331,
                    "VEGA": 0.00018508,
                    "RHO": 0.00434917,
                    "SECURITY_STATUS": "Normal",
                    "THEORETICAL_OPTION_VALUE": 103.02414938,
                    "UNDERLYING_PRICE": 172.9299,
                    "UV_EXPIRATION_TYPE": "S",
                    "MARK": 102.95,
                    "QUOTE_TIME_MILLIS": 1715786878997,
                    "TRADE_TIME_MILLIS": 1714140219464,
                    "EXCHANGE_ID": "O",
                    "EXCHANGE_NAME": "OPR",
                    "LAST_TRADING_DAY": 1715990400000,
                    "SETTLEMENT_TYPE": "P",
                    "NET_PERCENT_CHANGE": 1.09818108,
                    "MARK_CHANGE": 0.9996,
                    "MARK_CHANGE_PERCENT": 0.98047678,
                    "IMPLIED_YIELD": 0,
                    "IS_PENNY": True,
                    "OPTION_ROOT": "GOOG",
                    "HIGH_PRICE_52_WEEK": 103.07,
                    "LOW_PRICE_52_WEEK": 68.45,
                    "INDICATIVE_ASKING_PRICE": 0,
                    "INDICATIVE_BID_PRICE": 0,
                    "INDICATIVE_QUOTE_TIME": 0,
                    "EXERCISE_TYPE": "A"
                },
                {
                    "key": "MSFT  240517C00160000",
                    "delayed": True,
                    "assetMainType": "OPTION",
                    "cusip": "",
                    "DESCRIPTION": "MSFT 05/17/2024 160.00 C",
                    "BID_PRICE": 259.9,
                    "ASK_PRICE": 262.1,
                    "LAST_PRICE": 255.53,
                    "HIGH_PRICE": 0,
                    "LOW_PRICE": 0,
                    "CLOSE_PRICE": 256.56,
                    "TOTAL_VOLUME": 0,
                    "OPEN_INTEREST": 2,
                    "VOLATILITY": 355.24400385,
                    "MONEY_INTRINSIC_VALUE": 261.38,
                    "EXPIRATION_YEAR": 2024,
                    "MULTIPLIER": 100,
                    "DIGITS": 2,
                    "OPEN_PRICE": 0,
                    "BID_SIZE": 52,
                    "ASK_SIZE": 51,
                    "LAST_SIZE": 1,
                    "NET_CHANGE": -1.03,
                    "STRIKE_PRICE": 160,
                    "CONTRACT_TYPE": "C",
                    "UNDERLYING": "MSFT",
                    "EXPIRATION_MONTH": 5,
                    "DELIVERABLES": "100 MSFT",
                    "TIME_VALUE": -5.85000122,
                    "EXPIRATION_DAY": 17,
                    "DAYS_TO_EXPIRATION": 2,
                    "DELTA": 1,
                    "GAMMA": 0,
                    "THETA": 0,
                    "VEGA": 0,
                    "RHO": 0,
                    "SECURITY_STATUS": "Normal",
                    "THEORETICAL_OPTION_VALUE": 261.39,
                    "UNDERLYING_PRICE": 421.38,
                    "UV_EXPIRATION_TYPE": "S",
                    "MARK": 261,
                    "QUOTE_TIME_MILLIS": 1715786912341,
                    "TRADE_TIME_MILLIS": 1715696927227,
                    "EXCHANGE_ID": "O",
                    "EXCHANGE_NAME": "OPR",
                    "LAST_TRADING_DAY": 1715990400000,
                    "SETTLEMENT_TYPE": "P",
                    "NET_PERCENT_CHANGE": -0.40146554,
                    "MARK_CHANGE": 4.44,
                    "MARK_CHANGE_PERCENT": 1.73058934,
                    "IMPLIED_YIELD": 0,
                    "IS_PENNY": True,
                    "OPTION_ROOT": "MSFT",
                    "HIGH_PRICE_52_WEEK": 271.45,
                    "LOW_PRICE_52_WEEK": 199.23,
                    "INDICATIVE_ASKING_PRICE": 0,
                    "INDICATIVE_BID_PRICE": 0,
                    "INDICATIVE_QUOTE_TIME": 0,
                    "EXERCISE_TYPE": "A"
                }
            ]
        }
        self.assert_handler_called_once_with(handler, expected_item)
        self.assert_handler_called_once_with(async_handler, expected_item)


    ##########################################################################
    # LEVELONE_FUTURES

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_subs_and_add_success_all_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_FUTURES', 'SUBS'))]

        await self.client.level_one_futures_subs(['/ES', '/CL'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FUTURES',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': '/ES,/CL',
                'fields': ('0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,' +
                           '20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40')
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_FUTURES', 'ADD'))]

        await self.client.level_one_futures_add(['/NQ'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FUTURES',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': '/NQ',
                'fields': '0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,' +
                '17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,' +
                '34,35,36,37,38,39,40'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_unsubs_success_all_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('LEVELONE_FUTURES', 'SUBS')

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'LEVELONE_FUTURES', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'LEVELONE_FUTURES', 'UNSUBS'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_level_one_futures_handler(handler)
        self.client.add_level_one_futures_handler(async_handler)

        await self.client.level_one_futures_subs(['/ES', '/CL'])
        await self.client.handle_message()
        await self.client.level_one_futures_unsubs(['/ES', '/CL'])

        self.assert_handler_called_once_with(handler, {'service': 'LEVELONE_FUTURES', 'command': 'SUBS',
                                                       'timestamp': REQUEST_TIMESTAMP})
        self.assert_handler_called_once_with(async_handler, {'service': 'LEVELONE_FUTURES', 'command': 'SUBS',
                                                             'timestamp': REQUEST_TIMESTAMP})
        send_awaited = [
            call(StringMatchesJson({
                "requests": [{
                    "service": "LEVELONE_FUTURES",
                    "requestid": "1",
                    "command": "SUBS", 
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "/ES,/CL", 
                        "fields": "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,"+
                        "17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,"+
                        "34,35,36,37,38,39,40"
                    }
                }]
            })),
            call(StringMatchesJson({
                "requests": [{
                    "service": "LEVELONE_FUTURES",
                    "requestid": "2",
                    "command": "UNSUBS", 
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "/ES,/CL"
                    }
                }]
            }))
        ]
        socket.send.assert_has_awaits(send_awaited, any_order=False)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_subs_and_add_success_some_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_FUTURES', 'SUBS'))]

        await self.client.level_one_futures_subs(['/ES', '/CL'], fields=[
            StreamClient.LevelOneFuturesFields.SYMBOL,
            StreamClient.LevelOneFuturesFields.BID_PRICE,
            StreamClient.LevelOneFuturesFields.ASK_PRICE,
            StreamClient.LevelOneFuturesFields.FUTURE_PRICE_FORMAT,
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FUTURES',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': '/ES,/CL',
                'fields': '0,1,2,28'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_FUTURES', 'ADD'))]

        await self.client.level_one_futures_add(['/NQ'], fields=[
            StreamClient.LevelOneFuturesFields.SYMBOL,
            StreamClient.LevelOneFuturesFields.BID_PRICE,
            StreamClient.LevelOneFuturesFields.ASK_PRICE,
            StreamClient.LevelOneFuturesFields.FUTURE_PRICE_FORMAT,
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FUTURES',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': '/NQ',
                'fields': '0,1,2,28'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_subs_and_add_success_some_fields_no_symbol(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_FUTURES', 'SUBS'))]

        await self.client.level_one_futures_subs(['/ES', '/CL'], fields=[
            StreamClient.LevelOneFuturesFields.BID_PRICE,
            StreamClient.LevelOneFuturesFields.ASK_PRICE,
            StreamClient.LevelOneFuturesFields.FUTURE_PRICE_FORMAT,
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FUTURES',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': '/ES,/CL',
                'fields': '0,1,2,28'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_FUTURES', 'ADD'))]

        await self.client.level_one_futures_add(['/NQ'], fields=[
            StreamClient.LevelOneFuturesFields.BID_PRICE,
            StreamClient.LevelOneFuturesFields.ASK_PRICE,
            StreamClient.LevelOneFuturesFields.FUTURE_PRICE_FORMAT,
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FUTURES',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': '/NQ',
                'fields': '0,1,2,28'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_FUTURES', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_futures_subs(['/ES', '/CL'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_FUTURES', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_futures_unsubs(['/ES', '/CL'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_add_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_FUTURES', 'ADD')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_futures_add(['/NQ'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_handler(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {
            'data': [{
                'service': 'LEVELONE_FUTURES',
                'timestamp': 1719002950099,
                'command': 'SUBS',
                'content': [{
                    'key': '/CL',
                    'delayed': False,
                    'assetMainType': 'FUTURE',
                    '1': 80.61,
                    '2': 80.62,
                    '3': 80.6,
                    '4': 2,
                    '5': 14,
                    '6': '?',
                    '7': '?',
                    '8': 270916,
                    '9': 1,
                    '10': 1719002948817,
                    '11': 1719002944037,
                    '12': 81.79,
                    '13': 80.35,
                    '14': 81.29,
                    '15': '@',
                    '16': 'Light Sweet Crude Oil Futures,Aug-2024,ETH',
                    '17': '?',
                    '18': 81.27,
                    '19': -0.69,
                    '20': -0.84881289,
                    '21': 'XNYM',
                    '22': 'Normal',
                    '23': 352314,
                    '24': 80.61,
                    '25': 0.01,
                    '26': 10,
                    '27': '/CL',
                    '28': 'D,D',
                    '29': 'GLBX(de=1640;0=-17001600;1=-17001600d-15551640;7=d-16401555)',
                    '30': False,
                    '31': 1000,
                    '32': True,
                    '33': 81.29,
                    '34': '/CLQ24',
                    '35': 1721620800000,
                    '36': 'Unknown',
                    '37': 1719002948347,
                    '38': 1719002948817,
                    '39': True,
                    '40': 1718928000000
                }, {
                    'key': '/ES',
                    'delayed': False,
                    'assetMainType': 'FUTURE',
                    '1': 5536.5,
                    '2': 5537,
                    '3': 5536.75,
                    '4': 61,
                    '5': 112,
                    '6': '?',
                    '7': '?',
                    '8': 1250270,
                    '9': 2,
                    '10': 1719002947349,
                    '11': 1719002945728,
                    '12': 5550.75,
                    '13': 5519,
                    '14': 5544.5,
                    '15': '@',
                    '16': 'E-mini S&P 500 Index Futures,Sep-2024,ETH',
                    '17': '?',
                    '18': 5545,
                    '19': -7.75,
                    '20': -0.13977816,
                    '21': 'XCME',
                    '22': 'Normal',
                    '23': 1976987,
                    '24': 5536.75,
                    '25': 0.25,
                    '26': 12.5,
                    '27': '/ES',
                    '28': 'D,D',
                    '29': 'GLBX(de=1640;0=-17001600;1=r-17001600d-15551640;7=d-16401555)',
                    '30': False,
                    '31': 50,
                    '32': True,
                    '33': 5544.5,
                    '34': '/ESU24',
                    '35': 1726804800000,
                    '36': 'Unknown',
                    '37': 1719002947349,
                    '38': 1719002946189,
                    '39': True,
                    '40': 1718928000000
                }]
            }]
        }

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'LEVELONE_FUTURES', 'SUBS')),
            json.dumps(stream_item)]
        await self.client.level_one_futures_subs(['/ES', '/CL'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_level_one_futures_handler(handler)
        self.client.add_level_one_futures_handler(async_handler)
        await self.client.handle_message()

        expected_item = {
            'service': 'LEVELONE_FUTURES',
            'timestamp': 1719002950099,
            'command': 'SUBS',
            'content': [
                {
                    'key': '/CL',
                    'delayed': False,
                    'assetMainType': 'FUTURE',
                    'BID_PRICE': 80.61,
                    'ASK_PRICE': 80.62,
                    'LAST_PRICE': 80.6,
                    'BID_SIZE': 2,
                    'ASK_SIZE': 14,
                    'BID_ID': '?',
                    'ASK_ID': '?',
                    'TOTAL_VOLUME': 270916,
                    'LAST_SIZE': 1,
                    'QUOTE_TIME_MILLIS': 1719002948817,
                    'TRADE_TIME_MILLIS': 1719002944037,
                    'HIGH_PRICE': 81.79,
                    'LOW_PRICE': 80.35,
                    'CLOSE_PRICE': 81.29,
                    'EXCHANGE_ID': '@',
                    'DESCRIPTION': 'Light Sweet Crude Oil Futures,Aug-2024,ETH',
                    'LAST_ID': '?',
                    'OPEN_PRICE': 81.27,
                    'NET_CHANGE': -0.69,
                    'FUTURE_CHANGE_PERCENT': -0.84881289,
                    'EXCHANGE_NAME': 'XNYM',
                    'SECURITY_STATUS': 'Normal',
                    'OPEN_INTEREST': 352314,
                    'MARK': 80.61,
                    'TICK': 0.01,
                    'TICK_AMOUNT': 10,
                    'PRODUCT': '/CL',
                    'FUTURE_PRICE_FORMAT': 'D,D',
                    'FUTURE_TRADING_HOURS': 'GLBX(de=1640;0=-17001600;1=-17001600d-15551640;7=d-16401555)',
                    'FUTURE_IS_TRADABLE': False,
                    'FUTURE_MULTIPLIER': 1000,
                    'FUTURE_IS_ACTIVE': True,
                    'FUTURE_SETTLEMENT_PRICE': 81.29,
                    'FUTURE_ACTIVE_SYMBOL': '/CLQ24',
                    'FUTURE_EXPIRATION_DATE': 1721620800000,
                    'EXPIRATION_STYLE': 'Unknown',
                    'ASK_TIME_MILLIS': 1719002948347,
                    'BID_TIME_MILLIS': 1719002948817,
                    'QUOTED_IN_SESSION': True,
                    'SETTLEMENT_DATE': 1718928000000
                },
                {
                    'key': '/ES',
                    'delayed': False,
                    'assetMainType': 'FUTURE',
                    'BID_PRICE': 5536.5,
                    'ASK_PRICE': 5537,
                    'LAST_PRICE': 5536.75,
                    'BID_SIZE': 61,
                    'ASK_SIZE': 112,
                    'BID_ID': '?',
                    'ASK_ID': '?',
                    'TOTAL_VOLUME': 1250270,
                    'LAST_SIZE': 2,
                    'QUOTE_TIME_MILLIS': 1719002947349,
                    'TRADE_TIME_MILLIS': 1719002945728,
                    'HIGH_PRICE': 5550.75,
                    'LOW_PRICE': 5519,
                    'CLOSE_PRICE': 5544.5,
                    'EXCHANGE_ID': '@',
                    'DESCRIPTION': 'E-mini S&P 500 Index Futures,Sep-2024,ETH',
                    'LAST_ID': '?',
                    'OPEN_PRICE': 5545,
                    'NET_CHANGE': -7.75,
                    'FUTURE_CHANGE_PERCENT': -0.13977816,
                    'EXCHANGE_NAME': 'XCME',
                    'SECURITY_STATUS': 'Normal',
                    'OPEN_INTEREST': 1976987,
                    'MARK': 5536.75,
                    'TICK': 0.25,
                    'TICK_AMOUNT': 12.5,
                    'PRODUCT': '/ES',
                    'FUTURE_PRICE_FORMAT': 'D,D',
                    'FUTURE_TRADING_HOURS': 'GLBX(de=1640;0=-17001600;1=r-17001600d-15551640;7=d-16401555)',
                    'FUTURE_IS_TRADABLE': False,
                    'FUTURE_MULTIPLIER': 50,
                    'FUTURE_IS_ACTIVE': True,
                    'FUTURE_SETTLEMENT_PRICE': 5544.5,
                    'FUTURE_ACTIVE_SYMBOL': '/ESU24',
                    'FUTURE_EXPIRATION_DATE': 1726804800000,
                    'EXPIRATION_STYLE': 'Unknown',
                    'ASK_TIME_MILLIS': 1719002947349,
                    'BID_TIME_MILLIS': 1719002946189,
                    'QUOTED_IN_SESSION': True,
                    'SETTLEMENT_DATE': 1718928000000
                }
            ]
        }

        self.assert_handler_called_once_with(handler, expected_item)
        self.assert_handler_called_once_with(async_handler, expected_item)


    ##########################################################################
    # LEVELONE_FOREX

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_forex_subs_and_add_success_all_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_FOREX', 'SUBS'))]

        await self.client.level_one_forex_subs(['EUR/USD', 'EUR/GBP'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FOREX',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'EUR/USD,EUR/GBP',
                'fields': ('0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,' +
                           '20,21,22,23,24,25,26,27,28,29')
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_FOREX', 'ADD'))]

        await self.client.level_one_forex_add(['JPY/USD'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FOREX',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'JPY/USD',
                'fields': ('0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,' +
                           '20,21,22,23,24,25,26,27,28,29')
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_forex_unsubs_success_all_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('LEVELONE_FOREX', 'SUBS')

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'LEVELONE_FOREX', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'LEVELONE_FOREX', 'UNSUBS'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_level_one_forex_handler(handler)
        self.client.add_level_one_forex_handler(async_handler)

        await self.client.level_one_forex_subs(['EUR/USD', 'EUR/GBP'])
        await self.client.handle_message()
        await self.client.level_one_forex_unsubs(['EUR/USD', 'EUR/GBP'])

        self.assert_handler_called_once_with(handler, {'service': 'LEVELONE_FOREX', 'command': 'SUBS',
                                                       'timestamp': REQUEST_TIMESTAMP})
        self.assert_handler_called_once_with(async_handler, {'service': 'LEVELONE_FOREX', 'command': 'SUBS',
                                                             'timestamp': REQUEST_TIMESTAMP})
        send_awaited = [
            call(StringMatchesJson({
                "requests": [{
                    "service": "LEVELONE_FOREX",
                    "requestid": "1",
                    "command": "SUBS", 
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "EUR/USD,EUR/GBP",
                        "fields": "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,"+
                        "17,18,19,20,21,22,23,24,25,26,27,28,29"
                    }
                }]
            })),
            call(StringMatchesJson({
                "requests": [{
                    "service": "LEVELONE_FOREX",
                    "requestid": "2",
                    "command": "UNSUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "EUR/USD,EUR/GBP"
                    }
                }]
            }))
        ]
        socket.send.assert_has_awaits(send_awaited, any_order=False)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_forex_subs_and_add_success_some_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_FOREX', 'SUBS'))]

        await self.client.level_one_forex_subs(['EUR/USD', 'EUR/GBP'], fields=[
            StreamClient.LevelOneForexFields.SYMBOL,
            StreamClient.LevelOneForexFields.HIGH_PRICE,
            StreamClient.LevelOneForexFields.LOW_PRICE,
            StreamClient.LevelOneForexFields.MARKET_MAKER,
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FOREX',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'EUR/USD,EUR/GBP',
                'fields': '0,10,11,26'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_FOREX', 'ADD'))]

        await self.client.level_one_forex_add(['JPY/USD'], fields=[
            StreamClient.LevelOneForexFields.SYMBOL,
            StreamClient.LevelOneForexFields.HIGH_PRICE,
            StreamClient.LevelOneForexFields.LOW_PRICE,
            StreamClient.LevelOneForexFields.MARKET_MAKER,
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FOREX',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'JPY/USD',
                'fields': '0,10,11,26'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_forex_subs_and_add_success_some_fields_no_symbol(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_FOREX', 'SUBS'))]

        await self.client.level_one_forex_subs(['EUR/USD', 'EUR/GBP'], fields=[
            StreamClient.LevelOneForexFields.HIGH_PRICE,
            StreamClient.LevelOneForexFields.LOW_PRICE,
            StreamClient.LevelOneForexFields.MARKET_MAKER,
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FOREX',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'EUR/USD,EUR/GBP',
                'fields': '0,10,11,26'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_FOREX', 'ADD'))]

        await self.client.level_one_forex_add(['JPY/USD'], fields=[
            StreamClient.LevelOneForexFields.HIGH_PRICE,
            StreamClient.LevelOneForexFields.LOW_PRICE,
            StreamClient.LevelOneForexFields.MARKET_MAKER,
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FOREX',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'JPY/USD',
                'fields': '0,10,11,26'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_forex_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_FOREX', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_forex_subs(['EUR/USD', 'EUR/GBP'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_forex_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_FOREX', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_forex_unsubs(['EUR/USD', 'EUR/GBP'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_forex_add_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_FOREX', 'ADD')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_forex_add(['JPY/USD'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_forex_handler(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {
            'data': [{
                'service': 'LEVELONE_FOREX',
                'timestamp': 1590599267920,
                'command': 'SUBS',
                'content': [{
                    'key': 'EUR/GBP',
                    'delayed': False,
                    'assetMainType': 'FOREX',
                    '1': 0.84437,
                    '2': 0.84451,
                    '3': 0.84444,
                    '4': 1000000,
                    '5': 3000000,
                    '6': 310000,
                    '7': 10000,
                    '8': 1718811579076,
                    '9': 1718811579076,
                    '10': 0.84526,
                    '11': 0.842995,
                    '12': 0.844095,
                    '13': '!',
                    '14': 'Euro/GBPound Spot',
                    '15': 0.84526,
                    '16': 0.000345,
                    '17': 0,
                    '18': 'GFT',
                    '19': 2,
                    '20': 'Unknown',
                    '21': 0,
                    '22': 0,
                    '23': '',
                    '24': '',
                    '25': False,
                    '26': '',
                    '27': 0.84526,
                    '28': 0.842995,
                    '29': 0.84444
                }, {
                    'key': 'EUR/USD',
                    'delayed': False,
                    'assetMainType': 'FOREX',
                    '1': 1.07435,
                    '2': 1.07448,
                    '3': 1.074415,
                    '4': 3000000,
                    '5': 1000000,
                    '6': 14730000,
                    '7': 20000,
                    '8': 1718811581092,
                    '9': 1718811581092,
                    '10': 1.075305,
                    '11': 1.07272,
                    '12': 1.070485,
                    '13': '!',
                    '14': 'Euro/USDollar Spot',
                    '15': 1.07396,
                    '16': 0.00393,
                    '17': 0,
                    '18': 'GFT',
                    '19': 2,
                    '20': 'Unknown',
                    '21': 0,
                    '22': 0,
                    '23': '',
                    '24': '',
                    '25': False,
                    '26': '',
                    '27': 1.075305,
                    '28': 1.07272,
                    '29': 1.074415
                }]
            }]
        }

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'LEVELONE_FOREX', 'SUBS')),
            json.dumps(stream_item)]
        await self.client.level_one_forex_subs(['EUR/USD', 'EUR/GBP'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_level_one_forex_handler(handler)
        self.client.add_level_one_forex_handler(async_handler)
        await self.client.handle_message()

        expected_item = {
            'service': 'LEVELONE_FOREX',
            'timestamp': 1590599267920,
            'command': 'SUBS',
            'content': [{
                'key': 'EUR/GBP',
                'delayed': False,
                'assetMainType': 'FOREX',
                'BID_PRICE': 0.84437,
                'ASK_PRICE': 0.84451,
                'LAST_PRICE': 0.84444,
                'BID_SIZE': 1000000,
                'ASK_SIZE': 3000000,
                'TOTAL_VOLUME': 310000,
                'LAST_SIZE': 10000,
                'QUOTE_TIME_MILLIS': 1718811579076,
                'TRADE_TIME_MILLIS': 1718811579076,
                'HIGH_PRICE': 0.84526,
                'LOW_PRICE': 0.842995,
                'CLOSE_PRICE': 0.844095,
                'EXCHANGE_ID': '!',
                'DESCRIPTION': 'Euro/GBPound Spot',
                'OPEN_PRICE': 0.84526,
                'NET_CHANGE': 0.000345,
                'CHANGE_PERCENT': 0,
                'EXCHANGE_NAME': 'GFT',
                'DIGITS': 2,
                'SECURITY_STATUS': 'Unknown',
                'TICK': 0,
                'TICK_AMOUNT': 0,
                'PRODUCT': '',
                'TRADING_HOURS': '',
                'IS_TRADABLE': False,
                'MARKET_MAKER': '',
                'HIGH_PRICE_52_WEEK': 0.84526,
                'LOW_PRICE_52_WEEK': 0.842995,
                'MARK': 0.84444
            }, {
                'key': 'EUR/USD',
                'delayed': False,
                'assetMainType': 'FOREX',
                'BID_PRICE': 1.07435,
                'ASK_PRICE': 1.07448,
                'LAST_PRICE': 1.074415,
                'BID_SIZE': 3000000,
                'ASK_SIZE': 1000000,
                'TOTAL_VOLUME': 14730000,
                'LAST_SIZE': 20000,
                'QUOTE_TIME_MILLIS': 1718811581092,
                'TRADE_TIME_MILLIS': 1718811581092,
                'HIGH_PRICE': 1.075305,
                'LOW_PRICE': 1.07272,
                'CLOSE_PRICE': 1.070485,
                'EXCHANGE_ID': '!',
                'DESCRIPTION': 'Euro/USDollar Spot',
                'OPEN_PRICE': 1.07396,
                'NET_CHANGE': 0.00393,
                'CHANGE_PERCENT': 0,
                'EXCHANGE_NAME': 'GFT',
                'DIGITS': 2,
                'SECURITY_STATUS': 'Unknown',
                'TICK': 0,
                'TICK_AMOUNT': 0,
                'PRODUCT': '',
                'TRADING_HOURS': '',
                'IS_TRADABLE': False,
                'MARKET_MAKER': '',
                'HIGH_PRICE_52_WEEK': 1.075305,
                'LOW_PRICE_52_WEEK': 1.07272,
                'MARK': 1.074415
            }]
        }

        self.assert_handler_called_once_with(handler, expected_item)
        self.assert_handler_called_once_with(async_handler, expected_item)


    ##########################################################################
    # LEVELONE_FUTURES_OPTIONS

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_options_subs_and_add_success_all_fields(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_FUTURES_OPTIONS', 'SUBS'))]

        await self.client.level_one_futures_options_subs(
            ['./E3DM24P5490', './Q3DM24C19960'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FUTURES_OPTIONS',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': './E3DM24P5490,./Q3DM24C19960',
                'fields': ('0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,' +
                           '19,20,21,22,23,24,25,26,27,28,29,30,31')
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_FUTURES_OPTIONS', 'ADD'))]

        await self.client.level_one_futures_options_add(['./OYMM24P38550'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FUTURES_OPTIONS',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': './OYMM24P38550',
                'fields': ('0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,' +
                           '19,20,21,22,23,24,25,26,27,28,29,30,31')
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_options_unsubs_success_all_fields(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('LEVELONE_FUTURES_OPTIONS', 'SUBS')

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'LEVELONE_FUTURES_OPTIONS', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'LEVELONE_FUTURES_OPTIONS', 'UNSUBS'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_level_one_futures_options_handler(handler)
        self.client.add_level_one_futures_options_handler(async_handler)

        await self.client.level_one_futures_options_subs(
            ['./E3DM24P5490', './Q3DM24C19960'])
        await self.client.handle_message()
        await self.client.level_one_futures_options_unsubs(
            ['./E3DM24P5490', './Q3DM24C19960'])

        self.assert_handler_called_once_with(
                handler, {'service': 'LEVELONE_FUTURES_OPTIONS',
                          'command': 'SUBS',
                          'timestamp': REQUEST_TIMESTAMP})
        self.assert_handler_called_once_with(
                async_handler, {'service': 'LEVELONE_FUTURES_OPTIONS',
                                'command': 'SUBS',
                                'timestamp': REQUEST_TIMESTAMP})
        send_awaited = [
            call(StringMatchesJson({
                "requests": [{
                    "service": "LEVELONE_FUTURES_OPTIONS",
                    "requestid": "1",
                    "command": "SUBS", 
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "./E3DM24P5490,./Q3DM24C19960",
                        "fields": "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,"+
                        "17,18,19,20,21,22,23,24,25,26,27,28,29,30,31"
                    }
                }]
            })),
            call(StringMatchesJson({
                "requests": [{
                    "service": "LEVELONE_FUTURES_OPTIONS",
                    "requestid": "2",
                    "command": "UNSUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "./E3DM24P5490,./Q3DM24C19960"
                    }
                }]
            })),
        ]
        socket.send.assert_has_awaits(send_awaited, any_order=False)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_options_subs_and_add_success_some_fields(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_FUTURES_OPTIONS', 'SUBS'))]

        await self.client.level_one_futures_options_subs(
            ['./E3DM24P5490', './Q3DM24C19960'], fields=[
                StreamClient.LevelOneFuturesOptionsFields.SYMBOL,
                StreamClient.LevelOneFuturesOptionsFields.BID_SIZE,
                StreamClient.LevelOneFuturesOptionsFields.ASK_SIZE,
                StreamClient.LevelOneFuturesOptionsFields.CONTRACT_TYPE,
            ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FUTURES_OPTIONS',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': './E3DM24P5490,./Q3DM24C19960',
                'fields': '0,4,5,28'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_FUTURES_OPTIONS', 'ADD'))]

        await self.client.level_one_futures_options_add(
            ['./OYMM24P38550'], fields=[
                StreamClient.LevelOneFuturesOptionsFields.SYMBOL,
                StreamClient.LevelOneFuturesOptionsFields.BID_SIZE,
                StreamClient.LevelOneFuturesOptionsFields.ASK_SIZE,
                StreamClient.LevelOneFuturesOptionsFields.CONTRACT_TYPE,
            ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FUTURES_OPTIONS',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': './OYMM24P38550',
                'fields': '0,4,5,28'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_options_subs_and_add_success_some_fields_no_symbol(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_FUTURES_OPTIONS', 'SUBS'))]

        await self.client.level_one_futures_options_subs(
            ['./E3DM24P5490', './Q3DM24C19960'], fields=[
                StreamClient.LevelOneFuturesOptionsFields.BID_SIZE,
                StreamClient.LevelOneFuturesOptionsFields.ASK_SIZE,
                StreamClient.LevelOneFuturesOptionsFields.CONTRACT_TYPE,
            ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FUTURES_OPTIONS',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': './E3DM24P5490,./Q3DM24C19960',
                'fields': '0,4,5,28'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'LEVELONE_FUTURES_OPTIONS', 'ADD'))]

        await self.client.level_one_futures_options_add(
            ['./OYMM24P38550'], fields=[
                StreamClient.LevelOneFuturesOptionsFields.BID_SIZE,
                StreamClient.LevelOneFuturesOptionsFields.ASK_SIZE,
                StreamClient.LevelOneFuturesOptionsFields.CONTRACT_TYPE,
            ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_FUTURES_OPTIONS',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': './OYMM24P38550',
                'fields': '0,4,5,28'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_options_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_FUTURES_OPTIONS', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_futures_options_subs(
                ['./E3DM24P5490', './Q3DM24C19960'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_options_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_FUTURES_OPTIONS', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_futures_options_unsubs(
                ['./E3DM24P5490', './Q3DM24C19960'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_options_add_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'LEVELONE_FUTURES_OPTIONS', 'ADD')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_futures_options_add(['./OYMM24P38550'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_level_one_futures_options_handler(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {
            'data': [{
                'service': 'LEVELONE_FUTURES_OPTIONS',
                'timestamp': 1718814961845,
                'command': 'SUBS',
                'content': [{
                    'key': './E3DM24P5490',
                    'delayed': False,
                    'assetMainType': 'FUTURE_OPTION',
                    '1': 9.6,
                    '2': 9.7,
                    '3': 9.7,
                    '4': 19,
                    '5': 19,
                    '6': 63,
                    '7': 63,
                    '8': 959,
                    '9': 22,
                    '10': 1718814960408,
                    '11': 1718814929433,
                    '12': 11,
                    '13': 7.8,
                    '14': 11,
                    '15': 63,
                    '16': 'E-mini S&P 500 Options',
                    '17': 10,
                    '18': 524,
                    '19': 9.65,
                    '20': 0.05,
                    '21': 2.5,
                    '22': 50,
                    '23': 11,
                    '24': '/ESM24',
                    '25': 5490,
                    '26': 1718856000000,
                    '27': 'Weeklys',
                    '28': 'P',
                    '29': 'Normal',
                    '30': '@',
                    '31': 'XCME'
                }, {
                    'key': './Q3DM24C19960',
                    'delayed': False,
                    'assetMainType': 'FUTURE_OPTION',
                    '1': 54.25,
                    '2': 56.5,
                    '3': 55.5,
                    '4': 15,
                    '5': 16,
                    '6': 63,
                    '7': 63,
                    '8': 15,
                    '9': 1,
                    '10': 1718814960411,
                    '11': 1718813651721,
                    '12': 61,
                    '13': 54.5,
                    '14': 41.25,
                    '15': 63,
                    '16': 'E-mini NASDAQ-100 Options',
                    '17': 55,
                    '18': 2,
                    '19': 55.75,
                    '20': 0.05,
                    '21': 1,
                    '22': 20,
                    '23': 41.25,
                    '24': '/NQM24',
                    '25': 19960,
                    '26': 1718856000000,
                    '27': 'Weeklys',
                    '28': 'C',
                    '29': 'Normal',
                    '30': '@',
                    '31': 'XCME'
                }]
            }]
        }

        socket.recv.side_effect = [
            json.dumps(self.success_response(
                1, 'LEVELONE_FUTURES_OPTIONS', 'SUBS')),
            json.dumps(stream_item)]
        await self.client.level_one_futures_options_subs(
            ['./E3DM24P5490', './Q3DM24C19960'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_level_one_futures_options_handler(handler)
        self.client.add_level_one_futures_options_handler(async_handler)
        await self.client.handle_message()

        expected_item = {
            'service': 'LEVELONE_FUTURES_OPTIONS',
            'timestamp': 1718814961845,
            'command': 'SUBS',
            'content': [{
                'key': './E3DM24P5490',
                'delayed': False,
                'assetMainType': 'FUTURE_OPTION',
                'BID_PRICE': 9.6,
                'ASK_PRICE': 9.7,
                'LAST_PRICE': 9.7,
                'BID_SIZE': 19,
                'ASK_SIZE': 19,
                'BID_ID': 63,
                'ASK_ID': 63,
                'TOTAL_VOLUME': 959,
                'LAST_SIZE': 22,
                'QUOTE_TIME_MILLIS': 1718814960408,
                'TRADE_TIME_MILLIS': 1718814929433,
                'HIGH_PRICE': 11,
                'LOW_PRICE': 7.8,
                'CLOSE_PRICE': 11,
                'LAST_ID': 63,
                'DESCRIPTION': 'E-mini S&P 500 Options',
                'OPEN_PRICE': 10,
                'OPEN_INTEREST': 524,
                'MARK': 9.65,
                'TICK': 0.05,
                'TICK_AMOUNT': 2.5,
                'FUTURE_MULTIPLIER': 50,
                'FUTURE_SETTLEMENT_PRICE': 11,
                'UNDERLYING_SYMBOL': '/ESM24',
                'STRIKE_PRICE': 5490,
                'FUTURE_EXPIRATION_DATE': 1718856000000,
                'EXPIRATION_STYLE': 'Weeklys',
                'CONTRACT_TYPE': 'P',
                'SECURITY_STATUS': 'Normal',
                'EXCHANGE_ID': '@',
                'EXCHANGE_NAME': 'XCME'
            }, {
                'key': './Q3DM24C19960',
                'delayed': False,
                'assetMainType': 'FUTURE_OPTION',
                'BID_PRICE': 54.25,
                'ASK_PRICE': 56.5,
                'LAST_PRICE': 55.5,
                'BID_SIZE': 15,
                'ASK_SIZE': 16,
                'BID_ID': 63,
                'ASK_ID': 63,
                'TOTAL_VOLUME': 15,
                'LAST_SIZE': 1,
                'QUOTE_TIME_MILLIS': 1718814960411,
                'TRADE_TIME_MILLIS': 1718813651721,
                'HIGH_PRICE': 61,
                'LOW_PRICE': 54.5,
                'CLOSE_PRICE': 41.25,
                'LAST_ID': 63,
                'DESCRIPTION': 'E-mini NASDAQ-100 Options',
                'OPEN_PRICE': 55,
                'OPEN_INTEREST': 2,
                'MARK': 55.75,
                'TICK': 0.05,
                'TICK_AMOUNT': 1,
                'FUTURE_MULTIPLIER': 20,
                'FUTURE_SETTLEMENT_PRICE': 41.25,
                'UNDERLYING_SYMBOL': '/NQM24',
                'STRIKE_PRICE': 19960,
                'FUTURE_EXPIRATION_DATE': 1718856000000,
                'EXPIRATION_STYLE': 'Weeklys',
                'CONTRACT_TYPE': 'C',
                'SECURITY_STATUS': 'Normal',
                'EXCHANGE_ID': '@',
                'EXCHANGE_NAME': 'XCME'
            }]
        }

        self.assert_handler_called_once_with(handler, expected_item)
        self.assert_handler_called_once_with(async_handler, expected_item)

    ##########################################################################
    # NYSE_BOOK

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_nyse_book_subs_success_and_add_all_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'NYSE_BOOK', 'SUBS'))]

        await self.client.nyse_book_subs(['GOOG', 'MSFT'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'NYSE_BOOK',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'GOOG,MSFT',
                'fields': '0,1,2,3'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'NYSE_BOOK', 'ADD'))]

        await self.client.nyse_book_add(['INTC'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'NYSE_BOOK',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'INTC',
                'fields': '0,1,2,3'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_nyse_book_unsubs_success_all_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {'data': [{'service': 'NYSE_BOOK', 'command': 'SUBS', 'timestamp': REQUEST_TIMESTAMP, 'content': {}}]}

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'NYSE_BOOK', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'NYSE_BOOK', 'UNSUBS'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_nyse_book_handler(handler)
        self.client.add_nyse_book_handler(async_handler)

        await self.client.nyse_book_subs(['GOOG', 'MSFT'])
        await self.client.handle_message()
        await self.client.nyse_book_unsubs(['GOOG', 'MSFT'])

        self.assert_handler_called_once_with(
                handler, {'service': 'NYSE_BOOK',
                          'command': 'SUBS',
                          'timestamp': REQUEST_TIMESTAMP,
                          'content': {}})
        self.assert_handler_called_once_with(
                async_handler, {'service': 'NYSE_BOOK',
                                'command': 'SUBS',
                                'timestamp': REQUEST_TIMESTAMP,
                                'content': {}})
        send_awaited = [
            call(StringMatchesJson({
                "requests": [{
                    "service": "NYSE_BOOK",
                    "requestid": "1",
                    "command": "SUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "GOOG,MSFT",
                        "fields": "0,1,2,3"
                    }
                }]
            })),
            call(StringMatchesJson({
                "requests": [{
                    "service": "NYSE_BOOK",
                    "requestid": "2",
                    "command": "UNSUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "GOOG,MSFT"
                    }
                }]
            })),
        ]
        socket.send.assert_has_awaits(send_awaited, any_order=False)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_nyse_book_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'NYSE_BOOK', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.nyse_book_subs(['GOOG', 'MSFT'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_nyse_book_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'NYSE_BOOK', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.nyse_book_unsubs(['GOOG', 'MSFT'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_nyse_book_add_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'NYSE_BOOK', 'ADD')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.nyse_book_add(['INTC'])

    ##########################################################################
    # NASDAQ_BOOK

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_nasdaq_book_subs_success_and_add_all_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'NASDAQ_BOOK', 'SUBS'))]

        await self.client.nasdaq_book_subs(['GOOG', 'MSFT'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'NASDAQ_BOOK',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'GOOG,MSFT',
                'fields': ('0,1,2,3')
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'NASDAQ_BOOK', 'ADD'))]

        await self.client.nasdaq_book_add(['INTC'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'NASDAQ_BOOK',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'INTC',
                'fields': '0,1,2,3'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_nasdaq_book_unsubs_success_all_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {"data": [{"service": "NASDAQ_BOOK", "command": "SUBS", "timestamp": REQUEST_TIMESTAMP, 'content': {}}]}

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'NASDAQ_BOOK', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'NASDAQ_BOOK', 'UNSUBS'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_nasdaq_book_handler(handler)
        self.client.add_nasdaq_book_handler(async_handler)

        await self.client.nasdaq_book_subs(['GOOG', 'MSFT'])
        await self.client.handle_message()
        await self.client.nasdaq_book_unsubs(['GOOG', 'MSFT'])

        self.assert_handler_called_once_with(
                handler, {"service": "NASDAQ_BOOK",
                          "command": "SUBS",
                          "timestamp": REQUEST_TIMESTAMP,
                          'content': {}})
        self.assert_handler_called_once_with(
                async_handler, {"service": "NASDAQ_BOOK",
                                "command": "SUBS",
                                "timestamp": REQUEST_TIMESTAMP,
                                'content': {}})
        send_awaited = [
            call(StringMatchesJson({
                "requests": [{
                    "service": "NASDAQ_BOOK",
                    "requestid": "1",
                    "command": "SUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "GOOG,MSFT",
                        "fields": "0,1,2,3"
                    }
                }]
            })),
            call(StringMatchesJson({
                "requests": [{
                    "service": "NASDAQ_BOOK",
                    "requestid": "2",
                    "command": "UNSUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "GOOG,MSFT"
                    }
                }]
            })),
        ]
        socket.send.assert_has_awaits(send_awaited, any_order=False)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_nasdaq_book_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'NASDAQ_BOOK', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.nasdaq_book_subs(['GOOG', 'MSFT'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_nasdaq_book_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'NASDAQ_BOOK', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.nasdaq_book_unsubs(['GOOG', 'MSFT'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_nasdaq_book_add_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'NASDAQ_BOOK', 'ADD')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.nasdaq_book_add(['INTC'])

    ##########################################################################
    # OPTIONS_BOOK

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_options_book_subs_and_add_success_all_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'OPTIONS_BOOK', 'SUBS'))]

        await self.client.options_book_subs(
            ['GOOG  240517C00070000', 'MSFT  240517C00160000'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'OPTIONS_BOOK',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'GOOG  240517C00070000,MSFT  240517C00160000',
                'fields': '0,1,2,3'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'OPTIONS_BOOK', 'ADD'))]

        await self.client.options_book_add(['ADBE  240614C00500000'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'OPTIONS_BOOK',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
            'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'ADBE  240614C00500000',
                'fields': '0,1,2,3'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_options_book_unsubs_success_all_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {"data": [{"service": "OPTIONS_BOOK", "command": "SUBS", "timestamp": REQUEST_TIMESTAMP, 'content': {}}]}

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'OPTIONS_BOOK', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'OPTIONS_BOOK', 'UNSUBS'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_options_book_handler(handler)
        self.client.add_options_book_handler(async_handler)

        await self.client.options_book_subs(
            ['GOOG  240517C00070000', 'MSFT  240517C00160000'])
        await self.client.handle_message()
        await self.client.options_book_unsubs(
            ['GOOG  240517C00070000', 'MSFT  240517C00160000'])

        self.assert_handler_called_once_with(
                handler, {"service": "OPTIONS_BOOK",
                          "command": "SUBS",
                          "timestamp": REQUEST_TIMESTAMP,
                          'content': {}})
        self.assert_handler_called_once_with(
                async_handler, {"service": "OPTIONS_BOOK",
                                "command": "SUBS",
                                "timestamp": REQUEST_TIMESTAMP,
                                'content': {}})
        send_awaited = [
            call(StringMatchesJson({
                "requests": [{
                    "service": "OPTIONS_BOOK",
                    "requestid": "1",
                    "command": "SUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "GOOG  240517C00070000,MSFT  240517C00160000",
                        "fields": "0,1,2,3"
                    }
                }]
            })),
            call(StringMatchesJson({
                "requests": [{
                    "service": "OPTIONS_BOOK",
                    "requestid": "2",
                    "command": "UNSUBS",
                    "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
                    "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
                    "parameters": {
                        "keys": "GOOG  240517C00070000,MSFT  240517C00160000"
                    }
                }]
            })),
        ]
        socket.send.assert_has_awaits(send_awaited, any_order=False)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_options_book_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'OPTIONS_BOOK', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.options_book_subs(
                ['GOOG  240517C00070000', 'MSFT  240517C00160000'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_options_book_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'OPTIONS_BOOK', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.options_book_unsubs(
                ['GOOG  240517C00070000', 'MSFT  240517C00160000'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_options_book_add_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'OPTIONS_BOOK', 'ADD')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.options_book_add(['ADBE  240614C00500000'])

    ##########################################################################
    # Common book handler functionality

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_nyse_book_handler(self, ws_connect):
        async def subs():
            await self.client.nyse_book_subs(['GOOG', 'MSFT'])

        def register_handler():
            handler = Mock()
            async_handler = AsyncMock()
            self.client.add_nyse_book_handler(handler)
            self.client.add_nyse_book_handler(async_handler)
            return handler, async_handler

        await self.__test_book_handler(
            ws_connect, 'NYSE_BOOK', subs, register_handler)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_nasdaq_book_handler(self, ws_connect):
        async def subs():
            await self.client.nasdaq_book_subs(['GOOG', 'MSFT'])

        def register_handler():
            handler = Mock()
            async_handler = AsyncMock()
            self.client.add_nasdaq_book_handler(handler)
            self.client.add_nasdaq_book_handler(async_handler)
            return handler, async_handler

        await self.__test_book_handler(
            ws_connect, 'NASDAQ_BOOK', subs, register_handler)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_options_book_handler(self, ws_connect):
        async def subs():
            await self.client.options_book_subs(['GOOG', 'MSFT'])

        def register_handler():
            handler = Mock()
            async_handler = AsyncMock()
            self.client.add_options_book_handler(handler)
            self.client.add_options_book_handler(async_handler)
            return handler, async_handler

        await self.__test_book_handler(
            ws_connect, 'OPTIONS_BOOK', subs, register_handler)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_book_names_each_per_exchange_field(self, ws_connect):
        # The handler test above compares whole messages through a diff
        # helper, whose failure says only that they differed -- so a label
        # moving is invisible in it. The bid and ask breakdowns are separate
        # enums, and a rename of one leaves the other relabeling as it was,
        # so each is asserted here.
        socket = await self.login_and_get_socket(ws_connect)
        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'NYSE_BOOK', 'SUBS')),
            json.dumps({'data': [{
                'service': 'NYSE_BOOK',
                'timestamp': 1590532470149,
                'command': 'SUBS',
                'content': [{
                    'key': 'MSFT',
                    '1': 1590532442608,
                    '2': [{'0': 181.77, '1': 100, '2': 1,
                           '3': [{'0': 'edgx', '1': 100, '2': 63150257}]}],
                    '3': [{'0': 181.78, '1': 200, '2': 1,
                           '3': [{'0': 'arcx', '1': 200, '2': 62767878}]}],
                }]}]})]

        handler = Mock()
        self.client.add_nyse_book_handler(handler)
        await self.client.nyse_book_subs(['MSFT'])
        await self.client.handle_message()

        handler.assert_called_once()
        content = handler.call_args[0][0]['content'][0]
        self.assertEqual(
                {'EXCHANGE': 'edgx', 'BID_VOLUME': 100,
                 'QUOTE_TIME': 63150257},
                content['BIDS'][0]['BIDS'][0])
        self.assertEqual(
                {'EXCHANGE': 'arcx', 'ASK_VOLUME': 200,
                 'QUOTE_TIME': 62767878},
                content['ASKS'][0]['ASKS'][0])

    @no_duplicates
    async def __test_book_handler(
            self, ws_connect, service, subs, register_handler):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {
            'data': [
                {
                    'service': service,
                    'timestamp': 1590532470149,
                    'command': 'SUBS',
                    'content': [
                        {
                            'key': 'MSFT',
                            '1': 1590532442608,
                            '2': [
                                {
                                    '0': 181.77,
                                    '1': 100,
                                    '2': 1,
                                    '3': [
                                        {
                                            '0': 'edgx',
                                            '1': 100,
                                            '2': 63150257
                                        }
                                    ]
                                },
                                {
                                    '0': 181.75,
                                    '1': 545,
                                    '2': 2,
                                    '3': [
                                        {
                                            '0': 'NSDQ',
                                            '1': 345,
                                            '2': 62685730
                                        },
                                        {
                                            '0': 'arcx',
                                            '1': 200,
                                            '2': 63242588
                                        }
                                    ]
                                },
                                {
                                    '0': 157.0,
                                    '1': 100,
                                    '2': 1,
                                    '3': [
                                        {
                                            '0': 'batx',
                                            '1': 100,
                                            '2': 63082708
                                        }
                                    ]
                                }
                            ],
                            '3': [
                                {
                                    '0': 181.95,
                                    '1': 100,
                                    '2': 1,
                                    '3': [
                                        {
                                            '0': 'arcx',
                                            '1': 100,
                                            '2': 63006734
                                        }
                                    ]
                                },
                                {
                                    '0': 181.98,
                                    '1': 48,
                                    '2': 1,
                                    '3': [
                                        {
                                            '0': 'NSDQ',
                                            '1': 48,
                                            '2': 62327464
                                        }
                                    ]
                                },
                                {
                                    '0': 182.3,
                                    '1': 100,
                                    '2': 1,
                                    '3': [
                                        {
                                            '0': 'edgx',
                                            '1': 100,
                                            '2': 63192542
                                        }
                                    ]
                                },
                                {
                                    '0': 186.8,
                                    '1': 700,
                                    '2': 1,
                                    '3': [
                                        {
                                            '0': 'batx',
                                            '1': 700,
                                            '2': 60412822
                                        }
                                    ]
                                }
                            ]
                        },
                        {
                            'key': 'GOOG',
                            '1': 1590532323728,
                            '2': [
                                {
                                    '0': 1418.0,
                                    '1': 1,
                                    '2': 1,
                                    '3': [
                                        {
                                            '0': 'NSDQ',
                                            '1': 1,
                                            '2': 54335011
                                        }
                                    ]
                                },
                                {
                                    '0': 1417.26,
                                    '1': 100,
                                    '2': 1,
                                    '3': [
                                        {
                                            '0': 'batx',
                                            '1': 100,
                                            '2': 62782324
                                        }
                                    ]
                                },
                                {
                                    '0': 1417.25,
                                    '1': 100,
                                    '2': 1,
                                    '3': [
                                        {
                                            '0': 'arcx',
                                            '1': 100,
                                            '2': 62767878
                                        }
                                    ]
                                },
                                {
                                    '0': 1400.88,
                                    '1': 100,
                                    '2': 1,
                                    '3': [
                                        {
                                            '0': 'edgx',
                                            '1': 100,
                                            '2': 54000952
                                        }
                                    ]
                                }
                            ],
                            '3': [
                                {
                                    '0': 1421.0,
                                    '1': 300,
                                    '2': 2,
                                    '3': [
                                        {
                                            '0': 'edgx',
                                            '1': 200,
                                            '2': 56723908
                                        },
                                        {
                                            '0': 'arcx',
                                            '1': 100,
                                            '2': 62709059
                                        }
                                    ]
                                },
                                {
                                    '0': 1421.73,
                                    '1': 10,
                                    '2': 1,
                                    '3': [
                                        {
                                            '0': 'NSDQ',
                                            '1': 10,
                                            '2': 62737731
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, service, 'SUBS')),
            json.dumps(stream_item)]
        await subs()

        handler, async_handler = register_handler()
        await self.client.handle_message()

        expected_item = {
            'service': service,
            'timestamp': 1590532470149,
            'command': 'SUBS',
            'content': [
                        {
                            'key': 'MSFT',
                            'BOOK_TIME': 1590532442608,
                            'BIDS': [
                                {
                                    'BID_PRICE': 181.77,
                                    'TOTAL_VOLUME': 100,
                                    'NUM_BIDS': 1,
                                    'BIDS': [
                                        {
                                            'EXCHANGE': 'edgx',
                                            'BID_VOLUME': 100,
                                            'QUOTE_TIME': 63150257
                                        }
                                    ]
                                },
                                {
                                    'BID_PRICE': 181.75,
                                    'TOTAL_VOLUME': 545,
                                    'NUM_BIDS': 2,
                                    'BIDS': [
                                        {
                                            'EXCHANGE': 'NSDQ',
                                            'BID_VOLUME': 345,
                                            'QUOTE_TIME': 62685730
                                        },
                                        {
                                            'EXCHANGE': 'arcx',
                                            'BID_VOLUME': 200,
                                            'QUOTE_TIME': 63242588
                                        }
                                    ]
                                },
                                {
                                    'BID_PRICE': 157.0,
                                    'TOTAL_VOLUME': 100,
                                    'NUM_BIDS': 1,
                                    'BIDS': [
                                        {
                                            'EXCHANGE': 'batx',
                                            'BID_VOLUME': 100,
                                            'QUOTE_TIME': 63082708
                                        }
                                    ]
                                }
                            ],
                            'ASKS': [
                                {
                                    'ASK_PRICE': 181.95,
                                    'TOTAL_VOLUME': 100,
                                    'NUM_ASKS': 1,
                                    'ASKS': [
                                        {
                                            'EXCHANGE': 'arcx',
                                            'ASK_VOLUME': 100,
                                            'QUOTE_TIME': 63006734
                                        }
                                    ]
                                },
                                {
                                    'ASK_PRICE': 181.98,
                                    'TOTAL_VOLUME': 48,
                                    'NUM_ASKS': 1,
                                    'ASKS': [
                                        {
                                            'EXCHANGE': 'NSDQ',
                                            'ASK_VOLUME': 48,
                                            'QUOTE_TIME': 62327464
                                        }
                                    ]
                                },
                                {
                                    'ASK_PRICE': 182.3,
                                    'TOTAL_VOLUME': 100,
                                    'NUM_ASKS': 1,
                                    'ASKS': [
                                        {
                                            'EXCHANGE': 'edgx',
                                            'ASK_VOLUME': 100,
                                            'QUOTE_TIME': 63192542
                                        }
                                    ]
                                },
                                {
                                    'ASK_PRICE': 186.8,
                                    'TOTAL_VOLUME': 700,
                                    'NUM_ASKS': 1,
                                    'ASKS': [
                                        {
                                            'EXCHANGE': 'batx',
                                            'ASK_VOLUME': 700,
                                            'QUOTE_TIME': 60412822
                                        }
                                    ]
                                }
                            ]
                        },
                {
                            'key': 'GOOG',
                            'BOOK_TIME': 1590532323728,
                            'BIDS': [
                                {
                                    'BID_PRICE': 1418.0,
                                    'TOTAL_VOLUME': 1,
                                    'NUM_BIDS': 1,
                                    'BIDS': [
                                        {
                                            'EXCHANGE': 'NSDQ',
                                            'BID_VOLUME': 1,
                                            'QUOTE_TIME': 54335011
                                        }
                                    ]
                                },
                                {
                                    'BID_PRICE': 1417.26,
                                    'TOTAL_VOLUME': 100,
                                    'NUM_BIDS': 1,
                                    'BIDS': [
                                        {
                                            'EXCHANGE': 'batx',
                                            'BID_VOLUME': 100,
                                            'QUOTE_TIME': 62782324
                                        }
                                    ]
                                },
                                {
                                    'BID_PRICE': 1417.25,
                                    'TOTAL_VOLUME': 100,
                                    'NUM_BIDS': 1,
                                    'BIDS': [
                                        {
                                            'EXCHANGE': 'arcx',
                                            'BID_VOLUME': 100,
                                            'QUOTE_TIME': 62767878
                                        }
                                    ]
                                },
                                {
                                    'BID_PRICE': 1400.88,
                                    'TOTAL_VOLUME': 100,
                                    'NUM_BIDS': 1,
                                    'BIDS': [
                                        {
                                            'EXCHANGE': 'edgx',
                                            'BID_VOLUME': 100,
                                            'QUOTE_TIME': 54000952
                                        }
                                    ]
                                }
                            ],
                            'ASKS': [
                                {
                                    'ASK_PRICE': 1421.0,
                                    'TOTAL_VOLUME': 300,
                                    'NUM_ASKS': 2,
                                    'ASKS': [
                                        {
                                            'EXCHANGE': 'edgx',
                                            'ASK_VOLUME': 200,
                                            'QUOTE_TIME': 56723908
                                        },
                                        {
                                            'EXCHANGE': 'arcx',
                                            'ASK_VOLUME': 100,
                                            'QUOTE_TIME': 62709059
                                        }
                                    ]
                                },
                                {
                                    'ASK_PRICE': 1421.73,
                                    'TOTAL_VOLUME': 10,
                                    'NUM_ASKS': 1,
                                    'ASKS': [
                                        {
                                            'EXCHANGE': 'NSDQ',
                                            'ASK_VOLUME': 10,
                                            'QUOTE_TIME': 62737731
                                        }
                                    ]
                                }
                            ]
                        }
            ]
        }

        self.assert_handler_called_once_with(handler, expected_item)
        self.assert_handler_called_once_with(async_handler, expected_item)

    ##########################################################################
    # SCREENER_EQUITY

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_screener_equity_subs_and_add_success_all_fields(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'SCREENER_EQUITY', 'SUBS'))]

        await self.client.screener_equity_subs(['NYSE_VOLUME_5', 'NASDAQ_VOLUME_5'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'SCREENER_EQUITY',
            'command': 'SUBS',
            'requestid': '1',
            'SchwabClientCustomerId': self.pref_customer_id,
            'SchwabClientCorrelId': self.pref_correl_id,
            'parameters': {
                'keys': 'NYSE_VOLUME_5,NASDAQ_VOLUME_5',
                'fields': '0,1,2,3,4'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'SCREENER_EQUITY', 'ADD'))]

        await self.client.screener_equity_add(['$DJI_TRADES_10'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'SCREENER_EQUITY',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': self.pref_customer_id,
            'SchwabClientCorrelId': self.pref_correl_id,
            'parameters': {
                'keys': '$DJI_TRADES_10',
                'fields': '0,1,2,3,4'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_screener_equity_unsubs_success_all_fields(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('SCREENER_EQUITY', 'SUBS')

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'SCREENER_EQUITY', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'SCREENER_EQUITY', 'UNSUBS', 'UNSUBS command succeeded'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_screener_equity_handler(handler)
        self.client.add_screener_equity_handler(async_handler)

        await self.client.screener_equity_subs(['NYSE_VOLUME_5', 'NASDAQ_VOLUME_5'])
        await self.client.handle_message()
        await self.client.screener_equity_unsubs(['NYSE_VOLUME_5', 'NASDAQ_VOLUME_5'])

        self.assert_handler_called_once_with(
                handler, {'service': 'SCREENER_EQUITY',
                          'command': 'SUBS',
                          'timestamp': REQUEST_TIMESTAMP})
        self.assert_handler_called_once_with(
                async_handler, {'service': 'SCREENER_EQUITY',
                                'command': 'SUBS',
                                'timestamp': REQUEST_TIMESTAMP})

        send_awaited = [
            call(StringMatchesJson({
                'requests': [{
                    'service': 'SCREENER_EQUITY',
                    'requestid': '1',
                    'command': 'SUBS',
                    'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
                    'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
                    'parameters': {
                        'keys': 'NYSE_VOLUME_5,NASDAQ_VOLUME_5',
                        'fields': '0,1,2,3,4'
                    }
                }]
            })),
            call(StringMatchesJson({
                'requests': [{
                    'service': 'SCREENER_EQUITY',
                    'requestid': '2',
                    'command': 'UNSUBS',
                    'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
                    'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
                    'parameters': {
                        'keys': 'NYSE_VOLUME_5,NASDAQ_VOLUME_5'
                    }
                }]
            })),
        ]
        socket.send.assert_has_awaits(send_awaited, any_order=False)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_screener_equity_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'SCREENER_EQUITY', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.screener_equity_subs(['NYSE_VOLUME_5', 'NASDAQ_VOLUME_5'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_screener_equity_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'SCREENER_EQUITY', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.screener_equity_unsubs(['NYSE_VOLUME_5', 'NASDAQ_VOLUME_5'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_screener_equity_add_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'SCREENER_EQUITY', 'ADD')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.screener_equity_add(['$DJI_TRADES_10'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_screener_equity_handler(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {
            'data': [{
                'service': 'SCREENER_EQUITY',
                'timestamp': 1718996308740,
                'command': 'SUBS',
                'content': [{
                    'key': 'NYSE_VOLUME_5',
                    '1': 1718996308700,
                    '2': 'VOLUME',
                    '3': 5,
                    '4': [{
                        'symbol': 'XLF',
                        'description': 'SELECT STR FINANCIAL SELECT SPDR ETF',
                        'lastPrice': 41.305,
                        'netChange': 41.305,
                        'netPercentChange': 1,
                        'marketShare': 3.40228332,
                        'totalVolume': 51730877,
                        'volume': 1760031,
                        'trades': 644
                    }, {
                        'symbol': 'HYG',
                        'description': 'ISHARES IBOXX HIGH YIELD BOND ETF',
                        'lastPrice': 77.34,
                        'netChange': 77.34,
                        'netPercentChange': 1,
                        'marketShare': 2.10192454,
                        'totalVolume': 51730877,
                        'volume': 1087344,
                        'trades': 354
                    }, {
                        'symbol': 'CX',
                        'description': 'Cemex Sab De C V ADR',
                        'lastPrice': 6.26,
                        'netChange': 6.26,
                        'netPercentChange': 1,
                        'marketShare': 1.93456028,
                        'totalVolume': 51730877,
                        'volume': 1000765,
                        'trades': 825
                    }, {
                        'symbol': 'ABEV',
                        'description': 'Ambev S A ADR',
                        'lastPrice': 2.05,
                        'netChange': 2.05,
                        'netPercentChange': 1,
                        'marketShare': 1.84930559,
                        'totalVolume': 51730877,
                        'volume': 956662,
                        'trades': 1598
                    }, {
                        'symbol': 'CHPT',
                        'description': 'Chargepoint Holdings A',
                        'lastPrice': 1.365,
                        'netChange': 1.365,
                        'netPercentChange': 1,
                        'marketShare': 1.73645809,
                        'totalVolume': 51730877,
                        'volume': 898285,
                        'trades': 1024
                    }, {
                        'symbol': 'GME',
                        'description': 'Gamestop Corp A',
                        'lastPrice': 23.78,
                        'netChange': 23.78,
                        'netPercentChange': 1,
                        'marketShare': 1.28934021,
                        'totalVolume': 51730877,
                        'volume': 666987,
                        'trades': 5590
                    }, {
                        'symbol': 'DNA',
                        'description': 'GINKGO BIOWORKS HLDG A',
                        'lastPrice': 0.4151,
                        'netChange': 0.4151,
                        'netPercentChange': 1,
                        'marketShare': 0.93249917,
                        'totalVolume': 51730877,
                        'volume': 482390,
                        'trades': 880
                    }, {
                        'symbol': 'TELL',
                        'description': 'Tellurian Investment',
                        'lastPrice': 0.6347,
                        'netChange': 0.6347,
                        'netPercentChange': 1,
                        'marketShare': 0.84341311,
                        'totalVolume': 51730877,
                        'volume': 436305,
                        'trades': 378
                    }, {
                        'symbol': 'KVUE',
                        'description': 'KENVUE Inc',
                        'lastPrice': 18.775,
                        'netChange': 18.775,
                        'netPercentChange': 1,
                        'marketShare': 0.82902519,
                        'totalVolume': 51730877,
                        'volume': 428862,
                        'trades': 1654
                    }, {
                        'symbol': 'DNN',
                        'description': 'DENISON MINES CORP',
                        'lastPrice': 2.02,
                        'netChange': 2.02,
                        'netPercentChange': 1,
                        'marketShare': 0.82405717,
                        'totalVolume': 51730877,
                        'volume': 426292,
                        'trades': 270
                    }]
                }, {
                    'key': 'NASDAQ_VOLUME_5',
                    '1': 1718996308714,
                    '2': 'VOLUME',
                    '3': 5,
                    '4': [{
                        'symbol': 'NVDA',
                        'description': 'Nvidia Corp',
                        'lastPrice': 126.5299,
                        'netChange': 126.5299,
                        'netPercentChange': 1,
                        'marketShare': 5.84329551,
                        'totalVolume': 41090169,
                        'volume': 2401020,
                        'trades': 18452
                    },
                    {
                        'symbol': 'AREB',
                        'description': 'AMERICAN REBEL HLDGS',
                        'lastPrice': 0.7776,
                        'netChange': 0.7776,
                        'netPercentChange': 1,
                        'marketShare': 4.2931729,
                        'totalVolume': 41090169,
                        'volume': 1764072,
                        'trades': 2192
                    },
                    {
                        'symbol': 'KTRA',
                        'description': 'KINTARA THERAPEUTICS',
                        'lastPrice': 0.25335,
                        'netChange': 0.25335,
                        'netPercentChange': 1,
                        'marketShare': 2.12389976,
                        'totalVolume': 41090169,
                        'volume': 872714,
                        'trades': 333
                    },
                    {
                        'symbol': 'AAPL',
                        'description': 'Apple Inc',
                        'lastPrice': 210.2967,
                        'netChange': 210.2967,
                        'netPercentChange': 1,
                        'marketShare': 1.71872012,
                        'totalVolume': 41090169,
                        'volume': 706225,
                        'trades': 7024
                    },
                    {
                        'symbol': 'NKLA',
                        'description': 'NIKOLA CORP',
                        'lastPrice': 0.359,
                        'netChange': 0.359,
                        'netPercentChange': 1,
                        'marketShare': 1.64388226,
                        'totalVolume': 41090169,
                        'volume': 675474,
                        'trades': 1016
                    },
                    {
                        'symbol': 'SHCR',
                        'description': 'SHARECARE INC A',
                        'lastPrice': 1.365,
                        'netChange': 1.365,
                        'netPercentChange': 1,
                        'marketShare': 1.6360288,
                        'totalVolume': 41090169,
                        'volume': 672247,
                        'trades': 265
                    },
                    {
                        'symbol': 'SIRI',
                        'description': 'Sirius Xm Hldgs Inc',
                        'lastPrice': 2.995,
                        'netChange': 2.995,
                        'netPercentChange': 1,
                        'marketShare': 1.63058224,
                        'totalVolume': 41090169,
                        'volume': 670009,
                        'trades': 983
                    },
                    {
                        'symbol': 'CRKN',
                        'description': 'Crown Electrokinetic',
                        'lastPrice': 0.0401,
                        'netChange': 0.0401,
                        'netPercentChange': 1,
                        'marketShare': 1.34467201,
                        'totalVolume': 41090169,
                        'volume': 552528,
                        'trades': 885
                    },
                    {
                        'symbol': 'TSLA',
                        'description': 'Tesla Inc',
                        'lastPrice': 181.4049,
                        'netChange': 181.4049,
                        'netPercentChange': 1,
                        'marketShare': 1.0218454,
                        'totalVolume': 41090169,
                        'volume': 419878,
                        'trades': 4613
                    },
                    {
                        'symbol': 'WBD',
                        'description': 'Warner Brothers Disc',
                        'lastPrice': 7.125,
                        'netChange': 7.125,
                        'netPercentChange': 1,
                        'marketShare': 0.89736793,
                        'totalVolume': 41090169,
                        'volume': 368730,
                        'trades': 1249
                    }]
                }]
            }]
        }

        socket.recv.side_effect = [
            json.dumps(self.success_response(
                1, 'SCREENER_EQUITY', 'SUBS')),
            json.dumps(stream_item)]
        await self.client.screener_equity_subs(['NYSE_VOLUME_5', 'NASDAQ_VOLUME_5'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_screener_equity_handler(handler)
        self.client.add_screener_equity_handler(async_handler)
        await self.client.handle_message()

        expected_item = {
            'service': 'SCREENER_EQUITY',
            'timestamp': 1718996308740,
            'command': 'SUBS',
            'content': [{
                'key': 'NYSE_VOLUME_5',
                'TIMESTAMP': 1718996308700,
                'SORT_FIELD': 'VOLUME',
                'FREQUENCY': 5,
                'ITEMS': [{
                    'symbol': 'XLF',
                    'description': 'SELECT STR FINANCIAL SELECT SPDR ETF',
                    'lastPrice': 41.305,
                    'netChange': 41.305,
                    'netPercentChange': 1,
                    'marketShare': 3.40228332,
                    'totalVolume': 51730877,
                    'volume': 1760031,
                    'trades': 644
                }, {
                    'symbol': 'HYG',
                    'description': 'ISHARES IBOXX HIGH YIELD BOND ETF',
                    'lastPrice': 77.34,
                    'netChange': 77.34,
                    'netPercentChange': 1,
                    'marketShare': 2.10192454,
                    'totalVolume': 51730877,
                    'volume': 1087344,
                    'trades': 354
                }, {
                    'symbol': 'CX',
                    'description': 'Cemex Sab De C V ADR',
                    'lastPrice': 6.26,
                    'netChange': 6.26,
                    'netPercentChange': 1,
                    'marketShare': 1.93456028,
                    'totalVolume': 51730877,
                    'volume': 1000765,
                    'trades': 825
                }, {
                    'symbol': 'ABEV',
                    'description': 'Ambev S A ADR',
                    'lastPrice': 2.05,
                    'netChange': 2.05,
                    'netPercentChange': 1,
                    'marketShare': 1.84930559,
                    'totalVolume': 51730877,
                    'volume': 956662,
                    'trades': 1598
                },{
                    'symbol': 'CHPT',
                    'description': 'Chargepoint Holdings A',
                    'lastPrice': 1.365,
                    'netChange': 1.365,
                    'netPercentChange': 1,
                    'marketShare': 1.73645809,
                    'totalVolume': 51730877,
                    'volume': 898285,
                    'trades': 1024
                }, {
                    'symbol': 'GME',
                    'description': 'Gamestop Corp A',
                    'lastPrice': 23.78,
                    'netChange': 23.78,
                    'netPercentChange': 1,
                    'marketShare': 1.28934021,
                    'totalVolume': 51730877,
                    'volume': 666987,
                    'trades': 5590
                }, {
                    'symbol': 'DNA',
                    'description': 'GINKGO BIOWORKS HLDG A',
                    'lastPrice': 0.4151,
                    'netChange': 0.4151,
                    'netPercentChange': 1,
                    'marketShare': 0.93249917,
                    'totalVolume': 51730877,
                    'volume': 482390,
                    'trades': 880
                }, {
                    'symbol': 'TELL',
                    'description': 'Tellurian Investment',
                    'lastPrice': 0.6347,
                    'netChange': 0.6347,
                    'netPercentChange': 1,
                    'marketShare': 0.84341311,
                    'totalVolume': 51730877,
                    'volume': 436305,
                    'trades': 378
                }, {
                    'symbol': 'KVUE',
                    'description': 'KENVUE Inc',
                    'lastPrice': 18.775,
                    'netChange': 18.775,
                    'netPercentChange': 1,
                    'marketShare': 0.82902519,
                    'totalVolume': 51730877,
                    'volume': 428862,
                    'trades': 1654
                }, {
                    'symbol': 'DNN',
                    'description': 'DENISON MINES CORP',
                    'lastPrice': 2.02,
                    'netChange': 2.02,
                    'netPercentChange': 1,
                    'marketShare': 0.82405717,
                    'totalVolume': 51730877,
                    'volume': 426292,
                    'trades': 270
                }]
            }, {
                'key': 'NASDAQ_VOLUME_5',
                'TIMESTAMP': 1718996308714,
                'SORT_FIELD': 'VOLUME',
                'FREQUENCY': 5,
                'ITEMS': [{
                    'symbol': 'NVDA',
                    'description': 'Nvidia Corp',
                    'lastPrice': 126.5299,
                    'netChange': 126.5299,
                    'netPercentChange': 1,
                    'marketShare': 5.84329551,
                    'totalVolume': 41090169,
                    'volume': 2401020,
                    'trades': 18452
                }, {
                    'symbol': 'AREB',
                    'description': 'AMERICAN REBEL HLDGS',
                    'lastPrice': 0.7776,
                    'netChange': 0.7776,
                    'netPercentChange': 1,
                    'marketShare': 4.2931729,
                    'totalVolume': 41090169,
                    'volume': 1764072,
                    'trades': 2192
                }, {
                    'symbol': 'KTRA',
                    'description': 'KINTARA THERAPEUTICS',
                    'lastPrice': 0.25335,
                    'netChange': 0.25335,
                    'netPercentChange': 1,
                    'marketShare': 2.12389976,
                    'totalVolume': 41090169,
                    'volume': 872714,
                    'trades': 333
                }, {
                    'symbol': 'AAPL',
                    'description': 'Apple Inc',
                    'lastPrice': 210.2967,
                    'netChange': 210.2967,
                    'netPercentChange': 1,
                    'marketShare': 1.71872012,
                    'totalVolume': 41090169,
                    'volume': 706225,
                    'trades': 7024
                }, {
                    'symbol': 'NKLA',
                    'description': 'NIKOLA CORP',
                    'lastPrice': 0.359,
                    'netChange': 0.359,
                    'netPercentChange': 1,
                    'marketShare': 1.64388226,
                    'totalVolume': 41090169,
                    'volume': 675474,
                    'trades': 1016
                }, {
                    'symbol': 'SHCR',
                    'description': 'SHARECARE INC A',
                    'lastPrice': 1.365,
                    'netChange': 1.365,
                    'netPercentChange': 1,
                    'marketShare': 1.6360288,
                    'totalVolume': 41090169,
                    'volume': 672247,
                    'trades': 265
                }, {
                    'symbol': 'SIRI',
                    'description': 'Sirius Xm Hldgs Inc',
                    'lastPrice': 2.995,
                    'netChange': 2.995,
                    'netPercentChange': 1,
                    'marketShare': 1.63058224,
                    'totalVolume': 41090169,
                    'volume': 670009,
                    'trades': 983
                }, {
                    'symbol': 'CRKN',
                    'description': 'Crown Electrokinetic',
                    'lastPrice': 0.0401,
                    'netChange': 0.0401,
                    'netPercentChange': 1,
                    'marketShare': 1.34467201,
                    'totalVolume': 41090169,
                    'volume': 552528,
                    'trades': 885
                }, {
                    'symbol': 'TSLA',
                    'description': 'Tesla Inc',
                    'lastPrice': 181.4049,
                    'netChange': 181.4049,
                    'netPercentChange': 1,
                    'marketShare': 1.0218454,
                    'totalVolume': 41090169,
                    'volume': 419878,
                    'trades': 4613
                }, {
                    'symbol': 'WBD',
                    'description': 'Warner Brothers Disc',
                    'lastPrice': 7.125,
                    'netChange': 7.125,
                    'netPercentChange': 1,
                    'marketShare': 0.89736793,
                    'totalVolume': 41090169,
                    'volume': 368730,
                    'trades': 1249
                }]
            }]
        }

        self.assert_handler_called_once_with(handler, expected_item)
        self.assert_handler_called_once_with(async_handler, expected_item)

    ##########################################################################
    # SCREENER_OPTION

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_screener_option_subs_and_add_success_all_fields(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'SCREENER_OPTION', 'SUBS'))]

        await self.client.screener_option_subs(['OPTION_PUT_VOLUME_5', 'OPTION_CALL_VOLUME_5'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'SCREENER_OPTION',
            'command': 'SUBS',
            'requestid': '1',
            'SchwabClientCustomerId': self.pref_customer_id,
            'SchwabClientCorrelId': self.pref_correl_id,
            'parameters': {
                'keys': 'OPTION_PUT_VOLUME_5,OPTION_CALL_VOLUME_5',
                'fields': '0,1,2,3,4'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'SCREENER_OPTION', 'ADD'))]

        await self.client.screener_option_add(['OPTION_ALL_TRADES_10'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'SCREENER_OPTION',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': self.pref_customer_id,
            'SchwabClientCorrelId': self.pref_correl_id,
            'parameters': {
                'keys': 'OPTION_ALL_TRADES_10',
                'fields': '0,1,2,3,4'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_screener_option_unsubs_success_all_fields(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('SCREENER_OPTION', 'SUBS')

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'SCREENER_OPTION', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'SCREENER_OPTION', 'UNSUBS', 'UNSUBS command succeeded'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_screener_option_handler(handler)
        self.client.add_screener_option_handler(async_handler)

        await self.client.screener_option_subs(['OPTION_PUT_VOLUME_5', 'OPTION_CALL_VOLUME_5'])
        await self.client.handle_message()
        await self.client.screener_option_unsubs(['OPTION_PUT_VOLUME_5', 'OPTION_CALL_VOLUME_5'])

        self.assert_handler_called_once_with(
                handler, {'service': 'SCREENER_OPTION',
                          'command': 'SUBS',
                          'timestamp': REQUEST_TIMESTAMP})
        self.assert_handler_called_once_with(
                async_handler, {'service': 'SCREENER_OPTION',
                                'command': 'SUBS',
                                'timestamp': REQUEST_TIMESTAMP})

        send_awaited = [
            call(StringMatchesJson({
                'requests': [{
                    'service': 'SCREENER_OPTION',
                    'requestid': '1',
                    'command': 'SUBS',
                    'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
                    'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
                    'parameters': {
                        'keys': 'OPTION_PUT_VOLUME_5,OPTION_CALL_VOLUME_5',
                        'fields': '0,1,2,3,4'
                    }
                }]
            })),
            call(StringMatchesJson({
                'requests': [{
                    'service': 'SCREENER_OPTION',
                    'requestid': '2',
                    'command': 'UNSUBS',
                    'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
                    'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
                    'parameters': {
                        'keys': 'OPTION_PUT_VOLUME_5,OPTION_CALL_VOLUME_5'
                    }
                }]
            })),
        ]
        socket.send.assert_has_awaits(send_awaited, any_order=False)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_screener_option_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'SCREENER_OPTION', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.screener_option_subs(['OPTION_PUT_VOLUME_5', 'OPTION_CALL_VOLUME_5'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_screener_option_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'SCREENER_OPTION', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.screener_option_unsubs(['OPTION_PUT_VOLUME_5', 'OPTION_CALL_VOLUME_5'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_screener_option_add_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'SCREENER_OPTION', 'ADD')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.screener_option_add(['OPTION_ALL_TRADES_10'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_screener_option_handler(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {
            'data': [{
                'service': 'SCREENER_OPTION',
                'timestamp': 1718996045319,
                'command': 'SUBS',
                'content': [{
                    'key': 'OPTION_PUT_VOLUME_5',
                    '1': 1718996045310,
                    '2': 'VOLUME',
                    '3': 5,
                    '4': [{
                        'symbol': 'SPY   240621P00541000',
                        'description': 'SPY    Jun 21 2024 541.0 Put',
                        'lastPrice': 0.02,
                        'netChange': -0.275,
                        'netPercentChange': -0.93220339,
                        'marketShare': 9.01754274,
                        'totalVolume': 218951,
                        'volume': 19744,
                        'trades': 151
                    }, {
                        'symbol': 'SPY   240816P00514000',
                        'description': 'SPY    Aug 16 2024 514.0 Put',
                        'lastPrice': 2.31,
                        'netChange': -0.045,
                        'netPercentChange': -0.01910828,
                        'marketShare': 2.84995273,
                        'totalVolume': 218951,
                        'volume': 6240,
                        'trades': 3
                    }, {
                        'symbol': 'NVDA  240621P00130000',
                        'description': 'NVDA   Jun 21 2024 130.0 Put',
                        'lastPrice': 3.35,
                        'netChange': 1.895,
                        'netPercentChange': 1.3024055,
                        'marketShare': 2.6544752,
                        'totalVolume': 218951,
                        'volume': 5812,
                        'trades': 213
                    }, {
                        'symbol': 'SPY   240621P00542000',
                        'description': 'SPY    Jun 21 2024 542.0 Put',
                        'lastPrice': 0.05,
                        'netChange': -0.395,
                        'netPercentChange': -0.88764045,
                        'marketShare': 2.44940649,
                        'totalVolume': 218951,
                        'volume': 5363,
                        'trades': 108
                    }, {
                        'symbol': 'SPY   240621P00544000',
                        'description': 'SPY    Jun 21 2024 544.0 Put',
                        'lastPrice': 0.38,
                        'netChange': -0.565,
                        'netPercentChange': -0.5978836,
                        'marketShare': 2.34481688,
                        'totalVolume': 218951,
                        'volume': 5134,
                        'trades': 427
                    }, {
                        'symbol': 'NVDA  240621P00125000',
                        'description': 'NVDA   Jun 21 2024 125.0 Put',
                        'lastPrice': 0.19,
                        'netChange': -0.1956,
                        'netPercentChange': -0.50726141,
                        'marketShare': 2.0223703,
                        'totalVolume': 218951,
                        'volume': 4428,
                        'trades': 526
                    }, {
                        'symbol': 'NVDA  240621P00126000',
                        'description': 'NVDA   Jun 21 2024 126.0 Put',
                        'lastPrice': 0.47,
                        'netChange': -0.06,
                        'netPercentChange': -0.11320755,
                        'marketShare': 1.62821819,
                        'totalVolume': 218951,
                        'volume': 3565,
                        'trades': 367
                    }, {
                        'symbol': 'SPY   240816P00530000',
                        'description': 'SPY    Aug 16 2024 530.0 Put',
                        'lastPrice': 4.33,
                        'netChange': 0.0296,
                        'netPercentChange': 0.00688308,
                        'marketShare': 1.42863015,
                        'totalVolume': 218951,
                        'volume': 3128,
                        'trades': 7
                    }, {
                        'symbol': 'QQQ   240621P00480000',
                        'description': 'QQQ    Jun 21 2024 480.0 Put',
                        'lastPrice': 0.51,
                        'netChange': -0.7029,
                        'netPercentChange': -0.57952016,
                        'marketShare': 1.25918585,
                        'totalVolume': 218951,
                        'volume': 2757,
                        'trades': 197
                    }, {
                        'symbol': 'SPY   240621P00543000',
                        'description': 'SPY    Jun 21 2024 543.0 Put',
                        'lastPrice': 0.14,
                        'netChange': -0.515,
                        'netPercentChange': -0.78625954,
                        'marketShare': 1.23909002,
                        'totalVolume': 218951,
                        'volume': 2713,
                        'trades': 237
                    }]
                }, {
                    'key': 'OPTION_CALL_VOLUME_5',
                    '1': 1718996045320,
                    '2': 'VOLUME',
                    '3': 5,
                    '4': [{
                        'symbol': 'SPY   240621C00546000',
                        'description': 'SPY    Jun 21 2024 546.0 Call',
                        'lastPrice': 0.07,
                        'netChange': -1.1656,
                        'netPercentChange': -0.94334736,
                        'marketShare': 3.07333386,
                        'totalVolume': 276898,
                        'volume': 8510,
                        'trades': 280
                    }, {
                        'symbol': 'SPY   240621C00545000',
                        'description': 'SPY    Jun 21 2024 545.0 Call',
                        'lastPrice': 0.25,
                        'netChange': -1.75,
                        'netPercentChange': -0.875,
                        'marketShare': 2.45613908,
                        'totalVolume': 276898,
                        'volume': 6801,
                        'trades': 503
                    }, {
                        'symbol': 'SIRI  240621C00003000',
                        'description': 'SIRI   Jun 21 2024 3.0 Call',
                        'lastPrice': 0.02,
                        'netChange': -0.035,
                        'netPercentChange': -0.63636364,
                        'marketShare': 1.90286676,
                        'totalVolume': 276898,
                        'volume': 5269,
                        'trades': 150
                    }, {
                        'symbol': 'NVDA  240621C00128000',
                        'description': 'NVDA   Jun 21 2024 128.0 Call',
                        'lastPrice': 0.26,
                        'netChange': -3.4431,
                        'netPercentChange': -0.92978856,
                        'marketShare': 1.77610528,
                        'totalVolume': 276898,
                        'volume': 4918,
                        'trades': 306
                    }, {
                        'symbol': 'NVDA  240621C00127000',
                        'description': 'NVDA   Jun 21 2024 127.0 Call',
                        'lastPrice': 0.6,
                        'netChange': -3.9032,
                        'netPercentChange': -0.86676141,
                        'marketShare': 1.6320089,
                        'totalVolume': 276898,
                        'volume': 4519,
                        'trades': 491
                    }, {
                        'symbol': 'NVDA  240621C00130000',
                        'description': 'NVDA   Jun 21 2024 130.0 Call',
                        'lastPrice': 0.05,
                        'netChange': -2.2177,
                        'netPercentChange': -0.97795123,
                        'marketShare': 1.4745502,
                        'totalVolume': 276898,
                        'volume': 4083,
                        'trades': 217
                    }, {
                        'symbol': 'SPY   240621C00544000',
                        'description': 'SPY    Jun 21 2024 544.0 Call',
                        'lastPrice': 0.68,
                        'netChange': -2.32,
                        'netPercentChange': -0.77333333,
                        'marketShare': 1.42254549,
                        'totalVolume': 276898,
                        'volume': 3939,
                        'trades': 286
                    }, {
                        'symbol': 'IWM   240621C00200000',
                        'description': 'IWM    Jun 21 2024 200.0 Call',
                        'lastPrice': 0.11,
                        'netChange': -0.7012,
                        'netPercentChange': -0.86439842,
                        'marketShare': 1.24197358,
                        'totalVolume': 276898,
                        'volume': 3439,
                        'trades': 108
                    }, {
                        'symbol': 'EWZ   240802C00030000',
                        'description': 'EWZ    Aug 2 2024 30.0 Call',
                        'lastPrice': 0.2,
                        'netChange': 0.0446,
                        'netPercentChange': 0.28700129,
                        'marketShare': 1.20983178,
                        'totalVolume': 276898,
                        'volume': 3350,
                        'trades': 1
                    }, {
                        'symbol': 'CYH   240719C00004000',
                        'description': 'CYH    Jul 19 2024 4.0 Call',
                        'lastPrice': 0.1,
                        'netChange': 0,
                        'netPercentChange': 0,
                        'marketShare': 1.08415373,
                        'totalVolume': 276898,
                        'volume': 3002,
                        'trades': 96
                    }]
                }]
            }]
        }

        socket.recv.side_effect = [
            json.dumps(self.success_response(
                1, 'SCREENER_OPTION', 'SUBS')),
            json.dumps(stream_item)]
        await self.client.screener_option_subs(['OPTION_PUT_VOLUME_5', 'OPTION_CALL_VOLUME_5'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_screener_option_handler(handler)
        self.client.add_screener_option_handler(async_handler)
        await self.client.handle_message()

        expected_item = {
            'service': 'SCREENER_OPTION',
            'timestamp': 1718996045319,
            'command': 'SUBS',
            'content': [{
                'key': 'OPTION_PUT_VOLUME_5',
                'TIMESTAMP': 1718996045310,
                'SORT_FIELD': 'VOLUME',
                'FREQUENCY': 5,
                'ITEMS': [{
                    'symbol': 'SPY   240621P00541000',
                    'description': 'SPY    Jun 21 2024 541.0 Put',
                    'lastPrice': 0.02,
                    'netChange': -0.275,
                    'netPercentChange': -0.93220339,
                    'marketShare': 9.01754274,
                    'totalVolume': 218951,
                    'volume': 19744,
                    'trades': 151
                }, {
                    'symbol': 'SPY   240816P00514000',
                    'description': 'SPY    Aug 16 2024 514.0 Put',
                    'lastPrice': 2.31,
                    'netChange': -0.045,
                    'netPercentChange': -0.01910828,
                    'marketShare': 2.84995273,
                    'totalVolume': 218951,
                    'volume': 6240,
                    'trades': 3
                }, {
                    'symbol': 'NVDA  240621P00130000',
                    'description': 'NVDA   Jun 21 2024 130.0 Put',
                    'lastPrice': 3.35,
                    'netChange': 1.895,
                    'netPercentChange': 1.3024055,
                    'marketShare': 2.6544752,
                    'totalVolume': 218951,
                    'volume': 5812,
                    'trades': 213
                }, {
                    'symbol': 'SPY   240621P00542000',
                    'description': 'SPY    Jun 21 2024 542.0 Put',
                    'lastPrice': 0.05,
                    'netChange': -0.395,
                    'netPercentChange': -0.88764045,
                    'marketShare': 2.44940649,
                    'totalVolume': 218951,
                    'volume': 5363,
                    'trades': 108
                }, {
                    'symbol': 'SPY   240621P00544000',
                    'description': 'SPY    Jun 21 2024 544.0 Put',
                    'lastPrice': 0.38,
                    'netChange': -0.565,
                    'netPercentChange': -0.5978836,
                    'marketShare': 2.34481688,
                    'totalVolume': 218951,
                    'volume': 5134,
                    'trades': 427
                }, {
                    'symbol': 'NVDA  240621P00125000',
                    'description': 'NVDA   Jun 21 2024 125.0 Put',
                    'lastPrice': 0.19,
                    'netChange': -0.1956,
                    'netPercentChange': -0.50726141,
                    'marketShare': 2.0223703,
                    'totalVolume': 218951,
                    'volume': 4428,
                    'trades': 526
                }, {
                    'symbol': 'NVDA  240621P00126000',
                    'description': 'NVDA   Jun 21 2024 126.0 Put',
                    'lastPrice': 0.47,
                    'netChange': -0.06,
                    'netPercentChange': -0.11320755,
                    'marketShare': 1.62821819,
                    'totalVolume': 218951,
                    'volume': 3565,
                    'trades': 367
                }, {
                    'symbol': 'SPY   240816P00530000',
                    'description': 'SPY    Aug 16 2024 530.0 Put',
                    'lastPrice': 4.33,
                    'netChange': 0.0296,
                    'netPercentChange': 0.00688308,
                    'marketShare': 1.42863015,
                    'totalVolume': 218951,
                    'volume': 3128,
                    'trades': 7
                }, {
                    'symbol': 'QQQ   240621P00480000',
                    'description': 'QQQ    Jun 21 2024 480.0 Put',
                    'lastPrice': 0.51,
                    'netChange': -0.7029,
                    'netPercentChange': -0.57952016,
                    'marketShare': 1.25918585,
                    'totalVolume': 218951,
                    'volume': 2757,
                    'trades': 197
                }, {
                    'symbol': 'SPY   240621P00543000',
                    'description': 'SPY    Jun 21 2024 543.0 Put',
                    'lastPrice': 0.14,
                    'netChange': -0.515,
                    'netPercentChange': -0.78625954,
                    'marketShare': 1.23909002,
                    'totalVolume': 218951,
                    'volume': 2713,
                    'trades': 237
                }]
            }, {
                'key': 'OPTION_CALL_VOLUME_5',
                'TIMESTAMP': 1718996045320,
                'SORT_FIELD': 'VOLUME',
                'FREQUENCY': 5,
                'ITEMS': [{
                    'symbol': 'SPY   240621C00546000',
                    'description': 'SPY    Jun 21 2024 546.0 Call',
                    'lastPrice': 0.07,
                    'netChange': -1.1656,
                    'netPercentChange': -0.94334736,
                    'marketShare': 3.07333386,
                    'totalVolume': 276898,
                    'volume': 8510,
                    'trades': 280
                }, {
                    'symbol': 'SPY   240621C00545000',
                    'description': 'SPY    Jun 21 2024 545.0 Call',
                    'lastPrice': 0.25,
                    'netChange': -1.75,
                    'netPercentChange': -0.875,
                    'marketShare': 2.45613908,
                    'totalVolume': 276898,
                    'volume': 6801,
                    'trades': 503
                }, {
                    'symbol': 'SIRI  240621C00003000',
                    'description': 'SIRI   Jun 21 2024 3.0 Call',
                    'lastPrice': 0.02,
                    'netChange': -0.035,
                    'netPercentChange': -0.63636364,
                    'marketShare': 1.90286676,
                    'totalVolume': 276898,
                    'volume': 5269,
                    'trades': 150
                }, {
                    'symbol': 'NVDA  240621C00128000',
                    'description': 'NVDA   Jun 21 2024 128.0 Call',
                    'lastPrice': 0.26,
                    'netChange': -3.4431,
                    'netPercentChange': -0.92978856,
                    'marketShare': 1.77610528,
                    'totalVolume': 276898,
                    'volume': 4918,
                    'trades': 306
                }, {
                    'symbol': 'NVDA  240621C00127000',
                    'description': 'NVDA   Jun 21 2024 127.0 Call',
                    'lastPrice': 0.6,
                    'netChange': -3.9032,
                    'netPercentChange': -0.86676141,
                    'marketShare': 1.6320089,
                    'totalVolume': 276898,
                    'volume': 4519,
                    'trades': 491
                }, {
                    'symbol': 'NVDA  240621C00130000',
                    'description': 'NVDA   Jun 21 2024 130.0 Call',
                    'lastPrice': 0.05,
                    'netChange': -2.2177,
                    'netPercentChange': -0.97795123,
                    'marketShare': 1.4745502,
                    'totalVolume': 276898,
                    'volume': 4083,
                    'trades': 217
                }, {
                    'symbol': 'SPY   240621C00544000',
                    'description': 'SPY    Jun 21 2024 544.0 Call',
                    'lastPrice': 0.68,
                    'netChange': -2.32,
                    'netPercentChange': -0.77333333,
                    'marketShare': 1.42254549,
                    'totalVolume': 276898,
                    'volume': 3939,
                    'trades': 286
                }, {
                    'symbol': 'IWM   240621C00200000',
                    'description': 'IWM    Jun 21 2024 200.0 Call',
                    'lastPrice': 0.11,
                    'netChange': -0.7012,
                    'netPercentChange': -0.86439842,
                    'marketShare': 1.24197358,
                    'totalVolume': 276898,
                    'volume': 3439,
                    'trades': 108
                }, {
                    'symbol': 'EWZ   240802C00030000',
                    'description': 'EWZ    Aug 2 2024 30.0 Call',
                    'lastPrice': 0.2,
                    'netChange': 0.0446,
                    'netPercentChange': 0.28700129,
                    'marketShare': 1.20983178,
                    'totalVolume': 276898,
                    'volume': 3350,
                    'trades': 1
                }, {
                    'symbol': 'CYH   240719C00004000',
                    'description': 'CYH    Jul 19 2024 4.0 Call',
                    'lastPrice': 0.1,
                    'netChange': 0,
                    'netPercentChange': 0,
                    'marketShare': 1.08415373,
                    'totalVolume': 276898,
                    'volume': 3002,
                    'trades': 96
                }]
            }]
        }

        self.assert_handler_called_once_with(handler, expected_item)
        self.assert_handler_called_once_with(async_handler, expected_item)

    ##########################################################################
    # NEWS_HEADLINE

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_news_headline_subs_and_add_success_all_fields(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'NEWS_HEADLINE', 'SUBS'))]

        await self.client.news_headline_subs(['AAPL', 'MSFT'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'NEWS_HEADLINE',
            'command': 'SUBS',
            'requestid': '1',
            'SchwabClientCustomerId': self.pref_customer_id,
            'SchwabClientCorrelId': self.pref_correl_id,
            'parameters': {
                'keys': 'AAPL,MSFT',
                'fields': '0,1,2,3,4,5,6,7,8,9,10,11'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'NEWS_HEADLINE', 'ADD'))]

        await self.client.news_headline_add(['NVDA'])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'NEWS_HEADLINE',
            'command': 'ADD',
            'requestid': '2',
            'SchwabClientCustomerId': self.pref_customer_id,
            'SchwabClientCorrelId': self.pref_correl_id,
            'parameters': {
                'keys': 'NVDA',
                'fields': '0,1,2,3,4,5,6,7,8,9,10,11'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_news_headline_unsubs_success(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('NEWS_HEADLINE', 'SUBS')

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'NEWS_HEADLINE', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(
                2, 'NEWS_HEADLINE', 'UNSUBS', 'UNSUBS command succeeded'))
        ]
        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_news_headline_handler(handler)
        self.client.add_news_headline_handler(async_handler)

        await self.client.news_headline_subs(['AAPL', 'MSFT'])
        await self.client.handle_message()
        await self.client.news_headline_unsubs(['AAPL', 'MSFT'])

        self.assert_handler_called_once_with(
                handler, {'service': 'NEWS_HEADLINE',
                          'command': 'SUBS',
                          'timestamp': REQUEST_TIMESTAMP})
        self.assert_handler_called_once_with(
                async_handler, {'service': 'NEWS_HEADLINE',
                                'command': 'SUBS',
                                'timestamp': REQUEST_TIMESTAMP})

        send_awaited = [
            call(StringMatchesJson({
                'requests': [{
                    'service': 'NEWS_HEADLINE',
                    'requestid': '1',
                    'command': 'SUBS',
                    'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
                    'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
                    'parameters': {
                        'keys': 'AAPL,MSFT',
                        'fields': '0,1,2,3,4,5,6,7,8,9,10,11'
                    }
                }]
            })),
            call(StringMatchesJson({
                'requests': [{
                    'service': 'NEWS_HEADLINE',
                    'requestid': '2',
                    'command': 'UNSUBS',
                    'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
                    'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
                    'parameters': {
                        'keys': 'AAPL,MSFT'
                    }
                }]
            })),
        ]
        socket.send.assert_has_awaits(send_awaited, any_order=False)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_news_headline_subs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'NEWS_HEADLINE', 'SUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.news_headline_subs(['AAPL', 'MSFT'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_news_headline_unsubs_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'NEWS_HEADLINE', 'UNSUBS')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.news_headline_unsubs(['AAPL', 'MSFT'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_news_headline_add_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        response = self.success_response(1, 'NEWS_HEADLINE', 'ADD')
        response['response'][0]['content']['code'] = 21
        socket.recv.side_effect = [json.dumps(response)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.news_headline_add(['NVDA'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_news_headline_handler(self, ws_connect):
        # Shaped like the items delivered to a live account: numbered fields
        # 1-11, the subscribed symbol in `key`, and `seq`. The values are
        # abbreviated rather than copied.
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {
            'data': [{
                'service': 'NEWS_HEADLINE',
                'timestamp': 1789602230512,
                'command': 'SUBS',
                'content': [{
                    'key': 'NVDA',
                    'seq': 1174,
                    '1': 1789602230000,
                    # Fields 2 and 5 were equal in every live item. Distinct
                    # here, so a swap of their names cannot pass.
                    '2': 'tag_example_newsml_2',
                    '3': 'A',
                    '4': 'Chipmaker announces a product',
                    '5': 'tag_example_newsml_5',
                    '6': ['Software', 'Semiconductors'],
                    '7': ['NVDA', 'META'],
                    '8': False,
                    '9': 'Reuters',
                    '10': 'Chipmaker announces a product, with more text',
                    '11': 'en-US',
                }]
            }]
        }

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'NEWS_HEADLINE', 'SUBS')),
            json.dumps(stream_item)]
        await self.client.news_headline_subs(['NVDA'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_news_headline_handler(handler)
        self.client.add_news_headline_handler(async_handler)
        await self.client.handle_message()

        expected_item = {
            'service': 'NEWS_HEADLINE',
            'timestamp': 1789602230512,
            'command': 'SUBS',
            'content': [{
                'key': 'NVDA',
                'seq': 1174,
                'TIMESTAMP': 1789602230000,
                'HEADLINE_ID': 'tag_example_newsml_2',
                'UNKNOWN_3': 'A',
                'HEADLINE': 'Chipmaker announces a product',
                'STORY_ID': 'tag_example_newsml_5',
                'CATEGORIES': ['Software', 'Semiconductors'],
                'SYMBOLS': ['NVDA', 'META'],
                'UNKNOWN_8': False,
                'SOURCE': 'Reuters',
                'TEXT': 'Chipmaker announces a product, with more text',
                'LANGUAGE': 'en-US',
            }]
        }

        self.assert_handler_called_once_with(handler, expected_item)
        self.assert_handler_called_once_with(async_handler, expected_item)

    ##########################################################################
    # VIEW

    _VIEW_SERVICES = (
        ('level_one_equity_view', 'LEVELONE_EQUITIES', 'LevelOneEquityFields'),
        ('level_one_option_view', 'LEVELONE_OPTIONS', 'LevelOneOptionFields'),
        ('level_one_futures_view', 'LEVELONE_FUTURES',
         'LevelOneFuturesFields'),
        ('level_one_futures_options_view', 'LEVELONE_FUTURES_OPTIONS',
         'LevelOneFuturesOptionsFields'),
        ('level_one_forex_view', 'LEVELONE_FOREX', 'LevelOneForexFields'),
    )

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_view_sends_fields_and_no_keys(self, ws_connect):
        # A VIEW naming one symbol changed every symbol's fields, measured
        # live, so the request carries no keys. SYMBOL is added.
        for method, service, enum_name in self._VIEW_SERVICES:
            with self.subTest(method):
                self.setUp()
                socket = await self.login_and_get_socket(ws_connect)
                socket.recv.side_effect = [json.dumps(
                    self.success_response(1, service, 'VIEW'))]
                enum = getattr(StreamClient, enum_name)

                await getattr(self.client, method)([enum(2), enum(1)])

                socket.recv.assert_awaited_once()
                self.assertEqual(self.request_from_socket_mock(socket), {
                    'service': service,
                    'command': 'VIEW',
                    'requestid': '1',
                    'SchwabClientCustomerId': CLIENT_CUSTOMER_ID,
                    'SchwabClientCorrelId': CLIENT_CORRELATION_ID,
                    'parameters': {'fields': '0,1,2'},
                })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_view_does_not_change_the_callers_list(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)
        socket.recv.side_effect = [json.dumps(
            self.success_response(1, 'LEVELONE_EQUITIES', 'VIEW'))]
        fields = [StreamClient.LevelOneEquityFields.BID_PRICE]

        await self.client.level_one_equity_view(fields)

        self.assertEqual([StreamClient.LevelOneEquityFields.BID_PRICE], fields)
        request = self.request_from_socket_mock(socket)
        self.assertEqual('0,1', request['parameters']['fields'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_view_sends_symbol_once_when_the_caller_names_it(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)
        socket.recv.side_effect = [json.dumps(
            self.success_response(1, 'LEVELONE_EQUITIES', 'VIEW'))]
        fields = StreamClient.LevelOneEquityFields
        await self.client.level_one_equity_view(
            [fields.SYMBOL, fields.BID_PRICE, fields.BID_PRICE])
        request = self.request_from_socket_mock(socket)
        self.assertEqual('0,1', request['parameters']['fields'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_view_takes_a_single_member_and_raw_numbers(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)
        socket.recv.side_effect = [json.dumps(
            self.success_response(1, 'LEVELONE_EQUITIES', 'VIEW'))]
        await self.client.level_one_equity_view(
            StreamClient.LevelOneEquityFields.ASK_PRICE)
        self.assertEqual('0,2', self.request_from_socket_mock(
            socket)['parameters']['fields'])

        self.setUp()
        self.client = StreamClient(self.http_client, enforce_enums=False)
        socket = await self.login_and_get_socket(ws_connect)
        socket.recv.side_effect = [json.dumps(
            self.success_response(1, 'LEVELONE_EQUITIES', 'VIEW'))]
        await self.client.level_one_equity_view([0, 1, '1', 3])
        self.assertEqual('0,1,3', self.request_from_socket_mock(
            socket)['parameters']['fields'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_view_refuses_values_that_are_not_field_numbers(
            self, ws_connect):
        # '12' iterated is the fields 1 and 2, and b'12' the fields 49 and
        # 50; a name skipped would send the rest without it. Each is refused.
        # Replies are queued, so a missing refusal fails on the missing
        # ValueError rather than on the mock.
        self.client = StreamClient(self.http_client, enforce_enums=False)
        socket = await self.login_and_get_socket(ws_connect)
        socket.recv.side_effect = [json.dumps(
            self.success_response(1, 'LEVELONE_EQUITIES', 'VIEW'))] * 7
        with self.assertRaisesRegex(ValueError, 'not a str'):
            await self.client.level_one_equity_view('12')
        with self.assertRaisesRegex(ValueError, 'not a bytes'):
            await self.client.level_one_equity_view(b'12')
        with self.assertRaisesRegex(ValueError, 'not a bytearray'):
            await self.client.level_one_equity_view(bytearray(b'12'))
        with self.assertRaisesRegex(ValueError, 'not a memoryview'):
            await self.client.level_one_equity_view(memoryview(b'12'))
        with self.assertRaisesRegex(ValueError, 'int is not iterable'):
            await self.client.level_one_equity_view(12)
        with self.assertRaisesRegex(ValueError, 'got -1'):
            await self.client.level_one_equity_view([-1])
        with self.assertRaisesRegex(ValueError, "got 'BID_PRICE'"):
            await self.client.level_one_equity_view(['BID_PRICE', 1])
        socket.send.assert_not_awaited()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_view_refuses_no_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)
        # A reply is queued, so a VIEW sent without the guard would succeed
        # and fail this test on the missing ValueError, not on the mock.
        socket.recv.side_effect = [json.dumps(
            self.success_response(1, 'LEVELONE_EQUITIES', 'VIEW'))] * 2
        with self.assertRaisesRegex(ValueError, 'at least one field'):
            await self.client.level_one_equity_view([])
        # None means every field to subs and add, and the conversion hands it
        # back unchanged, so it reaches the same refusal rather than a
        # TypeError from iterating it.
        with self.assertRaisesRegex(ValueError, 'at least one field'):
            await self.client.level_one_equity_view(None)
        socket.send.assert_not_awaited()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_view_failure(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)
        response = self.success_response(1, 'LEVELONE_EQUITIES', 'VIEW')
        response['response'][0]['content']['code'] = 25
        socket.recv.side_effect = [json.dumps(response)]
        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_equity_view(
                [StreamClient.LevelOneEquityFields.BID_PRICE])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_view_enforces_the_enum(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)
        # A reply is queued, so a VIEW without the enum check fails on this
        # test's message rather than on the mock.
        socket.recv.side_effect = [json.dumps(
            self.success_response(1, 'LEVELONE_EQUITIES', 'VIEW'))]
        with self.assertRaisesRegex(
                ValueError, 'expected type "LevelOneEquityFields"'):
            await self.client.level_one_equity_view(
                [StreamClient.LevelOneOptionFields.BID_PRICE])

    ###########################################################################
    # Handler edge cases
    #
    # Note: We use CHART_EQUITY as a test case, which leaks the implementation
    # detail that the handler dispatching is implemented by a common component.
    # If this were to ever change, these tests will have to be revisited.

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_messages_received_while_awaiting_response(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('CHART_EQUITY', 'SUBS')

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_EQUITY', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(self.success_response(2, 'CHART_EQUITY', 'ADD'))]

        await self.client.chart_equity_subs(['GOOG,MSFT'])
        await self.client.chart_equity_add(['INTC'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_chart_equity_handler(handler)
        self.client.add_chart_equity_handler(async_handler)
        await self.client.handle_message()
        handler.assert_called_once_with(stream_item['data'][0])
        async_handler.assert_called_once_with(stream_item['data'][0])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_messages_received_while_awaiting_failed_response_bad_code(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('CHART_EQUITY', 'SUBS')

        failed_add_response = self.success_response(2, 'CHART_EQUITY', 'ADD')
        failed_add_response['response'][0]['content']['code'] = 21

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_EQUITY', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(failed_add_response)]

        await self.client.chart_equity_subs(['GOOG,MSFT'])
        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.chart_equity_add(['INTC'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_chart_equity_handler(handler)
        self.client.add_chart_equity_handler(async_handler)
        await self.client.handle_message()
        handler.assert_called_once_with(stream_item['data'][0])
        async_handler.assert_called_once_with(stream_item['data'][0])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_messages_received_while_receiving_unexpected_response(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry('CHART_EQUITY', 'SUBS')

        failed_add_response = self.success_response(999, 'CHART_EQUITY', 'ADD')
        failed_add_response['response'][0]['content']['code'] = 21

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_EQUITY', 'SUBS')),
            json.dumps(stream_item),
            json.dumps(failed_add_response)]

        await self.client.chart_equity_subs(['GOOG,MSFT'])
        with self.assertRaises(schwaby.streaming.UnexpectedResponse):
            await self.client.chart_equity_add(['INTC'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_chart_equity_handler(handler)
        self.client.add_chart_equity_handler(async_handler)
        await self.client.handle_message()
        handler.assert_called_once_with(stream_item['data'][0])
        async_handler.assert_called_once_with(stream_item['data'][0])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_notify_heartbeat_messages_ignored(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_EQUITY', 'SUBS')),
            json.dumps({'notify': [{'heartbeat': '1591499624412'}]})]

        await self.client.chart_equity_subs(['GOOG,MSFT'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_chart_equity_handler(handler)
        self.client.add_chart_equity_handler(async_handler)
        await self.client.handle_message()
        handler.assert_not_called()
        async_handler.assert_not_called()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_handle_message_orphaned_response_is_not_fatal(
            self, ws_connect):
        # A response with no request outstanding reaches handle_message
        # whenever a request was abandoned and the server answered afterwards.
        # It must not end the caller's receive loop, because the answer is
        # frequently a successful one and everything queued behind it would be
        # lost with it.
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_EQUITY', 'SUBS')),
            json.dumps(self.success_response(2, 'CHART_EQUITY', 'SUBS'))]

        await self.client.chart_equity_subs(['GOOG,MSFT'])

        with self.assertLogs(
                'schwaby.streaming', level='INFO') as logged:
            await self.client.handle_message()

        self.assertTrue(
                any('no request outstanding' in line for line in logged.output),
                'expected the orphaned response to be logged, got {}'.format(
                    logged.output))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_stale_response_does_not_poison_the_next_request(
            self, ws_connect):
        # A request which timed out is answered late. That answer must not be
        # handed to whichever request happens to be in flight when it arrives:
        # doing so fails an unrelated request, and then *its* real answer is
        # still queued, so the next request eats that one and fails too. One
        # timeout would wedge every subsequent request for the life of the
        # stream.
        socket = await self.login_and_get_socket(ws_connect)

        self.client._response_timeout = 0.05
        socket.recv.side_effect = asyncio.TimeoutError
        with self.assertRaises(schwaby.streaming.ResponseTimeoutError):
            await self.client.chart_equity_subs(['GOOG'])

        # The venue answers the abandoned request, then answers the next two
        # requests correctly.
        self.client._response_timeout = 5
        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_EQUITY', 'SUBS')),
            json.dumps(self.success_response(2, 'CHART_EQUITY', 'SUBS')),
            json.dumps(self.success_response(3, 'CHART_EQUITY', 'SUBS'))]

        await self.client.chart_equity_subs(['MSFT'])
        await self.client.chart_equity_subs(['AAPL'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_mismatched_service_still_fails_the_request(
            self, ws_connect):
        # A response carrying the right request id but the wrong service is
        # genuine protocol confusion, not lateness, and must still be raised.
        socket = await self.login_and_get_socket(ws_connect)

        wrong = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        socket.recv.side_effect = [json.dumps(wrong)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponse):
            await self.client.chart_equity_subs(['GOOG'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_id_one_past_the_highest_issued_fails_the_request(
            self, ws_connect):
        # The boundary the stale-response guard draws, and the single value
        # that decides it. While request 1 is in flight _request_id is 2, so a
        # frame carrying 2 names an id this client has never issued -- the
        # server and this client disagreeing about what was asked, not a late
        # answer to something abandoned. It must fail the request rather than
        # be set aside as an orphan, which is what `<` rather than `<=`
        # decides. Nothing else in this file uses that one value: loosening
        # the comparison left every other test in the suite passing.
        socket = await self.login_and_get_socket(ws_connect)
        self.client._response_timeout = 0.5

        frames = [json.dumps(self.success_response(2, 'CHART_EQUITY', 'SUBS'))]

        async def recv():
            if frames:
                return frames.pop(0)
            # Nothing further is coming. Parking rather than exhausting the
            # mock matters for the mutation: with `<=` the frame is set aside
            # as an orphan, and an exhausted AsyncMock would then fail this
            # test on StopAsyncIteration -- a red arriving from the harness
            # rather than from the timeout the mutation actually causes.
            await self.park()

        socket.recv.side_effect = recv

        with self.assertRaisesRegex(schwaby.streaming.UnexpectedResponse,
                                    'unexpected requestid: 2'):
            await self.client.chart_equity_subs(['GOOG'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_handle_message_orphaned_failure_is_a_warning(
            self, ws_connect):
        # A late acknowledgement of something that worked is routine. A late
        # rejection is not -- the request was abandoned, so nothing else will
        # ever report that Schwab refused it, and at INFO nobody would see it.
        socket = await self.login_and_get_socket(ws_connect)

        rejected = self.success_response(2, 'CHART_EQUITY', 'SUBS')
        rejected['response'][0]['content']['code'] = 21
        rejected['response'][0]['content']['msg'] = 'Bad command formatting'

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_EQUITY', 'SUBS')),
            json.dumps(rejected)]

        await self.client.chart_equity_subs(['GOOG,MSFT'])

        with self.assertLogs('schwaby.streaming', level='WARNING') as logged:
            await self.client.handle_message()

        self.assertTrue(
                any('no request outstanding' in line for line in logged.output),
                'a rejected orphan should be visible at WARNING, got {}'.format(
                    logged.output))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_handle_message_delivers_data_after_orphaned_response(
            self, ws_connect):
        # And the loop keeps working: a message arriving after the orphan is
        # still delivered.
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = {
            'service': 'CHART_EQUITY',
            'timestamp': 1590597641293,
            'command': 'SUBS',
            'content': [{'key': 'MSFT', '1': 1, '2': 2.0, '3': 3.0,
                         '4': 4.0, '5': 5.0, '6': 6, '7': 7, '8': 8}],
        }

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_EQUITY', 'SUBS')),
            json.dumps(self.success_response(2, 'CHART_EQUITY', 'SUBS')),
            json.dumps({'data': [stream_item]})]

        await self.client.chart_equity_subs(['GOOG,MSFT'])

        handler = Mock()
        self.client.add_chart_equity_handler(handler)

        await self.client.handle_message()      # the orphaned response
        await self.client.handle_message()      # the message behind it

        handler.assert_called_once()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_handle_message_unparsable_message(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_EQUITY', 'SUBS')),
            '{"data":[{"service":"LEVELONE_FUTURES", ' +
            '"timestamp":1590248118165,"command":"SUBS",' +
            '"content":[{"key":"/GOOG","delayed":false,' +
            '"1":�,"2":�,"3":�,"6":"?","7":"?","12":�,"13":�,' +
            '"14":�,"15":"?","16":"Symbol not found","17":"?",' +
            '"18":�,"21":"unavailable","22":"Unknown","24":�,'
            '"28":"D,D","33":�}]}]}']

        await self.client.chart_equity_subs(['GOOG,MSFT'])

        with self.assertRaises(schwaby.streaming.UnparsableMessage):
            await self.client.handle_message()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_handle_message_multiple_handlers(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item_1 = self.streaming_entry('CHART_EQUITY', 'SUBS')

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_EQUITY', 'SUBS')),
            json.dumps(stream_item_1)]

        await self.client.chart_equity_subs(['GOOG,MSFT'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_chart_equity_handler(handler)
        self.client.add_chart_equity_handler(async_handler)

        await self.client.handle_message()
        handler.assert_called_once_with(stream_item_1['data'][0])
        async_handler.assert_called_once_with(stream_item_1['data'][0])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_multiple_data_per_message(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        stream_item = self.streaming_entry(
            'CHART_EQUITY', 'SUBS', [{'msg': 1}])
        stream_item['data'].append(self.streaming_entry(
            'CHART_EQUITY', 'SUBS', [{'msg': 2}])['data'][0])

        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'CHART_EQUITY', 'SUBS')),
            json.dumps(stream_item)]

        await self.client.chart_equity_subs(['GOOG,MSFT'])

        handler = Mock()
        async_handler = AsyncMock()
        self.client.add_chart_equity_handler(handler)
        self.client.add_chart_equity_handler(async_handler)

        await self.client.handle_message()
        handler.assert_has_calls(
            [call(stream_item['data'][0]), call(stream_item['data'][1])])
        async_handler.assert_has_calls(
            [call(stream_item['data'][0]), call(stream_item['data'][1])])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_handle_message_without_login(self, ws_connect):
        with self.assertRaisesRegex(ValueError, '.*Socket not open.*'):
            await self.client.handle_message()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_subscribe_without_login(self, ws_connect):
        with self.assertRaisesRegex(ValueError, '.*Socket not open.*'):
            await self.client.chart_equity_subs(['GOOG,MSFT'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_unsubscribe_without_login(self, ws_connect):
        with self.assertRaisesRegex(ValueError, '.*Socket not open.*'):
            await self.client.chart_equity_unsubs(['GOOG,MSFT'])

    ###########################################################################
    # Private member _service_op
    #
    # Note: https://developer.schwabmeritrade.com/content/streaming-data#_Toc504640564
    # parameters are optional and in the case of UNSUBS commands,
    # fields should not be required since unsubscribing from a service
    # will return no data on the service or symbol

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_service_op_sends_some_fields_with_field_type_and_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_EQUITIES', 'SUBS'))]

        await self.client._service_op(
            symbols=['GOOG', 'MSFT'],
            service='LEVELONE_EQUITIES',
            command='SUBS',
            field_type=StreamClient.LevelOneEquityFields,
            fields=[
            StreamClient.LevelOneEquityFields.DESCRIPTION,
            StreamClient.LevelOneEquityFields.ASK_PRICE
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_EQUITIES',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'GOOG,MSFT',
                'fields': '2,15'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_service_op_sends_no_fields_without_field_type(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_EQUITIES', 'UNSUBS'))]

        await self.client._service_op(
            ['GOOG','MSFT'],
            'LEVELONE_EQUITIES',
            'UNSUBS'
        )
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertFalse('fields' in request['parameters'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_service_op_sends_no_fields_for_sub_without_field_type(self, ws_connect):
        """
        There's no service's sub/add commands without field_type defined but this tests for fields=None behavior if field_type=None
        Warning: Sub commands seems to fail if there's no fields parameters,
        (observed on the older tda-api implementation this was adapted from)

        The streaming client will properly throw UnexpectedResponse
        """
        socket = await self.login_and_get_socket(ws_connect)

        resp = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS', msg="SUBS command failed")
        resp['response'][0]['content']['code'] = 22
        socket.recv.side_effect = [
            json.dumps(resp)
        ]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode) as e:
            await self.client._service_op(
                symbols=['GOOG','MSFT'],
                service='LEVELONE_EQUITIES',
                command='SUBS'
            )
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertFalse('fields' in request['parameters'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_service_op_sends_no_fields_for_sub_with_fields(self, ws_connect):
        """
        There's no service's sub/add commands without field_type defined but this tests for fields=None behavior if field_type=None
        Warning: Sub commands seems to fail if there's no fields parameters,
        (observed on the older tda-api implementation this was adapted from)

        The streaming client will properly throw UnexpectedResponse
        """
        socket = await self.login_and_get_socket(ws_connect)

        resp = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS', msg="SUBS command failed")
        resp['response'][0]['content']['code'] = 22
        socket.recv.side_effect = [
            json.dumps(resp)
        ]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode) as e:
            await self.client._service_op(
                symbols=['GOOG','MSFT'],
                service='LEVELONE_EQUITIES',
                command='SUBS',
                fields=[
                    StreamClient.LevelOneEquityFields.DESCRIPTION,
                    StreamClient.LevelOneEquityFields.ASK_PRICE
                ]
            )
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertFalse('fields' in request['parameters'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_service_op_sends_all_fields_with_field_type(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'CHART_EQUITY', 'SUBS'))]

        await self.client._service_op(
            symbols=['GOOG','MSFT'],
            service='CHART_EQUITY',
            command='SUBS',
            field_type=StreamClient.ChartEquityFields
        )
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'CHART_EQUITY',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'GOOG,MSFT',
                'fields': '0,1,2,3,4,5,6,7,8'
            }
        })

        socket.reset_mock()

        socket.recv.side_effect = [json.dumps(self.success_response(
            2, 'CHART_EQUITY', 'ADD'))]

        await self.client._service_op(
            symbols=['GOOG','MSFT'],
            service='CHART_EQUITY',
            command='ADD',
            field_type=StreamClient.ChartEquityFields
        )
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'CHART_EQUITY',
            'command': 'ADD',
            'requestid': '2',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'GOOG,MSFT',
                'fields': '0,1,2,3,4,5,6,7,8'
            }
        })

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_service_op_sorts_fields(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        socket.recv.side_effect = [json.dumps(self.success_response(
            1, 'LEVELONE_EQUITIES', 'SUBS'))]

        await self.client._service_op(
            symbols=['GOOG', 'MSFT'],
            service='LEVELONE_EQUITIES',
            command='SUBS',
            field_type=StreamClient.LevelOneEquityFields,
            fields=[
            StreamClient.LevelOneEquityFields.ASK_SIZE,  # 5
            StreamClient.LevelOneEquityFields.ASK_PRICE,  # 2
            StreamClient.LevelOneEquityFields.MARGINABLE ,  # 14
            StreamClient.LevelOneEquityFields.REGULAR_MARKET_TRADE_MILLIS,  # 36
            StreamClient.LevelOneEquityFields.BID_PRICE ,  # 1
        ])
        socket.recv.assert_awaited_once()
        request = self.request_from_socket_mock(socket)

        self.assertEqual(request, {
            'service': 'LEVELONE_EQUITIES',
            'command': 'SUBS',
            'requestid': '1',
            "SchwabClientCustomerId": CLIENT_CUSTOMER_ID,
            "SchwabClientCorrelId": CLIENT_CORRELATION_ID,
            'parameters': {
                'keys': 'GOOG,MSFT',
                'fields': '1,2,5,14,36'
            }
        })


    ##########################################################################
    # add_error_handler
    #
    # A stream handler which raises is logged and skipped, which is right --
    # one bad message must not drop the connection. But it leaves the caller
    # with a log record and nothing to react to, so a consumer who cares has to
    # attach a logging.Handler and match on message text. add_error_handler is
    # the programmatic signal.
    #
    # The async site is the one that matters most: it does not look like the
    # other two, so it is the one an implementer forgets.

    async def subscribe_and_deliver(self, ws_connect, handler):
        socket = await self.login_and_get_socket(ws_connect)
        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')),
            json.dumps(self.streaming_entry('LEVELONE_EQUITIES', 'SUBS')),
        ]
        self.client.add_level_one_equity_handler(handler)
        await self.client.level_one_equity_subs(['GOOG'])
        await self.client.handle_message()

    async def until(self, condition, what):
        # Waits on the event loop, not the clock: each pass lets every other
        # ready task take a step, so the bound is the amount of work there is
        # rather than how fast the machine is, and a loaded machine cannot
        # make it fail.
        for _ in range(10000):
            if condition():
                return
            await asyncio.sleep(0)
        self.fail('never happened: %s' % what)

    @staticmethod
    async def park():
        # Never returns. Stands in for a handler slower than anything the test
        # waits for: the test cancels it, or it goes with the loop.
        await asyncio.Event().wait()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_synchronous_handler_failure_is_reported(self, ws_connect):
        boom = ValueError('handler blew up')
        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append((service, exc, msg)))

        await self.subscribe_and_deliver(ws_connect, Mock(side_effect=boom))

        self.assertEqual(1, len(errors))
        service, exc, msg = errors[0]
        self.assertEqual('LEVELONE_EQUITIES', service)
        self.assertIs(boom, exc)
        self.assertEqual('LEVELONE_EQUITIES', msg['service'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_async_handler_failure_is_reported(self, ws_connect):
        # The site a reader of the other two would miss: an async handler's
        # exception never passes through an except block. It surfaces in the
        # task's done callback, at a different logging level, in a different
        # function. A callback wired only where the `except` clauses are would
        # look complete and cover synchronous handlers only -- which is worse
        # than none, since it turns "no signal" into "a signal, and it is
        # quiet".
        boom = ValueError('async handler blew up')
        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append((service, exc, msg)))

        await self.subscribe_and_deliver(
                ws_connect, AsyncMock(side_effect=boom))

        # The failure surfaces when the task completes, not when it is created.
        await asyncio.gather(*self.client._handler_tasks,
                             return_exceptions=True)

        self.assertEqual(1, len(errors), 'async handler failure not reported')
        service, exc, msg = errors[0]
        self.assertIs(boom, exc)

        # Unpacking this as (_, exc, _) would have passed while the service and
        # the message were both None -- which they were, until the callback
        # started carrying them. "Mark this subscription unhealthy" is a stated
        # reason to register a handler, and it needs to know which one.
        self.assertEqual('LEVELONE_EQUITIES', service)
        self.assertEqual('LEVELONE_EQUITIES', msg['service'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_coroutine_error_handler_is_awaited(self, ws_connect):
        # Every other add_*_handler on this class accepts a coroutine function,
        # so writing `async def on_stream_error(...)` is the natural thing to
        # do. Calling it without awaiting drops the coroutine, runs none of the
        # body, and leaves only a RuntimeWarning -- the "signal, and it is
        # quiet" failure this callback exists to prevent, inside the callback.
        errors = []

        async def on_stream_error(service, exc, msg):
            errors.append((service, exc, msg))

        self.client.add_error_handler(on_stream_error)

        boom = ValueError('handler blew up')
        await self.subscribe_and_deliver(ws_connect, Mock(side_effect=boom))

        # No gathering: the report is awaited where it is made, so it has
        # already happened by the time handle_message returns.
        self.assertEqual(1, len(errors), 'coroutine error handler never ran')
        self.assertIs(boom, errors[0][1])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_error_handler_may_close_the_stream(self, ws_connect):
        # Tearing the stream down is a natural reaction to this signal, and it
        # has to be safe however many handlers do it. Reporting inline is what
        # makes that true: there is no set of scheduled reports for close() to
        # wait on, so nothing can wait on itself.
        closed = []

        async def close_on_error(service, exc, msg):
            await self.client.close()
            closed.append(True)

        self.client.add_error_handler(close_on_error)
        await asyncio.wait_for(
                self.subscribe_and_deliver(
                        ws_connect,
                        Mock(side_effect=ValueError('handler blew up'))),
                timeout=5.0)

        self.assertEqual([True], closed)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_async_stream_handler_reports_from_its_own_task(
            self, ws_connect):
        # An async stream handler's failure is reported from inside its own
        # task, before that task completes -- so awaiting the handler task is
        # enough, and there is no second set of scheduled reports that could
        # outlive it.
        delivered = []

        async def on_stream_error(service, exc, msg):
            await asyncio.sleep(0)
            delivered.append(exc)

        async def failing_handler(msg):
            await asyncio.sleep(0)
            raise ValueError('async handler blew up')

        self.client.add_error_handler(on_stream_error)
        await self.subscribe_and_deliver(ws_connect, failing_handler)

        await asyncio.gather(*self.client._handler_tasks,
                             return_exceptions=True)

        self.assertEqual(1, len(delivered),
                         'an async stream handler\'s report was lost')

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_close_failure_report_reaches_a_coroutine_handler(
            self, ws_connect):
        # The close failure is one of the three reported sites, and its report
        # is awaited inside logout's finally, so it has happened by the time
        # logout returns.
        delivered = []

        async def on_stream_error(service, exc, msg):
            await asyncio.sleep(0)
            delivered.append(exc)

        socket = await self.login_and_get_socket(ws_connect)
        self.client.add_error_handler(on_stream_error)

        socket.close.side_effect = ValueError('close blew up')
        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'ADMIN', 'LOGOUT'))]

        await self.client.logout()

        self.assertEqual(1, len(delivered),
                         'the close-failure report was never awaited')

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_coroutine_report_finishes_before_the_call_returns(
            self, ws_connect):
        # Awaiting the report where it is made means there is nothing
        # scheduled to outlive the call, so nothing has to be kept alive at
        # shutdown -- and nothing can deadlock, time out or be cancelled
        # half-delivered keeping it alive.
        order = []

        async def on_stream_error(service, exc, msg):
            await asyncio.sleep(0)
            order.append('reported')

        self.client.add_error_handler(on_stream_error)
        await self.subscribe_and_deliver(
                ws_connect, Mock(side_effect=ValueError('handler blew up')))
        order.append('handle_message returned')

        self.assertEqual(['reported', 'handle_message returned'], order)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_close_does_not_wait_for_in_flight_handlers(
            self, ws_connect):
        # The documented guarantee: close() returns without waiting on stream
        # handlers the caller has in flight.
        running = asyncio.Event()

        async def slow_handler(msg):
            running.set()
            await self.park()

        await self.subscribe_and_deliver(ws_connect, slow_handler)
        await running.wait()

        try:
            await asyncio.wait_for(self.client.close(), timeout=2.0)
        except asyncio.TimeoutError:
            self.fail('close() waited on a stream handler in flight')
        self.assertTrue(any(not task.done()
                            for task in self.client._handler_tasks))

        for task in tuple(self.client._handler_tasks):
            task.cancel()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_logout_can_still_be_cancelled_while_reporting(
            self, ws_connect):
        # The BaseException guard around the close-failure report must not
        # swallow cancellation. Discarding it makes logout() refuse to die, so
        # a supervisor calling cancel() during shutdown finds the task running
        # to completion regardless.
        #
        # The cancel lands once the handler is running, which is the only
        # moment the guard is in play. It used to land after 50ms, which on a
        # loaded machine could be before logout reached the report at all:
        # the cancellation then came from somewhere else and the test passed
        # without exercising the guard.
        reporting = asyncio.Event()

        async def slow_error_handler(service, exc, msg):
            reporting.set()
            await self.park()

        socket = await self.login_and_get_socket(ws_connect)
        self.client.add_error_handler(slow_error_handler)
        socket.close.side_effect = ValueError('close blew up')
        socket.recv.side_effect = [
            json.dumps(self.success_response(1, 'ADMIN', 'LOGOUT'))]

        task = asyncio.ensure_future(self.client.logout())
        await asyncio.wait_for(reporting.wait(), timeout=5)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_slow_error_handler_holds_up_handle_message(
            self, ws_connect):
        # The cost of awaiting the report rather than scheduling it, asserted
        # rather than documented. A caller that awaits inside an error
        # handler stalls the stream rather than getting a scheduled task --
        # and a stall presents as a dead feed, not as a slow handler,
        # so someone will eventually debug it from the wrong end. If this ever
        # stops being true it should be a decision, not a surprise.
        #
        # Asserted as an order rather than a duration: the handler is held
        # until handle_message has had every chance to return without it.
        started, release = asyncio.Event(), asyncio.Event()
        order = []

        async def slow(service, exc, msg):
            started.set()
            await release.wait()
            order.append('handler finished')

        self.client.add_error_handler(slow)

        delivery = asyncio.create_task(self.subscribe_and_deliver(
                ws_connect, Mock(side_effect=ValueError('handler blew up'))))
        delivery.add_done_callback(
                lambda _: order.append('handle_message returned'))
        await asyncio.wait_for(started.wait(), timeout=5)
        release.set()
        await asyncio.wait_for(delivery, timeout=5)
        await self.until(lambda: len(order) == 2, 'the handler finished')

        self.assertEqual(
                ['handler finished', 'handle_message returned'], order,
                'handle_message returned before the error handler finished, so '
                'the report is no longer awaited inline')

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_extra_rejection_in_a_matched_frame_is_logged(
            self, ws_connect):
        # _validate_response reads element 0 only, because that is the answer
        # to the outstanding request. A frame carrying a second response hands
        # it to the waiter unexamined, so a rejection sitting there went
        # unmentioned by anything.
        #
        # Logged where it is found. The report is queued for handle_message,
        # because this runs holding the read lock, on the request path the
        # request lock too, and inside the response deadline -- a user handler
        # called here could turn a subscription that succeeded into a
        # ResponseTimeoutError.
        socket = await self.login_and_get_socket(ws_connect)

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logging.getLogger('schwaby.streaming').addHandler(handler)
        try:
            ok = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
            ok['response'].append({
                'service': 'ACCT_ACTIVITY',
                'command': 'SUBS',
                'requestid': '77',
                'content': {'code': 21, 'msg': 'SUBS command failed'},
            })
            socket.recv.side_effect = [json.dumps(ok)]

            # The subscription itself must still succeed.
            await self.client.level_one_equity_subs(['GOOG'])
        finally:
            logging.getLogger('schwaby.streaming').removeHandler(handler)

        self.assertTrue(
                any('ACCT_ACTIVITY' in r and '21' in r for r in records),
                'the extra rejection was dropped: {}'.format(records))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_extra_response_is_not_reported_while_routing(
            self, ws_connect):
        # The guarantee that keeps a slow handler from failing a successful
        # subscribe and stops a re-subscribing one deadlocking: nothing called
        # from the routing path is user code. The report is queued there and
        # delivered afterwards, so what this pins is the ordering -- the
        # pending request is cleared and both locks are free before any handler
        # runs.
        socket = await self.login_and_get_socket(ws_connect)

        seen = []

        def record(service, exc, msg):
            seen.append((
                self.client._pending_request,
                self.client._read_lock.locked(),
                self.client._request_lock.locked()))

        self.client.add_error_handler(record)

        ok = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        ok['response'].append({
            'service': 'ACCT_ACTIVITY',
            'command': 'SUBS',
            'requestid': '77',
            'content': {'code': 21, 'msg': 'SUBS command failed'},
        })
        socket.recv.side_effect = [json.dumps(ok)]

        await self.client.level_one_equity_subs(['GOOG'])

        self.assertEqual([(None, False, False)], seen)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_slow_handler_cannot_fail_a_successful_subscribe(
            self, ws_connect):
        # The reason the extra-response report is queued rather than delivered
        # where it is found. That runs inside the response deadline and holding
        # the read lock, so calling a user handler there let a slow one cancel
        # the wait -- and the subscription came back as ResponseTimeoutError
        # even though element 0 was code 0 and the future had already been
        # resolved.
        #
        # The handler is slow only where the hazard is: called with a lock
        # held, it never returns, so the response deadline is what ends it.
        # Called where it belongs, with neither lock held, it returns at once.
        # The test's outcome rests on where the handler runs, not on which of
        # two durations is longer.
        socket = await self.login_and_get_socket(ws_connect)
        self.client._response_timeout = 0.2

        async def slow(service, exc, msg):
            if (self.client._read_lock.locked()
                    or self.client._request_lock.locked()):
                await self.park()

        self.client.add_error_handler(slow)

        ok = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        ok['response'].append({
            'service': 'ACCT_ACTIVITY',
            'command': 'SUBS',
            'requestid': '77',
            'content': {'code': 21, 'msg': 'SUBS command failed'},
        })
        socket.recv.side_effect = [json.dumps(ok)]

        # Must not raise: the subscribe succeeded.
        await self.client.level_one_equity_subs(['GOOG'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_extra_rejection_is_reported_by_whoever_read_it(
            self, ws_connect):
        # The delivery half of the contract. _request_lock keeps one request
        # outstanding, so a second response in a frame cannot answer anything
        # being waited on -- it is a late answer to an abandoned request, the
        # same class the orphan path reports. Reporting only the orphan framing
        # would make add_error_handler fire or stay silent for the same event
        # depending on whether Schwab batched it, which the caller cannot see.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append((service, exc, msg)))

        rejected = {
            'service': 'ACCT_ACTIVITY',
            'command': 'SUBS',
            'requestid': '77',
            'content': {'code': 21, 'msg': 'SUBS command failed'},
        }
        ok = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        ok['response'].append(rejected)

        socket.recv.side_effect = [json.dumps(ok)]

        # Delivered by whoever read the frame. Here that is the subscribe: it
        # won the read lock, so handle_message may be parked in recv() with its
        # own drain already behind it, and waiting for the next inbound message
        # would be unbounded on a quiet stream.
        await self.client.level_one_equity_subs(['GOOG'])

        self.assertEqual(1, len(errors))
        service, exception, message = errors[0]
        self.assertEqual('ACCT_ACTIVITY', service)
        self.assertIsInstance(
                exception, schwaby.streaming.UnexpectedResponseCode)
        # Same shape as the orphan path: the exception carries the whole frame,
        # and the rejected element arrives as `message`.
        self.assertEqual(ok, exception.response)
        self.assertEqual(rejected, message)
        self.assertIn('21', str(exception))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_batched_and_standalone_rejections_report_alike(
            self, ws_connect):
        # The contract under test: the framing is the server's choice, so the
        # two must be indistinguishable to a caller.
        rejected = {
            'service': 'ACCT_ACTIVITY',
            'command': 'SUBS',
            'requestid': '77',
            'content': {'code': 21, 'msg': 'SUBS command failed'},
        }

        async def reports_for(frames, subscribe):
            socket = await self.login_and_get_socket(ws_connect)
            errors = []
            self.client.add_error_handler(
                    lambda service, exc, msg: errors.append((service, msg)))
            socket.recv.side_effect = [json.dumps(f) for f in frames]
            if subscribe:
                await self.client.level_one_equity_subs(['GOOG'])
            self.client.add_level_one_equity_handler(lambda msg: None)
            await self.client.handle_message()
            return errors

        data = self.streaming_entry('LEVELONE_EQUITIES', 'SUBS')

        batched_frame = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        batched_frame['response'].append(rejected)
        batched = await reports_for([batched_frame, data], subscribe=True)

        self.setUp()
        standalone = await reports_for([{'response': [rejected]}, data],
                                       subscribe=False)

        self.assertEqual(batched, standalone)
        self.assertEqual([('ACCT_ACTIVITY', rejected)], batched)

    ##########################################################################
    # Documented success codes other than 0

    async def _subscribe_answered_with_code(self, ws_connect, code):
        socket = await self.login_and_get_socket(ws_connect)
        response = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        response['response'][0]['content']['code'] = code
        socket.recv.side_effect = [json.dumps(response)]
        await self.client.level_one_equity_subs(['GOOG'])
        # The answer was read, so the absence of a raise is about the code.
        socket.recv.assert_awaited_once()

    # Schwab documents 26-29 as successes. None has been observed -- every
    # success seen, VIEW included, was 0 -- so the fixtures are synthetic, and
    # there is one test per code because each is a separate claim.
    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_response_code_26_is_a_success(self, ws_connect):
        await self._subscribe_answered_with_code(ws_connect, 26)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_response_code_27_is_a_success(self, ws_connect):
        await self._subscribe_answered_with_code(ws_connect, 27)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_response_code_28_is_a_success(self, ws_connect):
        await self._subscribe_answered_with_code(ws_connect, 28)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_response_code_29_is_a_success(self, ws_connect):
        await self._subscribe_answered_with_code(ws_connect, 29)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_the_failure_codes_beside_them_are_not_successes(
            self, ws_connect):
        # 22-25 are FAILED_COMMAND_* and 30 is STOP_STREAMING, next to the
        # success codes in Schwab's table. `!= 0` could not be edited into
        # accepting one; a tuple can, and would turn a failed subscribe into a
        # silently dead feed.
        for code in (22, 23, 24, 25, 30):
            with self.subTest(code):
                self.setUp()
                with self.assertRaises(
                        schwaby.streaming.UnexpectedResponseCode):
                    await self._subscribe_answered_with_code(ws_connect, code)

    @staticmethod
    def _late_response(requestid, code):
        return {
            'service': 'LEVELONE_EQUITIES',
            'command': 'SUBS',
            'requestid': requestid,
            'content': {'code': code, 'msg': 'late'},
        }

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_late_documented_success_is_not_reported(self, ws_connect):
        # The orphan path must agree with the request path: a late 26 is a
        # success, logged at INFO and not reported. The late 21 beside it is
        # the positive control -- it must be reported, or the empty result for
        # 26 would mean nothing.
        socket = await self.login_and_get_socket(ws_connect)
        socket.recv.side_effect = [
            json.dumps({'response': [self._late_response('9998', 26)]}),
            json.dumps({'response': [self._late_response('9999', 21)]}),
        ]
        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        with self.assertLogs('schwaby.streaming', level='INFO') as logged:
            await self.client.handle_message()
            await self.client.handle_message()

        self.assertEqual(1, len(errors))
        self.assertIn('code 21', str(errors[0]))
        levels = {r.levelname for r in logged.records
                  if 'code 26' in r.getMessage()}
        self.assertEqual({'INFO'}, levels)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_batched_documented_success_is_not_reported(
            self, ws_connect):
        # The same agreement for a late answer batched behind a live one.
        socket = await self.login_and_get_socket(ws_connect)
        frame = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        frame['response'].append(self._late_response('77', 26))
        frame['response'].append(self._late_response('78', 21))
        socket.recv.side_effect = [json.dumps(frame)]
        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        with self.assertLogs('schwaby.streaming', level='INFO') as logged:
            await self.client.level_one_equity_subs(['GOOG'])

        self.assertEqual(1, len(errors))
        self.assertIn('code 21', str(errors[0]))
        levels = {r.levelname for r in logged.records
                  if 'code 26' in r.getMessage()}
        self.assertEqual({'INFO'}, levels)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_unhashable_late_code_is_a_rejection_not_a_crash(
            self, ws_connect):
        # A code is whatever the venue sent. Judged by membership in a set it
        # would raise TypeError and end the receive loop; it must be reported
        # as a rejection instead, as `!= 0` always did.
        socket = await self.login_and_get_socket(ws_connect)
        socket.recv.side_effect = [
            json.dumps({'response': [self._late_response('9999', [26])]})]
        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        await self.client.handle_message()

        self.assertEqual(1, len(errors))
        self.assertIsInstance(
            errors[0], schwaby.streaming.UnexpectedResponseCode)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_extra_success_is_not_reported(self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        ok = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        ok['response'].append({
            'service': 'ACCT_ACTIVITY',
            'command': 'SUBS',
            'requestid': '77',
            'content': {'code': 0, 'msg': 'SUBS command succeeded'},
        })
        socket.recv.side_effect = [
                json.dumps(ok),
                json.dumps(self.streaming_entry('LEVELONE_EQUITIES', 'SUBS')),
        ]

        await self.client.level_one_equity_subs(['GOOG'])
        self.client.add_level_one_equity_handler(lambda msg: None)
        await self.client.handle_message()

        self.assertEqual([], errors)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_queued_report_is_delivered_once(self, ws_connect):
        # Popped before it is awaited, so a handler which re-enters
        # handle_message finds an empty queue rather than the same rejection
        # a second time.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []

        async def reentrant(service, exc, msg):
            errors.append(exc)
            if len(errors) == 1:
                await self.client.handle_message()

        self.client.add_error_handler(reentrant)

        ok = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        ok['response'].append({
            'service': 'ACCT_ACTIVITY',
            'command': 'SUBS',
            'requestid': '77',
            'content': {'code': 21, 'msg': 'SUBS command failed'},
        })
        data = json.dumps(self.streaming_entry('LEVELONE_EQUITIES', 'SUBS'))
        socket.recv.side_effect = [json.dumps(ok), data, data]

        await self.client.level_one_equity_subs(['GOOG'])
        self.client.add_level_one_equity_handler(lambda msg: None)
        await self.client.handle_message()

        self.assertEqual(1, len(errors))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_handler_may_resubscribe_from_a_queued_report(
            self, ws_connect):
        # The hazard that made this a queue rather than an inline call. Where
        # the rejection is found, the read lock is held and on the request path
        # the request lock too, so a handler which subscribed would block on a
        # lock its own caller holds. The drain runs with both released.
        socket = await self.login_and_get_socket(ws_connect)

        resubscribed = []

        async def resubscribe(service, exc, msg):
            await self.client.level_one_equity_subs(['MSFT'])
            resubscribed.append(service)

        self.client.add_error_handler(resubscribe)

        ok = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        ok['response'].append({
            'service': 'ACCT_ACTIVITY',
            'command': 'SUBS',
            'requestid': '77',
            'content': {'code': 21, 'msg': 'SUBS command failed'},
        })
        socket.recv.side_effect = [
                json.dumps(ok),
                # The answer to the handler's own subscribe.
                json.dumps(self.success_response(
                    2, 'LEVELONE_EQUITIES', 'SUBS')),
                json.dumps(self.streaming_entry('LEVELONE_EQUITIES', 'SUBS')),
        ]

        await self.client.level_one_equity_subs(['GOOG'])
        self.client.add_level_one_equity_handler(lambda msg: None)

        # Would hang forever if the drain held either lock.
        await asyncio.wait_for(self.client.handle_message(), timeout=5)

        self.assertEqual(['ACCT_ACTIVITY'], resubscribed)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_handler_may_close_from_a_queued_report(self, ws_connect):
        # Tearing the stream down is a reasonable reaction to a rejection, and
        # it is safe because the report runs with both locks released and
        # nothing waiting on it.
        socket = await self.login_and_get_socket(ws_connect)

        closed = []

        async def close_it(service, exc, msg):
            await self.client.close()
            closed.append(service)

        self.client.add_error_handler(close_it)

        ok = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        ok['response'].append({
            'service': 'ACCT_ACTIVITY',
            'command': 'SUBS',
            'requestid': '77',
            'content': {'code': 21, 'msg': 'SUBS command failed'},
        })
        socket.recv.side_effect = [json.dumps(ok)]

        # The subscribe read the frame, so the subscribe delivers the report.
        # It must succeed even though its own handler closed the stream.
        await asyncio.wait_for(
                self.client.level_one_equity_subs(['GOOG']), timeout=5)

        self.assertEqual(['ACCT_ACTIVITY'], closed)
        socket.close.assert_called_once()

        # And afterwards the client is closed, like any other closed client.
        with self.assertRaises(ValueError):
            await self.client.handle_message()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_handle_message_returns_when_a_report_closes_the_stream(
            self, ws_connect):
        # The other drain site. A handler which closes from here must not make
        # handle_message go on to read a socket its own handler just removed.
        await self.login_and_get_socket(ws_connect)

        async def close_it(service, exc, msg):
            await self.client.close()

        self.client.add_error_handler(close_it)
        self.client._pending_reports.append(
                (schwaby.streaming.UnexpectedResponseCode({}, 'nope'),
                 'ACCT_ACTIVITY', {}))

        # Returns rather than raising "Socket not open" out of the very call
        # the handler was running in.
        await asyncio.wait_for(self.client.handle_message(), timeout=5)

        # The next call is an ordinary use of a closed client, and does raise.
        with self.assertRaises(ValueError):
            await self.client.handle_message()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_close_discards_reports_from_the_dead_session(
            self, ws_connect):
        # A queued report carries a frame from the connection it arrived on.
        # Delivering it after a later login() would report a dead session's
        # rejection against a live one.
        await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))
        self.client._pending_reports.append(
                (schwaby.streaming.UnexpectedResponseCode({}, 'old session'),
                 'ACCT_ACTIVITY', {}))

        await self.client.close()

        # Discarded, not carried into the next session. Each was logged when it
        # was found, so nothing unwritten is lost.
        self.assertEqual(0, len(self.client._pending_reports))
        self.assertEqual([], errors)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_report_is_not_stranded_behind_a_parked_reader(
            self, ws_connect):
        # The reason _request_response drains as well as handle_message. If the
        # request wins the read lock, it reads the frame and queues the report
        # -- but handle_message has already drained for its iteration and is
        # parked in recv(). Draining only there would hold the report until the
        # next inbound message, which on a quiet stream is unbounded.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        ok = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        ok['response'].append({
            'service': 'ACCT_ACTIVITY',
            'command': 'SUBS',
            'requestid': '77',
            'content': {'code': 21, 'msg': 'SUBS command failed'},
        })

        calls = []
        quiet = asyncio.Event()

        async def recv():
            calls.append(1)
            if len(calls) == 1:
                return json.dumps(ok)
            # Nothing more ever arrives.
            await quiet.wait()
            return json.dumps({'data': []})

        socket.recv = recv

        # Hold the lock so the subscribe queues on it first and wins it, then
        # park handle_message behind it.
        await self.client._read_lock.acquire()
        subscribe = asyncio.create_task(
                self.client.level_one_equity_subs(['GOOG']))
        await asyncio.sleep(0)
        handling = asyncio.create_task(self.client.handle_message())
        await asyncio.sleep(0)
        self.client._read_lock.release()

        try:
            await asyncio.wait_for(subscribe, timeout=5)
            await asyncio.sleep(0)

            self.assertEqual(1, len(errors))
            self.assertEqual(0, len(self.client._pending_reports))
            # The point of the test: delivered with the reader still parked.
            self.assertFalse(handling.done())
        finally:
            quiet.set()
            handling.cancel()
            with contextlib.suppress(BaseException):
                await handling

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_rejection_at_element_zero_is_reported_once_nobody_waits(
            self, ws_connect):
        # If the waiter already timed out or was cancelled, its future is
        # resolved but the pending slot is not yet cleared. A frame arriving in
        # that window has no claimant for element 0 either, so it must be
        # treated like the rest -- otherwise a rejection at element 0 is
        # dropped in silence while one at element 1 of the same frame is
        # reported, the inverse of what the contract promises.
        await self.login_and_get_socket(ws_connect)

        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        self.client._pending_request = (
                1, 'LEVELONE_EQUITIES', 'SUBS', future)

        frame = {'response': [
            {'service': 'LEVELONE_EQUITIES', 'command': 'SUBS',
             'requestid': '1',
             'content': {'code': 21, 'msg': 'first was rejected'}},
            {'service': 'ACCT_ACTIVITY', 'command': 'SUBS',
             'requestid': '1',
             'content': {'code': 22, 'msg': 'second was rejected'}},
        ]}
        self.client._overflow_items.appendleft(frame)

        self.assertIs(schwaby.streaming.ROUTED,
                      await self.client._read_and_route())

        queued = [str(exc) for exc, _, _ in self.client._pending_reports]
        self.assertEqual(2, len(queued))
        self.assertIn('21', queued[0])
        self.assertIn('22', queued[1])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_element_zero_stays_with_its_waiter(self, ws_connect):
        # The mirror of the above: while somebody *is* waiting, element 0 is
        # theirs and must not also be queued as an unclaimed rejection.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        rejected = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        rejected['response'][0]['content'] = {
                'code': 21, 'msg': 'subscribe rejected'}
        socket.recv.side_effect = [json.dumps(rejected)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_equity_subs(['GOOG'])

        # Raised to the caller, so it is not an absorbed failure.
        self.assertEqual([], list(self.client._pending_reports))
        self.assertEqual([], errors)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_failed_request_still_reports_what_it_read(
            self, ws_connect):
        # The drain is in a finally. The caller's exception is element 0's
        # rejection and says nothing about element 1, and a failed subscribe is
        # usually followed by tearing the client down rather than reading it
        # again -- so draining only on success dropped the batched rejection
        # exactly when nothing else would report it.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append((service, exc)))

        frame = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        frame['response'][0]['content'] = {
                'code': 21, 'msg': 'this request failed'}
        frame['response'].append({
            'service': 'ACCT_ACTIVITY', 'command': 'SUBS', 'requestid': '77',
            'content': {'code': 21, 'msg': 'the abandoned one'}})
        socket.recv.side_effect = [json.dumps(frame)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponseCode):
            await self.client.level_one_equity_subs(['GOOG'])

        self.assertEqual(1, len(errors))
        service, exception = errors[0]
        self.assertEqual('ACCT_ACTIVITY', service)
        self.assertIn('the abandoned one', str(exception))
        self.assertEqual(0, len(self.client._pending_reports))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_discards_reports_from_the_previous_session(
            self, ws_connect):
        # A caller reconnecting after a ConnectionClosed may call login() again
        # without close() first. The cross-session guarantee has to hold
        # whichever teardown they used.
        await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))
        self.client._pending_reports.append(
                (schwaby.streaming.UnexpectedResponseCode({}, 'old session'),
                 'ACCT_ACTIVITY', {}))

        # The socket swap itself, which is what a reconnect does. Calling
        # login() a second time would need a fresh request-id sequence and is
        # beside the point: the guarantee belongs to the connection being
        # replaced, not to the login handshake.
        ws_connect.return_value = AsyncMock()
        await self.client._init_from_preferences(
                account_preferences(), {})

        self.assertEqual(0, len(self.client._pending_reports))
        self.assertEqual([], errors)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_close_discards_unhandled_frames_from_the_dead_session(
            self, ws_connect):
        # _overflow_items holds frames read but not yet handled -- including
        # the late rejections the orphan path reports and data frames handlers
        # would be given. Clearing only _pending_reports would leave the
        # standalone framing leaking across sessions while the batched one did
        # not, which is the asymmetry clearing both prevents.
        await self.login_and_get_socket(ws_connect)

        self.client._overflow_items.appendleft(
                {'response': [{'service': 'ACCT_ACTIVITY', 'command': 'SUBS',
                               'requestid': '77',
                               'content': {'code': 21, 'msg': 'dead session'}}]})
        self.client._overflow_items.appendleft(
                self.streaming_entry('LEVELONE_EQUITIES', 'SUBS'))

        await self.client.close()

        self.assertEqual(0, len(self.client._overflow_items))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_login_discards_unhandled_frames_from_the_dead_session(
            self, ws_connect):
        # Same guarantee for a caller who reconnects with login() rather than
        # closing first.
        await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))
        handled = []
        self.client.add_level_one_equity_handler(handled.append)

        self.client._overflow_items.appendleft(
                {'response': [{'service': 'ACCT_ACTIVITY', 'command': 'SUBS',
                               'requestid': '77',
                               'content': {'code': 21, 'msg': 'dead session'}}]})

        ws_connect.return_value = AsyncMock()
        await self.client._init_from_preferences(
                account_preferences(), {})

        self.assertEqual(0, len(self.client._overflow_items))
        self.assertEqual([], errors)
        self.assertEqual([], handled)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_malformed_extra_cannot_fail_a_successful_subscribe(
            self, ws_connect):
        # The parsing counterpart of
        # test_a_slow_handler_cannot_fail_a_successful_subscribe. Nothing on
        # the routing path may raise once the waiter's future is resolved.
        socket = await self.login_and_get_socket(ws_connect)

        # Both shapes, because they are stopped by different guards. A bare
        # string is caught by the element check; a mapping whose `content` is
        # null gets past that one and is caught by the content check. Testing
        # only the first left the second unexercised, and a mutation removing
        # it kept this test green.
        payloads = ('not a dict at all',
                    {'service': 'ACCT_ACTIVITY', 'command': 'SUBS',
                     'requestid': '77', 'content': None},
                    {'service': 'ACCT_ACTIVITY', 'command': 'SUBS',
                     'requestid': '77', 'content': 'not a mapping'})

        for i, payload in enumerate(payloads):
            with self.subTest(payload=payload):
                ok = self.success_response(
                        self.client._request_id, 'LEVELONE_EQUITIES', 'SUBS')
                ok['response'].append(payload)
                socket.recv.side_effect = [json.dumps(ok)]

                # Must not raise: the subscribe succeeded.
                await self.client.level_one_equity_subs(['GOOG'])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_response_field_of_the_wrong_shape_is_ignored(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        for broken in ('not a list', {'nope': 1}, None):
            with self.subTest(broken=broken):
                self.client._pending_reports.clear()
                self.client._absorbed = 0
                # Must not raise, and must be absorbed rather than silently
                # dropped -- a response field this client cannot read is a
                # message it cannot use.
                self.client._log_extra_responses({'response': broken}, 0)
                queued = list(self.client._pending_reports)
                self.assertEqual(1, len(queued))
                self.assertIsInstance(
                        queued[0][0], schwaby.streaming.UnusableMessage)

        # A tuple is a list as far as this is concerned: set_json_decoder may
        # return one, and rejections must still be reported for such a caller.
        self.client._pending_reports.clear()
        self.client._absorbed = 0
        self.client._log_extra_responses({'response': (
            {'service': 'Y', 'command': 'SUBS',
             'content': {'code': 21, 'msg': 'from a tuple'}},
        )}, 0)
        self.assertEqual(1, len(self.client._pending_reports))
        self.assertIn('from a tuple',
                      str(self.client._pending_reports[0][0]))

        # A list whose elements are the wrong shape is absorbed per element,
        # and the well-formed ones beside them still report as rejections.
        self.client._pending_reports.clear()
        self.client._absorbed = 0
        self.client._log_extra_responses({'response': [
            'a string, not a dict',
            {'service': 'Y', 'command': 'SUBS',
             'content': {'code': 21, 'msg': 'this one is fine'}},
        ]}, 0)
        kinds = [type(exc) for exc, _, _ in self.client._pending_reports]
        self.assertEqual([schwaby.streaming.UnusableMessage,
                          schwaby.streaming.UnexpectedResponseCode], kinds)
        self.assertIn('this one is fine',
                      str(self.client._pending_reports[1][0]))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_handler_cannot_replace_the_requests_own_exception(
            self, ws_connect):
        # The drain is in a finally, so a handler raising a BaseException there
        # would surface instead of the rejection saying Schwab refused the
        # request -- and the caller would never learn why their subscribe
        # failed. Same guard, same reason, as the one in logout().
        socket = await self.login_and_get_socket(ws_connect)

        def explode(service, exc, msg):
            raise SystemExit('handler exit')

        self.client.add_error_handler(explode)

        frame = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        frame['response'][0]['content'] = {
                'code': 21, 'msg': 'this request failed'}
        frame['response'].append({
            'service': 'ACCT_ACTIVITY', 'command': 'SUBS', 'requestid': '77',
            'content': {'code': 22, 'msg': 'the abandoned one'}})
        socket.recv.side_effect = [json.dumps(frame)]

        with self.assertRaises(
                schwaby.streaming.UnexpectedResponseCode) as cm:
            await self.client.level_one_equity_subs(['GOOG'])

        # The useful error, not the handler's.
        self.assertIn('this request failed', str(cm.exception))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_slow_handler_does_not_delay_a_cancellation(
            self, ws_connect):
        # Running a handler to completion while unwinding a cancel makes
        # close(), a wait_for, or a TaskGroup shutdown block on user code --
        # the shape that deadlocks shutdown.
        socket = await self.login_and_get_socket(ws_connect)

        async def slow(service, exc, msg):
            await self.park()

        self.client.add_error_handler(slow)

        started = asyncio.Event()
        release = asyncio.Event()

        ok = self.success_response(1, 'LEVELONE_EQUITIES', 'SUBS')
        ok['response'].append({
            'service': 'ACCT_ACTIVITY', 'command': 'SUBS', 'requestid': '77',
            'content': {'code': 21, 'msg': 'SUBS command failed'}})

        calls = []

        async def recv():
            calls.append(1)
            if len(calls) == 1:
                # Queue the report, then park so the cancel lands with it
                # pending.
                self.client._log_extra_responses(ok, 1)
                started.set()
                await release.wait()
            return json.dumps(ok)

        socket.recv = recv

        task = asyncio.create_task(self.client.level_one_equity_subs(['GOOG']))
        await asyncio.wait_for(started.wait(), timeout=5)

        task.cancel()
        # The handler never returns, so a finally that awaited it would never
        # finish. The bound only names that; nothing here waits on a duration.
        done, _ = await asyncio.wait({task}, timeout=5)
        self.assertIn(task, done,
                      'unwinding the cancel waited on the error handler')
        self.assertTrue(task.cancelled())
        release.set()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_logging_in_again_closes_the_socket_it_replaces(
            self, ws_connect):
        # A caller who logs in again on a healthy client -- a re-auth, a
        # preferences refresh -- would otherwise drop a live websocket and its
        # reader with nothing closing it.
        first = await self.login_and_get_socket(ws_connect)

        ws_connect.return_value = AsyncMock()
        await self.client._init_from_preferences(account_preferences(), {})

        first.close.assert_called_once()
        self.assertIsNot(first, self.client._socket)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_failed_close_does_not_stop_logging_in_again(
            self, ws_connect):
        first = await self.login_and_get_socket(ws_connect)
        first.close.side_effect = Exception('socket already gone')

        ws_connect.return_value = AsyncMock()
        await self.client._init_from_preferences(account_preferences(), {})

        # The login is what the caller asked for.
        self.assertIsNotNone(self.client._socket)
        self.assertIsNot(first, self.client._socket)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_both_framings_survive_a_malformed_element(self, ws_connect):
        # The contract is that a late rejection behaves the same whether Schwab
        # sends it alone or batched. Two loops parsing the same JSON drift: one
        # was hardened and the other was not, so an identical payload was
        # skipped with a warning in one framing and ended the caller's receive
        # loop with an AttributeError in the other.
        #
        # The element chosen matters. An earlier version of this test used
        # {'content': None}, which does not raise -- `or {}` turns it into a
        # code of None and it is skipped as neither a rejection nor a success.
        # So the except clause it meant to exercise never ran, and reporting
        # malformed elements instead of skipping them left the test green. A
        # bare string raises on .get, which is the path that matters.
        raises = 'not a dict at all'
        rejection = {'service': 'ACCT_ACTIVITY', 'command': 'SUBS',
                     'requestid': '99',
                     'content': {'code': 21, 'msg': 'a real rejection'}}

        socket = await self.login_and_get_socket(ws_connect)

        def rejections(pairs):
            return [(service, message) for exc, service, message in pairs
                    if isinstance(exc, schwaby.streaming.UnexpectedResponseCode)]

        def absorbed(pairs):
            # exc.message, not the handler's `message`: the latter is now the
            # containing frame, which differs between the two framings by
            # construction.
            return [exc.message for exc, _, _ in pairs
                    if isinstance(exc, schwaby.streaming.UnusableMessage)]

        # Standalone framing, through handle_message's orphan path. Collected
        # from the queue rather than from the handler, so both sides are read
        # the same way -- the orphan path reports a rejection inline and queues
        # an absorbed element, and comparing one mechanism against the other
        # would compare their timing rather than their content.
        seen = []
        self.client.add_error_handler(
                lambda service, exc, msg: seen.append((exc, service, msg)))
        socket.recv.side_effect = [
                json.dumps({'response': [raises, rejection]})]
        await self.client.handle_message()
        standalone = seen + list(self.client._pending_reports)

        # Batched framing, through the routing path. Element 0 stands in for
        # the answer a waiter took, so the same two elements follow it.
        self.client._pending_reports.clear()
        self.client._log_extra_responses(
                {'response': [{}, raises, rejection]}, 1)
        batched = list(self.client._pending_reports)

        # The positive control: the good element reports in both framings, so
        # neither list is empty for want of the input ever arriving.
        self.assertEqual([('ACCT_ACTIVITY', rejection)], rejections(standalone))
        self.assertEqual(rejections(standalone), rejections(batched))

        # And the element which could not be read is absorbed on both, rather
        # than one framing reporting it and the other dropping it.
        self.assertEqual([raises], absorbed(standalone))
        self.assertEqual(absorbed(standalone), absorbed(batched))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_malformed_orphan_does_not_end_the_receive_loop(
            self, ws_connect):
        # One bad element among many must not cost the caller the good ones.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append((service, msg)))

        good = {'service': 'ACCT_ACTIVITY', 'command': 'SUBS',
                'requestid': '99',
                'content': {'code': 21, 'msg': 'a real rejection'}}
        socket.recv.side_effect = [json.dumps({'response': [
            'not a dict at all',
            {'service': 'X', 'command': 'SUBS', 'content': None},
            good,
        ]})]

        await self.client.handle_message()

        self.assertEqual([('ACCT_ACTIVITY', good)], errors)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_response_frame_of_the_wrong_shape_ends_nothing(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)
        socket.recv.side_effect = [json.dumps({'response': {'not': 'a list'}})]

        # Iterating a dict yields its keys, and .get on a str raises.
        await self.client.handle_message()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_malformed_frame_does_not_fail_an_in_flight_request(
            self, ws_connect):
        # The version of the above that matters, and the one the first attempt
        # missed. _read_and_route reads element 0's requestid before anything
        # reaches the parser, so with a request outstanding a malformed frame
        # raised there instead -- taking out the in-flight request through
        # _fail_pending_request and ending the receive loop, while the same
        # frame with nothing pending was logged and harmless.
        socket = await self.login_and_get_socket(ws_connect)

        for broken in ({'not': 'a list'}, [], [{'no': 'requestid'}],
                       ['not a dict'], [{'requestid': 'not a number'}]):
            with self.subTest(broken=broken):
                socket.recv.side_effect = [
                        json.dumps({'response': broken}),
                        json.dumps(self.success_response(
                            self.client._request_id, 'LEVELONE_EQUITIES',
                            'SUBS')),
                ]

                # The malformed frame is set aside, the real answer arrives,
                # and the subscribe succeeds.
                await asyncio.wait_for(
                        self.client.level_one_equity_subs(['GOOG']),
                        timeout=5)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_frame_read_during_the_socket_swap_is_discarded(
            self, ws_connect):
        # The clears run after the new socket is in place. Clearing first left
        # a hole: a concurrent reader on the old socket appends to
        # _overflow_items while this coroutine is suspended in close() or
        # connect(), and that frame survived into the new session.
        first = await self.login_and_get_socket(ws_connect)

        handled = []
        self.client.add_level_one_equity_handler(handled.append)

        stale = self.streaming_entry('LEVELONE_EQUITIES', 'SUBS')

        async def close_and_race():
            # Stands in for a reader which got a frame off the old socket
            # while this login was suspended mid-swap.
            self.client._overflow_items.appendleft(stale)

        first.close = close_and_race
        ws_connect.return_value = AsyncMock()

        await self.client._init_from_preferences(account_preferences(), {})

        self.assertEqual(0, len(self.client._overflow_items))
        self.assertEqual([], handled)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_malformed_answer_fails_only_its_own_request(
            self, ws_connect):
        # The sibling of the requestid guard. _validate_response read four more
        # fields straight after it, unguarded, and a KeyError there reached
        # _fail_pending_request -- which sets it on the future AND re-raises,
        # so one unreadable field failed the request with a bare KeyError and
        # ended the caller's receive loop too.
        socket = await self.login_and_get_socket(ws_connect)

        for missing in ('service', 'command', 'content'):
            with self.subTest(missing=missing):
                frame = self.success_response(
                        self.client._request_id, 'LEVELONE_EQUITIES', 'SUBS')
                del frame['response'][0][missing]
                socket.recv.side_effect = [json.dumps(frame)]

                with self.assertRaises(
                        schwaby.streaming.UnexpectedResponse) as cm:
                    await asyncio.wait_for(
                            self.client.level_one_equity_subs(['GOOG']),
                            timeout=5)

                # Says what was wrong, rather than surfacing a bare KeyError.
                self.assertIn('malformed response frame', str(cm.exception))

        # And the stream is still usable afterwards: the receive loop was not
        # taken down with the request.
        socket.recv.side_effect = [
                json.dumps(self.streaming_entry('LEVELONE_EQUITIES', 'SUBS')),
        ]
        handled = []
        self.client.add_level_one_equity_handler(handled.append)
        await self.client.handle_message()
        self.assertEqual(1, len(handled))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_rejection_without_a_message_still_reports_its_code(
            self, ws_connect):
        # The code is the part the caller can act on. Treating a missing `msg`
        # as an unreadable frame would lose it.
        socket = await self.login_and_get_socket(ws_connect)

        frame = self.success_response(
                self.client._request_id, 'LEVELONE_EQUITIES', 'SUBS')
        frame['response'][0]['content'] = {'code': 21}
        socket.recv.side_effect = [json.dumps(frame)]

        with self.assertRaises(
                schwaby.streaming.UnexpectedResponseCode) as cm:
            await asyncio.wait_for(
                    self.client.level_one_equity_subs(['GOOG']), timeout=5)

        self.assertIn('21', str(cm.exception))
        self.assertNotIn('malformed', str(cm.exception))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_malformed_data_element_does_not_end_the_receive_loop(
            self, ws_connect):
        # d.get('service') is evaluated at the call site, outside the try in
        # _dispatch_to_handlers, so a non-dict element ended the receive loop
        # with an AttributeError -- on the highest-volume channel. The response
        # path was hardened first and this one, three lines below it, was not.
        socket = await self.login_and_get_socket(ws_connect)

        handled = []
        self.client.add_level_one_equity_handler(handled.append)

        good = {'service': 'LEVELONE_EQUITIES', 'command': 'SUBS',
                'timestamp': 1590116673258}
        socket.recv.side_effect = [json.dumps({'data': ['not a dict', good]})]

        await self.client.handle_message()

        # The positive control: the good element beside it still dispatches,
        # so this cannot be green for want of the input arriving.
        self.assertEqual([good], handled)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_channel_of_the_wrong_shape_does_not_end_the_loop(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)

        handled = []
        self.client.add_level_one_equity_handler(handled.append)

        for broken in ({'data': 'oops'}, {'notify': [3]},
                       {'notify': 'nope'}, {'data': None}):
            with self.subTest(broken=broken):
                socket.recv.side_effect = [json.dumps(broken)]
                await self.client.handle_message()

        self.assertEqual([], handled)

        # Positive control in the same test: a well-formed frame afterwards is
        # still delivered, so the assertions above are not green because
        # nothing ever reached the dispatch.
        good = {'service': 'LEVELONE_EQUITIES', 'command': 'SUBS',
                'timestamp': 1590116673258}
        socket.recv.side_effect = [json.dumps({'data': [good]})]
        await self.client.handle_message()
        self.assertEqual([good], handled)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_frame_which_is_not_an_object_is_ignored(self, ws_connect):
        # A regression the channel hardening introduced and this pins shut.
        # `'data' in msg` tolerates any container; `msg.get('data')` does not,
        # so a top-level JSON array or string went from being ignored to
        # raising AttributeError out of the receive loop.
        socket = await self.login_and_get_socket(ws_connect)

        handled = []
        self.client.add_level_one_equity_handler(handled.append)

        for frame in ([], ['not', 'a', 'dict'], 'hello', 3, ['response']):
            with self.subTest(frame=frame):
                socket.recv.side_effect = [json.dumps(frame)]
                await self.client.handle_message()

        self.assertEqual([], handled)

        # Positive control: a well-formed frame afterwards still dispatches,
        # so the assertions above are not green for want of anything arriving.
        good = {'service': 'LEVELONE_EQUITIES', 'command': 'SUBS',
                'timestamp': 1590116673258}
        socket.recv.side_effect = [json.dumps({'data': [good]})]
        await self.client.handle_message()
        self.assertEqual([good], handled)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_service_which_is_not_a_name_is_ignored(self, ws_connect):
        # The handler lookup is evaluated in the `for` header, outside the
        # per-handler try, so an unhashable service raised TypeError out of the
        # receive loop. The element type was guarded; the field inside it was
        # not.
        socket = await self.login_and_get_socket(ws_connect)

        handled = []
        self.client.add_level_one_equity_handler(handled.append)

        for service in (['LEVELONE_EQUITIES'], {'a': 1}, {'x'}):
            with self.subTest(service=service):
                socket.recv.side_effect = [json.dumps(
                        {'data': [{'service': list(service)
                                   if isinstance(service, set) else service,
                                   'command': 'SUBS'}]})]
                await self.client.handle_message()

        self.assertEqual([], handled)

        good = {'service': 'LEVELONE_EQUITIES', 'command': 'SUBS',
                'timestamp': 1590116673258}
        socket.recv.side_effect = [json.dumps({'data': [good]})]
        await self.client.handle_message()
        self.assertEqual([good], handled)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_null_frame_is_logged_like_any_other(self, ws_connect):
        # A top-level JSON null was indistinguishable from the sentinel meaning
        # "this was routed to its waiter", so it was dropped without the
        # warning every other unusable frame gets.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        good = {'service': 'LEVELONE_EQUITIES', 'command': 'SUBS',
                'timestamp': 1590116673258}
        socket.recv.side_effect = [json.dumps(None), json.dumps({'data': [good]})]
        await self.client.handle_message()
        # Drained at the top of the loop, so it goes out on the next call --
        # the same as any other queued report.
        await self.client.handle_message()

        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], schwaby.streaming.UnusableMessage)
        self.assertIsNone(errors[0].message)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_absorbed_message_reaches_the_error_handler(
            self, ws_connect):
        # add_error_handler exists so an absorbed failure is not visible only
        # in a log. These are absorbed failures.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append((exc, msg)))

        for frame in ({'data': ['not a dict']},
                      {'data': 'oops'},
                      {'data': [{'service': ['a list'], 'command': 'SUBS'}]},
                      'hello'):
            with self.subTest(frame=frame):
                self.client._absorbed = 0
                socket.recv.side_effect = [json.dumps(frame)]
                await self.client.handle_message()

        # One more pass to drain the last one.
        good = {'service': 'LEVELONE_EQUITIES', 'command': 'SUBS',
                'timestamp': 1590116673258}
        socket.recv.side_effect = [json.dumps({'data': [good]})]
        await self.client.handle_message()

        self.assertEqual(4, len(errors))
        for exc, message in errors:
            self.assertIsInstance(exc, schwaby.streaming.UnusableMessage)
            # The containing frame reaches the handler, so a null channel is
            # not reported as (service=None, message=None) -- the signature the
            # logout-close failure already uses, which a consumer branching on
            # "both None" would file as a teardown problem.
            self.assertIsNotNone(message)

    @no_duplicates
    def test_an_absorbed_cause_whose_type_cannot_be_named_is_reported(self):
        # The line names the cause's type, and `type(x).__name__` consults the
        # metaclass first. Raising there escaped `handle_message`, ending the
        # receive loop with nothing logged and nothing reported.
        class Meta(type):
            @property
            def __name__(cls):
                raise RuntimeError('boom')

        class Boom(Exception, metaclass=Meta):
            pass

        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            self.client._absorb('a thing', 'offender', cause=Boom('x'))
        # The real name, read through type's own descriptor -- not the
        # fallback, which would say `object`.
        self.assertIn('Cause: Boom: x', '\n'.join(got.output))

    @no_duplicates
    def test_absorbed_warnings_do_not_flood(self):
        # Before this, a systematically malformed high-volume channel logged
        # one line per element per tick, forever -- a log-volume incident on
        # top of the data outage.
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logging.getLogger('schwaby.streaming').addHandler(handler)
        try:
            for _ in range(5000):
                self.client._absorb('a thing', 'offender')
        finally:
            logging.getLogger('schwaby.streaming').removeHandler(handler)

        # First three, then powers of ten: 1, 2, 3, 10, 100, 1000 -- not 5000
        # lines, and not silence between 3 and 1000 either.
        self.assertEqual(6, len(records))
        self.assertIn('1000 of these', records[-1])
        self.assertIn('10 of these', records[3])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_custom_decoder_returning_a_mapping_still_works(
            self, ws_connect):
        # set_json_decoder is a public hook which promises only "the decoded
        # JSON". A decoder returning a Mapping which is not a dict, or tuples
        # for arrays, worked before the type guards were added and must still.
        import collections

        socket = await self.login_and_get_socket(ws_connect)

        good = {'service': 'LEVELONE_EQUITIES', 'command': 'SUBS',
                'timestamp': 1590116673258}

        from schwaby.contrib.util import StreamJsonDecoder

        class MappingDecoder(StreamJsonDecoder):
            def decode_json_string(self, raw):
                loaded = json.loads(raw)
                return collections.ChainMap(
                        {'data': (collections.ChainMap(good),)}, loaded)

        self.client.set_json_decoder(MappingDecoder())

        handled = []
        self.client.add_level_one_equity_handler(handled.append)
        socket.recv.side_effect = ['{}']

        await self.client.handle_message()

        self.assertEqual(1, len(handled))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_mismatched_request_id_is_named_as_such(self, ws_connect):
        # Reading all five fields first meant a frame with an id this client
        # never issued AND a missing field was reported as "malformed response
        # frame: KeyError: 'service'". The id mismatch is the more diagnostic
        # fact and was hidden behind whichever field happened to be absent.
        socket = await self.login_and_get_socket(ws_connect)

        frame = self.success_response(
                self.client._request_id, 'LEVELONE_EQUITIES', 'SUBS')
        frame['response'][0]['requestid'] = '999'
        del frame['response'][0]['service']
        socket.recv.side_effect = [json.dumps(frame)]

        with self.assertRaises(schwaby.streaming.UnexpectedResponse) as cm:
            await asyncio.wait_for(
                    self.client.level_one_equity_subs(['GOOG']), timeout=5)

        self.assertIn('unexpected requestid: 999', str(cm.exception))
        self.assertNotIn('malformed', str(cm.exception))

    @no_duplicates
    def test_a_flood_of_one_kind_does_not_silence_another(self):
        # One shared counter meant a framing change on a few hundred symbols
        # drove the count past a thousand within two ticks, after which a
        # different fault was neither logged nor reported -- so an operator
        # investigating the first outage saw no trace of the second.
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logging.getLogger('schwaby.streaming').addHandler(handler)
        try:
            for i in range(5000):
                self.client._absorb('a bad element', i)

            before = len(records)
            self.client._absorb('a service which is not a name', ['a list'])
        finally:
            logging.getLogger('schwaby.streaming').removeHandler(handler)

        # The second kind is on its first occurrence and must be heard.
        self.assertEqual(before + 1, len(records))
        self.assertIn('a service which is not a name', records[-1])
        # And the total still tells the truth about scale.
        self.assertIn('5001 in all', records[-1])

    @no_duplicates
    def test_set_json_decoder_does_not_need_contrib_imported(self):
        # schwaby/__init__.py does not import contrib, so looking the base class
        # up as schwaby.contrib.util.StreamJsonDecoder raised AttributeError for
        # anyone who subclassed it where it is defined. Every other test here
        # imports contrib.util first, which is exactly what made the old code
        # work -- so this one must not.
        for name in list(sys.modules):
            if name.startswith('schwaby.contrib'):
                del sys.modules[name]

        blocked = 'schwaby.contrib'

        class Blocker:
            def find_spec(self, fullname, path=None, target=None):
                if fullname.startswith(blocked):
                    raise ModuleNotFoundError(
                            'No module named %r' % fullname, name=fullname)
                return None

        blocker = Blocker()
        sys.meta_path.insert(0, blocker)
        try:
            class Decoder(streaming.StreamJsonDecoder):
                def decode_json_string(self, raw):
                    return json.loads(raw)

            # Must not raise AttributeError reaching for schwaby.contrib.
            self.client.set_json_decoder(Decoder())
        finally:
            sys.meta_path.remove(blocker)

        self.assertIsInstance(self.client.json_decoder, Decoder)

    @no_duplicates
    def test_set_json_decoder_refuses_something_that_is_not_one(self):
        # The refusal itself, which nothing exercised -- only the accepting
        # path had a test. The check exists because the alternative is an
        # AttributeError from deep inside the read loop the first time a frame
        # arrives, long after the mistake was made.
        # Install a real one first, so the assertion after the loop is about
        # the refusal leaving it alone rather than about the default.
        class Decoder(streaming.StreamJsonDecoder):
            def decode_json_string(self, raw):
                return json.loads(raw)

        good = Decoder()
        self.client.set_json_decoder(good)

        for not_a_decoder in (None, object(), json, lambda raw: raw,
                              json.JSONDecoder()):
            with self.subTest(value=not_a_decoder):
                with self.assertRaises(ValueError) as ctx:
                    self.client.set_json_decoder(not_a_decoder)
                self.assertIn('StreamJsonDecoder', str(ctx.exception))

        # A refused decoder must not have replaced the working one on its way
        # out. `assertIsNot(None, ...)` would pass here whatever happened,
        # since the attribute is never None.
        self.assertIs(good, self.client.json_decoder)

    @no_duplicates
    async def test_receiving_before_login_says_so(self):
        # `self._socket is None` until login(). Without this the first read
        # raises AttributeError on None, which names neither the socket nor
        # login; the message is the whole point of the check.
        client = StreamClient(client=MagicMock())
        self.assertIsNone(client._socket)

        with self.assertRaises(ValueError) as ctx:
            await client._receive_from_socket()
        self.assertIn('login()', str(ctx.exception))

    @no_duplicates
    def test_a_debug_line_survives_an_unserialisable_frame(self):
        # Cosmetic rather than load-bearing -- logging swallows a formatting
        # failure -- but without it the content of every debug line is replaced
        # by a traceback, for exactly the people who customised the decoder.
        import collections

        rendered = self.client._pretty(collections.ChainMap({'a': 1}))

        self.assertIn('a', rendered)
        self.assertIn('1', rendered)

    @no_duplicates
    def test_a_debug_line_survives_a_type_whose_name_raises(self):
        class Meta(type):
            @property
            def __name__(cls):
                raise RuntimeError('metaclass raised')

        class Hostile(dict, metaclass=Meta):
            def __repr__(self):
                raise RuntimeError('repr raised')

        # json.dumps refuses the tuple key and repr raises, so the fallback's
        # own fallback names the type, which the metaclass must not break.
        # Caught here: pytest cannot report an error raised past a value
        # whose type name raises, and stops with an internal error instead.
        try:
            rendered = self.client._pretty(Hostile({(1, 2): 3}))
        except Exception as e:
            rendered = 'raised ' + e.__class__.__qualname__
        self.assertEqual('<Hostile that cannot be formatted>', rendered)

    @no_duplicates
    def test_a_debug_line_survives_a_value_json_cannot_name(self):
        # json.dumps names a value it cannot serialise through __class__,
        # which can raise something other than TypeError.
        class Hostile:
            @property
            def __class__(self):
                raise RuntimeError('class raised')

        rendered = self.client._pretty({'field': Hostile()})
        self.assertIn('field', rendered)

    @no_duplicates
    def test_a_mapping_channel_is_absorbed_once_not_once_per_key(self):
        # A mapping is iterable and yields its keys, so without excluding it
        # from _is_sequence a {'data': {...}} frame absorbs one report per key
        # rather than one for the channel.
        self.client._pending_reports.clear()

        list(self.client._iter_channel(
                {'data': {'a': 1, 'b': 2, 'c': 3}}, 'data'))

        self.assertEqual(1, len(self.client._pending_reports))
        exc, service, message = self.client._pending_reports[0]
        # The exception carries the offending value; the handler is given the
        # containing frame, so that (service, message) is not (None, None) and
        # cannot be mistaken for the logout-close report.
        self.assertEqual({'a': 1, 'b': 2, 'c': 3}, exc.message)
        self.assertEqual({'data': {'a': 1, 'b': 2, 'c': 3}}, message)

    @no_duplicates
    def test_responses_may_be_any_sequence(self):
        # A tuple is the realistic custom-decoder case and must work. A
        # generator is not: routing indexes element 0, validation reads it
        # again and handlers are given the frame afterwards, so a single-pass
        # iterable cannot serve however tolerant the iteration is -- it was
        # accepted for one release and made every subscribe time out.
        self.client._pending_reports.clear()
        self.client._log_extra_responses({'response': (
            {'service': 'X', 'command': 'SUBS', 'content': {'code': 0}},
            {'service': 'Y', 'command': 'SUBS',
             'content': {'code': 21, 'msg': 'from a tuple'}},
        )}, 1)

        self.assertEqual(1, len(self.client._pending_reports))
        self.assertIn('from a tuple',
                      str(self.client._pending_reports[0][0]))

    @no_duplicates
    def test_a_single_pass_iterable_is_refused_rather_than_half_read(self):
        # Refused cleanly and reported, not accepted and then found empty by
        # whichever function read it second.
        def generated():
            yield {'service': 'Y', 'command': 'SUBS',
                   'content': {'code': 21, 'msg': 'nope'}}

        self.client._pending_reports.clear()
        self.client._log_extra_responses({'response': generated()}, 0)

        self.assertEqual(1, len(self.client._pending_reports))
        exc = self.client._pending_reports[0][0]
        self.assertIsInstance(exc, schwaby.streaming.UnusableMessage)
        self.assertIn('is not a list', str(exc))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_relabel_failure_is_absorbed_not_blamed_on_handlers(
            self, ws_connect):
        # Relabeling is this library's work. Reporting its failure as "your
        # handler raised" blamed the consumer's code for a shape the venue
        # sent, and did it once per registered handler -- uncounted, so a
        # framing change flooded at one report per element per handler per tick.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        for _ in range(3):
            self.client.add_level_one_equity_handler(lambda msg: None)

        socket.recv.side_effect = [json.dumps({'data': [{
            'service': 'LEVELONE_EQUITIES', 'command': 'SUBS',
            'content': None}]}), json.dumps(self.streaming_entry(
                'LEVELONE_EQUITIES', 'SUBS'))]

        await self.client.handle_message()
        await self.client.handle_message()

        # Once, not once per handler, and as an absorbed message rather than a
        # handler failure.
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], schwaby.streaming.UnusableMessage)
        self.assertEqual(1, self.client._absorbed)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_unusable_message_is_not_mistaken_for_a_close_failure(
            self, ws_connect):
        # docs/streaming.rst designates (service=None, message=None) as the
        # logout-close signature, and a consumer branches on exactly that. A
        # null channel used to produce the same pair.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append((service, exc, msg)))

        for frame in ({'data': None}, {'notify': None}, {'data': [None]}):
            with self.subTest(frame=frame):
                socket.recv.side_effect = [json.dumps(frame)]
                await self.client.handle_message()

        socket.recv.side_effect = [json.dumps(
                self.streaming_entry('LEVELONE_EQUITIES', 'SUBS'))]
        self.client.add_level_one_equity_handler(lambda msg: None)
        await self.client.handle_message()

        self.assertEqual(3, len(errors))
        for service, exc, message in errors:
            self.assertIsInstance(exc, schwaby.streaming.UnusableMessage)
            self.assertFalse(service is None and message is None)

    def name_service(self, name_type):
        # A custom decoder handing back the service name as a str subclass.
        class Decoder(schwaby.streaming.StreamJsonDecoder):
            def decode_json_string(self, raw):
                frame = json.loads(raw)
                for item in frame.get('data', []):
                    item['service'] = name_type(item['service'])
                return frame
        self.client.json_decoder = Decoder()

    async def relabel_report_for(self, ws_connect, name_type):
        # The absorbed report is delivered when the next frame is read, hence
        # the second frame.
        socket = await self.login_and_get_socket(ws_connect)
        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))
        self.client.add_level_one_equity_handler(lambda msg: None)
        self.name_service(name_type)
        socket.recv.side_effect = [
                json.dumps({'data': [{'service': 'LEVELONE_EQUITIES',
                                      'command': 'SUBS', 'content': None}]}),
                json.dumps(self.streaming_entry('LEVELONE_EQUITIES', 'SUBS'))]

        with self.assertLogs(schwaby.streaming.get_logger(),
                             level='WARNING') as cm:
            await self.client.handle_message()
            await self.client.handle_message()
        return errors, cm.records

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_service_name_cannot_forge_a_relabel_report(
            self, ws_connect):
        class LyingName(str):
            def __str__(self):
                return 'ok\nCRITICAL forged'

        errors, records = await self.relabel_report_for(ws_connect, LyingName)

        self.assertEqual(1, len(errors))
        self.assertIn('could not be relabeled', str(errors[0]))
        self.assertNotIn('\n', str(errors[0]))
        self.assertFalse(any('\n' in r.getMessage() for r in records))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_service_name_that_raises_does_not_end_the_loop(
            self, ws_connect):
        # The report is built inside an except block, so a __str__ that
        # raised there escaped handle_message.
        class RaisingName(str):
            def __str__(self):
                raise RuntimeError('no')

        errors, _ = await self.relabel_report_for(ws_connect, RaisingName)

        self.assertEqual(1, len(errors))
        self.assertIn('could not be relabeled', str(errors[0]))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_service_name_cannot_forge_the_handler_failure_line(
            self, ws_connect):
        class LyingName(str):
            def __str__(self):
                return 'ok\nCRITICAL forged'

        socket = await self.login_and_get_socket(ws_connect)
        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        def handler(msg):
            raise ValueError('handler failed')
        self.client.add_level_one_equity_handler(handler)
        self.name_service(LyingName)
        socket.recv.side_effect = [json.dumps(
                self.streaming_entry('LEVELONE_EQUITIES', 'SUBS'))]

        with self.assertLogs(schwaby.streaming.get_logger(),
                             level='ERROR') as cm:
            await self.client.handle_message()

        lines = [r.getMessage() for r in cm.records
                 if 'Stream handler for service' in r.getMessage()]
        self.assertEqual(1, len(lines))
        self.assertNotIn('\n', lines[0])
        self.assertEqual(1, len(errors))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_relabel_failure_names_its_service_and_its_cause(
            self, ws_connect):
        # service, because a handler routing alerts by subscription needs to
        # know which one went dark, and it is the key the lookup just used.
        # cause, because _BookHandler indexes four levels deep and the KeyError
        # is the only thing that says which field moved.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append((service, exc)))
        self.client.add_nasdaq_book_handler(lambda msg: None)

        socket.recv.side_effect = [json.dumps({'data': [{
            'service': 'NASDAQ_BOOK', 'command': 'SUBS',
            'content': [{'BIDS': [{'no-nested-bids': 1}]}]}]}),
            json.dumps(self.streaming_entry('NASDAQ_BOOK', 'SUBS'))]

        await self.client.handle_message()
        await self.client.handle_message()

        self.assertEqual(1, len(errors))
        service, exc = errors[0]
        self.assertEqual('NASDAQ_BOOK', service)
        self.assertIsInstance(exc, schwaby.streaming.UnusableMessage)
        self.assertIsNotNone(exc.cause)
        # The cause is in the text too, so a log reader sees it as well.
        self.assertIn(type(exc.cause).__name__, str(exc))

    @no_duplicates
    def test_an_unusable_message_carries_its_counts_as_numbers(self):
        # The docs position the running count as the mitigation for suppressed
        # reports. A consumer alerting on drop volume should not have to parse
        # it out of prose.
        self.client._pending_reports.clear()

        for i in range(3):
            self.client._absorb('a bad element', i)
        self.client._absorb('a different thing', 'x')

        by_kind = [(e.count, e.total) for e, _, _ in
                   self.client._pending_reports]
        self.assertEqual([(1, 1), (2, 2), (3, 3), (1, 4)], by_kind)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_unusable_request_id_is_absorbed_not_only_logged(
            self, ws_connect):
        # The one unusable-message path that bypassed _absorb: uncounted,
        # uncoalesced, and with no programmatic signal at all -- while
        # repeating per re-read inside a single subscribe.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        self.client._response_timeout = 0.3

        quiet = asyncio.Event()
        first = [True]

        async def recv():
            if first[0]:
                first[0] = False
                return json.dumps({'response': [{
                    'service': 'X', 'command': 'SUBS',
                    'requestid': 'not a number'}]})
            await quiet.wait()
            return '{}'

        socket.recv = recv

        # The id is only read while a request is outstanding.
        with self.assertRaises(schwaby.streaming.ResponseTimeoutError):
            await self.client.level_one_equity_subs(['GOOG'])

        self.assertEqual(1, self.client._absorbed)
        quiet.set()

        socket.recv = AsyncMock(side_effect=[
                json.dumps(self.streaming_entry('LEVELONE_EQUITIES', 'SUBS'))])
        self.client.add_level_one_equity_handler(lambda msg: None)
        await self.client.handle_message()

        self.assertTrue(any(isinstance(e, schwaby.streaming.UnusableMessage)
                            for e in errors))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_unparsable_frame_is_reported_and_still_raised(
            self, ws_connect):
        # The one failure class that still ends the receive loop. It was also
        # the one reaching add_error_handler not at all, which is the half that
        # was an oversight rather than a decision: a consumer who replaced log
        # scraping with the callback got no signal for it.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        socket.recv.side_effect = ['this is not json at all']

        with self.assertRaises(schwaby.streaming.UnparsableMessage) as cm:
            await self.client.handle_message()

        # Reported as well as raised.
        self.assertEqual(1, len(errors))
        self.assertIs(cm.exception, errors[0])
        self.assertIsNotNone(errors[0].json_parse_exception)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_unparsable_frame_is_quoted_and_cut_in_its_message(
            self, ws_connect):
        # The frame is whatever arrived. A line break in it forged a line in
        # anything that printed the exception, and all of it went into the
        # text however long it was.
        socket = await self.login_and_get_socket(ws_connect)

        raw = 'not json\nCRITICAL forged' + 'A' * 100000
        socket.recv.side_effect = [raw]

        with self.assertRaises(schwaby.streaming.UnparsableMessage) as cm:
            await self.client.handle_message()

        self.assertNotIn('\n', str(cm.exception))
        self.assertLess(len(str(cm.exception)), 400)
        # The frame itself is kept whole, as it arrived.
        self.assertEqual(raw, cm.exception.raw_msg)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_frame_json_cannot_read_at_all_is_unparsable(
            self, ws_connect):
        # A binary frame that is not UTF-8 raises UnicodeDecodeError, one
        # that is bytes but not JSON failed while its message was built, and
        # one nested past the interpreter's depth raises RecursionError. None
        # of them was UnparsableMessage, so each ended the loop unreported.
        socket = await self.login_and_get_socket(ws_connect)
        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        frames = [b'\x80\x81 not utf-8', b'not json either',
                  '[' * 200000 + ']' * 200000]
        socket.recv.side_effect = list(frames)
        for raw in frames:
            with self.subTest(raw=raw[:16]):
                with self.assertRaises(schwaby.streaming.UnparsableMessage):
                    await self.client.handle_message()
        self.assertEqual(3, len(errors))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_unparsable_frame_is_not_reported_as_a_close_failure(
            self, ws_connect):
        # (service=None, message=None) is the logout-close signature, and a
        # parse failure has no service to name. Reporting nothing alongside it
        # would give a dead feed the same shape as a failed teardown -- the
        # absence-as-discriminator trap, which the structural path already
        # avoids. The raw text is passed
        # as `message` so the pair is never both empty.
        socket = await self.login_and_get_socket(ws_connect)

        seen = []
        self.client.add_error_handler(
                lambda service, exc, msg: seen.append((service, msg)))

        socket.recv.side_effect = ['definitely not json']

        with self.assertRaises(schwaby.streaming.UnparsableMessage):
            await self.client.handle_message()

        self.assertEqual(1, len(seen))
        service, message = seen[0]
        self.assertFalse(service is None and message is None)
        self.assertEqual('definitely not json', message)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_empty_frame_is_reported_with_an_empty_message(
            self, ws_connect):
        # raw_msg is '' for an empty text frame, so the pair is distinguishable
        # from the close failure under `is None` but not under a falsy test.
        # Pinned so the limitation is visible rather than discovered: the
        # documented discriminator is the exception type, not the emptiness.
        socket = await self.login_and_get_socket(ws_connect)

        seen = []
        self.client.add_error_handler(
                lambda service, exc, msg: seen.append((service, msg)))

        socket.recv.side_effect = ['']

        with self.assertRaises(schwaby.streaming.UnparsableMessage):
            await self.client.handle_message()

        service, message = seen[0]
        self.assertIsNone(service)
        self.assertEqual('', message)
        # Distinguishable the documented way...
        self.assertFalse(service is None and message is None)
        # ...and not by emptiness, which is why the docs say to use the type.
        self.assertTrue(not service and not message)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_unparsable_frame_is_reported_exactly_once(
            self, ws_connect):
        # handle_message hands the same exception to the waiting request
        # through _fail_pending_request before reporting it, so both readers
        # see one object for one bad frame. Reporting from both -- which is
        # what fixing the missing-report bug first produced -- turned "fires or
        # does not" into "fires once or twice", and both reports carry an
        # identical triple a consumer cannot tell from two distinct frames.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(id(exc)))

        release = asyncio.Event()
        served = [False]

        async def recv():
            if not served[0]:
                # Park until handle_message is the reader AND the subscribe is
                # registered and waiting, which is the interleaving that
                # produces two reports.
                await release.wait()
                served[0] = True
                return 'this is not json at all'
            await asyncio.Event().wait()

        socket.recv = recv

        handling = asyncio.create_task(self.client.handle_message())
        await self.until(self.client._read_lock.locked,
                         'handle_message became the reader')
        subscribing = asyncio.create_task(
                self.client.level_one_equity_subs(['GOOG']))
        await self.until(lambda: self.client._pending_request is not None,
                         'the subscribe registered its request')
        release.set()

        for task in (handling, subscribing):
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, timeout=5)

        self.assertEqual(1, len(errors))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_unparsable_frame_reports_from_the_request_path_too(
            self, ws_connect):
        # Two coroutines can be holding the read lock when a frame arrives, and
        # v2.5.0 reported from only one of them. Which one reads any given
        # frame is a lock race the caller cannot see, so a callback wired on
        # one path and not the other fires or does not fire for reasons nothing
        # in the API explains.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append((exc, msg)))

        socket.recv.side_effect = ['this is not json at all']

        # The subscribe is the reader here, not handle_message.
        with self.assertRaises(schwaby.streaming.UnparsableMessage):
            await self.client.level_one_equity_subs(['GOOG'])

        self.assertEqual(1, len(errors))
        exc, message = errors[0]
        self.assertIsInstance(exc, schwaby.streaming.UnparsableMessage)
        self.assertEqual('this is not json at all', message)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_the_request_path_report_holds_no_lock(self, ws_connect):
        # Same guarantee as the handle_message path: the read lock is released
        # by _await_response's finally and the request lock by the `async
        # with`, so neither is held when user code runs.
        socket = await self.login_and_get_socket(ws_connect)

        observed = []
        self.client.add_error_handler(
                lambda service, exc, msg: observed.append(
                    (self.client._read_lock.locked(),
                     self.client._request_lock.locked())))

        socket.recv.side_effect = ['{not json']

        with self.assertRaises(schwaby.streaming.UnparsableMessage):
            await self.client.level_one_equity_subs(['GOOG'])

        self.assertEqual([(False, False)], observed)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_reporting_an_unparsable_frame_holds_no_lock(
            self, ws_connect):
        # Reporting from inside the `async with` would call a handler under the
        # read lock -- the hazard the whole queue-and-drain design exists to
        # avoid. A handler which reads the lock state proves it is free.
        socket = await self.login_and_get_socket(ws_connect)

        observed = []
        self.client.add_error_handler(
                lambda service, exc, msg: observed.append(
                    self.client._read_lock.locked()))

        socket.recv.side_effect = ['{not json']

        with self.assertRaises(schwaby.streaming.UnparsableMessage):
            await self.client.handle_message()

        self.assertEqual([False], observed)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_handler_cannot_replace_the_parse_failure(
            self, ws_connect):
        # The caller needs the parse failure, not the handler's own accident.
        socket = await self.login_and_get_socket(ws_connect)

        def explode(service, exc, msg):
            raise SystemExit('handler exit')

        self.client.add_error_handler(explode)
        socket.recv.side_effect = ['nope']

        with self.assertRaises(schwaby.streaming.UnparsableMessage):
            await self.client.handle_message()

    @no_duplicates
    def test_a_flood_of_absorbed_messages_cannot_evict_a_rejection(self):
        # _absorb shares the bounded report queue with the late rejections. A
        # frame carrying a few hundred bad elements would otherwise push every
        # rejection out of it -- and a rejection of an abandoned request is the
        # one thing nothing else will ever report. Reports are coalesced onto
        # the same schedule as the log for exactly this reason.
        rejection = (schwaby.streaming.UnexpectedResponseCode({}, 'the one'),
                     'ACCT_ACTIVITY', {})
        self.client._pending_reports.append(rejection)

        for i in range(500):
            self.client._absorb('a bad element', i)

        self.assertIn(rejection, list(self.client._pending_reports))
        self.assertEqual(500, self.client._absorbed)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_logging_in_again_resets_the_absorbed_count(
            self, ws_connect):
        # The log says "on this connection". Without a reset, a client which
        # absorbed a few frames before a drop would log nothing for the first
        # ~995 bad elements of a brand-new outage on the next connection.
        await self.login_and_get_socket(ws_connect)

        for i in range(5):
            self.client._absorb('a bad element', i)
        self.assertEqual(5, self.client._absorbed)

        ws_connect.return_value = AsyncMock()
        await self.client._init_from_preferences(account_preferences(), {})

        self.assertEqual(0, self.client._absorbed)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_null_channel_is_absorbed_not_dropped(self, ws_connect):
        # {"data": null} is a malformed channel, and was the one shape
        # indistinguishable from a frame carrying no data at all -- so it was
        # dropped without a word while {"data": 5} was reported.
        socket = await self.login_and_get_socket(ws_connect)

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        socket.recv.side_effect = [json.dumps({'data': None}),
                                   json.dumps({'notify': None}),
                                   json.dumps({'data': []})]
        await self.client.handle_message()
        await self.client.handle_message()
        await self.client.handle_message()

        # Two absorbed. The third frame is a well-formed empty channel and is
        # not one of them.
        #
        # It used to be `{'command': 'nothing here'}`, chosen as a frame
        # carrying neither channel -- which the channel guard now reports,
        # correctly and for a different reason. That made this test pass while
        # its own comment was false, and only because there is no fourth
        # handle_message to drain the queued report. An empty `data` list
        # tests what this was written to test without overlapping the newer
        # guard; the guard has its own tests.
        self.assertEqual(2, len(errors))
        for exc in errors:
            self.assertIsInstance(exc, schwaby.streaming.UnusableMessage)
        self.assertEqual(2, self.client._absorbed)

    @no_duplicates
    def test_the_report_queue_is_bounded(self):
        # Nothing guarantees handle_message is ever called again. The log line
        # written when each rejection is found is the durable record, so
        # dropping the oldest queued report loses nothing unwritten -- but
        # growing without limit would be a leak on a stream nobody reads.
        for i in range(500):
            self.client._log_extra_responses({'response': [
                {'service': 'X', 'command': 'SUBS', 'content': {'code': 0}},
                {'service': 'Y', 'command': 'SUBS',
                 'content': {'code': 21, 'msg': 'no %d' % i}},
            ]})

        self.assertEqual(64, len(self.client._pending_reports))
        # The newest are the ones kept.
        self.assertIn('no 499', str(self.client._pending_reports[-1][0]))

    @no_duplicates
    def test_an_error_handler_of_the_wrong_arity_is_refused(self):
        # Every other add_*_handler on this class takes a one-argument
        # callback, so passing one here is the natural mistake. Discovered at
        # report time it raises TypeError inside the except clause that exists
        # to stop an error handler failing the stream -- so it would never run,
        # forever, with only a log line to say so.
        for bad in (lambda exc: None,
                    lambda service, exc: None,
                    lambda a, b, c, d: None):
            with self.assertRaises(ValueError):
                self.client.add_error_handler(bad)

        with self.assertRaises(ValueError):
            self.client.add_error_handler('not callable')

        # The correct shapes still register.
        self.client.add_error_handler(lambda service, exc, msg: None)

        def named(service, exception, message):
            pass

        self.client.add_error_handler(named)
        self.assertEqual(2, len(self.client._error_handlers))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_response_with_no_code_is_not_reported(self, ws_connect):
        # An absent code is neither a late rejection nor a late success.
        # Reporting it as a rejection pages someone over "code None, msg None".
        socket = await self.login_and_get_socket(ws_connect)
        socket.recv.side_effect = [json.dumps({'response': [{
            'service': 'LEVELONE_EQUITIES',
            'command': 'SUBS',
            'requestid': '9999',
            'content': {},
        }]})]

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        await self.client.handle_message()

        self.assertEqual([], errors)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_late_rejection_is_reported(self, ws_connect):
        # A response with no request outstanding is absorbed rather than
        # raised, which is right -- the request was abandoned and dropping the
        # session over a late answer loses everything queued behind it. But a
        # late *rejection* is the one thing nothing else will ever report.
        socket = await self.login_and_get_socket(ws_connect)
        socket.recv.side_effect = [json.dumps({'response': [{
            'service': 'LEVELONE_EQUITIES',
            'command': 'SUBS',
            'requestid': '9999',
            'content': {'code': 21, 'msg': 'Bad command formatting'},
        }]})]

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append((service, exc, msg)))

        await self.client.handle_message()

        self.assertEqual(1, len(errors))
        service, exc, _ = errors[0]
        self.assertEqual('LEVELONE_EQUITIES', service)
        self.assertIn('already', str(exc))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_late_success_is_not_reported(self, ws_connect):
        # The other half: a late acknowledgement of something that worked is
        # routine and must not wake anybody up.
        socket = await self.login_and_get_socket(ws_connect)
        socket.recv.side_effect = [json.dumps({'response': [{
            'service': 'LEVELONE_EQUITIES',
            'command': 'SUBS',
            'requestid': '9999',
            'content': {'code': 0, 'msg': 'SUBS command succeeded'},
        }]})]

        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        await self.client.handle_message()

        self.assertEqual([], errors)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_error_handler_which_raises_does_not_break_the_stream(
            self, ws_connect):
        # A callback for absorbed failures must not become a way to fail.
        second = []
        self.client.add_error_handler(
                Mock(side_effect=RuntimeError('error handler blew up')))
        self.client.add_error_handler(
                lambda service, exc, msg: second.append(exc))

        await self.subscribe_and_deliver(
                ws_connect, Mock(side_effect=ValueError('handler blew up')))

        # The second handler still ran, and handle_message returned normally.
        self.assertEqual(1, len(second))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_registering_no_error_handler_changes_nothing(
            self, ws_connect):
        # The behaviour without a callback is the behaviour before it existed:
        # the handler was called, it raised, the failure was absorbed, and the
        # stream is still usable. Asserting the handler list is empty would be
        # true whether or not any of that held.
        handler = Mock(side_effect=ValueError('handler blew up'))
        await self.subscribe_and_deliver(ws_connect, handler)

        handler.assert_called_once()
        self.assertIsNotNone(self.client._socket)


    @no_duplicates
    def test_a_non_string_field_key_is_relabeled_not_dropped(self):
        # `StreamJsonDecoder` is a public extension point that parses the
        # whole frame, so a decoder normalising numeric field ids to ints is
        # a plausible thing for a consumer to write. Such a message went
        # unrelabeled, and once the unknown-field check existed, `.isdigit()`
        # raised AttributeError on it -- absorbed by the relabel guard, so a
        # message that used to be delivered was dropped instead.
        streaming._reported_fields.clear()
        fields = streaming.StreamClient.LevelOneEquityFields
        raw = {'key': 'F', 1: 21.47, 2: 21.48}
        new = copy.deepcopy(raw)
        with self.assertNoLogs(streaming.get_logger(), level='WARNING'):
            fields.relabel_message(raw, new)
        # Relabeled, not merely survived, and not reported as unknown --
        # schwaby does have a name for field 1.
        self.assertEqual({'key': 'F', 'BID_PRICE': 21.47, 'ASK_PRICE': 21.48},
                         new)
        # Positive control: an int id it genuinely has no name for still
        # reports, so the assertNoLogs above is not vacuous.
        raw = {'key': 'F', 99: 'brand new'}
        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            fields.relabel_message(raw, copy.deepcopy(raw))
        streaming._reported_fields.clear()

    @no_duplicates
    def test_a_field_key_that_cannot_be_named_does_not_escape(self):
        # A key is whatever the decoder produced, and `str()` of a wide
        # integer raises past sys.get_int_max_str_digits(). Left where it is
        # -- it cannot be a field id -- rather than taking the message down.
        streaming._reported_fields.clear()
        fields = streaming.StreamClient.LevelOneEquityFields
        raw = {'key': 'F', '1': 21.47, 10 ** 6000: 'x'}
        new = copy.deepcopy(raw)
        fields.relabel_message(raw, new)
        self.assertEqual(21.47, new['BID_PRICE'])
        self.assertIn(10 ** 6000, new)

        # And a million-digit id passes isdigit(); what is retained and
        # logged is bounded, because the set never shrinks.
        raw = {'key': 'F', '9' * 1000000: 'x'}
        fields.relabel_message(raw, copy.deepcopy(raw))
        self.assertLessEqual(
                max(len(i) for _, i in streaming._reported_fields), 64)
        streaming._reported_fields.clear()

    @no_duplicates
    def test_the_new_shape_report_keeps_its_own_promises(self):
        """Its dedup, cap, truncation and kind discriminator, each pinned.

        Every one of those is stated in its own log line or in the changelog
        -- "reported once, not per message", bounded, per kind.
        """

        # Once, not per message.
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            self.assertTrue(
                    self.client._report_new_shape('service', 'SVC_A'))
            for _ in range(5):
                self.assertFalse(
                        self.client._report_new_shape('service', 'SVC_A'))
        self.assertEqual(1, len(got.output))

        # The kind is part of the identity: a channel named like a service is
        # a different fact about the venue.
        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            self.assertTrue(
                    self.client._report_new_shape('channel', 'SVC_A'))

        # A venue-controlled name is bounded; the set never shrinks.
        self.client._reported_shapes.clear()
        self.client._report_new_shape('service', 'S' * 100000)
        self.assertLessEqual(
                max(len(n) for _, n in self.client._reported_shapes), 64)

        # And the count is capped, with a line saying so rather than going
        # quiet in a way that looks like nothing new.
        self.client._reported_shapes.clear()
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            for i in range(_MAX_REPORTED_SHAPES * 3):
                self.client._report_new_shape('service', 'SVC_%d' % i)
        self.assertEqual(_MAX_REPORTED_SHAPES,
                         len(self.client._reported_shapes))
        self.assertIn('as many as it will name', '\n'.join(got.output))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_channel_name_that_cannot_be_named_does_not_escape(
            self, ws_connect):
        # A top-level frame key is whatever the decoder produced, and
        # `StreamJsonDecoder` is a public extension point -- the same
        # argument that made `relabel_message` handle a non-string key.
        # `str()` of a wide integer raises past the digit limit.
        socket = await self.login_and_get_socket(ws_connect)
        delivered = []
        self.client.add_level_one_equity_handler(delivered.append)

        class Unnameable:
            def __str__(self):
                raise RuntimeError('no name for you')

            def __hash__(self):
                return 7

        class Decoder(streaming.StreamJsonDecoder):
            def decode_json_string(self, raw):
                frame = json.loads(raw)
                frame[Unnameable()] = [{'x': 1}]
                frame[10 ** 5000] = [{'x': 1}]
                return frame

        self.client.set_json_decoder(Decoder())
        socket.recv.side_effect = [json.dumps(
                self.streaming_entry('LEVELONE_EQUITIES', 'SUBS'))]

        # Debug on deliberately. `_pretty` runs only for a debug line, so
        # without this the frame never reaches the `repr` fallback and the
        # test passes for want of ever getting there.
        logger = streaming.get_logger()
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        out = io.StringIO()
        handler = logging.StreamHandler(out)
        logger.addHandler(handler)
        try:
            await self.client.handle_message()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)

        # Reported rather than escaping, and the data beside it delivered.
        self.assertEqual(1, len(delivered))
        self.assertEqual(1, self.client._absorbed)
        # And the debug line says what arrived. The redaction filter catches
        # a line that cannot be formatted, so a failure here no longer
        # escapes either way; without the fallback, the line would name the
        # lazy wrapper instead of the frame.
        self.assertIn('Receive', out.getvalue())
        self.assertNotIn('LazyLog object', out.getvalue())

    @no_duplicates
    def test_a_venue_name_reaches_the_log_quoted(self):
        # `%r`, like `_absorb` ten lines away. With `%s` a newline in a
        # service name forged a second line indistinguishable from a real
        # WARNING from this logger, and an empty name rendered as nothing.
        forged = 'EVIL\nWARNING:schwaby.streaming:Schwab sent a service: FAKE'
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            self.client._report_new_shape('service', forged)
        self.assertEqual(1, len(got.output))
        self.assertNotIn('\nWARNING', got.output[0])
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            self.client._report_new_shape('service', '')
        self.assertIn("''", got.output[0])

    @no_duplicates
    def test_a_service_name_that_cannot_be_named_does_not_escape(self):
        # The service path called bare `str()` while the channel path went
        # through `_safe_name` -- three lines apart, one guarded. A decoder
        # producing such a service escaped `handle_message` entirely, ending
        # the receive loop and losing the good element in the same frame.
        class Unnameable:
            def __str__(self):
                raise RuntimeError('no name for you')

            def __hash__(self):
                return 11

        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            self.assertTrue(
                    self.client._report_new_shape('service', Unnameable()))
        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            self.assertTrue(
                    self.client._report_new_shape('service', 10 ** 5000))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_unnameable_service_does_not_end_the_receive_loop(
            self, ws_connect):
        # The end-to-end half, which the direct call above cannot show: the
        # loop survives and the good element in the same frame still reaches
        # its handler. Its channel sibling asserted this; this one did not.
        socket = await self.login_and_get_socket(ws_connect)
        delivered = []
        self.client.add_level_one_equity_handler(delivered.append)

        class Unnameable:
            def __str__(self):
                raise RuntimeError('no name for you')

            def __hash__(self):
                return 11

        class Decoder(streaming.StreamJsonDecoder):
            def decode_json_string(self, raw):
                frame = json.loads(raw)
                frame['data'].append({'service': Unnameable(),
                                      'command': 'SUBS', 'content': []})
                return frame

        self.client.set_json_decoder(Decoder())
        socket.recv.side_effect = [json.dumps(
                self.streaming_entry('LEVELONE_EQUITIES', 'SUBS'))]
        await self.client.handle_message()

        self.assertEqual(1, len(delivered))
        self.assertEqual(1, self.client._absorbed)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_venue_text_cannot_forge_a_response_log_line(
            self, ws_connect):
        # Reachable with ordinary JSON, no custom decoder: a newline in a
        # `service` on the orphan-response path went to the log through `%s`,
        # forging a second line that reads as a real WARNING from this
        # logger. `_absorb` a few lines away already used `%r`.
        socket = await self.login_and_get_socket(ws_connect)
        forged = 'OK\nWARNING:schwaby.streaming:Schwab rejected your order'
        socket.recv.side_effect = [json.dumps({'response': [{
            'service': forged, 'command': 'SUBS',
            'content': {'code': 3, 'msg': 'FORGED'}}]})]
        with self.assertLogs(streaming.get_logger(), level='INFO') as got:
            await self.client.handle_message()
        self.assertEqual(1, len(got.output))
        self.assertNotIn('\nWARNING', got.output[0])

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_response_value_that_cannot_be_formatted_is_survivable(
            self, ws_connect):
        # A subscribe Schwab *accepted* raised a bare ValueError out of the
        # message built for the late rejection beside it -- not a
        # SchwabError, not absorbed, reported to no handler, from a request
        # that had already succeeded.
        socket = await self.login_and_get_socket(ws_connect)

        class Decoder(streaming.StreamJsonDecoder):
            def decode_json_string(self, raw):
                return {'response': [{'service': 'X', 'command': 'SUBS',
                                      'content': {'code': 10 ** 5000,
                                                  'msg': 'm'}}]}

        self.client.set_json_decoder(Decoder())
        socket.recv.side_effect = ['{}']
        with self.assertLogs(streaming.get_logger(), level='INFO') as got:
            await self.client.handle_message()
        # The line survives with its content, rather than logging swallowing
        # a formatting failure and printing nothing useful.
        self.assertIn('Received a response', '\n'.join(got.output))

    @no_duplicates
    def test_the_field_cap_says_so_rather_than_going_quiet(self):
        # Going quiet at a cap is indistinguishable from nothing new
        # arriving, which is the one reading an operator must not be left to
        # make. The shapes cap said so; the fields cap did not.
        streaming._reported_fields.clear()
        fields = streaming.StreamClient.LevelOneEquityFields
        raw = {'key': 'F'}
        raw.update({str(i): 'x'
                    for i in range(
                        500, 500 + streaming._MAX_REPORTED_FIELDS)})
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            fields.relabel_message(raw, copy.deepcopy(raw))
        self.assertIn('as many as schwaby will name', '\n'.join(got.output))
        streaming._reported_fields.clear()

    @no_duplicates
    def test_fields_and_shapes_do_not_share_a_budget(self):
        # 254 declared field ids across 14 tables against 64 slots. Sharing
        # one budget let the lesser event consume it and leave a dropped
        # service unnamed -- the defect `_report_new_shape` exists to close.
        streaming._reported_fields.clear()
        fields = streaming.StreamClient.LevelOneEquityFields
        raw = {'key': 'F'}
        raw.update({str(i): 'x'
                    for i in range(
                        200, 200 + streaming._MAX_REPORTED_FIELDS * 2)})
        fields.relabel_message(raw, copy.deepcopy(raw))
        self.assertEqual(streaming._MAX_REPORTED_FIELDS,
                         len(streaming._reported_fields))
        # Saturated on one side, untouched on the other. Guaranteed by
        # construction once the shapes set moved into `__init__`, so it is
        # kept as a statement of intent and the real guard is below: a shape
        # still reports with the field budget full.
        self.assertEqual(set(), self.client._reported_shapes)
        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            self.assertTrue(
                    self.client._report_new_shape('service', 'SVC_X'))
        streaming._reported_fields.clear()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_every_unknown_service_is_named_at_least_once(
            self, ws_connect):
        """`_absorb` coalesces across names; this does not.

        Its counter is keyed on a fixed `what` string -- deliberately, so a
        value the venue chooses cannot grow the dict -- which means every
        unknown service shares one counter and the first-three-then-powers-
        of-ten rule applies across names. Measured before this: five new
        services on one connection and the fourth was never named, in no log
        line and no callback, over a thousand frames.

        That was the wrong way round. A dropped service is a worse event than
        an unnamed field, and the field path already guarantees a line per
        distinct id.
        """
        streaming._reported_fields.clear()
        socket = await self.login_and_get_socket(ws_connect)
        reported = []
        self.client.add_error_handler(
                lambda service, exc, msg: reported.append(service))
        names = ['SVC_%s' % c for c in 'ABCDE']
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            for name in names:
                socket.recv.side_effect = [json.dumps({'data': [{
                    'service': name, 'command': 'SUBS',
                    'content': [{'key': 'F'}]}]})] * 4
                for _ in range(4):
                    await self.client.handle_message()
        blob = '\n'.join(got.output)
        for name in names:
            with self.subTest(service=name):
                self.assertIn(name, blob)
        # And through `add_error_handler`, which is the half a consumer
        # actually branches on. `_absorb` coalesces per kind, so without a
        # forced first sighting the fourth and fifth reach no handler at all.
        await self.client._drain_pending_reports()
        self.assertEqual(set(names), set(reported))
        streaming._reported_fields.clear()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_invalid_service_is_reported_as_schwabs_error(
            self, ws_connect):
        """`'Invalid Service'` is Schwab's error label, which can arrive just
        before Schwab closes a connection. It is reported as Schwab's error,
        not as an unknown service, so nobody is asked to open an issue for a
        service that does not exist.

        The frame was never captured, so these fixtures are synthetic: the
        name as it was logged, on each channel a service is routed from.
        """
        streaming._reported_fields.clear()
        socket = await self.login_and_get_socket(ws_connect)
        reported = []
        self.client.add_error_handler(
                lambda service, exc, msg: reported.append((service, exc)))
        frames = [{'data': [{'service': 'Invalid Service', 'command': 'SUBS',
                             'content': [{'key': 'F'}]}]},
                  {'notify': [{'service': 'Invalid Service'}]},
                  # Positive control: a name this version does not know still
                  # gets the request for an issue, one with "invalid" in it
                  # included -- the marker is matched exactly.
                  {'data': [{'service': 'SVC_INVALID', 'command': 'SUBS',
                             'content': [{'key': 'F'}]}]}]
        socket.recv.side_effect = [json.dumps(f) for f in frames]
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            for _ in frames:
                await self.client.handle_message()
        await self.client._drain_pending_reports()

        invalid = [line for line in got.output if 'Invalid Service' in line]
        self.assertEqual(2, len(invalid))
        for line in invalid:
            with self.subTest(line=line):
                self.assertIn("Schwab\'s error", line)
                self.assertNotIn('open an issue', line)
                self.assertNotIn('does not know', line)
        self.assertTrue(any('SVC_INVALID' in line and 'open an issue' in line
                            for line in got.output))

        # Still reported, as the type a consumer already branches on, with
        # the name as the service.
        self.assertEqual(['Invalid Service', 'Invalid Service', 'SVC_INVALID'],
                         [service for service, _ in reported])
        for _, exc in reported:
            self.assertIsInstance(exc, streaming.UnusableMessage)
        streaming._reported_fields.clear()

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_every_unknown_channel_is_named_at_least_once(
            self, ws_connect):
        streaming._reported_fields.clear()
        socket = await self.login_and_get_socket(ws_connect)
        reported = []
        self.client.add_error_handler(
                lambda service, exc, msg: reported.extend(
                    exc.message if isinstance(exc.message, list) else []))
        names = ['chan%s' % c for c in 'ABCDE']
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            for name in names:
                frame = self.streaming_entry('LEVELONE_EQUITIES', 'SUBS')
                frame[name] = [{'x': 1}]
                socket.recv.side_effect = [json.dumps(frame)] * 4
                for _ in range(4):
                    await self.client.handle_message()
        blob = '\n'.join(got.output)
        for name in names:
            with self.subTest(channel=name):
                self.assertIn(name, blob)
        # And through the callback. Without this the channel half of `force`
        # is unproven: its red-proof mutation also kills the log line, so it
        # reds on the log assertions above and reads as covering both.
        await self.client._drain_pending_reports()
        self.assertEqual(set(names), set(reported))
        streaming._reported_fields.clear()


    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_second_client_hears_about_a_dropped_service_itself(
            self, ws_connect):
        """The guarantee lives where the callback lives.

        Held module-wide, the report deduped across every client in the
        process -- so a second client, with its own error handler that had
        never heard anything, was told about three of five new services and
        never about the other two. `_reported_fields` stays module-wide,
        because that report is log-only and the log is process-wide.
        """
        names = ['SVC_%s' % c for c in 'ABCDE']

        async def drive():
            socket = await self.login_and_get_socket(ws_connect)
            heard = []
            self.client.add_error_handler(
                    lambda service, exc, msg: heard.append(service))
            for name in names:
                socket.recv.side_effect = [json.dumps({'data': [{
                    'service': name, 'command': 'SUBS',
                    'content': [{'key': 'F'}]}]})] * 4
                for _ in range(4):
                    await self.client.handle_message()
            await self.client._drain_pending_reports()
            return set(heard)

        first = await drive()
        self.assertEqual(set(names), first)

        # A fresh client, fresh handler, same process.
        self.setUp()
        self.assertEqual(set(names), await drive())

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_non_finite_requestid_does_not_end_the_receive_loop(
            self, ws_connect):
        """`json.loads` maps `1e999` and `Infinity` to `float('inf')`.

        `int(inf)` raises `OverflowError`, which was in neither guard tuple,
        so an ordinary JSON number literal ended the receive loop and killed
        the in-flight request with a bare non-SchwabError, reported nowhere.
        `NaN` takes the same route and raises `ValueError`, which is why one
        of the two non-finite spellings was covered and this read as
        complete.

        This is the framing dependence `_read_and_route`'s own comment says
        it exists to remove: with nothing pending the same frame was logged
        and harmless.
        """
        bad = ('{"response":[{"requestid":%s,'
               '"service":"LEVELONE_EQUITIES","command":"SUBS",'
               '"content":{"code":0,"msg":"ok"}}]}')
        for literal in ('1e999', 'Infinity', '-Infinity', 'NaN',
                        '"notanint"', 'null', '{}'):
            with self.subTest(requestid=literal):
                # A fresh client each time: the request counter advances, so
                # one reused across the loop stops matching its own replies.
                self.setUp()
                socket = await self.login_and_get_socket(ws_connect)
                # The reply this subscribe will actually wait for. There
                # are two counters and only one goes into `requestid`:
                # `_request_id` is the request's, `request_number` is the
                # receive counter behind the debug lines. Reading the wrong
                # one made the "good" frame a mismatched id, so every case
                # passed down the id-mismatch path instead of the one it
                # names -- red under mutation either way, and testing
                # something else.
                good = ('{"response":[{"requestid":"%d",'
                        '"service":"LEVELONE_EQUITIES","command":"SUBS",'
                        '"content":{"code":0,"msg":"ok"}}]}'
                        % self.client._request_id)
                socket.recv.side_effect = [bad % literal, good]
                # The bad frame is absorbed and the real answer arrives
                # behind it, so the subscribe *succeeds*. That is the whole
                # claim: an unusable requestid must not end the loop.
                await self.client.level_one_equity_subs(['F'])
                # Released, so the client is still usable.
                self.assertFalse(self.client._read_lock.locked())
                self.assertFalse(self.client._request_lock.locked())

    @no_duplicates
    def test_validate_response_refuses_a_non_finite_requestid_too(self):
        """The second guard, tested where it is reachable.

        A frame only routes if element 0 carries a usable int, so
        `_read_and_route` catches the non-finite spellings first and this
        one never sees them on that path -- which is why the end-to-end test
        leaves it green. It is a defensive guard: it is what stands between
        a caller of `_validate_response` and an `OverflowError` if routing
        ever stops filtering, so it gets the direct case rather than none.
        """
        for value in (float('inf'), float('-inf'), float('nan'), 'notanint'):
            with self.subTest(requestid=value):
                frame = {'response': [{'requestid': value, 'service': 'X',
                                       'command': 'SUBS',
                                       'content': {'code': 0}}]}
                result = self.client._validate_response(
                        frame, 1, 'X', 'SUBS')
                self.assertIsInstance(
                        result, schwaby.streaming.UnexpectedResponse)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_accepted_subscribe_survives_an_unformattable_sibling(
            self, ws_connect):
        # The routing-path half. A frame whose element 0 accepts the request
        # and whose element 1 is a late rejection carrying a value that
        # cannot be formatted: the subscribe has already succeeded, and the
        # message built for the rejection beside it raised at the caller.
        socket = await self.login_and_get_socket(ws_connect)

        class Decoder(streaming.StreamJsonDecoder):
            def decode_json_string(self, raw):
                return {'response': [
                    {'requestid': '1', 'service': 'LEVELONE_EQUITIES',
                     'command': 'SUBS', 'content': {'code': 0, 'msg': 'ok'}},
                    {'requestid': '99', 'service': 'LEVELONE_EQUITIES',
                     'command': 'SUBS',
                     'content': {'code': 10 ** 5000, 'msg': 'm'}}]}

        self.client.set_json_decoder(Decoder())
        socket.recv.side_effect = ['{}']
        await self.client.level_one_equity_subs(['F'])   # must not raise

    @no_duplicates
    def test_a_formatted_venue_value_keeps_its_content_and_its_bound(self):
        # Both halves are asserted: a `_safe_value` returning '' would
        # satisfy one, and one with no length bound the other. The docstring
        # claims the content survives *and* that it is bounded.
        self.assertEqual("'hello'", streaming._safe_value('hello'))
        self.assertIn('hello', streaming._safe_value('hello'))
        long_one = streaming._safe_value('A' * 10000)
        self.assertLessEqual(len(long_one), 200)
        self.assertIn('AAAA', long_one)

        class Boom:
            def __repr__(self):
                raise RuntimeError('no repr')

        self.assertIn('Boom', streaming._safe_value(Boom()))
        self.assertIn('cannot be formatted', streaming._safe_value(Boom()))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_absorbed_offender_does_not_lose_its_own_log_line(
            self, ws_connect):
        # `_absorb`'s line is the complete record and the callback is the
        # convenience, so losing the line whole is the wrong way round. It
        # used `%r` on a venue-controlled offender, which logging swallows
        # when it raises -- the record dropped, the callback still firing.
        socket = await self.login_and_get_socket(ws_connect)

        class Decoder(streaming.StreamJsonDecoder):
            def decode_json_string(self, raw):
                return {'data': 10 ** 5000}

        self.client.set_json_decoder(Decoder())
        socket.recv.side_effect = ['{}']
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            await self.client.handle_message()
        self.assertIn('Ignoring', '\n'.join(got.output))
        self.assertEqual(1, self.client._absorbed)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_mapping_that_cannot_be_enumerated_still_dispatches(
            self, ws_connect):
        """`_is_mapping` is structural on purpose; the channel check was not.

        Its docstring says the test is `get` + `__contains__` and nothing
        about iteration, *because* "a lightweight mapping-like object worked
        before the type checks were added and would have had every frame
        dropped afterwards". `set(msg)` requires iteration. So a channel
        check that iterates ended the receive loop with a
        non-SchwabError on shapes 4.2.0 handled, through a documented public
        extension point.
        """
        payload = {'data': [{'service': 'LEVELONE_EQUITIES',
                             'command': 'SUBS',
                             'content': [{'key': 'F', '1': 21.47}]}]}

        class NoIter:
            def get(self, key, default=None):
                return payload.get(key, default)

            def __contains__(self, key):
                return key in payload

        class IterRaises(dict):
            def __iter__(self):
                raise RuntimeError('no iter')

        class UnhashableKeys(dict):
            def __iter__(self):
                return iter([['a', 'list']])

        for label, obj in (('get+contains only', NoIter()),
                           ('__iter__ raises', IterRaises(payload)),
                           ('unhashable keys', UnhashableKeys(payload)),
                           ('plain dict', dict(payload))):
            with self.subTest(shape=label):
                self.setUp()
                socket = await self.login_and_get_socket(ws_connect)
                delivered = []
                self.client.add_level_one_equity_handler(delivered.append)

                class Decoder(streaming.StreamJsonDecoder):
                    def decode_json_string(self, raw):
                        return obj

                self.client.set_json_decoder(Decoder())
                socket.recv.side_effect = ['{}']
                await self.client.handle_message()
                self.assertEqual(1, len(delivered))

        # Positive control: the check itself still fires on a real dict, so
        # the tolerance above did not simply disable it.
        self.setUp()
        socket = await self.login_and_get_socket(ws_connect)
        self.client.add_level_one_equity_handler(lambda m: None)
        frame = dict(payload, brandNew=[{'x': 1}])
        socket.recv.side_effect = [json.dumps(frame)]
        await self.client.handle_message()
        self.assertEqual(1, self.client._absorbed)

    @no_duplicates
    def test_every_validate_response_message_is_bounded_and_quoted(self):
        # Four venue-controlled formats in one function. One was swept and
        # the three above it were not -- a `service` of 100,000 characters
        # made a 100,000-character exception where the line twenty rows
        # below caps at 200, and a newline in one reached whatever the
        # caller logs unescaped.
        for field, frame in (
                # An int, not a digit string: `mismatched_id` is what
                # `int()` returned, and `int()` refuses a 100,000-character
                # string outright. A decoder can hand one over directly.
                ('requestid', {'response': [{'requestid': 10 ** 5000,
                                             'service': 'S', 'command': 'C',
                                             'content': {'code': 0}}]}),
                ('service', {'response': [{'requestid': '1',
                                           'service': 'X' * 100000,
                                           'command': 'C',
                                           'content': {'code': 0}}]}),
                ('command', {'response': [{'requestid': '1', 'service': 'S',
                                           'command': 'X' * 100000,
                                           'content': {'code': 0}}]})):
            with self.subTest(field=field):
                exc = self.client._validate_response(frame, 1, 'S', 'C')
                self.assertLess(len(str(exc)), 400)

        forged = 'EVIL\nWARNING:schwaby.streaming:the stream is healthy'
        exc = self.client._validate_response(
                {'response': [{'requestid': '1', 'service': forged,
                               'command': 'C', 'content': {'code': 0}}]},
                1, 'S', 'C')
        self.assertNotIn('\nWARNING', str(exc))
        # Positive control: an ordinary mismatch still names the value.
        exc = self.client._validate_response(
                {'response': [{'requestid': '1', 'service': 'WRONG',
                               'command': 'C', 'content': {'code': 0}}]},
                1, 'S', 'C')
        self.assertIn('WRONG', str(exc))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_an_absorbed_cause_does_not_name_its_type_twice(
            self, ws_connect):
        # `_safe_value` reprs, which is right where a value stands alone and
        # wrong beside an explicit type name: `Cause: TypeError:
        # TypeError("...")`. That degrades the line this module calls the
        # complete record.
        socket = await self.login_and_get_socket(ws_connect)
        self.client.add_level_one_equity_handler(lambda m: None)
        socket.recv.side_effect = [json.dumps({'data': [{
            'service': 'LEVELONE_EQUITIES', 'command': 'SUBS',
            'content': 5}]})]
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            await self.client.handle_message()
        line = '\n'.join(got.output)
        self.assertIn('Cause:', line)
        for name in ('TypeError', 'AttributeError'):
            self.assertLessEqual(line.count(name), 1)

        # And the same guard the value side has: a cause whose `str` raises
        # must not take the line with it.
        class Boom(Exception):
            def __str__(self):
                raise RuntimeError('no str')

        self.assertEqual('<unprintable>', streaming._safe_str(Boom()))
        self.assertLessEqual(len(streaming._safe_str('A' * 10000)), 200)
        self.assertEqual('plain', streaming._safe_str('plain'))

    # ---- Something Schwab added that this version cannot route ----------
    #
    # A field it does not recognise still reaches a handler. A *service* it
    # does not recognise cannot: there is nowhere to put the message, so it
    # is dropped -- and a dropped message is what `_absorb` is for. Going
    # quiet here means a whole feed can appear without anyone learning it
    # exists.

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_service_this_version_does_not_know_is_reported(
            self, ws_connect):
        socket = await self.login_and_get_socket(ws_connect)
        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append((service, exc)))

        # Two messages: _absorb queues the report and the drain runs at the
        # top of the next handle_message, so no user code runs while the read
        # lock is held. The second frame is ordinary traffic.
        socket.recv.side_effect = [
                json.dumps({'data': [{
                    'service': 'BRAND_NEW_SERVICE', 'command': 'SUBS',
                    'content': [{'key': 'F', '1': 1.0}]}]}),
                json.dumps(self.streaming_entry('LEVELONE_EQUITIES', 'SUBS'))]
        self.client.add_level_one_equity_handler(lambda msg: None)
        await self.client.handle_message()
        await self.client.handle_message()

        self.assertEqual(1, self.client._absorbed)
        self.assertEqual(1, len(errors))
        service, exc = errors[0]
        self.assertEqual('BRAND_NEW_SERVICE', service)
        self.assertIsInstance(exc, schwaby.streaming.UnusableMessage)
        self.assertIn('does not know', str(exc))
        # The offending value rides on `.message`, which is what a consumer
        # reads to learn *which* service appeared.
        self.assertEqual('BRAND_NEW_SERVICE', exc.message)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_known_service_without_a_handler_stays_quiet(
            self, ws_connect):
        # Registering no handler for a service is the caller's own choice and
        # is not news. Reporting it would put a line on every message of a
        # feed somebody deliberately ignored.
        socket = await self.login_and_get_socket(ws_connect)
        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        socket.recv.side_effect = [
                json.dumps(self.streaming_entry('CHART_FUTURES', 'SUBS')),
                json.dumps(self.streaming_entry('CHART_FUTURES', 'SUBS'))]
        await self.client.handle_message()
        await self.client.handle_message()

        self.assertEqual(0, self.client._absorbed)
        self.assertEqual([], errors)

        # Positive control: the same path does report an unknown one, so the
        # empty list above is not an empty list for want of ever arriving.
        socket.recv.side_effect = [
                json.dumps({'data': [{
                    'service': 'BRAND_NEW_SERVICE', 'command': 'SUBS',
                    'content': [{'key': 'F'}]}]}),
                json.dumps(self.streaming_entry('CHART_FUTURES', 'SUBS'))]
        await self.client.handle_message()
        await self.client.handle_message()
        self.assertEqual(1, len(errors))

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_notify_frame_naming_no_service_stays_quiet(
            self, ws_connect):
        # A notify frame is not required to name a service, and that is
        # documented rather than unexpected.
        socket = await self.login_and_get_socket(ws_connect)
        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))

        socket.recv.side_effect = [
                json.dumps({'notify': [{'heartbeat': '1234'}]}),
                json.dumps({'notify': [{'content': {'code': 0}}]})]
        await self.client.handle_message()
        await self.client.handle_message()

        self.assertEqual(0, self.client._absorbed)
        self.assertEqual([], errors)

    @no_duplicates
    @patch('schwaby.streaming.ws_client.connect', new_callable=AsyncMock)
    async def test_a_channel_this_version_does_not_read_is_reported(
            self, ws_connect):
        # A whole compartment, not one field: every message in it is being
        # dropped. Reported, and then carried on past, because the channels
        # that *are* understood still hold real data.
        socket = await self.login_and_get_socket(ws_connect)
        errors = []
        self.client.add_error_handler(
                lambda service, exc, msg: errors.append(exc))
        delivered = []
        self.client.add_level_one_equity_handler(delivered.append)

        entry = self.streaming_entry('LEVELONE_EQUITIES', 'SUBS')
        entry['brandNewChannel'] = [{'anything': 1}]
        socket.recv.side_effect = [
                json.dumps(entry),
                json.dumps(self.streaming_entry('LEVELONE_EQUITIES', 'SUBS'))]
        await self.client.handle_message()
        await self.client.handle_message()

        self.assertEqual(1, len(errors))
        self.assertIn('does not read', str(errors[0]))
        self.assertEqual(['brandNewChannel'], errors[0].message)
        # And the data beside it still reached its handler.
        self.assertEqual(2, len(delivered))


class LevelOneOptionStrikeFieldTest(IsolatedAsyncioTestCase):
    """Field 20 of LEVELONE_OPTIONS is documented by Schwab as "Strike Price".
    It was named STRIKE_TYPE, which is a different thing, so messages were
    relabeled with a name that described neither the field nor its contents."""

    @no_duplicates
    def test_field_twenty_is_the_strike_price(self):
        fields = streaming.StreamClient.LevelOneOptionFields
        self.assertEqual(20, fields.STRIKE_PRICE.value)
        self.assertEqual('STRIKE_PRICE', fields.key_mapping()['20'])

    @no_duplicates
    def test_the_old_spelling_is_gone(self):
        # STRIKE_TYPE was kept as an alias through the rename. Code still
        # naming it now fails at the point of use rather than silently reading
        # a field whose label no longer matches what it asked for.
        fields = streaming.StreamClient.LevelOneOptionFields
        with self.assertRaises(AttributeError):
            fields.STRIKE_TYPE
        self.assertEqual(
                1, [m.name for m in fields].count('STRIKE_PRICE'))

    @no_duplicates
    def test_an_alias_does_not_decide_the_label(self):
        # key_mapping used to iterate __members__, which yields aliases too, so
        # whichever alias came last would have named the field.
        fields = streaming.StreamClient.LevelOneOptionFields
        self.assertNotIn('STRIKE_TYPE', fields.key_mapping().values())


class AccountActivityMessageTypeTest(IsolatedAsyncioTestCase):
    """ACCT_ACTIVITY types outside the observed vocabulary are named once.

    Schwab documents the MESSAGE_TYPE vocabulary nowhere, and only fourteen
    types have been captured. A message of any other type is still delivered; what
    is new is that it no longer passes silently.
    """

    fields = streaming.StreamClient.AccountActivityFields

    def setUp(self):
        streaming._reported_message_types.clear()

    def tearDown(self):
        streaming._reported_message_types.clear()

    def relabel(self, message_type):
        raw = {'key': 'account', '1': 'account', '2': message_type,
               '3': 'data'}
        new = copy.deepcopy(raw)
        self.fields.relabel_message(raw, new)
        return new

    @no_duplicates
    def test_every_observed_type_is_known(self):
        observed = streaming.StreamClient.ACCOUNT_ACTIVITY_MESSAGE_TYPES
        with self.assertNoLogs(streaming.get_logger(), level='WARNING'):
            for message_type in observed:
                self.relabel(message_type)
        # Positive control: the same call does report a type outside it, so
        # the silence above is not silence everywhere.
        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            self.relabel('OrderExpired')

    @no_duplicates
    def test_a_case_variant_of_an_observed_type_is_not_new(self):
        # The docs say to match case-insensitively, so an upper-cased observed
        # type must not be reported as a new one.
        with self.assertNoLogs(streaming.get_logger(), level='WARNING'):
            self.relabel('ORDERCREATED')
            self.relabel('orderfillcompleted')

    @no_duplicates
    def test_a_truncation_is_not_a_case_variant(self):
        # ORDERUROUT is not OrderUROutCompleted in another case. It is a
        # different, shorter token, and folding does not make the two meet.
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            self.relabel('ORDERUROUT')
        self.assertIn("'ORDERUROUT'", got.output[0])

    @no_duplicates
    def test_an_unobserved_type_is_still_delivered(self):
        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            new = self.relabel('OrderExpired')
        self.assertEqual('OrderExpired', new['MESSAGE_TYPE'])
        # Positive control: the rest of the item relabeled as well.
        self.assertEqual('account', new['ACCOUNT'])

    @no_duplicates
    def test_it_is_reported_once_per_type_not_per_message(self):
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            for _ in range(3):
                self.relabel('OrderExpired')
            self.relabel('orderexpired')   # the same type in another case
        self.assertEqual(1, len(got.output))
        # A different type earns its own line.
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            self.relabel('OrderReplaced')
        self.assertEqual(1, len(got.output))
        self.assertIn("'OrderReplaced'", got.output[0])

    @no_duplicates
    def test_an_item_with_no_type_is_not_reported(self):
        raw = {'key': 'account', '1': 'account'}
        with self.assertNoLogs(streaming.get_logger(), level='WARNING'):
            self.fields.relabel_message(raw, copy.deepcopy(raw))
        # Positive control: the same shape with a type does report.
        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            self.relabel('OrderExpired')

    @no_duplicates
    def test_the_empty_type_carrying_a_notice_is_not_reported(self):
        # Schwab's "Feature not supported" notice has been recorded arriving as
        # a data item whose MESSAGE_TYPE is the empty string, which the docs
        # describe. It is not a type nobody has captured, so it asks nobody to
        # open an issue.
        raw = {'key': 'account', '1': 'account', '2': '',
               '3': 'Feature not supported'}
        new = copy.deepcopy(raw)
        with self.assertNoLogs(streaming.get_logger(), level='WARNING'):
            self.fields.relabel_message(raw, new)
        self.assertEqual('', new['MESSAGE_TYPE'])
        self.assertEqual('Feature not supported', new['MESSAGE_DATA'])
        # Nor does it spend the bounded budget of reported types.
        self.assertEqual(set(), streaming._reported_message_types)
        # The public set stays a list of real types: consumers build a set of
        # known lifecycle types from it.
        self.assertNotIn(
                '', streaming.StreamClient.ACCOUNT_ACTIVITY_MESSAGE_TYPES)
        # Positive control: the same call still reports a type outside it.
        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            self.relabel('OrderExpired')

    @no_duplicates
    def test_an_empty_type_carrying_a_payload_is_still_reported(self):
        # Only the notice's shape is exempt. An empty type carrying a JSON
        # object, text that starts like one and is broken, or no text at all
        # has never been captured, and passing it silently is what the report
        # is for.
        missing = object()
        for data in ('{"OrderExpired": {}}', '{broken', '', '   ', None,
                     missing):
            with self.subTest(data=data):
                streaming._reported_message_types.clear()
                raw = {'key': 'account', '1': 'account', '2': ''}
                if data is not missing:
                    raw['3'] = data
                with self.assertLogs(streaming.get_logger(),
                                     level='WARNING') as got:
                    self.fields.relabel_message(raw, copy.deepcopy(raw))
                self.assertEqual(1, len(got.output))
                self.assertIn("type ''", got.output[0])

    @no_duplicates
    def test_a_notice_that_cannot_be_checked_is_reported_not_dropped(self):
        # If contrib.util cannot be imported, the notice check cannot run. The
        # item is still relabeled, and reported as the empty type it is, rather
        # than raising out of the relabel and taking the item with it.
        import sys
        from unittest.mock import patch
        raw = {'key': 'account', '1': 'account', '2': '',
               '3': 'Feature not supported'}
        new = copy.deepcopy(raw)
        with patch.dict(sys.modules, {'schwaby.contrib.util': None}):
            with self.assertLogs(streaming.get_logger(), level='WARNING'):
                self.fields.relabel_message(raw, new)
        self.assertEqual('Feature not supported', new['MESSAGE_DATA'])

    @no_duplicates
    def test_a_newline_in_a_type_cannot_forge_a_log_line(self):
        forged = 'X\nWARNING:schwaby.streaming:the feed is healthy'
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            self.relabel(forged)
        self.assertEqual(1, len(got.output))
        self.assertNotIn('\nWARNING', got.output[0])

    @no_duplicates
    def test_the_report_is_bounded(self):
        cap = streaming._MAX_REPORTED_MESSAGE_TYPES
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            for i in range(cap * 3):
                self.relabel('Unobserved%d' % i)
        self.assertEqual(cap, len(streaming._reported_message_types))
        self.assertIn('as many as schwaby will name', '\n'.join(got.output))
        # A long type is bounded before it is retained.
        streaming._reported_message_types.clear()
        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            self.relabel('T' * 10000)
        self.assertLessEqual(
                max(len(t) for t in streaming._reported_message_types), 64)

    @no_duplicates
    def test_folding_keeps_distinct_types_distinct_and_bounded(self):
        # Folding can lengthen a name: 'ß' becomes 'ss'. Cutting again after
        # folding made two different types share one report, so the name is
        # cut once, before folding, and what is kept is bounded by folding's
        # own limit of three characters for one.
        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            self.relabel('\u00df' * 33 + 'A')
            self.relabel('\u00df' * 33 + 'B')
        self.assertEqual(2, len(got.output))
        self.assertLessEqual(
                max(len(t) for t in streaming._reported_message_types), 192)

    @no_duplicates
    def test_a_str_formatted_for_a_log_line_is_plain_and_bounded(self):
        # `_safe_str` formats an exception beside its type name. `str()` of a
        # KeyError is the repr of its key, and a decoder's key object can hand
        # back itself -- whose own `__len__` then ran, measured ending the
        # receive loop and losing a valid quote in the same frame.
        class Key(str):
            def __repr__(self):
                return self

            def __len__(self):
                raise RuntimeError('boom')

        text = streaming._safe_str(KeyError(Key('2')))
        self.assertIs(str, type(text))
        self.assertLessEqual(len(text), 200)

    @no_duplicates
    def test_a_type_name_that_is_a_str_subclass_is_read_as_plain_text(self):
        # A class's `__name__` can be set to a str subclass, and the
        # descriptor hands it back as it is; its own `__len__` ran next.
        class Name(str):
            def __len__(self):
                raise RuntimeError('boom')

        class Renamed:
            pass

        Renamed.__name__ = Name('Renamed')
        self.assertEqual('Renamed', streaming._type_name(Renamed()))

    @no_duplicates
    def test_a_custom_repr_or_class_name_cannot_forge_a_log_line(self):
        # A plain repr escapes a newline; a value's own `__repr__` and a class
        # name need not, and both reach log lines.
        forged = 'x\nCRITICAL:schwaby.streaming:the feed is healthy'

        class Loud:
            def __repr__(self):
                return forged

        Named = type('N' + forged, (), {})
        for text in (streaming._safe_value(Loud()),
                     streaming._type_name(Named())):
            with self.subTest(text=text):
                self.assertNotIn('\n', text)
                self.assertIn('CRITICAL', text)

        # Escaping takes one character to four here, so each is bounded again
        # afterwards: both inputs fit their bound before escaping.
        class Wide:
            def __repr__(self):
                return '\x01' * 200

        Wider = type('\x01' * 64, (), {})
        for text, bound in ((streaming._safe_value(Wide()), 200),
                            (streaming._type_name(Wider()), 64)):
            with self.subTest(bound=bound):
                self.assertLessEqual(len(text), bound)
                self.assertTrue(text.startswith('\\x01'))

    @no_duplicates
    def test_no_line_break_reaches_a_formatted_value(self):
        # `str()` of an exception is not a repr, and a decoder's exception can
        # quote venue text. Past ASCII, \x85, U+2028 and U+2029 break a line
        # too, for str.splitlines() and whatever reads the log that way.
        forged = 'CRITICAL:schwaby.streaming:the feed is healthy'
        for brk in ('\n', '\x85', '\u2028', '\u2029'):
            class Loud:
                def __repr__(self, brk=brk):
                    return 'x' + brk + forged

            for text in (streaming._safe_str(ValueError('x' + brk + forged)),
                         streaming._safe_value(Loud())):
                with self.subTest(brk=brk, text=text):
                    self.assertEqual(1, len(text.splitlines()))
                    self.assertIn('CRITICAL', text)

    @no_duplicates
    def test_an_unnameable_type_does_not_escape_a_name(self):
        # The fallback that names an unprintable value read
        # `type(x).__name__`, which consults the metaclass first.
        class Meta(type):
            @property
            def __name__(cls):
                raise RuntimeError('boom')

        class Unprintable(metaclass=Meta):
            def __str__(self):
                raise RuntimeError('boom')

        self.assertIs(str, type(streaming._safe_name(Unprintable())))

    @no_duplicates
    def test_a_value_formatted_for_a_log_line_is_plain_and_bounded(self):
        # `_safe_value` reprs a whole frame when one is absorbed. A decoder's
        # str subclass whose `__repr__` returns itself kept its own `__len__`:
        # raising ended the receive loop with nothing logged, and returning 0
        # logged the whole frame.
        class Frame(str):
            def __repr__(self):
                return self

            def __len__(self):
                raise RuntimeError('boom')

        class Quiet(str):
            def __repr__(self):
                return self

            def __len__(self):
                return 0

        for cls in (Frame, Quiet):
            with self.subTest(frame=cls.__name__):
                text = streaming._safe_value(cls('x' * 100000))
                self.assertIs(str, type(text))
                self.assertLessEqual(len(text), 200)

    @no_duplicates
    async def test_a_str_subclass_type_is_still_delivered(self):
        # A custom decoder can hand over a str subclass, and `str()` returns
        # one untouched when its `__str__` returns itself. Its own `casefold`
        # then ran unguarded -- raising absorbed the whole element, valid
        # items with it -- and its own `__len__` passed the length bound.
        class Type(str):
            def __str__(self):
                return self

            def casefold(self):
                raise RuntimeError('boom')

            def __len__(self):
                return 0

        client = StreamClient(client=MagicMock())
        got = []
        client.add_account_activity_handler(got.append)
        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            await client._dispatch_to_handlers(
                    'ACCT_ACTIVITY',
                    {'service': 'ACCT_ACTIVITY', 'command': 'SUBS',
                     'timestamp': 1,
                     'content': [{'key': 'account', '1': 'account',
                                  '2': 'OrderCreated', '3': 'data'},
                                 {'key': 'account', '1': 'account',
                                  '2': Type('Q' * 100000), '3': 'data'}]},
                    relabel=True)
        self.assertEqual(1, len(got))
        self.assertEqual(2, len(got[0]['content']))
        self.assertEqual(0, client._absorbed)
        self.assertLessEqual(
                max(len(t) for t in streaming._reported_message_types), 64)

        # A subclass whose only trick is its length, which the bound would
        # otherwise take at its word: the logged line stays short.
        class Long(str):
            def __str__(self):
                return self

            def __len__(self):
                return 0

        with self.assertLogs(streaming.get_logger(), level='WARNING') as got:
            self.relabel(Long('R' * 100000))
        self.assertLess(max(len(line) for line in got.output), 2000)

    @no_duplicates
    def test_the_vocabulary_is_not_an_enum_member(self):
        # A frozenset in an Enum body silently becomes a member, which would
        # add a fifth field and change key_mapping for every account
        # activity message. It lives on StreamClient for that reason.
        self.assertNotIn('ACCOUNT_ACTIVITY_MESSAGE_TYPES',
                         self.fields.__members__)
        self.assertEqual(
                {'0': 'SUBSCRIPTION_KEY', '1': 'ACCOUNT',
                 '2': 'MESSAGE_TYPE', '3': 'MESSAGE_DATA'},
                self.fields.key_mapping())

    @no_duplicates
    def test_it_is_exactly_the_observed_types(self):
        # Upper-case spellings of the change and monitor types circulate;
        # these are the spellings Schwab sends.
        self.assertEqual(
                {'SUBSCRIBED', 'OrderCreated', 'OrderAccepted',
                 'ExecutionRequested', 'ExecutionRequestCreated',
                 'ExecutionRequestCompleted', 'OrderFillCompleted',
                 'CancelAccepted', 'ExecutionCreated',
                 'OrderUROutCompleted',
                 'ChangeCreated', 'ChangeAccepted',
                 'OrderMonitorCreated', 'OrderMonitorCompleted'},
                set(streaming.StreamClient.ACCOUNT_ACTIVITY_MESSAGE_TYPES))

    @no_duplicates
    async def test_an_unobserved_type_reaches_a_handler(self):
        client = StreamClient(client=MagicMock())
        got = []
        client.add_account_activity_handler(got.append)
        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            await client._dispatch_to_handlers(
                    'ACCT_ACTIVITY',
                    {'service': 'ACCT_ACTIVITY', 'command': 'SUBS',
                     'timestamp': 1,
                     'content': [{'key': 'account', '1': 'account',
                                  '2': 'OrderExpired', '3': 'data'}]},
                    relabel=True)
        self.assertEqual(1, len(got))
        self.assertEqual('OrderExpired',
                         got[0]['content'][0]['MESSAGE_TYPE'])
        self.assertEqual(0, client._absorbed)


class UnknownStreamFieldTest(IsolatedAsyncioTestCase):
    """A field Schwab adds reaches a handler. Nobody was told it exists.

    The delivering half is deliberate and predates this: an id the field
    table does not have is left on the message verbatim while every known
    field still relabels, so a schema addition does not break a consumer.
    What was missing is any way to learn it happened.
    """

    def setUp(self):
        streaming._reported_fields.clear()

    def tearDown(self):
        streaming._reported_fields.clear()

    @no_duplicates
    def test_an_unknown_field_is_still_delivered_verbatim(self):
        fields = streaming.StreamClient.LevelOneEquityFields
        raw = {'key': 'F', '1': 21.47, '2': 21.48, '99': 'brand new'}
        new = copy.deepcopy(raw)
        fields.relabel_message(raw, new)
        self.assertEqual('brand new', new['99'])
        # Positive control: the rest of the message still relabeled, so this
        # is not passing because relabeling did nothing at all.
        self.assertEqual(21.47, new['BID_PRICE'])
        self.assertEqual(21.48, new['ASK_PRICE'])

    @no_duplicates
    def test_a_key_is_read_as_plain_text_before_it_is_judged_a_field(self):
        # A decoder's str subclass could answer isdigit for itself and put a
        # forged line into the unknown-field warning.
        class LyingKey(str):
            def __str__(self):
                return self

            def isdigit(self):
                return True

        fields = streaming.StreamClient.LevelOneEquityFields
        raw = {'key': 'F', LyingKey('x\nCRITICAL forged'): 1, '999': 2}
        with self.assertLogs(streaming.get_logger(),
                             level='WARNING') as caught:
            fields.relabel_message(raw, dict(raw))
        # Positive control: a genuine unknown id is still reported.
        self.assertTrue(any('999' in line for line in caught.output))
        self.assertFalse(
                any('\n' in r.getMessage() for r in caught.records))

    @no_duplicates
    def test_an_unknown_field_is_reported_once_per_table_and_id(self):
        fields = streaming.StreamClient.LevelOneEquityFields
        with self.assertLogs(streaming.get_logger(),
                             level='WARNING') as caught:
            for _ in range(3):
                raw = {'key': 'F', '1': 21.47, '99': 'brand new'}
                fields.relabel_message(raw, copy.deepcopy(raw))
        self.assertEqual(1, len(caught.output))
        self.assertIn('99', caught.output[0])
        self.assertIn('LevelOneEquityFields', caught.output[0])

        # A different id, and the same id on a different table, each earn a
        # line: they are different facts about the venue.
        with self.assertLogs(streaming.get_logger(),
                             level='WARNING') as caught:
            raw = {'key': 'F', '98': 'another'}
            fields.relabel_message(raw, copy.deepcopy(raw))
            raw = {'key': 'F', '99': 'brand new'}
            streaming.StreamClient.LevelOneOptionFields.relabel_message(
                    raw, copy.deepcopy(raw))
        self.assertEqual(2, len(caught.output))

    @no_duplicates
    def test_the_keys_that_are_not_fields_are_not_reported(self):
        # `key`, `seq`, `delayed` and `assetMainType` arrive alongside the
        # numbered fields and none of them is a field. Keying on "not in the
        # table" instead of on "numeric" would report all four on every
        # message, which is a flood rather than a signal.
        fields = streaming.StreamClient.LevelOneEquityFields
        raw = {'key': 'F', 'seq': 7, 'delayed': False,
               'assetMainType': 'EQUITY', '1': 21.47}
        with self.assertNoLogs(streaming.get_logger(), level='WARNING'):
            new = copy.deepcopy(raw)
            fields.relabel_message(raw, new)
        # Positive control: the same call does report a numeric one.
        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            raw['99'] = 'brand new'
            fields.relabel_message(raw, copy.deepcopy(raw))

    @no_duplicates
    def test_the_report_is_bounded(self):
        fields = streaming.StreamClient.LevelOneEquityFields
        for i in range(streaming._MAX_REPORTED_FIELDS * 3):
            raw = {'key': 'F', str(1000 + i): 'x'}
            fields.relabel_message(raw, copy.deepcopy(raw))
        self.assertLessEqual(len(streaming._reported_fields),
                             streaming._MAX_REPORTED_FIELDS)
        # And relabeling still works after the cap.
        raw = {'key': 'F', '1': 21.47, '99999': 'x'}
        new = copy.deepcopy(raw)
        fields.relabel_message(raw, new)
        self.assertEqual(21.47, new['BID_PRICE'])
        self.assertEqual('x', new['99999'])

    @no_duplicates
    async def test_a_new_field_reaches_a_handler_not_breaks_one(self):
        # End to end through the dispatch path, which is where a consumer
        # would actually meet this.
        client = StreamClient(client=MagicMock())
        got = []
        client.add_level_one_equity_handler(got.append)
        with self.assertLogs(streaming.get_logger(), level='WARNING'):
            await client._dispatch_to_handlers(
                    'LEVELONE_EQUITIES',
                    {'service': 'LEVELONE_EQUITIES', 'command': 'SUBS',
                     'timestamp': 1,
                     'content': [{'key': 'F', '1': 21.47, '99': 'brand new'}]},
                    relabel=True)
        self.assertEqual(1, len(got))
        self.assertEqual(21.47, got[0]['content'][0]['BID_PRICE'])
        self.assertEqual('brand new', got[0]['content'][0]['99'])
        # Nothing was absorbed and nothing was reported as a failure.
        self.assertEqual(0, client._absorbed)


class KnownServiceSetTest(IsolatedAsyncioTestCase):
    """`_KNOWN_SERVICES` is declared, so it can go stale.

    This is what stops it.
    """

    @no_duplicates
    def test_the_known_service_set_matches_the_code_that_uses_it(self):
        """The set is declared, so it can go stale. This is what stops it.

        Two other places name every service: the subscribe methods, which
        pass one to `_service_op`, and the handler registrations. A service
        added to those without being added here would be reported as unknown
        on the very traffic it was added to receive.
        """
        with open(schwaby.streaming.__file__) as f:
            source = f.read()
        subscribed = set(re.findall(
                r"_service_op\(\s*[^)]*?'([A-Z][A-Z_0-9]+)'", source, re.S))
        registered = set(re.findall(r"_handlers\['([A-Z_0-9]+)'\]", source))

        # Non-empty, or two typos in a regex would make this pass by matching
        # nothing at all. A floor rather than the exact count: pinning 13 made
        # *correctly* adding a service fail, with a message pointing at
        # nothing wrong, which is a check that punishes the thing it exists to
        # support.
        self.assertGreater(len(subscribed), 10)
        self.assertGreater(len(registered), 10)
        self.assertEqual(subscribed, registered)

        known = schwaby.streaming._KNOWN_SERVICES
        self.assertEqual(set(), subscribed - known)
        # ADMIN is request/response only and never reaches a handler, so it
        # is in the set and in neither of the two walks above.
        self.assertEqual({'ADMIN'}, known - subscribed)
