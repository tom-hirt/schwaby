.. highlight:: python
.. py:module:: schwaby.streaming

.. _stream:


================
Streaming Client
================

A wrapper around the Schwab streaming data API. This is a websockets-based
streaming API that provides up-to-the-second data on market activity,
including Level Two and time-of-sale data for major equities, options and
futures exchanges.

Here's an example of how you can receive book snapshots of ``GOOG`` (note if you
run this outside regular trading hours you may not see anything):

.. code-block:: python

  from schwaby.auth import easy_client
  from schwaby.client import Client
  from schwaby.streaming import StreamClient

  import asyncio
  import json

  # Assumes you've already created a token. See the authentication page for more
  # information.
  client = easy_client(
          api_key='YOUR_API_KEY',
          app_secret='YOUR_APP_SECRET',
          callback_url='https://127.0.0.1',
          token_path='/path/to/token.json')
  stream_client = StreamClient(client)

  async def read_stream():
      await stream_client.login()

      def print_message(message):
        print(json.dumps(message, indent=4))

      # Always add handlers before subscribing because many streams start sending
      # data immediately after success, and messages with no handlers are dropped.
      stream_client.add_nasdaq_book_handler(print_message)
      await stream_client.nasdaq_book_subs(['GOOG'])

      while True:
          await stream_client.handle_message()

  asyncio.run(read_stream())

.. _one_stream_per_user:

.. warning::

  **One streaming session per Schwab user, and a second one does not fail
  cleanly.** Schwab's response code 12 reads "A limit of 1 Streamer connection
  at any given time from a given user is available". Logging in a second time
  does not raise and does not queue --- it takes the connection away from the
  first. If both sides then reconnect, which any sensible supervisor will, the
  two sessions bump each other indefinitely and each sees a feed that keeps
  dying for no local reason.

  **A second brokerage account is not a way around it.** Streams for two
  different brokerage accounts were measured bumping each other off, each drop
  a clean ``ConnectionClosedOK: 1000`` every few seconds, which a limit per
  account cannot produce. A second app registration is not one either, by the
  wording above, which names the user rather than the credentials; that part
  rests on Schwab's text rather than a measurement.

  This is easy to hit by accident: a research script, a notebook left open, a
  second machine, or a supervised process that restarted while an older one
  was still alive. Nothing in the protocol says which session is the intruder,
  so the symptom appears identically on both.

  If a stream disconnects repeatedly while REST calls on the same token keep
  working, suspect a second subscriber before suspecting the network. Anything
  depending on that feed should treat the condition as a stop rather than a
  warning --- a stream that is being bumped still delivers *some* messages, so
  partial delivery and a healthy quiet market look alike from inside.

  REST is unaffected. ``Client`` calls do not contend with a stream or with
  each other, and can run alongside a streaming session freely.



++++++++++++
Use Overview
++++++++++++

The example above demonstrates the end-to-end workflow for using ``schwaby.stream``.
There's more in there than meets the eye, so let's dive into the details.


----------
Logging In
----------

Before we can perform any stream operations, the client must be logged in to the
stream. Unlike the HTTP client, in which every request is authenticated using a
token, this client sends unauthenticated requests and instead authenticates the
entire stream. As a result, this login process is distinct from the token
generation step that's used in the HTTP client.

Stream login is accomplished simply by calling :meth:`StreamClient.login()`. Once
this happens successfully, all stream operations can be performed. Attempting
to
perform operations that require login before this function is called raises an
exception.

.. autoclass:: schwaby.streaming.StreamClient
.. automethod:: schwaby.streaming.StreamClient.login

Requests to the streaming server -- logging in, subscribing, logging out -- give
up after ``response_timeout`` seconds if the server accepts the request but
never answers it, raising
:class:`~schwaby.streaming.ResponseTimeoutError`. It defaults to 60 seconds and
can be set per client:

.. code-block:: python

  stream_client = StreamClient(client, response_timeout=30.0)

Pass ``None`` to wait indefinitely. Note that the websockets keepalive does not
cover this case: a connection which is alive but simply not answering keeps
replying to pings, so without a timeout such a request would wait forever.


-----------------------
Logging Out and Closing
-----------------------

For a clean exit, it's recommended to log out of the stream when you're done.
This sends a logout request and then closes the connection.

.. automethod:: schwaby.streaming.StreamClient.logout

If the connection has already failed, or you are tearing a client down without
ceremony, :meth:`close <schwaby.streaming.StreamClient.close>` skips the logout
and just closes the socket. It is safe to call more than once, and safe on a
client which was never logged in.

.. automethod:: schwaby.streaming.StreamClient.close

The client is also an async context manager, which closes it on the way out:

.. code-block:: python

  async with StreamClient(client) as stream_client:
      await stream_client.login()
      ...

Closing matters more than it might appear. A client which is never closed leaves
its socket and keepalive task alive until the object is collected, which for a
long-running process means until it exits -- typically leaving the connection to
be finalized during interpreter shutdown, when the event loop may already be
gone.


----------------------
Subscribing to Streams
----------------------

These functions have names that follow the pattern ``SERVICE_NAME_subs``. These
functions send a request to enable streaming data for a particular data stream.
They are *not* thread safe, so they should only be called from one thread.

They can, however, be called while another coroutine is waiting in
``handle_message()``. One request is in flight at a time, and whichever coroutine
is reading the socket hands the response to whoever is waiting for it, so a
subscription made against a quiet stream is sent immediately rather than waiting
for a message to arrive first.

**Nothing waits behind a request's answer.** A ``subs``, ``add`` or ``unsubs``
that reads the socket while it waits for its own answer hands every other
frame it reads to your handlers there and then, as ``handle_message`` would:
each frame once, in the order it arrived, whichever coroutine read it. The
time your handlers take does not count against ``response_timeout``, and a
handler that raises is logged and reported to your error handlers, never
raised out of the ``subs``.

.. note::

  **Register handlers before you subscribe.** A frame that arrives while a
  ``subs`` waits goes to the handlers registered at that moment, not to ones
  added after it returns. A coroutine handler, a heartbeat one included, runs
  as a task, so it may issue stream commands, and so may an error handler: a
  report made during the wait is delivered once the request has finished.
  A synchronous handler cannot await one.

.. _stream_keys:

**The keys.** Each ``subs``, ``add`` and ``unsubs`` takes a list or a tuple of
keys. An empty one raises ``ValueError`` before anything is sent: Schwab was
measured refusing an empty ``NEWS_HEADLINE`` subscription with code 22, and
what it does on other services is unmeasured. A single string raises
``TypeError``, since it would otherwise be sent a letter at a time. A
``fields`` you pass is copied, never changed.

.. note::

  **Batching is off unless you ask.** ``keys_per_request=N``, on the
  :class:`StreamClient` or on one call, sends at most ``N`` keys a request,
  one request after another. A ``subs`` becomes a ``subs`` of the first batch
  and ``add``\ s of the rest, since a second ``subs`` would replace the
  first. A refusal part-way raises with ``sent_keys`` and ``unsent_keys`` on
  the error, and the batches sent before it stand. Schwab documents no limit
  on keys per request; the limits measured are totals per service (ten keys
  on each screener service), which batching does not lift.

.. _subs_vs_add:

Schwab's streamer documentation says a ``subs`` overwrites everything
subscribed before on that service, and an ``add`` adds to it. Three rules
account for every frame in a capture made by subscribing repeatedly and
grouping the results by subscription key, and they agree with that:

**A** ``subs`` **replaces the subscription for its service.** The keys named in
the previous call stop delivering. There is no acknowledgement of the
replacement and no error --- the old data simply ceases.

**An** ``add`` **appends to it.** Both the earlier keys and the added ones
deliver afterwards.

**Services are independent.** A ``subs`` on ``SCREENER_EQUITY`` does not touch
a live ``SCREENER_OPTION`` subscription, and the two go on pushing at their own
rates side by side.

The first rule has a consequence worth stating on its own: **a** ``subs``
**with a key Schwab silently ignores still replaces a working subscription.**
Schwab acknowledges the new key, delivers nothing for it, and the data you
were receiving is gone. Nothing in the protocol distinguishes that from a
market with nothing to report.

.. note::

  Those three rules are structural and held for every frame in the capture.
  Anything quantitative from the same session --- push rates, how much
  membership moves --- is one nine-minute window and is flagged as such where
  it appears; see :ref:`screener_cadence`.

  A frame count is not a liveness signal, incidentally. The key with by far
  the most frames in that capture had them only because it was subscribed
  longest.


-------------------------
Adding Symbols to Streams
-------------------------

These functions have names that follow the pattern ``SERVICE_NAME_add``, and
they append to the current subscription rather than replacing it --- see
:ref:`the subs and add rules <subs_vs_add>` above. Not every service offers
one; :ref:`equity_charts` and :ref:`futures_charts` do.


-------------------------
Un-Subscribing to Streams
-------------------------

These functions have names that follow the pattern ``SERVICE_NAME_unsubs``.
These functions send a request to stop streaming the named symbols for a
particular service. They are *not* thread safe, so they should only be called
from one thread. Symbols you do not name stay subscribed.


--------------------
Registering Handlers
--------------------

By themselves, the subscription functions outlined above do nothing except cause
messages to be sent to the client. The ``add_SERVICE_NAME_handler`` functions
register functions that will receive these messages when they arrive. When
messages arrive, these handlers will be called serially. There is no limit to
the number of handlers that can be registered to a service.


.. _registering_handlers:

-----------------
Handling Messages
-----------------

Once the stream client is properly logged in, subscribed to streams, and has
handlers registered, we can start handling messages. This is done simply by
awaiting on the ``handle_message()`` function. This function reads a single
message and dispatches it to the appropriate handler or handlers.

If a message is received for which no handler is registered, that message is
ignored.

.. automethod:: schwaby.streaming.StreamClient.handle_message

A handler which raises an ``Exception`` does not stop the others from seeing
the message, and does not propagate out of ``handle_message()`` into your
receive loop, where it would be indistinguishable from the connection failing.
The failure is reported on the ``schwaby.streaming`` logger instead, with the
service name attached. This applies equally to synchronous and coroutine
handlers.

That is worth knowing when debugging: **a handler which is quietly failing shows
up in the logs and nowhere else** -- unless you ask for it. See
:ref:`error_handlers` below. If you are relying on a handler to do something
important, do one or the other. :ref:`enable_logging` shows how to turn
logging on.

Handlers should take a single argument representing the stream message received:

.. code-block:: python

  import json

  def sample_handler(msg):
      print(json.dumps(msg, indent=4))


.. _error_handlers:

------------------------------
Reacting to Absorbed Failures
------------------------------

Skipping a failed handler is the right behaviour, but it leaves you with a log
record rather than something you can act on. If your program needs to *react* --
raise an alert, increment a counter, mark a subscription unhealthy -- register
an error handler:

.. code-block:: python

  import reprlib

  def on_stream_error(service, exception, message):
      # service is whatever Schwab sent. reprlib.repr escapes its line breaks
      # as %r would, and also stops at a bounded depth, where a plain repr of a
      # deeply nested value raises inside this handler. %r shows the exception
      # whole: reprlib would cut its message short.
      alert('schwaby stream: %s raised %r'
            % (reprlib.repr(service), exception))

  stream_client.add_error_handler(on_stream_error)

It is called for four things: a stream handler which raised, a late rejection of
a request nobody was waiting on, a connection which failed to close after logout,
and a message this client cannot use at all. :meth:`add_error_handler
<schwaby.streaming.StreamClient.add_error_handler>` below enumerates that last
group; it is the canonical list, and this page does not repeat it.
:class:`UnusableMessage`'s own docstring describes the type rather than
the list, and says so. That group arrives as ``UnusableMessage``, whose
``message``
attribute is the offending value as it arrived --- the sorted list of names,
for an unread channel --- alongside ``cause`` (the
exception which made it unusable, where there was one) and ``count``/``total``
as integers.

