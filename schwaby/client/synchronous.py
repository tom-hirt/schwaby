from .base import BaseClient
from ..utils import LazyLog, TokenFreshness
from ..debug import register_redactions_from_response

import json


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
        # Checked again inside the lock, since another call may have just
        # refreshed. The synchronous session has no lock of its own, so a
        # request in another thread can still refresh at the same moment,
        # as two requests can.
        with self._refresh_lock:
            if self._token_is_fresh(margin):
                return TokenFreshness.FRESH
            with self._translate_token_errors():
                self.session.refresh_token(
                        refresh_token=self._refresh_token_to_send())
        return TokenFreshness.REFRESHED

    def _get_request(self, path, params):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: GET to %s, params=%s',
                req_num, dest, LazyLog(lambda: json.dumps(params, indent=4)))

        with self._translate_token_errors():
            resp = self.session.get(dest, params=params)
        self._log_response(resp, req_num)
        register_redactions_from_response(resp)
        return resp

    def _post_request(self, path, data):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: POST to %s, json=%s',
            req_num, dest, LazyLog(lambda: json.dumps(data, indent=4)))

        with self._translate_token_errors():
            resp = self.session.post(dest, json=data)
        self._log_response(resp, req_num)
        register_redactions_from_response(resp)
        return resp

    def _put_request(self, path, data):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: PUT to %s, json=%s',
            req_num, dest, LazyLog(lambda: json.dumps(data, indent=4)))

        with self._translate_token_errors():
            resp = self.session.put(dest, json=data)
        self._log_response(resp, req_num)
        register_redactions_from_response(resp)
        return resp

    def _delete_request(self, path):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: DELETE to %s', req_num, dest)

        with self._translate_token_errors():
            resp = self.session.delete(dest)
        self._log_response(resp, req_num)
        register_redactions_from_response(resp)
        return resp
