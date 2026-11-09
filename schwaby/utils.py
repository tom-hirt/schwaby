'''Implements additional functionality beyond what's implemented in the client
module.'''

import collections
import enum
import typing
import json
# Aliased so `from schwaby.utils import *` cannot rebind a caller's own
# `datetime`, which is most often the class rather than the module.
import datetime as _datetime
import decimal
import re
import time
import webbrowser

# The class an HTTP error status raises, under a name this library owns.
#
# `raise_for_status()` on a response from any client call raises
# `httpx2.HTTPStatusError`. That package shares no exception hierarchy with
# `httpx`, so a handler written against the other one never runs -- no error,
# the `except` simply does not match, and nothing reports that it didn't.
# Catching this name means a caller never has to find out which HTTP package
# is installed to know what to catch. Measured: `httpx2` is the only HTTP
# dependency declared, `httpx` is not installed alongside it, and a 429 driven
# through the real session stack raises this class. Re-exported rather than
# subclassed, so `isinstance` against the original still holds.
from . import _redact
from httpx2 import HTTPStatusError

from authlib.integrations.base_client.errors import OAuthError


def class_fullname(o):
    return o.__module__ + '.' + o.__name__


class EnumEnforcer:
    def __init__(self, enforce_enums):
        self.enforce_enums = enforce_enums

    def type_error(self, value, required_enum_type):
        possible_members_message = ''

        if _is_instance(value, str):
            possible_members = []
            for member in required_enum_type.__members__:
                fullname = class_fullname(required_enum_type) + '.' + member
                if value in fullname:
                    possible_members.append(fullname)

            # Oxford comma insertion
            if possible_members:
                possible_members_message = 'Did you mean ' + ', '.join(
                    possible_members[:-2] + [' or '.join(
                        possible_members[-2:])]) + '? '

        raise ValueError(
            ('expected type "{}", got type "{}". {}(initialize with ' +
             'enforce_enums=False to disable this checking)').format(
                required_enum_type.__name__,
                _type_name(value),
                possible_members_message))

    def convert_enum(self, value, required_enum_type):
        if value is None:
            return None

        if _is_instance(value, required_enum_type):
            return value.value
        elif self.enforce_enums:
            self.type_error(value, required_enum_type)
        else:
            return value

    def convert_enum_iterable(self, iterable, required_enum_type):
        if iterable is None:
            return None

        if _is_instance(iterable, required_enum_type):
            return [iterable.value]

        values = []
        for value in iterable:
            if _is_instance(value, required_enum_type):
                values.append(value.value)
            elif self.enforce_enums:
                self.type_error(value, required_enum_type)
            else:
                values.append(value)
        return values

    def set_enforce_enums(self, enforce_enums):
        '''
        Turn enum checking on or off after construction. With it off, a raw
        value is passed through where a member of the matching enum is
        expected, so a value the enum does not list reaches Schwab rather
        than raising here.
        '''
        self.enforce_enums = enforce_enums


def _describe_error(response):
    '''Schwab's own words for a rejection, as a suffix, or the empty string.

    Bounded, because this ends up in an exception message and from there in
    somebody's logs: an unbounded body would put an entire order echo on one
    line. The whole response stays reachable as the exception's ``.response``
    for anyone who needs the rest.

    Anything unexpected yields no suffix rather than an error of its own. This
    runs on the failure path, and a formatter that raises there would replace a
    useful exception with a useless one.
    '''
    try:
        body = response.json()
        if not isinstance(body, dict):
            return ''
        parts = []
        message = body.get('message')
        if isinstance(message, str) and message.strip():
            parts.append(message.strip())
        errors = body.get('errors')
        if isinstance(errors, (list, tuple)):
            parts.extend(e.strip() for e in errors
                         if isinstance(e, str) and e.strip())
        if not parts:
            return ''
        detail = _redact.before_cut('; '.join(parts), _ERROR_DETAIL_LIMIT)
        if len(detail) > _ERROR_DETAIL_LIMIT:
            detail = detail[:_ERROR_DETAIL_LIMIT] + '... (truncated, see .response)'
        return ': ' + detail
    except Exception:
        return ''


_ERROR_DETAIL_LIMIT = 500


def _rebuild_exception(cls, args, state):
    '''Reconstructs an exception without going through its ``__init__``.

    Module level because ``__reduce__`` has to name something picklable.
    '''
    exc = cls.__new__(cls)
    BaseException.__init__(exc, *args)
    exc.__dict__.update(state)
    return exc


class SchwabError(Exception):
    '''Base class for every exception this library raises.

    Every exception class this library *defines* inherits it, so
    ``except SchwabError`` is one name for all of them.

    It is **not** everything the library can raise. Argument validation still
    raises builtin ``ValueError`` in about thirty places --- a negative
    quantity, a float price, a strike finer than the symbol format carries ---
    and those are ordinary "you passed a bad value" errors that a builtin
    describes correctly. Catch ``ValueError`` for those. What this class buys
    you is the ability to catch the library's own failures *without* also
    catching every ``int()`` and ``float()`` in the same block.

    :mod:`schwaby.testing`'s errors are not among them: each is a test's
    failure, an ``AssertionError``, so code under test that catches this
    does not hide one.

    Two of the order exceptions additionally inherit ``ValueError``, so an
    ``except ValueError`` around order placement catches them.
    :class:`OrderIdNotFoundError` deliberately does not: see its own
    documentation.
    '''

    def __reduce__(self):
        # Exceptions here take the thing they are about as a leading positional
        # argument -- a response, an order id, a raw frame -- and pass only the
        # message up to BaseException. The default __reduce__ reconstructs by
        # calling __init__ with self.args, which is just the message, so a copy
        # or a pickle would either raise TypeError for the missing positionals
        # or, worse, succeed with the message bound to the response and the
        # message itself lost.
        #
        # Nothing in this library moves an exception across a boundary --
        # checked: the login flow's queue carries flask.request.url and
        # RedirectServerExitedError is constructed in the parent. This is for
        # callers who do, a ProcessPoolExecutor over placements being the
        # obvious one, where the exception saying an order is live on the wrong
        # account is the last that should arrive as a TypeError about argument
        # counts.
        #
        # _rebuild_exception bypasses __init__ entirely -- __new__, then
        # BaseException.__init__ for the message, then __dict__. That handles
        # every signature below uniformly, including keyword-only ones, and
        # needs no per-class __reduce__. The cost is that any invariant a
        # subclass establishes in __init__ beyond plain assignment -- coercing
        # an id to int, normalising a hash -- is skipped on reconstruction. No
        # subclass does that today; one that starts to needs its own
        # __reduce__ or a __setstate__.
        return (_rebuild_exception, (type(self), self.args, dict(self.__dict__)))



