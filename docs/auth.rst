.. highlight:: python
.. py:module:: schwaby.auth

.. _auth:

==================================
Authentication and Client Creation
==================================

By now, you should have followed the instructions in :ref:`getting_started` and
are ready to start making API calls. Read this page to learn how to get over the
last remaining hurdle: OAuth authentication.

Before we begin, however, note that this guide is meant for users who want to run
applications on their own machines, without distributing them to others. If you
plan on distributing your app, or if you plan on running it on a server and
allowing access to other users, these login flows are not for you.


------------------------
The Quick and Easy Route
------------------------

If all you want to do is create a client, you should use
:func:`~schwaby.auth.easy_client`. This method will attempt to create a client in
a way that's appropriate to the context in which you're running:

 * If you've already got a token at ``token_path``,
   :func:`load it <schwaby.auth.client_from_token_file>` and continue. Otherwise
   create a new one.
 * In desktop environments, :func:`start a web browser
   <schwaby.auth.client_from_login_flow>` in which you can sign in, and
   automatically capture the created token.
 * In a notebook like Google Colab or Jupyter, instead run the :func:`manual
   flow <schwaby.auth.client_from_manual_flow>`.

.. note::

  It does not simply load an existing token. It discards one older than
  ``max_token_age`` --- 6.5 days by default --- and fetches a new one through
  the login flow, so an unattended program built on ``easy_client`` runs for
  6.5 days and then stops at a browser login. Pass ``max_token_age=0`` to turn
  the proactive refresh off. Schwab's refresh token expires seven days after
  authorization either way, so anything long-running needs some route back to a
  login; turning it off means choosing that route yourself rather than having
  one chosen at an arbitrary moment.

  **For an unattended or long-running process, prefer**
  :func:`~schwaby.auth.client_from_token_file`, which never initiates a login.
  What ``easy_client`` does when the token ages out is open a browser, and in
  an unattended process that is a window nobody sees --- under a running
  terminal UI it can open behind the interface and take the keyboard with it.
  Loading the token directly turns re-authentication into an explicit
  operator action, and an expired token into an ordinary API exception you can
  alert on.

Here's how you can use it. If for some reason this doesn't work, please report
your issues `on the issue tracker <https://github.com/tom-hirt/schwaby/issues>`__. See
:func:`~schwaby.auth.easy_client` for details:

.. code-block:: python

  import httpx2

  from schwaby.auth import easy_client

  # Follow the instructions on the screen to authenticate your client.
  c = easy_client(
          api_key='APIKEY',
          app_secret='APP_SECRET',
          callback_url='https://127.0.0.1',
          token_path='/tmp/token.json')

  resp = c.get_price_history_every_day('AAPL')
  assert resp.status_code == httpx2.codes.OK
  history = resp.json()


.. _callback_url_advisory:

-------------------------
Callback URL Requirements
-------------------------

:func:`~schwaby.auth.client_from_login_flow` starts a server on the port in your
callback URL. When you finish logging in, Schwab sends a request to that URL
with the login data in the query parameters. **Anyone who receives that request
can steal your token and act on your account as though they were you.**

That server uses a self-signed certificate, which is why your browser warns
about it --- see :ref:`the troubleshooting section <ssl_errors>`. The
certificate protects the data in transit. It does nothing about who is listening
on the port, which is why the host is restricted rather than left to you.

So only ``127.0.0.1`` is allowed as a host. Any other hostname raises a
``ValueError`` --- including ``localhost``, which resolves to the right place
but is not the same string.

Use a port above ``1024``. Most operating systems require superuser privileges
to listen below that, and some need firewall changes to accept connections to
those ports even from the same machine. Specifying *no* port is equivalent to
port ``443``, which your operating system will almost certainly refuse to open,
and this method will fail.

**The vast majority of users should use** ``https://127.0.0.1:8182``.

