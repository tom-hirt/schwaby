import collections.abc
import decimal
import json
import re
import unicodedata
from schwaby import _redact
from schwaby.streaming import StreamJsonDecoder
from schwaby.utils import SchwabError


def get_logger():
    return _redact.get_logger(__name__)



class HeuristicJsonDecoder(StreamJsonDecoder):
    def decode_json_string(self, raw):
        '''
        Attempts the following, in order:
        
        1. Return the JSON decoding of the raw string. 
        2. Replace all instances of ``\\\\\\\\`` with ``\\\\`` and return the 
           decoding.

        Note alternative (and potentially expensive) transformations are only 
        performed when ``JSONDecodeError`` exceptions are raised by earlier 
        stages.
        '''

        # Note "no cover" pragmas are added pending addition of real-world test 
        # cases which trigger this issue.

        try:
            return json.loads(raw)
        except json.decoder.JSONDecodeError:  # pragma: no cover
            if isinstance(raw, (bytes, bytearray)):
                # The repair works on text. Replacing in bytes with str
                # arguments raises TypeError, which would end the receive
                # loop unreported, so a bytes frame keeps its decode error.
                raise
            raw = raw.replace('\\\\', '\\')

        return json.loads(raw)  # pragma: no cover


#: The largest ``signScale`` :func:`decode_decimal` will compute with. A .NET
#: ``Decimal`` scale is 0 to 28, which is a ``signScale`` of at most 57.
MAX_SIGN_SCALE = 64

#: The largest exponent a bare value may carry, in either direction. The
#: dict path is bounded by :data:`MAX_SIGN_SCALE` for a measured reason; a
#: bare value needs the same bound for the same reason. ``1E-999999999``
#: parses, and then compares neither ``== 0`` nor ``<= 0`` while behaving as
#: zero in arithmetic -- so a corrupt ``LeavesQuantity`` reads as still
#: outstanding forever. ``1E999999999`` parses and makes the *caller\'s* next
#: arithmetic raise `decimal.Overflow`, which is an `ArithmeticError` and so
#: escapes the one ``except`` this module tells callers to write. No real
#: field on this feed is within thirty orders of magnitude of either.
MAX_EXPONENT = 64

#: What a bare numeric value may look like. `decimal.Decimal` accepts rather
#: more than this -- PEP 515 underscores anywhere (``1_0`` is ten, ``12_`` is
#: twelve where `int` refuses it outright), surrounding whitespace, a leading
#: ``+``, and every Unicode decimal digit, so ``'\u0663'`` is three and
#: ``'\uff11\uff12'`` is twelve. Each of those is a corrupt field decoding as
#: a confident wrong number, which is the same defect the mantissa members
#: are validated against, so the two paths refuse the same tokens.
#:
#: The exponent is bounded here, at four significant digits, and not left to
#: the magnitude check after construction: an exponent near 10**18 or beyond
#: -- the limit is on the adjusted exponent, and differs by sign -- makes
#: `decimal.Decimal` itself raise `InvalidOperation`, neither a SchwabError nor
#: a ValueError, before that check can run.
_BARE_NUMBER = re.compile(
        r'-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?0*[0-9]{1,4})?\Z')

#: Every key a serialized ``System.Decimal`` is known to carry. An object with
#: none of them is not one, and would otherwise take the mantissa-less
#: shortcut and decode as zero -- the worst wrong answer available on this
#: feed -- without anything having looked at it.
#:
#: A key *alongside* these refuses the object too, and is logged once. See
#: :func:`decode_decimal` for why decoding around one cannot be made to
#: work.
_DECIMAL_KEYS = frozenset(('lo', 'mid', 'hi', 'signScale'))


#: How many distinct unknown keys to name in the log before giving up. A cap
#: rather than an unbounded set, because the thing being counted is attacker-
#: or corruption-controlled and this set never shrinks.
_MAX_REPORTED_KEYS = 32

_reported_keys = set()


