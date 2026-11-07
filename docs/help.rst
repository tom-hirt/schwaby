.. highlight:: python
.. py:module:: schwaby.debug

.. _help:

============
Getting Help
============

Even the most experienced developer needs help on occasion. This page describes
how you can get help and make progress.

.. note::

   Bug reports and questions go to `the tracker
   <https://github.com/tom-hirt/schwaby/issues>`__.



--------------
Before You Ask
--------------

Most problems are solved faster by gathering a little information first.

 1. Check that you are on the latest version, and note which version you are
    using. ``print(schwaby.__version__)`` will tell you.
 2. Note your OS and how you are running your code -- a terminal, a notebook, a
    container, an IDE. Several common failures are specific to one of these.
 3. Capture the full stack trace and error message, not just the last line.
 4. Read the logs. Enabling them is described below, and often the answer is
    already in there.


---------------
Reporting a Bug
---------------

``schwaby`` is not perfect. Features are missing, documentation may be out of
date, and it almost certainly contains bugs. If you think of a way in which it
can be improved, we're glad to hear it.

.. _enable_logging:

~~~~~~~~~~~~~~
Enable Logging
~~~~~~~~~~~~~~

Behind the scenes, ``schwaby`` performs diagnostic logging of its activity
using Python's `logging <https://docs.python.org/3/library/logging.html>`__
module. Two things are needed to see it: somewhere for the messages to go,
and a level low enough to let them through.

.. code-block:: python

  import logging

  logging.getLogger('').addHandler(logging.StreamHandler())
  logging.getLogger('schwaby').setLevel(logging.DEBUG)

**The second line is the one people miss.** A handler on its own changes
nothing, because the root logger's default level is ``WARNING`` and almost
everything this library logs is ``DEBUG``. Adding the handler and stopping
there produces a program that looks like it has logging switched on and emits
none of it.

Setting the level on ``schwaby`` rather than on the root logger keeps the output
to this library. Point it at ``''`` instead if you want everything, including
whatever your other dependencies have to say.

Sometimes this additional logging is enough to debug the problem yourself.
Before you ask for help, read through your logs to see whether there is
anything there that explains it.

Note the streaming client reports handler failures on the ``schwaby.streaming``
logger rather than raising them, so a handler which is quietly failing shows up
there and nowhere else.

.. _never_logged:

**Secrets are never logged, at any level.** There is no mode to turn on: every
logger this library owns replaces them however the record is rendered --
by a handler, or by anything else that reads records -- in the message, in a
traceback and in a stack. Text this library cuts short for a message, such as
a frame quoted in an exception, is redacted before it is cut. Never logged:

- the app key and the app secret;
- every access, refresh and ID token, including each new one a refresh brings;
- the authorization code, and the whole URL a login is redirected to;
- full account numbers, from the first response or stream frame that names
  each one. Account hashes, which every request names an account by, and
  other identifiers are left alone, so a log can still say which account a
  line is about.

Each is replaced with ``<REDACTED ACCESS_TOKEN>`` or the like. A secret this
library has not seen yet is still masked where its *name* says what it is: the
``Authorization`` of a streamer request, an ``access_token`` field, a
``code=`` in a URL. An account number has no such name to find it by, so one
your own code puts in a request before any response has named it is logged as
it is.

The libraries this one drives are covered too. ``websockets`` logs every frame
at ``DEBUG``, the streamer's LOGIN request among them, and is handed a wrapper
around the logger it would have used anyway, ``websockets.client``, which
redacts each frame before anything sees it -- so its elision of a long frame's
middle only ever cuts redacted text. Its records still come from
``websockets.client``, so a level, handler, filter or ``disabled`` you set
there applies as it always did. If you pass ``websockets`` a ``logger`` of your
own through ``websocket_connect_args``, it is wrapped the same way rather than
replaced: no filter or handler of yours can see a frame unredacted, and
everything else passes through as it was logged. A fragment of a message is
not logged at all, since a value split across two fragments matches neither.
``httpx2``, ``httpcore2`` and ``authlib`` were measured logging
none of these at ``DEBUG``, and carry the redaction anyway: ``httpx2`` logs
every request's URL at ``INFO``, and a URL naming an account by its number
rather than its hash carries the number. A test runs a whole session with
every logger at ``DEBUG`` to keep all of this so.

.. note::

   A logged exception reaches your handlers as text. Its traceback is
   rendered and redacted before any handler sees the record, and the record's
   ``exc_info`` is then cleared, as the standard library's ``QueueHandler``
   does, because a formatter that renders the exception object again would
   print it unredacted. Every standard formatter prints the redacted text. A
   handler that reads ``exc_info`` directly, as an error tracker's logging
   integration does, gets the message and the text but not the exception
   object, and a handler's own ``formatException`` is not consulted. Such a
   record also pickles as its redacted text, so one sent to another process
   carries nothing to redact there. This holds for the records of every logger
   this library protects,
   ``httpx2``'s, ``httpcore2``'s and ``authlib``'s included, from the moment it
   is imported, and so for any other code in the process using those
   libraries; their messages also reach handlers already formatted, with no
   arguments left. Records of your own are not touched.

The one place the app key is shown is the terminal: both login flows print the
URL you have to open, and that URL carries it as ``client_id``. It is printed,
never logged.


~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Gather Logs For Your Bug Report
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If you still can't work out what is going wrong, ``schwaby`` can gather and
prepare logs for filing issues. It captures the logs, anonymizes them, and dumps
them to the console when the program exits. Enable it by calling this method
**before doing anything else in your application**:

.. code-block:: python

  schwaby.debug.enable_bug_report_logging()

This redacts common secrets such as account IDs, tokens and access keys.
However, **that redaction is not guaranteed to be complete, and checking the
logs before sharing them is your responsibility.** Never share the contents of
your token file.

.. warning::

  **Check by listing what is there, not by searching for what you expect.**

  Account-activity payloads can carry personal data in shapes a search will
  not find. Searching one for the account number and the stream key, and
  finding them replaced, does not make it clean. A ``TradeTag`` like this one:

  .. code-block:: json

    {"TradeTag": "TA_janedoeexamplecom1234567890"}

  is ``jane.doe@example.com`` with the ``@`` and the ``.`` removed,
  assembled that way by Schwab, not by the sender. ``grep`` for the address
  finds nothing. So does any pattern looking for an ``@``. The same payloads
  can also carry a customer ID that outlives the account number, and the
  holder's state of residence.

  A search can only confirm what you already suspect. Parse the payload, walk
  every value at every level --- ``MESSAGE_DATA`` is a JSON string, so its
  contents are one parse deeper than they look --- and read the list. Better
  still, if you are building a fixture rather than filing a report, emit only
  the fields the problem needs and drop the rest by construction. A fixture
  built this way loses nothing a bug report needs, since the shape of a
  problem rarely depends on an identifier.

For completeness, here is this method's documentation:

.. automethod:: schwaby.debug.enable_bug_report_logging


~~~~~~~~~~~~~~~~~~
Submit Your Ticket
~~~~~~~~~~~~~~~~~~

A good report includes:

 * **The code that triggers it.** If you would rather not share yours, a short
   script which reproduces the problem is just as good, and often narrows it
   down on its own.
 * **The full stack trace**, not an excerpt.
 * **Logs**, attached as a file rather than pasted into the issue body, and
   checked for anything sensitive first.
 * **Your version, OS and execution environment.**

Then `file an issue <https://github.com/tom-hirt/schwaby/issues>`__.