Whatever you choose has to match your app's configuration on `Schwab's developer
portal <https://developer.schwab.com/>`__ *exactly* --- case and trailing
slashes included. Changing it there will likely require app re-approval from
Schwab, which typically takes a few days.

.. _login_flow:

--------------------------------------
Fetching a Token and Creating a Client
--------------------------------------

This function will guide you through the process of logging in and creating a
token.

.. autofunction:: schwaby.auth.client_from_login_flow

.. autoexception:: schwaby.utils.NoBrowserAvailable

.. _manual_login:

If for some reason you cannot open a web browser, such as when running in a
cloud environment or a notebook, this function will guide you through the
process of manually creating a token by copy-pasting relevant URLs.

.. autofunction:: schwaby.auth.client_from_manual_flow

Once you have a token written on disk, you can reuse it without going through
the login flow again.

.. autofunction:: schwaby.auth.client_from_token_file

The following is a convenient wrapper around token creation and fetching,
calling each when appropriate:

.. autofunction:: schwaby.auth.easy_client

.. _webapp_flow:

------------------------------
Logging In From a Web Backend
------------------------------

The functions above assume the login and the callback happen in the same
process on the same machine. In a web application they do not: you send the
user to Schwab from one request and the redirect arrives as another, possibly
minutes later and possibly on a different host, and there is nothing for a
local callback server to do.

For that case, start the login with
:func:`~schwaby.auth.get_auth_context` and finish it with
:func:`~schwaby.auth.client_from_received_url`.

.. code-block:: python

  from schwaby.auth import get_auth_context, client_from_received_url

  # In the handler that begins a login:
  context = get_auth_context(API_KEY, 'https://your-app.example.com/callback')
  save_to_session(context)          # it is a namedtuple of strings
  redirect(context.authorization_url)

  # In the handler your callback URL is routed to:
  context = load_from_session()
  c = client_from_received_url(
          API_KEY, APP_SECRET, context,
          received_url=full_url_of_this_request,
          token_write_func=lambda token, *a, **kw: store_token(user, token))

To keep the token in a file, the way the functions above do, pass the writer
:func:`~schwaby.auth.token_file_writer` returns for that file, such as
``token_write_func=token_file_writer('/path/to/token.json')``. The file it
leaves is one :func:`~schwaby.auth.client_from_token_file` and
:func:`~schwaby.auth.easy_client` read.

Two things are easy to get wrong here.

**Pass the whole callback URL, query string included.** That query string
carries the authorization code, which is what makes the token. Treat the value
as you would the token itself --- in particular, keep it out of your request
logs, which record full URLs by default.

**Keep the** :class:`~schwaby.auth.AuthContext` **with the session that created
it.** Its ``state`` is checked against the redirect, so a callback belonging to
a different login is rejected instead of quietly authenticating the wrong
person.

The callback URL is not restricted to ``127.0.0.1`` here, since nothing in this
process listens on it. Everything in :ref:`callback_url_advisory` about who can
read that request still applies, and applies more, because the request now
crosses a network rather than a loopback interface.

.. autofunction:: schwaby.auth.get_auth_context

.. autoclass:: schwaby.auth.AuthContext

.. autofunction:: schwaby.auth.client_from_received_url

.. autofunction:: schwaby.auth.token_file_writer


.. _token_expiration:

-------------------------
Notes on Token Expiration
-------------------------

A token is good for seven days from the moment it was created --- not from
its last use. After that you will :ref:`see failures <invalid_client>` and
must delete the token file and create a new one.

In practice that means picking a moment to refresh rather than waiting to be
stopped by it. If you trade on weekdays, recreating the token on Sunday before
the open costs nothing and removes the question.

For users wanting to craft more custom workflows, the client :meth:`exposes the
age of the token <schwaby.client.Client.token_age>`. Note, however, that the
seven day token age restriction is implemented by Schwab, and so the token may
become expired sooner *or* later than seven days.