def _report_unknown_keys(unknown):
    """Say once, per distinct key, that Schwab sent something new.

    The object itself is refused --- see `decode_decimal` for why that is
    the whole rule --- so the caller already gets an exception naming the
    key, on every decimal field of every message. This line is the operator
    signal beside it: once per key, saying *this is a schema change rather
    than a bad value*, and greppable without a code change.

    Once per *process*, not per connection: the set is module-level and is
    not cleared by `login` or `close`, which do clear the absorbed counters.
    That is deliberate --- a key Schwab added is a fact about the venue, not
    about one socket --- and it means two clients in one process share the
    suppression.
    """
    room = _MAX_REPORTED_KEYS - len(_reported_keys)
    if room <= 0:
        return
    # Normalised to `str` on the way in so a non-string key cannot slip the
    # dedup by being unequal to its own spelling, and truncated to the room
    # left rather than after the fact: the cap is justified by input nobody
    # here controls, and `update` before the check would let one object
    # carrying 500 unknown keys put all 500 in a set bounded at 32.
    fresh = sorted({_safe_key(k) for k in unknown} - _reported_keys)[:room]
    if not fresh:
        return
    _reported_keys.update(fresh)
    if len(_reported_keys) >= _MAX_REPORTED_KEYS:
        get_logger().warning(
                'That is %d distinct unknown keys, which is as many as '
                'schwaby will name. Any further ones are refused the same '
                'way and are not reported.', _MAX_REPORTED_KEYS)
    get_logger().warning(
            'Schwab sent %s inside a decimal object, which this version of '
            'schwaby does not know about. Every decimal field carrying it is '
            'refused until schwaby knows the key -- an unrecognised key may '
            'be a renamed one, and decoding around it is a wrong price where '
            'it is. Reported once per key, not per message. Please open an '
            'issue at https://github.com/tom-hirt/schwaby/issues with the '
            'field: it is undocumented publicly and a capture is the only '
            'way anyone learns what a new key means.',
            ', '.join(_quoted(k) for k in fresh))


class UnusableDecimalScale(SchwabError, ValueError):
    '''Raised by :func:`decode_decimal` when an object cannot be turned into
    a number without guessing: a member or ``signScale`` that is not what it
    should be, a scale too large to be real, a key this version does not
    recognise, or a value that is not a number at all.

    An absent ``signScale`` is **not** one of these --- it is scale 0, which
    is how ``AskSize`` and ``BidSize`` arrive: one quote carries an ``Ask``
    of ``{"lo": "21480000", "signScale": 12}`` beside an ``AskSize`` of
    ``{"lo": "15300"}``.

    The scale is what turns the mantissa into a number, so guessing one is a
    silent wrong answer by construction --- and the guess that suggests itself,
    six decimal places, is only the value that happens to be common.

    A real .NET ``Decimal`` scale is 0 to 28, so ``signScale`` does not exceed
    about 57. Anything larger is refused rather than computed: past roughly two
    million the value underflows to a zero that compares equal to zero, and a
    corrupt ``LeavesQuantity`` reading as zero is a complete fill.

    A :class:`~schwaby.utils.SchwabError` so ``except SchwabError`` covers it
    like everything else this library defines, and a :class:`ValueError` so it
    also reads as what it is.
    '''


def _safe_keys(unknown):
    """The unknown keys as one bounded phrase.

    Bounded in count as well as per key: `_safe_key` caps each name but not
    the join, and an object carrying 100,000 unknown keys would otherwise
    produce a 2.9 MB exception message. Transient rather than retained, which
    is why it is a smaller problem than the sets -- but an exception a caller
    logs is retained by whatever logs it.
    """
    # Quoted, like the once-per-key log line. A key is venue text, and
    # joined bare, a newline in one would forge a second line wherever the
    # exception is logged -- including through the recipe the streaming
    # docs give for logging it.
    names = [_quoted(name) for name in sorted(map(_safe_key, unknown))]
    if len(names) <= 8:
        return ', '.join(names)
    return '{} and {} more'.format(', '.join(names[:8]), len(names) - 8)


def _quoted(name):
    """A bounded name, quoted, and bounded again.

    Quoted because a name is venue text, and joined bare a newline in one
    forges a second line wherever the message is logged. Bounded again
    because `repr` escapes a character to as many as ten, so a 64-character
    name could quote to 640 -- and both the exception and the once-per-key
    log line join these.
    """
    quoted = repr(name)
    return quoted if len(quoted) <= 80 else quoted[:77] + '...'


def _type_name(value):
    """A value's type name, for a message about a value that could not be read.

    Read through `type`'s own descriptor. `type(x).__name__` consults the
    metaclass first, and a metaclass can make that raise inside the very
    branch written not to.
    """
    try:
        # A plain str too: a class's `__name__` may be set to a str subclass,
        # and the descriptor hands it back as it is.
        name = str.__str__(type.__dict__['__name__'].__get__(type(value)))
    except Exception:
        name = 'object'
    return name if len(name) <= 64 else name[:61] + '...'