Those last ones are **coalesced**: the first three *of each kind* on a
connection, then powers
of ten, with the running count in the message. A systematically malformed
channel produces one per element per tick, so reporting every one would be a
log-volume incident on top of the data outage — and they share a bounded queue
with the late rejections, so a flood would push out the one thing nothing else
will ever report. The count tells you the true scale, and it is kept per kind —
a flood of one sort of malformed message does not silence a different one
arriving beside it.

For the late rejection, the ``UnexpectedResponseCode`` carries the whole
frame — which can hold several responses — so read the rejected one from
``message`` rather than from ``exception.response['response'][0]``. ``service``
and ``message`` are ``None`` where they do not apply --- but do not use that as
a discriminator. ``UnparsableMessage`` carries the frame as it arrived, text or bytes, as
``message`` for exactly this reason. Only the close failure leaves both unset *by design*; an
``UnusableMessage`` reports the containing frame as ``message``, which is
non-``None`` in every case but a top-level JSON ``null``. Test
``isinstance(exception, UnusableMessage)`` before anything else if you branch
on the kind. Registering none is fine: each
failure is handled as this page describes, and the log line is written either
way.

**A frame whose JSON will not decode is the one failure this client does not
absorb.** It is reported to your handler as ``UnparsableMessage`` and *also*
raised, so it still ends your receive loop. Everything else on this page is
skipped and reported; this one is skipped, reported and re-raised.

The difference is what is known about what was missed. A structurally unusable
element can be skipped precisely --- the elements beside it are still delivered
and you are told which one went. A frame which will not parse has unknown
contents, so continuing means accepting a gap of unknown size, and it might have
carried a fill. Ending the loop causes a reconnect and a re-subscribe, which is
the one thing that can recover state.

If your feed hits this often, try
:class:`~schwaby.contrib.util.HeuristicJsonDecoder` before concluding the stream
is broken --- it exists because Schwab really does emit JSON the default parser
rejects, which is evidence for a frame-level quirk rather than a dead
connection.

The late rejection reaches you however Schwab frames it. Only one request is
outstanding at a time, so a second response in a frame cannot be an answer to
anything you are waiting on — it is a late answer to an abandoned request,
whether the server sends it alone or batches it behind the answer to a live one.
Both report the same ``UnexpectedResponseCode`` with the same ``service`` and
``message``, because the framing is the server's choice and you cannot predict
which you will get.

The batched one is not delivered from the code that finds it. That code runs
while the read lock and the request lock are both held and the response deadline
is running, so a slow handler there would turn a subscription that *succeeded*
into a ``ResponseTimeoutError``, and one that re-subscribed would block on a lock
its own caller holds. The report is queued instead, and delivered by whichever
coroutine read the frame once it has released its locks — before
``handle_message`` returns, or before the subscribe that read it returns. Five
consequences:

* **Your handler can be called from inside a subscribe.** A slow one delays that
  call returning; it cannot make it fail, because the response has already been
  matched by then. Either way, keep it short.
* A failed request still delivers what it read. The exception you get describes
  the response that answered *your* request and says nothing about the others
  in the frame. Your handler cannot replace that exception: a ``BaseException``
  raised there is logged and swallowed, because the reason Schwab refused your
  request is far more useful than the reason your handler fell over.
* A **cancelled** request reports nothing. Draining while unwinding a cancel
  would make ``close()``, a ``wait_for`` or a ``TaskGroup`` shutdown block for
  as long as your handler takes. Whatever was queued waits for
  ``handle_message``, or is discarded with the session.
* The queue is cleared by ``close()`` and by a fresh ``login()``, along with any
  frames read but not yet handled. Nothing that arrived on a connection you have
  since replaced reaches you afterwards — not a rejection reported against the
  new session, and not a stale quote delivered to a handler as though it were
  live. That holds whether you closed first or simply logged in again.
* The queue is bounded and drops the oldest when full. That needs reports to
  arrive faster than they are drained, which means something is already wrong —
  and the log line is written either way, so nothing unwritten is lost.

The handler may be a coroutine function, like every other handler on this class:

.. code-block:: python

  async def on_stream_error(service, exception, message):
      await alerts.publish(service, exception)

  stream_client.add_error_handler(on_stream_error)

Where a stream handler failed, ``message`` is the message *as that handler saw
it* — relabeled, if the stream relabels. The exception is the one it raised.

If relabeling itself was what failed, the handler never saw anything, and
``message`` is the message as it arrived, with its numeric field ids intact.

This matters most when something else covers for the stream. If a subscription
quietly stops delivering and a REST poll is authoritative anyway, nothing looks
wrong until something the poll does not cover finally breaks. **A silent failure
that a fallback hides is the one most worth having a signal for.**

An error handler is called for effect. If it raises an ``Exception``, that is
logged and swallowed: a callback for absorbed failures must not itself become a
way to fail. A ``BaseException`` — ``CancelledError`` during shutdown, or
``SystemExit`` — is left to propagate, since swallowing those breaks
cancellation and process exit. Where it propagates to depends on which failure
was being reported: into your receive loop for a synchronous handler, into the
failing handler's own task for an asynchronous one.

**The handler runs inline, and a coroutine one is awaited.** The report finishes
before the call that reported the failure returns. Three consequences worth
knowing:

* **Keep it quick.** It runs on the path that found the failure, so a handler
  that blocks holds up ``handle_message`` — exactly as a slow synchronous
  stream handler does. If you need to do something slow, hand off to a task of
  your own and return.
* An error handler may itself call ``close()`` or ``logout()``. Tearing the
  stream down is a reasonable reaction to a failure, and it is safe however many
  handlers do it, because nothing is waiting on a set of scheduled reports.
* ``close()`` does not wait for stream handlers you have in flight. If an
  ``async`` stream handler is still running when you close, it has not failed
  yet and so has not reported yet. Await your own handler tasks first if you
  need their reports.

.. automethod:: schwaby.streaming.StreamClient.add_error_handler

.. _streaming_exceptions:

~~~~~~~~~~~~~~~~~~~~
Streaming exceptions
~~~~~~~~~~~~~~~~~~~~

The exceptions a stream raises, and the ones it hands to
:meth:`~schwaby.streaming.StreamClient.add_error_handler` instead. The first
three are raised by a request --- a login, a subscribe, a logout ---
and :class:`~schwaby.streaming.UnexpectedResponseCode` also reaches the error
handler when Schwab refuses a request that had already been abandoned.
:class:`~schwaby.streaming.UnparsableMessage` is raised by
:meth:`~schwaby.streaming.StreamClient.handle_message`, or by the request
waiting when the frame arrived, *and* reported.
:class:`~schwaby.streaming.UnusableMessage` is never raised: it is only
reported, so an ``except`` clause for it never runs. Every one inherits
:class:`~schwaby.utils.SchwabError`.

.. autoexception:: schwaby.streaming.ResponseTimeoutError
.. autoexception:: schwaby.streaming.UnexpectedResponse
.. autoexception:: schwaby.streaming.UnexpectedResponseCode
.. autoexception:: schwaby.streaming.StreamLoginRefused
.. autoexception:: schwaby.streaming.StreamNotice
.. autoexception:: schwaby.streaming.UnparsableMessage
.. autoexception:: schwaby.streaming.UnusableMessage

