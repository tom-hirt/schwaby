# Changelog

Release notes for `schwaby`, newest first, written for someone deciding whether
to upgrade.

`schwaby` began from [`alexgolec/schwab-py`](https://github.com/alexgolec/schwab-py)
and has been a separate project since 2.2.0; the
[README](README.md#where-this-came-from) has that story, and Alex Golec's
copyright and MIT licence are retained unchanged. The only trace of it in this
file is the numbering: **1.6.0 is the first release of this project, and
everything at or below 1.5.0 is `schwab-py`'s.** Those notes live in its
repository. 1.5.0 is still `schwab-py`'s latest release.

Entries are left as they were written on the day they shipped. The older ones
describe a project that still tracked upstream and still called itself a fork,
because at the time it did; none of that has been rewritten to agree with how
things work now, because a changelog edited to match the present is not a
record of anything.

Getting a fact wrong is the exception. Where an entry says something that was
untrue when it was written, it gets corrected and the correction says so.

---

## 5.3.0

*2026-11-05*

One change to what a token refresh or a login's code exchange keeps, and
changes to how this project's own tests and releases run. Nothing public is
added, removed or renamed. Code written for 5.2.0 runs unchanged unless it
relied on a token being stored from a response that was not a success.

### A token is kept only from a success

When the token endpoint answers a refresh, or a login's code exchange, with
a usable token at a status outside 200 to 299, such as a redirect, a 401 or
a 429, the token is no longer stored. 5.2.0 stored it, as authlib does. Now
the refresh raises `TokenRefreshError` and the exchange raises
`LoginExchangeError`, with the code `http_<status>`. `status_code`,
`rate_limited` and `endpoint_answered` are set as for any other refusal.
This holds for the synchronous and the asynchronous client alike. A response with a 5xx status is still handed to
authlib unchanged, as before.

Nothing has been seen sending a token at such a status. This closes the
case rather than answering a report of one.

### For contributors

- The publish workflow publishes only a published release's tag, and
  refuses a release commit that `main` does not hold. A failed publish is
  retried on the tag itself, with `gh workflow run publish --ref vX.Y.Z`.
  Before creating the release, `RELEASING.md` checks the tag as GitHub holds
  it.
- `tools/redproof.py` refuses an entry whose mutant does not parse where its
  original does: a Python file by compiling it, an sh or bash script by its
  shell's `-n`. Its pool, and the suite's `-n auto` workers, are bounded by
  `SUITE_CORES`, and its pool by `SUITE_MEMORY_GB`, when whatever runs the
  job sets them. `--touching BASE` runs only the entries a branch's test
  changes reach.
- The commit check refuses a staged file that carries a word on its
  `FORBIDDEN` list, and names the red-proof entries a commit's test changes
  reach.
- Each tool with a main, and both commit hooks, are tested as they are run:
  under `python -I`, from outside the checkout. The hooks are tested on
  Windows too.

---

## 5.2.0

*2026-10-24*

Two additions -- new attributes on the two token errors, and a socket a
login flow can serve on -- one change to what the login flow does with a
redirect, and a change to a log line. Nothing public is removed or renamed,
so code written for 5.1.1 runs unchanged.

### A refused token request says how the token endpoint answered

`TokenRefreshError`, raised from a call whose token refresh is refused, and
`LoginExchangeError`, raised when a login's code exchange is refused, each
gain three attributes:

- `status_code`: the HTTP status the token endpoint answered with, such as
  `429` or `401`; `None` when nothing was sent.
- `rate_limited`: `True` for a 429 and nothing else. The endpoint is asking
  for fewer requests, so wait longer before retrying.
- `endpoint_answered`: `False` when the error was raised before anything was
  sent. On a refresh, that is a stored token that cannot be used as it is.
  On a login, it is a redirect carrying a refusal, a fragment or no code, or
  a `state` that does not match.

All three are set the same way whether or not the endpoint's answer had a
JSON body. Telling a 429 from a 400 or a 401 used to mean matching this
library's `http_429`-style codes or its description text. Read the
attributes instead: the text is a message, and could change.

To carry the status, this library now raises the endpoint's JSON-bodied
refusal itself rather than leaving it to authlib. It is the same `OAuthError`
authlib raised, with the same class, `error`, `description` and `str()`,
under authlib 1.8 and 1.9. Compared against 5.1.1 over 450 refusals, it
differed in nothing but the three new attributes.

### A login flow can serve on a socket you already hold

`client_from_login_flow(..., callback_socket=sock)` serves the callback on a
TCP socket the caller has already bound to `127.0.0.1` at the callback
URL's port, instead of binding the port itself. A program that picks a free
port no longer has to let go of it before the flow binds it, which left a
gap for something else to take it. The flow puts the socket in the
listening state and leaves it open, and closing it is the caller's job. A
socket that is bound elsewhere, unbound, closed, or not TCP is refused with
`ValueError` before any server starts, and anything that is not a socket
with `TypeError`. Measured on Linux under the fork, spawn and forkserver
start methods, and in CI on Windows; macOS CI does not run the login-flow
server tests.

### The login flow skips a redirect that belongs to another login

A redirect whose `state` names a different login is now skipped with a
warning, and the flow goes on waiting for its own. 5.1.1 exchanged it, and
the exchange then failed with `LoginExchangeError` on the mismatched state.
This most often arrives from a browser tab finishing an earlier login late.
A held `callback_socket` makes that ordinary, because it goes on queueing
connections between flows. Measured: a retry on the same socket used to
receive the first flow's code. A redirect that names no `state` is still
exchanged, so it is still refused by name rather than timing out.

### `'Invalid Service'` on the stream is logged as Schwab's error

Schwab has once sent a stream frame whose service was the literal string
`'Invalid Service'`, in the second before it closed the connection. 5.1.1
logged it as a service it did not know and asked for an issue to be opened.
It is an error label, not a service, and is now logged as Schwab's error
without that request. It is still dropped, and still reported to
`add_error_handler` as `UnusableMessage` with `'Invalid Service'` as the
service, so a handler written for 5.1.1 sees it as before. Only that exact
name is treated this way.

### For contributors

- Each login-flow test holds its own callback port. Most serve on a socket
  from `bound_callback_socket()` in `tests/utils.py`. The two that test the
  flow binding a port by number pick one with `free_callback_port()`, and a
  collision in that gap fails as "port taken between pick and bind". A test
  whose flow is given any other loopback port fails. Two runs of the suite
  on one machine no longer fail each other, so the `--dist loadgroup`
  setting and the login-flow worker group from 5.1.1 are gone.
- Every test has a 60-second limit (`pytest-timeout`, now in `[dev]`), so a
  hung test fails by name.
- `requirements.lock` now pins the tested set for every supported Python,
  with markers where versions differ. `.python-versions` pins the exact
  interpreter builds CI runs, and a weekly workflow tests a fresh resolve
  without the lock.
- `git config core.hooksPath tools/hooks` turns on a light check at each
  commit (`tools/light_check.py`, with `pyflakes`, now in `[dev]`).
- `tools/redproof.py`, the mutation harness, is in the repository. It runs
  every entry on every run, keeps no verdicts between runs, and fails any
  run that writes to a tracked file. `tools/repeat.py` measures how often
  tests fail when run repeatedly.

---

## 5.1.1

*2026-10-23*

A fix to the login flow, and changes to how this project's own tests run.
Nothing public is added, removed or renamed.

### The login flow waits for its callback server to exit

`client_from_login_flow` stopped its callback server by signalling it and
returned without waiting for it to exit, and the port stays taken until it
has. So a second login flow started straight after the first could find the
port still in use: its own server died on "Address already in use", and the
flow raised `RedirectServerExitedError`. The flow now waits up to ten seconds
for the server to exit before it returns.

Measured with every core busy: without the wait, the port was still taken at
38 of 720 server starts; with it, at none. On an idle machine it did not occur
in 60 runs. A program that runs one login flow is unlikely to have met it; one
that starts another login straight after a first could have.

### For contributors

- `pytest tests/ -n auto` runs the suite on every core. `pytest-xdist` is in
  the `dev` extra, and `setup.cfg` passes `--dist loadgroup`, which keeps
  every test that runs the login flow on one worker so that no two of them
  use the callback port at once. Without `-n` the suite runs in one process,
  as before.
- `requirements.lock` is the exact set of packages the suite is tested
  against, resolved on Python 3.14. Installing `schwaby` never reads it;
  `setup.py`'s minimum versions are still what it requires.
- A test that passed only because of the order the suite ran in no longer
  depends on it.

---

## 5.1.0

*2026-10-19*

A security fix, and one change to what a refused token refresh raises. Nothing
public is removed or renamed.

### Security: secrets are never logged

**Upgrade if you log at `DEBUG`, or have ever shared, bundled or backed up a
log written by an earlier version.** Earlier versions wrote the access token
into logs in three places, measured against loopback stand-ins for Schwab:

- the streamer logged every request it sent at `DEBUG` on
  `schwaby.streaming`, and the LOGIN request carries the access token;
- bug-report mode, the mode meant for sharing logs, wrote that token into its
  "redacted" report. It registered a token only when one was loaded from a
  file, never on refresh, so a process that had refreshed and carried on
  leaked the current one;
- `websockets` logs every frame at `DEBUG`. Only its default elision of a
  long frame's middle hid the LOGIN token, and its documented
  `WEBSOCKETS_MAX_LOG_SIZE` setting turns that off.

Full account numbers reached `DEBUG` logs too, in REST response bodies and
`ACCT_ACTIVITY` frames.

**What an earlier version's `DEBUG` log can hold.** A scripted session through
every path above -- a login, a refresh, REST calls, the streamer, bug-report
mode -- found the access token and full account numbers, and no refresh token,
app secret or authorization code. An access token lasts thirty minutes, so a
line carrying one is dangerous for at most half an hour after it was written;
the refresh token, which lasts seven days, did not appear. The account numbers
do not expire.

Now no log this library writes carries a secret, at any level, with no mode to
enable. Every logger it owns replaces registered secrets in the message, a
traceback and a stack, however the record is rendered: by a handler, or by
anything else that reads records, such as an error tracker. Text this library
cuts short for a message, such as a frame quoted in an exception, is redacted
before it is cut.

Never logged: the app key and secret; every access, refresh and ID token,
including each one a refresh brings; the authorization code and the whole
redirect URL; full account numbers, from the first response or stream frame
that names each one. Account hashes and other identifiers are left alone, so a
log can still say which account a line is about. A secret this library has not
seen yet is still masked where its name says what it is: the `Authorization`
of a streamer request, an `access_token` field, a `code=` in a URL. An account
number has no such name, so one your own code puts in a request before any
response has named it is logged as it is.

`websockets`' frames are redacted before `websockets` renders them, so its
elision of a long frame's middle only ever cuts redacted text. Its records
still come from `websockets.client`, and a level, handler, filter or
`disabled` set there applies as it always did. If you pass `websockets` a
`logger` of your own through `websocket_connect_args`, it is wrapped rather
than replaced: `websockets` receives an adapter that redacts each frame before
your logger sees it and passes everything else through as it was logged. A
fragment of a message is not logged at all.

`httpx2`, `httpcore2` and `authlib` were measured logging none of these at
`DEBUG`, under authlib 1.8.0 and 1.9.1, and their loggers carry the redaction
anyway: `httpx2` logs every request's URL at `INFO`, and a URL naming an
account by its number rather than its hash carries the number.

A test runs a whole session -- login, refresh, REST, the streamer, their error
paths, bug-report mode -- with every logger at `DEBUG`, and fails on any secret
in any spelling, or on any ten-character piece of a token or key. Run
unchanged against 5.0.0, it fails.

A record is redacted when something renders it, so one nothing renders costs
nearly nothing, and a logger left at `DEBUG` under handlers at `INFO` costs
about what it did. A line that is written costs a few microseconds more.

**Caller-facing: a logged exception reaches your handlers as text.** Its
traceback is rendered and redacted before any handler sees the record, and the
record's `exc_info` is then cleared, as the standard library's `QueueHandler`
does: a formatter that renders the exception object again, as JSON formatters
do, would print it unredacted. Every standard formatter prints the redacted
text. A handler that reads `exc_info` directly, as an error tracker's logging
integration does, gets the message and the traceback text but not the
exception object, and a handler's own `formatException` is not consulted. Read
`exc_text` instead. This applies to records this library and its dependencies
log, not to your own. A record from them reaches your handlers with its
message already formatted and no arguments, and pickles as its redacted text.

The login flows still print the authorization URL to the terminal, because
you have to open it, and it carries the app key as `client_id`. It is printed,
never logged.

### A refused refresh with no JSON body raises `TokenRefreshError`

**Caller-facing.** When Schwab refuses a token refresh with a body that is not
JSON, such as an empty 401, `authlib` raised `json.JSONDecodeError` before 1.9
and `HTTPStatusError` from 1.9. `setup.py` sets only a floor, so which one you
got depended on what a fresh install resolved, and the documentation promised
the first. It is now `TokenRefreshError` under either version, like any other
refusal, and retryable, since nothing says the refresh token itself is dead.
If you caught `ValueError` or `json.JSONDecodeError` for this, catch
`TokenRefreshError`; code that handles refusals with a JSON body already does.

What did not change: a server error from the token endpoint is still
`HTTPStatusError`, and a successful response whose body is not JSON is still a
`ValueError`, under either version. A login's code exchange refused the same
way now raises `LoginExchangeError`.

---

## 5.0.1

*2026-09-26*

Documentation, the documentation build, and tests. With docstrings stripped,
the library's parsed trees are identical to 5.0.0's, `version.py` aside;
nothing public is added, removed or renamed. The one packaging change is to the
`dev` extra, which now lists `sphinx` itself.

- **An API index page.** Every class, function and method the documentation
  covers, on one page, grouped by module and class and linked to where each is
  described. It is generated when the documentation is built rather than
  written, so it cannot fall behind the pages. Enum members are counted rather
  than listed, since the streaming field enums alone hold hundreds.
- **Public names that had no documentation entry now have one**, several of
  which the pages already referred to by name: `StreamClient` itself, and so
  its constructor arguments; `StreamClient.handle_message`; all five streaming
  exceptions; `Client` and `AsyncClient`; `Client.Quote.Fields`;
  `set_enforce_enums`, which the client page told readers to call;
  `all_fields()`; and `OrderBuilder` itself, `build()`, the child-order methods
  that OCO and trigger orders are made of, and the tax-lot setters with
  `TaxLotMethod`. A test now fails on any public class or callable with no
  entry, unless it is listed as internal with the reason.
- **Cross-references that resolved to nothing are fixed, and the build now
  fails on the next one.** An unresolved reference renders as plain text and
  Sphinx says nothing, so 71 had accumulated --- 50 of them to `OrderBuilder`,
  and two to `GetQuote.Field`, a class that does not exist.
- **The streaming warning said one session per account. It is one per Schwab
  user.** Schwab's response code 12 says so, and streams for two different
  brokerage accounts were measured bumping each other off, which a limit per
  account cannot produce. So a second brokerage account is not a way around
  it, and by Schwab's wording neither is a second app registration --- that
  part rests on the text rather than a measurement.
- **The streaming exceptions section says which are raised and which only
  reach `add_error_handler`.** `UnusableMessage` is never raised, so an
  `except UnusableMessage` around `handle_message` never runs.
  `InvalidOrderException` said it was raised when building an incomplete order;
  nothing raises it.
- **Nothing here says more than was measured.** The docstrings are read from
  the code, and where the behaviour at Schwab is unknown they say so: what
  applies when no tax lot method is set, whether passing `all_fields()` to a
  view restores every field, and what a child order does on a partial fill.

### Corrections to earlier entries

The 5.0.0 and 3.0.3 entries are corrected in place, and each correction says
so. 5.0.0 called its book measurement "during market hours" when every frame
arrived before the open, and said a stale quote was still arriving "when the
capture ended" when the capture ran an hour longer. 3.0.3 said one streaming
session per account.

## 5.0.0

*2026-09-17*

One rename, and it is breaking. One documentation fix rides along.

- **The level two book's per-exchange `SEQUENCE` is now `QUOTE_TIME`.** It
  never held a sequence number. Measured against a live account before the
  open: all 8,904 per-exchange entries on `NASDAQ_BOOK` and `NYSE_BOOK`
  carried a value from inside the hour before their frame arrived, a median of
  one to two minutes. *(corrected in 5.0.1: this said "during market hours".
  Every book frame in the capture arrived between 07:20 and 07:29 Eastern,
  before the open.)* Schwab's own field table agrees, calling index 2 the
  quote time for this market maker's quote; only this library's reading of the
  number was wrong.

  It affects `PerExchangeBidFields` and `PerExchangeAskFields`, which relabel
  the innermost dictionaries of `NYSE_BOOK`, `NASDAQ_BOOK` and `OPTIONS_BOOK`
  messages. **Upgrading:** `entry['SEQUENCE']` becomes a loud `KeyError`, but
  `entry.get('SEQUENCE')` becomes a silent `None`, so grep for the name rather
  than waiting for an exception. Anyone who was treating the value as an
  opaque counter --- comparing it, sorting on it, deduplicating with it --- has
  a semantic change to make and not only a key to rename. Nothing else is
  renamed and nothing is removed.

  An alias for the old name would not have avoided the major version.
  `key_mapping()` iterates the enum rather than its members, deliberately, so
  that an alias cannot decide what a field is labelled --- the payload key
  changes either way, which is the half a consumer trips on.

  **The value is milliseconds since midnight Eastern, not epoch
  milliseconds.** That is the trap the old name hid and the new name does not
  fix by itself: every other time in a book frame is epoch milliseconds,
  `BOOK_TIME` included, so the obvious reading turns this morning's quote into
  a moment in January 1970 and raises nothing. The member documentation and
  the streaming page say what it is, how to turn it into an instant, that it
  was measured in September so daylight saving is unestablished, and that it
  is **not a freshness signal**: one participant's quote was still being
  re-sent half an hour after it was made, and was still arriving in the last
  frame before the subscription was dropped, so nothing bounds how stale one
  can be. *(corrected in 5.0.1: this said "when the capture ended"; the
  capture ran an hour longer, on other services.)* `OPTIONS_BOOK` shares
  the same two field tables and delivered no per-exchange entries when tried,
  so none of that was measured there.

- **`ChartEquityFields` fields 2 to 6 are documented as what they carry**: the
  minute bar's open, high, low, close and volume, which is what
  `ChartFuturesFields` has always said about the same five numbers. They read
  "Today's open price", "Previous day's close price" and "Today's trading
  volume". Measured on consecutive live bars: the close went 760.41, 760.53,
  760.3505, so it is not a previous close; the volume went 933, 636, 140, so
  it is not a running total. A reader summing that field for the day's volume
  got one minute of it and nothing said so. Names and numbers are unchanged,
  so nothing breaks here. The `SEQUENCE` three lines above them is genuine ---
  it rose by one per minute bar and matched across symbols in a frame --- and
  now says what was measured and what was not.

## 4.8.0

*2026-09-17*

New public surface, and measurements that replace things the documentation
called unknown. Nothing is removed or renamed. Two existing calls send
something different: a level one equity subscription that names no fields now
asks for five more, and a client date parameter given a `tzinfo` that returns
no UTC offset now sends the wall clock it was written with rather than that
clock converted from whichever zone the machine is in.

- **`NEWS_HEADLINE` is supported.** Schwab's streamer documentation lists
  thirteen services and none of them is news, but subscribing to a fourteenth
  returns `code: 0` and delivers headlines from Reuters, MT Newswire,
  GlobeNewswire and others. Every one was discarded before: the service was
  absent from the known set, so each frame logged "a service this version of
  schwaby does not know" and reached no handler. `news_headline_subs`,
  `news_headline_unsubs`, `news_headline_add` and `add_news_headline_handler`
  are the methods; `StreamClient.NewsHeadlineFields` names the fields, built
  from captured items rather than from the TD Ameritrade enum this project
  carried until 1.2.1, which is off by one and would mislabel every field
  while looking right. Two fields whose meaning is not established are
  `UNKNOWN_3` and `UNKNOWN_8`; naming them later would be a breaking change,
  and inventing a name now is worse. The service is undocumented, so it can
  change or stop without notice.
- **A `VIEW` command for the five level one services.**
  `level_one_equity_view` and its four siblings change which fields a live
  subscription delivers. Without it the only way to change fields was to
  subscribe again, which Schwab says replaces that service's entire symbol
  list. `VIEW` was accepted on all twelve services Schwab lists it for,
  answering `code 0` rather than the documented `29`; it is offered only where
  it was seen to do something. On the books, both screeners and `CHART_EQUITY`
  it was accepted and ignored. What was measured is a `VIEW` naming a symbol,
  which changed the fields for every symbol of the service; these methods name
  none, as Schwab's own description says to, and that form was seen on four
  frames, so the documentation calls its effect expected rather than
  established.
- **Response codes 26 to 29 are successes.** Schwab documents them as the
  success codes for `SUBS`, `UNSUBS`, `ADD` and `VIEW`, and every nonzero code
  raised. If Schwab ever answers a subscribe with `26`, a successful
  subscription raised `UnexpectedResponseCode`. Only `0` has ever been
  observed, so this is defensive: the tests are synthetic and say so.
- **`OrderBuilder.set_cancel_time`.** `GOOD_TILL_CANCEL` documented a cancel
  date that no method could set, under a `TODO` left there in 2024. It takes a
  timezone-aware `datetime` and refuses a string, a plain `date` and a naive
  `datetime`: on the order path an expiry meaning a different instant on
  different machines should stop the order rather than warn. Documented by
  Schwab, untested against a live order here.
- **`quantityType` on order legs.** `add_equity_leg` and `add_option_leg` take
  a keyword-only `quantity_type`, so dollar-denominated and whole-position
  orders are reachable. The key is emitted only when set, so orders built as
  before are byte-identical. **Schwab describes none of the three values and
  shows the field in none of its order examples, and a leg always carries a
  positive `quantity`** --- so a leg asking for 500 dollars is also a leg
  asking for 500 shares, and if Schwab ignores the field, that is what it
  buys. Untested against a live order here; the documentation carries this as
  a danger rather than a note.
- **The level one equity fields past Schwab's table.**
  `LevelOneEquityFields` gains `LAST_SIZE_FRACTIONAL` (52), `UNKNOWN_53`,
  `TOTAL_VOLUME_FRACTIONAL` (54), `BID_SIZE_FRACTIONAL` (55) and
  `ASK_SIZE_FRACTIONAL` (56). Schwab's field table stops at 51 and its own
  example request asks for 0-54; asked for more, a live account delivered
  these. **`LAST_SIZE` and `TOTAL_VOLUME` carry whole shares and drop the
  fraction**, so a print of less than one share reads `0` --- 81 of them in
  one morning --- and because an update carries only the fields that changed,
  the whole-share field is absent whenever only the fraction moved, on 56 of
  355 and 251 of 686 items. A handler reading `LAST_SIZE` to decide that a
  trade printed misses those. 55 and 56 always equalled `BID_SIZE` and
  `ASK_SIZE` and never carried a fraction, so their names follow 52 and 54 and
  are marked Unconfirmed. **A subscription that names no fields now asks for
  all of these**, so handlers receive five more keys per item.
- **What the screener documentation called unknown, measured with frames
  arriving.** A refused twelve-key `SUBS` still delivered the first ten keys
  sent, so the exception does not mean nothing is subscribed. `ADD` spends the
  same ten. `SCREENER_OPTION` caps at ten as well, with its own name in the
  refusal. The late `code: 0` that follows a refusal was seen on nine of nine
  refusals across both screeners, one of them an `ADD` answered `ADD command
  succeeded` --- 4.7.3 recorded this as happening on one of three, which was
  all the evidence then available.
- **A `tzinfo` that returns no offset is naive**, which is Python's own
  definition, and the client now treats it as one. It warned only when
  `tzinfo` was unset, so such a value passed in silence and was then converted
  from the host's local zone --- the same code sending a different instant
  from each machine, which is exactly what the warning exists to prevent.
  `set_cancel_time` already refused on the right definition; the two now agree,
  and the conversion they share does too.
- **The `VIEW` methods refuse what cannot be a field number.** A `str`,
  `bytes`, `bytearray` or `memoryview` is refused rather than iterated ---
  `b'12'` would otherwise ask for fields 49 and 50, which exist --- and a
  value that is not iterable at all raises this library's `ValueError` rather
  than escaping as a `TypeError`. A `TypeError` raised inside a caller's own
  generator is passed through untouched, because it says nothing about
  `fields`.
- **What each setter accepts**, the table in the order builder documentation,
  has a `set_cancel_time` row. The test that holds the table to the code found
  its setters by looking for two validator names, so a setter validating
  through a third was invisible to it and missing from the table while the test
  asserted nothing was missing.
- **What the news documentation now records**, all measured against a live
  account: `TEXT` was never longer than 100 characters in 449 items, so it is
  not the story; two identifiers were re-sent with changed content, so
  deduplicating on the identifier alone can drop a change; a story arrives
  under each subscribed symbol it names, 184 of 191 of them under all of
  theirs; translations carry their own
  identifiers; and `SUBS` and `ADD` each deliver one item per symbol
  immediately, some of it up to a day old.

## 4.7.3

*2026-09-12*

Documentation, and one test. The library's parsed trees are identical to
4.7.2's with docstrings stripped, `version.py` aside; nothing public is added,
removed or renamed.

- `SCREENER_EQUITY` takes at most ten subscription keys. An eleventh is refused
  with response code 19, which arrives as `UnexpectedResponseCode` carrying
  Schwab's own message: `SCREENER_EQUITY=10, DISCARDED=1`. The boundary was
  walked rather than read off a single refusal --- ten accepted, eleven refused
  --- and the ten belongs to the service rather than to the connection: twelve
  keys drew the same refusal while twenty-five `LEVELONE_EQUITIES` symbols were
  live and never refused. What the service keeps when it refuses is documented
  as unknown, because that needs delivered frames and a closed market delivers
  none. `SCREENER_OPTION` was not measured, so its own limit is documented as
  unknown rather than as ten, and `screener_equity_add` says only that code 19
  covers "Subscribe or Add" --- whether added keys share the ten was not
  measured either.
- A refused screener `SUBS` may be answered twice: a late `code: 0, "SUBS
  command succeeded"` arriving after the code-19 rejection. It reaches the
  orphan path, logs at INFO, and is deliberately not reported to
  `add_error_handler`, which reports a late rejection but not a late success ---
  so it is expected rather than this client losing a response. Sixty refusals in
  one session were each paired with one; a separate probe saw the pairing on one
  of three refusals and read a single frame after each, so how reliably it
  happens is not established.
- A test for the one value the stale-response guard's boundary decides: a
  response id one past the highest this client has issued must fail the request
  rather than be set aside as a late answer to an abandoned one. Loosening that
  comparison left the whole suite passing, so the strictness had no test behind
  it.

## 4.7.2

*2026-09-11*

One fix. Nothing public is added, removed or renamed.

- An `ACCT_ACTIVITY` content item whose `MESSAGE_TYPE` is the empty string and
  whose `MESSAGE_DATA` is a notice written as text is no longer logged as a
  message type nobody has captured. That is how the streaming documentation
  records Schwab's "Feature not supported" notice arriving: on at least 69 days
  across about three months of a live feed, most often around 00:30
  Eastern. It logged a warning asking for an issue to be opened, once in each
  process that received it. An empty type carrying a JSON object, broken JSON
  that starts like one, or no text at all is still logged, as is any non-empty
  type outside `ACCOUNT_ACTIVITY_MESSAGE_TYPES`, which is unchanged.

## 4.7.1

*2026-09-11*

Two fixes. `schwaby.utils` gains a `get_logger()`, as every other module already
has; nothing else public is added, and nothing is removed or renamed.

- `token_file_age` raises `ValueError` for a token file `client_from_token_file`
  refuses: one whose `token` entry is not a JSON object, or whose `token_type`
  or refresh token is neither a string nor null. It returned the age, so a
  monitor reported a healthy token that no client could be built from. These
  are the shapes the client checks before building a session; a token that
  passes can still fail at build, on an expiry authlib cannot read for
  instance, as it could in 4.7.0.
- `find_account_hash` skips an entry whose `hashValue` is missing, not a
  string or empty when its `accountNumber` is a string of ASCII digits other
  than the one asked for. Such an entry names another account, and it refused
  the whole list, so an odd linked or closed account would have stopped every
  lookup on the token, including those for accounts that were well-formed. No
  such entry has been seen from Schwab. A skipped
  entry logs a warning on `schwaby.utils` once per kind of entry, naming no
  account number. An unreadable entry that could be the account asked for
  still raises `UnusableAccountNumbersError`: one that is not an object, whose
  `accountNumber` is missing, not a string or not ASCII digits, or that is the
  account asked for with a hash that cannot be used.

## 4.7.0

*2026-09-11*

A public token-file writer, two fixes and a documentation note. One public name
is added, `schwaby.auth.token_file_writer`. Nothing is removed or renamed.

- `token_file_writer(token_path)` returns a `token_write_func` that writes the
  token file the way this library writes its own: atomically, readable only by
  the current user where the platform supports it, and through a symlink. Pass
  it to `client_from_received_url` or `client_from_access_functions`, which take
  a function rather than a path, and the file they leave is one
  `client_from_token_file`, `easy_client` and `token_file_age` read. A
  `token_path` that is not a `str` path, `bytes` included, raises `TypeError`,
  and one that is empty, names a directory, contains a NUL or is not in an
  existing directory raises `ValueError`, when the writer is made, before a
  login spends its code. A `~` is not expanded.
- A value whose class makes `__class__` raise is refused as an ordinary
  instance of that class is: by the enum checks, the app key and secret
  handling, the client's argument type checks and date formatting, the order
  builder's setters, `OptionSymbol` and `StreamClient.set_json_decoder`.
  `isinstance` asks `__class__` when the type does not match, and an abstract
  base asks it even for a real subclass, so that exception reached the caller
  in place of the refusal. A mock with a `spec` still passes where it did.
  `place_order`, `replace_order` and `preview_order` are not covered: an order
  whose `__class__` raises fails in JSON encoding too.
- A failed token refresh raises `TokenRefreshError` with the endpoint's code
  and description escaped and cut to 200 characters, in its message and in the
  chained `OAuthError`'s `str()`, which is what a traceback prints. A line break
  or terminal escape from the endpoint could forge a log line. That error is
  still the `__cause__`, the same object, with `error`, `description` and
  `repr` as authlib made them, so a caller classifying by walking the chain
  sees no difference.
- `execution_totals` now says that arithmetic done on the totals it returns is
  in the caller's own decimal context: with `Inexact` trapped there,
  multiplying a quantity by its average, or dividing a combined total back into
  one, can raise.

## 4.6.0

*2026-09-11*

A schwaby class for a refused login, a helper that adds up what an order
filled, and two fixes. Four public names are added to `schwaby.utils`:
`LoginExchangeError`, `execution_totals`, `ExecutionTotal` and
`UnusableOrderActivityError`. Nothing is removed or renamed.

Some calls now raise differently:
- a refused login raises `LoginExchangeError`, which is still an `OAuthError`.
  A redirect with a `state` that does not match, an empty `code` or a fragment
  used to raise authlib's `MismatchingStateException`, `MissingCodeException`,
  `MissingTokenException` or `MissingTokenTypeException`, which are not, and a
  fragment with a malformed expiry raised `ValueError`; one carrying
  `access_denied`, or no code at all, was sent to the token endpoint as a
  client-credentials grant and reported as that grant's refusal; and one whose
  fragment carried an access token, a token type and the login's `state` was
  written as the token, with no request;
- a stored token that has a refresh token, and an expiry authlib would not
  refresh while it is of use, is refreshed on the first call. A dead refresh
  token raises `TokenRefreshError` at once, where calls used to return 401 with
  no exception, and a token that cannot be sent and has no expiry authlib acts
  on is refreshed rather than reported with `refresh_token_invalid`;
- the enum and argument type checks raise `ValueError` for a caller's type whose
  name raises, rather than letting that exception through.

### A refused login raises `LoginExchangeError`

A login whose code exchange the token endpoint refuses, or answers with
something that is not a usable token, raised authlib's `OAuthError` itself. It
raises `schwaby.utils.LoginExchangeError` now. It is a `SchwabError` and still
an `OAuthError`, so `except OAuthError` keeps working, and `error` and
`description` carry the refusal's code and text. When this library refuses a
token response as `unusable_token_response`, the description is its own text;
an endpoint sending that code has its text treated like any other.
It also covers a redirect authlib refuses before any exchange, one whose
`state` does not match the login's or whose `code` is empty, and a redirect
carrying the authorization server's own refusal, such as `access_denied`, which
used to reach the caller as `unsupported_grant_type`.

Two redirects are refused before any request that were not:
- one with no code at all, as `missing_code`. authlib sent it to the token
  endpoint as a client-credentials grant carrying the app key and secret;
- one with a fragment, as `invalid_request`. authlib refused most, but took
  `#access_token=...&token_type=Bearer&state=...` carrying the login's `state`
  as the token and wrote it, with no request and none of the checks a token
  response gets.

Text from the redirect or the endpoint is escaped and cut to 200 characters,
and a value that is not a string is given as its repr, since it reaches
whatever logs the exception. authlib's own error is not chained as
`__cause__`: its text is not escaped, and a logged traceback prints it.
It has no `token_age` or `refresh_token_invalid`: no token exists yet, and a
code is good for one exchange, so every refusal means starting the login again.
A server error or a response that is not JSON raises as it did.

### A stored expiry authlib would not act on is refreshed on the first call

A caller-built token was never refreshed when authlib would not act on its
expiry, and once the access token lapsed every call failed with a 401 and no
exception. That covered an `expires_at` more than seven days off, one stored in
milliseconds for instance; one authlib cannot read as an int, such as an ISO
date or a float in a string, with no `expires_in` to fall back on; and no expiry
at all. When the token has a refresh token, such an expiry is taken as now,
so the first call refreshes, and a dead refresh token is reported as
`TokenRefreshError` at once. Without a refresh token, the token is used as it
is. This library has not written an expiry that far off since 4.5.1; 4.5.0
could, from a refresh answered with `expires_in` in milliseconds.

### `execution_totals`: what an order filled, per leg

`execution_totals(order)` adds up a parsed order's executions into
`{legId: ExecutionTotal(quantity, average_price)}`, in `Decimal` read from each
number's shortest text, with the price weighted by quantity and no contract
multiplier. Its arithmetic runs in its own decimal context, so the caller's
precision and traps do not change the result, and a sum is exact or refused: a
number too large, too small or too long to add up exactly raises rather than
being rounded. It counts only activities whose
`executionType` is `FILL`. A canceled or replaced order carries an `EXECUTION`
activity too, with
`executionType` `CANCELED` and execution legs whose quantities were never
filled, so adding up by `activityType` alone reports them as filled. A shape it
cannot read, or a `mismarkedQuantity` other than zero, raises
`UnusableOrderActivityError`, which is also a `ValueError`.

It was measured read-only over 423 historical orders: orders filled in one or
two executions; one vertical spread filled in one execution; and 25 canceled or
replaced orders. Not observed, and not claimed: a
partial fill before a replace, an option order filled in more than one
execution, a ratio spread, and a plain `EQUITY` instrument.

### A caller's type whose name raises

The enum check and the argument type check named a refused value's type with
`type(value).__name__`, which a metaclass can make raise, so the caller got
that exception rather than the `ValueError` naming the type. Both read the name
without the metaclass now.

## 4.5.1

*2026-09-10*

Fixes to token handling and to streaming reports, one helper made stricter, and
one piece of advice for an old token. Nothing is removed or renamed, but three
changes refuse input 4.5.0 accepted, each described below: a token expiry more
than seven days off, an account number that is not ASCII digits, and a stored
token whose `token_type` or refresh token is neither a string nor null.

Some failures also arrive as a different exception:
- a stream frame `json` cannot read at all, or a custom decoder's own
  `ValueError`, raises `UnparsableMessage` rather than `UnicodeDecodeError`,
  `TypeError`, `RecursionError` or `ValueError`;
- `find_account_hash` raises `ValueError` for a number that is not ASCII
  digits, where 4.5.0 raised `AccountNumberNotFoundError`, or
  `UnusableAccountNumbersError` when the list was malformed too;
- a token or creation timestamp that fakes its type raises `ValueError` rather
  than `TypeError`, and a stored token whose `token` entry is not a JSON object
  raises `ValueError` rather than `AttributeError`.

And a parse failure reported to `add_error_handler` can now carry bytes as its
`message`: a binary frame reaches the handler as it arrived.

### A token expiry more than seven days off is refused

An expiry counted as one authlib acts on if it was up to 10**9 seconds away,
which caught only an `expires_at` in milliseconds. An `expires_in` sent in
milliseconds, 1,800,000, is 20.8 days read as seconds: the token was stored,
authlib would not refresh it until then, and every call in between failed with a
401 and no exception.

Schwab documents a 30-minute access token and a seven-day refresh token, so the
bound is now seven days. A refresh response past it is refused before it is
stored, as `TokenRefreshError` with `unusable_token_response`, and a stored
token that cannot be sent and expires that far off is reported with
`refresh_token_invalid`. A login's token response past it is refused too, as
authlib's `OAuthError` with `unusable_token_response`, and nothing is written.
An expiry already past is still accepted.

### An old token's refusal stays retryable, and its message says to alert

For a refusal the library does not treat as terminal, such as `invalid_client`,
the message, the recipe in the docs and the troubleshooting entry gave three
different next steps. `refresh_token_invalid` is unchanged: Schwab has not held
exactly to its seven days, and stopping an application that could have
recovered is the worse mistake. Past seven days, a retryable refusal's message
now says to alert someone, the recipe alerts and keeps retrying, and the entry
and the `TokenRefreshError` docstring agree.

### `find_account_hash` refuses an account number that is not ASCII digits

4.5.0 refused whitespace around the number. A byte order mark or zero-width
space kept from a file, a NUL, an empty string from an unset environment
variable, a hyphen or a letter still raised `AccountNumberNotFoundError`, which
reads as an account the token does not cover. Schwab's account numbers are ASCII
digits, so anything else now raises `ValueError`, without naming the number.

### Loading a token

- **A stored token's shape is checked before a session is built on it.**
  - A mapping that is not a `dict` is used as one. Every call on it used to
    raise `AttributeError`.
  - A `token_type` or refresh token that is neither a string nor null raises
    `ValueError` when the client is built. authlib used to raise
    `AttributeError` or `TypeError` past `TokenRefreshError`, or send a list
    refresh token as bytes.
  - A null one counts as absent, which is what a null refresh token already
    amounted to. A null `token_type` no longer makes every call on a live
    token raise `AttributeError`.
  - The token passed in is copied rather than changed.
- **A caller-built token with `expires_in` and no `expires_at` authlib can
  read** (missing, null, or failing `int()`), when it has a refresh token, has
  its expiry taken as the moment the client is built, so the first call
  refreshes it. The expiry used to be counted from each build, so a process
  restarting more often than that never refreshed a lapsed token.
- **`token_file_age` no longer logs at INFO.** A monitor polling it wrote an
  INFO line per read. Building a client still logs the load at INFO.
- **`easy_client(asyncio=True)` in a notebook returns an async client.** The
  notebook route dropped the flag.
- **A faked `__class__` on a token or its creation timestamp** is refused with
  `ValueError` rather than escaping as `TypeError`. Refusing a timestamp too
  large for a float now says "a finite number a float can hold".

### Streaming

- **A frame `json` cannot read at all raises `UnparsableMessage`**, and is
  reported to `add_error_handler`. A binary frame that is not UTF-8, a bytes
  frame that is not JSON and a frame nested past the interpreter's depth each
  ended the receive loop with an unrelated exception and no report. A custom
  decoder's own `ValueError` is reported the same way, and the message names it
  as a possible cause. `json_parse_exception` is whatever the decoder raised,
  not always a `JSONDecodeError`.
- **`HeuristicJsonDecoder` lets the decode error stand on a bytes frame.** Its
  repair step raised `TypeError` on bytes, which ended the loop unreported.
- **`UnparsableMessage`'s text quotes and cuts the frame.** A line break in the
  frame forged a line in anything printing the exception. `raw_msg` still holds
  the frame as it arrived.
- **A custom decoder's string subclass** can no longer forge a line in the
  unknown-field warning, the relabel-failure report or the handler-failure log
  line, and one whose `__str__` raises no longer ends the receive loop.
- **A debug line no longer raises** on a value `json.dumps` fails on in an
  unexpected way, or on one whose type's name raises.
- **The error-handler recipe bounds `service` with `reprlib.repr`.** With
  `%r`, a deeply nested `service` raised inside the handler, and the alert was
  lost. The exception is still shown whole.

### Documentation

- A refreshable token is replaced as its expiry nears, 300 seconds early, not
  once it passes.
- The `unusable_token_response` entry covers a refusal during a call as well as
  at login.
- A refusal while a login exchanges its code raises authlib's `OAuthError`
  itself, with no `token_age` or `refresh_token_invalid`.
- A creation time ahead of this machine's clock gives a low or negative age;
  the page says why that is not refused.
- The token lifecycle example reads the age as a float, so it also works with
  a `Decimal` age from a token store.
- Three literals inside bold text rendered as raw backticks. A packaging test
  now checks every page and docstring, and `docutils` joins the `dev` extra.

## 4.5.0

*2026-09-10*

Three helpers for work every Schwab consumer ends up writing, a fix to
option-symbol parsing, and Schwab behaviour measured on a live account.
Nothing is removed or renamed, but two changes refuse input 4.4.2 accepted:
`OptionSymbol.parse_symbol` refuses a symbol that is not its fixed layout, and
a token whose creation timestamp is not a finite number, such as a string,
`null`, `true` or NaN, no longer loads.

### `schwaby.utils.find_account_hash`

Every account-specific call takes an account hash rather than the account
number. The README and the docs fetched one with
`get_account_numbers().json()[0]['hashValue']`. A token can cover several
accounts and Schwab documents no order for the list, so that picks an account
rather than finding one.

`find_account_hash(account_numbers, account_number)` takes the parsed JSON body
of the response and returns that account's hash. It makes no request, so the
synchronous and asynchronous clients share it.

- The account number must be a `str`, which is how Schwab's schema types it.
  An `int` is refused rather than converted, because converting drops a
  leading zero and then matches nothing.
- An account number with whitespace around it, as one read from a file can
  have, raises `ValueError` rather than reading as an account the token does
  not cover.
- An account the token does not cover raises `AccountNumberNotFoundError`.
  That is an ordinary answer, and you can catch it on its own.
- A list that is not the documented shape, or that has the number twice,
  raises `UnusableAccountNumbersError`, which is also a `ValueError`.
- No message names an account number.

Every example now uses it. `get_account_numbers`' docstring said its response
is a mapping; the response's JSON body is a list.

### `schwaby.auth.token_file_age`

`token_file_age(token_path)` returns the seconds since the token in a token
file was created by a login, without building a client. It uses the same
calculation as `Client.token_age()`, on the creation time stored in the file
now rather than when a client was built. It reads that stored time, not the
file's modification time: the file is rewritten every time the access token is
refreshed.

A token that is not a mapping, has no `token` entry, or has a creation timestamp
that is not a finite number is now refused with a `ValueError` saying so, by the
client as well, and so is a token nested too deeply to read. Any finite real
number is still accepted as the timestamp, including the `Decimal` a token store
such as DynamoDB hands back. With one, a refresh failure raised `TypeError` in
place of `TokenRefreshError`; it no longer does. In 4.4.2:

- A list or string that did not contain `creation_timestamp` was refused as a
  changed token format. One that did, `null` or a number raised `TypeError`, a
  missing `token` entry raised `KeyError`, and a deeply nested file raised
  `RecursionError`.
- A timestamp that was a string or `null` loaded, then raised `TypeError` once
  the client was asked for the token's age.
- A timestamp of `true`, `false`, NaN or infinity loaded without complaint.
  `true`, `false` and negative infinity read as a token too old to use, so
  `easy_client` discarded it and ran the login flow. With NaN or positive
  infinity the age never reached `max_token_age`, so `easy_client` never
  retired the token.

`easy_client` no longer recovers from such a file by running the login flow, as
4.4.2 did for a timestamp that read as too old. It raises the `ValueError`, and
the file has to be deleted before logging in again.

### `schwaby.contrib.util.parse_message_data`

An `ACCT_ACTIVITY` item's `MESSAGE_DATA` is a JSON string, sent empty on the
`SUBSCRIBED` ack and as plain prose for notices. `parse_message_data(value)`
returns:

- the `dict`, for a JSON object;
- `None`, for empty or whitespace-only text;
- the text itself, for anything else.

A notice never raises. A `dict` is returned unchanged. Text that starts like a
JSON object but is not one raises `UnparsableMessageData` rather than reading
as a notice; that covers truncation, corruption and nesting past the
interpreter's depth. Before looking for the `{`, it skips whitespace, and
Unicode format and control characters and separators such as a byte order mark
or a zero-width space. Other characters are not skipped, even ones that render
as nothing. After the payload only whitespace is dropped, so a payload followed
by a zero-width space raises.

### `OptionSymbol.parse_symbol` refuses a symbol that is not its fixed layout

An option symbol is read from fixed positions, and `parse_symbol` did not check
that a symbol had them. A root padded with one space instead of two parsed with
an expiration in 2061, and a seven-digit strike parsed as 12.5 instead of 125:
a different contract, with no error. It now refuses anything but 21 characters:
a root of one to six characters padded with spaces, six digits of expiration,
`C` or `P`, and eight digits of strike. This predates 4.4.0.

### Documentation

Measured on a live account, and stated no wider than the evidence:

- **Which fill quantity is the running total.** `CumulativeQuantity` is filled
  so far, `ExecutionQuantity` is this execution alone, and the posting
  `Quantity` is the order's quantity. The first two agree on single-execution
  fills, which is why reading the wrong one survives testing.
- **`PriceImprovement`'s unit is not established.** 94 of 188 fills checked fitted
  a total for the fill, and 9 fitted a price per share.
- **Fill timestamps.** `ExecutionTimeStamp` reads as US Eastern time with no
  offset, checked in summer only. `VenuExecutionTimeStamp` sits beside it and
  differed on every fill checked.
- **Charges.** Every fill checked carried its commission and fees, zero
  charges included.
- **The "Feature not supported" notice.** It arrives on the data channel with
  an empty `MESSAGE_TYPE`, most often around 00:30 Eastern. The notify-channel
  list named it too; over about three months it arrived only on data, so that
  sentence is narrowed.
- **Price history.**
  - A longer thirty-minute range did not reach further back.
  - Thirty-minute candles are labelled with their open time, measured over
    four regular sessions of one liquid ETF. The newest one returned can still
    be forming: seen on liquid ETFs over one day, and not on a thinly traded
    one.
- **Quotes.** `get_quotes` can return HTTP 429. No rate is given, because none
  was measured.
- **Account balances.** `cashAvailableForTrading` is sent as `0.0` when it is
  zero, not omitted.

## 4.4.2

*2026-09-10*

Fixes from a review of 4.4.0 and 4.4.1. Nothing is removed or renamed.

### A token that is not usable is no longer stored, from a refresh or a login

During an automatic refresh, the token endpoint's response was taken as the new
token unless it carried an `error` key. A body such as
`{"message": "Unauthorized"}` was written to the token file and kept in memory,
so every call after it raised `TokenRefreshError` without contacting Schwab —
after a restart too — until someone completed the login flow again, while the
exception said the failure might be transient. A list or a string was stored on
the session and then failed, so every later call failed without contacting
Schwab until the process restarted; after `[]`, the exception even said to log
in again. A refresh response whose `refresh_token` was present but empty erased
the stored refresh token for good. And the token a login exchanges its code for
was written without any check at all.

A token response is now refused before it is stored unless it carries a
non-empty string access token, the bearer type, and an expiry authlib acts on —
built, as authlib builds it, from `expires_at` or `expires_in` — and any
`refresh_token` it carries is a non-empty string. That holds on every client,
including one returned by a login flow, and at the login itself. A refresh
raises `TokenRefreshError`, nothing is written or kept, and the next call tries
again; a login raises authlib's `OAuthError`, with the code
`unusable_token_response`, before writing anything. Measured with a mocked token
endpoint; what Schwab's endpoint sends in these cases has not been observed.
This predates 4.4.0.

A stored token that authlib cannot send, and will never replace by itself, was
reported as retryable — a token file damaged before this release, say. authlib
replaces a token only as an expiry it acts on nears, and only with a refresh
token. If the expiry is missing, cannot be read as an int, or is too far off to
be reached, or there is no refresh token, the token fails locally, before
anything is sent, on every call. It is now reported as needing a new login; one
authlib will refresh stays retryable. Schwab's own `unsupported_token_type`
rejection is a different class and is unchanged: terminal with `invalid_grant`
nested inside it, retryable without.

### A bare number with a huge exponent no longer escapes `decode_decimal`

A string such as `"1E1000000000000000000"` — an exponent near 10^18 or beyond
— raised `decimal.InvalidOperation`. That is neither a `SchwabError` nor a
`ValueError`, so it escaped the one `except` callers are told to write and cost
the whole message rather than the field. It was reachable from any JSON string
field handed to the decoder, and it predates 4.4.0. It is refused as
`UnusableDecimalScale` now.

### Venue text cannot forge a log line

The refusal message joined unknown key names unquoted, so a key containing a
newline planted a second line — a fake `CRITICAL`, for instance — wherever the
exception was logged, including through the logging recipe on the streaming
page. Key names are quoted now.

The streaming client's own log lines and error reports could be forged the same
way through a custom `StreamJsonDecoder`: by a value whose `__repr__` carries a
line break, a class name that does, or an exception whose message quotes venue
text. All three are escaped now, including the line breaks past ASCII — `\x85`,
U+2028 and U+2029.

The error-handler recipe on the streaming page formatted the service name with
`%s`. That name is whatever Schwab sent, so a plain JSON frame could plant a
line in an alert copied from the recipe. It uses `%r` now.

### Values from a custom JSON decoder are read as plain types

`json.loads` never produces a subclass, but a custom `StreamJsonDecoder` can,
and several decided their own value or escaped: `str`, `float`, `int` and
`Decimal` subclasses, a mapping whose `get` answers differently on a second
read, a key whose comparison raises, and an object claiming a class it is not,
such as a mock. `decode_decimal` now routes on an object's real type, reads a
mapping once through its own `items()`, and reduces each value — a bare string
included — to its exact built-in type. Any `collections.abc.Mapping` is a
decimal object now; a `UserDict` or `mappingproxy` was refused as "not a
number". A mapping whose `items()` cannot be read is refused, and so is one
carrying the same key twice once its names are reduced to plain text. On the
stream, a `MESSAGE_TYPE` that was such a subclass could drop its whole data
element, valid items with it; it is delivered now, and a frame logged when
absorbed can no longer end the receive loop the same way.

One consequence of reading a mapping through `items()`: a key its `get` would
return but its `items()` does not list is not read, so such a mapping decodes as
though the key were absent — a mantissa hidden that way decodes as zero.
`json.loads` never produces one.

### Documentation

- **Getting a mid price.** 4.4.1 said to take `Mid`'s magnitude or compute
  `(Bid + Ask) / 2`, and under a reduced decimal context both round silently.
  Use `copy_abs()`, or do the arithmetic under a context with enough precision.
- **`HTTPStatusError` can come from the call itself.** The session refreshes an
  expired access token on the way past, and a server error from the token
  endpoint raises this class out of a call such as `get_quote()`, so keep the
  call inside the `try`. It does not cover network errors: a timeout or a
  dropped connection raises one of `httpx2`'s transport errors.
- **Classifying by substring.** The runtime warning and the
  `ACCOUNT_ACTIVITY_MESSAGE_TYPES` comment now say that `CANCEL` and `UROUT`
  also match the retired id of a price change on the option orders captured,
  while the order goes on working under a new id.
- **A rejected token refresh.** Below 500, JSON that is not a usable token —
  whether or not it carries an `error` key — raises `TokenRefreshError`, and a
  body that is not JSON raises a `ValueError`, usually `json.JSONDecodeError`.
  The recipe for catching a failed refresh says so too, and `TokenRefreshError`
  records a transient `unsupported_token_type` rejection seen once.
- **A refused token refresh, in troubleshooting.** The entry was headed
  `OAuthError: invalid_client: refresh token invalid` and said to recreate the
  token. The seven-day refusal has been observed as `unsupported_token_type`
  with `invalid_grant` nested inside, which sets `refresh_token_invalid`;
  `invalid_client` has not been observed here, is reported with
  `refresh_token_invalid` `False`, and the entry now says to check `token_age`
  when it arrives.
- **`replace_order`.** The old id of a replacement does not necessarily read
  `CANCELED`; `TERMINAL_STATUSES` covers `REPLACED` too.
- **Narrowed to the evidence:** `lo` can be wider than 32 bits, not always;
  the refusal cost any fill principal above roughly $4,295 at `signScale` 12,
  not "most"; option orders have been captured, and what their symbol keys
  hold has not been observed; where the account activity evidence comes from;
  and what `CancelType` does and does not show; and `Mid`'s sign is stated for
  the sell orders checked, not for every sell order.

### Corrections to earlier entries

The 4.4.1, 4.4.0, 4.3.0 and 4.2.0 entries are corrected in place where they
said more than the evidence supports, and each correction says so.

## 4.4.1

*2026-09-10*

### A decimal object's `lo` is no longer held to 32 bits

`decode_decimal` refused every decimal object whose `lo` was wider than 32
bits, and on Schwab's feed that includes **any fill principal above roughly
$4,295** at `signScale` 12. It raised `UnusableDecimalScale`, so nothing
decoded to a wrong number — but the value was gone, and a refused value writes
no log line of its own. *(corrected in 4.4.2: this said "most fill
principals". How many fill principals the archive holds in total is not known,
so that was unsupported.)*

The cause was reasoning where there should have been measurement. The
encoding is a serialized .NET `System.Decimal`, whose 96-bit mantissa is laid
out as three 32-bit members, `lo`, `mid` and `hi`, and this read `lo` as the
first of them. Schwab does not send that layout. Measured over 5,843 decimal
objects from a recorded `ACCT_ACTIVITY` archive:

- none carried `mid` or `hi`, and every `lo` was a string of digits;
  *(corrected in 4.4.2: this said every one carried `lo` alone, but a decimal
  object need not carry `lo` at all — `{}` and `{"signScale": 12}` were
  measured as zeros, on one payload.)*
- 129 had a `lo` above 4294967295, and all 129 were refused. They were
  `PrincipalAmmount` on `OrderFillCompleted` — Schwab's spelling — and
  `EstimatedPrincipalAmount`, `EstimatedPrincipalAmnt` and `EstimatedNetAmount`
  on `OrderCreated`;
- read whole, all 33 refused fill principals equalled price times quantity
  from the same message.

At `signScale` 12 that is anything from 4294.967296 up: any fill principal
above roughly $4,295.

A lone `lo` is now the whole mantissa, bounded by the 96 bits the encoding
allows. An object that carries `mid` or `hi` is still read as the three-member
layout with each member held to 32 bits; that layout has never been captured,
and the docs mark it Unconfirmed. Everything else that was refused still is.

**If you catch `UnusableDecimalScale` per field, log the exception.** Only an
unrecognised key writes a line of its own; a refused value does not, so a
handler that swallows the exception keeps no record that a real value was
dropped. The streaming page's example logs it.

Two smaller fixes to the same function, both from review:

- **A member that is an `int` subclass is reduced to a plain `int`.** The
  new lone-`lo` path formatted the member straight into the result, so a
  subclass's own `str` or `format` could make it decode as a different number
  or raise past the one `except` callers write. `json.loads` never produces
  one; a custom `StreamJsonDecoder` can.
- **A corrupt `mid` or `hi` is named as itself.** Members are checked widest
  first, so the refusal no longer blames a ten-digit `lo` that is ordinary on
  its own.

### A quote's `Mid` decodes negative on a sell order

*(corrected in 4.4.2: this was measured on the sell orders whose message
carried `BuySellCode` — creation messages only — and is not established for
every sell order.)*

Documentation only; the decoder is right about what Schwab sends. `Mid`'s
magnitude is exactly `(Bid + Ask) / 2` from the same quote, 563 of 563, but its
sign followed the order's side: on the 182 quotes whose message carried
`BuySellCode`, every buy was even and every sell odd. That is the opposite of
the amounts, which are negative on a buy. Take the magnitude with
`copy_abs()`, or compute the mid from `Bid` and `Ask` under a context with
enough precision. *(corrected in 4.4.2: this said to take the magnitude or
compute the mid, and both `abs()` and ordinary arithmetic apply the caller's
decimal context, so under a reduced precision each silently rounds — the
error `decode_decimal` exists to avoid.)*

### Corrections to earlier entries

Two passages in the 4.2.0 entry — its notes on `decode_decimal`, and its
documentation note about the decoder published in 4.1.0 — said that reading
`lo` alone truncates anything above 4294.967295. That was reasoned from the
.NET layout and is wrong about the wire. Both are corrected in place, and say
so. *(corrected in 4.4.2: this paragraph described the second passage as an
earlier entry. Both corrected passages are in the 4.2.0 entry.)*

## 4.4.0

*2026-09-10*

Nothing is removed or renamed. Three names are added, one new `WARNING` can
appear on a stream you already use, and the account activity documentation
is corrected where it said more than was ever observed.

### `schwaby.utils.HTTPStatusError`

The exception a client call's `raise_for_status()` raises, under a name this
library owns. It is `httpx2.HTTPStatusError` re-exported rather than
subclassed, so an `isinstance` check against either name holds.

It exists because the failure it prevents is silent. `httpx2` and `httpx`
share no exception hierarchy, so a handler written against the other package
never matches — no error, the `except` simply does not run — and the only way
to know which class to catch was to find out which HTTP package happened to be
installed. `httpx2` is the only one this library depends on.

It is **not** a `SchwabError`, and the docs say so: `except SchwabError` is
documented as covering what this library defines, and this class is defined
elsewhere.

### `Client.Order.TERMINAL_STATUSES`

The five order statuses after which an order stops changing: `FILLED`,
`REJECTED`, `CANCELED`, `EXPIRED` and `REPLACED`. `AsyncClient.Order` is the
same class and has it too.

**It holds strings, not enum members.** `Client.Order.Status` is a plain
`Enum`, so `'FILLED' in {Status.FILLED}` is `False`, and a set of members
would call the raw string a REST response carries in `order['status']` not
terminal — silently, on a fill path. The values are taken from the members, so
the two cannot drift.

What it claims is narrower than its name:

- Schwab's documentation lists every status and never says which are
  terminal. This is a reading, not a contract.
- `FILLED`, `CANCELED` and `REJECTED` have been observed ending an order, and
  `REPLACED` ending all five ids a price change retired on an option order.
  `EXPIRED` is included by reading, has never been captured, and is tagged
  Unconfirmed.
- `REPLACED` is terminal for that order id; the work continues under a new
  one, and no key captured on the retired order names it.
- It describes the REST `status` field. The account activity stream's message
  types are a different vocabulary and do not map onto it one for one.

### `StreamClient.ACCOUNT_ACTIVITY_MESSAGE_TYPES`, and a warning for any other type

Schwab documents the `ACCT_ACTIVITY` `MESSAGE_TYPE` vocabulary nowhere. The
new constant holds the fourteen types that have been captured on the wire.

**Use it to notice a type, not to classify one.** Only fourteen have been
captured, so an exact match against the set misses a real message whose type
has simply never been recorded. Classify by substring, case-insensitively, as the
streaming documentation has said all along.

A message of any other type is delivered to your handler exactly as before.
What changes is that it no longer passes silently: it is logged **once per
type, at `WARNING`, on the `schwaby.streaming` logger**. It is log-only and
does not reach `add_error_handler`, because nothing failed.

**Expect it to fire.** Expiry and partial fill have never been captured, so a
process may log each of those once. If you
alert on `WARNING` from `schwaby.streaming`, you will see them. The line names
the type; reporting it is how the set grows.

The comparison is case-insensitive, so `ORDERCREATED` is not reported as new.
A shortened token such as `ORDERUROUT` is a different type and is. At most 64
distinct types are named per process, each cut to 64 characters, and quoted so
a newline in one cannot forge a second log line.

The constant sits on `StreamClient` rather than beside the field on
`AccountActivityFields`, because a set in an `Enum` body silently becomes a
member of it — which would add a field to every account activity message.

### Documentation

- **An order can fill before `place_order` returns.** A single account
  activity push has been seen carrying a market order from `OrderCreated`
  through `OrderFillCompleted` before the placing call handed back the order
  id. A handler that decides an order is its own by looking up ids it already
  holds files that fill as someone else's, with no error. Seen once, not
  reproduced, and no rate is given.
- **Level one equity bid and ask sizes are documented as shares**, not round
  lots. Tagged Unconfirmed: the evidence is magnitude, not a measurement
  against an order of known size, and the comment names the measurement that
  would settle it. Equities only.
- **A price change to a working order gives it a new order id, and retires
  the old id with `CancelAccepted`.** A handler that reads that as "this order
  is gone" concludes a working order was cancelled while it goes on working
  under the new id. REST reads the retired id as `REPLACED`, but its activity
  reads `CANCELED` and nothing captured on it leads to the new order. The link
  is on the stream: the new id's `ChangeCreated` carries `ParentSchwabOrderID`,
  naming the id it replaced. Captured over eight changes on two option orders;
  the change and monitor types are in the exported set with their captured
  spellings, and no `OrderReplaced` type appeared. *(corrected in 4.4.2: REST
  was read on the five retired ids of one option order, not across all eight
  changes, and not for a replacement made through `replace_order`.)*
- **The account activity observation log no longer states what was never
  observed.**
  - The order id was said to appear under at least seven spellings. One has
    been observed, `SchwabOrderID`.
  - The symbol was said to appear under four keys "in descending order of
    preference". Lowercase `symbol` was never observed, the order was a
    parser's policy rather than Schwab's, and the page now says which message
    carries which key.
  - A claim about what an option leg's symbol holds is withdrawn; what an
    option order's symbol keys hold has not been observed. *(corrected in
    4.4.2: this said no option order had been captured, which was untrue —
    4.4.0 itself documents option-order captures.)*
  - Five message types that carry no symbol at any depth are now named, since
    a symbol lookup on them returns nothing rather than raising.
  - All five terminal statuses were titled "observed to be terminal" while
    two had never been captured.
  - Keys naming related orders' ids end in `SchwabOrderID` too, so the order
    id block now says to match that key exactly.

## 4.3.0

*2026-09-10*

### An unrecognised key in a decimal object is refused, and said out loud

4.2.0 refused a decimal object carrying any key it did not recognise, with no
message beyond the exception. This keeps the refusal, adds the report, and
records why the middle ground between them does not exist.

The middle ground is the obvious idea: ignore a key on an object that looks
complete, refuse one where a component is missing, on the reasoning that the
unknown key might be that component renamed. It was built and then broken
three times, each break a **$6.86 limit price decoding as $6,860,000**:

    {"lo": "6860000", "SignScale": 12}                       # key absent
    {"lo": "6860000", "signScale": null, "SignScale": 12}    # key null
    {"lo": "6860000", "signScale": 0,    "SignScale": 12}    # key zero

"Missing" has more spellings than anyone enumerates, and the third cannot be
closed at all — `signScale: 0` genuinely means scale 0, so refusing it would
refuse a real `AskSize`. Measured against a captured session, the middle
ground also bought nothing it claimed to: under a rename that keeps the old
key at its default, **27 of 37 objects still decoded to a wrong number**,
which is exactly what no guard at all does. It had the cost of refusing and
the risk of not refusing.

So: **any unrecognised key refuses the object**, and the key is logged once,
on the `schwaby.contrib.util` logger, which `enable_bug_report_logging` now
collects.

**The cost is real and is chosen.** A key Schwab adds arrives on every decimal
object at once, so every money and quantity field raises until `schwaby` knows
it. That is loud, it happens on the first message, the key is named, and
`UnusableDecimalScale` is catchable per field — so a caller wrapping each
field, as the docs have always said to, keeps the rest of the message. The
alternative on this feed is a silent wrong price on a funded account.

One thing this closes that the middle ground could not: a renamed *member* of
the mantissa. `{"lo": "705032704", "Mid": 1, "signScale": 12}` is $5,000.00
and decoded as `705.032704` — seven times low, silently — because `lo`, `mid`
and `hi` are omitted individually when zero, so the absence of one carries no
information. An earlier draft of this entry called that an inherent gap. It is
inherent only if you are trying to decode around the unknown key.

*Corrected in 4.4.2: the three-member layout this paragraph relies on was
reasoned from .NET, never captured. Schwab sends the whole mantissa in `lo`,
and none of 5,843 decimal objects in a recorded archive carried `mid` or
`hi`.*

### A field Schwab adds to a stream is now visible, not just survivable

Nothing else in the library *rejects* an unexpected field — measured rather
than assumed. An unknown numeric field id on a stream you subscribe to is
delivered to your handler under its numeric key while every known field still
relabels, so a new field reaches you rather than breaking you. That half
already worked and has not changed.

What did not work is finding out. It was silent, so a field could appear and
sit in your messages indefinitely with nothing to notice it. It is now logged
once per field per table on the `schwaby.streaming` logger.

Not reported through `add_error_handler`: nothing was absorbed and nothing
failed. It is a change in the venue, which is an operator's concern rather
than a caller's.

An unknown **service** or **channel** is reported both ways — the log and
`add_error_handler` — because those messages are dropped rather than
delivered, and **every distinct name is guaranteed to appear in both**, up to
the first 64 of them on a client. That guarantee is separate from the
coalescing, which counts per *kind*: five new services at once left the fourth
unnamed in any log line and any callback, and a dropped service is a worse
event than an unnamed field.

It is held per client, not per process. Held module-wide it deduped across
every client in one program, so a second client — with its own error handler,
which had never heard anything — was told about three of five new services and
never about the other two. The unknown-*field* report stays process-wide,
because that one is log-only and the log is process-wide.

Services and channels count against their own budget rather than the fields'.
Sharing one meant 254 declared field ids across 14 tables competed with them
for 64 slots, so a single frame of unknown ids could exhaust it and leave a
dropped service unnamed — the lesser event consuming the budget of the worse
one.

The discriminator is that Schwab's field ids are numeric strings. `key`,
`seq`, `delayed` and `assetMainType` all arrive alongside the numbered fields
and none of them is a field, so reporting anything merely missing from the
table would log four lines per message — a flood, which hides the one line
that matters.

### A service or a channel this version does not know is now reported

A new *field* still reaches your handler. A new **service** cannot — there is
no handler to route it to and no field table to relabel it with — and a
**channel** this version does not read is a whole compartment of the frame
nobody looks in. In both cases the messages are dropped, and until now they
were dropped without a word.

Both now go through the same path as every other message this client cannot
use: a `WARNING`, and an `UnusableMessage` through `add_error_handler`,
counted per kind and coalesced after the first few so a systematic change
cannot turn into a log flood. The name that appeared is on the exception's
`.message`.

Two deliberate quiet cases. A service you registered no handler for is **not**
reported — that is your own choice, and a line per message on a feed you
ignored on purpose is noise. A `notify` frame that names no service is not
reported either; it is not required to name one.

The frame is not dropped over an unread channel. The compartments that *are*
understood still hold real data, and refusing the whole frame would turn an
addition into an outage — which is the failure this whole group of changes
exists to avoid.

`_KNOWN_SERVICES` is a declared set rather than something derived, because two
walks over the same ground disagree. A test holds it against the subscribe
methods and the handler registrations, so a service added to those without
being added here fails rather than being reported as unknown on the very
traffic it was added to receive.

The channel check reads the frame's own keys, and `set_json_decoder` promises
only "the decoded JSON" — so a decoder returning a mapping that cannot be
enumerated, or whose keys are unhashable, skips the check rather than failing
the frame. `json.loads` produces only dicts, so this matters only if you have
supplied your own decoder.

### A non-finite `requestid` no longer ends the receive loop

Not part of the schema-drift work — found while reviewing it, and present
since long before this release.

`json.loads` maps the bare JSON literals `1e999` and `Infinity` to
`float('inf')`, and `int(inf)` raises `OverflowError`. Two guards around
`int(frame['response'][0]['requestid'])` caught `AttributeError`, `IndexError`,
`KeyError`, `TypeError` and `ValueError` — not that one. So an ordinary JSON
number literal in a `requestid`:

- ended the receive loop,
- killed the in-flight request with an exception that is **not** a
  `SchwabError`,
- and reported nothing to `add_error_handler`.

`NaN` takes the same route and raises `ValueError`, which is caught — so one
of the two non-finite spellings was covered, which is why the guard read as
complete. Locks were released cleanly, so nothing wedged.

This is the exact failure `_read_and_route` exists to remove: with no request
outstanding the identical frame was logged and harmless, and the framing
dependence it was written to close was still there, one exception type wide.

## 4.2.0

*2026-09-09*

### `schwaby.contrib.util.decode_decimal`

New, and the first thing in this release that is code rather than prose.

Schwab streams money and quantities on `ACCT_ACTIVITY` as a serialized .NET
`System.Decimal` — `{"lo": "6860000", "signScale": 12}` — not as numbers. The
encoding is undocumented publicly; Schwab's Trader API support has confirmed
the conversion. Every consumer ends up writing this, and the first
attempt is reliably wrong in at least one of five ways, each of which is a
**wrong number rather than an error**:

- the mantissa spans `lo`, `mid` and `hi`, so reading `lo` alone truncates
  anything over `4294.967295` at `signScale` 12 — `$5,000.00` decodes as
  `$705.03`; *(corrected in 4.4.1: this was reasoned from the .NET layout and
  is wrong about the wire. Schwab sends the whole mantissa in `lo`, and holding
  it to 32 bits refused any fill principal above roughly $4,295 at
  `signScale` 12. Corrected again in 4.4.2: this note said "most fill
  principals", which the archive does not establish.)*
- an odd `signScale` means negative, and the sign is not the side;
- an object with a `signScale` and no mantissa is **zero**, not unknown — it
  arrives that way as `LeavesQuantity` on the final fill of a completed order,
  where reading it as unknown reports a complete fill as outstanding;
- an object with a mantissa and **no `signScale` at all** is scale 0, not an
  error and not the usual 12. `AskSize` and `BidSize` arrive as
  `{"lo": "19200"}` beside an `Ask` of `{"lo": "13720000", "signScale": 12}`
  in the same quote — $13.72 with 19200 on the ask. Guessing 12 there is
  wrong by a factor of a million; refusing it loses both sizes on every
  quote;
- `10 ** (signScale // 2)` on a hostile scale builds an astronomical integer
  and hangs the thread, which a per-item `try`/`except` cannot rescue.

It returns a `decimal.Decimal`, never a float, so a decoded price can be fed
straight back into `set_price`.

Every member is validated by shape — an unsigned 32-bit integer, or a string
of ASCII digits inside that range — rather than coerced with `int()` and the
resulting exceptions enumerated. That distinction is the whole of this
function's review history and it is worth stating, because coercion fails in
both directions: `int()` raises a bare `ValueError` on some corruption and
silently *accepts* the rest. `int(1.9)` is 1, `int(True)` is 1, `int('1_0')`
is 10 under PEP 515, and `int('٣')` is 3. Bare values go through the same
validation, because `decimal.Decimal`'s parser is more permissive still — it
takes `'1_0'`, `' 12 '`, `'+12'`, `'12_'` where `int` refuses it outright, and
every Unicode decimal digit. Anything that is not a number this encoding could
have carried raises `UnusableDecimalScale`, which is both a `SchwabError` and
a `ValueError`, so one `except` around each field keeps the rest of the
message.

#### What five review rounds on it found

Worth recording, because the function is 80 lines and every round found
something, and because the shape of what they found is the argument for
shipping a tested one rather than a documentation sample:

- **The caller's `decimal` context changes the answer.** Construction of a
  `Decimal` is context-free; every *operation* is not, including `scaleb` and
  unary minus. A consumer who sets `getcontext().prec = 6` elsewhere in their
  process — an ordinary thing to do when formatting money — got `1234.57` for
  a price of `1234.5678`. Fixed by building the value from a string with the
  sign in it; a first fix that negated a string-built positive was exact for
  positive values and rounding for negative ones, which is the branch
  `EstimatedPrincipalAmount` arrives on.
- **A scale is bounded, not computed with.** Leaving it to the arithmetic is
  not enough: every scale between roughly two and four million underflows to a
  zero that compares equal to zero, and zero is what a completed fill looks
  like.
- **The library refused a shape Schwab actually sends.** The `{"lo": "19200"}`
  case above. It raised, and the docstring said the shape had never been
  observed, while a capture carried eight of them. Found by decoding real
  traffic rather than by reading the code — the only finding of the five
  rounds that reading could not have produced.
- **A guard's exception list covers the shapes you thought of.** Three
  consecutive rounds each added an exception type to the coercion path and the
  next round found a shape raising a different one, or none at all. Replaced
  with shape validation, which is what the bullet above describes.
- **The fix landed on one branch and not its sibling.** Members were validated
  while bare values, four lines away, still went to `Decimal`'s permissive
  parser. Both go through one list now, driven by one test, so they cannot
  drift apart again.

None of this changes a decoded value for any payload anyone has captured: all
37 decimal objects in the reference capture decode to the same numbers before
and after, except the eight that used to raise.

This library still models nothing else inside `MESSAGE_DATA`, and this does not
change that — it decodes one primitive encoding, in `contrib`, alongside the
JSON decoders the streaming docs already recommend. The documentation carried
this as a code sample until now, which meant every consumer copied it; the
sample was itself wrong about `mid` for a few hours today, which is the
argument for shipping one tested version instead.

### Documentation

**The `Duration` enum lists four values Schwab rejects for equity orders**, and
now says so where someone setting a duration will see it. `IMMEDIATE_OR_CANCEL`,
`END_OF_WEEK`, `END_OF_MONTH` and `NEXT_END_OF_MONTH` come back `HTTP 400`. The
enum mirrors Schwab's schema rather than what the equity endpoint takes, and the
rejection happens at *placement* rather than at build — so `OrderBuilder` builds
the order without complaint and it fails on the live path. A time-in-force
table built from this enum is wrong, and the note on the enum's own docstring
is not where a reader building one looks.

**The `{lo, signScale}` decoder published in 4.1.0 was wrong for large
numbers.** It read only `lo`. The encoding is a serialized .NET
`System.Decimal` whose 96-bit mantissa spans `lo`, `mid` and `hi`, so at
`signScale` 12 anything above **4294.967295** overflows into `mid` and a
`lo`-only decoder returns a plausible smaller number with no exception —
`{"lo": "705032704", "mid": 1, "signScale": 12}` is $5,000.00 and decoded as
$705.03. A principal, a total or a share price reaches that easily. Reasoned
from the .NET layout rather than observed: no captured payload has carried a
non-zero `mid`, but a value that does not fit in 32 bits cannot be sent in `lo`
alone.

*Corrected in 4.4.1: the premise was wrong. Schwab sends the whole mantissa in
`lo`, so a value that does not fit in 32 bits is sent in `lo` alone, and reading
`lo` whole is correct. Measured over 5,843 decimal objects, none carried `mid`
or `hi`.*

Three more facts about the same encoding: the conversion has been
**confirmed by Schwab Trader API support**, which stated it while confirming it
remains publicly undocumented; a decimal object with a `signScale` and no `lo` is **zero, not
unknown**, measured on a `LeavesQuantity` arriving on the final fill event of a
completed order, where reading it as unknown makes a complete fill report as
outstanding; and the sign is not the side — direction comes from `BuySellCode`,
and the odd-`signScale` branch exists so a genuinely negative field does not
decode positive.

**A normalizer in front of the setters defeats the `bool` rejection.** A
helper written to adapt to 4.1.0 to turn a `numpy.int64` quantity into
a plain `int`; `float(True)` is `1.0` and `(1.0).is_integer()` is `True`, so it
turned `True` into `1` and rebuilt the silent one-share order 4.1.0 was released
to close — in code written to accommodate that very fix. Documented next to the
accepted-types table.

**Three hazards were re-graded from `warning` to `danger`**, by consequence
rather than by feel: the asymmetric argument order on `cancel_order` and
`get_order`, which sends a well-formed request naming the wrong account or the
wrong order; `replace_order` returning a new ID, where anything tracking the
old one concludes the position closed while the replacement is still working;
and `'{:.2f}'.format(value)` rounding a buy limit up, which is a fill one tick
worse than intended. All three can cost money, which is the line `danger` is
for. The docs carry more of these than they did; the levels are what changed.

**A removed date left a sentence broken.** That same docstring read "for every
asset type. As of / Schwab accepts only" — release step 4 strips dates from
shipped documentation and greps for dates, not for the wreckage removing one
leaves. It shipped that way through 4.1.0. Fixed, and there is now a check that
no line ends with `As of`, since nothing but a date follows it.

**Removed the `httpx` warning from the HTTP client page.** It showed the wrong
code — `import httpx`, `except httpx.HTTPStatusError` — inside a `warning`
block, which is the most visually prominent thing on the page. Someone skimming
for a retry pattern could reasonably have copied it, which is the opposite of
what it was for.

It was written for readers arriving from `schwab-py`, and this has not been a
fork since 2.2.0. The section already says responses come from HTTPX2 and links
it, and every example on every page imports `httpx2`, so the fact is carried by
the code rather than by a caution about a package this library does not depend
on.

## 4.1.0

### An order field that is not a number is refused, and says which field

`OrderBuilder`'s numeric setters --- `set_quantity`, `set_stop_price_offset`,
`set_price_offset` and `set_activation_price` --- validated by asking whether
`float()` could parse the value. That is the wrong question in both directions,
and the two directions failed differently.

**Things that are not numbers answered no, and were let through.**
`set_quantity(None)` reached `quantity <= 0` and raised
`TypeError: '<=' not supported between instances of 'NoneType' and 'int'`,
naming neither the field nor the value. The two offset setters have no
comparison after the check at all, so they *accepted* it --- and since a `None`
is dropped when the order is built, `set_stop_price_offset(None)` produced an
order with no offset on it and no complaint. A trailing stop with no offset is
a different order from the one that was asked for, placed without an error.

**And things that are not numbers answered yes.** `float(b'1')` is `1.0`, so a
`bytes` passed the check and was stored as `bytes`; `json.dumps` refuses those,
so the order was not wrong so much as unsendable, discovered at the point of
sending it.

**A string on a numeric field was taken as well.**
`set_price_offset('abc')` built `{"priceOffset": "abc"}` and sent it. Schwab
types these fields as numbers; the string branch exists for the *price* fields,
which are strings in the schema, and the validator could not tell the two kinds
of field apart. It is now told which it is guarding, the same way it already
distinguished them for `decimal.Decimal`.

All four now raise a `ValueError` naming the field, and take an `int` or a
`float` and nothing else. The predicate is `_build_object`'s own, deliberately:
a `fractions.Fraction` and the numpy scalar types are numbers mathematically
and still cannot be serialized into an order, so they raise here rather than
at `build()` with `vars() argument must have __dict__ attribute`.

The price fields are unchanged: they still take a string or a
`decimal.Decimal`, and an unparseable price string is still left between the
caller and Schwab.

### What actually changes, measured against 4.0.0

Generated by running both versions rather than written from memory --- two
earlier attempts at this table were wrong, in opposite directions, about what
4.0.0 did.

| 4.0.0 | now | which calls |
| --- | --- | --- |
| setter raised `TypeError` | `ValueError` naming the field | `set_quantity` / `set_activation_price` given a `str`, `None` or `bytes`; the prebuilt templates likewise, since their leg goes through the same check |
| setter raised `ValueError` | `ValueError`, different message | `set_quantity(False)`, `set_activation_price(False)` --- `False <= 0` already caught these |
| **setter returned, `build()` then raised `TypeError`** | **setter raises** | a `fractions.Fraction` anywhere; `bytes` on the two offsets |
| **built an order** | **setter raises** | the two offsets given a `str`, `None`, `True` or `False`; `set_quantity(True)`; `set_activation_price(True)` |

Only the last two rows are breaking, and the fourth is the one that matters: a
call that produced a placeable order now raises. The third moves a failure from
`build()` to the setter, which is the same failure arriving somewhere useful.

Note what is *not* claimed: that Schwab refuses a string there. Schwab's schema
types `price` and `stopPrice` as `number($double)` too, and this library sends
those as strings because that is what the venue takes --- so the schema does
not separate the two groups, and nothing here has been measured against a live
account. The reason is that the four setters disagreed with each other:
`set_quantity` and `set_activation_price` already refused a string through
their range check, and the two offsets accepted one only because nothing
downstream compares them to zero. That is an accident of which setters have a
range check, not a decision anyone made.

### The four numeric setters now agree with each other

Worth stating on its own, because it changes what working integrator code has
to pass. Through 4.0.0 the builder enforced three different rules and nothing
said so:

| setter | 4.0.0 accepted | 4.1.0 accepts |
| --- | --- | --- |
| `set_price`, `set_stop_price` | `str`, `Decimal` | unchanged |
| `set_quantity`, `set_activation_price` | `str` → `TypeError`, `bool` → **accepted** | `int`, `float` |
| `set_price_offset`, `set_stop_price_offset` | `str`, `int`, `float`, `bool`, `None` | `int`, `float` |

`set_quantity(True)` used to be accepted, silently: `True` is an `int`
subclass, so it passed every check. Measured, it serializes as
`{"quantity": true}` — a JSON boolean, not `1`. What Schwab does with that has
not been observed, so the claim here is the checkable one: the order was built
without complaint carrying a value that is not a number in the schema, which is
unsendable rather than wrong. It is refused now, on all four setters.

The two price setters are unchanged and are deliberately the opposite rule:
they take a `str` or a `decimal.Decimal` and refuse an `int` or a `float`,
because a float cannot carry a price exactly. So the builder has two rules, not
one. `docs/order-builder.rst` now states the accepted types **per setter**, in
a table the test suite checks against the validators — in both directions, so
it fails if the table lies and if a validator drifts from it.

### Documentation, measured against a live account

Findings that could not have come from reading the library, each measured:

**The account-activity payload.** A cancel emits `ExecutionCreated` carrying a
non-zero `ExecutionQuantity` on an order that filled nothing — only
`ExecutionTransType` distinguishes it from a fill, so a consumer reading
quantities by shape books a phantom fill on every reprice. `ResponseType` and
`RouteStatus` arrive as *either* the label or its ordinal, two milliseconds
apart on the same order. Numbers are `{lo, signScale}` and an **odd**
`signScale` means negative, so a decoder handling only the even case flips the
sign on every cash-direction field. Timestamps arrive as `{}` rather than null,
`MESSAGE_DATA` is a JSON string, and content items within one message are not
in lifecycle order.

**The client surface.** `place_order`, `replace_order` and `preview_order` take
the account hash first; `cancel_order` and `get_order` take the order ID first.
Both are opaque strings, so swapping them raises nothing locally.
`Utils.extract_order_id` returns an `int` where order IDs are strings
everywhere else. And `easy_client` opens a browser when the token ages out,
which on a daemon is a window nobody sees — the install page now points
unattended processes at `client_from_token_file`.

### Tests for eighteen statements that had none

Reading every uncovered line in the package rather than the percentage. Most
were input validation that simply had no test --- `OptionSymbol`'s rejection of
a malformed expiration date, an unknown contract type and a wrong expiration
*type*, plus its accepted-but-unexercised `datetime.datetime` argument;
`convert_enum_iterable`'s enforcement arm; `Utils.set_account_hash`, which is
public and documented and was called by nothing; `_describe_error` on a body
that is not a JSON object; the token-refresh classifier on a non-string
description; `set_json_decoder`'s type check; and receiving before `login()`.

One was worth more than the rest. **`client_from_access_functions(asyncio=True)`
was never called by any test.** Every `client_from_*` entry point funnels into
that function, and its asyncio branch --- which selects the async session and
client, and wraps the token writer in an `async def` --- was unreached. The
line that actually writes the token there carried a `# pragma: no cover`, so it
was excluded from measurement as well as untested: a write that quietly did
nothing on the async path would have shown up as neither a failure nor a gap.
It is tested and measured now, and the pragma is gone.

Coverage went from 98.40% to 99.31%, which is not the point; what changed is
that fourteen of those statements were reachable from a public method with a
plausible argument.

## 4.0.0

**The package you import is now `schwaby`, not `schwab`.**

```python
import schwaby                      # was: import schwab
from schwaby.auth import easy_client
```

Every method, argument, enum and return value is what 3.0.3 shipped, and
nothing was added or removed from the API.

**Three things beyond the import line carry the name, and a find-and-replace
over imports alone will miss all of them.**

*Loggers are now `schwaby.*`.* They were `schwab.auth`, `schwab.client.base`,
`schwab.streaming` and `schwab.debug`, and they are now the same names under
`schwaby`. Anything configuring or filtering on them needs updating --- which
includes the incantation this project's own help page recommends:

```python
logging.getLogger('schwaby').setLevel(logging.DEBUG)   # was: 'schwab'
```

A log filter that silently stops matching is the failure worth calling out
here: it does not raise, and the symptom is an absence.

*Fully-qualified class names in a few error messages* now read `schwaby.…`.
Anything matching on those strings needs the same treatment. They are
diagnostics rather than an interface, but a test asserting on one will fail.

*The login flow's internal status path is now `/schwaby-internal/status`.* This
is the one actual behaviour change, and it is here because of what the release
makes possible. `client_from_login_flow` decides whether the thing listening on
your callback port is its own server by fetching that path and requiring a 200.
It was `/schwab-py-internal/status` --- the path `schwab-py` serves, for the
same purpose --- so a `schwab-py` login flow already holding the port answered
it, the check read that as success, and the browser delivered the authorization
code into the other project's queue while this one timed out. Unreachable while
the two could not be installed together; merely unlikely now. Nothing calls
this path but the library itself.

### Why

The distribution has been `schwaby` since 2.6.0 while the package stayed
`schwab` — the same directory `schwab-py` ships. That is what made the two
impossible to install together: `pip` does not know they are the same project,
so installing one over the other left both registered and both claiming the
same files, and `pip uninstall schwab-py` afterwards deleted the shared files
and destroyed the install.

Every workaround for that was a workaround for the shared name:

- an import-time check that scanned `sys.path` to detect the collision, which
  grew to 224 of the 234 lines in `__init__.py`. Ten remain.
- 17 tests and 15 red-proof cases holding it in place
- a `WARNING` block in the README and an install-order caveat in
  getting-started, both of them telling you to uninstall `schwab-py` first
- 3.0.2 spent entirely on reworking the check after it false-positived on this
  repository's own source tree

**Keeping `import schwab` working and removing the collision were mutually
exclusive.** Any compatibility shim would still have to ship a directory called
`schwab`, which is the collision. There was no version of this that gave both.

All of it is gone. `__init__.py` is ten lines.

### What this buys

`schwaby` and `schwab-py` now install side by side and do not interfere. They
ship different packages, so both can be present and you import whichever you
mean — which also makes it possible to compare the two against one account,
something that was not possible before.

### Migrating

Change the imports. There is no compatibility shim and there deliberately is
not one: a package named `schwab` is the thing that collides, so shipping one
to ease the transition would reintroduce exactly what this release removes.

If you pin `schwaby<4` you keep 3.0.3, which continues to work and continues
to collide.

**If you currently have both `schwaby` and `schwab-py` installed, reinstall
`schwab-py` after upgrading.** This is the one upgrade that breaks something,
and it breaks quietly.

`pip install -U schwaby` uninstalls 3.0.3 first, and uninstalling a
distribution deletes every path it recorded at install time. 3.0.3 recorded
`schwab/*` --- the same paths `schwab-py` had overwritten --- so pip removes
`schwab-py`'s files while still listing it as installed. Measured: 21 of the 25
files under `schwab/` go, `pip check` reports no broken requirements, and
`import schwab` still *succeeds*, because the four files 3.0.3 never shipped
survive and keep the directory a package. The first real use is what fails:

```
>>> import schwab.auth
ModuleNotFoundError: No module named 'schwab.auth'
```

Either uninstall `schwab-py` before upgrading, or run
`pip install --force-reinstall schwab-py` afterwards. Nothing needs doing if
you only ever had `schwaby`.

---

## 3.0.3

Documentation only. No behaviour changed, and the library's code is
byte-identical to 3.0.2 with docstrings stripped.

Upgrade if you read the documentation. Nothing about a running program changes.

### Three worked examples did not run

The first code on the HTTP client page imported `client_from_manual_flow` and
then called `easy_client`, which it never imported. Its asyncio counterpart did
the same and added two more faults: `redirect_uri`, which is not a parameter of
`easy_client`, and a missing `app_secret`, which is required. Copying either
gave a `NameError` or a `TypeError` before reaching the part you came for. Two
more examples used `httpx2` without importing it.

The test that exists to catch this could not see it. `DocExampleTest` resolves a
call only when the block imported that name from `schwab`, so the wrong import
concealed the wrong argument. With the imports corrected it catches the argument
error, verified by putting it back.

### Two links sent readers to the wrong place

`.. _orders-section:` labelled *Current Quotes*, and `util.rst` links to it when
explaining how to get an order ID. `:ref:`enable_logging <help>`` had its label
and target swapped, so it went to the top of the help page rather than to the
section showing the incantation.

### The logging instructions did not enable logging

`help.rst` told you to add a handler to the root logger and stop there. The root
logger defaults to `WARNING` and this library emits twelve debug calls and four
info calls against three warnings, so that snippet dropped almost everything it
existed to show. Anyone following the page to gather evidence for a bug report
got a program that looked instrumented and emitted nothing.

### The venue selector was documented nowhere

`set_requested_destination` appeared in no page. The section titled "Requested
Destination" documented `set_destination_link_name`, which is not how a venue is
chosen — Schwab types `destinationLinkName` as a free string and `Destination`'s
values belong to `requestedDestination`. Both are documented now, with the
distinction stated.

### What the movers and screener endpoints actually do

Measured against a live account rather than read from Schwab's documentation,
which is wrong or silent on all of it.

`get_movers` **ignores `sort_order` and `frequency`**. Both reach the wire and
the response is identical whichever values are passed: always the top ten by
share volume, always whole-session. Two different sort values returned identical
lists in all 495 index-cycles of a full session. `PERCENT_CHANGE_DOWN` does not
invert the ranking.

`totalVolume` **is the index total, not the instrument's** — identical on every
row, in all 990 responses of that session. `marketShare` is `volume /
totalVolume * 100` exactly, so it carries nothing the other two do not, and its
value depends on which index was queried.

**Membership barely moves.** Median zero changes between consecutive five-minute
polls, maximum one, twelve or thirteen distinct symbols across four hours. That
follows from ranking on cumulative session volume, which only grows.

**`INDEX_ALL` does not return indices.** It returns equities, it is not
`EQUITY_ALL`, and none of its members appeared in the `NYSE` list at any point
while eight or nine of ten appeared in `NASDAQ`. What it selects is undocumented
and is not established here, and the docstring says so.

### The streaming screener

**The subscription key is not a stock symbol.** It is
`(PREFIX)_(SORTFIELD)_(FREQUENCY)`. `docs/streaming.rst` always had this right;
the docstrings, which are what a reader sees at the call site, said "Equity
symbols to subscribe to".

**Schwab does not reject a malformed key.** `NOT_A_PREFIX_VOLUME_5`,
`NASDAQ_NOT_A_SORT_5`, `NASDAQ_VOLUME_7` and a bare `AAPL` each returned
`code: 0, "SUBS command succeeded"` and delivered nothing. There is no error to
catch, so a typo is indistinguishable from a quiet market. `AVERAGE_PERCENT_VOLUME`
behaves the same way despite being documented by Schwab.

**A `subs` replaces, an `add` appends, and the two screener services are
independent.** This page previously said the results "vary", were undocumented,
"seem to" clear the old subscription, and that the interpretation "may be
incorrect". The rules now come from grouping a capture's frames by subscription
key, and they account for every frame. The consequence worth knowing: a `subs`
carrying a key Schwab silently ignores still replaces a working subscription.

**A screener push is the whole list, not a change to it.** Both screener
services are *Whole* in Schwab's classification, so a symbol missing from a push
means "not in this snapshot" rather than "removed".

**One streaming session per account, and a second does not fail cleanly.** It
takes the connection from the first, and if both reconnect they bump each other
indefinitely. *(corrected in 5.0.1: the limit is per Schwab user, not per
account. Schwab's response code 12 says so, and streams for two different
brokerage accounts were measured bumping each other off, which a limit per
account cannot produce --- so a second account is not a way around it.)*
Nothing in the protocol names the intruder. A bumped stream still delivers some
messages, so it has to be treated as a stop rather than a warning.

Push cadence, the immediate frame on subscribe, and the equivalence between
stream frequency 0 and the REST endpoint are recorded with their sample size —
one nine-minute window — because that is what they rest on.

`$SPX.X` is corrected to `$SPX`. Over REST it is an HTTP 400; on the stream it
is accepted and silent forever.

### Everywhere

Dates and version history are out of the shipped documentation. A finding
measured against a live account still says so, because Schwab's documentation
has been wrong about several of these, but the date it was measured only asks a
reader a question they cannot answer. What changed belongs in this file.

Five hyperlink targets were split across a line. Sphinx joins them and the pages
render correctly; everything reading the source, `LinkTest` included, was seeing
`https://www.sec.gov/` and `https://www.investopedia.com/articles/active-trading/032614/`.
Now guarded by a test.

All ten documentation pages were read line by line. Roughly 250 lines of
trailing whitespace, a section that hyperlinked to itself, prose contradicting
its own JSON, an exception list naming four of five, and a composite-order
section that showed only the mistake and never the correct call.

---

## 3.0.2

One fix, to the collision warning 3.0.1 added, and the defects found while
making it.

### The warning fired on this project's own working tree

3.0.1 warned whenever it found a `schwab_py-*` distribution registered. That is
not enough to conclude anything: `schwab_py` is the name **this** project
published before 2.6.0, so an editable install made from a checkout of that era
registers it, and the warning reported a collision between the working tree and
itself. It fired on every `pytest` run in this repository.

The check now requires both names — `schwaby` **and** `schwab-py` — before it
says anything, which is what "both are installed" was always supposed to mean.

### What else changed in the same code

Everything below is a defect the check had independently of the false positive,
found by reviewing the fix for it.

- **One known false positive, left in deliberately.** A virtualenv that
  carried a pre-2.6.0 *editable* install and then received `pip install -e .`
  holds both names for a single source tree, and this warns about it. Telling
  that apart needs `direct_url.json` from both registrations; that was
  written, reviewed and then removed. It was a third of the check's code and
  produced both of the only two serious defects five review rounds found —
  one arrangement of registrations went entirely unwarned because of it.

  What it bought was suppressing a wrong warning for someone who can clear it
  with `pip uninstall schwab-py` (safe here: an editable install's `RECORD`
  claims no files under `schwab/`). That is not worth the code, and it is not
  worth the risk that the code silences a real collision.

- **Both `.dist-info` and `.egg-info` count, with a discriminator.** On a
  Debian or Ubuntu system interpreter the distro-packaged modules register as
  `.egg-info` and almost nothing else does — 73 against 35 `.dist-info` on the
  machine this was measured on — so skipping the layout would hide a
  legacy-installed `schwab-py`, which is exactly the old install this check
  exists to find. What separates a real install from the build artefact a
  source tree accumulates is the directory: an install directory never contains
  `setup.py` or `pyproject.toml`, and a checkout always does.

- **The distribution name is split at the version, not at a hyphen.** A name
  can contain one (`schwab-py-2.5.1`) and can end in one (`schwab-py`,
  unversioned), so neither the first nor the last hyphen is the boundary. A
  version component always begins with a digit and a name never does. The
  legacy `setup.py install` spelling, `<name>-<version>-py<X.Y>.egg-info`, is
  handled too.

- **Warnings-as-errors is told, and the import still survives.** Under
  `PYTHONWARNINGS=error`, `-W error`, or a consumer's pytest
  `filterwarnings = error`, `warnings.warn` raises. Failing `import schwab`
  over a condition where the files on disk still work is the opposite of what
  this check is for; swallowing the raise would leave the configuration that
  asked to be told loudest the only one told nothing. The raise is caught and
  the same text printed to stderr — as is a `warnings.showwarning` replacement
  that raises something else, and a stderr closed by the time the fallback
  runs. `sys.stderr` being `None` — pythonw, and hosts that detach it — is
  handled too, because `print(file=None)` writes to *stdout*, and a
  multi-line diagnostic in a program's data stream is its own bug. No
  diagnostic fails this import, and none of them lands anywhere it should
  not.

- **A checkout's own metadata is not an install, whichever spelling it is
  in.** `setup.py dist_info` writes a `.dist-info` into the source root, so
  restricting the source-tree discriminator to `.egg-info` left the false
  positive reachable by a different artefact.

- **The install order is documented, because it decides whether you are
  warned at all.** Both projects ship a `schwab/__init__.py`, so whichever is
  installed *second* overwrites the other's — install `schwab-py` over
  `schwaby` and the file carrying this check is the one that goes. The README
  and the getting-started guide said "`import schwab` warns" without that
  condition, which would let a user read silence as confirmation the install
  was fine, and then run the `pip uninstall schwab-py` that destroys it.

If you are on 3.0.1 and have never installed `schwab-py`, you will not have
seen any of this. Upgrading is worthwhile only if you did see a warning you
believe was wrong.

---

## 3.0.1

Two fixes and one addition, all found in the hours after 3.0.0 shipped. Nothing
in 3.0.0's published artifact is wrong; upgrading is worthwhile but not urgent.

### Fixed

**`setup.py` read `README.md` and `version.py` without an encoding**, so Python
decoded them with the locale's preferred one. On Linux that is UTF-8 and the
result is correct; on Windows it is a codepage, and every em dash in the README
became a replacement character in the `long_description` the build produced. A
wheel built on Linux is fine and one built on Windows is quietly corrupt.

The published 3.0.0 is unaffected — it was built on `ubuntu-latest`, and its
description on PyPI has its em dashes and no replacement characters.

There was already a test for this. It compares a UTF-8 read against what
`setup.py` produced, so it cannot fail where the locale is UTF-8 — which is
everywhere the suite normally runs. It passed on every Linux run and failed on
every Windows one. The new test reads `setup.py`'s source and requires an
encoding on every `open()`, which is locale-independent.

**Four tests failed on Windows** because `os.path.relpath` raises when the two
paths are on different drives, and the control fixtures live in a temp
directory — `D:` on a runner where the checkout is `C:`. This only ever showed
on tag pushes, since pushes to `main` run Linux alone and the full matrix runs
on tags.

### Added

**`import schwab` now warns when `schwab-py` is installed beside it.** This is
the only place it can be said. `pip` does not implement `Conflicts-Dist` — its
resolver never reads the field — and a wheel runs no code when it is installed,
by design, so nothing can refuse or flag the install itself. Import is the first
moment.

A warning rather than an exception: the library places orders, the files usually
work, and the damage comes from the *next* `pip uninstall`. Failing the import
would break a running system to complain about a state that has not broken it
yet. The message says not to run `pip uninstall schwab-py`, and what to run
instead.

It is a directory listing rather than an `importlib.metadata` lookup, which
opens and parses a metadata file for every installed package. Both answer the
question identically. Measured with `time.perf_counter` around a single call in
a fresh interpreter, five runs, 92 distributions installed, on one aarch64
Linux machine: the listing 0.21–0.23 ms, `importlib.metadata` 104–105 ms,
`import schwab` itself 143–166 ms. Those absolutes move a lot with the machine,
the filesystem cache and the number of packages — a reviewer on other hardware
measured 23–27 ms and 70–77 ms for the same two — so the ratio is the durable
part, and it says the metadata walk costs a large fraction of the import while
telling us nothing extra.

### Documentation

**The upgrade order is now in the README, the getting-started guide and the
3.0.0 upgrade table**, because installing `schwaby` over `schwab-py` leaves both
registered claiming the same files, and `pip uninstall schwab-py` afterwards
deletes the shared files and destroys the install — `pip` goes on reporting
`schwaby` as present while `import schwab` raises `ModuleNotFoundError`.

## 3.0.0

**This release removes public API.** `schwab.contrib.orders` and the
`schwab-order-codegen.py` script are gone. If you import either, this will not
be a drop-in upgrade; everything else is unchanged and needs no edits.

The major number is the point. 2.1.0 was also breaking and shipped as a minor,
on the reasoning that this project was installed from pinned git tags and so
nothing could upgrade into it by accident. That stopped being true at 2.6.0,
the first PyPI release: a requirement of `schwaby` or `schwaby>=2.6` now
resolves to this on a routine `pip install -U`, and a changelog banner does not
help someone who never reads one.

**A plain `pip install schwaby` is now the whole library.** The extras are gone
and the order-code generator is gone with them.

**Upgrading from 2.x.** For most programs this is a version bump and nothing
else. These are the things that need an edit, and only if you use them:

| If you | Then |
|---|---|
| import `schwab.contrib.orders` | It is gone. Nothing replaces it; the builder it reconstructed is still there. |
| run `schwab-order-codegen.py` or `schwab-generate-token.py` | Both are gone. `docs/auth.rst` has the four lines that replace the token script. |
| pin `schwaby[login]` or `schwaby[codegen]` | Drop the bracket. The pin still installs, with a warning. |
| call `set_destination_link_name` to pick a venue | Use `set_requested_destination`. The old one was writing to a field that does not select one. |
| pass `account_id=` to `StreamClient` | Remove it. It was accepted and never used. |
| let `AccountHashMismatchException` propagate | It now carries `.order_id`, so a live order on the wrong account is recoverable for the first time — but **anything between the call and your return is skipped on that path too**, journalling included. Catch it, record from `exc.order_id`, then re-raise. |
| construct `AccountHashMismatchException` yourself | It now requires `(response, order_id, account_hash, message)` rather than a bare message. Catching it is unchanged. |
| have `schwab-py` installed | **Uninstall it first**: `pip uninstall -y schwab-py && pip install schwaby`. Installing over it leaves both registered claiming the same files, and the obvious cleanup afterwards destroys the install. |
| pin by git URL rather than from PyPI | A PyPI install writes no `direct_url.json`, so anything verifying the pin by reading `requested_revision` out of it stops answering. Check how you assert your pin before you switch. |
| check `extract_order_id` for `None` | It raises now. Catch `OrderIdNotFoundError` — and read the entry below, because that case means an order may be live. **Anything you do between that call and your return is now skipped on that path**, so check what sits there: journalling and bookkeeping most often. |

Nothing else in the public surface moved.


### Added

**`set_requested_destination`, for routing an order to a specific venue.** The
`Destination` enum has been exported since the beginning and there was no
setter that put it on the right field --- see Fixed, below.

**`SchwabError`, a base class for every exception this library defines.**
`except SchwabError` is now one name for all of them. Before this the nearest
thing was `except ValueError`, which also catches every `int()` and `float()` in
the same block.

It is not everything the library can raise, and the documentation says so:
argument validation still raises builtin `ValueError` in about thirty places, and
a builtin describes "you passed a negative quantity" correctly.

All fifteen exception classes inherit it, and a test walks the package with
`pkgutil` and fails if one does not — a base covering most of them would be worse than none, since
it invites `except SchwabError` as a complete guard while quietly not being one.
`UnsuccessfulOrderException` and `AccountHashMismatchException` keep `ValueError`
as well, because code catching them that way predates this release and they do
describe a caller mistake.

**`OrderIdNotFoundError` deliberately does not inherit `ValueError`.** It was
going to, for consistency, but that hands back the
exact failure the raise was introduced to remove: `except ValueError` is the
idiom people reach for around `int()` and `float()`, order specs coerce exactly
those a few lines from the call, and the one exception here that means *an order
may be live and untracked* would be swallowed by a block aimed at parsing. It is
also the wrong word — `ValueError` says the caller passed a bad argument, and
they did not. This is remote state.

**`set_tax_lot_method`.** `TaxLotMethod` was likewise exported from
`schwab.orders.common` with no way to use it, so `taxLotMethod` could not be
sent at all. It matters on a closing order: FIFO and LIFO realise different
gains against the same position.


### Fixed

**The venue enum was being written to a field that does not select a venue.**
`set_destination_link_name` validated its argument against `Destination`, whose
twelve values are the ones Schwab lists for `requestedDestination`.
`destinationLinkName` is a free string in Schwab's schema, so an order asking
for NYSE was well-formed and nothing rejected it --- the only symptom would
have been routing you did not choose. Use
`set_requested_destination`. `set_destination_link_name` still exists and now
takes the string its field is typed as, which also means it no longer refuses
legal values.

*Not verified against a live account.* This is read from Schwab's published
order schema, which enumerates those values under `requestedDestination` and
types `destinationLinkName` as `string`. If you have been relying on the old
behaviour, check your fills before assuming either version routed as you
intended.

**`extract_order_id` returned `None` for two different situations, one of them
dangerous.** No `Location` header, and a `Location` that did not parse. Both
mean Schwab *accepted* the order and did not give back an ID — so the order is
very likely live and you have no handle on it — and both shared a return value
with every harmless thing that returns `None`. The natural handling,
`if order_id:`, therefore skipped tracking a live order.

Each now raises its own exception: `MissingLocationHeaderError` and
`UnrecognizedLocationError`, both under `OrderIdNotFoundError`, so catch the
base unless you need to tell them apart. The response is on the exception, and
`UnrecognizedLocationError.location` carries the header verbatim — please
include it in a bug report, because it means Schwab changed the URL format.

The messages say what matters rather than what happened: *the order may be
live: check get_orders_for_account*.

**Five exceptions were broken across a process boundary.** Any that carry the
thing they are about as a leading positional argument — a response, a raw frame
— passed only the message up to `BaseException`, so the default reconstruction
called `__init__` with the message alone. Measured on 2.6.0:
`UnparsableMessage` and `ResponseTimeoutError` raised
`TypeError: ... missing required positional arguments` under `copy`, `deepcopy`
and `pickle`; `UnexpectedResponse`, `UnexpectedResponseCode` and
`UnusableMessage` reconstructed with the message silently gone. The other six
were fine.

The new exceptions in this release have the same shape, so the fix covers all
fifteen rather than the five.

This library does not itself move an exception across a boundary — the login
flow's child process sends back a URL string, and `RedirectServerExitedError` is
constructed in the parent. It matters for callers who do: a `ProcessPoolExecutor`
over placements, say, where the exception saying *an order is live on the wrong
account* is the last one that should arrive as a `TypeError` about argument
counts. Fixed once on `SchwabError` rather than fourteen times, and a test
round-trips every exception class the package defines.

**`AccountHashMismatchException` parsed the order ID and threw it away.** It is raised only after the response came back successful *and* a valid order ID
was parsed out of it, on the line above — so Schwab placed something, on an
account the caller was not expecting to trade, and the ID was discarded on the
way out. The message was `order request account hash != Utils.account_hash`,
which reads as a configuration complaint.

So on 2.x that order was not merely untracked, it was **unrecoverable** short of
going back to `get_orders_for_account` and guessing which one it was. This is the
first release in which it can be owned.

In a fleet the plausible trigger is a `Utils` cross-wired between accounts, which
is exactly the fault you would most want to find with the order ID in hand.

It now carries `.order_id`, `.account_hash`, `.response` and
`.expected_account_hash` — the last keyword-only, so it cannot take the slot a
message is passed in — and says *the order is live on the account Schwab
named*.

**If you construct this exception yourself, the signature changed.** On 2.x it
had no `__init__` and took a bare message, so `AccountHashMismatchException('…')`
worked. It now requires `(response, order_id, account_hash, message)`. Catching
it is unchanged; only construction breaks, which in practice means test doubles
and wrappers. It keeps `ValueError`, because
that predates this release and it does describe a caller mistake, but the
docstring warns that a broad `except ValueError` will swallow it.

**A rejected order threw away Schwab's explanation of why.**
`UnsuccessfulOrderException` carried the HTTP status code and nothing else, so
`order not successful: status 400` was the whole of what you got --- a status
code does not distinguish a malformed order from one the account cannot afford.
Schwab types an error body as `{"message": ..., "errors": [...]}`, and that text
now appears in the exception message, bounded so it cannot put an entire order
echo into a log line. The rejected response is on the exception as `.response`
for the rest of it.

**The wheel was shipping a top-level `tests` package into your site-packages.**
`find_packages()` matched it alongside `schwab`, so `pip install schwaby`
installed 21 test files as an importable top-level `tests` module --- which
collides file-for-file with any other distribution that ships one, and answers
`import tests` from any directory that is not your project root. Present in
every release up to and including 2.6.0. Uninstalling and reinstalling clears
it; `pip uninstall schwaby` on its own may take files another package put
there, so check `site-packages/tests` afterwards if you had one.

**`cryptography` is a declared dependency now.** The callback server runs with
`ssl_context='adhoc'`, and `werkzeug` builds that certificate with
`from cryptography import x509` --- which neither Flask nor Werkzeug declares.
It was present only because `authlib` happens to require it. Had that changed,
the child process would have died inside `app.run` and the parent would have
reported `RedirectServerExitedError`, blaming your callback port for a missing
package. The parent checks for it explicitly, alongside `flask`.


### Changed

**The install is bigger, not smaller.** 2.3.0 split the login packages out and
took a fresh install from 27 packages to 15. Putting them back reverses that:
measured, 2.5.1 pulls in 14 packages and 3.0.0 pulls in 24, `cryptography`
among them. If you remember the slimming, it did not survive — the reasoning
below is why, and a trading box now has those back to patch and audit.

**`flask`, `multiprocess` and `psutil` are hard dependencies again.** They were
split out into a `login` extra in 2.3.0. The split saved twelve packages for
nobody — every consumer that authenticates at all installed the extra — and cost
three ways to be wrong, all of which fail late rather than at install time:

- `pip freeze` drops extras. A `schwaby[login]` line freezes to
  `schwaby==X.Y.Z`, and installing from that leaves the login packages out with
  no warning from either command. This is not specific to this package —
  measured on `requests[socks]`, which freezes the same way.
- `easy_client` needs the extra even when a token file already exists, because
  its `max_token_age` defaults to 6.5 days and it re-authenticates through the
  login flow past that. A program installed without the extra ran for 6.5 days
  and then failed on a routine refresh.
- An extra is a second thing to get right in a pin, and nothing checks it.

**The `login` and `codegen` extras are gone entirely.** A `schwaby[login]` pin
still installs correctly --- `pip` treats an unknown extra as a warning, not an
error --- it just prints `does not provide the extra 'login'` while doing it.
Keeping empty extras to suppress that would have defended about four hours of
PyPI history, since `schwaby[login]` was only ever installable from 2.6.0. Drop
the bracket from your requirements line and there is nothing to notice.

**Copyright is now asserted jointly.** `LICENSE` carries
`Copyright (c) 2023 Alex Golec` unchanged, as the MIT licence requires, with
`Copyright (c) 2026 Tom Hirt` added beneath it for the work since. The package
metadata reads `Author: Tom Hirt and Alex Golec`, and the documentation footer
says `2023 Alex Golec, 2026 Tom Hirt` rather than describing this as a fork
someone maintains.

Measured, for the record: of the 8,412 lines under `schwab/`, 4,832 are Alex
Golec's, 3,113 are Tom Hirt's, and 463 come from other contributors to the
original project. The licence is unchanged and remains MIT.

**The PyPI classifier said `Development Status :: 1 - Planning`**, inherited and
never updated, and it is what the project page would have shown. It now says
`5 - Production/Stable`.


### Removed

**The order-code generator.** `schwab.contrib.orders`, the
`schwab-order-codegen.py` script, the `codegen` extra's `autopep8` dependency,
and the `construct_repeat_order` / `code_for_builder` / `UnrepeatableOrderError`
API. It turned an order from your history back into the `OrderBuilder` code that
would place it again — a one-off aid for writing a program, not something a
running one calls, and it reconstructed a builder by pattern-matching against a
response format that has moved on. Nothing else in the library used it.

`schwab.contrib.util` is unaffected — `StreamJsonDecoder` and
`HeuristicJsonDecoder`, which the streaming documentation recommends for frames
that will not parse, are still there and still imported the same way.

**`StreamClient` no longer takes `account_id`.** It was accepted and never
used --- the parameter appeared exactly once in `streaming.py`, in the
signature. It is from the TD Ameritrade streamer, which needed one. The main
streaming example in the documentation passed it, so readers were being taught
to supply a value that went nowhere.

**`schwab/_optional.py`.** With nothing optional left to import, the
lazy-import-with-a-helpful-ImportError machinery has nothing to guard.
`auth.py` imports `flask`, `multiprocess` and `psutil` inside the functions
that use them, which keeps the parent-process import check that distinguishes a
broken install from a callback server that exited.

**The `schwab-generate-token.py` script is gone.** It fetched a token and wrote
it to a file without your having to write any Python, and it was the only thing
this package installed onto your `PATH`. Nothing in the library used it, and it
was packaged the legacy way --- `scripts=` copies the file verbatim, so the
installed command carried a `.py` extension and a project prefix from before the
rename.

The case it existed for is creating a token on a machine with a browser and
copying the file to one without. That recipe is still in `docs/auth.rst`, now
with the four lines of `client_from_login_flow` that replace the script. A
`pip install schwaby` no longer puts anything in `bin/`.

**`tox.ini` is gone, and CI runs the suite directly.** tox was not doing what
it looked like it was doing. It built an sdist, installed it into an isolated
environment, and then ran `coverage` as an external command --- which executed
under a different interpreter and imported `schwab` from the working tree, so
the environment tox had just built was never the one under test. Measured, not
inferred. The Python version came from `setup-python` either way, which is the
part that mattered, so the matrix still runs 3.10 through 3.14 on the same
platforms. `passenv` and `setenv` went with it: the `CI` variable the macOS skip
reads was only awkward because tox filtered the environment.

**`MAINTAINING.md` is gone, replaced by `RELEASING.md`.** A quarter of it
described a fork: the branch model, the `upstream-main` mirror, the pull-request
discipline, and why that arrangement ended. None of it applies to a standalone
project, and `upstream-main` no longer exists. What survives is the release
checklist and the rules this project has learned by getting them wrong, under a
name that says what the file is for.

**Every referral to the original project's tracker is gone**, from the README,
`docs/help.rst`, `CONTRIBUTING.rst`, `SECURITY.md`, the issue and pull-request
templates, and the issue-template contact links. They asked reporters to file
the same thing on somebody else's repository, which is a promise this project
cannot keep and a piece of coupling it has to maintain. The credit to Alex Golec
stays exactly as it is, in the README and the licence, because that is
authorship rather than process.

The warnings that `schwaby` and `schwab-py` cannot be installed together stay
too. Those are about a real collision that silently overwrites files, not about
the relationship between the projects.

### Documentation

**The documentation is published at
[schwaby.readthedocs.io](https://schwaby.readthedocs.io/), and every link now
points there.** Previously they pointed at the `.rst` sources on GitHub, which
renders them as plain text — including the link PyPI shows in its sidebar, and
the three URLs `client_from_login_flow` prints during an interactive login.

User-facing links use `/en/stable/`, which tracks the latest tag, so someone on
3.0.0 reads 3.0.0's documentation rather than whatever is on `main`.

**A caution about real money, where a reader will actually meet it.** The README
carried an inherited one-paragraph disclaimer at the very bottom, under the
licence, which is where nobody reads. There is now a `[!CAUTION]` block near the
top of the README, a `.. danger::` on the documentation front page, and a warning
at the head of the getting-started guide.

It makes the claim rather than hedging it: this places real orders with real
money and it *has* bugs, not "may have". Every defect ever found here was in code
with 100% test coverage and the ones that mattered were silent; the changelog
lists them precisely because the next one is in there unfound. You are
responsible for every order your code places, and the author, maintainer and
contributors are not. Start small, reconcile against the broker rather than
trusting what the library says happened, read the code on any path that places an
order, and assume the bug you have not found is on the path you did not read.

The bottom-of-file disclaimers were also rewritten. They were inherited, one
carried a "This authors" typo, and neither mentioned financial loss.

**A documented example called a method that does not exist.** The level two
page told readers to use `Client.search_instruments()` and gave a worked
example; the method is `get_instruments`, so anyone copying it got an
`AttributeError`. `sphinx -W` does not resolve cross-reference targets, so the
build had always passed. Every name the documentation points at is now resolved
against the code by a test.

**A second documentation example could not run.** `client.rst` showed placing
an order with `easy_client(..., webdriver_func=make_webdriver)` and a
`selenium` import. There is no `webdriver_func` parameter, and selenium is not
a dependency and appears nowhere in the library --- it is left over from the TD
Ameritrade era, when the login flow drove a real browser. A test now checks
every keyword argument in every documentation example against the signature of
the thing being called.

**`client_from_login_flow` pointed a `ValueError` at `schwab-py.readthedocs.io`
--- the original project's documentation site.** A user who got the callback URL
hostname wrong was sent to read a different project's docs. The callback-URL
advisory now has its own section in `docs/auth.rst`, which is where the error
links, and a test fails on any documentation host this project does not
control.

`easy_client`'s 6.5-day proactive re-authentication is now documented in
`auth.rst`, where it belongs. It was previously described only as a reason to
install an extra, so it disappeared with the extras despite being a property of
`easy_client` rather than of the packaging.

**Three dead links, one of them the first click in the onboarding flow.**
`docs/getting-started.rst` sent new users to `beta-developer.schwab.com` to
create their developer account and register their app --- a hostname that no
longer resolves, so step one of the guide failed with nothing else on the page
to try. It points at `developer.schwab.com` now. A docstring in
`orders/generic.py` linked to `developer.schwabmeritrade.com`, from the TD
Ameritrade era, and `auth.py` sent a rejected callback URL to the *original*
project's documentation site.

**`get_instruments`' docstring documented a `symbol` parameter.** The argument
is `symbols`, so the rendered signature and the rendered parameter list
disagreed.

**`place_order`'s first argument is an account *hash*, and the order-template
docs called it `account_id`.** One example passed the literal `1000`. Those are
different things: the hash comes from `get_account_numbers()`, and an account
number will not work.

**The rendered documentation was titled `schwab-py`.** `docs/conf.py` kept the
original project's name through the rename, so every page title and browser tab
named a different library while the text on the page said to install `schwaby`.
Alex Golec's `author` and `copyright` entries are unchanged and deliberately
so.

**The docs no longer call this project by three different names.**
`schwab-api`, from two renames ago, appeared four times across the client and
streaming pages. Prose written in the voice of the original project's community
("we in the community aren't currently clear...") has been rewritten to say
what is actually known, and a "July 21, 2024" update notice has become a
statement that does not need a date attached to stay true.

Documentation examples use `example.com`, which RFC 2606 reserves for the
purpose, rather than `callback.com` --- a real registrable domain that someone
else owns.

**The "Critical Schwab Bug" section is gone from the getting-started guide.** It
described a July 2024 outage on a developer console that no longer exists, and
it was the first thing a new reader saw.

**The README is Markdown now, not reStructuredText.** GitHub renders `.rst`
without admonition styling or much typography, so the repository landing page
was the plainest version of the text; PyPI takes `text/markdown` natively. The
documentation under `docs/` is unchanged and still reStructuredText, so the
Sphinx and Read the Docs build is untouched --- verified by building it in a
clean environment from `docs/requirements.txt` alone.

One thing this costs, recorded because it is not obvious: `twine check
--strict` was a real gate on the `.rst` README --- reStructuredText rejects
malformed markup, which is what caught a title underline one character short
that would have published a release with a blank description. Markdown rejects
almost nothing. Measured against `readme_renderer`, it refuses an empty
document and a whitespace-only one and accepts unclosed HTML tags and broken
link syntax. And without `readme_renderer[md]` installed, `twine check
--strict` reports PASSED having rendered nothing at all. So the release
workflow installs that extra, and the suite asserts the property directly: the
description renders, it is substantial, it still has its headings and its
table, and it names what a reader needs.

**The README leads with what the library does.** It opens with the claim, then
four short examples --- authenticate, fetch, place an order, stream --- that
show the whole surface before asking anyone to read prose. The origin story and
the credit to Alex Golec are the last section rather than the first thing a new
reader meets. Same for `docs/index.rst`.

**The web-application login flow is documented.** `get_auth_context`,
`client_from_received_url` and `AuthContext` have been public for a long time
with no docstrings and no mention in the documentation. They are how you log in
when the callback arrives as an ordinary request to a server you already run,
in a different process from the one that started the login --- the case
`client_from_login_flow`, which runs a local callback server and blocks, cannot
serve. `docs/auth.rst` has a worked example.

**The book message structure is documented.** Every other stream had a field
reference; the three order-book streams had none, so a subscriber received a
nested structure with nothing describing it. `BookFields`, `BidFields`,
`AskFields`, `PerExchangeBidFields` and `PerExchangeAskFields` are on the
streaming page now, with a worked example of the shape.

**Two more examples, and a warning that had no code.** `examples/` now holds
three files, chosen because each teaches an assembly the reference
documentation cannot: the streaming consumer with its bounded queue,
`auth/token_lifecycle.py`, and `orders/order_lifecycle.py`.

The token one exists because Schwab's refresh token expires seven days after
the original authorization and refreshing does not extend it, so every
unattended program stops within seven days and the only question is whether it
stops at a moment you chose. The documentation explains that across four
separate passages and never shows the loop.

The order one exists because `extract_order_id` returns `None` for two
different reasons --- a rejection, and a response carrying no `Location` header
--- and the snippet in `util.rst` ends in `assert order_id is not None`, which
papers over exactly that.

Separately, `client.rst` now warns that `httpx2` exceptions are not `httpx`
exceptions. Catching `httpx.HTTPStatusError` around a call into this library
fails silently: nothing raises at import, nothing raises at the `try`, and the
`except` simply never matches. A rate-limit retry written against `httpx`
stops working this way, silently, on the 2.0.0 upgrade, and until now that
appeared nowhere as code.

**The one file under `examples/` was four years stale and now has a test.** It
subscribed to `TWTR`, `FB` and `FIT` --- delisted in 2022, renamed in 2022, and
acquired in 2021 --- carried two TODOs about API that either works now or never
existed, and passed the `account_id` above. It has been rewritten around what
the example is actually for: a bounded queue between the socket and your
processing, and an error handler, which is the shape a long-running consumer
wants and the one thing a README snippet cannot show.

Nothing referenced `examples/` and nothing checked it, which is why it drifted.
The keyword-argument check that covers documentation code blocks now covers
these files too, and fails if the directory disappears from its walk.

## 2.6.0

**First release published to PyPI, as `schwaby`.**

```shell
pip install schwaby
```

```python
import schwab   # unchanged
```

### Changed

**The distribution is now `schwaby`; the importable package is still `schwab`.**
Those differ deliberately. Keeping the import means an existing consumer changes
one line of `requirements.txt` and nothing else — the Pillow and PyCryptodome
model for a project that began as a fork of a dormant one.

**The cost is that `schwaby` and `schwab-py` cannot be installed together.** Both
provide the `schwab` package, so whichever lands second silently overwrites the
other's files. `pip` gives no warning and nothing fails at install time; the
first sign is behaviour from a version you did not choose. Uninstall `schwab-py`
before installing this.

**The repository is `Hu1kSmash/schwaby` and is no longer a fork.** Old URLs
redirect, so existing clones and git-pinned installs keep working.

**The extras command no longer names a version.** A missing `login` or `codegen`
extra now says `pip install "schwaby[login]"`, which stays correct for every
release, rather than interpolating a git URL and a tag.

### Documentation

**The project is described as itself rather than as a fork of something else.**
The README opens with what `schwaby` is and puts the history in a note below it:
it began from `alexgolec/schwab-py`, which gave it its shape and most of its
code, and became separate because code that places real orders imposes
requirements a general-purpose wrapper has no reason to prioritise. Alex Golec's copyright and licence are unchanged.

**Two client endpoints were documented for the first time**, both inherited in
2024 and never covered: `preview_order`, which asks Schwab whether it would
accept an order without placing it, and `get_option_expiration_chain`, which is
cheaper than a full chain when you only need expiry dates. All 31 public client
methods are now in the documentation.

`get_option_expiration_chain`'s docstring was also wrong — it described
`get_user_preferences`.

**`set_json_decoder` is documented**, which mattered after 2.5.0 told readers to
try `HeuristicJsonDecoder` when frames will not parse without saying how. Four
exceptions a caller might catch, and `OptionSymbol.parse_symbol`, also gained
entries.

## 2.5.1

### Fixed

**`UnparsableMessage` reached `add_error_handler` from only one of the two
readers, so 2.5.0's headline claim was false on the other.** Two coroutines can
be holding the read lock when a frame arrives — `handle_message`, and a request
waiting on its response — and the report was added only to the first. Whenever a
subscribe won the read lock, an unparsable frame raised out of that subscribe
with the callback never firing. Which coroutine reads any given frame is a lock
race the caller cannot see, so the callback fired or did not fire for reasons
nothing in the API explains.

Both readers now report, through one guarded helper rather than two copies —
and exactly once. `handle_message` hands the same exception to the waiting
request through `_fail_pending_request` before reporting it, so both readers see
one object for one bad frame; reporting from each would have replaced "fires or
does not fire" with "fires once or twice", still decided by a lock race the
caller cannot see, and with both reports carrying an identical triple a consumer
cannot distinguish from two genuinely different bad frames.

**The comment claiming the raw text makes the pair "never both empty" was
wrong.** `raw_msg` is the empty string for an empty text frame, so a report is
distinguishable from the logout-close failure under `is None` but not under a
falsy test. The limitation is documented and pinned by a test rather than
overclaimed; the documented discriminator remains the exception type. The note
now sits on `UnparsableMessage`, which is the class that has a `raw_msg`, rather
than on `UnusableMessage`, which does not.

## 2.5.0

### Added

**`UnparsableMessage` now reaches `add_error_handler`.** It was the one failure
class that ended the caller's receive loop *and* gave a consumer who replaced
log-scraping with the callback no programmatic signal at all — the state 2.2.0
was added to eliminate, one layer below where 2.4.0 eliminated it. It is
reported and then re-raised, so the control flow is unchanged.

Reported from outside the `async with` that holds the read lock, so no user code
runs under it; a handler raising `BaseException` there is logged rather than
allowed to replace the parse failure the caller needs to see.

**That it still ends the loop is now a decision rather than an omission**, and
the docstring gives the reasoning and the counter-argument. A structurally
unusable element can be skipped precisely; a frame that will not parse has
unknown contents, so continuing accepts a gap of unknown size — and a reconnect
re-subscribes, which is the only thing that recovers state.
`HeuristicJsonDecoder` exists because Schwab really does emit unparsable JSON,
which is evidence the other way, so the docs now point at it.

Found by tracing the failure classes rather than grepping for them.

### Documentation

**`OrderUROutCompleted` was described as "an unsolicited out", in the sentence
that corrected its spelling.** That asserts nobody asked for the cancellation,
and the same token ends a cancel you issued yourself — so the description
contradicted the sequences printed a few lines below it. It now says the order
came off the book, and says explicitly that the token does not carry the cause.

A phrase asserting a cause the token cannot carry is a small lie on any screen
that shows it each time an order is rejected, and the same wording was in these
docs.

## 2.4.1

### Documentation

**The `ACCT_ACTIVITY` `MESSAGE_TYPE` tokens were documented in the wrong case,
and one of them in the wrong spelling.** Measured on 2026-09-05 by driving the
states deliberately against a live account: the tokens are CamelCase on the
wire — `OrderCreated`, `OrderAccepted`, `CancelAccepted`, `ExecutionCreated`,
`OrderUROutCompleted` — with `SUBSCRIBED` a genuine exception.

Comparing against `'ORDERCREATED'` matches nothing. Worse, `ORDERUROUT` is
not a case variant of `OrderUROutCompleted`, so upper-casing does not rescue
it — and a token copied from this list would have alerted on an ordinary
cancel.

The two sequences a caller most needs to tell apart are now written down: a
cancel you issue yourself, and a buy rejected for buying power. They differ only
by `ExecutionCreated`, and the rejection returns **HTTP 201 with a real order
id** and only becomes `REJECTED` about a second later — so a caller checking
only the HTTP status believes it worked.

**Five more `ACCT_ACTIVITY` `MESSAGE_TYPE` tokens.** `ORDERMONITORCREATED`,
`ORDERMONITORCOMPLETED`, `CHANGECREATED`, `CHANGEACCEPTED` and
`EXECUTIONCREATED` — the lifecycle of a resting limit or stop order, observed on
a live feed on 2026-07-27. A program placing only market orders never sees them
and will meet them the first time a human places an order by hand in the same
account, which is a plain way to get an unknown-shape alert from an allow-list.
The note also flags that `CHANGECREATED`/`CHANGEACCEPTED`/`ORDERREPLACED` mean a
*working order was amended* — a safety event for an order you placed yourself.

### Fixed

**CI was red on macOS on every tag since 2.0.0, and the README badge said
otherwise.** Seven `ClientFromLoginFlowTest` cases really start the callback
server in a child process and talk to it over loopback; on the macOS runners
the child starts and never answers on its port, so each fails after the full
30-second wait, on every Python version. Windows passes, and Windows also spawns
rather than forks, so this is not a start-method problem.

They are skipped on macOS CI runners only — not on a developer's Mac — with a
comment saying what would settle whether this is a runner restriction or a real
macOS defect. The other 923 tests pass on macOS and that coverage is kept.

## 2.4.0

### Added

**`add_error_handler` now reports a fourth kind of absorbed failure: a message
this client cannot use at all.** A frame which is not an object, an element of
`data` or `notify` which is not an object, a `service` which is not a name.
These arrive as the new `UnusableMessage`, whose `message` is the offending
value as it arrived, with `cause` (the exception that made it unusable, where
there was one) and `count`/`total` as integers — reports are suppressed after
the first few, so the counts are the only way to see the true scale, and a
consumer alerting on drop volume should not have to parse them out of prose.

That callback exists so an absorbed failure is not visible only in a log, and
these are absorbed failures. A consumer who registered a handler to stop
scraping logs would otherwise watch a subscription go silent after a framing
change with no programmatic signal at all — the state 2.2.0 added the callback
to eliminate.

`UnusableMessage` rather than the existing `UnparsableMessage`, which means the
JSON did not decode and carries the parse exception. Here the JSON is fine and
the structure is not; one type meaning two shapes is a trap this library has
been bitten by before.

### Changed

**A systematically malformed channel no longer logs one line per element.** The
first three on a connection are logged, then powers of ten, with the running
count. A framing change on a `LEVELONE_EQUITIES` subscription across a few
hundred symbols would otherwise produce a warning per element per tick
indefinitely — a log-volume incident on top of the data outage.

Reports are coalesced onto the same schedule for a second reason: they share a
bounded queue with the late rejections, so reporting every occurrence would let
a flood of unusable elements evict a rejection of an abandoned request, which is
the one thing nothing else will ever report.

### Fixed

**A malformed message ended the caller's receive loop.** Four related cases,
all of which raised out of `handle_message` where the caller could only read
them as the stream having failed:

* an element of `data` or `notify` which is not an object — `d.get('service')`
  is evaluated at the dispatch call site, outside the `try` that protects
  handlers;
* a `service` which is not a name, since the handler lookup is evaluated in the
  same place;
* a frame which is not an object at all — a top-level JSON array or string;
* a top-level JSON `null`, which was indistinguishable from the internal
  sentinel meaning "this response was routed to its waiter", so it was dropped
  without even a log line.

Each is now logged and skipped, and well-formed elements beside a bad one are
still dispatched.

**A failure to relabel a message was reported as a handler failure.** Relabeling
is this library's work, not the caller's, but it happens inside the per-handler
`try` — so a `content` the field tables could not read was reported as "your
handler raised", once per registered handler, uncounted and uncoalesced. It is
absorbed once per message now, and it carries the `service` it belongs to and
the exception that caused it: `_BookHandler` indexes four levels deep, so the
`KeyError` is the only thing that says which field moved.

**A response frame with an unreadable request id was logged and nothing else.**
It was the one unusable-message path that bypassed the counting and the
callback — so it repeated per re-read inside a single subscribe, and a venue
emitting a non-numeric `requestid` gave a consumer no programmatic signal at
all.

**An `UnusableMessage` could not be told from a failure to close after logout.**
Both arrived as `service=None, message=None`, which the documentation designates
as the close-failure signature. The report now carries the containing frame as
`message`, which is non-`None` for every case but a top-level JSON `null`, and
the documentation says to test `isinstance(exception, UnusableMessage)` rather
than branching on both being unset.

**A custom JSON decoder returning anything but `dict` and `list` broke the
stream.** `set_json_decoder` is a public hook which promises only "the decoded
JSON", but the type checks above were written against the concrete types, so a
decoder returning a `Mapping` which is not a `dict` — or tuples for arrays — had
every frame dropped, presenting as a permanently dead feed with no exception.
The checks are against the operations used, not against `collections.abc` — an
ABC matches only real subclasses and registered types, which would have narrowed
what a decoder may return rather than tolerating it. A JSON array must be
**indexable**, which tuples are: routing reads `frame['response'][0]`,
validation reads it again, and handlers get the frame afterwards, so a
single-pass iterable cannot serve however tolerant the iteration is. One is now
refused and reported rather than accepted and found empty by whichever function
read it second.

Separately, and cosmetically: the debug log formatted frames with `json.dumps`,
which refuses such an object. Logging swallows a formatting failure, so the
stream survived either way, but the content of every debug line was replaced by
a traceback — for exactly the people who customised the decoder and are most
likely to be reading it.

**A mismatched request id was reported as a malformed frame.** All five fields
were read before any was compared, so a frame carrying an id this client never
issued *and* missing another field said `malformed response frame: KeyError:
'service'`. The id mismatch is the more diagnostic fact — it means the server
and this client disagree about what was asked — and it was hidden behind
whichever field happened to be absent. The id is compared before the rest is
read.

**`set_json_decoder` looked its base class up through `schwab.contrib.util`,**
which raises `AttributeError` unless the caller happened to have imported that
module. Someone subclassing `StreamJsonDecoder` where it is actually defined,
in `schwab.streaming`, has no reason to have done so. It is the same class
either way; the local name is used now.

**A malformed answer took down the receive loop as well as its own request.**
`_validate_response` read four fields straight after the request id, unguarded.
A `KeyError` there reached `_fail_pending_request`, which sets the exception on
the waiting future *and* re-raises — so one unreadable field failed the request
with a bare `KeyError` and ended the caller's `handle_message` loop with it. A
frame too malformed to check is now returned as an `UnexpectedResponse` saying
so, which fails that request and leaves the stream usable.

Found by sweeping for the shape of the 2.3.0 fix rather than waiting for a
report: 2.3.0 guarded the request-id read, and this sat four lines below it.

A rejection carrying a code but no `msg` is also no longer treated as an
unreadable frame. The code is the part a caller can act on, and losing it to a
missing message field was the worse outcome.

### Documentation

**`pip freeze` drops the extra from a pinned line, and says nothing.** A
requirements file carrying `schwab-py[login] @ git+...@v2.3.0` installs
correctly, but freezing that environment writes `schwab-py @ git+...@<sha>`
back out, and installing from *that* silently omits `flask`, `multiprocess` and
`psutil`. The failure surfaces later, when something first calls the login flow.
Documented in `docs/getting-started.rst`. This is a `pip` limitation with direct
URL requirements, not something this package can fix.

## 2.3.0

### Changed

**The login flow and the code generator are now optional extras.** A plain
install requires three packages -- `authlib`, `httpx2`, `websockets` -- and
nothing else. `flask`, `multiprocess` and `psutil` move to `schwab-py[login]`;
`autopep8` moves to `schwab-py[codegen]`.

**This is breaking if you use `client_from_login_flow` or `easy_client`.** Add
`[login]` to your install. Calling either without it raises an `ImportError`
that names the extra and gives the command, rather than a bare "No module named
'flask'".

Note the second one carefully: **`easy_client` needs `[login]` even when you
already have a token file.** Its `max_token_age` defaults to 6.5 days, and a
token older than that is discarded and replaced through the login flow. So a
plain install works for 6.5 days and then fails on a routine re-authentication,
which on an unattended machine is the worst shape this break can take. Pass
`max_token_age=0` to disable the proactive refresh if you really want to run
`easy_client` without the extra — but Schwab's refresh token expires seven days
from authorization regardless, so anything long-running needs a way to log in
again either way.

In a Jupyter or Colab notebook `easy_client` routes to `client_from_manual_flow`
instead, which starts no callback server, so notebook users need no extra.

Not affected: `client_from_token_file`, `client_from_access_functions`,
`client_from_received_url`, `client_from_manual_flow`, and the streaming client.
None of them touch the callback server.

Measured on a clean 3.14 install: 27 packages before, 15 after. The twelve that
go are `flask` and its tree (`blinker`, `click`, `itsdangerous`, `jinja2`,
`markupsafe`, `werkzeug`), `multiprocess` and `dill`, `psutil`, and `autopep8`
with `pycodestyle`.

None of them were ever used outside the interactive login flow and the code
generator, and two -- `multiprocess` and `psutil` -- were imported at module
scope, so every `import schwab.auth` paid for them. A process that loads a token
from a file and streams quotes has no use for a web framework, and on a machine
that places trades each package is something to patch, audit and break on
upgrade.

### Fixed

**A malformed response element ended the caller's receive loop.** A `"content"`
present but JSON `null` made `content.get('code')` raise `AttributeError` out of
`handle_message`. Both framings of the same event now share one parser, so a bad
element is logged and skipped and the good elements beside it still report —
they had diverged, with one framing hardened and the other not, which is exactly
what the framing-independence contract above says cannot happen.

**`login()` did not close the connection it replaced.** Calling it on a healthy
client — a re-authentication, a preferences refresh — dropped a live websocket
and its reader with nothing closing it. After a `ConnectionClosed` the old
socket is already gone and this is a no-op, which is why it went unnoticed. A
failure to close is logged rather than raised, since the login is the operation
the caller asked for.

**A rejection riding along in a matched response frame was reported by
nothing.** `_validate_response` reads `response[0]`, which is the answer to the
outstanding request; a frame carrying a second response handed it to the waiter
unexamined. If that one was a rejection, nothing mentioned it -- the same gap
the late-rejection reporting closed in 2.2.0, one element along. Additional
responses are now checked. A rejection logs a warning and is reported to
`add_error_handler`; a success alongside the answer logs at INFO and is not
reported, exactly as on the orphan path.

The rule is that every response no waiter claimed is checked, which is usually
everything past element 0. In the window where a waiter has already timed out
but the pending slot is not yet cleared, nobody claims element 0 either, and it
is checked too.

Reporting it matters more than it might look. `_request_lock` keeps one request
outstanding, so a second response in a frame cannot answer anything the client
is waiting on — it is a late answer to an abandoned request, the same class the
orphan path has reported since 2.2.0. Whether Schwab sends it in its own frame
or batches it behind the answer to a live request is the server's choice. Had
only the orphan framing reported, a consumer who replaced log-scraping with
`add_error_handler` would see a rejection one day and silently miss the
identical one the next, for a reason invisible to them.

**But not from where it is found.** That code runs holding the read lock, on the
request path the request lock too, and inside the response deadline — a user
handler called there let a slow one turn a subscription that *succeeded* into a
`ResponseTimeoutError`, and one that re-subscribed blocked on a lock its own
caller held. Both were real, and both have tests. The report is queued instead,
and delivered by whichever coroutine read the frame once it has released its
locks: before `handle_message` returns, or before the request that read it
returns. Draining only in `handle_message` was not enough — when a subscribe
wins the read lock, `handle_message` can be parked in `recv()` with its own
drain already behind it, and the report would then wait for the next inbound
message, which on a quiet stream is unbounded.

The consequence to know: **your error handler can be called from inside a
subscribe.** A slow one delays that call returning; it cannot make it fail. A
request delivers what it read even when it fails, since the exception the caller
gets describes only the response that answered their own request — though a
handler's `BaseException` is logged rather than allowed to replace it, and a
*cancelled* request skips the report so a shutdown never waits on user code. The queue is
bounded, and is cleared by `close()` and by a fresh `login()`, so a torn-down
session's rejection is never reported against a new one whichever way the caller
reconnects. Frames read but not yet handled are cleared with it — that deque
predates this change and had the same leak, which would have left the standalone
framing crossing sessions while the batched one did not, and could hand a
handler a quote from a dead connection. The log line written when each
rejection is found is the complete record; the callback is the convenience. All
of this is stated in `add_error_handler`'s docstring and in `docs/streaming.rst`.

## 2.2.0

### Added

**`StreamClient.add_error_handler`.** A stream handler which raises is logged
and skipped, and a connection which fails to close after logout is logged. Both
are the right behaviour, and both leave the caller with a log record rather than
something to react to. The only way to react was to attach a `logging.Handler`
and match on message text, which every consumer who cared had to write and which
coupled them to strings this library is free to reword.

```python
def on_stream_error(service, exception, message):
    alert('schwab stream: %s raised %r' % (service, exception))

stream_client.add_error_handler(on_stream_error)
```

Registering none keeps the existing behaviour exactly, and the log line is
written either way.

It reports three kinds of failure: a stream handler which raised, a late
rejection of a request nobody was waiting on, and a connection which failed to
close after logout. The middle one is the least obvious and the most
actionable -- the request was abandoned, so nothing else will ever say Schwab
refused it.

Handler failures are wired at two places in the code, not one. A synchronous
handler's exception passes through an `except` block; an asynchronous handler's
does not -- it surfaces in the task's done callback, at a different logging
level, in a different function, with no `except` around it. Wiring only the
`except` clauses would look complete and cover synchronous handlers only, which
for this purpose is worse than none: it turns "no signal" into "a signal, and it
is quiet". There is a test that fails if the async site is left unwired and
passes if only the synchronous one is checked, and the async case asserts on the
service name as well as the exception -- reporting it as `None` would satisfy a
weaker assertion while making "mark this subscription unhealthy" impossible.

The handler may be a coroutine function, like every other handler on this class,
and it is **awaited** rather than scheduled — the report finishes before the call
that found the failure returns. That keeps the report reliable with no machinery
to keep it alive: nothing to drain at shutdown, so nothing that can deadlock,
time out or be cancelled half-delivered while draining. The cost is that the
handler runs on the path that found the failure, so a slow one holds up
`handle_message`, exactly as a slow synchronous handler always has. Hand off to
your own task if you need to do something slow.

A handler of the wrong shape is refused at registration rather than discovered
at report time. Every other `add_*_handler` here takes a one-argument callback,
so `add_error_handler(lambda exc: ...)` is the natural mistake -- and it would
raise `TypeError` on every report, inside the `except` clause that exists to
stop an error handler failing the stream, so it would never run and never say
so.

### Documented

**Relabeling is not applied uniformly, and the docs said nothing about it.**
Content on the `data` channel is relabeled from numeric field ids to names.
Content on the `notify` channel is forwarded unchanged, ids intact. Both reach
the same handlers, so a handler assuming relabeling mis-parses a notify frame --
by finding nothing rather than by raising, since the keys it looks for are
simply absent. The Data Field Relabeling section explained the mechanism and
never mentioned the exception.

**What `ACCT_ACTIVITY` payloads actually look like.** Schwab documents the
three top-level fields and nothing inside `MESSAGE_DATA`, so every consumer
reverse-engineers it privately. The order identifier appears under seven
spellings, the symbol under four, and both `CANCELED` and `CANCELLED` occur in
Schwab's own status tokens. That is now written down, collected from live
traffic, and labelled as an observation log rather than a
contract -- nothing in it is validated by this library, and a shape that has not
been seen is not thereby impossible.

### Changed

**This fork no longer tracks upstream.** Through 2.1.0 every change was branched from a mirror of
`alexgolec/schwab-py` and offered as a pull request before being merged here, on the reasoning that
anything upstream took would shrink the divergence. Upstream merged none of them, and its
maintainer confirmed in September 2026 that he does not intend to update the project.

So the arrangement had become a ritual with a real cost: the last several changes were cut from
`main` because they could not sensibly be cut from anywhere else, and each needed a paragraph
explaining why it had not been sent. Topic branches now come from `main`, there is no PR queue, and
changes are made because they are right for this library rather than because upstream might take
them. `MAINTAINING.md` records what this changed and what it did not.

Nothing about the code's authorship changes. This is still overwhelmingly Alex Golec's work, the
licence and attribution are untouched, and if upstream ever resumes, `upstream-main` is still
mirrored here and reconciling with it beats defending the fork.

## 2.1.0

> **This is a breaking release despite the minor version.** It removes three
> deprecated APIs and changes how `decimal.Decimal` prices are rendered. The
> number is minor because this fork is installed from pinned tags rather than
> version ranges -- nothing upgrades into it by accident -- but if you are
> moving from 2.0.x by hand, read this section rather than skimming it.

### Removed

Three APIs which had been deprecated with warnings. All three are breaking for
code still using them, and all three fail loudly at the point of use rather
than changing what an order or a subscription means.

**Prices must be strings.** `set_price` and `set_stop_price` used to accept
any number and truncate it -- two decimal places, or four below one. Passing a
number now raises `ValueError` naming the fix.

Note this includes **integers**: `set_price(1250)` raises, not just
`set_price(1250.0)`. Integer prices were the form this project's own
documentation used, so auditing your call sites for float literals alone will
miss some. The removed helper was named `truncate_float`, which is worth
searching for too -- it was a public module-level name, and anything importing
it now gets an `ImportError`.

The conversion was removed rather than kept because the library was choosing a
rounding, for a value denominated in money, on behalf of a caller who knows
what the order is for. Whether a limit should round up, down or to the nearest
tick is a trading decision.

**`'{:.2f}'.format(value)` is not an equivalent.** It rounds; the old code
truncated toward zero, and used four decimal places below one. `19.9999999`
became `19.99` rather than `20.00`, and `0.186992` became `0.1869` rather than
`0.19`. On a buy limit that difference is a price one tick higher than the one
asked for. The docs carry the exact `decimal.ROUND_DOWN` equivalent for anyone
who wants to migrate without moving any prices.

**`decimal.Decimal` now passes through exactly, where it used to be truncated.
This one is a silent change and the only one in this release.** A `Decimal` was
not a `str`, so it fell through to the same conversion floats did:
`set_price(Decimal('12.129'))` sent `'12.12'` up to 2.0.1, and sends `'12.129'`
now. If you compute prices as `Decimal` and relied on the library to round
them, it no longer will, and a sub-penny equity limit is rejected by Schwab
rather than caught here.

That is the intended behaviour rather than an oversight -- a `Decimal` carries
the precision its author chose, and silently discarding it was the bug -- but
it is a change to what a previously correct call puts on the wire, so check
your `Decimal` call sites. It is rendered with `format(d, 'f')` rather than
`str(d)`, because `str(Decimal('1E+2'))` is `'1E+2'`, which is not a price.

**Build a `Decimal` from a string, not a float.** `Decimal(0.1)` is that
float's binary expansion to 55 decimal places, and rendering it exactly -- the
point of accepting `Decimal` at all -- puts all 57 characters on the wire. Up
to 2.0.1 the truncation hid this.

It is not refused. A guard on decimal places was written for this release and
then removed, because measuring it showed the guard could not do what it
claimed: over realistic inputs, float-derived values span 0 to 53 decimal
places while string arithmetic spans 1 to 9, so no threshold separates
contamination from an honest computed limit. What a threshold does reliably is
refuse a valid price at order-placement time. And the payload it prevents is
merely long, not wrong -- Schwab types both `price` and `stopPrice` as
`number($double)`, so a 57-character spelling parses to the same double a short
one does.

So `Decimal(str(value))` is a habit this library recommends and does not
enforce, and rounding a computed price stays your decision:
`value.quantize(Decimal('0.01'))`.

`copy_price` and `copy_stop_price` still skip the type check, which is what
`contrib.orders` uses to reconstruct a historical order. They do refuse a
non-finite `Decimal`, since rendering one produces the transmittable string
`"NaN"`; `float('nan')` is still accepted there because it does not survive
serialization -- `httpx2` builds request bodies with `allow_nan=False`, so the
request raises before it is sent. Note that a bare `json.dumps` of the built
order does *not* raise: it emits the invalid-JSON token `NaN`. If you serialize
a spec yourself for logging or a diff, that is what you will see.

**`websocket_connect_args['extra_headers']` is no longer translated.**
websockets 14.0 renamed it to `additional_headers`; this library had been
rewriting it silently and warning. Passing it now raises `ValueError` naming
the replacement, as `create_protocol` and `read_limit` already did.

The argument is documented as a passthrough to `websockets.connect()`. Quietly
substituting one name for another on the way through is not a passthrough, and
it meant the name a caller wrote was not the name the library called.

**`LevelOneOptionFields.STRIKE_TYPE` is gone.** It was an alias of
`STRIKE_PRICE`, kept when field 20 was renamed to what Schwab actually
documents it as. Code naming it now raises `AttributeError` instead of reading
a field whose messages are labelled something else.

### Fixed

**`Decimal` is for the price fields only, and is refused elsewhere at the
setter.** `quantity`, `activationPrice`, `stopPriceOffset` and `priceOffset`
are `number($double)` in Schwab's schema, so passing a `Decimal` to one raises
where the field is still known.

This is narrower than it may read: a plain `str` in those fields is still
accepted and still produces a string where Schwab's schema says number.
`Decimal` is refused because this library would be the one rendering it, which
is a choice it should not make silently; a string is what the caller wrote.
Whether to refuse that too is a separate question, not settled here.

**`copy_price` and `copy_stop_price` could not take a `Decimal`.** They skip
validation by design, so a `Decimal` reached the object builder raw -- and
having no `__dict__`, it fell to the reflection path and raised `vars()
argument must have __dict__ attribute` from `build()`, far from the call that
caused it and naming neither the field nor the type.

**A non-finite `Decimal` now raises a clear error from every numeric setter.**
`_assert_finite` reads `float(Decimal('sNaN'))` raising `ValueError` as "not a
number at all" and returned, so a signalling NaN reached the `<= 0` comparisons
in the setters and raised a bare `decimal.InvalidOperation` whose message is a
repr of its own class -- the exact failure that check runs first to prevent.
2.0.x refused these too, just unreadably; this is a diagnosis fix, not a safety
one. (`Decimal('sNaN')` never reached an order in a released version. It could
briefly during this release's development, which is why the test exists.)

**`OptionSymbol` accepted a non-finite strike.** `float('nan')` parses and
`nan <= 0` is `False`, so `'nan'` and `'inf'` passed the constructor's
positivity check and failed later inside `build()` as `cannot convert NaN to
integer`, naming neither the strike nor the symbol. They are refused at
construction now, next to the positivity check.

**`OptionSymbol` accepted strikes it could not encode.** (The checks that
enforce this are exact regardless of the process-wide `decimal` context. They
read the parsed strike's own digits rather than scaling it first, because
multiplying consults the context and no fixed precision covers every input.) The symbol's strike
field is eight digits of thousandths, so it cannot carry more than three
decimal places and stops just below `$100,000`. Neither limit was checked: a
strike of `2.0019` was truncated to `00002001`, naming a `$2.001` contract, and
`0.0005` became a strike of zero, while `700000` widened the field and produced
a 22-character symbol. Both are refused at construction now, where the strike
is still in hand, rather than producing a symbol for a different contract or
none.

**Option symbols encoded some strike prices a tenth of a cent low.**
`OptionSymbol.build()` scaled the strike by 1000 as a binary float and
truncated with `int()`, which truncates the representation error along with the
value: `2.01 * 1000` is `2009.9999999999998`, so the symbol came out
`...C00002009` rather than `...C00002010` -- a different contract, or one that
does not exist. 590 of the 100,000 cent-granular strikes between `$0.01` and
`$1000.00` were affected, on the order-placement path.

This is the same defect that made limit prices a cent low, in a different
function, and it survived the fix to that one. It now scales in `decimal`,
which is exact: the strike is already validated as a string on the way in.

### Note

The three removals convert a silent accommodation into an error, so code which
already passed strings, used `additional_headers` and named `STRIKE_PRICE` is
unaffected by them.

The `Decimal` change above is the exception, and the only thing here that
alters a working call without raising. If you pass `Decimal` prices, read that
paragraph.

## 2.0.1

### Documented

**2.0.0 moved where TLS trust comes from, and did not say so.** `httpx` depended on
`certifi` and built its default SSL context from the CA bundle shipped inside that
wheel. `httpx2` does not depend on `certifi` at all -- it uses `truststore`, which
reads the **operating system** trust store:

```
httpx/_config.py    create_ssl_context -> import certifi
                    ssl.create_default_context(cafile=certifi.where())

httpx2/_config.py   create_ssl_context -> import truststore
                    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
```

Two things follow, neither of which is a defect, but both of which are worth
knowing before you move this onto a new host:

- **TLS now depends on the host having a populated CA store.** A slim or distroless
  container without `ca-certificates` worked on 1.11.4, because the bundle travelled
  inside the `certifi` wheel. On 2.0.0 and later, certificate verification fails and
  surfaces as `httpx2.ConnectError`. It will not hide: nothing in this library
  catches that outside the login flow's readiness probe, so it reaches your caller
  on every request rather than being absorbed and retried. A sudden run of connect
  errors immediately after a host or image change is this, not the network.
- **The trust set is whatever the OS trusts.** An interception or corporate root
  installed in the host store is now trusted for Schwab traffic, where previously
  only certifi's bundle was. That is a real change in posture for a process holding
  tokens with full account access.

`SSL_CERT_FILE` and `SSL_CERT_DIR` still override, if you would rather pin the trust
set explicitly than inherit the host's.

### Removed

**Four dependency declarations that nothing imports.** `certifi` was there to keep a
current CA bundle in the `httpx -> httpcore -> certifi` chain, which 2.0.0 removed;
leaving it declared implies a bundle that is no longer in the path. `urllib3`,
`python-dateutil` and `nose` are referenced nowhere in the library, the tests or the
tooling.

**A warning suppression which could never fire.** The login flow's readiness probe
wrapped its `verify=False` request in a filter for
`urllib3.exceptions.InsecureRequestWarning`. `urllib3` is not in `httpx2`'s stack and
was not in `httpx`'s either; measured against a self-signed local server, `httpx2`
emits no warning at all on `verify=False`. The filter, the `urllib3` import and two
`-W ignore::urllib3.exceptions.*` entries in `tox.ini` are all gone.

### Fixed

**`make release` uploaded to PyPI with the tests switched off.** It ran
`twine upload dist/*` against `schwab-py`, which on PyPI is the *upstream* project --
so it would have replaced someone else's package with this library under their name.
The test step had been commented out with a `# TODO: Reinstate tests before
releasing` left in place. Inherited from upstream, where publishing to PyPI is
correct. There is now no release target: it prints why and exits non-zero, and
points at `MAINTAINING.md`. `make coverage` ran `nose`, which does not work on any
Python this project supports, and `make dist` used the deprecated
`setup.py sdist bdist_wheel`.

**`requests` was used by the test suite and declared nowhere.** It arrived only
because `twine` pulls in `requests-toolbelt`. It is now a dev dependency in its own
right.

## 2.0.0

### Changed

**This library now uses `httpx2` rather than `httpx`, and requires Authlib
1.8 or later.** Both are breaking changes for anything that catches an
exception from a client call or type-checks a response.

Authlib 1.8 stopped importing `httpx` directly. Its `httpx_client` integration
imports `httpx2` when that package is installed and falls back to `httpx` when
it is not, emitting a deprecation warning it forces past the default filters.
The fallback is documented as temporary. Since `OAuth2Client` inherits from
whichever module Authlib resolved, the client's response and exception types
were being decided by whatever else happened to be installed.

`httpx` and `httpx2` share no hierarchy: `httpx2.Response` is not
`httpx.Response`, and `httpx2.HTTPStatusError` is not a subclass of
`httpx.HTTPStatusError`. Disagreeing with Authlib therefore fails silently
rather than loudly -- an `isinstance` check stops matching, an `except` clause
stops catching, and only in production, where the response comes from Authlib's
session rather than from a test which constructed its own.

**If you catch `httpx` exceptions around calls into this library, or check
responses against `httpx` types, those now need to name `httpx2`.** Catching a
tuple of both is a way to cross the change without a flag day.

Declaring `httpx2` alone would not have been enough. The previous floor of
`authlib>=1.6.0` left Authlib 1.7.2 with `httpx2` installed a legal
resolution -- Authlib on `httpx`, this library's guards on `httpx2`, every
guard silently dead. The floor moved to `authlib>=1.8` for that reason.

### Added

**A test that a half-finished version of this change cannot pass.**
`tests/http_module_test.py` compares the module Authlib built `OAuth2Client`
on against the module each guarding file imported, and fails when they differ.

It was needed because nothing else could see the problem. Every other test in
the suite constructs the type it then asserts on, so the whole of it passed
against Authlib 1.8 with `httpx2` installed while the library still guarded on
`httpx` -- 824 of 824 green, on 3.12 and 3.14, in exactly the configuration
where `add_child_order_strategy` no longer recognises a response and the login
flow's readiness wait no longer absorbs a refused connection.

## 1.11.4

### Fixed

**A test added in 1.11.3 raced a server it never started.** The new coverage
for the connect-timeout case mocked the readiness check to report success, then
let the flow open a browser whose stub makes a real request to the callback
port -- where nothing was listening, because the readiness check was a mock.
Local timing hid it; one CI runner did not:

```
ConnectionRefusedError: [Errno 111] Connection refused
FAILED test (ubuntu-latest, 3.12, py312)
```

No library change. The test now delivers no callback and asserts on the timeout
plus the attempt count, which is what it was actually for.

## 1.11.3

Cross-platform. This fork's CI runs Linux on every push and all three platforms
on tags, and the tag builds had been red on macOS and Windows for as long as
that matrix has existed -- while the badge, which tracks the default branch,
stayed green on Linux. Nobody looked.

### Fixed

**A connect timeout ended the login flow instead of being waited through.**
`client_from_login_flow` starts a local server and polls it until it answers,
catching `httpx.ConnectError` for "not listening yet". Whether an unbound port
refuses a connection or silently drops it is a property of the host: a firewall
drops, and so do the macOS runners. A dropped attempt raises
`httpx.ConnectTimeout`, which is a *sibling* of `ConnectError` rather than a
subclass --

    ConnectError    -> NetworkError     -> TransportError
    ConnectTimeout  -> TimeoutException -> TransportError

-- so it escaped and took the login flow down while the server was still
starting. Every login-flow test fails on macOS for this reason. It is not a
test-only problem: on a machine that drops rather than refuses, the login flow
could not complete at all. Offered upstream as #264.

### Documentation

**The token file is not protected on Windows, and the docs said it was.**
Windows has no POSIX file mode -- `os.chmod` toggles a read-only bit and
ignores the rest -- so the token is left readable by any account on the
machine. The docs stated flatly that it is written mode `0600`. For a file that
grants full access to a funded account, claiming a protection that does not
exist is worse than claiming nothing. Now a warning saying what a Windows user
should do instead, and noting that the atomic-write and durability properties
*do* hold there; only the permission narrowing does not.

The test asserting mode `0600` is now skipped off POSIX rather than relaxed to
something both platforms satisfy, which would have stopped it meaning anything
where it matters.

### Notes

Every "tests passing" figure reported during the 1.9.0-1.11.2 work was Linux
only, and said so nowhere. The macOS and Windows columns existed the whole
time.

## 1.11.2

A second review, over the whole divergence from 1.5.0 rather than the last
release. One serious defect and four small ones.

### Fixed

**A late stream response wedged every request that followed it.**
`_read_and_route` checked each response frame against whichever request was
pending and, on a mismatch, failed that request with it. So the late answer to
an abandoned request killed an unrelated one -- and was consumed doing it,
leaving the next stale answer for the next request:

```
request 1: ResponseTimeoutError (timed out)
request 2: UnexpectedResponse: unexpected requestid: 0
request 3: UnexpectedResponse: unexpected requestid: 1
request 4: UnexpectedResponse: unexpected requestid: 2
```

One client-side timeout and the stream never completed another request. 1.9.0
taught `handle_message` to tolerate an orphaned response; this is the same
tolerance one level down, where the routing happens. Since this fork added
`DEFAULT_RESPONSE_TIMEOUT`, it supplied the trigger as well as the fault.

The rule is bounded on both sides: an id issued *earlier* is lateness and the
frame is handed back as an orphan, while an id never issued is genuine
protocol confusion and still fails, as it always has.

**The library warned about a datetime it invented itself.**
`get_price_history_every_day('AAPL')`, with no dates at all, warned about
`start_datetime` and blamed the caller's line -- the loudest possible false
alarm from 1.9.0's warning, on the most common way these endpoints are called.
The substituted default was naive, so the check could not tell it from
something a caller wrote. It also made the `startDate` for an unparameterized
call depend on the host's offset. Now explicitly UTC, and the suite passes
identically under four timezones, which it did not before.

**`priceOffset` was dropped when repeating an order.** `contrib.orders` knew
`priceLinkBasis` and `priceLinkType` but not the offset those apply to, so
`construct_repeat_order` returned a price-linked order with no offset -- a
different order at a different price, silently. `set_price_offset` is this
fork's own addition, which is why the table never grew an entry for it.

**A mangled link.** The fork's URL rewrite broke a string concatenation in
`add_child_order_strategy`, leaving `.../docs/index.rstorder-templates.html`,
a 404.

**A docstring promised a guard one configuration does not have.** Passing a
collection of order statuses raises `ValueError` with the enum check on, and
does not with `enforce_enums=False` -- which `client_from_login_flow` defaults
to. Now qualified.

### Notes

The `priceOffset` gap survived the audit's own round-trip probe, which
reported 48 of 48 templates identical. None of those templates uses a
price-linked order, so the number said nothing about the field. A probe that
passes because it never exercised the case is the same failure as a test that
passes either way.

1.11.1 shipped a comment claiming the path separator in the frame walk could
not be tested without a real sibling package on disk, and concluded it was
untestable. The first half was right; the conclusion was not. Building that
layout on disk is about ten lines, and there is now a test which fails when
the separator is removed.

## 1.11.1

A final audit of the audit. Everything here is a defect in something 1.9.0
through 1.11.0 introduced -- three of them in guards which did not guard as
much as they claimed.

### Fixed

**The finite-number check covered only prices.** It missed the other numbers
in an order, and it missed the worse case: the existing positivity guards do
not catch NaN, because NaN compares False against everything. `quantity <= 0`
and `activation_price <= 0.0` are both False for it.

```python
OrderBuilder().set_activation_price(nan).set_stop_price_offset(nan)
              .set_quantity(nan).build()
# {'quantity': nan, 'stopPriceOffset': nan, 'activationPrice': nan}
# json.dumps → {"quantity": NaN, ...}   which is not valid JSON
```

Those are the fields a trailing stop and a conditional order use.
`set_quantity`, the order legs, `set_activation_price`, `set_stop_price_offset`
and `set_price_offset` now check before the positivity comparison. Offered
upstream as #262.

**A locally-unusable token was reported as retryable.** `MissingTokenError`
and `InvalidTokenError` are `OAuthError` subclasses which authlib raises
*before any HTTP call*, when the stored token has no refresh token or cannot
be used. They arrived with `refresh_token_invalid=False` and a message saying
the failure "may be transient", so an application following the documented
pattern retried forever on a permanent, purely local condition -- the exact
failure the attribute exists to prevent, reached through the attribute.
Offered upstream as #266.

**The naive-datetime warning blamed the library, not the caller.** A fixed
`stacklevel` cannot be right when the endpoints sit at different depths --
`get_price_history_every_day` wraps `get_price_history`, and
`get_orders_for_account` goes through `_make_order_query`. Two of the four
entry points pointed at `base.py`, which tells a caller nothing they can act
on. The frames are now counted. Offered upstream as #259.

**The bug-report log guard missed a broken pipe.** It caught `ValueError` but
not `BrokenPipeError`, which is an `OSError`, so `python bot.py 2>&1 | head`
still produced the "Exception ignored in atexit callback" traceback the guard
was added to remove. Offered upstream as #257.

**A rejected subscription became invisible.** 1.9.0 stopped an orphaned stream
response from ending the receive loop, which was right, but logged every one
at INFO -- including failures. The request was abandoned, so nothing else ever
reports that Schwab refused it. Non-zero codes now log at WARNING. Offered
upstream as #261.

### Changed

**The token temp-file sweep waits five minutes rather than one.** The
threshold is not a tidiness knob, it is the entire mechanism keeping the sweep
from deleting a file a *different* process is still writing -- which was
demonstrated, and costs that process its token update. Widened, with the
reasoning moved into the constant, and the assumption that mtime and the local
clock agree written down for anyone on a network mount.

### Notes

Two of these were found because a test passed when it should not have. The
warning-attribution fix shipped with an off-by-one that a filename-only
assertion could not see, and the sweep's concurrency test used a file created
that instant, which survives any threshold. Both tests now fail against the
code they are meant to catch.

One thing is deliberately untested and says so in the source: the path
separator in the frame walk's prefix comparison. Reaching it needs a real
sibling package on disk, and every test written for it passed with the
separator removed.

## 1.11.0

### Added

**`TokenRefreshError.refresh_token_invalid`, saying whether a retry can work
at all.** 1.10.0 reported the token's age and left the caller to infer the
rest. This makes the inference directly.

A refresh token Schwab has rejected as invalid cannot be retried back to life:
a new one comes only from the authorization_code flow, which needs a human at
a browser. An unattended application which treats that like a dropped
connection keeps asking, and the endpoint it hammers is the one its own
recovery depends on.

```python
try:
    r = c.get_quote('AAPL')
except TokenRefreshError as e:
    if e.refresh_token_invalid:
        alert('refresh token is dead, log in again')
    else:
        retry_later()
```

RFC 6749 §5.2 defines `invalid_grant` for exactly this case, so the signal is
a standard one. Schwab does not put it where the standard says. It answers
with an outer code of `unsupported_token_type` -- which RFC 7009 defines for
the *revocation* endpoint and which describes nothing that happened -- and
nests the real response as a JSON string inside the description:

```
unsupported_token_type: 400 Bad Request: {"error_description":"Refresh token
is invalid, expired or revoked","error":"invalid_grant"}
```

Observed on a live account on 2026-08-02, by letting a refresh token reach its
seven day expiry deliberately. Schwab documents neither the response nor the
nesting, so that is one account on one day rather than a specification. Both
placements are accepted, in case the outer code is ever corrected.

Anything unrecognized returns `False`. An application stopped by a failure it
could have retried through is worse off than one which retried a little too
long, so the unrecognized case falls on the transient side.

Offered upstream as #266.

### Notes

1.10.0's documentation said this library would not tell you whether a retry
could succeed, because Schwab documents the seven day term but not the
response. That was true of the documentation and remains true; it was not true
that nobody had seen the response. This corrects it.

## 1.10.0

The rest of the production-readiness audit -- the findings which were real but not
urgent, plus the one piece of design it turned up.

One behaviour change worth reading before upgrading: a rejected token refresh
now raises a schwab-py exception rather than an authlib one.

### Added

**`TokenRefreshError`, so a failed refresh has a type of ours.** The session
refreshes the token on the way past an ordinary request, so a refusal by
Schwab surfaced from `get_quote` as
`authlib.integrations.base_client.errors.OAuthError` -- a type this library
never mentioned, from a package the user did not choose to depend on. Measured
across the failure spectrum, callers were catching three different third-party
types and none of them ours.

`schwab.utils.TokenRefreshError` now wraps it, keeping the original as
`__cause__` and carrying the token's age:

```python
from schwab.utils import TokenRefreshError

try:
    r = c.get_quote('AAPL')
except TokenRefreshError as e:
    if e.token_age is not None and e.token_age > 7 * 24 * 60 * 60:
        alert('token past its seven day window, log in again')
    else:
        retry_later()
```

It deliberately does **not** classify the failure as terminal or transient.
Schwab documents the seven day refresh token term but not what the token
endpoint returns when that term expires, so a classifier keyed on error codes
would be built on a payload shape nobody has seen. The age is documented; the
error code is not. Offered upstream as #263.

**`Client.close_session()`.** `AsyncClient` could always be closed and the
synchronous client could not, so an application creating clients repeatedly
held their connections until each session happened to be garbage collected.
Both are now documented, which neither was. Offered upstream as #265.

### Fixed

**Temporary token files left behind by a hard kill are now swept.** The
cleanup on the exception path never runs for a `SIGKILL`, and what a killed
write leaves behind is not an empty file -- it is a complete, readable copy of
the token. Measured: 25 processes killed inside the write window left 12 of
them, and nothing ever removed them. A refresh token stays valid for the rest
of its seven days, so a process which crash-loops accumulates live credentials
next to its token file. Each subsequent write now removes any it finds, but
only files matching this library's own temporary name, and only once they are
a minute old -- another process may be partway through its own write, and
deleting its temporary file would break it. Added to #232, which introduced
the temporary file.

**The wait for the login callback server is bounded, and checks what
answered.** A server which started but never answered left
`client_from_login_flow` spinning with nothing on screen to say why; it was
still going when killed at 12 seconds. Worse, the status check's response was
assigned and never read, so *any* listener on that port counted as the
callback server -- and continuing hands it the login redirect, which carries an
authorization code good enough to take over the account. A non-200 now refuses
the flow. Interactive path only. Offered upstream as #264.

### Documentation

`client_from_access_functions` said "Please see this example for details"
about the exact signatures a custom token-storage function must have, and
linked to a file which returns 404 in this fork and upstream. The one place
the signatures were explained was the one place that was not there. They are
now written out inline, where they cannot rot the same way.

## 1.9.0

From a production-readiness audit which mostly ran the library rather than
reading it. Four defects, three of them silent.

Two change behaviour in ways worth reading before upgrading: aware datetimes
now produce different (correct) requests on three endpoints, and a NaN price is
now refused rather than sent.

### Fixed

**A timezone-aware datetime was formatted, not converted, before being labelled
UTC.** Schwab documents `fromEnteredTime`, `toEnteredTime` and the transaction
dates as `yyyy-MM-dd'T'HH:mm:ss.SSSZ`, where the trailing `Z` asserts UTC.
`_format_date_as_iso` applied `strftime` to whatever it was given, so a
datetime carrying any other timezone had its local wall clock stamped as UTC:

```python
eastern.localize(datetime.datetime(2024, 6, 5, 0, 3, 2))   # == 04:03:02 UTC
# sent as fromEnteredTime=2024-06-05T00:03:02Z             -- four hours early
```

Passing a correctly-zoned datetime is what triggered it. Naive datetimes were
unaffected, as were the price history endpoints, which use a different encoding
that was already correct. Affects `get_orders_for_account`,
`get_orders_for_all_linked_accounts` and `get_transactions`. Offered upstream
as #258.

**A response nobody was waiting for ended the caller's receive loop.**
`handle_message` raised `UnexpectedResponse` on any response frame. One reaches
it whenever a request was abandoned -- timed out, or cancelled -- and the
server answered afterwards, and that answer is frequently a *successful* one.
So a subscribe which timed out client-side and in fact worked killed the
session the moment Schwab acknowledged it, taking every message queued behind
it. That is most likely exactly when the venue is slow, which is when a stream
is least worth dropping. Now logged and skipped. `UnexpectedResponse` still
covers a response whose service, command or request id does not match the
request being waited on. Offered upstream as #261.

**Prices which are not a finite number built sendable orders.**

```python
equity_buy_limit('AAPL', 10, float('nan')).build()
# {'orderType': 'LIMIT', 'price': 'NaN', ...}
```

NaN is not typed, it is computed -- a limit derived from a quote that was
missing. The builder already refused a non-positive quantity, so this is the
same check on the other half of the order. The string spellings are refused
too, since `str()` of a computed price is the documented way to pass one.
Strings which are not numbers at all still pass through, and `copy_price` still
bypasses everything as documented. Offered upstream as #262.

**Bug report logs did not redact Schwab's account identifiers.** The default
patterns matched an earlier API's field names -- `accountId`, `displayName`.
Schwab returns `accountNumber` and `hashValue` and neither matched anything, so
the two identifiers a user is least likely to want in a public issue were the
two that survived. Named in full rather than adding `account` as a substring:
redaction is a whole-log string replacement, and `account` also matches
`accountValue` and `accountColor`, which would take every balance and the word
"Green" with it. Offered upstream as #260.

### Added

**A warning when a date parameter is given a datetime with no timezone.** These
parameters name an instant; a naive datetime does not. The epoch-millisecond
encoding reads one as the host's local time, so the same source line sends
different requests on different machines:

```
host set to UTC:                startDate=1786008600000
host set to America/New_York:   startDate=1786023000000
```

Four hours apart, with nothing in the request recording which was meant.
Warning rather than reinterpreting: silently treating them as UTC would change
behaviour for anyone relying on local time without telling them. Aware
datetimes and plain `date` objects are unaffected. Offered upstream as #259.

### Documentation

A **Dates and Times** section in the client documentation, which previously
said nothing about timezones anywhere. Covers both encodings, the measured
numbers above, and the three spellings that remove the ambiguity.

### Notes

Nothing here changes how a request is built for a caller already passing aware
datetimes to the price history endpoints, or already passing prices as strings.

The audit also confirmed, by measurement rather than reading: 80 `SIGKILL`s
inside the token write window corrupted nothing; 400 streaming cancellation
storms left no orphaned lock and every client usable, where v1.7.1 fails 40 out
of 40; price truncation agrees exactly with an independent oracle across 40,009
values; and all 48 order templates round-trip byte-identically through
`contrib.orders`.

## 1.8.1

### Fixed

**`enable_bug_report_logging()` wrote its report to whatever `sys.stderr` was at
import time.** The stream was captured as a default argument, which Python
evaluates once when the module is imported -- not when the logs are written,
which happens at program exit. An application that redirects `sys.stderr` after
importing the library, as a daemon writing to a log file does, had its bug
report delivered to the stream it redirected away from. Measured with a
`StringIO` standing in for the log file, the redirected stream received 0 bytes
before the fix and the whole report after it.

If that original stream had since been closed, the program's last output was a
traceback out of an `atexit` handler instead of the report. This library's own
test suite printed one on every run.

`sys.stderr` is now looked up when the report is written, and a closed stream is
treated as nowhere to write rather than something to raise about. Passing
`output=` explicitly is unchanged. Offered upstream as #257.

### Documentation

The README's "Why should I use `schwab-py`?" section listed two reasons and left
out the two largest parts of the library: the streaming client and order
construction. Rewritten to cover both, with the "minimal wrapping" claim narrowed
to the HTTP layer where it holds -- the parameter enums, `OrderBuilder` and the
streaming relabeler are all deliberately opinionated. Offered upstream as #255.

Nothing in this release changes how a request is built or an order is
constructed.

## 1.8.0

Most of this comes from reading Schwab's published documentation against the
library, rather than from finding something at runtime.

### Fixed

**Field 20 of `LEVELONE_OPTIONS` is the strike price, not a "strike type".**
Schwab's streamer documentation gives it as `| 20 | Strike Price | double |
Contract strike price |`. It was named `STRIKE_TYPE`, so every relabeled option
quote carried a key describing neither the field nor its contents -- and the
enum had no strike price at all, across fifty-six fields on an options quote
feed. Reported upstream as #197.

`STRIKE_TYPE` is kept as an alias, so code naming it keeps working. Messages are
now labeled `STRIKE_PRICE`, which is a **breaking change** for anything reading
that key.

This needed a supporting fix. `key_mapping` built its number-to-name table from
`__members__`, which yields aliases as well as canonical members, so whichever
alias was defined last would silently have decided the label. It now iterates
the enum itself. No other field enum has an alias, so nothing else changes.

**A historical order with an `UNKNOWN` field could not be reconstructed, and
said so badly.** Schwab documents `UNKNOWN` as a value both `duration` and
`orderType` can come back as, while stating it is not accepted as an input.
Omitting it from the request enums is therefore right, but
`construct_repeat_order` fed historical values into those same enums and failed
with a bare `KeyError: 'UNKNOWN'`, naming neither the field nor the problem.

Such an order genuinely cannot be repeated, so the fix is to say so rather than
to accept the value: `UnrepeatableOrderError` names the field, carries the
value, and explains that Schwab reports it but will not accept it.

**`get_price_history`'s docstring contradicted Schwab's documentation.** It said
`period` "should not be provided" alongside a date range. What Schwab documents,
under `startDate`, is "if not specified startDate will be (endDate - period)" --
so `period` derives a start date when one is absent rather than overriding one
that is present. Supplying both is harmless. The valid `period` values per
`period_type` are now documented too; they were not stated anywhere.

That wording is what led 1.7.0's release notes to claim Schwab honours `period`
over an explicit range. It does not, and those notes have been corrected.

### Added

`UnrepeatableOrderError`, described above.

### Changed

Documented what this fork already did but never explained: that the token file is
a credential written `0600` and replaced atomically, that surrounding whitespace
is stripped from keys and secrets and why the warning is worth acting on, and
that a subscription can be made while another coroutine waits in
`handle_message`.

CI now runs `actions/checkout` and `actions/setup-python` at v7 rather than v2,
which were five years old and being force-run on a Node runtime they were not
built for. Dependabot watches them monthly so they cannot drift that far again.

### Verified against the documentation, and correct

Worth recording, since the point of the exercise was to find gaps: all thirteen
order enums match the published schemas value for value, apart from the `UNKNOWN`
handling above. All eleven market-data parameter enums match. Every market-data
endpoint and parameter is implemented. Across eight streaming services and 230
fields, every field number is correct and field 20 was the only name that meant
something different from the field.

One thing that looked wrong and is not: the `CHART_EQUITY` field table in
Schwab's streamer documentation disagrees with this library, and the library is
right. Against a real message, only the library's mapping yields self-consistent
OHLC; the documented one gives an open of 779 on a $421 stock and a high below
the low.

### Breaking changes

- Option quotes are relabeled with `STRIKE_PRICE` where they previously used
  `STRIKE_TYPE`. The enum member `STRIKE_TYPE` still resolves, so only code
  reading the key out of a relabeled message is affected.

---

## 1.7.2

### Fixed

**Cancelling a stream request left the client unusable.** A request waiting for
its response races two things: the response arriving via whoever is currently
reading the socket, and the socket becoming free so it can read for itself. The
second is an `asyncio.Lock.acquire()`, and it can succeed at the same moment the
waiter stops waiting.

That race was handled when the wait timed out, but not when the waiting
coroutine was cancelled. A cancellation delivered while the acquire had already
completed skipped the branch which releases, so the read lock was left held by
nobody:

    after cancel: read_lock held=True, request_lock held=False
    client still usable after a cancelled request: no

Nothing could read after that, for the rest of the process. Every exit from the
wait now releases what it took.

This was introduced in 1.7.0 by the change which stopped requests blocking
behind a waiting consumer, so it does not affect 1.6.0 or earlier. Anyone on
1.7.0 or 1.7.1 who cancels stream operations -- during shutdown, or by wrapping
a subscription in a timeout -- should take this.

---

## 1.7.1

No library changes: `schwab/` is identical to 1.7.0 apart from the version string.
This release exists because the 1.7.0 tag shipped install instructions which
installed the wrong project.

### Fixed

**The install instructions installed upstream.** The README's getting-started
section and `docs/getting-started.rst` both said `pip install schwab-py`, which
fetches the original project from PyPI. A reader who got as far as installing
got upstream, whatever the fork notice at the top of the page said -- and it
would appear to work, since the importable package has the same name either way.
The fork notice itself still pinned 1.6.0.

**The documentation build could not run from a clean checkout.**
`docs/requirements.txt` pinned `websockets==12.0`, and this library imports
`websockets.asyncio`, which does not exist before 14.0. It went unnoticed because
a development environment already has a current websockets, so it only failed
somewhere clean. `authlib` and `httpx` were pinned below the floors `setup.py`
requires. All are floors now, so they cannot drift from `setup.py` silently.

### Added

A security policy, an issue template chooser, a pull request template, and
`.gitignore` rules for the token file -- which is a live credential, and which
nothing previously stopped git from offering to commit.

---

## 1.7.0

### Fixed

**Response redaction never ran.** Both client modules imported
`register_redactions_from_response` and then defined a no-op of the same name
over the top of it, so every call after every request went to the stub. The API
key and the token are registered elsewhere and were genuinely redacted, but
nothing the redactor was meant to pick out of responses — account numbers,
account hashes, the other id-ish values it looks for — ever was. Since
`enable_bug_report_logging()` tells users their logs are scrubbed, someone
following the documented process for filing a bug could publish those.

Collection is now gated on `enable_bug_report_logging()`, which is the only
thing that promises redaction and already documents that it carries a
performance penalty. Clients which do not call it are unaffected, which is what
they were getting anyway.

**A request could not be made while a consumer was waiting for a message.** One
lock covered every stream operation and `handle_message` held it across the
read, so on a quiet stream a subscription, unsubscription or logout waited for
a message to arrive first — indefinitely, if none did. Measured beforehand: a
subscribe issued against a parked consumer sat for a full second without a
single byte reaching the socket.

Reading and requesting are now separated. A read lock keeps a single reader,
since `websockets` does not allow two coroutines to call `recv()` concurrently,
and whoever holds it routes what it reads: a response to a request in flight is
delivered to the coroutine waiting for it. A request sends immediately and then
waits on whichever comes first — its response arriving via the reader, or the
socket becoming free so it can read itself. Response validation and every error
it can raise are unchanged.

**The price history helpers ignored the date range they were given.**
`get_price_history` documents that `period` "should not be provided if
`start_datetime` and `end_datetime`", and all seven `get_price_history_every_*`
helpers provided both. `period` is now sent only when no range was asked for,
since the helpers synthesize one spanning decades when the caller gives none.

What Schwab does when it receives both is **not** consistent across accounts. The
upstream report describes the range being disregarded; one account measured
here returned byte-identical responses with and without `period`
across four frequency and range combinations, so there the range was honoured
either way. Both can be true — entitlements, symbol class, or the API changing
since the report would all explain it. The fix does not depend on which: sending a
`period` the caller never asked for is wrong regardless, and it is what the
library's own docstring says not to do.

**The asyncio example called a function which does not exist.**
`asyncio.run_until_complete()` is not a thing; `run_until_complete` is a method
on an event loop. The example now uses `asyncio.run()`.

**Two format strings did nothing.** A DELETE was logged with
`'Req %s: DELETE to %s'.format(...)`, which does not substitute `%s`, so every
DELETE logged its placeholders verbatim. The price history path called
`.format(symbol)` on a string with no placeholders.

### Added

**`OrderBuilder.set_price_offset`.** The stop-price side already had a basis, a
type and an offset; the price-linked side had a basis and a type but no offset,
so a price-linked order could not be expressed at all and had to be assembled as
a raw order.

### Changed

Removed the original project's Discord invitations, funding links and badges,
its `tda-api` transition guide, and 44 links to `developer.tdameritrade.com` and
`tickertape.tdameritrade.com`, whose portals were retired along with the API
they documented. Several documentation and issue links pointed at repositories
which do not exist. Documentation now describes this fork's behaviour, and
issues are directed at this fork's tracker with a note that anything not caused
by these changes is better reported upstream.

`tox.ini` now covers py313 and py314, which CI already ran.

### Breaking changes

- A request which is never answered raises `ResponseTimeoutError` rather than
  blocking message handling. This was already true in 1.6.0; what changed here
  is that it no longer blocks anything except subsequent requests.
- Messages read while a request is in flight are handed to `handle_message` in
  arrival order. Previously they were held back until the request completed,
  which could not interleave because the lock prevented it.

### Verification

Full suite passes on CPython 3.12 and 3.14.

---

## 1.6.0

First release of this project. Branched from `alexgolec/schwab-py`'s `main`
a few commits past their v1.5.0 release — far enough to pick up an
in-development version string reading `1.5.1`, which was never tagged and never
released. **v1.5.0 is that project's last release**, so 1.5.0 is the highest
version number in this file that belongs to anyone else.

### Fixed

**Prices passed as floats could be a cent too low.** `set_price` and `set_stop_price` truncate
rather than round, which is intentional and documented, but the truncation was computed by scaling a
binary float and taking `int()`. That truncates the representation error along with the value:
`8.2 * 100` is `819.9999999999999`, so a price already at two decimal places came back as `8.19`.
Across every cent from $1.00 to $1000.00, 4,583 of 99,901 prices — 4.6% — were affected, always one
tick low and always silently. Truncation is now done in decimal. Sub-dollar prices at four decimal
places were wrong 5.7% of the time and are likewise fixed.

**The token file could be destroyed by a crash during a refresh.** The token was written with a
plain truncating `open(path, 'w')`, so a process that died between the truncation and the end of the
write left a file that could not be parsed — unrecoverable without a fresh interactive login. Tokens
are now written to a temporary file and renamed into place, which is atomic, so an interrupted write
leaves the existing token intact. The rename is flushed, and symlinked token paths are resolved so a
linked token file is written through rather than replaced.

**The token file was created world-readable.** It was written with whatever the umask permitted,
commonly `0644`, despite holding a credential granting full account access. It is now created `0600`,
with the mode set before the file becomes visible at its final name. Tokens written by earlier
versions are corrected on their next refresh.

**A stream handler that raised could take down the receive loop, or fail silently.** Handlers are
registered through one API but were dispatched with opposite error semantics: a synchronous handler
that raised propagated out of `handle_message`, aborting dispatch for every other handler on that
message and surfacing in the caller's loop as though the connection had failed; an asynchronous one
was scheduled and never awaited, so its exception was discarded entirely. Both kinds are now
isolated and logged. Message relabeling is inside the guard, so a message with an unexpected shape
is reported rather than escaping.

**A request the streaming server never answered wedged the entire client.** `_await_response` waited
with no upper bound while holding the lock that serializes stream operations, so an unanswered
request blocked every other operation — including message handling — for the life of the process.
Requests now time out. The websockets keepalive does not cover this case: a connection that is alive
but not answering keeps replying to pings.

**`notify` messages without a `service` key raised.** The `notify` dispatch path lacked the
membership check its `data` counterpart has. With a `defaultdict` this silently accreted an empty
entry per unrecognised service, and a frame without a `service` key raised `KeyError` out of
`handle_message`.

**The default price-history end date was off by the host's UTC offset**, and used the deprecated
`datetime.utcnow()`. `utcnow()` returns a naive datetime holding a UTC wall time, which
`_format_date_as_millis` then converted as though it were local.

**`get_orders_for_account` documented a `statuses` parameter that does not exist.** Passing it
raised `TypeError`. Multiple statuses are deliberately rejected, so the line was vestigial. Both
order-query methods now state that only a single status is accepted.

### Added

**`StreamClient.close()`**, plus `async with` support. There was previously no way to close a
stream: `logout()` sent the logout frame but left the socket, its keepalive task and its buffers
alive until the object was collected, leaving callers to finalize the connection during interpreter
shutdown. `logout()` now closes the socket as well, and a failure to close no longer masks the error
that caused it.

**Twenty-four equity order templates**, covering stop, stop-limit, trailing stop, trailing stop
limit, market-on-close and limit-on-close across all four equity instructions. The existing eight
templates covered `MARKET` and `LIMIT` only, so every other order type had to be assembled by hand.
Signatures of the existing templates are unchanged.

On the trailing templates, `stop_price_link_type` is a required argument rather than a defaulted
one. An offset of `2.5` means a 2.5% trail under `PERCENT` and a $2.50 trail under `VALUE`, the
venue accepts both, and there is no error to observe if the wrong one is chosen — so the caller is
asked to say which they mean.

**`response_timeout`** on `StreamClient`, defaulting to 60 seconds. `None` restores the previous
wait-forever behaviour.

### Changed

**`StreamClient` now uses `websockets.asyncio` instead of `websockets.legacy`.** The legacy
implementation has been deprecated since websockets 14.0 (2024-11-09), and because the import is at
module scope its eventual removal would break `import schwab.streaming` outright rather than
degrading a single code path. The dependency floor is now `websockets>=14.0`.

`websocket_connect_args` is a documented passthrough to `connect()`, and websockets 14.0 renamed
`extra_headers` to `additional_headers` and removed `create_protocol` and `read_limit`.
`extra_headers` is translated automatically with a `DeprecationWarning`; the removed arguments raise
an error naming the problem rather than an opaque `TypeError` from inside the library.

The connection object is now `ClientConnection` rather than `WebSocketClientProtocol`. Nothing in
the library names or annotates it, but code that annotates it itself will need updating.

**`Duration` now documents which values Schwab accepts for equity orders.** `IMMEDIATE_OR_CANCEL`,
`END_OF_WEEK`, `END_OF_MONTH` and `NEXT_END_OF_MONTH` are rejected at placement for equities with
`HTTP 400`. The values are retained, since they may be valid for other asset types, and because
removing an enum member would break anyone who has one wired up. Established by placing one equity
order per value against a live account.

### Breaking changes

- A synchronous stream handler that raises no longer propagates to the caller. Failures are logged
  with the service name. Callers relying on an exception to detect handler bugs should watch the
  `schwab.streaming` logger instead.
- Stream operations now raise `ResponseTimeoutError` after 60 seconds rather than waiting forever.
  Pass `response_timeout=None` to restore the old behaviour.
- `websockets>=14.0` is required.

### Verification

The full test suite passes on CPython 3.12 and 3.14. The websockets migration was additionally
verified against a live Schwab stream: login, level-one and account-activity subscriptions, a custom
`ssl_context`, and the `extra_headers` compatibility path.

The behaviour of `Duration` and the required fields for each equity order type were established by
placing real orders against a live account rather than from documentation.