def _safe_key(key):
    """A key's name, bounded and guaranteed not to raise.

    `str()` of a wide integer raises past `sys.get_int_max_str_digits()`, and
    a key is whatever the decoder produced. Bounded in length as well as in
    count: `_reported_keys` never shrinks, so a 2 MB key would be retained
    forever and named in full in a 2 MB log line.
    """
    try:
        text = str(key)
    except Exception:
        # Bounded too. A type whose name is enormous would otherwise go
        # straight into `_reported_keys`, which never shrinks -- the exact
        # scenario the length bound exists for, through the branch written
        # to be safe.
        text = '<{} that cannot be named>'.format(_type_name(key))
    # A plain `str`: `str()` hands back a subclass untouched when `__str__`
    # returns one, and its own `len`, `hash` and slicing would run next.
    text = _redact.before_cut(str.__str__(text), 64)
    return text if len(text) <= 64 else text[:61] + '...'


def _safe_repr(value):
    """`repr` that cannot raise on the value it is describing.

    An error message is not the place to hit the integer digit limit:
    `repr` of a dict containing a 5,000-digit int raises `ValueError`, which
    would replace a `SchwabError` describing corruption with a bare exception
    describing nothing. Measured -- `decimal.Decimal(10 ** 10000)` is fine and
    `str()` of that integer is not, so the hazard is the formatting rather
    than the arithmetic.
    """
    try:
        # A plain str, for the reason `_safe_key` gives.
        text = str.__str__(repr(value))
    except Exception:
        # Not just ValueError. `json.loads` yields plain dicts, whose `repr`
        # raises only the integer digit limit -- but `StreamJsonDecoder` is a
        # public extension point, which is the same argument that made
        # `relabel_message` handle a non-string key.
        text = '<{} that cannot be formatted>'.format(_type_name(value))
    # A plain dict's repr escapes a newline, but neither a value's own
    # `__repr__` nor a class name need -- a mapping type or a str subclass
    # from a custom decoder supplies both -- and every refusal ends with this
    # text, so either could forge a line. Cut, escaped, and cut again, since
    # escaping one character can take ten. Redacted before any cut, so a
    # secret across the cut leaves nothing.
    text = _redact.before_cut(text, 200)
    if len(text) > 200:
        text = text[:197] + '...'
    if not text.isprintable():
        text = ''.join(c if c.isprintable() else repr(c)[1:-1] for c in text)
    return text if len(text) <= 200 else text[:197] + '...'


def _decode_bare(value):
    """The other half of the surface, validated the same way as a member.

    This path is defensive: no captured payload has carried a money or
    quantity field as a bare number or string, and the docstring above says
    so rather than implying otherwise. It carries the same untrusted text as
    the mantissa members, so it is validated as strictly.
    `decimal.Decimal`'s parser accepts more than `int`'s does, so unchecked,
    every token `_unsigned` refuses would decode here instead: ``'1_0'`` as
    ten, ``'\u0663'`` as three, ``' 12 '`` and ``'+12'`` and
    ``'\uff11\uff12'`` as twelve. All of them are `json.loads`-reachable and
    none of them would raise.
    """
    kind = type(value)   # not `isinstance`; see `decode_decimal`
    if issubclass(kind, bool) or value is None:
        # `None` is handled by the caller; a bool is not a number here for the
        # same reason it is not one in a mantissa.
        raise UnusableDecimalScale(
                'not a number: {}'.format(_safe_repr(value)))

    # Each branch works on the exact built-in type, never the subclass that
    # arrived. A subclass decides its own `str`, `isascii`, `bit_length` and
    # `is_finite`, so it could decode as another number or raise past the one
    # `except` callers write. `json.loads` never produces one; a custom
    # decoder can.
    if issubclass(kind, decimal.Decimal):
        value = result = decimal.Decimal(value)
    elif issubclass(kind, int):
        value = int.__index__(value)
        # Bounded before conversion, cheaply. `decimal.Decimal(int)` is
        # fine at any width (measured); it is `str()` and `repr()` that raise
        # a bare `ValueError` past `sys.get_int_max_str_digits()`. `_safe_repr`
        # handles that where it matters, so this is a cheap pre-check rather
        # than the thing standing between a caller and an escape. 256 bits is
        # 78 digits, well outside the bound below, so it refuses only what
        # that would refuse anyway.
        if value.bit_length() > 256:
            # Reported by width rather than by value: formatting the value
            # is the operation being guarded against.
            raise UnusableDecimalScale(
                    'a {}-bit integer is outside +/-1E{}, so it is not a '
                    'real value'.format(value.bit_length(), MAX_EXPONENT))
        result = decimal.Decimal(value)
    elif issubclass(kind, float):
        # `str` of a float is short, ASCII and never surprising; going
        # through it stops the binary expansion coming along.
        value = float.__float__(value)
        result = decimal.Decimal(str(value))
    elif issubclass(kind, str):
        value = str.__str__(value)
        if not (value.isascii() and _BARE_NUMBER.match(value)):
            # The empty string reaches here from the SUBSCRIBED ack.
            raise UnusableDecimalScale(
                'not a number: {}'.format(_safe_repr(value)))
        result = decimal.Decimal(value)
    else:
        raise UnusableDecimalScale(
                'not a number: {}'.format(_safe_repr(value)))

    if not result.is_finite():
        # `json.loads` accepts bare NaN and Infinity, and Decimal accepts
        # those and their strings. A NaN quantity is not caught downstream
        # either: `leaves <= 0` is False for NaN, so a corrupt field reads as
        # still outstanding forever.
        raise UnusableDecimalScale(
                'not a number: {}'.format(_safe_repr(value)))

    exponent = result.as_tuple().exponent
    if not (-MAX_EXPONENT <= exponent
            and result.adjusted() <= MAX_EXPONENT):
        raise UnusableDecimalScale(
                'magnitude is outside 1E+/-{}, so this is not a real '
                'value: {}'.format(MAX_EXPONENT, _safe_repr(value)))

    return result