``UnexpectedResponseCode``, and ``StreamLoginRefused`` with it, carries the
refusal's parts read from the element that refused: ``service`` and
``command``, ``code`` (an ``int``, or ``None`` when the frame's is not one) and
``msg`` (a ``str`` or ``None``). The whole frame stays in ``response``.

.. _stream_close:

~~~~~~~~~~~~~~~~~~~~~~~
When the stream ends
~~~~~~~~~~~~~~~~~~~~~~~

A closed connection still raises the websockets exception it always has, a
clean close as ``ConnectionClosedOK``. Beside it, the stream says how it ended:
``last_close`` holds a
:class:`~schwaby.streaming.StreamClose` once a connection has ended, set
before the call that finds the close raises, and reset to ``None`` when a
:meth:`~schwaby.streaming.StreamClient.login` succeeds. A login still in
flight, or cancelled, leaves it as it was. A handler registered with
:meth:`~schwaby.streaming.StreamClient.add_close_handler` is called with it
once for each connection that ends, before that raise, whichever call found
the close; a second call that found it too waits for the handlers before
raising, unless it is that close's own handlers, or a task they started,
finding it again. A failed :meth:`login
<schwaby.streaming.StreamClient.login>` sets ``last_close`` but calls no close
handler: no connection ended, and its caller has the error. One whose
preferences do not read leaves the connection, and ``last_close``, as they
were. A close handler
that logs in again should back off between tries.
Reports from the moments before the close reach the error handlers first. An
error handler that logs in again while they do so does not cancel the close:
its handlers are still called, with that close, though ``last_close`` then
says how that login went: ``None`` if it succeeded, its failure if it did
not. A close handler that reconnects may skip it when ``client.last_close is
None``; in any other case it should reconnect. If the call is cancelled
while the reports go out, a ``wait_for`` around ``handle_message`` say, the
close is left for the next call that finds it, unless a login has succeeded
in the meantime. A login that failed after closing the socket leaves none to
find it: the next call raises that the socket is not open, and the next
login starts afresh.

A close the client makes itself is not said: :meth:`~schwaby.streaming.StreamClient.close`,
:meth:`~schwaby.streaming.StreamClient.logout`, or a new
:meth:`~schwaby.streaming.StreamClient.login` replacing the socket. A
``handle_message`` that was reading the socket a login replaced reads on
from the new one once it is open; one that reads while the login is still
connecting raises, as before. Frames read but not yet handled when
a socket is replaced or closed are delivered first, never dropped.

.. code-block:: python

  def on_close(close):
      if close.kind == 'bumped':
          alert('another login took this Schwab user\'s stream')

  stream_client.add_close_handler(on_close)

A login that fails sets it too, without calling the close handlers:
``login_refused`` when Schwab refuses the
LOGIN, which raises :class:`~schwaby.streaming.StreamLoginRefused` with its
socket closed; ``dead_token`` when the preferences call finds the refresh
token dead, the :class:`~schwaby.utils.TokenRefreshError` propagating as it
always has; ``connection_lost`` for any other failure there.

.. warning::

  **A bump is not yet told apart from Schwab cycling the server.** Measured,
  another login taking the stream closes this one cleanly (1000). Whether
  Schwab sends a notice first, which would make it ``bumped`` rather than
  ``server_closed``, is unmeasured. A notice on the ``ADMIN`` service, or one
  carrying a severing code (12 or 30), reaches the error handlers as a
  :class:`~schwaby.streaming.StreamNotice` and is kept as the close's
  ``notice``.

``last_frame_at`` and
``last_heartbeat_at`` say, as
``time.monotonic()``, when the last frame and the last heartbeat arrived:
plain attributes, safe to read from another thread, and any frame counts,
one that will not parse included. A handler registered with
:meth:`~schwaby.streaming.StreamClient.add_heartbeat_handler` is called with
each heartbeat's value. **Schwab documents no heartbeat interval and none is
measured**, so nothing here decides when a quiet stream is dead; a
threshold is the caller's.

.. automethod:: schwaby.streaming.StreamClient.add_close_handler
.. automethod:: schwaby.streaming.StreamClient.add_heartbeat_handler
.. autoclass:: schwaby.streaming.StreamClose
  :members:
.. autoclass:: schwaby.streaming.StreamCloseKind
  :members:
  :undoc-members:
  :member-order: bysource

.. _data_field_relabeling:

---------------------
Data Field Relabeling
---------------------

Under the hood, this API returns JSON objects with numerical key representing
labels:

.. note::

  The example below relabels every key, because every id in it is one this
  version knows. **An id it does not know stays numeric** and is delivered to
  your handler as it arrived --- see :ref:`unknown field ids
  <unknown_field_ids>`. If you are building a replay fixture or a test double
  for this library, copy the shape a handler *receives* rather than the wire
  format: a double that emits raw numeric keys rehearses against something
  ``schwaby`` never produces.

.. code-block:: python

  {
    "service": "CHART_EQUITY",
    "timestamp": 1715908546054,
    "command": "SUBS",
    "content": [{
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
      }]
  }

These labels are tricky to decode, and require a knowledge of the documentation
to decode properly. ``schwaby`` makes your life easier by doing this decoding
for you, replacing numerical labels with names proposed by the community. The
message above is delivered as:

.. code-block:: python

  {
    "service": "CHART_EQUITY",
    "timestamp": 1715908546054,
    "command": "SUBS",
    "content": [{
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
      }]
  }

This documentation describes the various fields and their numerical values. You
can find them by investigating the various enum classes ending in ``***Fields``.

.. warning::

   **Relabeling is not applied uniformly.** Schwab delivers messages on two
   channels. Content arriving on the ``data`` channel is relabeled as above.
   Content arriving on the ``notify`` channel is passed to your handler
   **unchanged**, with its bare numeric field ids intact.

   Both reach the same handlers, so a handler which assumes relabeling will
   mis-parse a notify frame -- and it will do so by finding nothing rather than
   by raising, since the keys it looks for are simply absent. If you read fields
   by name, check they are present rather than assuming them.

   This is a property of the library, not of Schwab's protocol.

Some streams let you ask for a subset of the fields. The five
:ref:`level_one` subscription functions take a list of the appropriate field
enums as an extra ``fields`` argument; omit it and every supported field is
requested. No other service accepts one. To change the fields of a
subscription that is already running, rather than at subscribe time, see
:ref:`the note on VIEW <level_one_view>`. Every field enum has an
``all_fields()``:

.. automethod:: schwaby.streaming::StreamClient.LevelOneEquityFields.all_fields


---------------
Stream Statuses
---------------

Schwab's streaming functionality is closely modelled on that of the former
TDAmeritrade API, and this module is adapted from an implementation written
against it.

As a result, some streams here may not actually work. Some are known only from
documentation that no longer exists.

Which of them still work is not documented anywhere, so it is worked out by
trying them. If you find one that behaves differently from what is described
here, please report it `on the issue tracker
<https://github.com/tom-hirt/schwaby/issues>`__.

The following streams are confirmed working:
 * :ref:`charts`
 * :ref:`level_one`
 * :ref:`level_two`
 * :ref:`screener`
 * :ref:`news_headline` (``SUBS`` and ``ADD``; ``UNSUBS`` acknowledged, its
   effect unconfirmed)
 * :ref:`account_activity`


.. _charts:

++++++++++++
OHLCV Charts
++++++++++++

These streams summarize trading activity on a minute-by-minute basis for
equities and futures, providing OHLCV (Open/High/Low/Close/Volume) data.


.. _equity_charts:

-------------
Equity Charts
-------------

Minute-by-minute OHLCV data for equities.

.. automethod:: schwaby.streaming::StreamClient.chart_equity_subs
.. automethod:: schwaby.streaming::StreamClient.chart_equity_unsubs
.. automethod:: schwaby.streaming::StreamClient.chart_equity_add
.. automethod:: schwaby.streaming::StreamClient.add_chart_equity_handler
.. autoclass:: schwaby.streaming::StreamClient.ChartEquityFields
  :members:
  :undoc-members:


.. _futures_charts:

--------------
Futures Charts
--------------

Minute-by-minute OHLCV data for futures.

.. automethod:: schwaby.streaming::StreamClient.chart_futures_subs
.. automethod:: schwaby.streaming::StreamClient.chart_futures_unsubs
.. automethod:: schwaby.streaming::StreamClient.chart_futures_add
.. automethod:: schwaby.streaming::StreamClient.add_chart_futures_handler
.. autoclass:: schwaby.streaming::StreamClient.ChartFuturesFields
  :members:
  :undoc-members:


.. _level_one:

++++++++++++++++
Level One Quotes
++++++++++++++++

Level one quotes provide an up-to-date view of bid/ask/volume data. In
particular they list the best available bid and ask prices, together with the
requested volume of each. They are updated live as market conditions change.

.. _level_one_view:

**Changing the fields of a live subscription.** Each level one service has a
``*_view`` method that sends Schwab's ``VIEW`` command, which asks Schwab to
change the fields delivered without resubscribing. Resubscribing to change
fields is the alternative, and a ``SUBS`` replaces the service's whole symbol
list.

.. note::

  **A VIEW applies to every symbol in the service, whatever symbols it
  names.** Schwab documents it that way, and it was measured against a live
  account: two futures and two currency pairs were subscribed without a field,
  and a VIEW naming only the first of each pair added it to both. So the view
  methods take no symbols, and send none.

  The well-evidenced measurement used a VIEW that named a symbol: a field was
  added to every symbol across dozens of frames, on futures and on forex, and
  a field was added on futures options too. A VIEW sent without symbols, as
  these methods send it, was seen on only four frames, on futures and forex,
  and what it brought back was ``LAST_PRICE``, which the keyed VIEW before it
  had removed. That removal is the weakly evidenced direction, because level
  one frames carry only the fields that changed, so its return shows little.
  On equities and options Schwab accepted a VIEW
  but delivered nothing afterwards to show its effect. So that these methods
  change the fields is expected, from Schwab's description and from the keyed
  measurement, rather than established for the form they send. A successful
  VIEW answered ``code 0``, not the ``29`` Schwab documents.

  Whether an ``add`` sent after a VIEW brings back every field for the whole
  service has not been measured. An ``add`` without ``fields`` asks for all of
  them, so pass the same fields to it if that matters to you.

  The other services are not offered a VIEW. Schwab answers ``code 0`` on
  every other service it lists VIEW for, but a VIEW asking for fewer fields
  changed nothing on ``NASDAQ_BOOK``, ``NYSE_BOOK`` and ``CHART_EQUITY``,
  measured before the open, or on either screener, measured overnight: every
  field kept arriving.
  Those measurements named symbols. ``OPTIONS_BOOK`` and ``CHART_FUTURES``
  delivered nothing to measure. Those services' subscribe methods do not
  choose fields either.


.. _level_one_quotes_stream:

---------------
Equities Quotes
---------------

Level one quotes for equities traded on NYSE, AMEX, and PACIFIC.

.. _level_one_equity_fractional:

.. warning::

  **A print of less than one share has a LAST_SIZE of 0.** ``LAST_SIZE`` and
  ``TOTAL_VOLUME`` carry whole shares, and the fraction is dropped rather than
  rounded, silently. Schwab's field table stops at field ``51``; asked for
  more, it delivers fields ``52`` to ``56``, and two of those carry the
  fractions. Measured against a live account before the open: the whole part
  of ``LAST_SIZE_FRACTIONAL`` equalled ``LAST_SIZE`` on 299 of 299 items, 81
  of them a print under one share that read ``0``, and the whole part of
  ``TOTAL_VOLUME_FRACTIONAL`` equalled ``TOTAL_VOLUME`` on 435 of 435.

  **Read the fractional fields, not the whole-share ones.** An update carries
  only the fields that changed, so when only the fraction moves the
  whole-share field is left out: ``LAST_SIZE_FRACTIONAL`` arrived without
  ``LAST_SIZE`` on 56 of 355 items, and ``TOTAL_VOLUME_FRACTIONAL`` without
  ``TOTAL_VOLUME`` on 251 of 686. A handler that takes the presence of
  ``LAST_SIZE`` to mean a trade printed misses those trades.

  A subscription that does not name its fields asks for all of them, and gets
  these. One that names its fields gets them only by naming them. None of the
  five is documented by Schwab, so they can change or stop without notice;
  what each carried is on its member below.

.. automethod:: schwaby.streaming::StreamClient.level_one_equity_subs
.. automethod:: schwaby.streaming::StreamClient.level_one_equity_unsubs
.. automethod:: schwaby.streaming::StreamClient.level_one_equity_add
.. automethod:: schwaby.streaming::StreamClient.level_one_equity_view
.. automethod:: schwaby.streaming::StreamClient.add_level_one_equity_handler
.. autoclass:: schwaby.streaming::StreamClient.LevelOneEquityFields
  :members:
  :undoc-members:


.. _level_one_option_stream:

--------------
Options Quotes
--------------

Level one quotes for options. Note you can use
:meth:`Client.get_option_chain() <schwaby.client.Client.get_option_chain>` to fetch
available option symbols.

.. automethod:: schwaby.streaming::StreamClient.level_one_option_subs
.. automethod:: schwaby.streaming::StreamClient.level_one_option_unsubs
.. automethod:: schwaby.streaming::StreamClient.level_one_option_add
.. automethod:: schwaby.streaming::StreamClient.level_one_option_view
.. automethod:: schwaby.streaming::StreamClient.add_level_one_option_handler
.. autoclass:: schwaby.streaming::StreamClient.LevelOneOptionFields
  :members:
  :undoc-members:


.. _level_one_futures_stream:

--------------
Futures Quotes
--------------

Level one quotes for futures.

.. automethod:: schwaby.streaming::StreamClient.level_one_futures_subs
.. automethod:: schwaby.streaming::StreamClient.level_one_futures_unsubs
.. automethod:: schwaby.streaming::StreamClient.level_one_futures_add
.. automethod:: schwaby.streaming::StreamClient.level_one_futures_view
.. automethod:: schwaby.streaming::StreamClient.add_level_one_futures_handler
.. autoclass:: schwaby.streaming::StreamClient.LevelOneFuturesFields
  :members:
  :undoc-members:


.. _level_one_futures_options_stream:

----------------------
Futures Options Quotes
----------------------

Level one quotes for futures options.

.. automethod:: schwaby.streaming::StreamClient.level_one_futures_options_subs
.. automethod:: schwaby.streaming::StreamClient.level_one_futures_options_unsubs
.. automethod:: schwaby.streaming::StreamClient.level_one_futures_options_add
.. automethod:: schwaby.streaming::StreamClient.level_one_futures_options_view
.. automethod:: schwaby.streaming::StreamClient.add_level_one_futures_options_handler
.. autoclass:: schwaby.streaming::StreamClient.LevelOneFuturesOptionsFields
  :members:
  :undoc-members:


.. _level_one_forex_stream:

------------
Forex Quotes
------------

Level one quotes for foreign exchange pairs.

.. automethod:: schwaby.streaming::StreamClient.level_one_forex_subs
.. automethod:: schwaby.streaming::StreamClient.level_one_forex_unsubs
.. automethod:: schwaby.streaming::StreamClient.level_one_forex_add
.. automethod:: schwaby.streaming::StreamClient.level_one_forex_view
.. automethod:: schwaby.streaming::StreamClient.add_level_one_forex_handler
.. autoclass:: schwaby.streaming::StreamClient.LevelOneForexFields
  :members:
  :undoc-members:


.. _level_two:

++++++++++++++++++++
Level Two Order Book
++++++++++++++++++++

Level two streams provide a view on continuous order books of various securities.
The level two order book describes the current bids and asks on the market, and
these streams provide snapshots of that state.

Due to the lack of official documentation, these streams are largely reverse
engineered. While the labeled data represents a best-effort attempt to
interpret stream fields, it's possible that something is wrong or incorrectly
labeled.

The documentation lists more book types than are implemented here. In
particular, it also lists ``FOREX_BOOK``, ``FUTURES_BOOK``, and
``FUTURES_OPTIONS_BOOK`` as accessible streams. All experimentation has resulted
in these streams refusing to connect, typically returning errors about
unavailable services. Due to this behavior and the lack of official
documentation for book streams generally, ``schwaby`` assumes these streams are not
actually implemented, and so excludes them. If you have any insight into using
them, please `let us know <https://github.com/tom-hirt/schwaby/issues>`__.


-------------------------------------
Equities Order Books: NYSE and NASDAQ
-------------------------------------

``schwaby`` supports level two data for NYSE and NASDAQ, which are the two major
exchanges dealing in equities, ETFs, etc. Stocks are typically listed on one or
the other, and it is useful to learn about the differences between them:

 * `"The NYSE and NASDAQ: How They Work" on Investopedia
   <https://www.investopedia.com/articles/basics/03/103103.asp>`__
 * `"Here's the difference between the NASDAQ and NYSE" on Business Insider
   <https://www.businessinsider.com/heres-the-difference-between-the-nasdaq-and-nyse-2017-7>`__
 * `"Can Stocks Be Traded on More Than One Exchange?" on Investopedia
   <https://www.investopedia.com/ask/answers/05/stockmultipleexchanges.asp>`__

You can identify on which exchange a symbol is listed by using
:meth:`Client.get_instruments() <schwaby.client.Client.get_instruments>`:

.. code-block:: python

  import httpx2

  from schwaby.client import Client

  r = client.get_instruments(
          ['GOOG'], projection=Client.Instrument.Projection.FUNDAMENTAL)
  assert r.status_code == httpx2.codes.OK, r.raise_for_status()
  print(r.json())

However, many symbols have order books available on these streams even though
this API call returns neither NYSE nor NASDAQ. The only sure-fire way to find out
whether the order book is available is to attempt to subscribe and see what
happens.

Note that, to match what little documentation exists, the NYSE book is called
"listed". Testing indicates this stream corresponds to the NYSE book; if you
see behaviour suggesting otherwise, please
`let us know <https://github.com/tom-hirt/schwaby/issues>`__.

.. automethod:: schwaby.streaming::StreamClient.nyse_book_subs
.. automethod:: schwaby.streaming::StreamClient.nyse_book_unsubs
.. automethod:: schwaby.streaming::StreamClient.nyse_book_add
.. automethod:: schwaby.streaming::StreamClient.add_nyse_book_handler

.. automethod:: schwaby.streaming::StreamClient.nasdaq_book_subs
.. automethod:: schwaby.streaming::StreamClient.nasdaq_book_unsubs
.. automethod:: schwaby.streaming::StreamClient.nasdaq_book_add
.. automethod:: schwaby.streaming::StreamClient.add_nasdaq_book_handler


------------------
Options Order Book
------------------

This stream provides the order book for options. It's not entirely clear what
exchange it aggregates from, but it's been tested to work and deliver data. The
leading hypothesis is that it is the order book for the
`Chicago Board of Exchange <https://www.cboe.com/us/options>`__ options
exchanges, though that is a guess and not an informed one.

.. automethod:: schwaby.streaming::StreamClient.options_book_subs
.. automethod:: schwaby.streaming::StreamClient.options_book_unsubs
.. automethod:: schwaby.streaming::StreamClient.options_book_add
.. automethod:: schwaby.streaming::StreamClient.add_options_book_handler


.. _book_fields:

---------------------
Book Message Contents
---------------------

All three book streams deliver the same shape, and it is nested where the other
services are flat. A message has a symbol, a timestamp, and two lists of price
levels; each level carries its total size and a further list breaking that size
down by exchange:

.. code-block:: python

  {
      'SYMBOL': 'GOOG',
      'BOOK_TIME': 1757088000000,
      'BIDS': [
          {
              'BID_PRICE': 207.42,
              'TOTAL_VOLUME': 900,
              'NUM_BIDS': 3,
              'BIDS': [
                  {'EXCHANGE': 'NSDQ', 'BID_VOLUME': 500,
                   'QUOTE_TIME': 43082150},
                  {'EXCHANGE': 'ARCA', 'BID_VOLUME': 400,
                   'QUOTE_TIME': 43104660},
              ],
          },
          ...
      ],
      'ASKS': [...],   # the same, with ASK_PRICE, NUM_ASKS and ASK_VOLUME
  }

.. warning::

  **The per-exchange quote time is milliseconds since midnight Eastern, not
  epoch milliseconds.** Every other time in a book frame is epoch
  milliseconds, ``BOOK_TIME`` in the same frame included, so the natural
  reading is the wrong one and it fails silently: passed to a function
  expecting epoch milliseconds, a quote from this morning becomes a moment in
  January 1970, and nothing raises.

  To get an instant, add it to midnight Eastern of the frame's own day, and if
  that lands after the frame's ``BOOK_TIME``, use the day before instead. The
  day belongs to the value rather than to the arrival: in one overnight session
  a quote made at 23:55 and delivered at 00:02 needs the previous day, while
  one made at 00:29 and delivered a minute later needs the current one, so no
  rule keyed on when the frame arrived is right for both. Measured during
  daylight saving, so whether the value follows it or a fixed offset is not
  established: the two agree all summer and differ by an hour in winter.

  Measured on ``NASDAQ_BOOK`` and ``NYSE_BOOK`` against a live account, before
  the open: all 8,904 per-exchange entries carried a value from inside the hour
  before the frame arrived, with a median of one to two minutes. **It is not a
  freshness signal.** One participant's quote was still being re-sent half an
  hour after it was made, and was still arriving in the last frame before the
  subscription was dropped, so nothing here bounds how stale one can be. A
  quote sitting untouched that long is likelier before the open, which is when
  this was measured, than inside the session. ``OPTIONS_BOOK`` shares these two
  field tables and delivered no per-exchange entries when tried, so none of
  this was measured there.

  It answers to ``QUOTE_TIME``. Index 2 is not a sequence number: the venue's
  own table calls it the quote time for this market maker's quote, and the
  measurement agrees with the table.

Note that ``BIDS`` names two different things one level apart: the list of price
levels, and the per-exchange breakdown inside each level. That is Schwab's
numbering, kept as-is rather than renamed, so the labels match the field
numbers if you ever have to read a raw frame.

As with everything on this page, these labels are reverse engineered --- see
the caveat at the top of :ref:`level_two`. Field *numbers* come from Schwab;
the names attached to them are a best-effort reading.

.. autoclass:: schwaby.streaming::StreamClient.BookFields
  :members:
  :undoc-members:

.. autoclass:: schwaby.streaming::StreamClient.BidFields
  :members:
  :undoc-members:

.. autoclass:: schwaby.streaming::StreamClient.AskFields
  :members:
  :undoc-members:

.. autoclass:: schwaby.streaming::StreamClient.PerExchangeBidFields
  :members:
  :undoc-members:

.. autoclass:: schwaby.streaming::StreamClient.PerExchangeAskFields
  :members:
  :undoc-members:


.. _screener:

++++++++
Screener
++++++++

Top 10 advances and decliners by volume, trades, percent change and average percent
volume.

.. note::

  **A screener push is the whole list, not a change to it.** Schwab classifies
  ``SCREENER_EQUITY`` and ``SCREENER_OPTION`` as *Whole* services, unlike the
  ``LEVELONE_*`` services which send only what changed. Every message carries
  the complete ranked list as it stands, so a symbol missing from one push
  means "not in this snapshot" rather than "removed" --- diffing consecutive
  pushes into additions and removals invents events that Schwab never sent.

  Each push is also self-describing: ``SORT_FIELD`` and ``FREQUENCY`` are
  present in every message, so the active criteria never have to be inferred
  from what was subscribed.

**The key is not a stock symbol.** It is ``(PREFIX)_(SORTFIELD)_(FREQUENCY)``
--- passing a ticker subscribes to nothing and reports no error.

(PREFIX)_(SORTFIELD)_(FREQUENCY) where PREFIX is:
 * Indices: $COMPX, $DJI, $SPX, INDEX_ALL
 * Exchanges: NYSE, NASDAQ, OTCBB, EQUITY_ALL
 * Option: OPTION_PUT, OPTION_CALL, OPTION_ALL

and sortField is:
 * VOLUME, TRADES, PERCENT_CHANGE_UP, PERCENT_CHANGE_DOWN, AVERAGE_PERCENT_VOLUME

and frequency is:
 * 0, 1, 5, 10, 30, 60 minutes (0 is for all day)

.. danger::

  **Schwab does not reject a malformed key. It acknowledges success and then
  sends nothing, indefinitely.** Measured against a live account:
  ``NOT_A_PREFIX_VOLUME_5``, ``NASDAQ_NOT_A_SORT_5``, ``NASDAQ_VOLUME_7`` and
  the bare ticker ``AAPL`` each returned ``code: 0, "SUBS command
  succeeded"`` and delivered zero frames.

  So there is no error to catch and no exception to see. A typo in a key is
  indistinguishable, from inside your program, from a quiet market --- and a
  screener that legitimately has nothing to report looks exactly the same.
  Validate keys before subscribing rather than after.

  This is why the prefix above matters. ``$SPX.X`` is not a valid prefix:
  over REST it is an HTTP 400, and on the stream it is accepted and silent
  forever.

.. warning::

  **Subscribe ten keys or fewer: ten is the most either screener's
  subscription took.** An eleventh is refused with response code 19, which
  reaches you as ``UnexpectedResponseCode``, whose text carries Schwab's own
  message. For eleven keys, verbatim:

  .. code-block:: text

    You've reached the maximum number of symbols allowed.  (SCREENER_EQUITY=10, DISCARDED=1)

  The parenthetical names the cap and a count of the keys past it.
  ``SCREENER_OPTION`` refuses the same way, with its own name in the
  parenthetical: ten keys accepted, eleven ``DISCARDED=1``, twelve
  ``DISCARDED=2``.

  **The boundary was walked rather than inferred.** Ten keys are accepted,
  ``code 0``. Eleven are refused, and the refusal counts the excess exactly:
  ``DISCARDED=1`` for eleven keys, ``DISCARDED=2`` for twelve. So ten is the
  cap itself and not a number read off one refusal's arithmetic, on both
  screeners. Measured against a live account: the equity walk with nothing
  else subscribed, the option walk alongside a news subscription. Sixty
  identical twelve-key equity refusals in ordinary use, not under a probe
  corroborate the equity figure.

  **The ten belongs to this service, not to the connection.** Schwab's table
  calls code 19 ``REACHED_SYMBOL_LIMIT``, "Subscribe or Add command has
  reached a *total* subscription symbol limit", and *total* is not what was
  measured. Twelve screener keys drew the identical ``DISCARDED=2`` on a
  connection already carrying twenty-five ``LEVELONE_EQUITIES`` symbols ---
  which were themselves never refused --- so the screener's own budget was
  still ten while twenty-five were in use elsewhere. A connection-wide budget
  of ten cannot do that. What code 19 means for the other services which
  share it is a separate question, and unmeasured.

  **This is not the ten in "top 10" above.** That is how many symbols a push
  ranks, and it is a property of the payload. This one is how many keys you
  may subscribe to, and it is a property of this service's subscription.

  **A refused SUBS is not an empty subscription.** Twelve ``SCREENER_EQUITY``
  keys drew code 19 and ``DISCARDED=2``, and then the first ten keys sent, in
  the order sent, delivered pushes, every one of them. So the exception you
  caught means two keys were dropped, not that nothing is subscribed, and a
  handler keeps receiving pushes for the ten. Which ten was seen for one order
  of keys; it is what happened, not a rule Schwab states. ``ADD`` spends the
  same ten: eight keys subscribed and three added drew ``DISCARDED=1``, and the
  first two added delivered. The keys past the cap delivered nothing, but
  neither was ever subscribed inside it, so their silence cannot tell a dropped
  key from a quiet one. Subscribe ten or fewer and the question does not
  arise.

.. note::

  **A refused screener SUBS is answered twice, and the one refused ADD was
  too.** In ordinary live use, not under a probe, sixty code-19 refusals were each
  followed within the same second by a second response to the same request:
  ``code: 0, "SUBS command succeeded"`` --- sixty rejections, sixty late
  successes, one-to-one. A probe that went on reading after each refusal saw
  the same pairing on nine of nine, on both screeners, each within a tenth of a
  second. One of the nine was an ``ADD``, on the equity screener, answered
  ``"ADD command succeeded"``. Expect it after a ``SUBS``, and probably after
  an ``ADD``.

  Only one request is outstanding at a time, so the code-19 frame has already
  resolved your ``subs`` or ``add`` --- by raising --- and the late success
  answers a request nothing is waiting on. It is logged at INFO by the orphan
  path the next time you call ``handle_message``, and is *not* reported to
  :meth:`add_error_handler <schwaby.streaming.StreamClient.add_error_handler>`,
  which reports a late *rejection* but not a late success. A ``SUBS command
  succeeded`` or ``ADD command succeeded`` after a code-19 raise is therefore
  expected, and is not this client losing a response.

  It cannot damage the request after it either. A response carrying an id this
  client issued earlier goes to the orphan path rather than to whichever
  request is in flight, so the next subscribe still receives its own answer;
  handing it over instead would fail an innocent request and leave its real
  answer queued for the one after that.

.. note::

  ``AVERAGE_PERCENT_VOLUME`` is documented by Schwab and accepted by the
  stream, and delivers nothing --- ``SUBS command succeeded`` followed by
  zero frames, the same signature as an invalid key. The other four sort
  fields all return distinct populations. Treat it as unavailable rather
  than as something you are using incorrectly.

.. _screener_cadence:

-------------------------------
What was measured, and how much
-------------------------------

Schwab documents none of the following. It comes from a single capture ---
one nine-minute window, midday on a normal trading session, one account ---
so treat the numbers as an order of magnitude rather than a specification,
and re-measure if you are going to depend on them.

**The frequency selects a time bucket, not a push rate.** Subscribing at
``_0``, ``_1``, ``_5`` and ``_60`` gave four different rankings and the same
push interval. The bucket changes *what* is ranked; it does not change how
often you hear about it.

**Push interval is a property of the service.** ``SCREENER_EQUITY`` pushed
about every 10 seconds and ``SCREENER_OPTION`` about every 5, at every
frequency tried. If you subscribe to both, expect the two rates interleaved
--- a stall detector that assumes one rate will misjudge the other.

**A subscription answers immediately, then falls onto the server's clock.**
The first frame arrived within 0.03 seconds of the ``SUBS`` acknowledgement
in eleven of fourteen subscriptions. The interval that follows it is a
partial cycle, so discard it before averaging anything.

**Frequency** ``0`` **is what the REST endpoint returns.** A stream
subscription at ``NASDAQ_VOLUME_0`` and a
:meth:`~schwaby.client.Client.get_movers` call 70 seconds apart produced the
same ten symbols in the same order. The other buckets shared only four to six
of those ten. So :meth:`~schwaby.client.Client.get_movers` is not a different
view of the data --- it is this service's whole-session bucket, and it is the
only one of the six that REST exposes.

Both the equity and option screener streams use a common set of fields:

.. autoclass:: schwaby.streaming::StreamClient.ScreenerFields
  :members:
  :undoc-members:


---------------
Screener Equity
---------------

.. automethod:: schwaby.streaming::StreamClient.screener_equity_subs
.. automethod:: schwaby.streaming::StreamClient.screener_equity_unsubs
.. automethod:: schwaby.streaming::StreamClient.screener_equity_add
.. automethod:: schwaby.streaming::StreamClient.add_screener_equity_handler


---------------
Screener Option
---------------

.. automethod:: schwaby.streaming::StreamClient.screener_option_subs
.. automethod:: schwaby.streaming::StreamClient.screener_option_unsubs
.. automethod:: schwaby.streaming::StreamClient.screener_option_add
.. automethod:: schwaby.streaming::StreamClient.add_screener_option_handler


.. _news_headline:

+++++++++++++
News Headline
+++++++++++++

Headlines for the symbols you subscribe to, from sources including Reuters,
MT Newswire and GlobeNewswire.

.. warning::

  **Schwab does not document this service.** Its streamer documentation lists
  thirteen services and none of them is news. ``NEWS_HEADLINE`` was found by
  subscribing to it: Schwab answers ``code: 0`` and then delivers headlines.
  Nothing on this page about it comes from Schwab: what is here was measured
  against a live account, and where it was not, the page says so. An
  undocumented service can change or stop without notice. Do not make it the
  only way your program learns about news.

``schwab-py`` 1.0.0 to 1.2.1 carried a news enum written for TD Ameritrade,
whose numbering Schwab does not follow. The timestamp, the identifiers, the
headline and the source each arrive one field lower than that enum put them;
Schwab's field ``6`` is a list where the enum's ``COUNT_FOR_KEYWORD``, one
higher, expected a count; and Schwab's fields ``10`` and ``11`` have nothing
one higher to correspond to. Relabeled with it, the fields get the wrong
names; use :class:`~schwaby.streaming.StreamClient.NewsHeadlineFields`.

**The fields you ask for are ignored.** A subscription asking for a subset of
the fields was delivered all eleven, so the subscribe methods here take no
``fields`` argument. The subscribed symbol arrives as the item's ``key``, not
as field ``0``.

**A story arrives several times.** A copy arrives under each subscribed
symbol it names, each with its own ``key`` and ``seq`` and the same
identifiers. Of 191 identifiers, 184 arrived that way; the seven that reached
only some of their symbols all came in the burst on subscribing, described
below. Copies also arrived again under the same symbol: 100 identifiers
arrived more than once and 43 more than once under one symbol, 40 of those 43
differing only in ``CATEGORIES`` or ``SYMBOLS``. A handler that does not
deduplicate will see most stories repeatedly.

**Deduplicating on the identifier alone can drop a change.** Two identifiers
were sent again with different content: one under a minute later with a
different headline and text and a new timestamp, the other with a new timestamp
and the same headline and text. Keying on the identifier together with
``TIMESTAMP`` and ``HEADLINE`` kept both versions of each and removed every
other repeat in that capture. Two things that key gives up: a repeat can carry
a longer ``SYMBOLS`` list than the copy kept, and without ``key`` in it the
copies under other subscribed symbols are dropped, which a handler that routes
by symbol needs.

**The same story can arrive again in another language.** A translation carries
its own identifiers and a different ``LANGUAGE``, so deduplicating on the
identifiers alone does not remove it. Filter on ``LANGUAGE`` if you want one:
fourteen languages arrived, and fewer than half of the items were
``'en-US'``.

.. warning::

  **TEXT is not the story.** Across both captures, 449 items, it was never
  longer than 100 characters, and 440 of the 449 were exactly 100 --- the shape
  of a limit, not of how long stories are. A program that stores it as the
  article keeps a fragment and nothing says so. What further text exists, and
  how to get it, is not established.

What ``UNKNOWN_3`` and ``UNKNOWN_8`` mean is not established. ``UNKNOWN_8``
was ``False`` in every item captured; ``UNKNOWN_3`` was ``'A'`` in all but one,
which carried ``'D'``.

``SUBS`` and ``ADD`` each delivered one item for every symbol straight away,
some of them up to a day old by ``TIMESTAMP``, so expect a burst on subscribing
that is not new news. ``UNSUBS`` was answered ``code: 0``; that nothing more
arrives for the symbol afterwards is not established, because in the three
minutes watched only one headline arrived for any symbol.

How many headlines to expect was measured once. Thirty widely held symbols
drew 416 items with 191 distinct identifiers in eighty minutes of one morning,
thirty of those items the burst on subscribing. Other symbols and hours were
not measured.

.. autoclass:: schwaby.streaming::StreamClient.NewsHeadlineFields
  :members:
  :undoc-members:

.. automethod:: schwaby.streaming::StreamClient.news_headline_subs
.. automethod:: schwaby.streaming::StreamClient.news_headline_unsubs
.. automethod:: schwaby.streaming::StreamClient.news_headline_add
.. automethod:: schwaby.streaming::StreamClient.add_news_headline_handler


.. _account_activity:

++++++++++++++++
Account Activity
++++++++++++++++

.. automethod:: schwaby.streaming::StreamClient.account_activity_sub
.. automethod:: schwaby.streaming::StreamClient.account_activity_unsubs
.. automethod:: schwaby.streaming::StreamClient.add_account_activity_handler
.. autoclass:: schwaby.streaming::StreamClient.AccountActivityFields
  :members:
  :undoc-members:

.. autoattribute:: schwaby.streaming::StreamClient.ACCOUNT_ACTIVITY_MESSAGE_TYPES

----------------------------------------
What the payload looks like, as observed
----------------------------------------

The three fields above are relabeled for you. What is *inside* ``MESSAGE_DATA``
is not documented by Schwab, so every consumer ends up reverse-engineering it
and keeping the results privately.

What follows was collected by watching a live ``ACCT_ACTIVITY`` feed over
roughly a year. **It is an observation log, not a contract.** Schwab publishes
none of this, nothing here is validated by this library, and a shape that has
not been seen is not thereby impossible. Treat it as a map drawn by someone who
has been there, not as a specification.

.. _confidence_tags:

Some of it we are not sure about, and those are marked. Two tags appear below
and on the other pages:

**Unconfirmed** --- reasoned from a specification or from how the data must be
laid out, and never actually seen on a feed. We handle it because the failure
would be silent, not because we have met it.

**Seen once, not reproduced** --- observed and written down at the time, but not
reliably reproducible since, so we cannot tell you when it happens.

**If you can confirm or refute either kind, please**
`open an issue <https://github.com/tom-hirt/schwaby/issues>`__. A single
message saying "I see this" or "my traffic contradicts it" settles a question
that no amount of reasoning here will, and account and asset mix differ enough
that someone else's feed is genuinely different evidence. Being told we are
wrong is the point of publishing these rather than keeping them.

**The order identifier has been observed under one spelling:**
``SchwabOrderID``. No other spelling has been seen in any capture. An order id
from the REST API does not come from a payload at all --- it is in the
``Location`` header of the :meth:`place_order
<schwaby.client.Client.place_order>` response, which
:ref:`extract_order_id <extract_order_id>` reads.

Other keys hold the ids of *related* orders, and their names end in that same
spelling: ``ParentSchwabOrderID``, ``LifecycleSchwabOrderID`` and
``LegParentSchwabOrderID`` have been captured on the items of a
:ref:`price change <account_activity_price_change>`. A search for any key
ending in ``SchwabOrderID`` can return one of those rather than the order's own
id. Match the key exactly.

**The symbol has been observed under three keys.** ``Symbol`` appears on
``OrderCreated``, ``OrderAccepted``, ``ExecutionRequested`` and
``OrderFillCompleted``. ``PrimaryMarketSymbol`` and ``UnderlyingSymbol``
appear only on ``OrderCreated``, where each order leg's ``Security`` block
carries all three keys together. ``OrderCreated`` also carries a second
``Symbol`` in each leg's ``QuoteOnOrderAcceptance`` block, so a search by key
name finds two. ``ChangeCreated`` carries a ``QuoteOnOrderAcceptance``
container too; whether it holds a ``Symbol`` has not been checked. A lowercase
``symbol`` has never been seen.

.. code-block:: python

  ('Symbol', 'PrimaryMarketSymbol', 'UnderlyingSymbol')

**Several messages carry no symbol at all.** ``ExecutionRequestCreated``,
``ExecutionRequestCompleted``, ``CancelAccepted``, ``OrderUROutCompleted`` and
``ExecutionCreated`` have none of these keys at any depth, so attributing one
to an instrument means joining on the order id --- and a symbol lookup there
returns nothing rather than raising. The last three come from a small sample
on a single account.

The symbol findings above come from equity captures. Option orders have also
been captured --- see :ref:`price changes <account_activity_price_change>` ---
but what these keys hold on one has not been observed.

**Order statuses that end an order** are exported as
:attr:`Client.Order.TERMINAL_STATUSES
<schwaby.client.Client.Order.TERMINAL_STATUSES>`:

.. code-block:: python

  ('FILLED', 'REJECTED', 'CANCELED', 'EXPIRED', 'REPLACED')

Note ``CANCELED`` with one L. These are values of the REST ``status`` field ---
what :meth:`get_order <schwaby.client.Client.get_order>` returns --- and the
``MESSAGE_TYPE`` tokens below are a different vocabulary that does not map onto
them one for one. ``FILLED``, ``CANCELED`` and ``REJECTED`` have been observed
ending an order, and ``REPLACED`` ending each id that a
:ref:`price change <account_activity_price_change>` retired --- five of five,
on an option order. No equity order's replacement has been captured.
``EXPIRED`` is :ref:`Unconfirmed <confidence_tags>`: included by reading, and
never captured.

``MESSAGE_TYPE`` **tokens observed.** These are the tokens **as they appear on
the wire**, and most of them are CamelCase. ``SUBSCRIBED`` is a genuine
exception. A consumer comparing against an upper-case ``'ORDERCREATED'``
matches nothing. Schwab documents this vocabulary nowhere at all; a search of
the whole developer portal returns nothing describing these values.

Fourteen have been captured, and they are exported as
:attr:`StreamClient.ACCOUNT_ACTIVITY_MESSAGE_TYPES
<schwaby.streaming.StreamClient.ACCOUNT_ACTIVITY_MESSAGE_TYPES>`:

.. code-block:: python

  ('SUBSCRIBED', 'OrderCreated', 'OrderAccepted',
   'ExecutionRequested', 'ExecutionRequestCreated',
   'ExecutionRequestCompleted', 'OrderFillCompleted',
   'CancelAccepted', 'ExecutionCreated', 'OrderUROutCompleted',
   'ChangeCreated', 'ChangeAccepted',
   'OrderMonitorCreated', 'OrderMonitorCompleted')

A message of any other type is still delivered to your handler, and is logged
once on ``schwaby.streaming``, so that a type nobody has captured becomes known
rather than passing silently. The exception is an empty type carrying a notice
written as text, described below, which is not logged. A buy rejected for
buying power has been captured, and so has a price change to a working order;
both used only types in that list. **Expiry and partial fill have not been
captured**, so the first of each a process receives may log once. That is
expected; please `open an issue
<https://github.com/tom-hirt/schwaby/issues>`__ with the type it names.

No other type is known from a captured frame. The absence of others from the
captures is not evidence either way: the archive most of them come from records
only fill-bearing and unexpected pushes, so a type on a push that carried
neither a fill nor anything unexpected would not have been recorded.

**Match case-insensitively, and do not assume case is the only difference.**
``ORDERUROUT`` is not a case variant of ``OrderUROutCompleted`` but a truncation
of it, and no amount of case folding will make the two meet.

**Classify by substring; use the exported set only to notice.** A consumer
holding an exact list --- including this one --- can miss a real message and
see only silence, which on an order feed is the failure that costs something.
Decide what a message *means* by testing for a fragment such as ``FILL``,
``CANCEL`` or ``UROUT``, and consult the exported set only to ask whether a
type is one anyone has seen before. ``CANCEL`` and ``UROUT`` also match the
items that retire the old id of a price change, while the order goes on working
under a new one --- see :ref:`price changes <account_activity_price_change>`.

``OrderUROutCompleted`` says the order **came off the book**. It does not say
why, and it is worth resisting the obvious gloss. Calling it "an unsolicited
out" asserts that nobody asked for the cancellation --- and the same token ends
a cancel you issued yourself. Any name or operator-facing phrase built on it
should describe what the venue did, not what caused it. The cause is carried by
the token beside it, below.

**A cancel you issue yourself looks like this**, measured by placing a
resting order and cancelling it::

  place   -> OrderCreated, OrderAccepted
  cancel  -> CancelAccepted, ExecutionCreated, OrderUROutCompleted

and ``get_order`` then reads ``CANCELED``. Identical for a limit and a stop.

**A buy rejected for buying power looks almost the same**, and this is worth
knowing because the HTTP response does not tell you::

  place   -> HTTP 201 with a real order id
             OrderCreated, OrderAccepted, CancelAccepted, OrderUROutCompleted

about a second later, with ``get_order`` reading ``REJECTED`` and a
``statusDescription`` naming the buying power. **A caller which checks only the
HTTP status believes the order was accepted.** Re-read the order.

Note the two sequences differ only by ``ExecutionCreated``, so do not use the
presence of ``OrderUROutCompleted`` alone to tell a cancel from a rejection.

.. danger::

  **A cancel emits** ``ExecutionCreated`` **carrying a non-zero quantity, on an
  order that filled nothing.** Measured by placing an unfillable limit against
  a funded account and cancelling it:

  .. code-block:: python

    # Inside the parsed MESSAGE_DATA, not beside it -- see below, where
    # MESSAGE_DATA turns out to be a JSON string that has to be parsed a
    # second time. `EventType` is that inner document's own type field and
    # is not the same key as the content item's MESSAGE_TYPE.
    {"EventType": "ExecutionCreated",
     "ExecutionInfo": {
        "ExecutionQuantity": {"lo": "1000000", "signScale": 12},   # decodes to 1
        "ExecutionTransType": "UROut",
        "CancelType": "ClientCancel"}}

  A genuine fill carries the *same* ``ExecutionQuantity`` field with
  ``ExecutionTransType: "Fill"``. **The quantity cannot tell a fill from a
  cancel. Only the trans-type can.**

  This is the expensive one because of where it lands: an execution algorithm
  cancels a clip to reprice, many times per parent order, by design. A consumer
  reading quantities by shape books a phantom fill on every reprice, concludes
  the clip is done, never places the remainder, and believes it holds a
  position it does not have. Nothing raises.

  Exclude on any ``UROUT`` / ``CANCEL`` / ``BUST`` / ``REJECT`` signal rather
  than trying to enumerate every fill label. The error is asymmetric ---
  under-counting is recoverable from a REST poll, over-counting is not.

.. danger::

  **A fill carries three quantities, and only one is the running total.** All
  three sit inside ``OrderFillCompleted``, under
  ``BaseEvent/OrderFillCompletedEventOrderLegQuantityInfo``:

  - ``QuantityInfo/CumulativeQuantity`` is how much of the order has filled so
    far;
  - ``ExecutionInfo/ExecutionQuantity`` is this execution alone, and
    ``ExecutionInfo/ExecutionPrice`` is its price;
  - ``OrderInfoForTransactionPosting/Quantity`` is the order's quantity, not
    this fill's: on each order that filled in two executions it held the same
    value on both fills, and on the two of those that could be compared it
    equalled the order's own leg quantity.

  On an order that fills in one execution, the first two are equal, so reading
  the wrong one survives every test built on such fills. On an order that fills
  in two, the second fill's ``ExecutionQuantity`` is the second tranche only.
  Measured on equity orders from a recorded archive of 160 filled orders:
  all three that filled in two executions showed the split --- the first fill
  with ``LeavesQuantity`` above zero and the two quantities equal, the second
  with ``ExecutionQuantity`` below ``CumulativeQuantity`` and
  ``LeavesQuantity`` zero --- and all 157 that filled in one had the two
  equal. Where these keys sit on an option order has not been checked.

  ``OrderCreated`` carries a ``Quantity`` as well, per leg under ``OrderLegs``.
  It is the size ordered, on an order that may never fill.

``PriceImprovement`` **has no established unit.** It sits directly under
``BaseEvent/OrderFillCompletedEventOrderLegQuantityInfo``, beside the
quantities above. It was checked against 188 live fills, dividing it by the
shares filled and comparing the result with the improvement over the quote held
when the order was placed. 94 fitted a total for the whole fill, 9 fitted a
price per share, 28 looked as though the quote had moved, and 57 fitted
neither. The quote at placement is not the market at execution, and the fill
carries no bid or ask to compare with instead, so the check cannot settle it.
Do not multiply it or divide it by the quantity until you have checked it
against a fill of your own.

**A fill carries two timestamps, and in that archive they were never the same
moment.** Under
``BaseEvent/OrderFillCompletedEventOrderLegQuantityInfo/ExecutionInfo``, both
``ExecutionTimeStamp/DateTimeString`` and
``VenuExecutionTimeStamp/DateTimeString`` (Schwab's spelling) were present on
all 218 fill items in a recorded archive --- 163 distinct fills, since the
archive can store one message twice --- and they differed on every one, by
under a second. Both are strings of the form ``YYYY-MM-DD HH:MM:SS.fff`` with
no offset. ``ExecutionTimeStamp`` reads as US Eastern time. For 106 of those
fills an independently recorded, zoned placement time was available: read as
Eastern, all 106 landed within a minute of it, and read as UTC none did. That
was measured in summer only, so whether it follows daylight saving is not
established. The venue stamp's time zone was
not measured.

**Charges were present on every fill in that archive, zero charges included.**
``ActualChargedCommissionAmount`` sits directly under the same
``ExecutionInfo``. ``SECFees``, ``TAF`` and ``ORF`` sit one level down, under
``ActualChargedFeesCommissionAndTax``. All four were present on all 163
distinct equity fills, and a zero charge arrived there as a decimal object
carrying only ``signScale``. An empty ``{}`` has also been seen as a genuine
zero commission, on another payload, and ``decode_decimal`` reads both as zero.
``RouteName`` was a non-empty string on all 163. ``ExecutionBroker`` was absent
on 2, so treat it as optional.

**Two enumerated fields arrive as either the label or its ordinal.**
``ResponseType`` and ``RouteStatus`` were captured as both a string and an
integer on the *same* order, from two ``ExecutionRequestCompleted`` frames two
milliseconds apart::

  seq=10   ResponseType='Accepted'   RouteStatus='RouteVenueAccepted'
  seq=11   ResponseType=8            RouteStatus=8

Not a version difference and not an account difference --- one order, one
cancel, one capture. Any model declaring these ``str``, or ``int``, breaks on
the sibling frame.

**Numbers are** ``{"lo": "<mantissa>", "signScale": N}``, **and an odd**
``signScale`` **means the value is negative.**

**This library ships one**, because every consumer needs it and a first
attempt is reliably wrong in at least one of the ways below:

.. code-block:: python

  from schwaby.contrib.util import decode_decimal

  decode_decimal({"lo": "7350000", "signScale": 12})   # Decimal('7.350000')
  decode_decimal({"lo": "7350000", "signScale": 13})   # Decimal('-7.350000')
  decode_decimal({"signScale": 12})                    # Decimal('0')
  decode_decimal({"lo": "15300"})                      # Decimal('15300')

**Wrap each field, not the message.** ``decode_decimal`` raises rather than
guess --- at a corrupt member, and at any key it does not recognise, which is
how a change at Schwab's end reaches you. Decoded one field at a time, a value
it refuses costs that field; decoded in one ``try``, it costs the message:

.. code-block:: python

  import logging

  from schwaby.contrib.util import decode_decimal, UnusableDecimalScale

  def decode_fields(message, names):
      out = {}
      for name in names:
          if name not in message:
              continue
          try:
              out[name] = decode_decimal(message[name])
          except UnusableDecimalScale as exc:
              # Keep the exception. It carries the specific complaint and a
              # bounded repr of the value -- a corrupt member and an
              # unrecognised key are both refusals, and only one of them
              # also writes a line of its own.
              logging.warning('could not decode %s: %s', name, exc)
      return out

``UnusableDecimalScale`` is both a :class:`~schwaby.utils.SchwabError` and a
``ValueError``, so either name catches it.

.. autofunction:: schwaby.contrib.util.decode_decimal
.. autoclass:: schwaby.contrib.util.UnusableDecimalScale

.. danger::

  **The whole mantissa arrives in** ``lo``, **and it can be wider than 32
  bits.** A serialized .NET ``System.Decimal`` has a 96-bit mantissa, and the
  obvious reading of ``lo`` is the low 32 bits of it, with ``mid`` and ``hi``
  carrying the rest. That is not what Schwab sends. In 5,843 decimal objects
  from a recorded ``ACCT_ACTIVITY`` archive, none carried ``mid`` or ``hi``,
  and 129 carried a ``lo`` wider than 32 bits --- each a string of ten digits,
  at ``signScale`` 12 or 13:

  .. code-block:: python

    {"lo": "7350000000", "signScale": 12}     # captured shape, invented value
    # lo as one 32-bit member   ->  refused, or wrapped to a smaller number
    # lo as the whole mantissa  ->  7350.000000      <- the actual value

  Those 129 were ``PrincipalAmmount`` on ``OrderFillCompleted`` --- Schwab's
  spelling --- and ``EstimatedPrincipalAmount``, ``EstimatedPrincipalAmnt`` and
  ``EstimatedNetAmount`` on ``OrderCreated``. Read whole, every one of the 33
  fill principals equalled price times quantity from the same message. At
  ``signScale`` 12 anything from ``4294.967296`` up needs the extra width, so a
  decoder holding ``lo`` to 32 bits loses any fill principal above roughly
  $4,295 --- loudly if it refuses one, silently if it wraps it.

  ``decode_decimal`` reads a lone ``lo`` as the whole mantissa, up to the 96
  bits the encoding allows. An object that does carry ``mid`` or ``hi`` is read
  as the three-member layout, ``lo + (mid << 32) + (hi << 64)`` with each
  member held to 32 bits, and a ``lo`` wider than that beside one is refused.
  That layout is :ref:`Unconfirmed <confidence_tags>`: reasoned from .NET, and
  no captured payload has carried ``mid`` or ``hi`` at all. **If you have one,
  please** `open an issue <https://github.com/tom-hirt/schwaby/issues>`__
  **with the field.**

.. _decimal_context:

``Decimal`` rather than a float, deliberately: these are money, and
:meth:`set_price <schwaby.orders.generic.OrderBuilder.set_price>` refuses a
float for the reason :ref:`price_strings` gives. A value decoded into a binary
float cannot be fed back into a reprice without going through the conversion
this library exists to avoid, and accumulating one over a day's principal
reintroduces exactly the error class that sends a limit price a cent low. The
value is built from a string, sign included, because **every operation** on a
``Decimal`` applies the caller's context --- only construction, and the
``copy_`` methods such as ``copy_abs()``, do not: a consumer who sets
``decimal.getcontext().prec = 6`` elsewhere in their process would otherwise
get ``1234.57`` for a price of ``1234.5678``, and negating a positive result is
such an operation, so the sign has to go into the string too.

The scale is also bounded rather than computed with. ``10 ** (signScale // 2)``
on a garbage or hostile ``signScale`` builds an astronomical integer and hangs
the thread, and a hang is not catchable by a per-item ``try``/``except``, so one
bad field would take the stream down rather than one message. Leaving the
arithmetic to refuse is not enough either --- a scale of a few million
underflows to a zero that compares equal to zero, which on a fill feed is the
worst answer available. No such payload has been observed; the bound is there
because the failure would be silent.

On a 1-share buy at a limit, with the values invented and the shapes measured:
``LimitPrice`` ``{"lo": "7350000", "signScale": 12}`` is ``7.35``, and
``EstimatedPrincipalAmount`` ``{"lo": "7350000", "signScale": 13}`` is
``-7.35`` --- principal on a *buy*, so cash out. **A decoder that handles only
the even case flips the sign on every cash-direction field, silently.**

The rule itself is not guesswork. **Schwab's Trader API support has confirmed
the conversion**, while stating that it remains undocumented publicly. That is
the strongest attestation any part of this section has, and nothing official
publishes it.

.. danger::

  **A decimal object carrying no mantissa at all is zero, not unknown.**
  Measured on three fields of one payload:
  ``ActualChargedCommissionAmount {}`` was a genuine $0 commission,
  ``RoutedPrice {}`` was a market order with no limit price, and
  ``LeavesQuantity {"signScale": 12}`` arrived on the **final fill event** of a
  completed order.

  Reading that third one as "unknown" makes a *complete* fill report as
  "remaining outstanding, quantity unknown". Silent, wrong, and on the fill
  path --- which is the worst combination available on this feed.

  **"No mantissa" means none of** ``lo``, ``mid`` **or** ``hi``, not an absent
  ``lo``. The serializer omits zero members --- including the scale, see
  below. Under the three-member layout above, which has never been captured, a
  value whose low 32 bits are zero would arrive as
  ``{"mid": 1, "signScale": 12}`` --- ``4294.967296`` --- and a guard keyed on
  ``lo`` alone would read it as zero.

.. danger::

  **An absent** ``signScale`` **is scale 0, not a missing scale.** The
  serializer omits whatever is zero, and that applies to the scale exactly as
  it applies to a mantissa member. One quote, in the shape Schwab sends it
  (the values are invented), is what settles it:

  .. code-block:: python

    {"Ask":     {"lo": "21480000", "signScale": 12},   # 21.48
     "AskSize": {"lo": "15300"},                       # 15300 shares
     "Bid":     {"lo": "21470000", "signScale": 12},   # 21.47
     "BidSize": {"lo": "2600"},                        # 2600 shares
     "Mid":     {"lo": "21475000", "signScale": 12}}   # 21.475

  Prices scaled by six places, sizes with no scale at all, in the same object.
  $21.48 with 15300 on the ask is a coherent quote; 0.0153 shares is not.

  Refusing the shape, on the reasoning that a missing scale cannot be told
  from a lost one, sounds careful and costs both sizes on every quote carrying
  them. If your decoder
  treats an absent ``signScale`` as an error, or guesses 12 because that is
  the common value, ``AskSize`` and ``BidSize`` are what it gets wrong: the
  second guess is off by a factor of a million with nothing raised.

.. danger::

  **Any key you do not recognise inside a decimal object means the object
  cannot be trusted, and** ``decode_decimal`` **refuses it.**

  The temptation is to ignore the key and decode the rest --- the encoding is
  positional, so a fifth key does not move the other four. That is true and
  it is not the problem. The problem is that an unrecognised key may be a
  *renamed* one, and from inside a payload the two are indistinguishable: the
  serializer omits whatever is zero, so a component missing because it was
  renamed looks exactly like one missing because it is zero.

  .. code-block:: python

    {"lo": "7350000", "SignScale": 12}    # a rename, or an addition?

  Read as an addition, there is no ``signScale``, so the absent-scale rule
  applies and a **$7.35 limit price decodes as $7,350,000**. Telling the two cases
  apart means handling three spellings of "missing" --- the key absent, the
  key present and ``null``, the key present and ``0`` --- and the last of
  those cannot be told apart at all, because ``signScale: 0`` genuinely means
  scale 0.

  **So the cost is paid deliberately.** A key Schwab adds arrives on every
  decimal object at once, so every money and quantity field raises until
  ``schwaby`` knows about it. That is loud, it is catchable per field, and the
  key is named in the log on the first message --- against a silent wrong
  price on a funded account, it is not a close call. If you see it, please
  `open an issue <https://github.com/tom-hirt/schwaby/issues>`__ with the
  field.

.. _unknown_field_ids:

.. note::

  **A field id this library has no name for is delivered to your handler
  under its numeric key**, while every field it does know is relabeled as
  usual. A field Schwab adds reaches you rather than breaking you.

  It is also logged, once per field per table, on the ``schwaby.streaming``
  logger --- because being delivered and being *noticed* are different
  things, and the field tables are only as current as the last time someone
  looked. If you see that line, please `open an issue
  <https://github.com/tom-hirt/schwaby/issues>`__ with the id and the value.

  This one is not reported through :func:`add_error_handler
  <schwaby.streaming.StreamClient.add_error_handler>`: nothing was absorbed
  and nothing failed. It is a change in the venue, which is an operator's
  concern rather than a caller's.

.. warning::

  **A whole service, or a whole channel, that this version does not know is
  a different matter: the messages are dropped.** There is no handler to
  route an unknown service to and no field table to relabel it with, and a
  channel this version does not read is a compartment of the frame nobody
  looks in. Your handlers never fire for either, so without a report they
  are invisible from inside a consumer.

  Both *are* reported --- a ``WARNING``, and an :class:`UnusableMessage`
  through :func:`add_error_handler
  <schwaby.streaming.StreamClient.add_error_handler>`. **Every distinct
  service or channel name is named at least once, in both** --- up to the
  first 64 of them on a client, after which the guarantee stops and a line
  says so --- and everything after that first sighting is coalesced so a systematic change cannot become
  a log flood. The coalescing counts per *kind*, which is why the first
  sighting is guaranteed separately: without it a fourth new service arriving
  beside three others would never be reported at all.

  What appeared is on the exception's ``message``: the service name for a
  service, the sorted list of channel names for a channel.

  A service you simply registered no handler for is **not** reported. That is
  your own choice, and a line per message on a feed you deliberately ignored
  is noise rather than news. Nor is a ``notify`` frame that names no service,
  which is not required to name one.

  The frame is not dropped over an unread channel --- the compartments that
  *are* understood still hold real data, and refusing the whole frame would
  turn an addition into an outage.

**The sign of a field is not a side.** Buy versus sell comes from
``BuySellCode``. Prices other than ``Mid``, and every quantity, arrived
positive, with an even ``signScale``; the odd branch exists so a genuinely
negative field does not decode positive. Across that recorded archive only
four fields were ever negative: ``EstimatedPrincipalAmount``,
``EstimatedPrincipalAmnt`` and ``EstimatedNetAmount``, negative on a buy
because the cash goes out, and ``Mid``, negative on the sell orders where the
side was checked --- the opposite way round. ``Bid``, ``Ask``, ``LimitPrice``,
``ExecutionPrice``, ``PrincipalAmmount`` and every quantity were positive
throughout. Two fields whose signs followed the side in opposite directions are
reason enough: do not infer direction from the sign of anything.

.. danger::

  **A quote's** ``Mid`` **decoded negative on every sell order checked.** Its
  magnitude is exactly ``(Bid + Ask) / 2`` from the same quote, 563 times of
  563, and wherever the order's side was checked its sign followed it. On the
  182 quotes whose message also carried ``BuySellCode``, every buy had an even
  ``signScale`` and every sell an odd one --- 88 and 94 of them, with no
  exceptions. That is the opposite way round from the amounts above, and Schwab
  documents neither.

  ``Mid`` arrives in three containers: ``QuoteOnOrderAcceptance`` on
  ``OrderCreated`` and ``ChangeCreated``, ``QuoteOnOrderEntry`` on
  ``OrderAccepted`` and ``ChangeAccepted``, and ``Quote`` on
  ``ExecutionRequested``. Only the first pair carries ``BuySellCode``. Most
  quotes arrive on the others, where the side comes from joining on
  ``SchwabOrderID``, so the side correlation is measured on creation messages
  only.

  If you want a mid price, take the magnitude with ``copy_abs()``, which
  ignores the decimal context and is exact. ``abs()`` and ordinary arithmetic
  do not: they apply the caller's context, as
  :ref:`the decoder's construction <decimal_context>` explains. With
  ``decimal.getcontext().prec = 6``, ``abs()`` turns a ``Mid`` of
  ``-12.345678`` into ``12.3457``, while ``copy_abs()`` gives ``12.345678``.
  To compute a mid from the same quote's ``Bid`` and ``Ask`` instead, do the
  arithmetic under a context with enough precision, and trap ``Inexact`` so a
  result that would have to round raises ``decimal.Inexact`` instead. That is
  an ``ArithmeticError``, not a
  :class:`~schwaby.contrib.util.UnusableDecimalScale`, so the per-field
  handler above does not catch it:

  .. code-block:: python

    import decimal

    from schwaby.contrib.util import decode_decimal

    # `quote` is a parsed quote container, like the one shown above.
    mid = decode_decimal(quote['Mid']).copy_abs()    # exact in any context

    bid = decode_decimal(quote['Bid'])
    ask = decode_decimal(quote['Ask'])
    with decimal.localcontext() as ctx:
        ctx.prec = 34
        ctx.traps[decimal.Inexact] = True
        mid_from_quote = (bid + ask) / 2

  Do not read the side from ``Mid`` either: a convention that held on a sample
  and appears nowhere in Schwab's documentation is not one to trade on.

**Timestamp and container fields arrive as** ``{}``, not ``null`` and not
absent, where the populated form is ``{"DateTimeString": "..."}``. Seen on
``ExecutionTime``, ``RouteAcknowledgmentTimeStamp``, ``AsOfTimeStamp``,
``PreferredRoute``, ``EquityOrderLeg`` and others in a single five-frame
capture. A truthiness check handles it; ``d['ExecutionTime']['DateTimeString']``
does not.

``MESSAGE_DATA`` **is a string, not an object** --- it has to be parsed a
second time. It is the empty string on the ``SUBSCRIBED`` ack, which is not a
malformed message and should not be reported as one.

.. warning::

  **And it is not always JSON.** Schwab sends notices through the same field
  as plain prose --- ``"Feature not supported"`` has been observed --- so a
  consumer calling ``json.loads`` unconditionally raises on a message that is
  merely informational, on the account feed, at whatever moment Schwab decides
  to tell you something. Text that starts like a JSON object and does not
  parse is a truncated or corrupt payload, and ``parse_message_data`` below
  raises on it rather than passing it off as a notice. Any other text that is
  not JSON is a notice, not a broken frame.

  That notice arrives as an ordinary ``data``-channel content item whose
  ``MESSAGE_TYPE`` is the empty string, so a handler that dispatches on the
  type has nothing to dispatch on. An empty type carrying a notice like this
  is not logged as a message type nobody has captured; an empty type carrying a
  JSON object still is. It recurs: it was seen on at least 69 days
  across about three months of recorded traffic from one feed, most often
  around 00:30 Eastern, and also around 18:00 and 21:00.

``parse_message_data`` does that second parse and keeps the three cases apart:

.. autofunction:: schwaby.contrib.util.parse_message_data

.. autoclass:: schwaby.contrib.util.UnparsableMessageData

.. warning::

  **A re-subscribe can replay recent activity in a stripped shape.**
  :ref:`Seen once, not reproduced <confidence_tags>`.

  What was recorded at the time: after a reconnect, the stream re-sent recent
  activity with **no account, no symbol and no execution id** --- a bare
  quantity and price with none of the identifying envelope. A consumer without
  a dedup guard re-books an old fill as a fresh one.

  The shape is the useful part, because it is recognisable: those three absent
  together is not what a live event looks like. A guard can key on that as well
  as on execution id.

  **We cannot tell you when this happens.** A later capture is a
  counter-example --- a subscribe on an account with four filled orders that
  same day produced a bare ``SUBSCRIBED`` ack and then silence, replaying
  nothing. So "replays on every re-subscribe" is wrong, and what triggers it is
  unknown. What is true either way, and worth building for: **a consumer must
  be idempotent across reconnects.**

  `An issue <https://github.com/tom-hirt/schwaby/issues>`__ from anyone who
  can reproduce this on demand would be worth a great deal.

**Content items within one message are not necessarily in lifecycle order.** A
``CancelAccepted`` naming no symbol was seen arriving ahead of the
``ExecutionRequested`` for the same order that did name the instrument, so a
consumer ruling item-by-item makes its verdict depend on Schwab's ordering
within the batch. Scan the whole batch before classifying any of it.

This section draws on several sources:

- a deliberate place-and-cancel of an unfillable limit order on a funded
  account;
- a recorded archive of fill-bearing and unexpected pushes, which is where
  the decimal and ``Mid`` counts come from;
- option orders placed by hand whose prices were then changed, described
  below;
- for the batch ordering above, an observation made live without the
  frame being retained, so no captured bytes back it.

Schwab documents none of it.

.. _account_activity_price_change:

**A price change to a working order gives it a new order id.** It does not
amend the order in place. Captured on two option orders --- a two-leg order
whose limit price was changed three times, and a single-leg order changed five
times --- each change produced items for two ids:

- the **new** id: ``ChangeCreated`` and ``ChangeAccepted``, and for each leg
  ``OrderMonitorCreated``, ``ExecutionRequested``, ``ExecutionRequestCreated``,
  ``OrderMonitorCompleted`` and ``ExecutionRequestCompleted``;
- the **old** id: ``CancelAccepted``, and for each leg ``ExecutionRequested``,
  ``ExecutionCreated``, ``OrderUROutCompleted`` and
  ``ExecutionRequestCompleted``.

Those are the types each id carried, not their order or how many of each ---
``ExecutionRequestCompleted`` has appeared more than once for a leg on the old
id. Items for the two ids interleave within a message, so a handler cannot
expect one order's items to finish before the other's begin. The
``ExecutionCreated`` on the old id is the order coming off the book, not a
trade.

.. danger::

   **The old id is retired with a** ``CancelAccepted``. A handler that reads it
   --- or any type containing ``CANCEL`` --- as "this order is gone" concludes
   that a working order was cancelled while it goes on working under the new
   id, and nothing raises. The ``CancelRequestType`` on those items read
   ``ClientCancel`` in every change, and each ``ExecutionCreated`` on the
   retired id carried ``ExecutionTransType`` ``UROut`` and ``CancelType``
   ``ClientCancel`` --- the same pair as the cancel shown above, so
   ``CancelType`` does not separate a change from a cancel. That cancel does
   not show a ``CancelRequestType``, so whether that one differs has not been
   compared.

   **REST does not connect them either.** :meth:`get_order
   <schwaby.client.Client.get_order>` read ``REPLACED`` on each of the five
   retired ids of the single-leg order, and that top-level ``status`` is the
   only place it differed from a cancel. Each retired order's activity
   collection held a single ``EXECUTION`` activity reading ``CANCELED``, so
   code reading activities sees a cancel. None of the retired orders carried a
   key whose name mentions a parent or a replacement, and nothing captured lets
   a program holding only a retired id find its successor through REST.

   The only link captured is on the stream, on the new id's ``ChangeCreated``:
   its ``ParentSchwabOrderID`` named the id it replaced, in every change.
   ``LifecycleSchwabOrderID`` named the original order on every
   ``ChangeCreated`` and every ``CancelAccepted``, the same value throughout
   the chain. A fresh order's ``OrderCreated``, captured on an option order,
   carried no ``ParentSchwabOrderID``, and its ``LifecycleSchwabOrderID`` named
   itself.

No ``OrderReplaced`` type appeared in any of the eight changes. Those eight are
the only sample, so its absence elsewhere is not evidence.

``OrderMonitorCreated`` and ``OrderMonitorCompleted`` have been captured only
inside changes to an order resting on the book. A marketable option order that
filled without resting produced neither. Whether an order left resting and
never changed produces them is not established. The spellings above are as
captured on the wire.

A program that never changes a working order will not see the change types
until someone changes one by hand in the same account, from Schwab's own
interface --- which is how these were captured. No item in a captured change
was a fill; the order that filled did so in a later message of its own, as
``OrderFillCompleted``. Changes are also numerous: each produced more than
twenty items on the two-leg order and thirteen on the single-leg one. Consider
logging them below the level you use for fills.

**A change to a working order is worth separating from the rest.** For an order
your own program placed, a change mid-flight is a safety event; for one you do
not manage, it is somebody editing their own order. ``ChangeCreated`` is the
item that marks it.

**Distinguishing a relabeled item from a raw one.** After relabeling, a
``data``-channel content item carries ``seq``, ``key``, ``ACCOUNT``,
``MESSAGE_TYPE`` and ``MESSAGE_DATA``. A content item carrying none of those is
a ``notify``-channel item, which this library forwards unchanged --- see the
warning under :ref:`Data Field Relabeling <data_field_relabeling>` above.
Observed values there include an activity token of ``orderfill``. The
``Feature not supported`` notice has been seen on the ``data`` channel instead:
over about three months of recorded traffic from one feed it arrived only
there, as ``MESSAGE_DATA`` text.

If you learn something this list gets wrong, a pull request correcting it is
more useful than a private patch.


.. _json_decoding:

++++++++++++++++++++++++++++
When the JSON Will Not Parse
++++++++++++++++++++++++++++

Schwab sometimes sends the streaming server's messages in a form Python's
``json`` module refuses. When that happens the frame raises
:class:`~schwaby.streaming.UnparsableMessage`, which is reported to your error
handler and then ends your receive loop --- see
:ref:`Reacting to Absorbed Failures <error_handlers>` for why that one is not
absorbed like the others.

If you see it more than once, try the heuristic decoder before concluding the
stream is broken. It attempts the plain decoding first and falls back to
repairing the escaping Schwab is known to get wrong:

.. code-block:: python

  from schwaby.contrib.util import HeuristicJsonDecoder

  stream_client.set_json_decoder(HeuristicJsonDecoder())

That this exists at all is the evidence that unparsable frames are a quirk of
the venue rather than a sign of a dead connection.

You can supply your own decoder by subclassing
:class:`~schwaby.contrib.util.StreamJsonDecoder`. It must return the decoded
JSON; this library reads it structurally rather than requiring ``dict`` and
``list`` exactly, so a decoder returning a mapping type of your own works ---
but a JSON array must be **indexable**, because routing reads element zero and
handlers are given the frame afterwards. A generator will not serve.

.. automethod:: schwaby.streaming::StreamClient.set_json_decoder

.. autoclass:: schwaby.contrib.util.StreamJsonDecoder
  :members:

.. autoclass:: schwaby.streaming.NaiveJsonStreamDecoder

.. autoclass:: schwaby.contrib.util.HeuristicJsonDecoder
  :members:
