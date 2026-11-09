from .base import BaseClient, TOKEN_PATH
from ..debug import register_redactions_from_response
from ..utils import LazyLog, TokenFreshness, TokenRefreshError

import asyncio
import inspect
import json
import time
from asyncio import sleep as _sleep


class AsyncClient(BaseClient):
    '''
    :class:`Client` for ``asyncio``: the same methods, each a coroutine to be
    awaited. Made by the auth functions when given ``asyncio=True``.
    '''

    async def close_async_session(self):
        '''Closes the underlying HTTP session and the connections it is
        holding. The ``asyncio`` counterpart of
        :meth:`Client.close_session`; the client cannot be used after it.
        '''
        await self.session.aclose()

    async def ensure_token_fresh(self, margin=600.0):
        """The coroutine of :meth:`Client.ensure_token_fresh
        <schwaby.client.Client.ensure_token_fresh>`, for ``asyncio``: it
        refreshes the access token now if it expires within ``margin``
        seconds, or the session's own leeway if that is longer, and says
        whether it did. It holds the session's own refresh lock, which a
        request's refresh on the way past holds too, so the two never each
        send a refresh.

        :param margin: seconds; the token is refreshed when it has this long
                       left or less.
        :return: :attr:`TokenFreshness.REFRESHED
                 <schwaby.utils.TokenFreshness.REFRESHED>` when it refreshed,
                 :attr:`TokenFreshness.FRESH
                 <schwaby.utils.TokenFreshness.FRESH>` when it sent nothing.
        """
        margin = self._check_margin(margin)
        if self._token_is_fresh(margin):
            return TokenFreshness.FRESH
        _, refreshed = await _refresh_unless(
                self, lambda: self._token_is_fresh(margin), attempt=1)
        return (TokenFreshness.REFRESHED if refreshed
                else TokenFreshness.FRESH)

    async def _get_request(self, path, params):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: GET to %s, params=%s',
                req_num, dest, LazyLog(lambda: json.dumps(params, indent=4)))

        return await _send(self, req_num, 'GET', path,
                           lambda **auth:
                               self.session.get(dest, params=params, **auth),
                           safe=True)

    async def _post_request(self, path, data):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: POST to %s, json=%s',
                req_num, dest, LazyLog(lambda: json.dumps(data, indent=4)))

        return await _send(self, req_num, 'POST', path,
                           lambda **auth:
                               self.session.post(dest, json=data, **auth),
                           safe=False)

    async def _put_request(self, path, data):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: PUT to %s, json=%s',
                req_num, dest, LazyLog(lambda: json.dumps(data, indent=4)))

        return await _send(self, req_num, 'PUT', path,
                           lambda **auth:
                               self.session.put(dest, json=data, **auth),
                           safe=False)

    async def _delete_request(self, path):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: DELETE to %s', req_num, dest)

        return await _send(self, req_num, 'DELETE', path,
                           lambda **auth: self.session.delete(dest, **auth),
                           safe=False)


# A function, not a method: every coroutine method of the client is one a
# caller awaits, and this is only ever awaited by them.
async def _send(client, req_num, method, path, send, *, safe):
    '''Sends ``client``'s request with ``send()``, through the pace when
    there is one, and, for a ``safe`` request (a GET), again after a rate
    limit when :meth:`~schwaby.client.Client.set_rate_limit_retry` asked for
    it: after a 429 response, and after a token refresh answered with 429,
    which sent nothing.'''
    attempt, waited = 0, 0.0
    while True:
        attempt += 1
        waited += await _call_pace(client, 'api', method, path, attempt)
        try:
            # With a pace, the refresh the session would make is made here,
            # through it, just before the session would.
            if client._pace is not None and client._token_due():
                paced, _ = await _refresh_unless(
                        client, lambda: not client._token_due(),
                        attempt=attempt)
                waited += paced
            with client._translate_token_errors():
                resp = await send(**client._send_auth())
        except TokenRefreshError as e:
            # An order is never held back: one sent late could fill at a
            # price its caller never saw, or after it was placed again.
            wait = (client._refresh_retry_wait(e, attempt, waited) if safe
                    else None)
            if wait is None:
                raise
            client._log_retry(req_num, 'its token refresh was', wait,
                              attempt)
            await _sleep(wait)
            waited += wait
            continue
        client._log_response(resp, req_num)
        register_redactions_from_response(resp)
        wait = (client._response_retry_wait(resp, attempt, waited) if safe
                else None)
        if wait is None:
            return resp
        client._log_retry(req_num, 'it was', wait, attempt)
        await _sleep(wait)
        waited += wait


async def _refresh_unless(client, fresh, *, attempt):
    '''Refreshes ``client``'s token through the pace, unless ``fresh()``
    finds it fresh. One call refreshes at a time: another that needs it
    waits for that one, and checks again, so a refresh is paced once and
    sent once. Returns the seconds this call waited, and whether it
    refreshed.'''
    # A token with no refresh token is dead before the pace is asked.
    with client._translate_token_errors():
        client._refresh_token_to_send()
    waited = 0.0
    while True:
        if fresh():
            return waited, False
        flight = client._refresh_flight
        if flight is not None:
            started = time.monotonic()
            await flight.wait()
            waited += time.monotonic() - started
            continue
        flight = client._refresh_flight = asyncio.Event()
        try:
            waited += await _call_pace(client, 'token_refresh', 'POST',
                                       TOKEN_PATH, attempt)
            # The session's own lock, which its refresh on the way past a
            # request holds, so the two never send a refresh each; checked
            # again inside it, since one may have just finished.
            lock = getattr(client.session, '_token_refresh_lock', None)
            if not hasattr(lock, '__aenter__'):
                lock = _NoLock()
            async with lock:
                if fresh():
                    return waited, False
                with client._translate_token_errors():
                    await client.session.refresh_token(
                            refresh_token=client._refresh_token_to_send())
            return waited, True
        finally:
            client._refresh_flight = None
            flight.set()


async def _call_pace(client, purpose, method, path, attempt):
    '''Calls ``client``'s pace, when there is one, waiting for what it
    returns when that is awaitable, and returns the seconds it took.'''
    pace = client._pace
    if pace is None:
        return 0.0
    started = time.monotonic()
    result = pace(client._pace_request(purpose, method, path, attempt))
    if inspect.isawaitable(result):
        await result
    return time.monotonic() - started


class _NoLock:
    """Stands in for a session lock that is not there."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False