The age is this machine's clock minus the creation time the logging-in machine
wrote. If this clock is behind that one's, the age reads low, and negative while
the creation time is still ahead of this clock, so ``easy_client`` retires the
token that much later. Such a token is not refused, because a clock that is only
behind would then fail a working token at startup. An expired refresh token has
been observed refused as ``invalid_grant`` on every attempt across four days
of failing refreshes, which sets ``refresh_token_invalid``
whatever the age says. A refusal under another code does not set it, and the
seven-day alert in the recipe below then comes that much later.

A program that only needs the age --- a monitor, or a job that warns before the
window closes --- can read it from the token file without building a client:

.. autofunction:: schwaby.auth.token_file_age
.. autofunction:: schwaby.auth.token_file_created_at
.. autofunction:: schwaby.auth.refresh_token_expires_at


.. _token_file:

--------------
The Token File
--------------

The token file is a credential in its own right. Anyone holding it can read your
balances and positions and place trades with them, so it deserves the same care
as a password.

On Linux and macOS, ``schwaby`` writes it readable only by you (mode
``0600``). A file written by an older version, or copied around with a
permissive umask, is corrected the first time the token is refreshed. It is
worth checking anyway::

  ls -l /path/to/token.json

.. warning::

   **On Windows there is no such protection.** Windows has no POSIX file mode:
   ``os.chmod`` toggles a read-only bit and ignores the rest, so the token file
   is left readable by any account on the machine. This library cannot narrow
   that, and does not pretend to -- setting Windows ACLs needs ``pywin32``,
   which is not a dependency here.

   The file is still written atomically and durably on Windows; it is only the
   permission narrowing that does not apply. If you are on Windows and the
   machine has other users, put the token somewhere covered by an ACL you
   control, and treat its location as part of your security posture rather than
   assuming the library has handled it.

The write is atomic: the new token goes to a temporary file in the same
directory, which is flushed and then renamed over the destination. So a process
which dies partway through a refresh -- killed by a supervisor, or a machine
losing power -- leaves the previous token intact rather than a half-written file
that cannot be parsed. That matters because an unparseable token file cannot be
repaired; it requires a fresh interactive login.

If ``token_path`` is a symlink, the link is followed and its target is written,
rather than the link being replaced.

Never commit a token file, never paste one into an issue, and never share it in
logs. See :ref:`the help page <help>` for what redaction does and does not
cover.


.. _shared_token_file:

++++++++++++++++++++++++
Sharing One Token File
++++++++++++++++++++++++

By default a token file belongs to one client. Several clients reading and
refreshing the same file, in one process or several, can each refresh it, and
a refresh of an old token can overwrite a newer login's. ``shared=True``
makes them take turns:

.. code-block:: python

  client = schwaby.auth.client_from_token_file(
          '/path/to/token.json', api_key, app_secret, shared=True)

With it:

- a refresh, and the write it makes, holds an exclusive lock on the file,
  across threads and processes alike. A load takes none;
- before refreshing, the client reads the file, and when another client has
  refreshed already it takes that token and sends nothing;
- a token is written only if it is at least as new as the file's: by the
  login it came from, then its access token's expiry. A fresh login always
  wins over a refresh of the old token;
- the lock is a file beside the token file's real path, ``token.json.lock``,
  created readable only by you, or ``lock_path``. Its directory must be
  yours alone (no group or other write) and neither it nor the lock file may
  be a symlink, or :class:`~schwaby.utils.UnsafeTokenLockFile` is raised: a
  lock someone else could replace or redirect is no lock;
- a fresh sign-in is written whatever the file holds: the exchange
  :func:`~schwaby.auth.client_from_received_url` makes, and with it every
  login function, writes its token unconditionally, so a token dated in the
  future by a wrong clock cannot outrank it. A writer from
  :func:`~schwaby.auth.token_file_writer` or
  :func:`~schwaby.auth.shared_token_file` takes ``sign_in=True`` for a sign-in
  you write yourself;
- the wait for the lock is bounded by ``lock_timeout``, 10 seconds by
  default. Past it a refresh raises
  :class:`~schwaby.utils.TokenRefreshError` with kind ``transient``, having
  sent nothing, the request included, and a login's write raises
  :class:`~schwaby.utils.TokenFileLockTimeout`.

The login functions and :func:`~schwaby.auth.easy_client` take ``shared``,
``lock_path`` and ``lock_timeout`` too, so a fresh login takes the lock as a
refresh does. For a token kept somewhere else, the reader and writer
:func:`~schwaby.auth.shared_token_file` returns take ``read_bytes`` and
``write_bytes`` of your own; the lock and the order of tokens stay this
library's.

.. note::

   The lock works where the operating system's file lock does: on a local
   disk, ``flock`` on Linux and macOS, ``msvcrt.locking`` on Windows. On a
   network file system it may lock nothing. Every client that shares the
   file must pass ``shared=True``, and the same ``lock_path`` if any: one
   that does not neither takes the lock nor waits for it. A relative
   ``lock_path`` is resolved against each process's own working directory.

   An ``asyncio`` client waits for the lock in a worker thread. A task that
   stops waiting leaves that thread to take the lock and give it back, and
   ``asyncio.run`` waits for it, up to ``lock_timeout``, before it returns.

.. autofunction:: schwaby.auth.shared_token_file
.. autoexception:: schwaby.utils.TokenFileLockTimeout
.. autoexception:: schwaby.utils.UnsafeTokenLockFile


++++++++++++++++++++++++++
A Note on Keys and Secrets
++++++++++++++++++++++++++

Copy-pasting an app key or secret out of the developer console picks up a
trailing space or newline more easily than you would expect, and Schwab does not
handle that consistently -- some endpoints tolerate it and some reject it, so
the symptom is an authentication failure that comes and goes.

Surrounding whitespace is therefore stripped from ``api_key`` and ``app_secret``
wherever they are accepted, and a warning is emitted when there was any. The
warning is worth acting on: the value is being corrected in passing, but the
copy it came from is still wrong. Whitespace *inside* a value is left alone,
since that is the caller's value and not a paste artifact.


+++++++++++++++++++++++++
Catching a Failed Refresh
+++++++++++++++++++++++++

Every request refreshes the token first if it is close to expiring, so a
refresh Schwab rejects surfaces from an ordinary call rather than from anything
token-shaped. Its ``kind`` says what it calls for:

.. code-block:: python

  from schwaby import TokenErrorKind, TokenRefreshError

  try:
      r = c.get_quote('AAPL')
  except TokenRefreshError as e:
      if e.kind == TokenErrorKind.DEAD_TOKEN:
          # Only a new login flow fixes this. Retrying cannot.
          alert('refresh token is dead, log in again')
      else:
          if e.token_age is not None and e.token_age > 7 * 24 * 60 * 60:
              # Past Schwab's documented seven days. It may still recover,
              # so keep retrying, but someone should know. This runs on every
              # failed retry, so have alert() send it once.
              alert('token is past seven days and refreshes are failing')
          # A kind of TokenErrorKind.RATE_LIMITED asks for fewer requests:
          # wait longer before this one.
          retry_later()

``refused_no_body`` and ``app_refused`` are reported as what was seen, for a
program to decide on. Neither has been measured to mean a dead token: one
that treats them as dead should be one with a person to sign in again.

To refresh at a moment of your choosing, before the token is needed rather
than on the first call after it expires, see
:meth:`~schwaby.client.Client.ensure_token_fresh`.

Not every failure to refresh arrives as ``TokenRefreshError``. A server error
from the token endpoint raises :class:`~schwaby.utils.HTTPStatusError`, a
dropped connection raises one of ``httpx2``'s transport errors, and a success
whose body is not JSON at all raises a ``ValueError`` ---
:class:`~schwaby.utils.HTTPStatusError` sets out which is which. An unattended
program that means to retry has to catch those as well. A *refusal* whose body
is not JSON is a ``TokenRefreshError`` like any other refusal, whichever
version of ``authlib`` is installed.

.. autoclass:: schwaby.utils.TokenRefreshError

.. autoclass:: schwaby.utils.TokenErrorKind
  :members:
  :undoc-members:
  :member-order: bysource

A refresh error is often raised again, wrapped, by the code around the call.
These find it, and the code Schwab sent with it, without reading any
library's private attributes:

.. autofunction:: schwaby.utils.find_token_error
.. autofunction:: schwaby.utils.server_error_code

**Read the endpoint's answer from the attributes, not from the text.**
``rate_limited`` says the token endpoint answered with a 429, asking for
fewer requests, so the retry should wait longer. ``status_code`` is the HTTP
status it answered with, and ``endpoint_answered`` says whether it was asked
at all. All three are set the same way for a refusal with a JSON body and one
without, and they are what to branch on: the ``http_429`` code this library
gives a refusal with no body, or with a token in it, is a message, and could
be worded differently in a later release.

**Retrying a dead refresh token cannot work, and the retries are not free.**
A refresh token is good for seven days and is replaced only by the full
authorization_code flow, which needs a human at a browser. An unattended
application which treats that failure like a dropped connection will keep
asking, and the endpoint it is hammering is the same one the recovery needs.
``refresh_token_invalid`` is there so the two cases can be told apart.

It is deliberately conservative. Anything this library does not recognize
comes back ``False``, because an application stopped by a failure it could
have retried through is worse off than one which retried a little too long.

.. note::

   Schwab does not report this the way the standard describes, which is worth
   knowing if you are reading the raw error yourself. RFC 6749 section 5.2
   defines ``invalid_grant`` for a grant which is invalid, expired or revoked.
   Schwab answers with an outer code of ``unsupported_token_type`` -- which
   RFC 7009 defines for the *revocation* endpoint, and which describes nothing
   that happened -- and nests the real response as a JSON string inside the
   description::

     unsupported_token_type: 400 Bad Request: {"error_description":"Refresh
     token is invalid, expired or revoked","error":"invalid_grant"}

   Observed on a live account by letting a refresh token reach
   its expiry deliberately. Schwab documents neither the response nor the
   nesting, so this is one account on one day rather than a specification.
   Both placements are accepted, in case it is ever corrected.

Only a refusal from the token endpoint, a JSON answer that is not a usable
token, or a stored token that cannot be refreshed or sent, is reported this
way. A connection failure
while refreshing raises its ``httpx2`` exception unchanged, because a
connection failure while refreshing and one while fetching a quote are the same
problem and cannot be told apart from inside the library.

Note also that ``token_age`` remains available and remains worth watching.
Knowing a token is on its sixth day is how an application avoids the failure
altogether, rather than reacting to it.


----------------------
Advanced Functionality
----------------------

The token fetchers above assume a writable filesystem and a terminal that
accepts input. Both hold for almost everyone. They do not hold in a serverless
deployment, or anywhere the token has to live somewhere other than a file, and
the function below is the escape hatch for those cases: you supply the reading
and writing yourself.

**Important:** This is an extremely advanced method. If you read the
documentation and think anything other than "oh wow, this is exactly what I've
been looking for," you don't need this function. Please use the other helpers
instead.

.. autofunction:: schwaby.auth.client_from_access_functions


++++++++++++++++++++++++++++++++++++++++
Technical Details about Token Refreshing
++++++++++++++++++++++++++++++++++++++++

This section is for readers who are curious about the technical details of token
refreshing. If you just want to use the token, feel free to ignore it.

Under the hood, what the library and documentation calls a "token" is actually
*two* tokens: an access token and a refresh token. Both tokens are randomly
generated strings that are associated with your account, but they serve two
different purposes.

Access tokens are attached to each API request and are used to verify your
identity to the API server. Requests with no token or invalid tokens are likely
to be rejected. However, the access token is only valid for thirty minutes at a
time. Requests associated with an access token older than thirty minutes are
rejected. This is a security measure: if someone were to intercept your access
token, they can only place requests as you for thirty minutes.

In order to continue using the API after thirty minutes, we must request a new
access token. This is where the refresh token comes into play: if you attempt to
place an API call but ``schwaby`` detects that your access token is expired
(or about to expire), it will use the refresh token to request a new access
token from Schwab. Once it receives the new access token, it will place your API
request. This entire process is automatically managed and invisible to you.

This is where Schwab implements token expiration and other security measures:
requests for a new access token using a refresh token older than seven days are
refused, as :ref:`described below <invalid_client>`. There is currently no way
to make a refresh token last longer than seven days. Once you start seeing this
refusal, you have no choice but to delete your old token file and create a new
one.


---------------
OAuth Refresher
---------------

*Purely for the curious. Skip it if you already understand OAuth, or if you
just want to get going --- but it is worth a read if something is behaving
strangely and you cannot see why.*

OAuth exists to let applications reach one another's APIs with the minimum
trust possible. A full treatment is well beyond this guide; what follows is
just enough to make Schwab's version of it legible.

The first thing to understand is that the OAuth webapp flow was created to allow
client-side applications consisting of a webapp frontend and a remotely hosted
backend to interact with a third party API. Unlike the `backend application flow
<https://requests-oauthlib.readthedocs.io/en/latest/oauth2_workflow.html#backend-application-flow>`__, in which the remotely hosted backend has a secret
which allows it to access the API on its own behalf, the webapp flow allows
either the webapp frontend or the remotely host backend to access the API *on
behalf of its users*.

If you've ever installed a GitHub, Facebook, Twitter, GMail, etc. app, you've
seen this flow. You click on the "install" link, a login window pops up, you
enter your password, and you're presented with a page that asks whether you want
to grant the app access to your account.

Here's what's happening under the hood. The window that pops up is the
authentication URL, which opens a login page for the target API. The aim is to
allow the user to input their username and password without the webapp frontend
or the remotely hosted backend seeing it.  On web browsers, this is accomplished
using the browser's refusal to send credentials from one domain to another.

Once login here is successful, the API replies with a redirect to a URL that the
remotely hosted backend controls. This is the callback URL. This redirect will
contain a code which securely identifies the user to the API, embedded in the
query of the request.

You might think that code is enough to access the API, and it would be if the
API author were willing to sacrifice long-term security. The exact reasons why
it doesn't work involve some deep security topics like robustness against replay
attacks and session duration limitation, but we'll skip them here.

This code is useful only for fetching a token from the authentication endpoint.
*This token* is what we want: a secure secret which the client can use to access
API endpoints, and can be refreshed over time.

If your head is spinning, that is the correct response. Never write your own
implementation of a security protocol; there are well-tested ones, and
``schwaby``'s authentication module is a thin layer over them.



---------------
Troubleshooting
---------------

Authentication looks simple and is not, and the mistakes are easy to make.
The most common ones are below. If yours is not here, or the remedy does not
help, see the :ref:`help` page or `open an issue
<https://github.com/tom-hirt/schwaby/issues>`__.


++++++++++++++++++++++++++++++++++++
Suspicious errors during signin flow
++++++++++++++++++++++++++++++++++++

All API endpoints require an approved app. When your app is first created and
anytime it's modified, it will go into state ``Approved - Pending``, a
confusingly-named status indicating that the application is being manually
approved by Schwab. Until that status changes to ``Ready For Use``, you cannot
proceed using ``schwaby``, and you will encounter difficult-to-debug errors. A
listing of the types of errors people have reported:

 * ``401 Unauthorized`` errors in the signin flow
 * ``4001``, ``Session rejected``, or ``assertion_rejected`` payloads
 * ``Access Denied`` and ``You don't have permission to access
   "http://api.schwabapi.com/v1/oauth/authorize?" on this server"``

.. image:: _static/access-denied.png
   :width: 500
   :align: center

Approval appears to be a manual process, and most users have reported
transitioning to the ``Ready For Use`` status within a few days. Please note
this behavior is implemented on Schwab's side, so the library authors have no
ability to influence this or speed up your approval time.


.. _ssl_errors:

+++++++++++++++++++++++++++++++++++++++++++++++++++++++
Browser Warnings About Invalid/Self-Signed Certificates
+++++++++++++++++++++++++++++++++++++++++++++++++++++++

When creating a token using :func:`client_from_login_flow
<schwaby.auth.client_from_login_flow>`, you will likely encounter a warning from
your browser about refusing to connect to a site using an invalid or self-signed
certificate. Under the hood,
:func:`client_from_login_flow <schwaby.auth.client_from_login_flow>` starts a
server on your machine to listen for the OAuth callback. Since Schwab requires
``https:`` callback URLs, this server must declare an SSL context. However,
certificate authorities do not sign certificates for ``localhost`` or
``127.0.0.1``, and so the server must self-sign the certificate. As this would
be a security issue in any other context, your browser shows you a stern
security warning.

It is safe to ignore this warning and proceed anyway. *However*, you should
always verify that the address of the page displaying the warning matches your
callback URL.  :func:`client_from_login_flow
<schwaby.auth.client_from_login_flow>` prints a message reminding you of your
callback URL each time you run it.


++++++++++++++++++++
``401 Unauthorized``
++++++++++++++++++++

This is raised when a request carries an access token Schwab rejects. You
should not normally see it, because the library refreshes an access token
before it expires.

Deleting the token file and creating a new one clears it. The cause is not
understood, so a stack trace `on the issue tracker
<https://github.com/tom-hirt/schwaby/issues>`__ is genuinely useful.


.. _invalid_client:

+++++++++++++++++++++++
A Refused Token Refresh
+++++++++++++++++++++++

Tokens can only be refreshed for approximately seven days, at which point Schwab
refuses to refresh your token and you need to recreate it. :ref:`See here to
learn about how tokens work <token_expiration>`.

A refusal during a call reaches you as
:class:`~schwaby.utils.TokenRefreshError`, with authlib's ``OAuthError`` as its
``__cause__``. The seven-day refusal has been observed as
``unsupported_token_type`` with ``invalid_grant`` nested inside it, and that
sets ``refresh_token_invalid``. Text the endpoint sent is escaped and cut to
200 characters in the message and in the chained error's ``str()``, which is
what a traceback prints; that error's ``error``, ``description`` and ``repr``
are as authlib made them.

A refusal while a login exchanges its code for a token is different: it raises
:class:`~schwaby.utils.LoginExchangeError`, which is also authlib's
``OAuthError``, with the code in ``error``. So does a redirect that carries a
refusal, such as ``access_denied`` when the user declines, one with no code or
an empty one, one with a fragment, and one whose ``state`` does not match the
login's. It has no ``token_age`` or ``refresh_token_invalid``: no token exists
yet, and a code is good for one exchange, so every refusal there means starting
the login again. So does a refusal whose body is not JSON. A server error, or a
success whose body is not JSON, raises as it does during a call.

.. autoclass:: schwaby.utils.LoginExchangeError

.. autoclass:: schwaby.utils.LoginErrorKind
  :members:
  :undoc-members:
  :member-order: bysource

Each kind can cover several codes worded differently, such as
``access_denied`` and a ``state`` that does not match: ``error`` keeps the
code, and :func:`~schwaby.utils.server_error_code` the code Schwab itself
sent. A refusal with no JSON body is told apart by ``status_code``.

``endpoint_answered`` tells the two kinds of refusal apart: ``False`` for a
login refused before its code was sent, by the redirect or by the ``state``
check, and ``True`` for one the token endpoint refused, with its HTTP status
in ``status_code`` and ``rate_limited`` set for a 429. Either way the login
has to start again.

During a refresh, ``OAuthError: invalid_client`` is reported with
``refresh_token_invalid`` ``False``. RFC 6749 defines ``invalid_client`` as the
client -- the app key and
secret -- failing to authenticate, but Schwab's codes do not always mean what
the RFC says, and what Schwab means by this one has not been observed here, so
the library does not guess. It is retryable, and the recipe above keeps
retrying. If it keeps arriving, complete the login flow again, whatever
``token_age`` says, since Schwab does not hold exactly to seven days; if a
fresh login is refused the same way, check the app key and secret. Past seven
days the exception's message says so, and the recipe alerts.


+++++++++++++++++++++++++++++++++++++++
``OAuthError: unusable_token_response``
+++++++++++++++++++++++++++++++++++++++

A login raises this, as :class:`~schwaby.utils.LoginExchangeError`, when the
token endpoint answers with something that is not a usable token. Nothing is
written, so a token file already on disk is left as it was.

During a call it arrives as :class:`~schwaby.utils.TokenRefreshError`, with
``refresh_token_invalid`` ``False`` and this error as its ``__cause__``.
Nothing is stored, and the next call refreshes again.


++++++++++++++++++++++
Token Parsing Failures
++++++++++++++++++++++

``schwaby`` handles creating and refreshing tokens. Simply put, *the user
should never create or modify the token file*. If you are experiencing parse
errors when accessing the token file or getting exceptions when accessing it,
it's probably because you created it yourself or modified it. If you're
experiencing token parsing issues, remember that:

1. You should never create the token file yourself. If you don't already have a
   token, you should pass a nonexistent file path to
   :func:`~schwaby.auth.client_from_login_flow` or
   :func:`~schwaby.auth.easy_client`.  If the file already exists, these methods
   assume it's a valid token file. If the file does not exist, they will go
   through the login flow to create one.
2. You should never modify the token file. The token file is automatically
   managed by ``schwaby``, and modifying it will almost certainly break it.
3. You should never share the token file. If the token file is shared between
   applications, one of them will beat the other to refreshing, locking the
   slower one out of using ``schwaby``.

If you didn't do any of this and are still seeing issues using a token file that
you're confident is valid, please `file a ticket
<https://github.com/tom-hirt/schwaby/issues>`__. Just remember, **never share
your token file, not even with** ``schwaby`` **developers**. Sharing the token
file is as dangerous as sharing your Schwab username and password.

++++++++++++++++++++++++++++++
What If I Can't Use a Browser?
++++++++++++++++++++++++++++++

Launching a browser can be inconvenient in some situations, most notably in
containerized applications running on a cloud provider, or when running in a
notebook. ``schwaby`` supports two alternatives to creating tokens by opening
a web browser.

Firstly, the :ref:`manual login flow<manual_login>` flow allows you to go
through the login flow on a different machine than the one on which
``schwaby`` is running. Instead of starting the web browser and automatically
opening the relevant URLs, this flow allows you to manually copy-paste around
the URLs. It is a little more cumbersome, and it needs no browser on the machine
running it.

Alternately, you can take advantage of the fact that token files are portable.
Create the token on a machine that has a browser:

.. code-block:: python

  from schwaby.auth import client_from_login_flow

  client_from_login_flow(
          api_key='YOUR_API_KEY',
          app_secret='YOUR_APP_SECRET',
          callback_url='https://127.0.0.1:8182',
          token_path='token.json')

then copy ``token.json`` to the machine that will use it --- a container, or an
application in the cloud. Make sure you do not use the same token on two
machines at once, and delete the copy on the browser-capable machine as soon as
it has been transferred.
