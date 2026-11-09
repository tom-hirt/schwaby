from .base import BaseClient, TOKEN_PATH
from ..utils import LazyLog, TokenFreshness, TokenRefreshError
from ..debug import register_redactions_from_response

import inspect
import json
import time
from time import sleep as _sleep


class Client(BaseClient):
    '''
    The HTTP client: one method per endpoint Schwab publishes, each returning
    the raw ``httpx2`` response. Make one with :ref:`the auth functions
    <auth>` rather than directly, since they manage the token.
    :class:`AsyncClient` is the same client for ``asyncio``.
    '''

    def close_session(self):
        '''Closes the underlying HTTP session and the connections it is
        holding.

        Only worth calling if you are creating clients repeatedly -- one client
        held for the life of the process needs nothing. Without it, connections
        are released whenever the session is garbage collected, which is not a
        moment anything guarantees.

        The client cannot be used after this.
        :meth:`AsyncClient.close_async_session` is the equivalent for the
        asyncio client.
        '''
        self.session.close()

    def ensure_token_fresh(self, margin=600.0):
        '''Refreshes the access token now if it expires within ``margin``
        seconds, so that a later call does not have to.

        A request refreshes the token on the way past once it is about to
        expire, so the first call after that waits for a refresh, and a
        refusal surfaces from it. This lets a program choose when that
        happens instead, before it needs the token. The new token is written
        through the client's token writer, as a request's refresh writes it,
        and a refusal raises :class:`~schwaby.utils.TokenRefreshError`, with
        its ``kind``, as a request's would. A token whose expiry cannot be
        read is refreshed.

        It refreshes when the token has ``margin`` seconds left or less, or
        the session's own leeway if that is longer, since a request would
        refresh within that anyway. Two calls of it at once refresh once:
        the second finds the token fresh.

        :param margin: seconds; the token is refreshed when it has this long
                       left or less.
        :return: :attr:`TokenFreshness.REFRESHED
                 <schwaby.utils.TokenFreshness.REFRESHED>` when it refreshed,
                 :attr:`TokenFreshness.FRESH
                 <schwaby.utils.TokenFreshness.FRESH>` when it sent nothing.
        '''
        margin = self._check_margin(margin)
        if self._token_is_fresh(margin):
            return TokenFreshness.FRESH
        _, refreshed = self._refresh_unless(
                lambda: self._token_is_fresh(margin), attempt=1)
        return (TokenFreshness.REFRESHED if refreshed
                else TokenFreshness.FRESH)

    def _refresh_unless(self, fresh, *, attempt):
        '''Refreshes the token through the pace, unless ``fresh()`` finds it
        fresh once the lock is held. Returns the seconds the pace took, and
        whether it refreshed.'''
        # A token with no refresh token is dead before the pace is asked.
        with self._translate_token_errors():
            self._refresh_token_to_send()
        # Paced before the lock, so a pace that waits holds nothing else up.
        paced = self._call_pace('token_refresh', 'POST', TOKEN_PATH, attempt)
        # Checked again inside the lock, since another call may have just
        # refreshed. Without a pace the synchronous session refreshes on its
        # own, with no lock, so a request in another thread can still refresh
        # at the same moment, as two requests can.
        with self._refresh_lock:
            if fresh():
                return paced, False
            with self._translate_token_errors():
                self.session.refresh_token(
                        refresh_token=self._refresh_token_to_send())
        return paced, True

    def _call_pace(self, purpose, method, path, attempt):
        '''Calls the pace, when there is one, and returns the seconds it
        took.'''
        pace = self._pace
        if pace is None:
            return 0.0
        started = time.monotonic()
        result = pace(self._pace_request(purpose, method, path, attempt))
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError(
                    'the pace returned an awaitable, which Client cannot '
                    'wait for; an async pace is for AsyncClient')
        return time.monotonic() - started

    def _get_request(self, path, params):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: GET to %s, params=%s',
                req_num, dest, LazyLog(lambda: json.dumps(params, indent=4)))

        return self._send(req_num, 'GET', path,
                          lambda: self.session.get(dest, params=params),
                          safe=True)

    def _post_request(self, path, data):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: POST to %s, json=%s',
            req_num, dest, LazyLog(lambda: json.dumps(data, indent=4)))

        return self._send(req_num, 'POST', path,
                          lambda: self.session.post(dest, json=data),
                          safe=False)

    def _put_request(self, path, data):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: PUT to %s, json=%s',
            req_num, dest, LazyLog(lambda: json.dumps(data, indent=4)))

        return self._send(req_num, 'PUT', path,
                          lambda: self.session.put(dest, json=data),
                          safe=False)

    def _delete_request(self, path):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: DELETE to %s', req_num, dest)

        return self._send(req_num, 'DELETE', path,
                          lambda: self.session.delete(dest),
                          safe=False)

    def _send(self, req_num, method, path, send, *, safe):
        '''Sends a request with ``send()``, through the pace when there is
        one, and, for a ``safe`` request (a GET), again after a rate limit
        when :meth:`set_rate_limit_retry` asked for it: after a 429
        response, and after a token refresh answered with 429, which sent
        nothing.'''
        attempt, waited = 0, 0.0
        while True:
            attempt += 1
            waited += self._call_pace('api', method, path, attempt)
            try:
                # With a pace, the refresh the session would make is made
                # here, through it, just before the session would.
                if self._pace is not None and self._token_due():
                    paced, _ = self._refresh_unless(
                            lambda: not self._token_due(), attempt=attempt)
                    waited += paced
                with self._translate_token_errors():
                    resp = send()
            except TokenRefreshError as e:
                # An order is never held back: one sent late could fill at a
                # price its caller never saw, or after it was placed again.
                wait = (self._refresh_retry_wait(e, attempt, waited) if safe
                        else None)
                if wait is None:
                    raise
                self._log_retry(req_num, 'its token refresh was', wait,
                                attempt)
                _sleep(wait)
                waited += wait
                continue
            self._log_response(resp, req_num)
            register_redactions_from_response(resp)
            wait = (self._response_retry_wait(resp, attempt, waited) if safe
                    else None)
            if wait is None:
                return resp
            self._log_retry(req_num, 'it was', wait, attempt)
            _sleep(wait)
            waited += wait
