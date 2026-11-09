from .base import BaseClient
from ..debug import register_redactions_from_response
from ..utils import LazyLog, TokenFreshness

import json


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

    async def ensure_token_fresh(self, margin=300.0):
        '''Refreshes the access token now if it expires within ``margin``
        seconds, so that a later call does not have to.

        A request refreshes the token on the way past once it is about to
        expire, so the first call after that waits for a refresh, and a
        refusal surfaces from it. This lets a program choose when that
        happens instead, before it needs the token. The new token is written
        through the client's token writer, as a request's refresh writes it,
        and a refusal raises :class:`~schwaby.utils.TokenRefreshError`, with
        its ``kind``, as a request's would. A token whose expiry cannot be read is refreshed.

        The coroutine of :meth:`Client.ensure_token_fresh
        <schwaby.client.Client.ensure_token_fresh>`, for ``asyncio``.

        :param margin: seconds; the token is refreshed when it has this long
                       left or less.
        :return: :attr:`TokenFreshness.REFRESHED
                 <schwaby.utils.TokenFreshness.REFRESHED>` when it refreshed,
                 :attr:`TokenFreshness.FRESH
                 <schwaby.utils.TokenFreshness.FRESH>` when it sent nothing.
        '''
        if self._token_is_fresh(self._check_margin(margin)):
            return TokenFreshness.FRESH
        with self._translate_token_errors():
            await self.session.refresh_token(
                    refresh_token=self._refresh_token_to_send())
        return TokenFreshness.REFRESHED

    async def _get_request(self, path, params):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: GET to %s, params=%s',
                req_num, dest, LazyLog(lambda: json.dumps(params, indent=4)))

        with self._translate_token_errors():
            resp = await self.session.get(dest, params=params)
        self._log_response(resp, req_num)
        register_redactions_from_response(resp)
        return resp

    async def _post_request(self, path, data):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: POST to %s, json=%s',
                req_num, dest, LazyLog(lambda: json.dumps(data, indent=4)))

        with self._translate_token_errors():
            resp = await self.session.post(dest, json=data)
        self._log_response(resp, req_num)
        register_redactions_from_response(resp)
        return resp

    async def _put_request(self, path, data):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: PUT to %s, json=%s',
                req_num, dest, LazyLog(lambda: json.dumps(data, indent=4)))

        with self._translate_token_errors():
            resp = await self.session.put(dest, json=data)
        self._log_response(resp, req_num)
        register_redactions_from_response(resp)
        return resp

    async def _delete_request(self, path):
        dest = 'https://api.schwabapi.com' + path

        req_num = self._req_num()
        self.logger.debug('Req %s: DELETE to %s', req_num, dest)

        with self._translate_token_errors():
            resp = await self.session.delete(dest)
        self._log_response(resp, req_num)
        register_redactions_from_response(resp)
        return resp
