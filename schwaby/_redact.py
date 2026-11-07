'''Keeps secrets out of every log this library writes, at every level.

Always on. There is no mode to enable and no flag to forget: every logger this
library owns carries :class:`RedactingFilter`, first among its filters, and
``websockets`` is handed :class:`FrameRedactingAdapter` over whichever logger
it uses. A secret is
registered the moment it enters the library -- the app key and secret when a
client or flow is made, each token as it is loaded, exchanged for or
refreshed, the authorization code and the URL carrying it, each account number
a response or frame names -- and from then on any log line, exception text or
stack carrying it has the value replaced however the record is rendered.

Registration cannot be the only defence, because it only covers what was
registered. :func:`mask` replaces the values of the keys that carry secrets by
name, before this library formats a payload for a log line, so a token is
masked in the streamer's LOGIN request whether or not it was registered.

Imports nothing from this package, so every module can use it.
'''

import collections
import dataclasses
import json
import logging
import re
import sys
import threading
import urllib.parse

# Shorter values are not registered: replacing every occurrence of a short
# string across a log would mangle unrelated text, and nothing this library
# guards is that short.
_MIN_LENGTH = 6

# Old values are dropped past this many per label, since every registered value
# is a cost on every line logged. A token lives thirty minutes and a long
# process sees a new one every half hour, so only the last few can still be in
# use. Account numbers are never dropped: they do not expire, and every one
# registered came from a response naming the user's own accounts, so the set
# is bounded by the user rather than by uptime.
_PER_LABEL = 64
_LABEL_LIMITS = {'ACCESS_TOKEN': 4, 'ID_TOKEN': 4, 'REFRESH_TOKEN': 4,
                 'AUTHORIZATION_CODE': 4, 'REDIRECT_URL': 4,
                 'ACCOUNT_NUMBER': None}

#: Keys whose values are secrets wherever they appear in a payload this
#: library logs, compared case-insensitively. `code` is the OAuth authorization
#: code; Schwab's own payloads use `code` only for response codes, which are
#: integers and are left alone, since only strings are masked.
SECRET_KEYS = frozenset({
    'authorization', 'access_token', 'refresh_token', 'id_token',
    'client_secret', 'code',
})

# A secret-named parameter in any URL or form body, masked by pattern whether or
# not its value was registered: the login flow's callback server runs in a
# process of its own, which has registered nothing. A value starting with `<`
# is a redaction already, and is left as it is.
_SECRET_PARAMETER = re.compile(
        r'(?i)(?<![A-Za-z0-9_])((?:code|access_token|refresh_token|id_token|'
        r'client_secret)=)(?!<REDACTED)[^&#\s"\'<]+')

# A secret-named key in JSON text, masked by pattern for the same reason. Here
# without `code`, which Schwab's own frames use for response codes; the OAuth
# code reaches nothing JSON-shaped this library logs.
_SECRET_JSON_VALUE = re.compile(
        r'(?i)("(?:authorization|access_token|refresh_token|id_token|'
        r'client_secret)"\s*:\s*")(?!<REDACTED)[^"]*(")')

_lock = threading.Lock()
_registered = collections.defaultdict(collections.OrderedDict)
_pattern = None


def register(value, label):
    '''Never log ``value``; write ``<REDACTED label>`` in its place.

    Its URL-encoded spellings are registered with it, since a secret reaches a
    log inside URLs and form bodies as well as on its own.
    '''
    global _pattern
    if not isinstance(value, str) or len(value) < _MIN_LENGTH:
        return
    forms = frozenset({value, urllib.parse.quote(value, safe=''),
                       urllib.parse.quote_plus(value)})
    with _lock:
        # Keyed by the value, so the cap counts values rather than spellings.
        entries = _registered[label]
        if value in entries:
            entries.move_to_end(value)
            return
        entries[value] = forms
        limit = _LABEL_LIMITS.get(label, _PER_LABEL)
        while limit is not None and len(entries) > limit:
            entries.popitem(last=False)
        _pattern = None


def _compiled():
    global _pattern
    with _lock:
        if _pattern is None:
            labels = {}
            for label, entries in _registered.items():
                for forms in entries.values():
                    for form in forms:
                        labels[form] = label
            if not labels:
                _pattern = (None, {})
            else:
                # Longest first, so a value is never half-replaced by a
                # registered prefix of itself. A value made only of digits is
                # matched only where no digit adjoins it, so a registered
                # account number does not take the middle out of a longer
                # number that happens to contain it.
                parts = []
                for form in sorted(labels, key=len, reverse=True):
                    escaped = re.escape(form)
                    if form.isdigit():
                        escaped = r'(?<!\d)' + escaped + r'(?!\d)'
                    parts.append(escaped)
                _pattern = (re.compile('|'.join(parts)), labels)
        return _pattern