def _unsigned(member, name, value, bits):
    """One place that decides what a decimal member is.

    An `int` that is not a `bool`, or a string of ASCII digits, and in either
    case inside an unsigned range ``bits`` wide. `bool` is excluded because
    `int(True)` is 1 and `int(False)` is 0, so a JSON `true` in a mantissa
    would decode as a number rather than as the corruption it is -- the same
    reason the order setters refuse one.

    ``bits`` is 96 for a ``lo`` carrying the whole mantissa, which is what
    Schwab sends, and 32 for the scale and for each member of the
    three-member layout.
    """
    kind = type(member)   # not `isinstance`; see `decode_decimal`
    if issubclass(kind, bool) or not issubclass(kind, (int, str)):
        ok = False
    elif issubclass(kind, str):
        # The plain text, as `int.__index__` below gives the plain value: a
        # subclass's own `isdigit` and `lstrip` would otherwise decide it.
        member = str.__str__(member)
        # `isdigit` alone is not ASCII: '\u00b2'.isdigit() is True and
        # `int` refuses it, while '\u0663'.isdigit() is True and `int`
        # decodes it as 3. Both are corruption, and only one raises.
        ok = member.isascii() and member.isdigit()
        if ok:
            # Length before value, because `int()` refuses a string past
            # `sys.get_int_max_str_digits()` -- 4300 by default -- with a
            # bare `ValueError` that is not a SchwabError, and the range
            # check below cannot run until `int()` has returned. Leading
            # zeros are stripped rather than counted so a padded member is
            # judged on its value: the widest has as many digits as
            # `2 ** bits - 1`, ten at 32 bits and twenty-nine at 96.
            digits = member.lstrip('0') or '0'   # an all-zero member
            ok = len(digits) <= len(str(2 ** bits - 1))
            member = int(digits) if ok else 0
        else:
            member = 0
    else:
        # A plain int, not whatever subclass arrived. The member is formatted
        # into the result, and a subclass decides its own `str` and `format`,
        # so it could decode as a different number or raise -- and a custom
        # JSON decoder can hand one over. `int.__index__` is int's own
        # conversion, which the subclass cannot override.
        member = int.__index__(member)
        ok = True
    if not ok or not 0 <= member < 2 ** bits:
        raise UnusableDecimalScale(
                '{} is not an unsigned {}-bit integer: {}'.format(
                    name, bits, _safe_repr(value)))
    return member


