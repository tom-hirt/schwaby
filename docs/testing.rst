.. _testing:

=================
Testing Your Code
=================

.. module:: schwaby.testing

Code built on this library has to be tested without Schwab's servers: a
test that reaches them is slow, needs an account, and can refresh a real
token. :mod:`schwaby.testing` gives a test a client whose every request,
the token endpoint's included, goes to a fake that answers as the test says,
and makes sure nothing else gets out.

.. code-block:: python

    import schwaby
    import schwaby.testing as st

    def test_a_dead_token_asks_for_a_login(tmp_path):
        path = tmp_path / 'token.json'
        st.write_token_file(path, expires_in=-60)    # due a refresh

        endpoint = st.FakeTokenEndpoint()
        endpoint.answer_refresh(st.refusal(400, error='invalid_grant'))

        with st.block_network():
            client = schwaby.auth.client_from_token_file(
                    path, 'APP_KEY', 'APP_SECRET', transport=endpoint)
            ...   # the code under test, which calls client.get_quote()

        assert endpoint.posts('refresh_token') == 1

The client is built as it is in production, with ``transport=``: every
``client_from_*`` function and :func:`~schwaby.auth.easy_client` take one.
Only where its requests go changes. A refresh is checked, written to the
token file and reported as it is from Schwab's answer, so a test sees the
:class:`~schwaby.utils.TokenRefreshError`, its ``kind`` and all, that the
same answer from Schwab would raise.

``import schwaby.testing`` is for tests: nothing else in the library imports
it.

.. note:: The streaming connection does not go through ``transport=``.
   :func:`block_network` refuses it, but there is no fake streamer yet.


---------------------
Answering the request
---------------------

A :class:`FakeTokenEndpoint` keeps a queue of answers for refreshes and one
for logins, and takes each answer once, in order. Every other request goes to
the handler given to :meth:`~FakeTokenEndpoint.on_api`. A request with
nothing to answer it raises :class:`UnexpectedRequest`, so a test that
refreshes more often than it expected fails instead of passing on a made-up
token.

.. code-block:: python

    endpoint.answer_refresh(st.token(expires_in=1800))
    endpoint.answer_refresh(st.refusal(401, body=None))        # no body
    endpoint.answer_refresh(st.answer(503))                    # a 5xx
    endpoint.answer_refresh(st.answer(200, body=b'<html>'))    # not JSON
    endpoint.answer_refresh(st.rate_limited(retry_after=5))
    endpoint.answer_refresh(st.raises(httpx2.ConnectError('down')))
    endpoint.answer_refresh(st.token(), repeat=True)           # every one after

    endpoint.on_api(lambda request: {'AAPL': {'quote': {}}})

    endpoint.posts('refresh_token')   # how many refreshes were sent
    endpoint.requests                 # every request, in order

A login works the same way. :func:`received_url` builds the redirect Schwab
would send, and :meth:`~FakeTokenEndpoint.answer_exchange` answers the
exchange of its code:

.. code-block:: python

    context = schwaby.auth.get_auth_context('APP_KEY', callback_url)
    endpoint.answer_exchange(st.token())
    client = schwaby.auth.client_from_received_url(
            'APP_KEY', 'APP_SECRET', context, st.received_url(context),
            schwaby.auth.token_file_writer(path), transport=endpoint)

.. note:: A login exchanges its code synchronously, even for an ``asyncio``
   client, so a transport given to one has to serve both kinds of request.
   :class:`FakeTokenEndpoint` and ``httpx2.MockTransport`` do.

.. autoclass:: schwaby.testing.FakeTokenEndpoint
  :members: answer_refresh, answer_exchange, on_api, posts, requests

.. autofunction:: schwaby.testing.token
.. autofunction:: schwaby.testing.refusal
.. autofunction:: schwaby.testing.answer
.. autofunction:: schwaby.testing.rate_limited
.. autofunction:: schwaby.testing.raises
.. autoexception:: schwaby.testing.UnexpectedRequest


---------------------
Keeping the test home
---------------------

:func:`block_network` refuses every connection and name lookup that leaves
the machine, and clears the ``*_PROXY`` settings while it is active. A
request the client was not given a transport for, or one from code the test
did not expect to call out, fails the test there.

.. code-block:: python

    with st.block_network():
        ...

    @st.block_network()
    def test_something():
        ...

    # pytest: imported into the test module or conftest.py
    from schwaby.testing import no_network

    def test_something_else(no_network):
        ...

.. note:: Code that catches the error and carries on does not get the test
   past it: leaving the block raises it again. A test that means to provoke
   one clears the block's ``attempts`` first.

.. autoclass:: schwaby.testing.block_network
.. autofunction:: schwaby.testing.no_network
.. autoexception:: schwaby.testing.NetworkBlocked


-----------------------
Token files and writers
-----------------------

.. autofunction:: schwaby.testing.write_token_file
.. autofunction:: schwaby.testing.received_url
.. autofunction:: schwaby.testing.failing_writer