class UnsuccessfulOrderException(SchwabError, ValueError):
    '''
    Raised by :meth:`Utils.extract_order_id` when attempting to extract an
    order ID from a :meth:`Client.place_order()
    <schwaby.client.Client.place_order>` response that was not successful.

    The rejected response is on the exception as ``.response``, and Schwab's own
    explanation is in the message when there was one. Schwab types an error body
    as ``{"message": ..., "errors": [...]}``, and that text is the only place
    the reason for a rejection appears -- a status code alone does not
    distinguish a malformed order from one the account cannot afford.
    '''

    def __init__(self, response, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.response = response


class OrderIdNotFoundError(SchwabError):
    '''
    Raised by :meth:`Utils.extract_order_id` when Schwab accepted the order but
    the response did not yield an order ID.

    **The order may be live.** Schwab returned a success status, so the order
    was very likely placed; what is missing is the handle you would use to
    watch or cancel it. Treat this as "go and look", not as "nothing
    happened" --- :meth:`Client.get_orders_for_account()
    <schwaby.client.Client.get_orders_for_account>` over a recent time
    window will find it.

    ``.response`` is the ``place_order`` response. ``.location`` is the raw
    ``Location`` header, or ``None`` when there was not one.

    **This deliberately does not inherit** ``ValueError``, unlike its two
    siblings. ``except ValueError`` is the idiom people reach for around
    ``int()`` and ``float()``, and order specs coerce exactly those a few lines
    from the call --- so inheriting it would let the one exception here that
    means *an order may be live and untracked* be swallowed by a block aimed at
    parsing. It is a :class:`SchwabError` and nothing else.

    ``ValueError`` also says the caller passed a bad argument, and they did
    not. This is remote state.
    '''

    def __init__(self, response, location, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.response = response
        self.location = location


class MissingLocationHeaderError(OrderIdNotFoundError):
    '''The response carried no ``Location`` header at all.

    Catch :class:`OrderIdNotFoundError` unless you specifically need to tell
    this apart from :class:`UnrecognizedLocationError`.
    '''


class UnrecognizedLocationError(OrderIdNotFoundError):
    '''The ``Location`` header was present and did not look like an order URL.

    Most likely Schwab changed the format. The header is on the exception as
    ``.location`` --- please include it in a bug report.
    '''


class AccountHashMismatchException(SchwabError, ValueError):
    '''
    Raised by :meth:`Utils.extract_order_id` when the response belongs to a
    different account than the one this :class:`Utils` was built with.

    **The order is live.** Not "may be": this is raised after the response came
    back successful *and* a valid order ID was parsed out of it, so Schwab
    placed something. The mismatch is in your wiring, and the consequence is an
    order on an account you were not expecting to trade.

    So everything needed to go and cancel it is on the exception --- read it,
    do not re-derive it from the message:

    * ``.order_id`` --- the order Schwab created
    * ``.account_hash`` --- the account it was placed on, as Schwab reported it
    * ``.expected_account_hash`` --- the one this :class:`Utils` was built
      with. ``None`` if the exception was raised by something other than
      :meth:`Utils.extract_order_id`, so compare it before trusting it
    * ``.response`` --- the whole ``place_order`` response

    The two hashes are both here on purpose. A handler far from the call site
    has no ``Utils`` left to ask, and reaching only the one Schwab named leaves
    it interpolating the other out of the message text.

    .. warning::

       This also inherits ``ValueError``, so a broad ``except ValueError``
       around order building will swallow it. Given what it means, narrow that.
    '''

    # expected_account_hash is keyword-only, and that is not stylistic. Placed
    # positionally ahead of *args it would take the slot the message is passed
    # in, so a four-argument call would bind the message to it and leave
    # self.args empty -- an exception whose str() is '' on the one failure that
    # means an order is live on the wrong account. That is the same
    # succeeded-while-losing-the-message defect this class's __reduce__ exists
    # to prevent, through a different door. TokenRefreshError in this file and
    # UnusableMessage in streaming.py take their extras keyword-only for the
    # same reason.
    def __init__(self, response, order_id, account_hash, *args,
                 expected_account_hash=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.response = response
        self.order_id = order_id
        self.account_hash = account_hash
        self.expected_account_hash = expected_account_hash


def _printable(text, bound):
    """``text`` with control characters escaped, and bounded again afterwards.

    A value's own ``__repr__``, a class name, and text a venue sends can carry
    a newline or a terminal escape, and each reaches log lines or exception
    messages. Escaping takes one character to as many as ten, hence the second
    bound.
    """
    if not text.isprintable():
        text = ''.join(c if c.isprintable() else repr(c)[1:-1] for c in text)
    return text if len(text) <= bound else text[:bound - 3] + '...'


def _is_instance(value, types):
    '''``isinstance``, for a value a caller passed in.

    ``isinstance`` asks ``value.__class__`` when the value's own type does not
    match, and a class can make that raise, so a wrong type would reach the
    caller as that exception instead of the refusal naming it. The real type
    answers then. Everything else is ``isinstance`` exactly: a mock with a
    ``spec``, which reports the class it imitates, still passes.
    '''
    try:
        return isinstance(value, types)
    except Exception:
        return issubclass(type(value), types)


# Captured when this module is imported, and read from here rather than from
# the `datetime` module at call time. The client's tests replace
# `datetime.datetime` with a subclass to fix the current time, and that
# replacement lands on the module itself, so it is visible to every importer
# for the length of the test. Checked against the name at call time, a real
# datetime is not an instance of the subclass: the client's type checks would
# refuse it, and `_format_iso_instant` below would silently treat it as a date
# and send midnight with no timezone -- a wrong instant and no error.
_DATETIME = _datetime.datetime
_DATE = _datetime.date


def _format_iso_instant(dt):
    '''``dt`` as Schwab's ISO-8601 form, ``yyyy-MM-dd'T'HH:mm:ssZ``.

    The one place that decides what string an instant becomes for the
    client's ISO date parameters, kept apart from the type check and the
    naive-datetime warning so another caller can share the conversion without
    also inheriting those. The caller has already checked the type and decided
    what to do about a missing timezone; this only converts.

    A ``date`` becomes midnight of that day, with no timezone.
    '''
    if not _is_instance(dt, _DATETIME):
        dt = _datetime.datetime(year=dt.year, month=dt.month, day=dt.day)

    # The trailing Z asserts UTC, so a datetime carrying some other
    # timezone has to be converted rather than merely formatted. Without
    # this, 00:03:02-04:00 is sent as '00:03:02Z', which is a different
    # instant by the size of the offset. A naive datetime has no timezone
    # to convert from and is passed through as written.
    #
    # Aware means `utcoffset()` returns an offset, Python's own definition
    # and the one `_cancel_time_string` refuses against. A `tzinfo` whose
    # `utcoffset` returns None is naive despite being set, and `astimezone`
    # reads such a value as the host's local time -- so keying on `tzinfo`
    # alone would send a different instant from each machine, silently.
    if dt.utcoffset() is not None:
        dt = dt.astimezone(_datetime.timezone.utc)

    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _venue_text(value):
    """Text a redirect or the token endpoint sent, escaped and cut to 200
    characters, since it reaches an exception message and whatever logs it.
    A value that is not a string, which a JSON error body can carry, is taken
    as its repr; ``None`` stays ``None``."""
    if value is None:
        return None
    if issubclass(type(value), str):
        text = str.__str__(value)
    else:
        text = repr(value)
    # Redacted before it is cut, so a secret across the cut leaves nothing.
    text = _redact.before_cut(text, 200)
    return _printable(text if len(text) <= 200 else text[:197] + '...', 200)


def _refusal_text(error):
    '''An authlib error's code and description, as text safe to put in a
    message or a log: the endpoint's, escaped and cut. A description this
    library wrote is kept whole -- an ``unusable_token_response`` refusal is
    marked where it is raised -- and told apart by the mark rather than by the
    code, which an endpoint can send too.'''
    if vars(error).get('_described_by_this_library') is True:
        description = error.description
    else:
        description = _venue_text(error.description)
    return _venue_text(error.error), description


def _token_endpoint_answer(error):
    '''``(True, status)`` when the token endpoint answered the request an
    authlib error reports, with its HTTP status; ``(False, None)`` when the
    error was raised before anything was asked. Read from the status the
    token response hook puts on every refusal it raises, which is every
    endpoint refusal that reaches the caller as ``TokenRefreshError`` or
    ``LoginExchangeError``; a server error is ``HTTPStatusError`` instead.
    Not from the error's class, which says what went wrong rather than
    whether anything was sent.'''
    if '_token_endpoint_status' in vars(error):
        return True, error._token_endpoint_status
    return False, None


def _type_name(value):
    '''A value's type name for a message, read through type's own descriptor
    so that a metaclass cannot raise inside the message.'''
    try:
        return str.__str__(type.__dict__['__name__'].__get__(type(value)))
    except Exception:
        return 'object'


def _qualified_type_name(value):
    '''``module.name`` for a value's type, both read through type's own
    descriptors, so that a caller's class whose ``__name__`` raises still gets
    the message it was refused with.'''
    try:
        module = str.__str__(
                type.__dict__['__module__'].__get__(type(value)))
    except Exception:
        module = '<unknown module>'
    return '{}.{}'.format(module, _type_name(value))


class AccountNumberNotFoundError(SchwabError):
    '''
    Raised by :func:`find_account_hash` when no account in the response has the
    account number asked for.

    This is an ordinary answer rather than a fault: the token does not cover
    that account. Its message names neither account number, so it is safe to
    log.
    '''


class UnusableAccountNumbersError(SchwabError, ValueError):
    '''
    Raised by :func:`find_account_hash` when the account list is not the shape
    of the JSON body :meth:`Client.get_account_numbers
    <schwaby.client.Client.get_account_numbers>` responds with, has an entry
    that could be the account asked for but cannot be read, or lists the
    account number more than once.

    In each case the answer cannot be trusted to name one account, so no hash
    is returned. Its message names neither account number.
    '''


def get_logger():
    return _redact.get_logger(__name__)



#: The kinds of skipped account entry already reported, so a list that carries
#: one on every call warns once rather than on every lookup.
_reported_account_entry_problems = set()


def _report_skipped_account_entry(entry):
    '''Warn, once per kind, that :func:`find_account_hash` skipped an entry for
    another account whose hash cannot be used. Names the problem, never the
    account number.'''
    if not dict.__contains__(entry, 'hashValue'):
        problem = 'has no hashValue'
    elif issubclass(type(dict.get(entry, 'hashValue')), str):
        problem = 'has an empty hashValue'
    else:
        problem = 'has a hashValue that is not a string'
    if problem in _reported_account_entry_problems:
        return
    _reported_account_entry_problems.add(problem)
    get_logger().warning(
            'find_account_hash skipped an account list entry for another '
            'account that %s. It is not the account asked for, so the answer '
            'is unaffected. Reported once per kind of entry.', problem)


def find_account_hash(account_numbers, account_number):
    '''
    Returns the account hash for ``account_number``, which is what every
    account-specific call takes in place of the account number.

    ``account_numbers`` is the parsed JSON body of the response from
    :meth:`Client.get_account_numbers
    <schwaby.client.Client.get_account_numbers>`, that is
    ``client.get_account_numbers().json()``. This makes no request, so it
    serves the synchronous and asynchronous clients alike.

    A token can cover several accounts and Schwab documents no order for the
    list, so taking the first entry's ``hashValue`` picks an account rather
    than finding one.

    :param account_numbers: The parsed ``get_account_numbers()`` response
                            body, a list of
                            ``{"accountNumber": ..., "hashValue": ...}``
                            objects.
    :param account_number: The account number, as a ``str`` --- Schwab's schema
                           types it as a string. Anything else is refused
                           rather than converted, because converting a number
                           drops any leading zero and then matches nothing.
    :raises TypeError: ``account_number`` is not a ``str``.
    :raises ValueError: ``account_number`` is not ASCII digits.
    :raises AccountNumberNotFoundError: No account has that number.
    :raises UnusableAccountNumbersError: The list is not a list of objects,
                                         an entry that could be this account
                                         cannot be read, or the list has that
                                         number more than once.

    An entry whose ``hashValue`` is missing, not a string or empty is skipped
    when its ``accountNumber`` is a string of ASCII digits other than this one:
    it names another account, so it cannot change the answer. It is reported
    once per kind of entry, as a warning on this module's logger that names no
    account number. Any other unreadable entry could be this account, and
    refuses the list rather than being ignored.
    '''
    # Types are checked through type() rather than isinstance, which a class
    # can satisfy by faking __class__.
    if not issubclass(type(account_number), str):
        raise TypeError(
                'account_number must be a str, as Schwab types it, not a '
                '{}'.format(_type_name(account_number)))
    account_number = str.__str__(account_number)
    # Schwab's account numbers are ASCII digits. Anything else -- a newline or
    # a byte order mark kept from a file, an unset environment variable, a
    # hyphen -- would read as an account the token does not cover, and that
    # sends a caller with a typo off to log in again.
    if not (account_number.isascii() and account_number.isdigit()):
        raise ValueError(
                'account_number must be ASCII digits, and this one is empty '
                'or has other characters in it')

    if not issubclass(type(account_numbers), list):
        raise UnusableAccountNumbersError(
                'expected the list get_account_numbers().json() returns, not '
                'a {}'.format(_type_name(account_numbers)))

    # Read through the built-in types' own methods, and reduce every value to
    # a plain str, so that what is checked is what is matched and returned. A
    # subclass could otherwise answer the check one way and the match another.
    malformed = ('an entry is not an accountNumber and hashValue pair of '
                 'strings')
    entries = list.__getitem__(account_numbers, slice(None))
    hashes = []
    for entry in entries:
        if not issubclass(type(entry), dict):
            raise UnusableAccountNumbersError(malformed)
        number = dict.get(entry, 'accountNumber')
        if not issubclass(type(number), str):
            raise UnusableAccountNumbersError(malformed)
        number = str.__str__(number)
        hash_value = dict.get(entry, 'hashValue')
        if issubclass(type(hash_value), str) and str.__str__(hash_value):
            if number == account_number:
                hashes.append(str.__str__(hash_value))
            continue
        # A hash that cannot be used. An entry that provably names another
        # account -- ASCII digits that are not this number -- cannot change the
        # answer, so it is skipped rather than stopping a lookup that has
        # nothing to do with it. One that could be this account still refuses:
        # ignoring it would turn an odd list into a wrong "not found" or a
        # wrong hash.
        if (number != account_number
                and number.isascii()
                and number.isdigit()):
            _report_skipped_account_entry(entry)
            continue
        raise UnusableAccountNumbersError(malformed)

    if not hashes:
        raise AccountNumberNotFoundError(
                'none of the {} accounts listed has that account '
                'number'.format(len(entries)))
    if len(hashes) > 1:
        raise UnusableAccountNumbersError(
                'that account number is listed {} times'.format(len(hashes)))
    return hashes[0]


#: The furthest off an expiry may be. Schwab documents a 30-minute access
#: token and a refresh token that lasts seven days, so an expiry beyond that is
#: not one Schwab issued in seconds: an ``expires_in`` sent in milliseconds,
#: read as seconds, is 20.8 days. authlib would not refresh such a token until
#: then, and every call in between fails with a 401 and no exception. Refused,
#: it fails loudly on the first refresh instead.
_LONGEST_EXPIRY = 7 * 24 * 60 * 60


#: What an order's fills add up to for one leg: the quantity filled, and the
#: price weighted by quantity. Both are ``Decimal``, and ``average_price`` is
#: ``None`` for a leg whose fills add up to no quantity.
ExecutionTotal = collections.namedtuple(
        'ExecutionTotal', ['quantity', 'average_price'])


class UnusableOrderActivityError(SchwabError, ValueError):
    '''
    Raised by :func:`execution_totals` when an order's activity is not the
    shape it reads, or an execution leg carries a ``mismarkedQuantity`` other
    than zero, whose meaning has not been observed. Its message names the
    field and where it sits, and repeats no value from the order.
    '''


#: The decimal context ``execution_totals`` adds up in, whatever the caller's
#: is. The sums and products of a real order's quantities and prices fit its
#: 60 digits exactly; one that does not -- too large, too small or too long --
#: would be rounded, so ``Inexact`` is trapped and a total is exact or refused.
_EXECUTION_CONTEXT = decimal.Context(
        prec=60, rounding=decimal.ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
        capitals=1, clamp=0, flags=[],
        traps=[decimal.InvalidOperation, decimal.DivisionByZero,
               decimal.Overflow, decimal.Inexact])

#: The average is a quotient, so it is rounded to the same 60 digits rather
#: than refused; one too small to hold would round to zero, so that is refused.
_AVERAGE_CONTEXT = decimal.Context(
        prec=60, rounding=decimal.ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
        capitals=1, clamp=0, flags=[],
        traps=[decimal.InvalidOperation, decimal.DivisionByZero,
               decimal.Overflow, decimal.Underflow])


def _order_number(leg, key, where):
    '''A number from an execution leg, as a ``Decimal``. A float is read from
    its shortest round-trip text, which for a number Schwab sends is its JSON
    text, rather than from its binary expansion.'''
    value = dict.get(leg, key)
    if (issubclass(type(value), bool) or not issubclass(
            type(value), (int, float, decimal.Decimal))):
        raise UnusableOrderActivityError(
                '{} has no {} number'.format(where, key))
    if issubclass(type(value), float):
        value = decimal.Decimal(float.__repr__(value))
    else:
        # Decimal reads an int or a Decimal by its value, not through a
        # subclass's overrides.
        value = decimal.Decimal(value)
    if not value.is_finite():
        raise UnusableOrderActivityError(
                '{} has a {} that is not finite'.format(where, key))
    return value


def _add_up_fills(activities):
    """The per-leg totals of an ``orderActivityCollection`` already known to
    be a list: summed in whatever decimal context is current, and averaged in
    ``_AVERAGE_CONTEXT``."""
    quantities, amounts = {}, {}
    for a, activity in enumerate(
            list.__getitem__(activities, slice(None)), 1):
        where = 'activity {}'.format(a)
        if not issubclass(type(activity), dict):
            raise UnusableOrderActivityError(where + ' is not an object')
        activity_type = dict.get(activity, 'activityType')
        if not issubclass(type(activity_type), str):
            raise UnusableOrderActivityError(
                    where + ' has no activityType string')
        if str.__str__(activity_type) != 'EXECUTION':
            continue
        execution_type = dict.get(activity, 'executionType')
        if not issubclass(type(execution_type), str):
            raise UnusableOrderActivityError(
                    where + ' has no executionType string')
        if str.__str__(execution_type) != 'FILL':
            continue

        legs = dict.get(activity, 'executionLegs')
        if not issubclass(type(legs), list):
            raise UnusableOrderActivityError(
                    where + ' has no executionLegs list')
        for e, leg in enumerate(list.__getitem__(legs, slice(None)), 1):
            at = '{}, execution leg {}'.format(where, e)
            if not issubclass(type(leg), dict):
                raise UnusableOrderActivityError(at + ' is not an object')
            leg_id = dict.get(leg, 'legId')
            if (issubclass(type(leg_id), bool)
                    or not issubclass(type(leg_id), int)):
                raise UnusableOrderActivityError(
                        at + ' has no integer legId')
            leg_id = int.__int__(leg_id)
            quantity = _order_number(leg, 'quantity', at)
            price = _order_number(leg, 'price', at)
            if quantity < 0:
                raise UnusableOrderActivityError(
                        at + ' has a negative quantity')
            if (dict.get(leg, 'mismarkedQuantity') is not None
                    and _order_number(leg, 'mismarkedQuantity', at) != 0):
                raise UnusableOrderActivityError(
                        at + ' has a mismarkedQuantity other than zero, which '
                        'has not been observed, so what it means for the '
                        'quantity filled is unknown')
            quantities[leg_id] = quantities.get(
                    leg_id, decimal.Decimal(0)) + quantity
            amounts[leg_id] = amounts.get(
                    leg_id, decimal.Decimal(0)) + quantity * price

    return {leg_id: ExecutionTotal(
                quantity,
                _AVERAGE_CONTEXT.divide(amounts[leg_id], quantity)
                if quantity else None)
            for leg_id, quantity in quantities.items()}


def execution_totals(order):
    '''
    Returns what an order's fills add up to, per leg, as
    ``{legId: ExecutionTotal(quantity, average_price)}``.

    ``order`` is one parsed order: ``client.get_order(...).json()``, or one
    element of ``client.get_orders_for_account(...).json()``. This makes no
    request.

    Only an activity whose ``activityType`` is ``EXECUTION`` *and* whose
    ``executionType`` is ``FILL`` counts. A canceled or replaced order carries
    an ``EXECUTION`` activity too, with ``executionType`` ``CANCELED`` and
    execution legs whose quantities were never filled, so adding up by
    ``activityType`` alone reports canceled quantity as filled.

    ``quantity`` is the sum of a leg's execution quantities, and
    ``average_price`` is their price weighted by quantity. The arithmetic runs
    in its own decimal context, so the caller's precision and traps do not
    change it. The totals come back as plain ``Decimal`` values, though, so
    arithmetic done on them afterwards is in the caller's context again: with
    ``Inexact`` trapped there, multiplying a quantity by its average, or
    dividing a combined total back into one, can raise. The sums and products
    are exact to 60 significant digits, which a real order does not approach,
    and a number too large, too small or too long for that is refused rather
    than rounded; the average is divided to 60 significant digits. A price is
    as Schwab quotes it, with no contract multiplier applied; for the equity
    fills compared against recorded fill prices, that was per share. A
    float is read into ``Decimal`` from its shortest text, which is its JSON
    text for any price of up to 15 significant digits, so a price of
    ``58.1853`` is exactly that. An order with no fills gives an empty dict.

    Leg totals are not checked against ``filledQuantity``: a ratio spread's
    legs would legitimately differ.

    :param order: The parsed order.
    :raises UnusableOrderActivityError: The order, one of its activities or an
                                        execution leg is not the shape this
                                        reads; a quantity or price is not a
                                        finite number, or a quantity is
                                        negative; a number is too large, too
                                        small or too long to add up exactly;
                                        or a ``mismarkedQuantity`` is not
                                        zero.
    '''
    # Types through type(), which a class cannot fake the way it can fake
    # __class__ for isinstance, and read through the built-in methods.
    if not issubclass(type(order), dict):
        raise UnusableOrderActivityError(
                'expected one parsed order, not a {}'.format(
                    _type_name(order)))
    activities = dict.get(order, 'orderActivityCollection')
    if activities is None:
        return {}
    if not issubclass(type(activities), list):
        raise UnusableOrderActivityError(
                'orderActivityCollection is not a list')

    try:
        with decimal.localcontext(_EXECUTION_CONTEXT):
            return _add_up_fills(activities)
    except decimal.Inexact:
        # Overflow and Underflow are kinds of Inexact.
        raise UnusableOrderActivityError(
                'a quantity or price is too large, too small or too long to '
                'add up exactly') from None


def _expiry_authlib_acts_on(expires_at):
    '''Whether authlib will refresh a token carrying this ``expires_at`` while
    it is still of use.

    authlib's ``OAuth2Token.is_expired`` checks one only when it is an int, and
    refreshes only once it is reached, so one further off than
    ``_LONGEST_EXPIRY`` is refused. The check on a token response and the
    classification of a stored token both ask this, so that they cannot
    disagree: otherwise a response with a milliseconds expiry could be refused
    as never reached while a stored token with the same expiry would be
    reported as retryable on every call.
    '''
    return (isinstance(expires_at, int)
            and expires_at <= time.time() + _LONGEST_EXPIRY)


class TokenErrorKind(str, enum.Enum):
    '''What a failed token refresh calls for, as
    a :class:`TokenRefreshError`'s ``kind`` gives it. A ``str``, so ``kind ==
    'dead_token'`` holds, and a log or a JSON document carries the plain word.
    The words do not change.'''

    __str__ = str.__str__
    __format__ = str.__format__

    #: The refresh token is dead: only a new login, which needs a person at a
    #: browser, gives a working token. What ``refresh_token_invalid`` is
    #: ``True`` for.
    DEAD_TOKEN = 'dead_token'
    #: The token endpoint answered 429, asking for fewer requests, whatever
    #: else it said.
    RATE_LIMITED = 'rate_limited'
    #: The token endpoint answered ``invalid_client`` or
    #: ``unauthorized_client``, the codes RFC 6749 gives for the app's own key
    #: and secret. That is the code Schwab sent, not a verdict: what Schwab
    #: means by it has not been observed, and it has not been seen to be
    #: final.
    APP_REFUSED = 'app_refused'
    #: The token endpoint refused with a 400 or a 401 and no JSON body to say
    #: why. What was seen, not a verdict: it has not been measured to mean
    #: either a dead token or a passing failure.
    REFUSED_NO_BODY = 'refused_no_body'
    #: Anything else, a redirect included. It may clear on its own.
    TRANSIENT = 'transient'


class LoginErrorKind(str, enum.Enum):
    '''What a failed login calls for, as a :class:`LoginExchangeError`'s
    ``kind`` gives it. A ``str``, like :class:`TokenErrorKind`, whose words
    do not change.'''

    __str__ = str.__str__
    __format__ = str.__format__

    #: The token endpoint refused the authorization code (``invalid_grant``):
    #: it was already used, or it expired.
    CODE_REFUSED = 'code_refused'
    #: The token endpoint answered 429.
    RATE_LIMITED = 'rate_limited'
    #: The token endpoint answered ``invalid_client`` or
    #: ``unauthorized_client``, the codes RFC 6749 gives for the app's own key
    #: and secret.
    APP_REFUSED = 'app_refused'
    #: The token endpoint refused with a 400 or a 401 and no JSON body;
    #: ``status_code`` says which.
    REFUSED_NO_BODY = 'refused_no_body'
    #: The person declined at Schwab's page (``access_denied``). Nothing was
    #: sent to the token endpoint.
    ACCESS_DENIED = 'access_denied'
    #: The redirect's ``state`` is not this login's. Nothing was sent.
    STATE_MISMATCH = 'state_mismatch'
    #: The redirect has no code, an empty one, or a fragment. Nothing was
    #: sent.
    MALFORMED_REDIRECT = 'malformed_redirect'
    #: Anything else, an unusable token response included.
    TRANSIENT = 'transient'


class NoBrowserAvailable(SchwabError, webbrowser.Error):
    '''Raised by a login flow that has to open Schwab's sign-in page in a
    browser when Python finds no browser it can run, as on a server with no
    display. It is also a ``webbrowser.Error``, which is what escaped before
    it existed, so code catching that still catches it. A login that cannot
    open a browser can be completed with
    :func:`~schwaby.auth.client_from_manual_flow` instead.

    It cannot see a browser that reports it opened the page when it did not,
    such as a text-mode one on a machine with no display.'''


class PaceRequest(typing.NamedTuple):
    '''What a client is about to send, as the pace given to
    :meth:`~schwaby.client.Client.set_pace` is told it. Never the request's
    query, body or headers.'''

    #: ``'GET'``, ``'POST'``, ``'PUT'`` or ``'DELETE'``.
    method: str
    #: The path, such as ``'/marketdata/v1/quotes'``, or the token
    #: endpoint's, ``'/v1/oauth/token'``.
    path: str
    #: ``'api'`` for a call to the API, ``'token_refresh'`` for the refresh
    #: of the access token, which is sent before the call that needs it.
    purpose: str
    #: ``1`` for a call's first send, ``2`` for its first retry, and so on. A
    #: refresh has the attempt of the call it is sent for.
    attempt: int
    #: What :meth:`~schwaby.client.Client.pace_tag` set around the call, or
    #: ``None``. Always ``None`` for a ``'token_refresh'``: one refresh serves
    #: every call waiting on it, whatever their tags, so a pace with lanes
    #: routes it by ``purpose``.
    tag: object = None


class TokenFreshness(str, enum.Enum):
    '''What :meth:`~schwaby.client.Client.ensure_token_fresh` did.'''

    __str__ = str.__str__
    __format__ = str.__format__

    #: It refreshed the token.
    REFRESHED = 'refreshed'
    #: The token had longer than the margin left, so nothing was sent.
    FRESH = 'fresh'


#: The codes that say the token endpoint refused the app itself.
_APP_REFUSALS = frozenset({'invalid_client', 'unauthorized_client'})


def _safe_code(value):
    '''``value`` when it is a short code of plain ASCII, safe to show or log;
    otherwise ``None``.'''
    if issubclass(type(value), str):
        value = str.__str__(value)
        if re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value):
            return value
    return None


def _sent_code(error):
    '''The code an authlib error carries as the token endpoint or a redirect
    sent it, or ``None``: for a code this library made up itself, for one
    authlib made up before sending anything, and for one not safe to show.
    Read from the instance, never from a class default. Schwab's nesting of
    ``invalid_grant`` inside ``unsupported_token_type``'s description is
    unwrapped to the code it nests, and nothing else is.'''
    fields = vars(error)
    if fields.get('_described_by_this_library') is True:
        return None
    if '_token_endpoint_status' not in fields:
        return None
    code = fields.get('error')
    description = fields.get('description')
    if code == 'unsupported_token_type' and issubclass(type(description),
                                                         str):
        start, end = description.find('{'), description.rfind('}')
        if 0 <= start < end:
            try:
                nested = json.loads(description[start:end + 1])
            except ValueError:
                nested = None
            if isinstance(nested, dict) and nested.get(
                    'error') == 'invalid_grant':
                code = 'invalid_grant'
    return _safe_code(code)


def _no_body(error, status):
    '''Whether the token endpoint refused with a 400 or a 401 and no JSON
    body.'''
    return (vars(error).get('_no_json_body') is True
            and status in (400, 401))


def _refresh_kind(error, invalid, status):
    '''The :class:`TokenErrorKind` of a failed refresh, the first that
    applies.'''
    if invalid:
        return TokenErrorKind.DEAD_TOKEN
    if status == 429:
        return TokenErrorKind.RATE_LIMITED
    if _sent_code(error) in _APP_REFUSALS:
        return TokenErrorKind.APP_REFUSED
    if _no_body(error, status):
        return TokenErrorKind.REFUSED_NO_BODY
    return TokenErrorKind.TRANSIENT


def _exchange_kind(error, answered, status):
    '''The :class:`LoginErrorKind` of a code exchange authlib refused, or
    the token endpoint did, the first that applies.'''
    if not answered:
        # Refused by authlib, before anything was sent: its own exceptions
        # carry their code on the class.
        code = getattr(type(error), 'error', None)
        if code == 'mismatching_state':
            return LoginErrorKind.STATE_MISMATCH
        return LoginErrorKind.TRANSIENT
    sent = _sent_code(error)
    if sent == 'invalid_grant':
        return LoginErrorKind.CODE_REFUSED
    if status == 429:
        return LoginErrorKind.RATE_LIMITED
    if sent in _APP_REFUSALS:
        return LoginErrorKind.APP_REFUSED
    if _no_body(error, status):
        return LoginErrorKind.REFUSED_NO_BODY
    return LoginErrorKind.TRANSIENT


def _redirect_kind(code):
    '''The :class:`LoginErrorKind` of a refusal the redirect itself
    carried.'''
    if code == 'access_denied':
        return LoginErrorKind.ACCESS_DENIED
    if code in _APP_REFUSALS:
        return LoginErrorKind.APP_REFUSED
    return LoginErrorKind.TRANSIENT


def find_token_error(exc, *, follow_context=False, of=None):
    '''The first :class:`TokenRefreshError` or :class:`LoginExchangeError`
    in ``exc`` and the exceptions it was raised from, or ``None``.

    It follows ``__cause__``, the link ``raise ... from`` makes, and stops
    after 20 links or at one already seen. ``__context__``, the link to an
    exception that was being handled when this one was raised, is followed
    only when ``follow_context`` is true: such an exception did not cause
    this one, and following that link can find a token error behind an
    unrelated failure. ``of`` narrows the types looked for, as one type or a
    tuple of them, such as ``of=TokenRefreshError``, since the two errors'
    kinds share some words. It never raises.

    :param exc: an exception, or anything else, which gives ``None``.
    '''
    seen, current = set(), exc
    try:
        if of is None:
            types = (TokenRefreshError, LoginExchangeError)
        elif isinstance(of, type):
            types = (of,)
        else:
            types = tuple(of)
        for _ in range(20):
            if current is None or id(current) in seen:
                return None
            if isinstance(current, types):
                return current
            seen.add(id(current))
            following = getattr(current, '__cause__', None)
            if following is None and follow_context:
                following = getattr(current, '__context__', None)
            current = following
    except Exception:
        return None
    return None


def server_error_code(exc):
    '''The code Schwab sent with a failed refresh or login, as safe text, or
    ``None``.

    That is the token endpoint's own code, such as ``'invalid_grant'`` or
    ``'invalid_client'``, or the one a login's redirect carried, such as
    ``'access_denied'``, with Schwab's nested ``invalid_grant`` unwrapped. It
    is read from the :class:`TokenRefreshError` or
    :class:`LoginExchangeError` that :func:`find_token_error` finds in
    ``exc``. It is ``None`` when Schwab sent no code (a refusal with no JSON
    body), for the codes this library makes up for what it refuses itself
    (``http_<status>``, ``unusable_token_response``, ``missing_code``), for a
    code with anything but ASCII letters, digits, ``_``, ``.`` and ``-``, or
    longer than 64 characters, and for any exception holding no token error.
    It never raises.'''
    found = find_token_error(exc)
    if found is None:
        return None
    return vars(found).get('server_error_code')


class TokenRefreshError(SchwabError):
    '''
    Raised when a refresh of the OAuth token fails: Schwab rejects it, the
    token endpoint answers with something that is not a usable token, or with
    a token at a status other than a success, or the stored token cannot be
    refreshed at all.

    Every request refreshes the token first if it is close to expiring, so this
    surfaces from an ordinary call rather than from anything token-shaped. It
    exists so that an unattended application can catch a failure to refresh
    without importing ``authlib`` and catching an exception type this library
    never mentions. The original error is preserved as ``__cause__``, with
    its ``error``, ``description`` and ``repr`` as authlib made them. Text the
    endpoint sent is escaped and cut to 200 characters in this message and in
    that error's ``str()``, which is what a traceback prints.

    ``token_age`` is the number of seconds since the token was originally
    authorized, or ``None`` if this client was built without token metadata.
    Schwab documents a refresh token as valid for seven days after creation,
    and refreshing does not extend that, so a ``token_age`` past 604800 is past
    its documented term. Schwab has not held exactly to that term, so the age
    alone does not make a failure terminal -- ``refresh_token_invalid`` says
    that -- but past seven days a retryable failure's message says to alert
    someone.

    ``refresh_token_invalid`` is ``True`` when no amount of retrying will
    produce a working token, and only the full authorization_code flow -- which
    needs a human at a browser -- will help. That covers two cases: Schwab
    saying the refresh token is invalid, expired or revoked, and a stored token
    that cannot be used and will not change on its own, which the underlying
    OAuth library reports without contacting Schwab at all -- one with no
    refresh token, or one it cannot send and will never replace by itself: its
    expiry is missing, cannot be read as an int, or is more than seven days
    off, or there is no refresh token to refresh it with.

    A stored token it cannot send that *will* be refreshed is ``False``,
    although nothing was sent: it is replaced as its expiry nears.

    It is ``False`` for everything else, *including* failures this library did
    not recognize -- the conservative direction, since treating a recoverable
    failure as terminal would stop an application which only needed to try
    again. Seen once: Schwab answered a refresh with ``unsupported_token_type``
    and no nested ``invalid_grant``, and it recovered without a new login.

    It is also raised for a refusal whose body is not JSON, such as an empty
    401, whichever version of ``authlib`` is installed: ``authlib`` raised
    ``json.JSONDecodeError`` for one before 1.9 and ``HTTPStatusError`` after.

    ``endpoint_answered`` is ``True`` when the token endpoint answered the
    refresh, and ``status_code`` is then the HTTP status it answered with.
    Both come from the response itself, so they are set whether or not its
    body was JSON. A stored token that fails before anything is sent leaves
    them ``False`` and ``None``. ``rate_limited`` is ``True`` for a 429 and
    nothing else: the endpoint asking for fewer requests, so wait before
    retrying.

    Not every failed refresh is one of these. A server error from the token
    endpoint raises :class:`~schwaby.utils.HTTPStatusError`, a dropped
    connection raises one of ``httpx2``'s transport errors, and a success whose
    body is not JSON at all raises a ``ValueError``.
    '''

    def __init__(self, message, *, token_age=None, refresh_token_invalid=False,
                 status_code=None, endpoint_answered=False,
                 kind=None, server_error_code=None):
        super().__init__(message)

        #: What the failure calls for, a :class:`TokenErrorKind`: decided as
        #: ``dead_token`` when ``refresh_token_invalid`` is ``True``, then
        #: ``rate_limited`` for a 429, then ``app_refused``, then
        #: ``refused_no_body``, then ``transient``. Built without one, it
        #: follows ``refresh_token_invalid`` and ``status_code``.
        if kind is None:
            kind = (TokenErrorKind.DEAD_TOKEN if refresh_token_invalid
                    else TokenErrorKind.RATE_LIMITED if status_code == 429
                    else TokenErrorKind.TRANSIENT)
        self.kind = TokenErrorKind(kind)

        #: The code the token endpoint sent, as :func:`server_error_code`
        #: describes it, or ``None``.
        self.server_error_code = _safe_code(server_error_code)

        #: Seconds since the token was originally authorized, or ``None``.
        self.token_age = token_age

        #: ``True`` when no retry can produce a working token -- Schwab
        #: rejected the refresh token, or the stored token cannot be used and
        #: will not change on its own -- and only a new login flow will help.
        #: ``False`` when the failure may be transient, or was not recognized.
        self.refresh_token_invalid = refresh_token_invalid

        #: The HTTP status the token endpoint answered with, or ``None`` when
        #: nothing was sent.
        self.status_code = status_code

        #: ``True`` for a 429: the endpoint asked for fewer requests.
        self.rate_limited = status_code == 429

        #: ``True`` when the token endpoint answered the refresh.
        self.endpoint_answered = endpoint_answered


class LoginExchangeError(SchwabError, OAuthError):
    '''
    Raised when a login cannot exchange its authorization code for a token.
    The redirect carries the authorization server's refusal (``access_denied``
    when the user declines), has no code or an empty one, carries a fragment,
    or has a ``state`` that does not match the login's; or the token endpoint
    refuses the code, or answers with something that is not a usable token
    (``unusable_token_response``), or with a token at a status other than a
    success (``http_<status>``).

    It is also ``OAuthError``, the type authlib raises for a refused
    exchange, so a caller catching ``OAuthError`` catches it, and ``error``
    and ``description`` carry the refusal's code and text. Text from the
    redirect or the endpoint reaches whatever logs the exception, so it has
    its control characters escaped and is cut to 200 characters, and a value
    that is not a string is given as its repr. authlib's own error is not
    chained as ``__cause__``, since its text is not escaped and a logged
    traceback prints it. When this library refuses a token response as
    ``unusable_token_response``, the description is its own text; an endpoint
    sending that code has its text treated like any other.

    Unlike :class:`TokenRefreshError` it has no ``token_age`` or
    ``refresh_token_invalid``. No token exists yet, and an authorization code
    is good for one exchange, so every refusal means starting the login again.
    Nothing is written, so a token file already on disk is left as it was.

    A refusal whose body is not JSON raises this too. A server error from the
    token endpoint, or a success whose body is not JSON, raises as it does
    during a call rather than as this.

    ``endpoint_answered`` is ``True`` when the token endpoint answered the
    exchange, and ``status_code`` is then the HTTP status it answered with,
    whether or not its body was JSON. It is ``False``, and ``status_code``
    ``None``, when the login was refused before the code was sent: the
    redirect carried a refusal, a fragment or no code, or its ``state`` did
    not match. ``rate_limited`` is ``True`` for a 429 and nothing else.
    '''

    def __init__(self, error=None, description=None, uri=None, *,
                 status_code=None, endpoint_answered=False,
                 kind=None, server_error_code=None):
        super().__init__(error, description, uri)

        #: What the failure calls for, a :class:`LoginErrorKind`. ``error``
        #: keeps the refusal's own code, for wording that tells the cases
        #: of one kind apart. Built without one, it is ``rate_limited`` for a
        #: 429 and ``transient`` otherwise.
        if kind is None:
            kind = (LoginErrorKind.RATE_LIMITED if status_code == 429
                    else LoginErrorKind.TRANSIENT)
        self.kind = LoginErrorKind(kind)

        #: The code Schwab sent, from the token endpoint or the redirect, as
        #: :func:`server_error_code` describes it, or ``None``.
        self.server_error_code = _safe_code(server_error_code)

        #: The HTTP status the token endpoint answered with, or ``None`` when
        #: the code was never sent.
        self.status_code = status_code

        #: ``True`` for a 429: the endpoint asked for fewer requests.
        self.rate_limited = status_code == 429

        #: ``True`` when the token endpoint answered the exchange.
        self.endpoint_answered = endpoint_answered

        #: ``True`` when the authorization code reached the token endpoint,
        #: which answered; ``False`` when the login was refused before it
        #: was sent. The same as ``endpoint_answered``.
        self.sent = endpoint_answered


class LazyLog:
    'Helper to defer evaluation of expensive variables in log messages'
    def __init__(self, func):
        self.func = func
    def __str__(self):
        return self.func()


class Utils(EnumEnforcer):
    '''Helper for placing orders on equities. Provides easy-to-use
    implementations for common tasks such as market and limit orders.'''

    def __init__(self, client, account_hash):
        '''Creates a new ``Utils`` instance. For convenience, this object
        assumes the user wants to work with a single account hash at a time.'''
        super().__init__(True)

        self.client = client
        self.account_hash = account_hash

    def set_account_hash(self, account_hash):
        '''Set the account hash used by this ``Utils`` instance.'''
        self.account_hash = account_hash

    def extract_order_id(self, place_order_response):
        '''Extracts the order ID from a response returned by
        :meth:`Client.place_order() <schwaby.client.Client.place_order>`.

        Every outcome other than success raises, and each one raises something
        different, because they call for different handling:

        * :class:`UnsuccessfulOrderException` --- Schwab rejected the order.
          Its own explanation is in the message and the whole response is on
          the exception. Nothing was placed.
        * :class:`MissingLocationHeaderError` and
          :class:`UnrecognizedLocationError` --- Schwab accepted the order and
          the ID could not be read. **The order may be live.** Both subclass
          :class:`OrderIdNotFoundError`, so catch that unless you need to tell
          them apart.
        * :class:`AccountHashMismatchException` --- the response belongs to a
          different account than this :class:`Utils` was built with. **The
          order is live**, on the account Schwab named; the exception carries
          its ID.

        :param place_order_response: Response from
                                     :meth:`Client.place_order()
                                     <schwaby.client.Client.place_order>`.
        '''
        if place_order_response.is_error:
            raise UnsuccessfulOrderException(
                    place_order_response,
                    'order not successful: status {}{}'.format(
                        place_order_response.status_code,
                        _describe_error(place_order_response)))

        try:
            location = place_order_response.headers['Location']
        except KeyError:
            # `from None` because the chained "During handling of the above
            # exception" opens the traceback with KeyError: 'Location', which
            # in a log capture reads as the library crashing on a dict lookup
            # -- the opposite of what the message below is trying to say.
            raise MissingLocationHeaderError(
                    place_order_response, None,
                    'order was accepted but the response carried no Location '
                    'header, so it has no order ID to return. The order may be '
                    'live: check get_orders_for_account.') from None

        m = re.match(
                r'https://api.schwabapi.com/trader/v1/accounts/(\w+)/orders/(\d+)',
                location)

        if m is None:
            raise UnrecognizedLocationError(
                    place_order_response, location,
                    'order was accepted but its Location header does not look '
                    'like an order URL, so it has no order ID to return. The '
                    'order may be live: check get_orders_for_account. Header '
                    'was: {!r}'.format(location))
        account_hash, order_id = m.group(1), int(m.group(2))

        if str(account_hash) != str(self.account_hash):
            raise AccountHashMismatchException(
                place_order_response, order_id, account_hash,
                'order {} was placed on account {!r}, but this Utils was built '
                'with {!r}. The order is live on the account Schwab named; its '
                'id is on this exception as .order_id.'.format(
                    order_id, account_hash, self.account_hash),
                expected_account_hash=self.account_hash)

        return order_id
