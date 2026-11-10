from abc import ABC, abstractmethod
from collections import defaultdict, deque
from enum import Enum

import asyncio
import copy
import httpx2
import inspect
import json
import time

import websockets.asyncio.client as ws_client
from websockets.exceptions import ConnectionClosed

from . import _redact
from .utils import (EnumEnforcer, LazyLog, SchwabError, TokenErrorKind,
                    TokenRefreshError, _is_instance, _printable)


class StreamJsonDecoder(ABC):
    @abstractmethod
    def decode_json_string(self, raw):
        '''
        Parse a JSON-formatted string into a proper object. Raises
        ``JSONDecodeError`` on parse failure.
        '''
        raise NotImplementedError()


class NaiveJsonStreamDecoder(StreamJsonDecoder):
    '''The decoder a :class:`StreamClient` uses unless given another:
    ``json.loads``, and nothing more. See
    :meth:`StreamClient.set_json_decoder`.'''
    def decode_json_string(self, raw):
        return json.loads(raw)


def get_logger():
    return _redact.get_logger(__name__)



class _BaseFieldEnum(Enum):
    @classmethod
    def all_fields(cls):
        '''
        Every member of this field enum, in order: what a subscribe sends
        when no ``fields`` are given. Passed to one of the level one
        ``*_view`` methods it asks for every field again; whether that
        restores them is not established for the form those methods send.
        See :ref:`the note on VIEW <level_one_view>`.
        '''
        return list(cls)

    @classmethod
    def key_mapping(cls):
        try:
            return cls._key_mapping
        except AttributeError:
            # Iterate the enum rather than __members__, which also yields
            # aliases. An alias is a second spelling of a field, not a second
            # field, and it must not decide what the field is labeled.
            cls._key_mapping = dict(
                (str(enum.value), enum.name) for enum in cls)
            return cls._key_mapping

    @classmethod
    def relabel_message(cls, old_msg, new_msg):
        # Make a copy of the items so we can modify the dict during iteration
        for old_key, value in list(old_msg.items()):
            # Compared as text, because a key is not guaranteed to be one.
            # `StreamJsonDecoder` is a public extension point that parses the
            # whole frame, and a decoder normalising numeric field keys to
            # ints is a plausible thing to write. Keyed on the raw object,
            # such a message would go unrelabeled: `.isdigit()` below would
            # raise AttributeError on it, and the relabel guard would absorb
            # that and drop the whole message.
            try:
                # A plain str: a decoder's subclass could answer isdigit for
                # itself, and put a forged line in the warning below.
                text_key = str.__str__(str(old_key))
            except Exception:
                # A key is whatever the decoder produced, and `str()` of a
                # wide integer raises past sys.get_int_max_str_digits(). It
                # cannot be a field id, so leave it exactly where it is.
                continue
            if text_key in cls.key_mapping():
                new_key = cls.key_mapping()[text_key]
                new_msg[new_key] = new_msg.pop(old_key)
            elif text_key.isdigit():
                # A numeric key this table does not have is a field Schwab
                # added. It is delivered verbatim either way -- that is
                # deliberate, and it is why a new field reaches a handler
                # instead of breaking one -- but silently, so nobody learns
                # the field exists. Said once, here.
                #
                # One consequence of matching on text: a content item
                # carrying both `1` and `'1'` has two spellings of one id,
                # and the second relabel overwrites the first. That needs a
                # custom decoder emitting both forms in one item; the
                # alternative would be dropping such a message entirely.
                #
                # `isdigit` is the discriminator because Schwab's field ids
                # are numeric strings and the other keys in a content item
                # are not: `key`, `seq`, `delayed` and `assetMainType` all
                # arrive alongside the numbered fields and none of them is a
                # field. Keying on "not in the table" alone would report all
                # four on every message.
                #
                # This runs on the hot path, so it sits in the `else` of a
                # test the loop already performs: only a key that missed the
                # table is examined, and in a normal message that is the four
                # names above.
                _report_unknown_field(cls, text_key[:64])


#: Every service this version knows how to route, and ``ADMIN``, which is
#: request/response only and never reaches a handler. A name outside this set
#: is something Schwab added, and the client has nowhere to put the message.
#:
#: Held as a declared set with a test that it matches the subscribe methods
#: and the handler registrations, rather than derived from either: two walks
#: over the same ground disagree, and the one that decides what a service *is*
#: should be a single place a reader can look at.
_KNOWN_SERVICES = frozenset((
    'ACCT_ACTIVITY',
    'ADMIN',
    'CHART_EQUITY',
    'CHART_FUTURES',
    'LEVELONE_EQUITIES',
    'LEVELONE_FOREX',
    'LEVELONE_FUTURES',
    'LEVELONE_FUTURES_OPTIONS',
    'LEVELONE_OPTIONS',
    'NASDAQ_BOOK',
    'NEWS_HEADLINE',
    'NYSE_BOOK',
    'OPTIONS_BOOK',
    'SCREENER_EQUITY',
    'SCREENER_OPTION',
))

#: What Schwab has sent in place of a service name: an error label, not a
#: service. Seen once, in the second before Schwab closed the connection, and
#: documented nowhere. Matched exactly, since a real service could one day
#: have "invalid" in its name.
_INVALID_SERVICE = 'Invalid Service'

#: The compartments a frame is made of. Anything else at the top level is a
#: channel this version does not read, which means a whole class of message
#: is being dropped rather than one field being unnamed.
_KNOWN_CHANNELS = frozenset(('response', 'data', 'notify'))

#: Response codes that mean success. Schwab's streamer documentation lists 0
#: and also 26-29, SUCCEEDED_COMMAND_SUBS, _UNSUBS, _ADD and _VIEW. Every
#: success observed against a live account, VIEW included, has been 0; the
#: others are accepted so a documented success cannot surface as a failure.
#:
#: A success code is accepted whatever command it answers, so a 29
#: (SUCCEEDED_COMMAND_VIEW) would pass as the answer to a SUBS. The service and
#: command echoes are checked before the code, and matching codes to commands
#: would assert a pairing nobody has observed, where the cost of the leniency
#: is accepting a documented success.
#:
#: A tuple rather than a set. `in` on a tuple compares with ==, so a code of
#: an unhashable type -- the venue's to choose -- is judged rather than raising
#: TypeError.
_SUCCESS_CODES = (0, 26, 27, 28, 29)


def _is_success_code(code):
    """Whether a response code means success. The one place that decides it:
    the request path and both late-response paths ask here, so a code cannot
    succeed on one path and be reported as a rejection on another."""
    return code in _SUCCESS_CODES


#: How many distinct (field table, field id) pairs to name before giving up.
#: The set never shrinks and what goes into it is the venue's to choose.
_MAX_REPORTED_FIELDS = 64

_reported_fields = set()

#: Services and channels count separately from fields, and against their own
#: budget. Sharing one would let a category that cannot starve itself starve
#: the other: 254 declared field ids across 14 tables against 64 slots, so
#: one frame of unknown ids could leave a new service unnamed, with the
#: *lesser* event having consumed the whole budget.
#:
#: Held per client rather than module-wide, unlike `_reported_fields`. The
#: field report is log-only, so process-level dedup is right for it -- the
#: log is process-wide. This one drives `add_error_handler`, which is per
#: client, so a second client in the same process has its own handlers and
#: must hear about a dropped service for itself. With process-wide dedup, a
#: second client would hear about only some of the names (measured: three of
#: five with one frame per name, two of five with four), because what leaks
#: through is `_absorb`'s per-kind coalescing rather than anything about the
#: names.
_MAX_REPORTED_SHAPES = 64


def _safe_str(value):
    """`str` of a value whose type name is already being printed.

    `_safe_value` reprs, which is right where the value stands alone and
    wrong beside an explicit `type(x).__name__` -- that would read
    `Cause: TypeError: TypeError("...")`, degrading the line this module
    calls the complete record.
    """
    try:
        text = str(value)
    except Exception:
        text = '<unprintable>'
    text = str.__str__(text)   # a plain str; `_safe_value` says why
    # An exception's own message can quote venue text, line breaks and all.
    # Redacted before it is cut, so a secret across the cut leaves nothing.
    text = _redact.before_cut(text, 200)
    return _printable(text if len(text) <= 200 else text[:197] + '...', 200)


def _type_name(value):
    """A value's type name, for a line about a value that could not be read.

    Read through `type`'s own descriptor and reduced to a plain str.
    `type(x).__name__` consults the metaclass first, which can raise inside
    the very branch written not to.
    """
    try:
        name = str.__str__(type.__dict__['__name__'].__get__(type(value)))
    except Exception:
        name = 'object'
    return _printable(name if len(name) <= 64 else name[:61] + '...', 64)


def _safe_value(value):
    """`repr` of a venue-controlled value, bounded and unable to raise.

    Pre-formatted rather than handed to `%r`, because `%r` itself raises on
    an integer past `sys.get_int_max_str_digits()` -- and a logging failure
    is swallowed, so the *content* of the line is lost rather than the line.
    Quoted, because a bare `%s` on a name containing a newline forges a
    second line indistinguishable from a real one; that is reachable with
    ordinary JSON, no custom decoder required.
    """
    try:
        text = repr(value)
    except Exception:
        text = '<{} that cannot be formatted>'.format(
                _type_name(value))
    # A plain `str`: `repr` hands back a subclass untouched when `__repr__`
    # returns one. Measured on an absorbed frame from a custom decoder: a
    # raising `__len__` ended the receive loop with nothing logged, and one
    # returning 0 logged the whole frame.
    text = str.__str__(text)
    # Redacted before it is cut, so a secret across the cut leaves nothing.
    text = _redact.before_cut(text, 200)
    return _printable(text if len(text) <= 200 else text[:197] + '...', 200)


def _safe_name(value):
    """A key's name, bounded and guaranteed not to raise.

    `str()` of a wide integer raises past `sys.get_int_max_str_digits()`, and
    a top-level frame key is whatever the decoder produced --
    `StreamJsonDecoder` is a public extension point, which is also why
    `relabel_message` handles a non-string key.
    """
    try:
        text = str(value)
    except Exception:
        text = '<{} that cannot be named>'.format(_type_name(value))
    # A plain `str`: `str()` hands back a subclass untouched when `__str__`
    # returns one. Measured on a `MESSAGE_TYPE`: one returning itself kept its
    # own `casefold`, which raised and absorbed the whole element, and its own
    # `__len__`, which reported 0 and kept a megabyte in the report set.
    text = _redact.before_cut(str.__str__(text), 64)
    return text if len(text) <= 64 else text[:61] + '...'


def _report_unknown_field(field_enum_type, field_id):
    """Say once that a stream carried a field this version does not know.

    Not an error and not reported through :meth:`add_error_handler`: nothing
    was absorbed, nothing failed, and the message reached its handler intact
    with the unrecognised id still on it. It is a schema change, which is an
    operator's concern rather than a caller's, so it goes to the log.

    Once per table and id rather than once per message: this fires on a live
    feed, where per-message is a flood and a flood is its own way of hiding
    the message.
    """
    # Bounded in length as well as in count: `_reported_fields` never
    # shrinks, so a million-digit id -- which passes `isdigit` -- would be
    # retained forever and named in full in a million-character log line.
    seen = (field_enum_type.__name__, field_id)
    if (seen in _reported_fields
            or len(_reported_fields) >= _MAX_REPORTED_FIELDS):
        return
    _reported_fields.add(seen)
    if len(_reported_fields) >= _MAX_REPORTED_FIELDS:
        # Said out loud, like the shapes cap. Going quiet at a cap is
        # indistinguishable from nothing new arriving, which is the one
        # reading an operator must not be left to make.
        get_logger().warning(
                'That is %d distinct unknown field ids, which is as many as '
                'schwaby will name. Any further ones are delivered the same '
                'way and are not reported.', _MAX_REPORTED_FIELDS)
    get_logger().warning(
            'Schwab sent field %s on a %s message, which this version of '
            'schwaby has no name for. The field is delivered to your handler '
            'under its numeric key and everything else is relabeled as '
            'usual; this is reported once per field, not per message. If you '
            'see this, please open an issue at '
            'https://github.com/tom-hirt/schwaby/issues with the value -- '
            'the field tables are only as current as the last time someone '
            'looked.',
            field_id, field_enum_type.__name__)


#: How many distinct unobserved ``ACCT_ACTIVITY`` types to name before giving
#: up. The set never shrinks and what goes into it is the venue's to choose.
_MAX_REPORTED_MESSAGE_TYPES = 64

_reported_message_types = set()


def _is_notice_text(message_data):
    """Whether ``MESSAGE_DATA`` is a notice written as text, read the way
    ``parse_message_data`` reads it, so that the two cannot disagree about
    what a notice is. Anything that cannot be read that way is not one."""
    try:
        # contrib.util imports this module, so it is imported here rather than
        # at the top, and inside the try: a notice that cannot be checked is
        # reported, not dropped with the item it arrived in.
        from .contrib.util import parse_message_data
        return issubclass(type(parse_message_data(message_data)), str)
    except Exception:
        return False


def _report_unknown_message_type(name):
    """Say once that ACCT_ACTIVITY carried a type nobody has captured.

    ``name`` has already been through `_safe_name`, so it is a bounded string
    that cannot raise.

    Log-only, like an unknown field and for the same reason: the message was
    delivered, nothing failed, and the news is about the venue rather than the
    caller's code. Not through `add_error_handler`, which is for messages this
    client could not use.

    Once per type, compared case-insensitively, and once per process because
    the log is process-wide. Expiry and partial fill have never been captured,
    so a process may see each of those once; a line per message would bury the
    one that names the missing type.
    """
    # Folded without cutting again. `name` is already at most 64 characters
    # and folding at most triples one, so what is kept stays bounded -- and
    # cutting after folding would make two different types, 'ß' * 33 + 'A'
    # and 'ß' * 33 + 'B', share one report.
    folded = name.casefold()
    if (folded in _reported_message_types
            or len(_reported_message_types) >= _MAX_REPORTED_MESSAGE_TYPES):
        return
    _reported_message_types.add(folded)
    if len(_reported_message_types) >= _MAX_REPORTED_MESSAGE_TYPES:
        get_logger().warning(
                'That is %d distinct unobserved ACCT_ACTIVITY message types, '
                'which is as many as schwaby will name. Any further ones are '
                'delivered the same way and are not reported.',
                _MAX_REPORTED_MESSAGE_TYPES)
    get_logger().warning(
            'ACCT_ACTIVITY carried message type %r, which is not among the '
            'types anyone has captured. The message is not refused for that; '
            'this is reported once per type, not per message. Classify these '
            'messages by substring rather than by exact type, and do not read '
            'a CANCEL or UROUT as necessarily ending the order: on the '
            'option orders captured, a price change retired the old order id '
            'the same way. If you see this, please open an issue at '
            'https://github.com/tom-hirt/schwaby/issues with the type -- '
            'Schwab documents this vocabulary nowhere, and a report is how '
            'the list of observed types grows.',
            name)