def redact(text):
    '''``text`` with every registered secret replaced.'''
    if not isinstance(text, str) or not text:
        return text
    # Each pattern runs only on a line that could match it, since nearly none
    # do and this is paid on every line written. A substring test per
    # registered spelling is several times faster than the alternation that
    # does the replacing.
    pattern, labels = _compiled()
    if pattern is not None and any(form in text for form in labels):
        text = pattern.sub(
                lambda m: '<REDACTED {}>'.format(labels[m.group(0)]), text)
    if '"' in text:
        text = _SECRET_JSON_VALUE.sub(r'\1<REDACTED>\2', text)
    if '=' in text:
        text = _SECRET_PARAMETER.sub(r'\1<REDACTED>', text)
    return text


def register_account_numbers(obj):
    '''Registers every full account number in a decoded response.

    Only the ``accountNumber`` key, compared case-insensitively. Hashes and
    other identifiers are left alone on purpose: an account's hash is what
    every request names it by, and masking it would leave a log unable to say
    which account a line is about.
    '''
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() == 'accountnumber':
                if isinstance(value, int) and not isinstance(value, bool):
                    value = str(value)
                register(value, 'ACCOUNT_NUMBER')
            else:
                register_account_numbers(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            register_account_numbers(value)


def register_stream_accounts(frame):
    '''Registers the account number every ACCT_ACTIVITY content item names.

    Field ``1`` is the account, as :class:`StreamClient.AccountActivityFields`
    labels it. Reads defensively: this runs on every frame, before anything
    has checked its shape, and must never be what ends a receive loop. Each
    entry and each item is read on its own, so one malformed item does not
    leave the accounts after it unregistered.
    '''
    try:
        entries = list(frame.get('data') or ())
    except Exception:
        return
    for entry in entries:
        try:
            if entry.get('service') != 'ACCT_ACTIVITY':
                continue
            items = list(entry.get('content') or ())
        except Exception:
            continue
        for item in items:
            try:
                value = item.get('1')
            except Exception:
                continue
            if isinstance(value, str):
                register(value, 'ACCOUNT_NUMBER')


#: How far past its bound a text is redacted before it is cut. Cutting first
#: can leave the head of a secret that straddles the cut, which matches
#: nothing; this is longer than any secret this library registers.
_WINDOW = 4096


def before_cut(text, bound):
    '''``text`` redacted, for a caller about to cut it to ``bound``.

    Every helper here that bounds a value for a message or a log line cuts
    it, and a secret straddling the cut leaves its head behind -- the same
    defect as websockets' elision. So the redaction runs first, over a window,
    and the cut comes after.

    The window has to survive the redaction shrinking it. A secret cut by the
    window's own edge is left raw at the end of what is redacted, so its head
    is inside the bound only if the redacted text is shorter than the bound
    plus the secret -- which a long ID token, replaced by a short label several
    times over, can make it. So the window doubles until the redacted text
    keeps ``_WINDOW`` characters past the bound, or holds all of ``text``.

    A plain ``str`` first: the text is often venue-controlled, and a subclass
    brings its own ``len`` and slicing to every step after.
    '''
    text = str.__str__(text)
    window = bound + _WINDOW
    while True:
        redacted = redact(text[:window])
        if len(text) <= window or len(redacted) >= bound + _WINDOW:
            return redacted
        window *= 2


def _redact_frame(frame):
    '''A copy of a websockets frame with its payload redacted.

    websockets formats a frame when the record is rendered, and elides a long
    one's middle as it does. Redacting the rendered line is too late: the cut
    can land inside a token, leaving a tail that is neither a registered value
    nor next to the key that names it. So the payload is redacted first, and
    the elision only ever cuts redacted text.

    An incoming frame is logged by websockets before this library has decoded
    it, so an account it names is registered here, from the frame itself.

    A fragment of a message is not logged at all. Neither half of a value
    split across two fragments matches it, and a fragment of JSON does not
    parse, so nothing it names can be registered from it. Whether Schwab ever
    fragments is not known; websockets reassembles either way.
    '''
    data = frame.data
    if not data:
        return frame
    if not frame.fin or getattr(frame.opcode, 'name', None) == 'CONT':
        return dataclasses.replace(frame, data='<fragment of {} bytes, not '
                                   'logged>'.format(len(data)).encode())
    try:
        text, encoding = bytes(data).decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        # Registered values are ASCII, and latin-1 maps every byte to one
        # character and back, so a binary frame keeps every byte not redacted.
        text, encoding = bytes(data).decode('latin-1'), 'latin-1'
    if 'ACCT_ACTIVITY' in text:
        try:
            register_stream_accounts(json.loads(text))
        except Exception:
            pass
    redacted = redact(text)
    if redacted == text:
        return frame
    return dataclasses.replace(frame, data=redacted.encode(encoding))


def _is_frame(value):
    cls = type(value)
    return (cls.__name__ == 'Frame'
            and cls.__module__.split('.', 1)[0] == 'websockets'
            and dataclasses.is_dataclass(value))


class _RedactedFrame:
    '''A websockets frame, redacted the first time anything renders it.

    Lazy because redacting decodes and searches the whole payload, on the
    event loop, and most frames websockets logs at DEBUG go to no handler
    that writes DEBUG. Deciding up front which ones will be written cannot be
    done safely: a filter or an error tracker can render a record that no
    handler's level accepts, and then sees whatever was not redacted. So
    nothing is decided; whoever renders it gets it redacted.
    '''

    __slots__ = ('_frame', '_text')

    def __init__(self, frame):
        self._frame = frame
        self._text = None

    def __str__(self):
        if self._text is None:
            self._text = str(_redact_frame(self._frame))
        return self._text

    __repr__ = __str__

    def __getattr__(self, name):
        # A caller's filter can read a frame's opcode or fin; its payload is
        # handed over redacted.
        if name == 'data':
            return _redact_frame(self._frame).data
        return getattr(self._frame, name)

    def __reduce__(self):
        # Pickled as its redacted text. A record sent to another process is
        # rendered there, against a registry that holds nothing.
        return (str, (str(self),))


class _RedactedMessage:
    '''A record's message and arguments, rendered and redacted when the
    message is first asked for.

    Lazy for the reason :class:`_RedactedFrame` gives: a record nothing
    renders costs nothing, and a ``LazyLog`` argument stays lazy, but any
    code that renders it -- a handler, a filter, an error tracker reading
    records directly -- gets the redacted text.
    '''

    __slots__ = ('_msg', '_args', '_text')

    def __init__(self, msg, args):
        self._msg = msg
        self._args = args
        self._text = None

    def __str__(self):
        # A failure to format is left to raise, into logging's own error
        # report, which names the call site. That report prints this
        # object's repr, which is redacted.
        if self._text is None:
            text = str(self._msg)
            if self._args:
                text = text % self._args
            self._text = redact(text)
        return self._text

    def __repr__(self):
        try:
            text = repr((self._msg, self._args))
        except Exception:
            text = '<a message that cannot be formatted>'
        return redact(text)

    def __reduce__(self):
        # Pickled as its redacted text, for the reason _RedactedFrame gives.
        # A handler that renders only a record with args before pickling it
        # would otherwise ship the raw message, or fail on a LazyLog.
        return (str, (str(self),))


def mask(obj):
    '''A copy of ``obj`` with the value of every secret-carrying key masked.

    Walks dicts, lists and tuples. Anything else is returned as it is, so a
    payload that is not JSON-shaped loses nothing but gains no masking either;
    the filter's registered-value redaction still applies to the line.
    '''
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if (isinstance(key, str) and key.lower() in SECRET_KEYS
                    and isinstance(value, str)):
                out[key] = '<REDACTED {}>'.format(key)
            else:
                out[key] = mask(value)
        return out
    if isinstance(obj, (list, tuple)):
        # A tuple subclass -- a namedtuple from a custom decoder -- cannot be
        # rebuilt from one iterable, so anything but a plain tuple is a list.
        masked = [mask(v) for v in obj]
        return tuple(masked) if type(obj) is tuple else masked
    return obj


class RedactingFilter(logging.Filter):
    '''Replaces registered secrets in a record, whatever renders it.

    The message and its arguments are taken into a :class:`_RedactedMessage`
    and the arguments cleared, so whatever renders the record later -- a
    handler, or anything else that reads records -- gets the message redacted
    and cannot reintroduce a secret from an argument. A traceback and a stack
    are rendered and redacted here: an exception's text can quote whatever it
    was handed, and a formatter needs them as text.
    '''

    def filter(self, record):
        if not isinstance(record.msg, _RedactedMessage):
            record.msg = _RedactedMessage(record.msg, record.args)
            record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(
                    record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
            # The exception itself is dropped once its text is redacted, as
            # QueueHandler does: a formatter that renders exc_info again --
            # a JSON one, an error tracker's -- would print it unredacted.
            # Every standard formatter prints exc_text instead.
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


class FrameRedactingAdapter(logging.LoggerAdapter):
    '''The logger ``websockets`` is handed: redacts each frame it is given.

    The frame is wrapped as websockets hands it over, before the record exists,
    so no filter or handler of the logger beneath -- a caller's own, in
    whatever order, built however -- and nothing else that renders the record
    can see it unredacted. A filter on that logger could not promise as much:
    a filter added before it can render the record, and a logger made with
    ``logging.Logger(name)`` is not the one its name finds.

    Anything else is passed through untouched. ``extra`` included: this
    adapter has none of its own, and ``log`` hands the logger beneath the
    keyword arguments it was given rather than calling ``process``.
    '''

    def log(self, level, msg, *args, **kwargs):
        if not self.isEnabledFor(level):
            return
        if args and any(_is_frame(a) for a in args):
            args = tuple(_RedactedFrame(a) if _is_frame(a) else a
                         for a in args)
        # One frame more than the call websockets made, so a record names
        # websockets' line rather than this one. Before 3.11 logging counted
        # the frames of an adapter beneath this one as well.
        extra = 2 if (sys.version_info < (3, 11) and isinstance(
                self.logger, logging.LoggerAdapter)) else 1
        kwargs['stacklevel'] = kwargs.get('stacklevel', 1) + extra
        self.logger.log(level, msg, *args, **kwargs)


_FILTER = RedactingFilter()


def protect(logger):
    '''Puts the filter on ``logger``, once and first, and returns it.

    First, because a caller's filter can render a record, and loggers are
    created when this library first logs on them -- so a ``dictConfig``
    naming one with a filter has put that filter there already.
    '''
    if _FILTER not in logger.filters:
        logger.filters.insert(0, _FILTER)
    return logger


#: Dependencies whose loggers carry the filter too. None was measured logging
#: a secret on its own, but httpx2 logs every request's URL at INFO, and a URL
#: naming an account by its number rather than its hash -- a caller's mistake
#: Schwab's own documentation invites -- carries that number.
DEPENDENCY_LOGGERS = ('httpx2', 'httpcore2', 'authlib')


def protect_dependencies():
    '''Protects every logger the dependencies have created so far.

    Loggers are created when their modules are imported, so this runs after
    the imports and again as each client is made: httpcore2 is imported when
    httpx2 builds its first transport, which is the session a client is made
    from, not at import. Its ``http2`` and ``socks`` loggers appear only when
    those are used, which this library never asks for.
    '''
    for name, logger in list(logging.root.manager.loggerDict.items()):
        if (isinstance(logger, logging.Logger)
                and name.split('.', 1)[0] in DEPENDENCY_LOGGERS):
            protect(logger)


def get_logger(name):
    '''``logging.getLogger(name)``, protected.'''
    return protect(logging.getLogger(name))


_WEBSOCKET_LOGGER = None


def websocket_logger(logger=None):
    '''The logger handed to ``websockets`` for the stream's connection.

    A caller's own ``logger``, or by default ``websockets.client``, the one
    websockets uses when given none -- so records come from the logger
    websockets would use anyway, and every configuration of it, its level,
    handlers, filters and ``disabled``, applies unchanged. Either way it is
    wrapped in :class:`FrameRedactingAdapter`, which is what makes the frames
    safe to log.
    websockets elides the middle of a long frame by default, which happens to
    hide the LOGIN token -- and at some sizes of its documented
    ``WEBSOCKETS_MAX_LOG_SIZE`` cuts inside the token instead, leaving its
    tail, which is why a frame is redacted before it is rendered.

    No filter is put on ``websockets.client``: it is websockets' logger, and
    every other client in the process logs on it. Its frames are where a
    secret is, and the adapter covers those.
    '''
    global _WEBSOCKET_LOGGER
    if logger is not None:
        return FrameRedactingAdapter(logger, {})
    if _WEBSOCKET_LOGGER is None:
        _WEBSOCKET_LOGGER = FrameRedactingAdapter(
                logging.getLogger('websockets.client'), {})
    return _WEBSOCKET_LOGGER