def decode_decimal(value):
    '''Decodes the scaled-integer decimal objects Schwab streams on
    ``ACCT_ACTIVITY``.

    Those fields do not arrive as numbers. They arrive as a serialized .NET
    ``System.Decimal``::

        {"lo": "7350000", "signScale": 12}       ->  Decimal('7.350000')

    a 96-bit integer mantissa, which arrives whole in ``lo`` as a string of
    digits, and a ``signScale`` packing the scale in its magnitude and the
    sign in its parity. The conversion is undocumented publicly; Schwab's
    Trader API support has confirmed it, and it reproduces every payload it
    has been checked against, including Schwab's own worked example
    ``{"lo": "40000000", "signScale": 13}`` -> ``Decimal('-40.000000')``.

    Five things this gets right that a first attempt usually does not, each of
    which costs a value, usually as a wrong number rather than an error:

    * **The mantissa can be wider than 32 bits.** ``lo`` carries all of it, and
      a fill principal above ``4294.967295`` at ``signScale`` 12 needs more.
      A decoder that treats ``lo`` as one 32-bit member of .NET's three
      refuses the largest amounts on the feed, or wraps them.
    * **An odd** ``signScale`` **means negative.** The sign is not the side:
      direction is ``BuySellCode``, and this branch exists so a genuinely
      negative field, such as principal on a buy, does not decode positive.
    * **A mantissa-less object is zero, not unknown.** ``LeavesQuantity``
      arrives that way on the final fill of a completed order, and reading it
      as unknown reports a complete fill as still outstanding.
    * **An absent** ``signScale`` **is scale 0, not a missing scale.** The
      serializer omits whatever is zero, and that applies to the scale as
      much as to a mantissa member: ``AskSize`` and ``BidSize`` arrive as
      ``{"lo": "15300"}`` beside an ``Ask`` of
      ``{"lo": "21480000", "signScale": 12}`` on the same quote. Refusing
      the shape costs both sizes on every quote it appears in.
    * **A** ``Decimal`` **is returned, never a float.** These are money;
      :meth:`OrderBuilder.set_price
      <schwaby.orders.generic.OrderBuilder.set_price>` refuses floats for the
      reason :ref:`price_strings` gives, so a decoded value can be fed
      straight back into a reprice.

    :param value: A decimal object, or a number or string. A non-object is
                  validated and returned as a :class:`~decimal.Decimal`. That
                  path is defensive rather than observed: in the captures
                  this was checked against, every money and quantity field
                  is an object, on every message. What *is* measured is the
                  empty string, which arrives as the ``SUBSCRIBED`` ack's
                  ``MESSAGE_DATA`` and reaches here from any consumer
                  decoding fields generically.
    :raises UnusableDecimalScale: if a lone ``lo`` is not an unsigned
                                  integer below ``2 ** 96``, if any member
                                  of a ``mid`` or ``hi`` layout is not an
                                  unsigned 32-bit integer, if the
                                  ``signScale`` is outside ``0..64``, or if
                                  the object carries any
                                  key that is not one of ``lo``, ``mid``,
                                  ``hi`` and ``signScale`` --- see below. An
                                  object carrying *none* of the four is
                                  refused with a different message, on the
                                  assumption that a schema change keeps at
                                  least one of the four names. If Schwab ever
                                  renames all of them at once that message
                                  will read as a caller's mistake, and only
                                  the exception will say so --- there is no
                                  once-per-key line for that case.

                                  An empty object is neither: it
                                  is the omit-everything spelling of zero,
                                  and decodes as one. Each of these is
                                  refused rather than computed with, because
                                  each produces a plausible wrong number
                                  rather than an error.

                                  **Any** unrecognised key refuses the
                                  object. Not decoded around: an
                                  unrecognised key may be a renamed one, and
                                  from inside a payload the two are
                                  indistinguishable --- the serializer omits
                                  what is zero, so a component that is
                                  missing because it was renamed looks
                                  exactly like one that is missing because
                                  it is zero.

                                  The cost is deliberate. A key Schwab adds
                                  arrives on every decimal object at once, so
                                  every money and quantity field raises until
                                  ``schwaby`` knows the key --- a loud,
                                  same-day failure, catchable per field, and
                                  the key is named in the log on the first
                                  message. The alternative on this feed is a
                                  silent wrong price.

                                  A mapping whose ``items()`` cannot be read
                                  is refused, and so is one carrying the
                                  same key twice once its names are reduced
                                  to plain text.
    :raises UnusableDecimalScale: if a non-object value is not a number, or
                                  carries an exponent past 64 in either
                                  direction. The empty string reaches this
                                  function from the ``SUBSCRIBED`` ack.

    Every failure is a :class:`~schwaby.utils.SchwabError` and a
    :class:`ValueError`, so a caller wrapping each field in one ``except``
    keeps the rest of the message. That is the point of the bound above: a
    hang is not catchable, so it must not be reachable.
    '''
    if value is None:
        return None

    # Routed on `type()`, not `isinstance`, here and in the helpers: an
    # `isinstance` check also believes an object's `__class__`, so a mock or a
    # proxy claiming to be a dict would pass it and then fail dict's own
    # methods with a bare TypeError. Any `Mapping` is a decimal object, not
    # only a dict, so a `UserDict` or a `mappingproxy` is accepted rather than
    # refused as "not a number".
    try:
        is_mapping = issubclass(type(value), collections.abc.Mapping)
    except Exception:
        # The ABC's cache hashes the class, and a metaclass can make a class
        # unhashable: a bare TypeError, past the one `except` callers write.
        is_mapping = False
    if not is_mapping:
        return _decode_bare(value)

    # Read once, through the mapping's own `items()`, into a plain dict, and
    # every read below is from that copy: a `get` that answers differently
    # the second time could choose the lone-lo layout and drop `mid`. Through
    # the mapping's own method rather than `dict.items`, which reads dict's
    # storage directly and so sees nothing in a dict subclass keeping its data
    # elsewhere -- measured, `DotMap` read that way decodes a $5,000 principal
    # as a clean zero. A mapping whose `items()` cannot be read is refused.
    #
    # JSON object keys are always strings. A str key is reduced to its plain
    # text before it is compared, so a subclass cannot run its own `__eq__` or
    # `__hash__`; anything that is not a str is an unknown key, named but never
    # hashed or compared. Two keys with the same plain text are refused, since
    # only one of them can be the member.
    try:
        pairs = [(key, member) for key, member in value.items()]
    except Exception:
        raise UnusableDecimalScale(
                'a mapping whose items could not be read: {}'.format(
                    _safe_repr(value))) from None
    fields = {}
    unknown = []
    for key, member in pairs:
        name = str.__str__(key) if issubclass(type(key), str) else None
        if name is not None and name in fields:
            raise UnusableDecimalScale(
                    'decimal object carries {!r} twice: {}'.format(
                        name, _safe_repr(value)))
        if name in _DECIMAL_KEYS:
            fields[name] = member
        else:
            unknown.append(key)
    if unknown:
        # Any unrecognised key refuses the object. Outright, with no attempt
        # to decode around it.
        #
        # Decoding around an unknown key -- ignoring one on an object that
        # looks complete, refusing one where a component is missing, on the
        # reasoning that the key might be that component renamed -- cannot be
        # made safe, because "missing" has more spellings than can be listed:
        # the key absent, the key present and null, the key present and zero.
        # Each can turn a $7.35 limit price into $7,350,000, and the last
        # cannot be guarded at all -- `signScale: 0` genuinely means scale 0,
        # so refusing it would refuse a real `AskSize`.
        #
        # Measured against a real capture, that approach also buys nothing:
        # against a rename that keeps the old key at its default, 27 of 37
        # objects decode to a wrong number, which is exactly what no guard at
        # all does. It has the cost of refusing and the risk of not
        # refusing.
        #
        # So: the cost of this is that a key Schwab *adds* -- which arrives
        # on every decimal object at once -- makes every money and quantity
        # field raise until schwaby knows the key. That is a loud, same-day,
        # catchable failure, and `UnusableDecimalScale` is per field, so a
        # caller wrapping each one keeps the rest of the message. The
        # alternative on this feed is a silent wrong price on a funded
        # account, and between those two there is no contest.
        if not fields:
            # Not a decimal object at all -- a PascalCase
            # `{"Lo": ..., "SignScale": ...}`, or something unrelated. Worth
            # its own message: this is a caller's mistake, where the branch
            # below is the venue changing under everyone.
            raise UnusableDecimalScale(
                    'not a decimal object -- no lo, mid, hi or signScale, '
                    'only {}: {}'.format(
                        _safe_keys(unknown),
                        _safe_repr(value)))
        # Reported here and not above the raise: this branch is the venue
        # changing under everyone, and the one above is a caller handing this
        # function the wrong object. Reporting both would put the second
        # one's key names into a set that is bounded, module-level and never
        # cleared -- measured, one real ACCT_ACTIVITY frame walked generically
        # fills all 32 slots with `AccountNumber`, `BaseEvent`, `EventType`
        # and the like, after which a genuine schema addition logs nothing.
        # The report is the whole of what this adds over refusing silently,
        # and non-schema input could consume all of it for the life of the
        # process.
        _report_unknown_keys(unknown)
        raise UnusableDecimalScale(
                'decimal object carries {}, which this version of schwaby '
                'does not know. Refused rather than decoded around, because '
                'an unrecognised key may be a renamed one and the difference '
                'is not visible from here: {}'.format(
                    _safe_keys(unknown),
                    _safe_repr(value)))

    # The scale is read, validated and bounded *before* the mantissa-less
    # shortcut, not after. That shortcut returns without looking at anything
    # else, so read after it, `{"signScale": "garbage"}` -- or 1.9, or -1, or
    # 65 -- would decode as a clean zero while the identical corruption one
    # key over raises. No wrong number would come of it, since a
    # mantissa-less object is zero at any scale, but it would discard the
    # only signal that the message is corrupt.
    #
    # An absent `signScale` is scale 0, by the same rule as an absent
    # mantissa member: the serializer omits what is zero. A missing scale
    # looks as though it cannot be told from a lost one, but `AskSize` and
    # `BidSize` arrive as `{"lo": "15300"}` with no scale at all, beside an
    # `Ask` of `{"lo": "21480000", "signScale": 12}` on the same quote.
    # $21.48 and 15300 shares, which is coherent; 0.0153 shares is not. So
    # the shape is ordinary traffic, and refusing it would cost both sizes on
    # every quote.
    #
    # `.get` on the plain copy, so an absent key and an explicit JSON `null`
    # are one absence here, exactly as they are in the member loop: the two
    # follow one rule.
    scale_member = fields.get('signScale')
    scale = _unsigned(0 if scale_member is None else scale_member,
                      'signScale', value, 32)

    # Bounded explicitly rather than left to the arithmetic to refuse.
    # `10 ** (scale // 2)` on a hostile exponent builds an astronomical integer
    # and hangs the thread, which a per-item try/except cannot rescue -- but
    # `scaleb` is not the answer on its own either: its operand limit only
    # trips far out, and every scale between roughly two million and four
    # million underflows to a zero that compares equal to zero instead of
    # raising. Zero is the worst wrong answer available on this feed.
    if not 0 <= scale <= MAX_SIGN_SCALE:
        raise UnusableDecimalScale(
                'signScale {} is outside 0..{}, so it is not a real one: '
                '{}'.format(scale, MAX_SIGN_SCALE, _safe_repr(value)))

    if all(fields.get(k) is None for k in ('lo', 'mid', 'hi')):
        # Measured on three fields of one payload: a $0 commission, a market
        # order's absent limit price, and LeavesQuantity on a final fill.
        #
        # All three parts, not `lo` alone: the serializer omits zero members,
        # so under the three-member layout -- never captured, but the only
        # reading the names support -- a value whose low 32 bits are zero
        # would arrive as `{"mid": 1, ...}`, and keying on `lo` would decode
        # it as zero.
        #
        # Returning `Decimal(0)` rather than falling through is normalisation
        # and only that, since the scale above is validated either way: falling
        # through gives `Decimal('0.000000')` at scale 12, and
        # `Decimal('-0.000000')` at scale 13. Both compare equal to zero, and
        # the second one *prints* as a negative quantity.
        return decimal.Decimal(0)

    # Validated by shape rather than coerced and caught. `int()` raises a
    # bare `ValueError` on a non-numeric string, a `TypeError` on a
    # non-scalar and an `OverflowError` on an infinite float -- and where it
    # does *not* raise it is worse, because `int(1.9)` is 1 and `int('1_0')`
    # is 10, so corruption becomes a confident wrong number. Listing
    # exception types cannot close that set.
    #
    # So this asks what a member *is* instead: an unsigned integer of a
    # stated width. Anything else is refused, once, here.
    #
    # The width depends on the layout. Schwab sends the whole mantissa in
    # `lo`, as a string of digits -- measured on 5,843 objects from a
    # recorded ACCT_ACTIVITY archive, none of which carried `mid` or
    # `hi` -- and a fill principal above 4294.967295 at signScale 12 needs
    # more than 32 bits to do it. Reading `lo` as the first of a .NET
    # Decimal's three 32-bit members would refuse every such object: 129 in
    # that archive, 33 of them fill principals, each of which equals price
    # times quantity from the same message when `lo` is read whole. A lone
    # `lo` is bounded by the .NET mantissa itself, 96 bits.
    #
    # An object that carries `mid` or `hi` is read as the three-member
    # layout, each member 32 bits. That layout has never been captured; it
    # is kept because it is the only reading those names support. A `lo`
    # wider than 32 bits beside one is both layouts at once, so it is refused
    # rather than guessed. The choice is made on `mid` *and* `hi`: keyed on
    # `mid` alone, `{"lo": ..., "hi": 1}` would read `lo` whole and drop `hi`
    # without a word.
    #
    # An absent `lo` is zero here too, as an absent member is everywhere.
    # The shortcut above has already returned for that case, but it is
    # normalisation only, and the arithmetic must not depend on it: read
    # unconditionally, a mantissa-less object that reached this line would
    # raise instead of decoding as the zero it is.
    if fields.get('mid') is None and fields.get('hi') is None:
        lo = fields.get('lo')
        mantissa = 0 if lo is None else _unsigned(lo, 'lo', value, 96)
    else:
        mantissa = 0
        # Widest first, so a corrupt `mid` or `hi` is named as itself. In the
        # other order a ten-digit `lo` -- ordinary on its own -- is refused
        # first, and the message sends whoever reads it to the wrong field.
        for name, shift in (('hi', 64), ('mid', 32), ('lo', 0)):
            member = fields.get(name)
            if member is None:
                continue
            mantissa += _unsigned(member, name, value, 32) << shift

    # Built from a string, sign included, because every *operation* on a
    # Decimal applies the caller's context and only construction does not.
    # A consumer who sets `decimal.getcontext().prec = 6` somewhere else in
    # their process -- an ordinary thing to do when formatting money -- would
    # otherwise get 1234.57 for a price of 1234.5678, silently, from a
    # function whose whole purpose is that its result can be fed back into
    # `set_price`.
    #
    # The sign is part of that. `-result` is `Decimal.__neg__`, which is a
    # context-aware operation and rounds -- so negating after construction
    # would be exact for positive values and round negative ones, which is
    # the branch `EstimatedPrincipalAmount` arrives on.
    return decimal.Decimal('{}{}E-{}'.format(
            '-' if scale % 2 else '', mantissa, scale // 2))


#: Unicode categories skipped before a ``{`` in ``MESSAGE_DATA``: format and
#: control characters, and space, line and paragraph separators.
_SKIPPED_BEFORE_OBJECT = frozenset(('Cf', 'Cc', 'Zs', 'Zl', 'Zp'))


class UnparsableMessageData(SchwabError, ValueError):
    '''
    Raised by :func:`parse_message_data` when ``MESSAGE_DATA`` starts like a
    JSON object --- its first character other than whitespace, a format or
    control character, or a separator is ``{`` --- but is not one.

    A truncated or corrupt payload is not a notice, and returning it as text
    would let it read as one. The message does not repeat the payload, which
    carries account data.
    '''


def parse_message_data(value):
    '''
    Reads an ``ACCT_ACTIVITY`` content item's ``MESSAGE_DATA``.

    ``MESSAGE_DATA`` is a JSON string rather than an object, so it has to be
    parsed a second time, and Schwab also sends it empty, on the ``SUBSCRIBED``
    ack, and as plain prose, such as ``"Feature not supported"``. This
    returns:

    * the parsed ``dict``, when it is a JSON object;
    * ``None``, when it is empty or only whitespace;
    * the text itself, for anything else. That is a notice, and a notice never
      raises.

    The one notice observed, ``"Feature not supported"``, arrived in a content
    item whose ``MESSAGE_TYPE`` was empty, so a handler that dispatches on the
    type should expect notices here.

    A ``dict`` is returned as it is, so calling this on a value that has
    already been parsed is harmless.

    Format and control characters and separators in front of the ``{`` --- a
    byte order mark, a zero-width space --- are skipped, so a payload behind
    one is still read as a payload. Other characters are not skipped, even
    one that renders as nothing, such as a combining mark. After the payload
    only whitespace, as ``str.strip`` defines it, is dropped, so a payload
    followed by a zero-width space raises :class:`UnparsableMessageData`.

    :raises UnparsableMessageData: The text starts, after any whitespace,
                                   format or control characters and
                                   separators, with ``{`` but is not a JSON
                                   object.
    :raises TypeError: ``value`` is neither a ``str`` nor a ``dict``.
    '''
    # Types through type(), which a class cannot fake the way it can fake
    # __class__ for isinstance.
    if issubclass(type(value), dict):
        return value
    if not issubclass(type(value), str):
        raise TypeError(
                'MESSAGE_DATA must be a str or a dict, not a {}'.format(
                    _type_name(value)))

    # A plain str: a decoder's subclass could otherwise decide its own strip or
    # startswith.
    text = str.__str__(value)
    stripped = text.strip()
    if not stripped:
        return None
    # Format and control characters str.strip leaves in place -- a byte order
    # mark, a zero-width space, a NUL -- and any separator after one of them
    # must not turn a payload into a notice.
    start = 0
    skipped = _SKIPPED_BEFORE_OBJECT
    while (start < len(stripped)
           and unicodedata.category(stripped[start]) in skipped):
        start += 1
    if not stripped.startswith('{', start):
        return text

    try:
        parsed = json.loads(stripped[start:])
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError and UnicodeDecodeError; a payload
        # nested past the interpreter's depth raises RecursionError instead.
        parsed = None
    if not isinstance(parsed, dict):
        raise UnparsableMessageData(
                'MESSAGE_DATA starts like a JSON object but could not be read '
                'as one ({} characters)'.format(len(text)))
    return parsed