class UnexpectedResponse(SchwabError):
    '''A response that does not answer the request it arrived for.

    Raised by a request -- a login, subscribe or logout -- when its answer
    names a service or command other than the one sent, carries a request id
    higher than any this client has issued, or has a request id but cannot
    otherwise be read.
    ``response`` holds the frame, and the message says which. An answer to an
    *earlier* request is not this: it is set aside as a late answer, and one
    with no readable request id at all leaves the request to time out. Compare
    :class:`UnexpectedResponseCode`, which is a well-formed answer that
    refused.
    '''
    def __init__(self, response, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.response = response


class UnexpectedResponseCode(SchwabError):
    '''Schwab refused a request.

    Raised when the answer to a request carries a code outside the ones Schwab
    documents as success, and passed to
    :meth:`StreamClient.add_error_handler` instead when the refusal answers a
    request that had already been abandoned, since nothing is left waiting to
    raise it to. ``response`` holds the frame, and the message carries
    Schwab's code and its own text, which is usually the part worth reading
    -- a screener subscription past its key limit, for one, says so there.
    '''
    def __init__(self, response, *args, service=None, command=None,
                 code=None, msg=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.response = response
        #: The refused request's service and command, as Schwab named them
        #: in the element that refused it, or None.
        self.service = _text_or_none(service)
        self.command = _text_or_none(command)
        #: Schwab's code, an int, or None when the frame's is not one.
        self.code = _int_or_none(code)
        #: Schwab's message, a str, or None.
        self.msg = _text_or_none(msg)


class StreamLoginRefused(UnexpectedResponseCode):
    '''Schwab refused the stream's LOGIN: any reply to it other than
    success, its ``code`` saying which. The socket is closed before it is
    raised. Code 3 is the one documented ("Login Denied ... token is invalid
    or has expired").'''


class StreamNotice(SchwabError):
    '''A notice from Schwab's streaming server about the connection itself
    rather than any data: one on the ``ADMIN`` service, or carrying a code
    Schwab documents as severing the stream, 12 ("a limit of 1 Streamer
    connection at any given time from a given user") or 30 (stopped
    streaming). It reaches the error handlers, and when the connection then
    closes it is the :class:`StreamClose`'s ``notice``.'''

    def __init__(self, frame, *args, code=None, msg=None, **kwargs):
        super().__init__(*args, **kwargs)
        #: The element that carried it, as Schwab sent it.
        self.frame = frame
        #: Schwab's code, an int, or None.
        self.code = _int_or_none(code)
        #: Schwab's message, a str, or None.
        self.msg = _text_or_none(msg)


class StreamCloseKind(str, Enum):
    '''How a stream ended, as :class:`StreamClose`'s ``kind`` says it. A
    ``str``, whose words do not change.'''

    __str__ = str.__str__
    __format__ = str.__format__

    #: A code-12 notice came first: another login for the same Schwab user
    #: took the one stream a user may have.
    BUMPED = 'bumped'
    #: Schwab refused the LOGIN.
    LOGIN_REFUSED = 'login_refused'
    #: The login's preferences call found the refresh token dead.
    DEAD_TOKEN = 'dead_token'
    #: A code-30 notice came first: Schwab stopped streaming.
    STOPPED = 'stopped'
    #: Schwab closed the connection cleanly (1000) with no notice first.
    #: **A bump has been seen to look like this**: whether Schwab sends a
    #: notice before one is unmeasured.
    SERVER_CLOSED = 'server_closed'
    #: Anything else: an abnormal close, a ping unanswered, a login that
    #: failed before a socket existed for a reason other than a dead token.
    CONNECTION_LOST = 'connection_lost'


class StreamClose:
    '''How and when a stream ended: ``StreamClient.last_close``, and what
    each close handler is called with.'''

    __slots__ = ('kind', 'close_code', 'close_reason', 'notice', 'at')

    def __init__(self, kind, close_code=None, close_reason=None, notice=None,
                 at=None):
        #: A :class:`StreamCloseKind`.
        self.kind = StreamCloseKind(kind)
        #: The websocket close code, or None when no close frame arrived.
        self.close_code = _int_or_none(close_code)
        #: The websocket close reason, or None.
        self.close_reason = _text_or_none(close_reason)
        #: The :class:`StreamNotice` that came before the close, or None.
        self.notice = notice
        #: ``time.time()`` when the close was found.
        self.at = time.time() if at is None else at

    def __repr__(self):
        return ('StreamClose(kind={!r}, close_code={!r}, close_reason={!r}, '
                'notice={})'.format(
                    str(self.kind), self.close_code,
                    _safe_value(self.close_reason),
                    'None' if self.notice is None else self.notice.code))


def _int_or_none(value):
    return (value if isinstance(value, int) and not isinstance(value, bool)
            else None)


def _text_or_none(value):
    return value if isinstance(value, str) else None


#: The codes Schwab documents as severing the stream, which a notice carries.
_SEVERING_CODES = frozenset({12, 30})


class UnparsableMessage(SchwabError):
    '''A frame whose JSON did not decode.

    **This one still ends the caller's receive loop**, unlike
    :class:`UnusableMessage`, which is absorbed. Both are reported to
    ``add_error_handler``; only this one is also raised.

    The difference is what is known about what was missed. A structurally
    unusable element can be skipped precisely: the elements beside it are still
    delivered, and the caller is told which one went. A frame which will not
    parse has unknown contents -- it might have carried a fill -- so continuing
    means silently accepting a gap of unknown size. Tearing the stream down
    causes a reconnect and a re-subscribe, which is the one thing that can
    recover state; skipping cannot.

    That is a deliberate choice rather than an oversight, but the argument is
    not one-sided.
    ``schwaby.contrib.util.HeuristicJsonDecoder`` exists because Schwab really
    does emit JSON this library cannot parse, which is evidence for a
    frame-level quirk rather than a dead stream. If your feed hits this often
    and a reconnect is worse for you than a gap, set that decoder before
    concluding the stream is broken.

    ``raw_msg`` is what arrived, and reaches ``add_error_handler`` as the
    ``message`` argument. It can be the empty string --- an empty text frame
    gives ``json.loads`` nothing to parse --- so such a report is
    distinguishable from the logout-close failure under ``is None`` but *not*
    under a falsy test. Discriminate on the exception type.
    '''
    def __init__(self, raw_msg, json_parse_exception, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.raw_msg = raw_msg
        self.json_parse_exception = json_parse_exception


class UnusableMessage(SchwabError):
    '''A message which decoded successfully but which this client cannot use.

    Distinct from :class:`UnparsableMessage`, which means the JSON itself did
    not decode and carries the parse exception. Here the JSON is fine and
    this client cannot use the message --- either its structure is not what
    the protocol allows, or Schwab added something this version does not
    know. :meth:`add_error_handler
    <schwaby.streaming.StreamClient.add_error_handler>` enumerates the
    categories and this docstring deliberately does not, so there is only one
    list to keep current.

    Reusing ``UnparsableMessage`` would make one type mean two shapes, with
    ``json_parse_exception`` set for one of them and ``None`` for the
    other.

    ``message`` is the offending value, exactly as it arrived --- except for
    an unread channel, where it is the sorted list of channel names, since
    the offending thing is which compartments appeared rather than any one
    value inside them.

    ``cause`` is the exception which made it unusable, where there was one --
    a relabeling failure carries the ``KeyError`` from the field tables, which
    is the only thing that says *which* field moved. ``None`` where the message
    was rejected on inspection rather than by failing.

    ``count`` and ``total`` are how many of this kind, and how many in all,
    have been absorbed on this connection. These are suppressed after the first
    few, so the numbers are the only way to see the true scale -- as integers
    rather than only in the message text, since a consumer alerting on drop
    volume should not have to parse prose.
    '''
    def __init__(self, message, *args, cause=None, count=None, total=None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.message = message
        self.cause = cause
        self.count = count
        self.total = total


class _Handler:
    def __init__(self, func, field_enum_type):
        self._func = func
        self._field_enum_type = field_enum_type

    def __call__(self, *args, **kwargs):
        return self._func(*args, **kwargs)

    def label_message(self, msg):
        if 'content' in msg:
            new_msg = copy.deepcopy(msg)
            for idx in range(len(msg['content'])):
                self._field_enum_type.relabel_message(msg['content'][idx],
                                                      new_msg['content'][idx])
            return new_msg
        else:
            return msg


# Kwargs which websockets 14.0 renamed when it introduced the asyncio
# implementation, mapped to their current names.
_RENAMED_CONNECT_ARGS = {
    'extra_headers': 'additional_headers',
}

# Kwargs which websockets 14.0 removed outright, with no equivalent in the
# asyncio implementation.
_REMOVED_CONNECT_ARGS = ('create_protocol', 'read_limit')


def _prepare_connect_args(websocket_connect_args):
    '''
    Checks user-provided ``websocket_connect_args`` against the ``websockets``
    asyncio client, and returns a copy for this library to add to.

    ``StreamClient.login`` documents ``websocket_connect_args`` as a
    passthrough to ``connect()``, so a caller may still be passing a name which
    was valid before websockets 14.0. Both the renamed and the removed names
    are refused here with a message naming the replacement, rather than being
    left to surface as an opaque ``TypeError`` from inside ``websockets``.

    They are refused rather than translated. Translating a rename would keep
    working code working, but the passthrough this argument promises would
    not be a passthrough: the name the caller wrote would not be the name
    ``websockets`` received, which is a confusing thing for a library to do to
    an argument documented as going straight through.

    The copy matters -- ``login`` adds ``ssl`` to what it gets back, and the
    dict belongs to the caller.

    It also adds ``logger``. websockets logs every frame at DEBUG, the LOGIN
    request and its access token included, and only its default elision of a
    long frame's middle hides the token -- which its own
    ``WEBSOCKETS_MAX_LOG_SIZE`` setting turns off. So it is handed a logger
    carrying this library's redaction. A caller's own ``logger``, or adapter,
    is wrapped in an adapter that redacts each frame before the logger
    beneath sees it, and passes everything else through untouched.
    '''
    for old_name, new_name in _RENAMED_CONNECT_ARGS.items():
        if old_name not in websocket_connect_args:
            continue

        # Both names is the likelier half-migration: the new key was added and
        # the old one left behind. Telling that caller to "pass
        # additional_headers instead" names what they are already passing.
        if new_name in websocket_connect_args:
            raise ValueError(
                'websocket_connect_args contains both {!r} and {!r}. {!r} is '
                'the name websockets has used since 14.0; please drop '
                '{!r}.'.format(old_name, new_name, new_name, old_name))

        raise ValueError(
            'websocket_connect_args[{!r}] was renamed to {!r} in '
            'websockets 14.0. Please pass {!r} instead.'.format(
                old_name, new_name, new_name))

    for removed in _REMOVED_CONNECT_ARGS:
        if removed in websocket_connect_args:
            raise ValueError(
                'websocket_connect_args[{!r}] was removed in websockets 14.0 '
                'and has no equivalent in the asyncio implementation. Please '
                'remove it.'.format(removed))

    args = dict(websocket_connect_args)
    args['logger'] = _redact.websocket_logger(args.get('logger'))
    return args


class ResponseTimeoutError(SchwabError):
    '''
    Raised when the streaming server accepts a request but does not send a
    response to it within the configured timeout. Distinct from the connection
    failing, which surfaces as a ``websockets`` exception.
    '''
    def __init__(self, service, command, timeout, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.service = service
        self.command = command
        self.timeout = timeout


# Returned by _read_and_route for a frame it delivered to a waiting request.
# A plain None cannot serve: a top-level JSON null is a frame a server can
# send, and treating it as "already routed" would drop it silently.
ROUTED = object()


def _checked_keys(symbols):
    """The keys of a SUBS, ADD or UNSUBS, as a list: refused before anything
    is sent when there are none, or when they are one string, which would be
    sent a letter at a time."""
    if isinstance(symbols, (str, bytes)):
        raise TypeError('symbols must be a list or tuple of keys, not one '
                        'string: {!r} would be sent a letter at a '
                        'time'.format(symbols))
    try:
        symbols = list(symbols)
    except TypeError:
        raise TypeError('symbols must be a list or tuple of keys, got '
                        '{}'.format(_type_name(symbols))) from None
    if not symbols:
        # Measured: Schwab refuses an empty NEWS_HEADLINE SUBS with code 22.
        raise ValueError('symbols is empty: there is nothing to subscribe '
                         'to, add or remove')
    return symbols


def _checked_batch_size(size):
    if size is None:
        return None
    if isinstance(size, bool) or not isinstance(size, int):
        raise TypeError('keys_per_request must be an int or None, got '
                        '{}'.format(_type_name(size)))
    if size < 1:
        raise ValueError('keys_per_request must be one or more, got '
                         '{}'.format(size))
    return size


def _carries_a_heartbeat(frame):
    try:
        notify = frame.get('notify') if _is_mapping(frame) else None
        return isinstance(notify, list) and any(
                _is_mapping(d) and 'heartbeat' in d for d in notify)
    except Exception:
        return False


def _is_mapping(obj):
    '''Whether ``obj`` can be read the way a decoded JSON object is read.

    Structural, not ``isinstance(obj, Mapping)``. ``set_json_decoder`` is a
    public hook promising only "the decoded JSON", and the reads that follow
    it -- ``'response' in frame``, ``msg.get('data')`` -- work on anything
    providing them. An ABC matches only real subclasses and explicitly
    registered types, so a lightweight mapping-like object that those reads
    accept would have every frame dropped.
    '''
    return hasattr(obj, 'get') and hasattr(obj, '__contains__')


def _is_power_of_ten(n):
    while n >= 10 and n % 10 == 0:
        n //= 10
    return n == 1


def _is_sequence(obj):
    '''Whether ``obj`` can be used the way a decoded JSON array is.

    Indexable, not merely iterable. Routing reads ``frame['response'][0]`` and
    validation reads it again, and handlers are given the frame afterwards, so
    a single-pass iterable cannot serve however tolerant the iteration is: a
    decoder returning generators would make every response frame fall into
    the "no usable request id" branch and every subscribe time out. Tuples
    work, which is the realistic custom-decoder case; generators cannot.

    Strings and bytes are indexable and are not arrays; a mapping is iterable
    and yields its keys, so ``{'notify': {...}}`` would otherwise reach
    handlers as a message per key.
    '''
    return (hasattr(obj, '__getitem__')
            and hasattr(obj, '__len__')
            and not isinstance(obj, (str, bytes, bytearray))
            and not _is_mapping(obj))


class StreamClient(EnumEnforcer):
    '''
    A connection to Schwab's streaming API. Log in with :meth:`login`,
    subscribe, register handlers, then call :meth:`handle_message` in a loop.

    Schwab allows one streaming connection per user, so logging in here
    disconnects any other stream on the same login, wherever it is running.
    Streaming for a different brokerage account does not avoid it. See
    :ref:`the warning on this <one_stream_per_user>`.

    :param client: an authenticated :class:`~schwaby.client.Client` or
                   :class:`~schwaby.client.AsyncClient`. The stream uses it at
                   login, to fetch the streamer's address and credentials, and
                   authenticates with its access token.
    :param enforce_enums: refuse field arguments that are not members of the
                          matching field enum. On by default.
    :param ssl_context: an :class:`ssl.SSLContext` for the websocket
                        connection, if the default will not do.
    :param response_timeout: seconds to wait for the server to answer a
                             request before raising
                             :class:`ResponseTimeoutError`. ``None`` waits
                             indefinitely.
    :param keys_per_request: the most keys one SUBS, ADD or UNSUBS sends, or
                             ``None`` (the default) for all of them in one.
                             With a number, more keys go in batches, each
                             awaited in turn, and a SUBS's batches after the
                             first are sent as ADDs. A refusal part-way raises
                             with ``sent_keys`` and ``unsent_keys`` on the
                             error: the batches before it stand. Each
                             subscribe method takes ``keys_per_request`` too,
                             which overrides this. Schwab documents no limit
                             per request; the limits measured are totals per
                             service.
    '''

    #: Seconds to wait for the streaming server to respond to a request before
    #: giving up. A request which is never answered would otherwise wait
    #: forever, and only one request is outstanding at a time, so it would also
    #: block every subsequent one.
    DEFAULT_RESPONSE_TIMEOUT = 60.0

    def __init__(self, client, *,
                 enforce_enums=True, ssl_context=None,
                 response_timeout=DEFAULT_RESPONSE_TIMEOUT,
                 keys_per_request=None):
        super().__init__(enforce_enums)

        self._keys_per_request = _checked_batch_size(keys_per_request)

        self._ssl_context = ssl_context
        self._client = client

        #: How the last connection ended, a :class:`StreamClose`, or None
        #: while it has not, and before the first. Set before the call that
        #: finds the close raises, and reset by :meth:`login`.
        self.last_close = None
        #: ``time.monotonic()`` when the last frame of any kind arrived, one
        #: that did not parse included, or None. A plain attribute, safe to
        #: read from any thread.
        self.last_frame_at = None
        #: ``time.monotonic()`` when the last heartbeat arrived, or None.
        self.last_heartbeat_at = None
        self._close_handlers = []
        self._heartbeat_handlers = []
        self._close_found = False
        self._close_due = False
        self._last_notice = None
        self._response_timeout = response_timeout

        # Set by the login() function
        self._account = None
        self._stream_correl_id = None
        self._stream_customer_id = None
        self._stream_channel = None
        self._stream_function_id = None
        self._socket = None

        # Internal fields
        self._request_id = 0
        self._handlers = defaultdict(list)

        # Callbacks for failures this client absorbs rather than raising. See
        # add_error_handler.
        self._error_handlers = []

        # Tasks for handlers which turned out to be coroutines. The event loop
        # only holds a weak reference to a running task, so a task which nothing
        # else refers to can be garbage collected before it finishes. Hold onto
        # them until they complete.
        self._handler_tasks = set()

        # When listening for responses, we sometimes encounter non-response
        # messages. Since this happens outside the context of the handler
        # dispatcher, we cannot handle these messages. However, we still need to
        # deliver these messages. This list records the messages that were read
        # from the stream but not handled yet. Messages should be read from this
        # list before they are read from the stream.
        self._overflow_items = deque()

        # Rejections found riding along in a frame which answered an
        # outstanding request, waiting to be reported. They are found while the
        # read lock is held -- on the request path, the request lock and the
        # response deadline too -- which is no place to run a user handler, so
        # they are set aside and reported from handle_message the way an
        # overflow frame is. Bounded because nothing guarantees handle_message
        # is ever called again; the log line written when each is found is the
        # durable record, and the callback is the convenience, so dropping the
        # oldest loses nothing that was not already written down.
        self._pending_reports = deque(maxlen=64)

        # How many messages this connection could not use, in total and by
        # kind. The count is per kind so that a flood of one does not silence
        # a different one; the total keeps the log line honest about scale.
        self._absorbed = 0
        self._absorbed_kinds = defaultdict(int)
        self._reported_shapes = set()

        # Logging-related fields
        self.logger = get_logger()
        self.request_number = 0

        # Initialize the JSON parser to be the naive parser which directly calls
        # ``json.loads``
        self.json_decoder = NaiveJsonStreamDecoder()

        # Guarantees a single reader. websockets raises if two coroutines call
        # recv() concurrently, so exactly one coroutine may be inside a read at
        # a time -- but it is not held while waiting for a response, so a
        # request does not block message handling.
        self._read_lock = asyncio.Lock()

        # Keeps one request outstanding at a time, so a response can be matched
        # against the request it answers without ambiguity.
        self._request_lock = asyncio.Lock()

        # (request_id, service, command, future) for the request currently
        # awaiting a response, or None. Whoever is reading routes the matching
        # response here rather than the reader having to be the requester.
        self._pending_request = None

    def set_json_decoder(self, json_decoder):
        '''
        Sets a custom JSON decoder.

        :param json_decoder: Custom JSON decoder to use for to decode all
                             incoming JSON strings. See
                             :class:`StreamJsonDecoder` for details.
        '''
        # The local name, not schwaby.contrib.util's. They are the same class
        # -- contrib.util imports it from here -- but reaching it through the
        # package attribute raises AttributeError unless the caller happens to
        # have imported schwaby.contrib.util, which someone subclassing the
        # class where it is actually defined has no reason to have done.
        if not _is_instance(json_decoder, StreamJsonDecoder):
            raise ValueError('Custom JSON parser must be a subclass of ' +
                             'schwaby.contrib.util.StreamJsonDecoder')
        self.json_decoder = json_decoder

    @staticmethod
    def _pretty(obj):
        """json.dumps for a debug line, falling back to repr.

        set_json_decoder is a public hook promising only "the decoded JSON", so
        a decoder returning a Mapping which is not a dict, or tuples for
        arrays, produces something json.dumps refuses.

        This is cosmetic, not a crash fix: logging swallows a formatting
        failure and prints a "--- Logging error ---" block to stderr, so the
        stream survives either way. What it costs without this is the content
        of every debug line replaced by a traceback, for exactly the people who
        customised the decoder and are most likely to be debugging it.

        The exception does not end the receive loop. Note that a test run can
        suggest otherwise, because pytest's capture handler re-raises where
        logging would have swallowed it.
        """
        try:
            return json.dumps(obj, indent=4)
        except Exception:
            # json.dumps names a value it cannot serialise through __class__,
            # which can raise anything, and recurses on deep nesting.
            try:
                return repr(obj)
            except Exception:
                # The fallback needs one of its own. `repr` of a dict holding
                # a wide integer key raises past the digit limit, which is
                # the same hazard `_safe_repr` and `_safe_name` exist for --
                # and here it defeats the whole point of this function, which
                # is that a formatting failure stays cosmetic. In production
                # logging swallows it and prints a "--- Logging error ---"
                # block for every debug line; under pytest's capture handler
                # it re-raises.
                return '<{} that cannot be formatted>'.format(
                        _type_name(obj))

    def req_num(self):
        self.request_number += 1
        return self.request_number

    async def _send(self, obj):
        if self._socket is None:
            raise ValueError(
                'Socket not open. Did you forget to call login()?')

        # Masked by key as well as by registered value: the LOGIN request
        # carries the access token, and this line is how it reached the logs.
        self.logger.debug('Send %s: Sending %s',
                self.req_num(),
                LazyLog(lambda: self._pretty(_redact.mask(obj))))

        try:
            await self._socket.send(json.dumps(obj))
        except ConnectionClosed as exc:
            self._found_close(exc)
            raise

    async def _receive_from_socket(self):
        if self._socket is None:
            raise ValueError(
                'Socket not open. Did you forget to call login()?')

        try:
            raw = await self._socket.recv()
        except ConnectionClosed as exc:
            self._found_close(exc)
            raise
        # Any frame counts, one that will not parse included: the socket
        # is alive.
        self.last_frame_at = time.monotonic()
        try:
            ret = self.json_decoder.decode_json_string(raw)
        except (ValueError, RecursionError) as e:
            # ValueError covers JSONDecodeError and the UnicodeDecodeError of a
            # binary frame that is not UTF-8; a frame nested past the
            # interpreter's depth raises RecursionError. Each is a frame that
            # will not parse, and each is reported.
            # The text is quoted and cut: the frame is whatever arrived, line
            # breaks and megabytes included, and raw_msg keeps all of it.
            msg = ('Failed to parse message. This often happens with ' +
                   'unknown symbols or other error conditions, or a custom ' +
                   'decoder raising ValueError. Message text, quoted and ' +
                   'cut: ' + _safe_value(raw))
            raise UnparsableMessage(raw, e, msg)

        # An account activity frame carries the full account number, in the
        # field this library labels ACCOUNT. Registered before the frame is
        # logged, so this line carries it masked, and so does the copy inside
        # the message data, which repeats it.
        _redact.register_stream_accounts(ret)
        if _carries_a_heartbeat(ret):
            self.last_heartbeat_at = self.last_frame_at

        self.logger.debug(
            'Receive %s: Returning message from stream: %s',
            self.req_num(), LazyLog(lambda: self._pretty(_redact.mask(ret))))

        return ret

    async def _receive(self):
        if self._socket is None:
            raise ValueError(
                'Socket not open. Did you forget to call login()?')

        if len(self._overflow_items) > 0:
            ret = self._overflow_items.pop()

            self.logger.debug(
                'Receive %s: Returning message from overflow: %s',
                self.req_num(), LazyLog(lambda: self._pretty(ret)))

            return ret

        return await self._receive_from_socket()

    async def _init_from_preferences(self, prefs, websocket_connect_args):
        # Record streamer subscription keys
        stream_info = prefs['streamerInfo'][0]

        self._stream_correl_id = stream_info['schwabClientCorrelId']
        self._stream_customer_id = stream_info['schwabClientCustomerId']
        self._stream_channel = stream_info['schwabClientChannel']
        self._stream_function_id = stream_info['schwabClientFunctionId']

        # Initialize socket
        wss_url = stream_info['streamerSocketUrl']

        websocket_connect_args = _prepare_connect_args(websocket_connect_args)

        if self._ssl_context:
            websocket_connect_args['ssl'] = self._ssl_context

        # Close whatever is being replaced. After a ConnectionClosed the old
        # socket is already gone and this is a no-op, but a caller who calls
        # login() on a healthy client -- a re-auth, a preferences refresh --
        # would otherwise drop a live websocket and its reader on the floor
        # with nothing closing it. Failure to close is logged, not raised: the
        # login is the operation the caller asked for.
        previous, self._socket = self._socket, None
        if previous is not None:
            try:
                await previous.close()
            except Exception:
                self.logger.exception(
                        'Failed to close the previous stream connection while '
                        'logging in again. Continuing with the new one.')

        self._socket = await ws_client.connect(
                wss_url, **websocket_connect_args)

        # Cleared *after* the new socket is in place, not before. Anything
        # still queued belongs to the connection being replaced: its exception
        # carries a frame from that session, and a stale data frame would reach
        # handlers as though it were live. Clearing first would leave a hole
        # -- a concurrent reader on the old socket appends to _overflow_items
        # while this coroutine is suspended in close() or connect(), and that
        # frame would survive into the new session.
        #
        # close() clears both too, but a caller reconnecting after a
        # ConnectionClosed may call login() again without closing first, and
        # the guarantee has to hold whichever teardown they used.
        self._pending_reports.clear()
        self._overflow_items.clear()
        self._absorbed = 0
        self._absorbed_kinds.clear()


    def add_close_handler(self, handler):
        '''Has ``handler(close)`` called, with a :class:`StreamClose`, once
        for each connection that ends: whichever call finds the close, and
        before that call raises. A coroutine it returns is awaited. Its own
        exception is logged and reported through the error handlers, and
        does not stop the others. It may log in again: no lock is held.'''
        self._close_handlers.append(handler)

    def add_heartbeat_handler(self, handler):
        '''Has ``handler(heartbeat_ms)`` called for each heartbeat Schwab
        sends, with its value as an int, or None when it is not one. A
        coroutine it returns is awaited, and its own exception is logged and
        reported. Schwab documents no interval, and none is measured.'''
        self._heartbeat_handlers.append(handler)

    def _found_close(self, exc):
        '''Records how the connection ended, once, where it is found: under
        a lock, so nothing here calls user code.'''
        if self._close_found:
            return
        rcvd = getattr(exc, 'rcvd', None)
        code = getattr(rcvd, 'code', None)
        notice = self._last_notice
        if notice is not None and notice.code == 12:
            kind = StreamCloseKind.BUMPED
        elif notice is not None and notice.code == 30:
            kind = StreamCloseKind.STOPPED
        elif code == 1000:
            kind = StreamCloseKind.SERVER_CLOSED
        else:
            kind = StreamCloseKind.CONNECTION_LOST
        self._set_close(kind, code, getattr(rcvd, 'reason', None), notice)

    def _set_close(self, kind, code=None, reason=None, notice=None):
        self._close_found = True
        self.last_close = StreamClose(kind, code, reason, notice)
        self._close_due = True

    async def _login_failed(self, kind):
        self._set_close(kind, notice=self._last_notice)
        await self._run_close_handlers()

    async def _run_close_handlers(self):
        if not self._close_due:
            return
        self._close_due = False
        close = self.last_close
        for handler in list(self._close_handlers):
            await self._call_user_handler(handler, close, 'Close handler')

    async def _run_heartbeat_handlers(self, value):
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        value = _int_or_none(value)
        for handler in list(self._heartbeat_handlers):
            await self._call_user_handler(handler, value, 'Heartbeat handler')

    async def _call_user_handler(self, handler, argument, what):
        try:
            result = handler(argument)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            self.logger.error('%s raised %s. Ignoring it.', what,
                              _type_name(exc), exc_info=exc)
            await self._report_error(exc)

    def _notice_from(self, element, content):
        '''A :class:`StreamNotice` when ``element`` is one: on the ADMIN
        service other than an answer to a login or logout, or carrying a
        severing code. Otherwise None.'''
        try:
            service = element.get('service')
            command = element.get('command')
            content = content if _is_mapping(content) else {}
            code = content.get('code', element.get('code'))
            msg = content.get('msg', element.get('msg'))
        except Exception:
            return None
        admin = service == 'ADMIN' and command not in ('LOGIN', 'LOGOUT')
        if not admin and _int_or_none(code) not in _SEVERING_CODES:
            return None
        return StreamNotice(
                element, 'Schwab sent a notice about the stream: code {}, '
                'msg {}'.format(_safe_value(code), _safe_value(msg)),
                code=code, msg=msg)

    async def _report_notice(self, notice, element):
        self._last_notice = notice
        self.logger.warning('%s', notice)
        await self._report_error(notice, service='ADMIN', message=element)

    def _make_request(self, *, service, command, parameters):
        request_id = self._request_id
        self._request_id += 1

        request = {
            'service': service,
            'requestid': str(request_id),
            'command': command,
            'SchwabClientCustomerId': self._stream_customer_id,
            'SchwabClientCorrelId': self._stream_correl_id,
            'parameters': parameters,
        }

        return request, request_id

    @staticmethod
    def _validate_response(resp, request_id, service, command):
        '''
        Checks a response frame against the request it is supposed to answer.
        Returns ``None`` if it matches, or the exception to fail that request
        with if it does not.

        A frame too malformed to check is *returned as* an exception rather
        than raising one. Raising here reaches _read_and_route, which hands it
        to _fail_pending_request and re-raises -- so one unreadable field
        would fail the request with a bare KeyError and end the caller's
        receive loop as well. Failing just the request, with an exception that
        says what was wrong, is what the caller can act on: the answer to their
        request really was unusable.
        '''
        try:
            first = resp['response'][0]
            resp_request_id = int(first['requestid'])

            # Compared before the rest is read. Reading all five first would
            # report a frame carrying an id this client never issued *and*
            # missing some other field as "malformed response frame: KeyError:
            # 'service'". The id mismatch is the more diagnostic fact -- it
            # means the server and this client disagree about what was asked --
            # and it would be hidden behind whichever field happened to be
            # absent.
            mismatched_id = (resp_request_id
                             if resp_request_id != request_id else None)

            resp_service = first['service'] if mismatched_id is None else None
            resp_command = first['command'] if mismatched_id is None else None
            content = first['content'] if mismatched_id is None else {}
            resp_code = content['code'] if mismatched_id is None else 0
        except (AttributeError, IndexError, KeyError, OverflowError,
                TypeError, ValueError) as exc:
            # Only the reads are inside the try. Wrapping the whole check
            # would catch a bug of ours -- a signature change missing this
            # call site raises TypeError -- and report it to the caller as a
            # malformed frame from Schwab, carrying that frame as evidence.
            # Every subscribe would fail with the investigation pointed at the
            # venue rather than at this library.
            return UnexpectedResponse(
                resp, 'malformed response frame: {}: {}'.format(
                    _type_name(exc), _safe_value(exc)))

        # Built outside the try above, which exists to turn *reads* of a
        # malformed frame into a clean error. Constructing the exception in
        # there would mean a signature change to UnexpectedResponse reported
        # every id mismatch as "malformed response frame: TypeError", blaming
        # the venue for a bug of ours -- the exact misattribution that try is
        # narrow to avoid.
        if mismatched_id is not None:
            return UnexpectedResponse(
                resp, 'unexpected requestid: {}'.format(
                    _safe_value(mismatched_id)))

        # Validate service
        if resp_service != service:
            return UnexpectedResponse(
                resp, 'unexpected service: {}'.format(
                    _safe_value(resp_service)))

        # Validate command
        if resp_command != command:
            return UnexpectedResponse(
                resp, 'unexpected command: {}'.format(
                    _safe_value(resp_command)))

        # `msg` is read with .get: a rejection which carries a code but no
        # message is still a rejection, and reporting it as an unreadable
        # frame instead would lose the code -- the one part the caller can act
        # on.
        if not _is_success_code(resp_code):
            return UnexpectedResponseCode(
                resp,
                'unexpected response code: {}, msg is {}'.format(
                    _safe_value(resp_code),
                    _safe_value(content.get('msg'))),
                service=resp_service, command=resp_command, code=resp_code,
                msg=content.get('msg'))

        return None

    async def _read_and_route(self, *, use_overflow=True):
        '''
        Reads one frame and routes it. The caller must hold ``_read_lock``,
        which is what guarantees a single reader: ``websockets`` raises if two
        coroutines call ``recv()`` concurrently.

        Returns the frame if it is for the caller to deal with, or ``ROUTED``
        if it was a response which has been delivered to the operation waiting
        on it.

        ``ROUTED`` rather than ``None`` because ``None`` is a frame a server
        can send: a top-level JSON ``null`` would be indistinguishable from a
        routed response, and dropped without the warning every other unusable
        frame gets.

        ``use_overflow`` is ``False`` for a caller waiting on a response, which
        must read from the socket rather than from the queue of frames set
        aside for ``handle_message`` -- otherwise it would take back the frames
        it just deferred, and never get past them.
        '''
        frame = (await self._receive() if use_overflow
                 else await self._receive_from_socket())

        # `'response' not in frame` is itself a read which assumes a container,
        # and raises on a top-level JSON number. Handed back rather than
        # dropped here, so handle_message logs it in one place along with every
        # other frame this cannot route.
        if not _is_mapping(frame):
            return frame

        if 'response' not in frame:
            return frame

        pending = self._pending_request
        if pending is None:
            # A response nobody is waiting for. Hand it back so the caller can
            # complain about it.
            return frame

        request_id, service, command, future = pending

        # A response carrying an id we issued *earlier* is the late answer to a
        # request which was abandoned -- timed out, or cancelled. It is not
        # this request's answer and says nothing about it.
        #
        # Failing the pending request with it would be wrong twice over. The
        # innocent request dies, and the abandoned request's answer is consumed
        # in the process, so the next request reads the *next* stale answer and
        # dies the same way. One timeout would wedge every subsequent request
        # for the life of the stream. Hand it back instead, and let
        # handle_message log it as the orphan it is.
        #
        # An id we have never issued is a different matter. That is not
        # lateness, it is the server and this client disagreeing about what was
        # asked, and the request being waited on has no better prospect than
        # the one which just arrived. Let that fail below.
        # Read defensively. Everything downstream of here parses through
        # _iter_responses, which logs a malformed element and skips it -- but
        # that guard is unreachable if reading the id raises first. A frame
        # whose `response` is not a list, or whose element 0 has no usable
        # requestid, would take out the in-flight request through
        # _fail_pending_request and end the caller's receive loop, while the
        # identical frame arriving with nothing pending was logged and
        # harmless. The outcome would depend on framing, which is what this
        # routing exists to prevent.
        #
        # Handed back rather than raised: handle_message logs and skips it,
        # which is what it does with the same frame when no request is
        # outstanding.
        try:
            response_id = int(frame['response'][0]['requestid'])
        except (AttributeError, IndexError, KeyError, OverflowError,
                TypeError, ValueError):
            # `OverflowError` because `json.loads` maps the bare literals
            # `1e999` and `Infinity` to `float('inf')`, and `int(inf)` raises
            # that rather than ValueError. `NaN` takes the same route and
            # raises ValueError instead, so both are needed.
            #
            # Through _absorb like every other unusable message, so it is
            # counted, coalesced and reported. Logged directly, it would have
            # no programmatic signal at all -- and it repeats per re-read, so
            # a venue emitting a non-numeric requestid would produce a
            # continuous warning stream inside a single subscribe.
            self._absorb('a response frame with no usable request id',
                         frame.get('response'), frame=frame)
            return frame

        if response_id != request_id and response_id < self._request_id:
            return frame

        if not future.done():
            error = self._validate_response(frame, request_id, service, command)
            if error is None:
                future.set_result(frame)
            else:
                future.set_exception(error)

            # Element 0 went to the waiter, so it is accounted for.
            first_unclaimed = 1
        else:
            # The future was already resolved -- the waiter timed out, or was
            # cancelled, and this frame arrived in the window before the
            # pending slot was cleared. Nothing is waiting for element 0
            # either, so it is as unclaimed as the rest. Without this, a
            # rejection at element 0 would be dropped in silence while a
            # rejection at element 1 of the same frame was reported, which is
            # the inverse of the framing-independence this exists for.
            first_unclaimed = 0

        # Everything not claimed above is logged and, if it is a rejection,
        # queued for report. The protocol treats element 0 as the answer to the
        # outstanding request, and _validate_response reads only that one, so
        # any further response in the frame reaches the waiter unexamined.
        #
        # Logged here, but NOT reported here. This runs holding the read
        # lock, on the request path holding the request lock too, and inside
        # the response deadline -- so calling a user handler here would let a
        # slow one turn a subscription that SUCCEEDED into a
        # ResponseTimeoutError, and a handler that re-subscribed would block on
        # a lock its own caller holds. The report is queued and delivered by
        # whichever coroutine read the frame once it holds neither lock and
        # runs under no deadline: at the top of handle_message's loop, or at
        # the end of _request_response.
        self._log_extra_responses(frame, first_unclaimed)

        return ROUTED

    def _iter_responses(self, frame, start=0):
        '''Yields ``(response, code, content)`` for the responses in a frame.

        Shared by both framings of the same event -- a late rejection in a
        frame of its own, and one batched behind the answer to a live request
        -- because the contract is that they behave alike, and two loops
        parsing the same JSON drift apart: an identical payload could be
        skipped with a warning in one framing and end the caller's receive loop
        with an AttributeError in the other.

        Nothing here raises. On the routing path this runs after the waiter's
        future is resolved, so an exception escaping would reach the caller of
        a request which SUCCEEDED; in handle_message it would end the receive
        loop over one bad element among many.
        '''
        responses = frame.get('response')
        if not _is_sequence(responses):
            self._absorb('a response frame whose "response" is not a list',
                         responses, frame=frame)
            return

        for response in responses[start:]:
            if not _is_mapping(response):
                self._absorb('an unparseable response element', response,
                             frame=frame)
                continue

            # Normalised, not just defaulted. The key is often present with a
            # JSON null, which .get(key, {}) returns as None -- and `or {}`
            # alone is not enough either, because a *truthy* non-mapping (a
            # string) survives it and is then handed to callers which call
            # .get on it. handle_message's orphan loop does exactly that.
            content = response.get('content')
            if not _is_mapping(content):
                content = {}

            code = content.get('code')

            yield response, code, content

    def _report_new_shape(self, what, name):
        """Name a thing Schwab added, once, and say so through both channels.

        Returns True the first time this client sees a given (kind, name),
        which the caller passes to `_absorb` as `force` so the callback names
        it too -- `_absorb` coalesces on a counter keyed per kind, so without
        that a fourth new service arriving beside three others reaches no
        handler at all. Bounded: a first sighting happens at most
        `_MAX_REPORTED_SHAPES` times per client.

        Per client, and not reset by `login` or `close`, so a reconnect does
        not emit this dedicated line again -- the handlers are the ones that
        already heard. `_absorb`'s own counters *are* reset by `login`, so
        the same unknown service still reaches the callback through ordinary
        coalescing after a reconnect; what does not recur is the guarantee
        that every distinct name gets through. A *different* client has
        different handlers and hears for itself.

        The name goes through `_safe_name`: it is venue-controlled, `str()`
        of it can raise, and it is bounded because this set never shrinks.
        Formatted with `%r` for the same reason `_absorb` goes through
        `_safe_value` -- a newline
        in a service name would otherwise forge a second log line
        indistinguishable from a real one.
        """
        seen = (what, _safe_name(name))
        if seen in self._reported_shapes:
            return False
        if len(self._reported_shapes) >= _MAX_REPORTED_SHAPES:
            return False
        self._reported_shapes.add(seen)
        if len(self._reported_shapes) >= _MAX_REPORTED_SHAPES:
            self.logger.warning(
                    'That is %d distinct services and channels schwaby does '
                    'not know, which is as many as it will name. Any further '
                    'ones are dropped the same way and are not reported.',
                    _MAX_REPORTED_SHAPES)
        self.logger.warning(
                'Schwab sent a %s this version of schwaby does not know: %r. '
                'Messages for it are dropped -- there is no handler to route '
                'them to -- and this is reported once, not per message. '
                'Please open an issue at '
                'https://github.com/tom-hirt/schwaby/issues.',
                what, seen[1])
        return True

    def _absorb(self, what, offender, frame=None, service=None,
                cause=None, force=False):
        """Records a message this client cannot use.

        Two things happen to it, and each earns its place:

        * a WARNING, so it is visible without code changes -- but only for the
          first few. A systematically malformed high-volume channel would
          otherwise emit one line per element per tick indefinitely, turning a
          data outage into a log-volume incident as well. Powers of ten, so the
          running total stays visible without the flood.
        * a report through add_error_handler, because that callback's whole
          purpose is that a failure this client absorbs should not be visible
          only in a log. Queued the same way a batched rejection is, and
          delivered from the same drain, so no user code runs here.
        """
        self._absorbed += 1
        self._absorbed_kinds[what] += 1
        n = self._absorbed_kinds[what]

        # Counted per kind, not across all of them. With one shared counter, a
        # framing change on a few hundred symbols would drive the count past a
        # thousand within two ticks, after which a *different* fault -- a
        # notify frame whose service is a list -- would be neither logged nor
        # reported, so an operator investigating the first outage would see no
        # trace of the second.
        #
        # The first few of each kind, then powers of ten. A fixed modulus would
        # go quiet after the third occurrence and say nothing more until the
        # thousandth, hiding the shape of an early burst.
        # `force` is the first sighting of a service or channel name. The
        # counter above is keyed per kind, so without this the callback
        # coalesces across *names* and a fourth new service reaches no
        # handler at all -- bounded, because a first sighting happens at
        # most `_MAX_REPORTED_SHAPES` times.
        if not force and n > 3 and not _is_power_of_ten(n):
            return

        self.logger.warning(
                'Ignoring %s: %s. (%d of these, %d in all, on this '
                'connection.)%s', what, _safe_value(offender), n,
                self._absorbed,
                '' if cause is None
                else ' Cause: {}: {}'.format(
                    _type_name(cause), _safe_str(cause)))

        # Reported on the same schedule as the log, not on every occurrence.
        # These share _pending_reports with the late rejections, which is a
        # bounded deque: a frame carrying a few hundred bad elements would
        # otherwise evict every rejection queued beside it -- and a rejection
        # of an abandoned request is the one thing nothing else will ever
        # report. Coalescing keeps this callback from starving the reports it
        # was built for.
        self._pending_reports.append((
                UnusableMessage(
                    offender,
                    'Ignoring {} ({} of these, {} in all, on this '
                    'connection){}'.format(
                        what, n, self._absorbed,
                        '' if cause is None
                        else ': {}: {}'.format(
                            _type_name(cause), _safe_str(cause))),
                    cause=cause, count=n, total=self._absorbed),
                # Usually None -- a message whose shape could not be read has
                # no service name to be had -- but not always: a relabeling
                # failure knows exactly which subscription went dark, and a
                # handler routing alerts by service needs it.
                service,
                # The containing frame, not the offending value, because the
                # value is often None -- a null channel, a null element -- and
                # a report of (None, None) is the signature the logout-close
                # failure uses. A caller discriminating on "both None" would
                # file a dead feed under "could not close after logout".
                # Where there is no containing frame, the offender is the
                # frame, and `isinstance(exception, UnusableMessage)` is the
                # discriminator the docs point at.
                frame if frame is not None else offender))

    def _iter_channel(self, msg, channel):
        '''Yields the well-formed elements of ``msg[channel]``.

        The data and notify counterpart of _iter_responses, for the same
        reason: ``d.get('service')`` is evaluated at the call site, *outside*
        the try in _dispatch_to_handlers, so without this a non-dict element or
        a non-list channel would end the caller's receive loop with an
        AttributeError or a TypeError.

        A bad element is logged and skipped; the good ones beside it are still
        dispatched.
        '''
        if channel not in msg:
            return

        # Distinguished from absence deliberately: `{"data": null}` is a
        # malformed channel, not a frame with no data, and is reported like
        # `{"data": 5}` rather than dropped without a word.
        elements = msg.get(channel)

        if not _is_sequence(elements):
            self._absorb('a %s frame whose %r is not a list'
                         % (channel, channel), elements, frame=msg)
            return

        for element in elements:
            if not _is_mapping(element):
                self._absorb('an unparseable %s element' % channel, element,
                             frame=msg)
                continue

            yield element

    def _log_extra_responses(self, frame, start=1):
        '''Logs every response past the first, and queues the rejections.

        Because _request_lock keeps one request outstanding at a time, a second
        response in the same frame cannot be an answer to anything this client
        is currently waiting on. It is a late answer to a request which was
        abandoned -- exactly what the orphan path in handle_message reports.
        Whether Schwab sends that in a frame of its own or batches it behind
        the answer to a live request is the server's choice, so reporting only
        the first would make add_error_handler fire or stay silent for the same
        event depending on framing the caller cannot see or predict.
        '''
        for response, code, content in self._iter_responses(frame, start):

            # Matches the orphan path: a late acknowledgement of something that
            # worked is routine and logs at INFO, a late rejection warns.
            log = (self.logger.info if _is_success_code(code)
                   else self.logger.warning)
            log('Response frame carried an additional %s/%s response: code %s, '
                'msg %s. It answers no request this client is waiting on.',
                _safe_value(response.get('service')),
                _safe_value(response.get('command')),
                _safe_value(code), _safe_value(content.get('msg')))

            # `is not None` as well, for the same reason as the orphan path: a
            # response with no code at all is neither a rejection nor a
            # success, and reporting it would page someone over `code None`.
            if code is not None and not _is_success_code(code):
                self._pending_reports.append((
                        UnexpectedResponseCode(
                            frame,
                            'Schwab rejected a request which had already '
                            'been abandoned: code {}, msg {}'.format(
                                _safe_value(code),
                                _safe_value(content.get('msg'))),
                            service=response.get('service'),
                            command=response.get('command'), code=code,
                            msg=content.get('msg')),
                        response.get('service'),
                        response))

    async def _report_unparsable(self, exc):
        """Reports a frame whose JSON did not decode, then lets it propagate.

        Called by *both* readers. There are two coroutines which can be holding
        the read lock when a frame arrives -- handle_message and a request
        waiting on its response. Which one reads any given frame is decided by
        a lock race the caller cannot see, so a callback wired on one path and
        not the other would fire or not fire for reasons nothing in the API
        explains.

        The caller must invoke this with the read lock released. Reporting from
        inside the block that holds it would run a user handler under the lock,
        which is the hazard the queue-and-drain design exists to avoid.

        Reports each exception once. handle_message hands the same object to
        the waiting request through _fail_pending_request before reporting it,
        so it arrives here twice for one bad frame -- and both reports carry an
        identical (None, exc, raw_msg), which a consumer cannot tell from two
        genuinely distinct bad frames. Marking the object is what makes "once"
        true regardless of which coroutine won the read; without it the same
        lock race would show as "fires once or twice" instead.
        """
        if getattr(exc, '_schwab_reported', False):
            return
        try:
            exc._schwab_reported = True
        except AttributeError:      # pragma: no cover -- Exception allows it
            pass

        try:
            # message=raw, not nothing. There is no service to name -- nothing
            # decoded, so there is no frame to read one from -- and reporting
            # neither would give this the same shape as the logout-close
            # failure. Note raw can be the empty string for an empty frame, so
            # the pair is distinguishable under `is None` but not under a
            # falsy test; the documented discriminator is the exception type.
            await self._report_error(exc, message=exc.raw_msg)
        except asyncio.CancelledError:
            raise
        except BaseException:
            # Must not replace the parse failure the caller needs to see, for
            # the same reason logout() guards its own report.
            self.logger.exception(
                    'Error handler raised while reporting an unparsable '
                    'message. Ignoring it.')

    async def _drain_pending_reports(self):
        '''Delivers reports queued by _log_extra_responses.

        Called from two places, both with the read lock released, no request
        lock held and no deadline running: the top of handle_message's loop,
        and the end of _request_response. Whichever coroutine read the frame
        delivers what it queued, so a report is not stranded behind a reader
        parked in recv(). A user handler can therefore run inside a subscribe.

        Each entry is popped before it is awaited, so a handler which re-enters
        handle_message finds an empty queue rather than reporting the same
        rejection twice.

        '''
        while self._pending_reports:
            exception, service, message = self._pending_reports.popleft()
            await self._report_error(
                    exception, service=service, message=message)

    def _fail_pending_request(self, exception):
        '''Hands an exception to the operation waiting on a response, if any.

        Used when the read fails rather than the response: without this, a
        connection which drops while a request is outstanding would leave that
        request waiting for a reply which can never arrive.
        '''
        pending = self._pending_request
        if pending is not None and not pending[3].done():
            pending[3].set_exception(exception)

    async def _await_response(self, request_id, service, command):
        '''
        Waits for the response to a request which has already been sent.

        The wait is over whichever of two things happens first: the response is
        routed to us by whoever is currently reading the socket, or the socket
        becomes free and we read it ourselves. That is what stops a subscription
        from blocking behind a ``handle_message`` which is parked on a quiet
        stream -- the reader delivers our response as soon as it arrives,
        without ever having to give up the socket.
        '''
        loop = asyncio.get_running_loop()
        future = self._pending_request[3]

        deadline = (None if self._response_timeout is None
                    else loop.time() + self._response_timeout)

        def remaining():
            return None if deadline is None else max(deadline - loop.time(), 0)

        def timed_out():
            return ResponseTimeoutError(
                    service, command, self._response_timeout,
                    'timed out after {}s waiting for a response to {}/{}'.format(
                        self._response_timeout, service, command))

        while not future.done():
            acquire = asyncio.ensure_future(self._read_lock.acquire())
            became_reader = False

            # Every exit from this block goes through the finally, including
            # being cancelled while waiting. Without that, a cancellation
            # delivered after the acquire had already succeeded would leave the
            # read lock held by nobody and wedge the client permanently.
            try:
                done, _ = await asyncio.wait(
                        {acquire, future}, timeout=remaining(),
                        return_when=asyncio.FIRST_COMPLETED)

                became_reader = acquire in done and not acquire.cancelled()

                if became_reader:
                    if future.done():
                        break
                    try:
                        frame = await asyncio.wait_for(
                                self._read_and_route(use_overflow=False),
                                timeout=remaining())
                    except asyncio.TimeoutError:
                        raise timed_out()
                    except BaseException as exc:
                        # The read failed, so nothing will ever answer this
                        # request. Fail it with the same error.
                        self._fail_pending_request(exc)
                        raise
                    if frame is not ROUTED:
                        # Not ours, so it belongs to handle_message. Set it
                        # aside as it arrives rather than at the end: a
                        # concurrent handle_message reads between our reads, so
                        # holding frames back would let a later one be
                        # dispatched first. appendleft here, pop from the right
                        # in _receive, so arrival order is preserved.
                        self._overflow_items.appendleft(frame)
                elif not done:
                    raise timed_out()
            finally:
                if became_reader:
                    self._read_lock.release()
                else:
                    # We did not become the reader, or we are unwinding. Give up
                    # the attempt, handling the race where it succeeded just as
                    # we stopped waiting for it.
                    acquire.cancel()
                    if acquire.done() and not acquire.cancelled():
                        self._read_lock.release()

        return future.result()

    async def _request_response(self, request, request_id, service, command):
        '''Sends a request and waits for the response which answers it.

        ``_request_lock`` keeps one request outstanding at a time, which is what
        lets a single pending slot be enough and keeps the response validation
        unambiguous. Note it does not cover reading, so message handling
        continues while a request is in flight.
        '''
        cancelled = False

        try:
            async with self._request_lock:
                future = asyncio.get_running_loop().create_future()
                self._pending_request = (request_id, service, command, future)
                try:
                    await self._send({'requests': [request]})
                    await self._await_response(request_id, service, command)
                finally:
                    self._pending_request = None
                    # Nobody will look at this future again. Cancel it if it is
                    # still open, so a reader which grabbed a reference to it
                    # just before it was cleared cannot complete it into the
                    # void, and retrieve any exception so asyncio does not
                    # report it as never having been retrieved.
                    if not future.done():
                        future.cancel()
                    elif not future.cancelled():
                        future.exception()
        except UnparsableMessage as exc:
            # The other reader. _await_response releases the read lock in its
            # own finally and the request lock is released by the `async with`
            # above, so this runs with neither held.
            try:
                await self._report_unparsable(exc)
            except asyncio.CancelledError:
                # Sibling except clauses do not see each other, so a
                # cancellation arriving inside the report would leave
                # `cancelled` False and let the finally drain while unwinding
                # -- blocking a close() or a TaskGroup shutdown for a user
                # handler's full duration, which is exactly what the flag
                # exists to prevent.
                cancelled = True
                raise
            raise
        except ConnectionClosed:
            # The locks are released here, so a close handler may log in.
            try:
                await self._run_close_handlers()
            except asyncio.CancelledError:
                cancelled = True
                raise
            raise
        except asyncio.CancelledError:
            # Noted so the finally can skip the drain. Running a user handler
            # to completion while unwinding a cancellation makes close(), a
            # wait_for, or a TaskGroup shutdown block for as long as that
            # handler takes -- measured at the handler's full duration. The
            # reports stay queued for handle_message, and are discarded by
            # close() or a fresh login() like anything else from a session
            # being torn down.
            cancelled = True
            raise
        finally:
            # Outside the `async with`, so neither lock is held and the
            # response deadline has passed. Whoever read the frame delivers
            # what it queued: when this coroutine was the reader,
            # handle_message may be parked in recv() having already drained for
            # its iteration, and the report would otherwise wait for the next
            # inbound message -- unbounded on a quiet stream.
            #
            # In a `finally`, so a request which *failed* still delivers what
            # it read. The caller's exception is element 0's rejection and says
            # nothing about element 1, and a failed subscribe is usually
            # followed by tearing the client down rather than reading it again
            # -- so draining only on success would drop the batched rejection
            # exactly when it is least likely to be reported another way.
            if not cancelled:
                try:
                    await self._drain_pending_reports()
                except asyncio.CancelledError:
                    # Not swallowed with the rest, for the reason logout()
                    # gives: discarding a cancellation makes this refuse to
                    # die.
                    raise
                except BaseException:
                    # BaseException is deliberately left to propagate out of
                    # _report_error everywhere else. Not here. This is a
                    # finally clause, so a handler raising SystemExit would
                    # replace the request's own exception -- the caller would
                    # never learn that Schwab refused their subscribe, which is
                    # the more useful error by far. Same guard, same reason, as
                    # the one in logout().
                    self.logger.exception(
                            'Error handler raised while reporting a rejection '
                            'carried alongside a response. Ignoring it.')

    async def _service_op(self, symbols, service, command, field_type=None,
                          *, fields=None, keys_per_request=None):
        symbols = _checked_keys(symbols)
        size = _checked_batch_size(
                self._keys_per_request if keys_per_request is None
                else keys_per_request)
        if size is None or len(symbols) <= size:
            await self._one_service_op(symbols, service, command, field_type,
                                       fields=fields)
            return
        # A SUBS replaces the service's subscriptions, so only the first
        # batch is one; the rest are added to it.
        sent = []
        for start in range(0, len(symbols), size):
            batch = symbols[start:start + size]
            batch_command = ('ADD' if command == 'SUBS' and start
                             else command)
            try:
                await self._one_service_op(batch, service, batch_command,
                                           field_type, fields=fields)
            except (UnexpectedResponseCode, ResponseTimeoutError) as e:
                # The earlier batches stand.
                e.sent_keys = list(sent)
                e.unsent_keys = list(symbols[start:])
                raise
            sent.extend(batch)

    async def _one_service_op(self, symbols, service, command, field_type,
                              *, fields=None):
        parameters = {
            'keys': ','.join(symbols)
        }

        if field_type is not None:
            if fields is None:
                fields = field_type.all_fields()

            fields = sorted(self.convert_enum_iterable(fields, field_type))
            parameters['fields'] = ','.join(str(f) for f in fields)

        request, request_id = self._make_request(
            service=service, command=command,
            parameters=parameters)

        await self._request_response(request, request_id, service, command)

    async def _view_op(self, service, field_type, fields):
        '''Sends VIEW, which changes the fields delivered for a service.

        No ``keys``: Schwab documents a VIEW as applying to all symbols of the
        service, and a VIEW naming one symbol was measured changing the fields
        of every symbol, on futures and on forex. Keys would suggest a scoping
        Schwab does not do.

        Fields are converted first and handled as numbers, so the caller's
        iterable is never changed, a single member or a generator works, and
        a field given twice -- or ``SYMBOL`` given explicitly -- is sent once,
        whether it was given as a member or, with ``enforce_enums=False``, as
        a number. ``SYMBOL`` is added when missing, as the subscribe methods
        add it.

        A string or a buffer is refused rather than iterated: ``'12'`` would
        otherwise become the fields ``1`` and ``2``, and ``b'12'`` the fields
        ``49`` and ``50``. So is a value that is not iterable at all, which
        would otherwise escape as the ``TypeError`` raised inside the
        conversion.

        A ``TypeError`` from the conversion is relabelled only when
        ``fields`` is itself not iterable. One raised inside a caller's own
        generator says nothing about ``fields``, and relabelling that would
        tell them the wrong thing and drop the traceback that says otherwise.
        '''
        if _is_instance(fields, (str, bytes, bytearray, memoryview)):
            raise ValueError(
                'fields must be an iterable of {} members or field numbers, '
                'not a {}'.format(field_type.__name__, _type_name(fields)))
        try:
            values = self.convert_enum_iterable(fields, field_type)
        except TypeError:
            # A single member is accepted and is not iterable, so this asks
            # about the value rather than assuming the conversion's complaint
            # was about it. None reaches the refusal below, as it means every
            # field to subs and add and no field here.
            try:
                iter(fields)
            except TypeError:
                raise ValueError(
                    'fields must be an iterable of {} members or field '
                    'numbers; {} is not iterable'.format(
                        field_type.__name__, _type_name(fields))) from None
            raise
        numbers = set()
        for value in values or ():
            try:
                number = int(str(value))
            except ValueError:
                number = -1
            if number < 0:
                raise ValueError(
                    'fields must be {} members or field numbers, got '
                    '{}'.format(field_type.__name__, _safe_value(value)))
            numbers.add(number)
        if not numbers:
            raise ValueError('fields must name at least one field')
        numbers.add(field_type.SYMBOL.value)
        parameters = {'fields': ','.join(str(n) for n in sorted(numbers))}

        request, request_id = self._make_request(
            service=service, command='VIEW', parameters=parameters)

        await self._request_response(request, request_id, service, 'VIEW')

    def add_error_handler(self, error_handler):
        '''
        Registers a callback for failures this client absorbs rather than
        raising.

        Four things reach it, all of them failures this client is right to
        absorb and none of which the caller could otherwise react to:

        * a stream handler which raises, synchronous or coroutine, because one
          misbehaving handler must not stop the others or drop the connection;
        * a response arriving for a request nobody is waiting on which carries
          a rejection, because the request was abandoned and nothing else will
          ever say Schwab refused it. Note the ``UnexpectedResponseCode``
          raised here carries the whole frame, and a frame can hold several
          responses -- so ``exception.response['response'][0]`` is not
          necessarily the rejected one, unlike the same exception raised from a
          request you are waiting on. The rejected element is what arrives as
          ``message``;
        * a connection which fails to close after logout, because the logout
          itself succeeded;
        * a message this client cannot use at all -- a frame which is not an
          object, an element of ``data`` or ``notify`` which is not an object,
          a ``service`` which is not a name, a channel or a ``response``
          whose value is not a list, an unparseable element of a
          ``response``, a response frame with no usable request id, a
          message which could not be relabeled --- the only category that
          populates ``cause``, though not the only one that populates
          ``service`` --- and, since Schwab may add to this protocol at any
          time, a ``service`` which *is* a name this version does not know,
          or a frame carrying a whole channel it does not read. An unknown
          service populates ``service``; an unread channel does not, because
          a channel is not one --- its names are on ``message`` instead.
          Those last two are messages this
          client drops because Schwab added something rather than because
          anything is malformed, and they are the ones a consumer cannot see
          any other way: no handler of theirs fires for either. **Every distinct
          service or channel name reaches this callback at least once**, up
          to the first 64 of them on this client, which the coalescing below
          would otherwise prevent -- it counts per kind, so without that
          guarantee a fourth new service arriving beside three others would
          never be reported at all. Past 64 that guarantee stops: a further
          name may still be reported, if `_absorb`'s own per-kind counter
          happens to be low enough, but nothing promises it. A line says so
          when the cap is reached. These arrive as
          :class:`UnusableMessage`, whose ``message`` is the offending value
          as it arrived --- except for an unread channel, where it is the
          sorted list of channel names, since what offends is which
          compartments appeared rather than any one value inside them. These
          are *coalesced*: the first three **of each kind** on a
          connection, then powers of ten, with the running count in the
          message. Per kind, so a flood of one sort does not silence a
          different sort arriving beside it. A
          systematically malformed channel produces one of these per element
          per tick, which would otherwise be a log-volume incident on top of
          the outage -- and, because these share a bounded queue with the late
          rejections above, would push out the one thing nothing else will ever
          report. Schwab's ``'Invalid Service'``, which it has once sent in
          place of a service name in the second before closing a connection,
          is reported the same way, with that as ``service``. It is not one
          of the two above: it is logged as Schwab's error rather than as a
          service to support, and it is coalesced like the malformed kinds.

        Each is also logged. The callback is what makes them something other
        than a log record.

        The second of those covers a rejection however Schwab frames it. It
        may arrive in a frame of its own, or riding along behind the answer to
        a request you are waiting on -- only one request is outstanding at a
        time, so anything past the first response in a frame is a late answer
        to an abandoned one either way. Which framing you get is the server's
        choice, and both report identically.

        The batched one is not reported from where it is found. Routing a
        frame happens holding the read lock, on the request path holding the
        request lock too, and inside the response deadline: a slow handler
        called there would turn a subscription that succeeded into a
        ``ResponseTimeoutError``, and one which re-subscribed would block on a
        lock its own caller holds. The report is queued instead, and delivered
        by whichever coroutine read the frame once it has released its locks --
        before :meth:`handle_message` returns, or before the request which read
        it returns. Both run with neither lock held and no deadline.

        That means a handler can be called from inside a subscribe, so a slow
        one delays that call returning. It cannot fail it: by then the response
        has already been matched.

        A request delivers what it read even when it fails, since the
        exception the caller gets describes the response which answered *their*
        request and says nothing about the others in the frame. Two limits
        there: a handler's ``BaseException`` is logged and swallowed rather
        than replacing that exception, and a request which is *cancelled*
        skips the report entirely, so a shutdown is not delayed by the length
        of a handler. Anything skipped stays queued for
        :meth:`handle_message`.

        The queue is bounded, and is cleared both by :meth:`close` and by a
        fresh :meth:`login`, so a rejection from a torn-down session is never
        reported against a new one. The log line written when each rejection is
        found is the complete record; this callback is the convenience.

        That matters most where a fallback covers for the stream. If a
        subscription quietly stops delivering, and a REST poll is authoritative
        anyway, nothing is wrong until something the poll does not cover
        finally breaks. A silent failure a fallback hides is the one worth
        having a signal for.

        ``error_handler`` is called as ``error_handler(service, exception,
        message)``:

        * ``service`` is the stream service the failure belongs to, or ``None``
          where it has none -- the close failure, or a message which named no
          service. **Do not discriminate on** ``service`` **and** ``message``
          **both being** ``None``. The close failure is the only report
          which leaves
          both unset by design, and every other site passes at least one:
          :class:`UnusableMessage` carries the containing frame and
          :class:`UnparsableMessage` carries the frame as it arrived, text
          or bytes. But that is a
          property of today's call sites, not a contract --- a category added
          later with nothing to say would join the signature for free.
          **Discriminate on the exception type.**
        * ``exception`` is the exception that was raised.
        * ``message`` is the message being handled, or ``None``.

        It may be a coroutine function, in which case it is awaited before the
        call which reported the failure continues. Unlike a coroutine stream
        handler, it is not scheduled as a task: a report which is awaited needs
        nothing keeping it alive at shutdown. The cost is that a slow error
        handler delays whatever was reporting to it, so keep it short --
        enqueue and return rather than doing work inline.

        If it raises an ``Exception``, that is logged and swallowed: a callback
        for absorbed failures must not become a way to fail. A ``BaseException``
        -- ``CancelledError`` during shutdown, or ``SystemExit`` -- is left to
        propagate, because swallowing those breaks cancellation and process
        exit, which are worse outcomes than the masking.

        Where it propagates *to* depends on which failure was being reported.
        Reporting a synchronous handler's failure happens inside
        ``handle_message``, so it reaches your receive loop. Reporting an
        asynchronous handler's happens inside that handler's own task, so it
        reaches whoever awaits the task, or the event loop's exception handler
        if nobody does. A ``SystemExit`` from an error handler therefore exits
        the process in the first case and not necessarily in the second.

        Handlers are called in registration order. With none registered,
        these failures are only logged.
        '''
        if not callable(error_handler):
            raise ValueError('error handler must be callable')

        # Checked here rather than discovered at report time. Every other
        # add_*_handler on this class takes a one-argument callback, so passing
        # one here is the natural mistake -- and it would raise TypeError on
        # every single report, inside the except clause that exists to stop an
        # error handler failing the stream. The result is a handler that never
        # runs, forever, with nothing but a log line to say so: the exact state
        # this callback is meant to replace.
        try:
            signature = inspect.signature(error_handler)
        except ValueError:
            # Some C-implemented callables and proxies have no retrievable
            # signature. Accepted rather than refused: the check exists to
            # catch a wrong-arity Python function, not to exclude anything it
            # cannot introspect.
            signature = None

        if signature is not None:
            try:
                signature.bind(None, None, None)
            except TypeError as exc:
                raise ValueError(
                        'error handler must accept three positional arguments '
                        '(service, exception, message); {!r} does not: '
                        '{}'.format(error_handler, exc)) from None

        self._error_handlers.append(error_handler)

    async def _report_error(self, exception, *, service=None, message=None):
        '''Delivers ``exception`` to every registered error handler.

        Alongside the logging rather than instead of it, so a caller who
        registers nothing still has the log line.

        Awaited rather than scheduled. A coroutine error handler therefore
        finishes before the call that reported the failure returns, which is
        what makes the report reliable without any machinery to keep the task
        alive: nothing has to be drained at shutdown, so nothing can deadlock
        draining it, time out draining it, or be cancelled half-drained.
        Scheduling them instead would need a drain at shutdown to keep them
        alive, and a drain can leak the socket, deadlock against itself or
        against a second closer, or skip the report it exists to protect.
        Awaiting needs none of that.
        '''
        for error_handler in self._error_handlers:
            try:
                result = error_handler(service, exception, message)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # Logged, not reported: reporting an error handler's failure
                # through the error handlers is a loop, and the whole point of
                # this callback is that it cannot take the stream down.
                self.logger.exception(
                        'Error handler raised while reporting an error. '
                        'Ignoring it.')

    def _on_handler_task_done(self, task):
        self._handler_tasks.discard(task)

    async def _run_handler(self, awaitable, service, payload):
        '''Awaits a coroutine handler and reports its failure from inside its
        own task.

        The alternative is a done callback, which is synchronous and so cannot
        await a coroutine error handler -- it has to schedule one, and then
        something has to keep that alive until it finishes. Reporting here
        keeps the report inside the task the caller can already see and wait
        for.
        '''
        try:
            await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error(
                    'Asynchronous stream handler raised an exception. The '
                    'message it was handling has been dropped.',
                    exc_info=exc)
            await self._report_error(exc, service=service, message=payload)
        except BaseException as exc:
            # Logged and re-raised, not absorbed. SystemExit and friends are
            # control flow rather than a stream failure, so they are not
            # reported to the error handlers -- but losing them silently would
            # make a handler calling sys.exit() a mystery, so they are logged
            # here.
            self.logger.error(
                    'Asynchronous stream handler raised %s. The message it '
                    'was handling has been dropped, and the exception is '
                    'being propagated.', _type_name(exc), exc_info=exc)
            raise

    async def _dispatch_to_handlers(self, service, msg, *, relabel):
        '''
        Delivers ``msg`` to every handler registered for ``service``.

        A handler which raises is logged and skipped: one misbehaving handler,
        or one message whose shape the library does not expect, must not prevent
        the other handlers from seeing the message, and must not escape into the
        caller's receive loop where it is indistinguishable from the stream
        having failed.

        Handlers may be synchronous or coroutine functions, and errors are
        reported the same way for both.
        '''
        try:
            handlers = self._handlers.get(service, ())
        except TypeError:
            # An unhashable service -- a list or a dict where a name belongs --
            # raises from the lookup itself, which is evaluated in the `for`
            # header and so sits outside the per-handler try below. Guarded
            # here rather than in each caller: this is the funnel they all go
            # through, and guarding one caller would leave the others
            # unguarded.
            self._absorb('a message whose service is not a name', service,
                         frame=msg)
            return

        if service == _INVALID_SERVICE:
            # Not a service Schwab added, so not reported as one: the warning
            # for a new service asks for an issue, and would send a reader
            # looking for something to support. Dropped and reported all the
            # same, since a handler watching the stream needs to know the
            # venue said something was wrong.
            self._absorb('a message naming Schwab\'s error "Invalid Service" '
                         'in place of a service, which Schwab has once sent '
                         'just before closing the connection',
                         service, frame=msg, service=service)
            return

        if service is not None and service not in _KNOWN_SERVICES:
            # Schwab added a service. There is no handler to route it to and
            # no field table to relabel it with, so the message is dropped --
            # which is the point: a dropped message is exactly what _absorb
            # is for, and going quiet here means a whole feed can appear
            # without anyone learning it exists.
            #
            # `service is not None` because a notify frame is not required to
            # name one, and that is documented rather than unexpected.
            #
            # The counted `what` is a fixed string with the name in the
            # offender, not in the key: `_absorbed_kinds` is a dict keyed on
            # `what`, and putting a value the venue chooses into that key
            # makes it grow without bound.
            # Named once per service beside the absorb, which coalesces
            # across every unknown service because its counter is keyed on
            # the fixed string above.
            first = self._report_new_shape('service', service)
            self._absorb('a message for a service this version does not know',
                         service, frame=msg, service=service, force=first)
            return

        relabel_failed = False

        for handler in handlers:
            try:
                # Relabeling reads into the message, so a message with an
                # unexpected shape fails here rather than in the handler.
                payload = handler.label_message(msg) if relabel else msg
            except Exception as exc:
                # Absorbed, not reported as a handler failure. Relabeling is
                # this library's work, not the caller's: reporting it as
                # "your handler raised" would blame the caller's code for a
                # shape the venue sent, once per registered handler, uncounted
                # and uncoalesced. Once per message, and only the
                # first handler pays for it.
                if not relabel_failed:
                    relabel_failed = True
                    self._absorb(
                            'a %s message which could not be relabeled'
                            % _printable(_safe_name(service), 64),
                            msg, frame=msg, service=service, cause=exc)
                continue

            try:
                result = handler(payload)
            except Exception as exc:
                self.logger.exception(
                        'Stream handler for service %s raised an exception. '
                        'The message it was handling has been dropped.',
                        _printable(_safe_name(service), 64))
                # payload, not msg: the handler was given the relabeled
                # message, so that is the one it failed on, and the one an
                # error handler reading fields by name can make sense of.
                await self._report_error(
                        exc, service=service, message=payload)
                continue

            # Check if the result is an awaitable, if so schedule it. This
            # allows for both sync and async handlers. It is wrapped so that a
            # failure is reported from inside the task rather than from a done
            # callback, which cannot await a coroutine error handler.
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(
                        self._run_handler(result, service, payload))
                self._handler_tasks.add(task)
                task.add_done_callback(self._on_handler_task_done)

    async def handle_message(self):
        '''
        Read one message and dispatch it to the handlers registered for it.

        Call it in a loop once logged in and subscribed. A response to a
        request that is in flight goes to the request waiting for it rather
        than being dispatched, so this keeps reading until it has a message to
        dispatch. A message no handler is registered for is ignored.

        A handler that raises an ``Exception`` does not stop the others or
        escape from here; see :ref:`error_handlers` for how to hear about it.
        What does escape: a failure of the connection itself;
        :class:`UnparsableMessage` for a frame that is not JSON; a
        ``ValueError`` if called before :meth:`login`; whatever a custom
        decoder raises other than ``ValueError`` or ``RecursionError``, which
        become :class:`UnparsableMessage`; and a ``BaseException``, such as a
        cancellation, from a synchronous handler or an error handler.
        '''
        # Read until something arrives which is actually ours. A response to a
        # request that is in flight is routed to whoever is waiting for it and
        # is not a message to be handled, so keep reading in that case rather
        # than returning nothing.
        while True:
            # Before the read, not after it. No lock is held here and no
            # deadline is running, which is what makes it safe to call user
            # code -- and draining after the read would hold every queued
            # report until the *next* message arrived, which on a quiet stream
            # is indefinitely. A matched frame read here returns None and comes
            # back round, so its report goes out on the following iteration.
            #
            # This is not the only drain. When a request is the reader, it
            # queues the report and this loop may already be parked in recv(),
            # so _request_response drains once it has released its locks.
            was_open = self._socket is not None
            await self._drain_pending_reports()

            # An error handler is allowed to tear the stream down -- that is a
            # reasonable reaction to a rejection, and every other path which
            # calls a handler returns rather than reading again afterwards.
            # Without this, closing from a queued report would be the one place
            # it raised "Socket not open" out of the call the handler was
            # running in.
            #
            # Conditioned on the socket having been open *before* the drain, so
            # this covers only a handler closing it. A client which was never
            # logged in, or which the caller closed itself, still raises
            # "Socket not open" from the read below rather than returning
            # quietly for one iteration.
            if was_open and self._socket is None:
                return

            try:
                async with self._read_lock:
                    try:
                        msg = await self._read_and_route()
                    except BaseException as exc:
                        # A read failure is also a failure for any request
                        # waiting on a response, which will otherwise wait for
                        # a reply that can no longer arrive.
                        self._fail_pending_request(exc)
                        raise
            except UnparsableMessage as exc:
                # Outside the `async with`, so the read lock is released before
                # any user code runs.
                await self._report_unparsable(exc)
                raise
            except ConnectionClosed:
                # Likewise outside it: a close handler may log in again.
                await self._run_close_handlers()
                raise

            if msg is not ROUTED:
                break

        # Every read below assumes a mapping. `'data' in msg` tolerates a
        # top-level JSON array or string; `msg.get('data')` does not, so a
        # non-dict frame would raise. Guarded once, here, rather than at each
        # read.
        if not _is_mapping(msg):
            self._absorb('a message which is not an object', msg)
            return

        try:
            unknown_channels = set(msg) - _KNOWN_CHANNELS
        except Exception:
            # `_is_mapping` is structural on purpose -- `get` and
            # `__contains__`, nothing about iteration -- so a decoder may
            # hand back a mapping this cannot enumerate, or one whose keys
            # are unhashable. Skipping the check loses only the
            # unknown-channel report; letting it out would end the receive
            # loop with a non-SchwabError over a documented extension point.
            unknown_channels = set()
        if unknown_channels:
            # A whole compartment this version does not read. Every message in
            # it is being dropped, which is a bigger thing than an unnamed
            # field and deserves the same treatment as any other message this
            # client cannot use.
            #
            # Reported and then carried on past: the channels that *are*
            # understood still hold real data, and refusing the frame over an
            # unread compartment would turn an addition into an outage.
            names = sorted(_safe_name(c) for c in unknown_channels)
            first = any([self._report_new_shape('channel', n)
                         for n in names])
            self._absorb(
                    'a frame carrying a channel this version does not read',
                    names, frame=msg, force=first)

        # response
        if 'response' in msg:
            # A response with nothing waiting for it is not a reason to end the
            # caller's receive loop. It happens whenever a request was
            # abandoned -- cancelled, or given up on -- and the server answered
            # afterwards, and the answer is frequently a successful one. Ending
            # the session over a late acknowledgement of something that worked
            # loses every message queued behind it, and does so precisely when
            # the server is slow, which is when a stream is least worth
            # dropping. Log it and carry on.
            for response, code, content in self._iter_responses(msg):
                notice = self._notice_from(response, content)
                if notice is not None:
                    await self._report_notice(notice, response)
                    continue
                # A late acknowledgement of something that worked is routine.
                # A late *rejection* is not: the request was abandoned, so
                # nothing else will ever report that Schwab refused it, and at
                # INFO it would not be seen at all. Losing the raise is right;
                # losing the news is not.
                log = (self.logger.info if _is_success_code(code)
                       else self.logger.warning)

                log('Received a response to %s/%s with no request '
                    'outstanding: code %s, msg %s. Ignoring it.',
                    _safe_value(response.get('service')),
                    _safe_value(response.get('command')),
                    _safe_value(code), _safe_value(content.get('msg')))

                # A late rejection is reported, a late success is not. This is
                # the third kind of absorbed failure: the request it answers
                # was abandoned, so nothing else will ever say Schwab refused
                # it. A caller who registered an error handler to replace
                # scraping the log would otherwise stop seeing exactly that.
                # `is not None` as well: a response with no code at all is
                # neither a late rejection nor a late success, and reporting it
                # as a rejection pages someone over `code None, msg None`.
                if code is not None and not _is_success_code(code):
                    # msg, not response: _validate_response builds this same
                    # exception from the whole frame, and handing it a single
                    # element would make one type mean two shapes -- the
                    # KeyError that followed would be swallowed by
                    # _report_error's except clause, leaving nothing but a log
                    # line, which is what this callback exists to replace.
                    #
                    # The frame can carry several responses and this reports
                    # per element, so exc.response['response'][0] is not
                    # necessarily the one that was rejected. The rejected
                    # element is what reaches the handler as `message`.
                    await self._report_error(
                            UnexpectedResponseCode(
                                msg,
                                'Schwab rejected a request which had already '
                                'been abandoned: code {}, msg {}'.format(
                                    _safe_value(code),
                                    _safe_value(content.get('msg'))),
                                service=response.get('service'),
                                command=response.get('command'), code=code,
                                msg=content.get('msg')),
                            service=response.get('service'),
                            message=response)
            return

        # data
        for d in self._iter_channel(msg, 'data'):
            notice = self._notice_from(d, d.get('content'))
            if notice is not None:
                await self._report_notice(notice, d)
                continue
            await self._dispatch_to_handlers(
                    d.get('service'), d, relabel=True)

        # notify
        for d in self._iter_channel(msg, 'notify'):
            if 'heartbeat' in d:
                await self._run_heartbeat_handlers(d.get('heartbeat'))
                continue
            notice = self._notice_from(d, d.get('content'))
            if notice is not None:
                await self._report_notice(notice, d)
            else:
                # Not every notify message is guaranteed to name a service,
                # and one that does not must not raise out of here.
                await self._dispatch_to_handlers(
                        d.get('service'), d, relabel=False)

    ##########################################################################
    # LOGIN

    async def login(self, websocket_connect_args=None):
        '''

        Performs initial stream setup:
         * Fetches streaming information from the HTTP client's
           :meth:`~schwaby.client.Client.get_user_preferences` method
         * Initializes the socket
         * Builds and sends and authentication request
         * Waits for response indicating login success

        All stream operations are available after this method completes.

        :param websocket_connect_args: ``dict`` of additional arguments to pass
                                       to the websocket ``connect`` call. Useful
                                       for setting timeouts and other connection
                                       parameters. See `the official
                                       documentation <https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html#websockets.asyncio.client.connect>`__
                                       for details.

                                       Note that websockets 14.0 renamed
                                       ``extra_headers`` to
                                       ``additional_headers`` and removed
                                       ``create_protocol`` and ``read_limit``.
                                       Passing any of the three raises
                                       ``ValueError``: for ``extra_headers``
                                       naming the replacement, and for the
                                       other two saying there is none.
        '''

        # A new connection: nothing has closed on it yet.
        self.last_close = None
        self._close_found = False
        self._close_due = False
        self._last_notice = None

        # Fetch required data and initialize the client
        try:
            r = self._client.get_user_preferences()

            # We don't actually know whether the client is synchronous or
            # asynchronous, so work around by awaiting the response if
            # necessary
            if inspect.iscoroutine(r):
                r = await r
        except TokenRefreshError as e:
            await self._login_failed(
                    StreamCloseKind.DEAD_TOKEN
                    if e.kind == TokenErrorKind.DEAD_TOKEN
                    else StreamCloseKind.CONNECTION_LOST)
            raise
        except Exception:
            await self._login_failed(StreamCloseKind.CONNECTION_LOST)
            raise
        # Not an assert, which -O strips. A refusal is the HTTPStatusError it
        # always was; a success other than 200 is not one this reads.
        if not 200 <= r.status_code < 300:
            await self._login_failed(StreamCloseKind.CONNECTION_LOST)
            r.raise_for_status()
        if r.status_code != httpx2.codes.OK:
            await self._login_failed(StreamCloseKind.CONNECTION_LOST)
            raise UnexpectedResponse(
                    r, 'the preferences call answered {}, not 200'.format(
                        r.status_code))
        r = r.json()

        await self._init_from_preferences(
                r, websocket_connect_args if websocket_connect_args else {})

        # Build and send the request object
        _redact.register(self._client.token_metadata.token['access_token'],
                         'ACCESS_TOKEN')
        request_parameters = {
                'Authorization': self._client.token_metadata.token['access_token'],
                'SchwabClientChannel': self._stream_channel,
                'SchwabClientFunctionId': self._stream_function_id,
        }

        request, request_id = self._make_request(
            service='ADMIN', command='LOGIN',
            parameters=request_parameters)
        try:
            await self._request_response(request, request_id, 'ADMIN',
                                         'LOGIN')
        except UnexpectedResponseCode as e:
            # Any refusal of the LOGIN: the socket is closed first.
            try:
                await self.close()
            except Exception:
                self.logger.exception('Closing a refused login\'s socket '
                                      'failed. Ignoring it.')
            await self._login_failed(StreamCloseKind.LOGIN_REFUSED)
            raise StreamLoginRefused(
                    e.response, *e.args, service=e.service,
                    command=e.command, code=e.code, msg=e.msg) from None

    ##########################################################################
    # LOGOUT

    async def logout(self):
        '''
        Performs a logout operation on the stream and closes the underlying
        connection. After this method is called, no further stream operations
        are possible. The client must be re-initialized with :meth:`login` to
        perform further operations.
        '''
        request, request_id = self._make_request(
            service='ADMIN', command='LOGOUT',
            parameters={})
        try:
            await self._request_response(
                    request, request_id, 'ADMIN', 'LOGOUT')
        finally:
            # The stream is unusable after a logout whether or not the venue
            # acknowledged it, so the socket is closed either way. A failure to
            # close is logged rather than raised: it must not replace whatever
            # went wrong with the logout itself, which is the more useful error.
            try:
                await self.close()
            except Exception as exc:
                self.logger.exception(
                        'Failed to close the stream connection after logout.')
                try:
                    await self._report_error(exc)
                except asyncio.CancelledError:
                    # Not swallowed with the rest. Discarding a cancellation
                    # makes logout() refuse to die -- a supervisor or TaskGroup
                    # calling cancel() during shutdown would find this task
                    # running to completion regardless, which is a worse
                    # outcome than the masking the guard below prevents.
                    raise
                except BaseException:
                    # BaseException is deliberately left to propagate out of
                    # _report_error everywhere else. Not here: this is the
                    # finally clause whose whole point is that a close failure
                    # must not replace whatever went wrong with the logout, and
                    # an error handler raising SystemExit would do exactly
                    # that. Reporting is the least important thing happening
                    # here.
                    self.logger.exception(
                            'Error handler raised while reporting a close '
                            'failure. Ignoring it.')


    async def close(self):
        '''
        Closes the connection to the streaming server without logging out.

        Safe to call more than once, and safe to call on a client which was
        never logged in. Prefer :meth:`logout` where the stream is still
        healthy; this exists for shutting down a client whose connection has
        already failed, or which is being torn down without ceremony.

        The client must be re-initialized with :meth:`login` to perform further
        operations.
        '''
        socket, self._socket = self._socket, None

        # Anything still queued belongs to the session being torn down. Its
        # exception carries a frame from that connection, so delivering it
        # after a later login() would report a dead session's rejection against
        # a live one. Each was logged when it was found, so nothing unwritten
        # is lost.
        #
        # _overflow_items for the same reason: it holds frames read but not
        # yet handled, including the late rejections the orphan path reports
        # and data frames handlers would be given. Clearing only the reports
        # would let the standalone framing leak across sessions while the
        # batched one does not.
        self._pending_reports.clear()
        self._overflow_items.clear()
        self._absorbed = 0
        self._absorbed_kinds.clear()

        if socket is not None:
            await socket.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    ##########################################################################
    # ACCT_ACTIVITY

    class AccountActivityFields(_BaseFieldEnum):
        '''

        Data fields for equity account activity. Primarily an implementation detail
        and not used in client code. Provided here as documentation for key
        values stored returned in the stream messages.
        '''

        #: Passed back to the client from the request to identify a subscription this response belongs to.
        SUBSCRIPTION_KEY = 0

        #: Account Number that the activity occurred on.
        ACCOUNT = 1

        #: Message Type that dictates the format of the Message Data field.
        MESSAGE_TYPE = 2

        #: The core data for the message. Either JSON-formatted data describing the update, NULL in some cases,
        #: or plain text in case of ERROR.
        MESSAGE_DATA = 3

        @classmethod
        def relabel_message(cls, old_msg, new_msg):
            super().relabel_message(old_msg, new_msg)
            # Relabeling has just put the type under its name. A type outside
            # the observed vocabulary is still delivered -- this only says so,
            # once, so that a type nobody has captured becomes known rather
            # than passing silently. It runs on the data channel only, which is
            # the only place the type is relabeled at all.
            token = new_msg.get('MESSAGE_TYPE')
            if token is not None:
                known = StreamClient._ACCOUNT_ACTIVITY_MESSAGE_TYPES_FOLDED
                name = _safe_name(token)
                # An empty type carrying a notice written as text is not a type
                # nobody has captured: Schwab's "Feature not supported" notice
                # arrives that way, as the streaming documentation records.
                # Reporting it would ask for an issue about a documented shape,
                # in every process that received it, and teach an operator to
                # acknowledge the one warning that has to be read when a real
                # new type comes. An empty type carrying a JSON object is not
                # that shape, and is still reported.
                notice = not name and _is_notice_text(
                        new_msg.get('MESSAGE_DATA'))
                if not notice and name.casefold() not in known:
                    _report_unknown_message_type(name)

    #: ``MESSAGE_TYPE`` values observed on a live ``ACCT_ACTIVITY`` feed, as
    #: they appear on the wire.
    #:
    #: A reference for noticing a type nobody has seen before, **not** a
    #: list to classify messages against. Schwab documents this vocabulary
    #: nowhere, and only these have been captured, so an exact match
    #: against it will miss a real message whose type has simply never been
    #: recorded. Classify by substring and case-insensitively, as the
    #: streaming documentation describes -- remembering that ``CANCEL`` and
    #: ``UROUT`` also match the items retiring the old id of a price change,
    #: on the option orders captured, while the order goes on working under a
    #: new one -- and use this only to ask whether a type is one anyone has
    #: seen.
    #:
    #: A type outside it is still delivered to your handler, and is logged once
    #: on ``schwaby.streaming``. An empty type carrying a notice written as
    #: text, the way the streaming documentation records Schwab's "Feature not
    #: supported" notice arriving, is not logged; one carrying a JSON object
    #: is. A buy rejected for buying power and a price change to a working
    #: order have both been captured using only types listed here; expiry and
    #: partial fill have not, so the first of each a process receives may log
    #: once. That is expected, and a report of the type it names is how this
    #: list grows.
    #:
    #: Held on ``StreamClient`` rather than on ``AccountActivityFields``,
    #: because a set in an ``Enum`` body silently becomes a member, which would
    #: add a fifth field and change ``key_mapping`` for every such message.
    ACCOUNT_ACTIVITY_MESSAGE_TYPES = frozenset((
        'SUBSCRIBED',
        'OrderCreated', 'OrderAccepted',
        'ExecutionRequested', 'ExecutionRequestCreated',
        'ExecutionRequestCompleted', 'OrderFillCompleted',
        'CancelAccepted', 'ExecutionCreated', 'OrderUROutCompleted',
        'ChangeCreated', 'ChangeAccepted',
        'OrderMonitorCreated', 'OrderMonitorCompleted',
    ))

    # Folded once, from the public set, so a case variant of an observed type
    # is not reported as new -- the documentation already says to match
    # case-insensitively -- while a genuinely different token, such as the
    # truncation ``ORDERUROUT``, still is.
    _ACCOUNT_ACTIVITY_MESSAGE_TYPES_FOLDED = frozenset(
        message_type.casefold()
        for message_type in ACCOUNT_ACTIVITY_MESSAGE_TYPES)

    async def account_activity_sub(self):
        '''

        Subscribe to account activity for the account id associated with this
        streaming client. See :class:`AccountActivityFields` for more info.
        '''
        await self._service_op(
            [self._stream_correl_id], 'ACCT_ACTIVITY', 'SUBS',
            self.AccountActivityFields)

    async def account_activity_unsubs(self):
        '''

        Un-Subscribe to account activity for the account id associated with this
        streaming client. See :class:`AccountActivityFields` for more info.
        '''
        await self._service_op([self._stream_correl_id], 'ACCT_ACTIVITY', 'UNSUBS')

    def add_account_activity_handler(self, handler):
        '''
        Adds a handler to the account activity subscription. See
        :ref:`registering_handlers` for details.
        '''
        self._handlers['ACCT_ACTIVITY'].append(_Handler(handler,
                                                        self.AccountActivityFields))

    ##########################################################################
    # CHART_EQUITY

    class ChartEquityFields(_BaseFieldEnum):
        '''

        Data fields for equity OHLCV data. Primarily an implementation detail
        and not used in client code. Provided here as documentation for key
        values stored returned in the stream messages.

        Schwab's published table for this service puts the sequence number
        last and the prices one place earlier. The wire order is the one
        here, measured against a live account: the sequence arrives at ``1``
        and the bar's prices and volume at ``2`` to ``6``. Fields ``7`` and
        ``8`` agree with the table.
        '''

        #: Ticker symbol
        SYMBOL = 0

        #: Sequence number, and really one: across five consecutive bars for
        #: two symbols on a live account it rose by exactly one per minute
        #: bar and carried the same value for every symbol in a frame, so a
        #: value missing for one symbol is a bar that symbol did not get.
        #: Where it starts, and whether it resets, was not measured. Unlike
        #: the book's
        #: :attr:`StreamClient.PerExchangeBidFields.QUOTE_TIME`, which is not
        #: a sequence number, this one is what its name says.
        SEQUENCE = 1

        #: Opening price for the minute
        OPEN_PRICE = 2

        #: Highest price for the minute
        HIGH_PRICE = 3

        #: Lowest price for the minute
        LOW_PRICE = 4

        #: Closing price for the minute. Not the previous day's close:
        #: measured, it changed from one bar to the next within a session.
        CLOSE_PRICE = 5

        #: Volume for the minute. Not the day's: measured, consecutive bars
        #: carried 933, 636 and 140 rather than a rising total.
        VOLUME = 6

        #: When the bar starts, in milliseconds since the epoch. Measured:
        #: consecutive values were sixty thousand apart, on the minute.
        CHART_TIME_MILLIS = 7

        #: The bar's date, in days since the epoch --- not a day of the year
        #: or a weekday. Measured: ``20713`` on a frame from the day that
        #: many days after 1 January 1970.
        CHART_DAY = 8

    async def chart_equity_subs(self, symbols, *, keys_per_request=None):
        '''

        Subscribe to equity charts. Behavior is undefined if called multiple
        times.

        :param symbols: Equity symbols to subscribe to.'''
        await self._service_op(
            symbols, 'CHART_EQUITY', 'SUBS', self.ChartEquityFields,
            fields=self.ChartEquityFields.all_fields(),
            keys_per_request=keys_per_request)

    async def chart_equity_unsubs(self, symbols, *, keys_per_request=None):
        '''

        Un-Subscribe to equity charts. Behavior is undefined if called multiple
        times.

        :param symbols: Equity symbols to subscribe to.'''
        await self._service_op(symbols, 'CHART_EQUITY', 'UNSUBS',
            keys_per_request=keys_per_request)

    async def chart_equity_add(self, symbols, *, keys_per_request=None):
        '''

        Add a symbol to the equity charts subscription. Behavior is undefined
        if called before :meth:`chart_equity_subs`.

        :param symbols: Equity symbols to add to the subscription.
        '''
        await self._service_op(
            symbols, 'CHART_EQUITY', 'ADD', self.ChartEquityFields,
            fields=self.ChartEquityFields.all_fields(),
            keys_per_request=keys_per_request)

    def add_chart_equity_handler(self, handler):
        '''
        Adds a handler to the equity chart subscription. See
        :ref:`registering_handlers` for details.
        '''
        self._handlers['CHART_EQUITY'].append(_Handler(handler,
                                                       self.ChartEquityFields))

    ##########################################################################
    # CHART_FUTURES

    class ChartFuturesFields(_BaseFieldEnum):
        '''

        Data fields for equity OHLCV data. Primarily an implementation detail
        and not used in client code. Provided here as documentation for key
        values stored returned in the stream messages.
        '''

        #: Ticker symbol in upper case.
        SYMBOL = 0

        #: Milliseconds since Epoch
        CHART_TIME_MILLIS = 1

        #: Opening price for the minute
        OPEN_PRICE = 2

        #: Highest price for the minute
        HIGH_PRICE = 3

        #: Chart's lowest price for the minute
        LOW_PRICE = 4

        #: Closing price for the minute
        CLOSE_PRICE = 5

        #: Total volume for the minute
        VOLUME = 6

    async def chart_futures_subs(self, symbols, *, keys_per_request=None):
        '''

        Subscribe to futures charts. Behavior is undefined if called multiple
        times.

        :param symbols: Futures symbols to subscribe to.
        '''
        await self._service_op(
            symbols, 'CHART_FUTURES', 'SUBS', self.ChartFuturesFields,
            fields=self.ChartFuturesFields.all_fields(),
            keys_per_request=keys_per_request)

    async def chart_futures_unsubs(self, symbols, *, keys_per_request=None):
        '''

        Un-Subscribe to futures charts. Behavior is undefined if called multiple
        times.

        :param symbols: Futures symbols to subscribe to.
        '''
        await self._service_op(symbols, 'CHART_FUTURES', 'UNSUBS',
            keys_per_request=keys_per_request)

    async def chart_futures_add(self, symbols, *, keys_per_request=None):
        '''

        Add a symbol to the futures chart subscription. Behavior is undefined
        if called before :meth:`chart_futures_subs`.

        :param symbols: Futures symbols to add to the subscription.
        '''
        await self._service_op(
            symbols, 'CHART_FUTURES', 'ADD', self.ChartFuturesFields,
            fields=self.ChartFuturesFields.all_fields(),
            keys_per_request=keys_per_request)

    def add_chart_futures_handler(self, handler):
        '''
        Adds a handler to the futures chart subscription. See
        :ref:`registering_handlers` for details.
        '''
        self._handlers['CHART_FUTURES'].append(_Handler(handler,
                                                        self.ChartFuturesFields))

    ##########################################################################
    # LEVELONE_EQUITIES

    class LevelOneEquityFields(_BaseFieldEnum):
        '''

        Fields for equity quotes.
        '''

        #: Ticker symbol
        SYMBOL = 0

        #: Bid price
        BID_PRICE = 1

        #: Ask price
        ASK_PRICE = 2

        #: Last trade price
        LAST_PRICE = 3

        #: Size of the highest bid, in shares rather than round lots.
        #: :ref:`Unconfirmed <confidence_tags>`: inferred from magnitude on
        #: liquid names, where reading it as lots would put implausibly deep
        #: books at the touch. It has not been checked against a quantity of
        #: known size; a resting round-lot order joining the best bid
        #: would settle it.
        BID_SIZE = 4

        #: Size of the lowest ask, in shares rather than round lots.
        #: :ref:`Unconfirmed <confidence_tags>`, for the reason given on
        #: ``BID_SIZE``.
        ASK_SIZE = 5

        #: Exchange ID of the lowest ask
        ASK_ID = 6

        #: Exchange ID of the highest bid
        BID_ID = 7

        #: Total volume trade to date, in whole shares: a fraction is dropped.
        #: See ``TOTAL_VOLUME_FRACTIONAL``.
        TOTAL_VOLUME = 8

        #: Size of the last trade, in whole shares: a fraction is dropped, so
        #: a print under one share reads ``0``. See ``LAST_SIZE_FRACTIONAL``.
        LAST_SIZE = 9

        #: Daily high price
        HIGH_PRICE = 10

        #: Daily low price
        LOW_PRICE = 11

        #: Previous close price
        CLOSE_PRICE = 12

        #: Exchange ID
        EXCHANGE_ID = 13

        #: Is this equity marginable?
        MARGINABLE = 14

        #: Description
        DESCRIPTION = 15

        #: Exchange ID of the last trade
        LAST_ID = 16

        #: Today's open price
        OPEN_PRICE = 17

        #: Net change
        NET_CHANGE = 18

        #: 52 week high price
        HIGH_PRICE_52_WEEK = 19

        #: 52 week low price
        LOW_PRICE_52_WEEK = 20

        #: P/E ratio
        PE_RATIO = 21

        #: Dividend amount
        DIVIDEND_AMOUNT = 22

        #: Dividend yield
        DIVIDEND_YIELD = 23

        #: ETF net asset value
        NAV = 24

        #: Exchange name
        EXCHANGE_NAME = 25

        #: Dividend date
        DIVIDEND_DATE = 26

        #: Is this a regular market quote?
        REGULAR_MARKET_QUOTE = 27

        #: Is this a regular market trade?
        REGULAR_MARKET_TRADE = 28

        #: Regular market last price
        REGULAR_MARKET_LAST_PRICE = 29

        #: Regular market last size
        REGULAR_MARKET_LAST_SIZE = 30

        #: Regular market net change
        REGULAR_MARKET_NET_CHANGE = 31

        #: Security status
        SECURITY_STATUS = 32

        #: Mark
        MARK = 33

        #: Quote time in milliseconds
        QUOTE_TIME_MILLIS = 34

        #: Last trade time in milliseconds
        TRADE_TIME_MILLIS = 35

        #: Regular market trade time in milliseconds
        REGULAR_MARKET_TRADE_MILLIS = 36

        #: Bid time in millis
        BID_TIME_MILLIS = 37

        #: Ask time in millis
        ASK_TIME_MILLIS = 38

        #: Ask MIC ID
        ASK_MIC_ID = 39

        #: Bid MIC ID
        BID_MIC_ID = 40

        #: Last trade MIC ID
        LAST_MIC_ID = 41

        #: Net change in percent
        NET_CHANGE_PERCENT = 42

        #: Regular market change in percent
        REGULAR_MARKET_CHANGE_PERCENT = 43

        #: Mark change
        MARK_CHANGE = 44

        #: Mark change in percent
        MARK_CHANGE_PERCENT = 45

        #: HTB quantity
        HTB_QUANTITY = 46

        #: HTB rate
        HTB_RATE = 47

        #: Is this equity hard to borrow?
        HARD_TO_BORROW = 48

        #: Is this equity shortable
        IS_SHORTABLE = 49

        #: Post market net change
        POST_MARKET_NET_CHANGE = 50

        #: Post market net change percent
        POST_MARKET_NET_CHANGE_PERCENT = 51

        # Schwab's field table stops at 51. The fields below were delivered
        # to a live account asked for more, and are named from what they
        # carried; see :ref:`level_one_equity_fractional`.

        #: Size of the last trade, with any fraction of a share. Its whole
        #: part equalled ``LAST_SIZE`` on every item carrying both, and a
        #: print of less than one share is ``0`` in ``LAST_SIZE`` and the
        #: fraction here.
        LAST_SIZE_FRACTIONAL = 52

        #: Meaning not established. Seen only in the items sent on
        #: subscribing, never in an update, as an integer roughly two to
        #: forty-five times that item's ``TOTAL_VOLUME``.
        UNKNOWN_53 = 53

        #: Total volume traded to date, with any fraction of a share. Its
        #: whole part equalled ``TOTAL_VOLUME`` on every item carrying both.
        TOTAL_VOLUME_FRACTIONAL = 54

        #: Equalled ``BID_SIZE`` on every item carrying both, and was always a
        #: whole number. That it carries a fraction is
        #: :ref:`Unconfirmed <confidence_tags>`: the name follows fields
        #: ``52`` and ``54``.
        BID_SIZE_FRACTIONAL = 55

        #: Equalled ``ASK_SIZE`` on every item carrying both, and was always a
        #: whole number. :ref:`Unconfirmed <confidence_tags>`, for the reason
        #: given on ``BID_SIZE_FRACTIONAL``.
        ASK_SIZE_FRACTIONAL = 56

    async def level_one_equity_subs(self, symbols, *, fields=None,
                                    keys_per_request=None):
        '''

        Subscribe to level one equity quote data.

        :param symbols: Equity symbols to receive quotes for
        :param fields: Iterable of :class:`LevelOneEquityFields` representing
                       the fields to return in streaming entries. If unset, all
                       fields will be requested.
        '''
        if fields and self.LevelOneEquityFields.SYMBOL not in fields:
            fields = list(fields) + [self.LevelOneEquityFields.SYMBOL]
        await self._service_op(
            symbols, 'LEVELONE_EQUITIES', 'SUBS', self.LevelOneEquityFields,
            fields=fields,
            keys_per_request=keys_per_request)

    async def level_one_equity_unsubs(self, symbols, *, keys_per_request=None):
        '''

        Un-Subscribe to level one equity quote data.

        :param symbols: Equity symbols to receive quotes for
        '''

        await self._service_op(symbols, 'LEVELONE_EQUITIES', 'UNSUBS',
            keys_per_request=keys_per_request)

    async def level_one_equity_add(self, symbols, *, fields=None,
                                   keys_per_request=None):
        '''

        Add symbols to the list to receive quotes for.

        :param symbols: Equity symbols to receive quotes for
        :param fields: Iterable of :class:`LevelOneEquityFields` representing
                       the fields to return in streaming entries. If unset, all
                       fields will be requested.
        '''
        if fields and self.LevelOneEquityFields.SYMBOL not in fields:
            fields = list(fields) + [self.LevelOneEquityFields.SYMBOL]
        await self._service_op(
            symbols, 'LEVELONE_EQUITIES', 'ADD',
            self.LevelOneEquityFields, fields=fields,
            keys_per_request=keys_per_request)

    async def level_one_equity_view(self, fields):
        '''
        Ask Schwab to change the fields delivered for every symbol in the
        level one equity quotes subscription, without resubscribing.

        This applies to the whole subscription, not to particular symbols:
        Schwab documents a VIEW as applying to every symbol of the service,
        and that was measured on futures and forex, though not on this
        service. So this method takes no symbols. See
        :ref:`the note on VIEW <level_one_view>`.

        :param fields: Iterable of :class:`LevelOneEquityFields`
                       to deliver from now on. ``SYMBOL`` is added if
                       missing.
        '''
        await self._view_op(
            'LEVELONE_EQUITIES', self.LevelOneEquityFields, fields)

    def add_level_one_equity_handler(self, handler):
        '''
        Register a function to handle level one equity quotes as they are sent.
        See :ref:`registering_handlers` for details.
        '''
        self._handlers['LEVELONE_EQUITIES'].append(
                _Handler(handler, self.LevelOneEquityFields))

    ##########################################################################
    # LEVELONE_OPTIONS

    class LevelOneOptionFields(_BaseFieldEnum):
        '''
        '''

        #: Option symbol
        SYMBOL = 0

        #: Description
        DESCRIPTION = 1

        #: Highest bid price
        BID_PRICE = 2

        #: Lowest ask price
        ASK_PRICE = 3

        #: Last trade price
        LAST_PRICE = 4

        #: Today's high price
        HIGH_PRICE = 5

        #: Today's low price
        LOW_PRICE = 6

        #: Last close price
        CLOSE_PRICE = 7

        #: Today's total volume
        TOTAL_VOLUME = 8

        #: Open interest
        OPEN_INTEREST = 9

        #: Volatility
        VOLATILITY = 10

        #: Money intrinsic value
        MONEY_INTRINSIC_VALUE = 11

        #: Expiration year
        EXPIRATION_YEAR = 12

        #: Multiplier
        MULTIPLIER = 13

        #: Digits
        DIGITS = 14

        #: Open price
        OPEN_PRICE = 15

        #: Highest bid size
        BID_SIZE = 16

        #: Lowest ask size
        ASK_SIZE = 17

        #: Last trade size
        LAST_SIZE = 18

        #: Net change
        NET_CHANGE = 19

        #: Contract strike price
        STRIKE_PRICE = 20

        #: Contract type
        CONTRACT_TYPE = 21

        #: Underlying symbol
        UNDERLYING = 22

        #: Expiration month
        EXPIRATION_MONTH = 23

        #: Deliverables
        DELIVERABLES = 24

        #: Time value
        TIME_VALUE = 25

        #: Expiration day
        EXPIRATION_DAY = 26

        #: Days to expiration
        DAYS_TO_EXPIRATION = 27

        #: Delta
        DELTA = 28

        #: Gamma
        GAMMA = 29

        #: Theta
        THETA = 30

        #: Vega
        VEGA = 31

        #: Rho
        RHO = 32

        #: Security status
        SECURITY_STATUS = 33

        #: Theoretical option value
        THEORETICAL_OPTION_VALUE = 34

        #: Underlying price
        UNDERLYING_PRICE = 35

        #: UV expiration type
        UV_EXPIRATION_TYPE = 36

        #: Mark
        MARK = 37

        #: Quote time in millis
        QUOTE_TIME_MILLIS = 38

        #: Last trade time in millis
        TRADE_TIME_MILLIS = 39

        #: Exchange ID
        EXCHANGE_ID = 40

        #: Exchange name
        EXCHANGE_NAME = 41

        #: Last trading day
        LAST_TRADING_DAY = 42

        #: Settlement type
        SETTLEMENT_TYPE = 43

        #: Net percent change
        NET_PERCENT_CHANGE = 44

        #: Mark change
        MARK_CHANGE = 45

        #: Mark change in percent
        MARK_CHANGE_PERCENT = 46

        #: Implied yield
        IMPLIED_YIELD = 47

        #: Is penny stock?
        IS_PENNY = 48

        #: Option root
        OPTION_ROOT = 49

        #: 52 week high price
        HIGH_PRICE_52_WEEK = 50

        #: 52 week low price
        LOW_PRICE_52_WEEK = 51

        #: Indicative asking price
        INDICATIVE_ASKING_PRICE = 52

        #: Indicative bid price
        INDICATIVE_BID_PRICE = 53

        #: Indicative quote time
        INDICATIVE_QUOTE_TIME = 54

        #: Exercise type
        EXERCISE_TYPE = 55

    async def level_one_option_subs(self, symbols, *, fields=None,
                                    keys_per_request=None):
        '''

        Subscribe to level one option quote data.

        :param symbols: Option symbols to receive quotes for
        :param fields: Iterable of :class:`LevelOneOptionFields` representing
                       the fields to return in streaming entries. If unset, all
                       fields will be requested.
        '''
        if fields and self.LevelOneOptionFields.SYMBOL not in fields:
            fields = list(fields) + [self.LevelOneOptionFields.SYMBOL]
        await self._service_op(
            symbols, 'LEVELONE_OPTIONS', 'SUBS', self.LevelOneOptionFields,
            fields=fields,
            keys_per_request=keys_per_request)

    async def level_one_option_unsubs(self, symbols, *, keys_per_request=None):
        '''

        Un-Subscribe to level one option quote data.

        :param symbols: Option symbols to receive quotes for
        '''
        await self._service_op(symbols, 'LEVELONE_OPTIONS', 'UNSUBS',
            keys_per_request=keys_per_request)

    async def level_one_option_add(self, symbols, *, fields=None,
                                   keys_per_request=None):
        '''

        Add symbols to the list to receive quotes for.

        :param symbols: Option symbols to add to list to receive quotes for
        :param fields: Iterable of :class:`LevelOneOptionFields` representing
                       the fields to return in streaming entries. If unset, all
                       fields will be requested.
        '''
        if fields and self.LevelOneOptionFields.SYMBOL not in fields:
            fields = list(fields) + [self.LevelOneOptionFields.SYMBOL]
        await self._service_op(
            symbols, 'LEVELONE_OPTIONS', 'ADD',
            self.LevelOneOptionFields, fields=fields,
            keys_per_request=keys_per_request)

    async def level_one_option_view(self, fields):
        '''
        Ask Schwab to change the fields delivered for every symbol in the
        level one option quotes subscription, without resubscribing.

        This applies to the whole subscription, not to particular symbols:
        Schwab documents a VIEW as applying to every symbol of the service,
        and that was measured on futures and forex, though not on this
        service. So this method takes no symbols. See
        :ref:`the note on VIEW <level_one_view>`.

        :param fields: Iterable of :class:`LevelOneOptionFields`
                       to deliver from now on. ``SYMBOL`` is added if
                       missing.
        '''
        await self._view_op(
            'LEVELONE_OPTIONS', self.LevelOneOptionFields, fields)

    def add_level_one_option_handler(self, handler):
        '''
        Register a function to handle level one options quotes as they are sent.
        See :ref:`registering_handlers` for details.
        '''
        self._handlers['LEVELONE_OPTIONS'].append(
                _Handler(handler, self.LevelOneOptionFields))

    ##########################################################################
    # LEVELONE_FUTURES

    class LevelOneFuturesFields(_BaseFieldEnum):
        '''
        '''

        #: Ticker symbol in upper case.
        SYMBOL = 0

        #: Current Best Bid Price
        BID_PRICE = 1

        #: Current Best Ask Price
        ASK_PRICE = 2

        #: Price at which the last trade was matched
        LAST_PRICE = 3

        #: Number of contracts for bid
        BID_SIZE = 4

        #: Number of contracts for ask
        ASK_SIZE = 5

        #: Exchange with the best bid
        BID_ID = 6

        #: Exchange with the best ask
        ASK_ID = 7

        #: Aggregated contracts traded throughout the day, including pre/post market hours.
        TOTAL_VOLUME = 8

        #: Number of contracts traded with last trade
        LAST_SIZE = 9

        #: Time of the last quote in milliseconds since epoch
        QUOTE_TIME_MILLIS = 10

        #: Time of the last trade in milliseconds since epoch
        TRADE_TIME_MILLIS = 11

        #: Day's high trade price
        HIGH_PRICE = 12

        #: Day's low trade price
        LOW_PRICE = 13

        #: Previous day's closing price
        CLOSE_PRICE = 14

        #: Primary "listing" Exchange
        EXCHANGE_ID = 15

        #: Description of the product
        DESCRIPTION = 16

        #: Exchange where last trade was executed
        LAST_ID = 17

        #: Day's Open Price
        OPEN_PRICE = 18

        #: Current Last-Prev Close
        NET_CHANGE = 19

        #: Current percent change
        FUTURE_CHANGE_PERCENT = 20

        #: Name of exchange
        EXCHANGE_NAME = 21

        #: Trading status of the symbol
        SECURITY_STATUS = 22

        #: The total number of futures contracts that are not closed or delivered on a particular day
        OPEN_INTEREST = 23

        #: Mark-to-Market value is calculated daily using current prices to determine profit/loss
        MARK = 24

        #: Minimum price movement
        TICK = 25

        #: Minimum amount that the price of the market can change
        TICK_AMOUNT = 26

        #: Futures product
        PRODUCT = 27

        #: Display in fraction or decimal format.
        FUTURE_PRICE_FORMAT = 28

        #: Trading hours
        FUTURE_TRADING_HOURS = 29

        #: Flag to indicate if this future contract is tradable
        FUTURE_IS_TRADABLE = 30

        #: Point value
        FUTURE_MULTIPLIER = 31

        #: Indicates if this contract is active
        FUTURE_IS_ACTIVE = 32

        #: Closing price
        FUTURE_SETTLEMENT_PRICE = 33

        #: Symbol of the active contract
        FUTURE_ACTIVE_SYMBOL = 34

        #: Expiration date of this contract
        FUTURE_EXPIRATION_DATE = 35

        #: Expiration Style
        EXPIRATION_STYLE = 36

        #: Time of the last ask-side quote in milliseconds since epoch
        ASK_TIME_MILLIS = 37

        #: Time of the last bid-side quote in milliseconds since epoch
        BID_TIME_MILLIS = 38

        #: Indicates if this contract has quoted during the active session
        QUOTED_IN_SESSION = 39

        #: Expiration date of this contract
        SETTLEMENT_DATE = 40

    async def level_one_futures_subs(self, symbols, *, fields=None,
                                     keys_per_request=None):
        '''

        Subscribe to level one futures quote data.

        :param symbols: Futures symbols to receive quotes for
        :param fields: Iterable of :class:`LevelOneFuturesFields` representing
                       the fields to return in streaming entries. If unset, all
                       fields will be requested.
        '''
        if fields and self.LevelOneFuturesFields.SYMBOL not in fields:
            fields = list(fields) + [self.LevelOneFuturesFields.SYMBOL]
        await self._service_op(
            symbols, 'LEVELONE_FUTURES', 'SUBS', self.LevelOneFuturesFields,
            fields=fields,
            keys_per_request=keys_per_request)

    async def level_one_futures_unsubs(self, symbols, *,
                                       keys_per_request=None):
        '''

        Un-Subscribe to level one futures quote data.

        :param symbols: Futures symbols to receive quotes for
        '''

        await self._service_op(symbols, 'LEVELONE_FUTURES', 'UNSUBS',
            keys_per_request=keys_per_request)

    async def level_one_futures_add(self, symbols, *, fields=None,
                                    keys_per_request=None):
        '''

        Add symbols to the list to receive quotes for.

        :param symbols: Futures symbols to add to the list to receive quotes for
        :param fields: Iterable of :class:`LevelOneFuturesFields` representing
                       the fields to return in streaming entries. If unset, all
                       fields will be requested.
        '''
        if fields and self.LevelOneFuturesFields.SYMBOL not in fields:
            fields = list(fields) + [self.LevelOneFuturesFields.SYMBOL]
        await self._service_op(
            symbols, 'LEVELONE_FUTURES', 'ADD',
            self.LevelOneFuturesFields, fields=fields,
            keys_per_request=keys_per_request)

    async def level_one_futures_view(self, fields):
        '''
        Ask Schwab to change the fields delivered for every symbol in the
        level one futures quotes subscription, without resubscribing.

        This applies to the whole subscription, not to particular symbols:
        Schwab documents a VIEW as applying to every symbol of the service,
        and one naming a single symbol was measured changing the fields of
        every symbol here. So this method takes no symbols. See
        :ref:`the note on VIEW <level_one_view>`.

        :param fields: Iterable of :class:`LevelOneFuturesFields`
                       to deliver from now on. ``SYMBOL`` is added if
                       missing.
        '''
        await self._view_op(
            'LEVELONE_FUTURES', self.LevelOneFuturesFields, fields)

    def add_level_one_futures_handler(self, handler):
        '''
        Register a function to handle level one futures quotes as they are sent.
        See :ref:`registering_handlers` for details.
        '''
        self._handlers['LEVELONE_FUTURES'].append(
            _Handler(handler, self.LevelOneFuturesFields))

    ##########################################################################
    # LEVELONE_FOREX

    class LevelOneForexFields(_BaseFieldEnum):
        '''
        '''

        #: Ticker symbol in upper case.
        SYMBOL = 0

        #: Current Bid Price
        BID_PRICE = 1

        #: Current Ask Price
        ASK_PRICE = 2

        #: Price at which the last trade was matched
        LAST_PRICE = 3

        #: Number of currency pairs for bid
        BID_SIZE = 4

        #: Number of currency pairs for ask
        ASK_SIZE = 5

        #: Aggregated currency pairs traded throughout the day, including pre/post market hours.
        TOTAL_VOLUME = 6

        #: Number of currency pairs traded with last trade
        LAST_SIZE = 7

        #: Trade time of the last quote in milliseconds since epoch
        QUOTE_TIME_MILLIS = 8

        #: Trade time of the last trade in milliseconds since epoch
        TRADE_TIME_MILLIS = 9

        #: Day's high trade price
        HIGH_PRICE = 10

        #: Day's low trade price
        LOW_PRICE = 11

        #: Previous day's closing price
        CLOSE_PRICE = 12

        #: Exchange Id
        EXCHANGE_ID = 13

        #: Description of the product
        DESCRIPTION = 14

        #: Day's Open Price
        OPEN_PRICE = 15

        #: Current Last-Prev Close
        NET_CHANGE = 16

        #: Current percent change
        CHANGE_PERCENT = 17

        #: Name of exchange
        EXCHANGE_NAME = 18

        #: Valid decimal points
        DIGITS = 19

        #: Trading status of the symbol
        SECURITY_STATUS = 20

        #: Minimum price movement
        TICK = 21

        #: Minimum amount that the price of the market can change
        TICK_AMOUNT = 22

        #: Product name
        PRODUCT = 23

        #: Trading hours
        TRADING_HOURS = 24

        #: Flag to indicate if this forex is tradable
        IS_TRADABLE = 25

        #: Market Maker
        MARKET_MAKER = 26

        #: Highest price traded in the past 12 months, or 52 weeks
        HIGH_PRICE_52_WEEK = 27

        #: Lowest price traded in the past 12 months, or 52 weeks
        LOW_PRICE_52_WEEK = 28

        #: Mark-to-Market value is calculated daily using current prices to determine profit/loss
        MARK = 29

    async def level_one_forex_subs(self, symbols, *, fields=None,
                                   keys_per_request=None):
        '''

        Subscribe to level one forex quote data.

        :param symbols: Forex symbols to receive quotes for
        :param fields: Iterable of :class:`LevelOneForexFields` representing
                       the fields to return in streaming entries. If unset, all
                       fields will be requested.
        '''
        if fields and self.LevelOneForexFields.SYMBOL not in fields:
            fields = list(fields) + [self.LevelOneForexFields.SYMBOL]
        await self._service_op(
            symbols, 'LEVELONE_FOREX', 'SUBS', self.LevelOneForexFields,
            fields=fields,
            keys_per_request=keys_per_request)

    async def level_one_forex_unsubs(self, symbols, *, keys_per_request=None):
        '''

        Un-Subscribe to level one forex quote data.

        :param symbols: Forex symbols to receive quotes for
        '''

        await self._service_op(symbols, 'LEVELONE_FOREX', 'UNSUBS',
            keys_per_request=keys_per_request)

    async def level_one_forex_add(self, symbols, *, fields=None,
                                  keys_per_request=None):
        '''

        Add symbols to the list to receive quotes for.

        :param symbols: Forex symbols to add to list to receive quotes for
        :param fields: Iterable of :class:`LevelOneForexFields` representing
                       the fields to return in streaming entries. If unset, all
                       fields will be requested.

        '''
        if fields and self.LevelOneForexFields.SYMBOL not in fields:
            fields = list(fields) + [self.LevelOneForexFields.SYMBOL]
        await self._service_op(
            symbols, 'LEVELONE_FOREX', 'ADD',
            self.LevelOneForexFields, fields=fields,
            keys_per_request=keys_per_request)

    async def level_one_forex_view(self, fields):
        '''
        Ask Schwab to change the fields delivered for every symbol in the
        level one forex quotes subscription, without resubscribing.

        This applies to the whole subscription, not to particular symbols:
        Schwab documents a VIEW as applying to every symbol of the service,
        and one naming a single symbol was measured changing the fields of
        every symbol here. So this method takes no symbols. See
        :ref:`the note on VIEW <level_one_view>`.

        :param fields: Iterable of :class:`LevelOneForexFields`
                       to deliver from now on. ``SYMBOL`` is added if
                       missing.
        '''
        await self._view_op(
            'LEVELONE_FOREX', self.LevelOneForexFields, fields)

    def add_level_one_forex_handler(self, handler):
        '''
        Register a function to handle level one forex quotes as they are sent.
        See :ref:`registering_handlers` for details.
        '''
        self._handlers['LEVELONE_FOREX'].append(_Handler(handler,
                                                         self.LevelOneForexFields))

    ##########################################################################
    # LEVELONE_FUTURES_OPTIONS

    class LevelOneFuturesOptionsFields(_BaseFieldEnum):
        '''
        '''

        #: Ticker symbol in upper case.
        SYMBOL = 0

        #: Current Bid Price
        BID_PRICE = 1

        #: Current Ask Price
        ASK_PRICE = 2

        #: Price at which the last trade was matched
        LAST_PRICE = 3

        #: Number of contracts for bid
        BID_SIZE = 4

        #: Number of contracts for ask
        ASK_SIZE = 5

        #: Exchange with the bid
        BID_ID = 6

        #: Exchange with the ask
        ASK_ID = 7

        #: Aggregated contracts traded throughout the day, including pre/post market hours.
        TOTAL_VOLUME = 8

        #: Number of contracts traded with last trade
        LAST_SIZE = 9

        #: Trade time of the last quote in milliseconds since epoch
        QUOTE_TIME_MILLIS = 10

        #: Trade time of the last trade in milliseconds since epoch
        TRADE_TIME_MILLIS = 11

        #: Day's high trade price
        HIGH_PRICE = 12

        #: Day's low trade price
        LOW_PRICE = 13

        #: Previous day's closing price
        CLOSE_PRICE = 14

        #: Exchange where last trade was executed
        LAST_ID = 15

        #: Description of the product
        DESCRIPTION = 16

        #: Day's Open Price
        OPEN_PRICE = 17

        #: Open Interest
        OPEN_INTEREST = 18

        #: Mark-to-Market value is calculated daily using current prices to determine profit/loss
        MARK = 19

        #: Minimum price movement
        TICK = 20

        #: Minimum amount that the price of the market can change
        TICK_AMOUNT = 21

        #: Point value
        FUTURE_MULTIPLIER = 22

        #: Closing price
        FUTURE_SETTLEMENT_PRICE = 23

        #: Underlying symbol
        UNDERLYING_SYMBOL = 24

        #: Strike Price
        STRIKE_PRICE = 25

        #: Expiration date of this contract
        FUTURE_EXPIRATION_DATE = 26

        #: Expiration Style
        EXPIRATION_STYLE = 27

        #: Contract Type
        CONTRACT_TYPE = 28

        #: Security Status
        SECURITY_STATUS = 29

        #: Exchange character
        EXCHANGE_ID = 30

        #: Display name of exchange
        EXCHANGE_NAME = 31

    async def level_one_futures_options_subs(self, symbols, *, fields=None,
                                             keys_per_request=None):
        '''

        Subscribe to level one futures options quote data.

        :param symbols: Futures options symbols to receive quotes for
        :param fields: Iterable of :class:`LevelOneFuturesOptionsFields`
                       representing the fields to return in streaming entries.
                       If unset, all fields will be requested.
        '''
        if fields and self.LevelOneFuturesOptionsFields.SYMBOL not in fields:
            fields = list(fields) + [self.LevelOneFuturesOptionsFields.SYMBOL]
        await self._service_op(
            symbols, 'LEVELONE_FUTURES_OPTIONS', 'SUBS',
            self.LevelOneFuturesOptionsFields, fields=fields,
            keys_per_request=keys_per_request)

    async def level_one_futures_options_unsubs(self, symbols, *,
                                               keys_per_request=None):
        '''

        Un-Subscribe to level one futures options quote data.

        :param symbols: Futures options symbols to receive quotes for
        '''

        await self._service_op(symbols, 'LEVELONE_FUTURES_OPTIONS', 'UNSUBS',
            keys_per_request=keys_per_request)

    async def level_one_futures_options_add(self, symbols, *, fields=None,
                                            keys_per_request=None):
        '''

        Add symbols to the list to receive quotes for.

        :param symbols: Futures options symbols add to list to receive quotes for
        :param fields: Iterable of :class:`LevelOneFuturesOptionsFields`
                       representing the fields to return in streaming entries.
                       If unset, all fields will be requested.
        '''
        if fields and self.LevelOneFuturesOptionsFields.SYMBOL not in fields:
            fields = list(fields) + [self.LevelOneFuturesOptionsFields.SYMBOL]
        await self._service_op(
            symbols, 'LEVELONE_FUTURES_OPTIONS', 'ADD',
            self.LevelOneFuturesOptionsFields, fields=fields,
            keys_per_request=keys_per_request)

    async def level_one_futures_options_view(self, fields):
        '''
        Ask Schwab to change the fields delivered for every symbol in the
        level one futures options quotes subscription, without resubscribing.

        This applies to the whole subscription, not to particular symbols:
        Schwab documents a VIEW as applying to every symbol of the service,
        and that was measured on futures and forex, though not on this
        service. So this method takes no symbols. See
        :ref:`the note on VIEW <level_one_view>`.

        :param fields: Iterable of :class:`LevelOneFuturesOptionsFields`
                       to deliver from now on. ``SYMBOL`` is added if
                       missing.
        '''
        await self._view_op(
            'LEVELONE_FUTURES_OPTIONS', self.LevelOneFuturesOptionsFields,
            fields)

    def add_level_one_futures_options_handler(self, handler):
        '''
        Register a function to handle level one futures options quotes as they
        are sent. See :ref:`registering_handlers` for details.
        '''
        self._handlers['LEVELONE_FUTURES_OPTIONS'].append(
            _Handler(handler, self.LevelOneFuturesOptionsFields))

    ##########################################################################
    # Common book utilities

    class BookFields(_BaseFieldEnum):
        SYMBOL = 0
        BOOK_TIME = 1
        BIDS = 2
        ASKS = 3

    class BidFields(_BaseFieldEnum):
        BID_PRICE = 0
        TOTAL_VOLUME = 1
        NUM_BIDS = 2
        BIDS = 3

    class PerExchangeBidFields(_BaseFieldEnum):
        #: The market participant quoting this size.
        EXCHANGE = 0

        #: Size quoted by this participant at this price.
        BID_VOLUME = 1

        #: When this participant's quote was made, in **milliseconds since
        #: midnight Eastern** --- not epoch milliseconds, which is what every
        #: other time in a book frame carries. Read as epoch it lands in
        #: January 1970. Measured on the two equity books before the open:
        #: all 8,904 per-exchange entries carried a value from inside the hour
        #: before their frame, a median of one to two minutes, and one quote
        #: was still being re-sent half an hour on --- so it is not a
        #: freshness signal. Schwab's table calls index 2 the quote time for
        #: this market maker's quote. See
        #: :ref:`the book payload <book_fields>`.
        QUOTE_TIME = 2

    class AskFields(_BaseFieldEnum):
        ASK_PRICE = 0
        TOTAL_VOLUME = 1
        NUM_ASKS = 2
        ASKS = 3

    class PerExchangeAskFields(_BaseFieldEnum):
        #: The market participant quoting this size.
        EXCHANGE = 0

        #: Size quoted by this participant at this price.
        ASK_VOLUME = 1

        #: When this participant's quote was made. See
        #: :attr:`StreamClient.PerExchangeBidFields.QUOTE_TIME`: the same
        #: encoding, and the same warning about reading it as epoch
        #: milliseconds.
        QUOTE_TIME = 2

    class _BookHandler(_Handler):
        def label_message(self, msg):
            # Relabel top-level fields
            new_msg = super().label_message(msg)

            # Relabel bids
            for content in new_msg['content']:
                if 'BIDS' in content:
                    for bid in content['BIDS']:
                        # Relabel top-level bids
                        StreamClient.BidFields.relabel_message(bid, bid)

                        # Relabel per-exchange bids
                        for e_bid in bid['BIDS']:
                            StreamClient.PerExchangeBidFields.relabel_message(
                                e_bid, e_bid)

            # Relabel asks
            for content in new_msg['content']:
                if 'ASKS' in content:
                    for ask in content['ASKS']:
                        # Relabel top-level asks
                        StreamClient.AskFields.relabel_message(ask, ask)

                        # Relabel per-exchange bids
                        for e_ask in ask['ASKS']:
                            StreamClient.PerExchangeAskFields.relabel_message(
                                e_ask, e_ask)

            return new_msg

    ##########################################################################
    # NYSE_BOOK

    async def nyse_book_subs(self, symbols, *, keys_per_request=None):
        '''
        Subscribe to the NYSE level two order book.

        :param symbols: NYSE symbols to subscribe to.
        '''
        await self._service_op(
            symbols, 'NYSE_BOOK', 'SUBS',
            self.BookFields, fields=self.BookFields.all_fields(),
            keys_per_request=keys_per_request)

    async def nyse_book_unsubs(self, symbols, *, keys_per_request=None):
        '''
        Un-Subscribe to the NYSE level two order book.

        :param symbols: NYSE symbols to unsubscribe from.
        '''
        await self._service_op(symbols, 'NYSE_BOOK', 'UNSUBS',
            keys_per_request=keys_per_request)

    async def nyse_book_add(self, symbols, *, keys_per_request=None):
        '''
        Add to the NYSE level two order book.

        :param symbols: NYSE symbols to add to the subscription.
        '''
        await self._service_op(symbols, 'NYSE_BOOK', 'ADD', self.BookFields,
            keys_per_request=keys_per_request)

    def add_nyse_book_handler(self, handler):
        '''
        Register a function to handle level two NYSE book data as it is updated
        See :ref:`registering_handlers` for details.
        '''
        self._handlers['NYSE_BOOK'].append(
            self._BookHandler(handler, self.BookFields))

    ##########################################################################
    # NASDAQ_BOOK

    async def nasdaq_book_subs(self, symbols, *, keys_per_request=None):
        '''
        Subscribe to the NASDAQ level two order book.

        :param symbols: NASDAQ symbols to subscribe to.
        '''
        await self._service_op(symbols, 'NASDAQ_BOOK', 'SUBS',
                               self.BookFields,
                               fields=self.BookFields.all_fields(),
            keys_per_request=keys_per_request)

    async def nasdaq_book_unsubs(self, symbols, *, keys_per_request=None):
        '''
        Un-Subscribe to the NASDAQ level two order book.

        :param symbols: NASDAQ symbols to unsubscribe from.
        '''
        await self._service_op(symbols, 'NASDAQ_BOOK', 'UNSUBS',
            keys_per_request=keys_per_request)

    async def nasdaq_book_add(self, symbols, *, keys_per_request=None):
        '''
        Add to the NASDAQ level two order book.

        :param symbols: NASDAQ symbols to add to the subscription.
        '''
        await self._service_op(symbols, 'NASDAQ_BOOK', 'ADD', self.BookFields,
            keys_per_request=keys_per_request)

    def add_nasdaq_book_handler(self, handler):
        '''
        Register a function to handle level two NASDAQ book data as it is
        updated See :ref:`registering_handlers` for details.
        '''
        self._handlers['NASDAQ_BOOK'].append(
            self._BookHandler(handler, self.BookFields))

    ##########################################################################
    # OPTIONS_BOOK

    async def options_book_subs(self, symbols, *, keys_per_request=None):
        '''
        Subscribe to the level two order book for options.

        :param symbols: Option symbols to subscribe to.
        '''
        await self._service_op(symbols, 'OPTIONS_BOOK', 'SUBS',
                               self.BookFields,
                               fields=self.BookFields.all_fields(),
            keys_per_request=keys_per_request)

    async def options_book_unsubs(self, symbols, *, keys_per_request=None):
        '''
        Un-Subscribe to the level two order book for options.

        :param symbols: Option symbols to unsubscribe from.
        '''
        await self._service_op(symbols, 'OPTIONS_BOOK', 'UNSUBS',
            keys_per_request=keys_per_request)

    async def options_book_add(self, symbols, *, keys_per_request=None):
        '''
        Add to the level two order book for options.

        :param symbols: Option symbols to add to the subscription.
        '''
        await self._service_op(symbols, 'OPTIONS_BOOK', 'ADD', self.BookFields,
            keys_per_request=keys_per_request)

    def add_options_book_handler(self, handler):
        '''
        Register a function to handle level two options book data as it is
        updated See :ref:`registering_handlers` for details.
        '''
        self._handlers['OPTIONS_BOOK'].append(
            self._BookHandler(handler, self.BookFields))

    ##########################################################################
    # SCREENER_EQUITY/SCREENER_OPTION

    class ScreenerFields(_BaseFieldEnum):
        #: The symbol used to look up either actives, gainers or losers
        SYMBOL = 0

        #: Market snapshot timestamp in milliseconds since Epoch
        TIMESTAMP = 1

        #: Field to sort on
        SORT_FIELD = 2

        #: Frequency of data to sort
        FREQUENCY = 3

        #: Array of fields
        ITEMS = 4

    # Deliberately not validated. Schwab accepts a malformed key, replies
    # `code: 0, "SUBS command succeeded"` and then delivers nothing forever,
    # so a client-side check would turn a permanent silence into an
    # exception -- which is a real argument for adding one.
    #
    # It is not added because a validator that rejects what Schwab accepts is
    # worse than none: it reads as a configuration error rather than as two
    # documents disagreeing, and there is no way for the caller to get past
    # it. Schwab documents a prefix list, but whether it is the whole set is
    # not known, and keys are easy to get wrong: `$SPX.X` looks like a
    # plausible key and is not a valid prefix.
    # Roughly eight prefixes have been tried against the live service, out of
    # a set whose size nobody knows.
    #
    # A validator would first need checking against real traffic, long enough
    # to say whether the documented set is the actual set. Until then the key
    # format is documented and the caller decides.
    async def screener_equity_subs(self, symbols, *, keys_per_request=None):
        '''
        Subscribe to Screener Equity.

        The subscription key is **not** a stock symbol. It is
        ``(PREFIX)_(SORTFIELD)_(FREQUENCY)``, for example ``NASDAQ_VOLUME_5``
        or ``$SPX_PERCENT_CHANGE_UP_60``. Passing a ticker subscribes to
        nothing and reports no error. See :ref:`screener` for the
        full set of prefixes, sort fields and frequencies.

        **Ten keys is the most this takes.** Eleven are refused with response
        code 19, which arrives as ``UnexpectedResponseCode``; Schwab's message
        ends with the cap and the excess,
        ``(SCREENER_EQUITY=10, DISCARDED=1)``.
        The refusal does not leave nothing subscribed: the first ten keys
        sent delivered. Subscribe ten or fewer rather than relying
        on which ten survive. :ref:`screener` has the measurement and its
        limits.

        :param symbols: Screener keys to subscribe to.
        '''
        await self._service_op(
            symbols, 'SCREENER_EQUITY', 'SUBS', self.ScreenerFields,
            keys_per_request=keys_per_request)

    async def screener_equity_unsubs(self, symbols, *, keys_per_request=None):
        '''
        Un-Subscribe to Screener Equity.

        :param symbols: Screener keys to unsubscribe from, in the
                        ``(PREFIX)_(SORTFIELD)_(FREQUENCY)`` form.
        '''
        await self._service_op(symbols, 'SCREENER_EQUITY', 'UNSUBS',
            keys_per_request=keys_per_request)

    async def screener_equity_add(self, symbols, *, keys_per_request=None):
        '''
        Add keys to the Screener Equity subscription.

        Added keys share the ten
        :meth:`~schwaby.streaming.StreamClient.screener_equity_subs`
        describes: with eight subscribed, adding three is refused with code 19
        and ``DISCARDED=1``, and the first two added delivered.

        :param symbols: Screener keys to add, in the
                        ``(PREFIX)_(SORTFIELD)_(FREQUENCY)`` form.
        '''
        await self._service_op(
            symbols, 'SCREENER_EQUITY', 'ADD', self.ScreenerFields,
            keys_per_request=keys_per_request)

    def add_screener_equity_handler(self, handler):
        '''
        Register a function to handle Screener Equity data as it is
        updated See :ref:`registering_handlers` for details.
        '''
        self._handlers['SCREENER_EQUITY'].append(
            _Handler(handler, self.ScreenerFields))

    async def screener_option_subs(self, symbols, *, keys_per_request=None):
        '''
        Subscribe to Screener Option.

        The subscription key is **not** a stock symbol. It is
        ``(PREFIX)_(SORTFIELD)_(FREQUENCY)``, for example ``OPTION_CALL_VOLUME_5``
        or ``OPTION_ALL_TRADES_60``. Passing a ticker subscribes to
        nothing and reports no error. See :ref:`screener` for the
        full set of prefixes, sort fields and frequencies.

        **Ten keys is the most this takes**, as for
        :meth:`~schwaby.streaming.StreamClient.screener_equity_subs`. Eleven
        are refused with response code 19, ending
        ``(SCREENER_OPTION=10, DISCARDED=1)``. See :ref:`screener`.

        :param symbols: Screener keys to subscribe to.
        '''
        await self._service_op(
            symbols, 'SCREENER_OPTION', 'SUBS', self.ScreenerFields,
            keys_per_request=keys_per_request)

    async def screener_option_unsubs(self, symbols, *, keys_per_request=None):
        '''
        Un-Subscribe to Screener Option.

        :param symbols: Screener keys to unsubscribe from, in the
                        ``(PREFIX)_(SORTFIELD)_(FREQUENCY)`` form.
        '''
        await self._service_op(symbols, 'SCREENER_OPTION', 'UNSUBS',
            keys_per_request=keys_per_request)

    async def screener_option_add(self, symbols, *, keys_per_request=None):
        '''
        Add keys to the Screener Option subscription.

        Schwab's response code 19 is defined for "Subscribe or Add", so an
        ``ADD`` can be refused for the ten-key limit the same way a ``SUBS``
        can. That added keys share it was measured on the equity screener,
        not on this one.

        :param symbols: Screener keys to add, in the
                        ``(PREFIX)_(SORTFIELD)_(FREQUENCY)`` form.
        '''
        await self._service_op(
            symbols, 'SCREENER_OPTION', 'ADD', self.ScreenerFields,
            keys_per_request=keys_per_request)

    def add_screener_option_handler(self, handler):
        '''
        Register a function to handle Screener Option data as it is
        updated See :ref:`registering_handlers` for details.
        '''
        self._handlers['SCREENER_OPTION'].append(
            _Handler(handler, self.ScreenerFields))

    ##########################################################################
    # NEWS_HEADLINE

    class NewsHeadlineFields(_BaseFieldEnum):
        '''
        Fields of a ``NEWS_HEADLINE`` item. Schwab's streamer documentation
        does not describe this service at all. The numbering comes from items
        delivered to a live account; the names describe the values seen there,
        except where a member says otherwise. See :ref:`news_headline` for what
        was measured and what was not.
        '''

        #: Held for the subscribed symbol, which arrives as the item's ``key``:
        #: no item delivered to a live account carried a field ``0``.
        SYMBOL = 0

        #: Epoch milliseconds. Which moment it marks is not established; it
        #: was never later than receipt.
        TIMESTAMP = 1

        #: An identifier. Named after TD Ameritrade's field one position
        #: higher, not from anything measured: it has equalled ``STORY_ID`` in
        #: every item captured, so which is which is not established.
        HEADLINE_ID = 2

        #: Meaning not established. ``'A'`` in every item captured but one,
        #: which carried ``'D'``.
        UNKNOWN_3 = 3

        #: The headline.
        HEADLINE = 4

        #: An identifier. Named after TD Ameritrade's field one position
        #: higher; it has equalled ``HEADLINE_ID`` in every item captured.
        STORY_ID = 5

        #: A list of category names.
        CATEGORIES = 6

        #: A list of symbols, including the subscribed one.
        SYMBOLS = 7

        #: Meaning not established. ``False`` in every item captured.
        UNKNOWN_8 = 8

        #: The name of the news source.
        SOURCE = 9

        #: Further text, never over 100 characters in any item captured: not
        #: the story. See :ref:`news_headline`.
        TEXT = 10

        #: A language tag, such as ``'en-US'``.
        LANGUAGE = 11

    async def news_headline_subs(self, symbols, *, keys_per_request=None):
        '''
        Subscribe to news headlines for the given symbols.

        Schwab does not document this service, so it could change or stop
        without notice. See :ref:`news_headline`.

        :param symbols: Symbols to receive headlines for.
        '''
        await self._service_op(
            symbols, 'NEWS_HEADLINE', 'SUBS', self.NewsHeadlineFields,
            keys_per_request=keys_per_request)

    async def news_headline_unsubs(self, symbols, *, keys_per_request=None):
        '''
        Un-Subscribe to news headlines for the given symbols. See
        :ref:`news_headline`.

        :param symbols: Symbols to stop receiving headlines for.
        '''
        await self._service_op(symbols, 'NEWS_HEADLINE', 'UNSUBS',
            keys_per_request=keys_per_request)

    async def news_headline_add(self, symbols, *, keys_per_request=None):
        '''
        Add symbols to the news headline subscription. See
        :ref:`news_headline`.

        :param symbols: Symbols to receive headlines for.
        '''
        await self._service_op(
            symbols, 'NEWS_HEADLINE', 'ADD', self.NewsHeadlineFields,
            keys_per_request=keys_per_request)

    def add_news_headline_handler(self, handler):
        '''
        Register a function to handle news headlines as they are sent. See
        :ref:`registering_handlers` for details.
        '''
        self._handlers['NEWS_HEADLINE'].append(
            _Handler(handler, self.NewsHeadlineFields))
