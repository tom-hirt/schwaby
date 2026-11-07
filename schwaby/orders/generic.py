import decimal
import math

from enum import Enum

from schwaby.orders import common
from schwaby.utils import (EnumEnforcer, _DATETIME, _format_iso_instant,
                           _is_instance, _qualified_type_name)

import httpx2


def _cancel_time_string(dt):
    '''``dt`` as the string ``cancelTime`` is sent as.

    Refuses a value that is not an aware datetime with ``ValueError``. A
    datetime whose conversion to UTC fails -- ``datetime.max`` in a zone
    behind UTC overflows, for one -- raises what the conversion raises, as
    the client's own date parameters do.

    Only a datetime that names an instant is accepted. A naive datetime means a
    different instant on every machine that reads it, and a plain date would
    be sent as midnight UTC, which is not what "cancel on that day" means in
    any zone a trader is likely to be in. On an order both are worth refusing
    rather than warning about: the mistake is in something sent to the venue,
    not in something reported.

    Aware means ``utcoffset()`` returns an offset, which is Python's own
    definition. A ``tzinfo`` whose ``utcoffset`` returns ``None`` is naive
    despite being set, and the UTC conversion would read it as local time.

    The type is checked with ``_is_instance`` so a value whose ``__class__``
    raises gets this refusal rather than that exception, and against the
    ``datetime`` captured at import, for the reason given where it is captured.
    '''
    if not _is_instance(dt, _DATETIME):
        raise ValueError(
            "cancel time must be a datetime.datetime with a timezone, "
            "got '{}'".format(_qualified_type_name(dt)))

    if dt.utcoffset() is None:
        raise ValueError(
            "cancel time must carry a timezone, and this datetime has no UTC "
            "offset. "
            "An order's cancelTime names an instant, and a datetime without "
            "a timezone names a different one on each machine. Attach one "
            "explicitly, for example "
            "datetime.datetime(..., tzinfo=datetime.timezone.utc) or "
            "zoneinfo.ZoneInfo('America/New_York').")

    return _format_iso_instant(dt)


def _build_object(obj):
    # Literals are passed straight through
    if _is_instance(obj, (str, int, float)):
        return obj

    # A Decimal has no __dict__, so without a branch here it falls to the
    # vars() path below and raises `vars() argument must have __dict__
    # attribute` -- at build() time, naming neither the field nor the type.
    #
    # It is refused rather than rendered. Rendering every Decimal as a string
    # is wrong for the fields Schwab types as numbers: quantity,
    # stopPriceOffset and activationPrice would go out as "10" and "0.50", and
    # a non-finite one would go out as "sNaN" instead of failing. Only the
    # price fields take a Decimal, and they convert it where it is set.
    elif _is_instance(obj, decimal.Decimal):
        raise ValueError(
                'a decimal.Decimal reached the built order ({}). Only prices '
                'take one -- set_price, set_stop_price and their copy_ '
                'variants. Every other numeric field is a number in Schwab\'s '
                'schema, so pass an int or a float there.'.format(
                    format(obj, 'f')))

    # Note enums are not handled because call callers convert their enums to
    # values.

    # Dicts and lists are iterated over, with keys intact
    elif _is_instance(obj, dict):
        return dict((key, _build_object(value)) for key, value in obj.items())
    elif _is_instance(obj, list):
        return [_build_object(i) for i in obj]

    # Objects have their variables translated into keys
    else:
        ret = {}
        for name, value in vars(obj).items():
            if value is None or name[0] != '_':
                continue

            name = name[1:]
            ret[name] = _build_object(value)
        return ret


def _assert_finite(name, price, *, price_field=False):
    '''Rejects order fields which are not a finite number.

    ``price_field`` says which kind of field is being guarded, because the two
    take different types and only the caller knows which this is. A price field
    is a string or a ``decimal.Decimal`` in Schwab's schema; a numeric one --
    quantity, the offsets, the activation price -- is a number, and takes an
    ``int`` or a ``float`` and nothing else. Passing a string to a numeric
    field raises rather than building it straight into the order, where
    ``set_price_offset('abc')`` would become ``{"priceOffset": "abc"}``; so
    does every prebuilt template given a string quantity.

    NaN and the infinities do not name a price, a size or an offset, and they
    arrive by computation rather than by typing: a limit derived from a quote
    that was missing, a trailing stop offset divided by a size that turned out
    to be zero. Without this they serialize happily and leave as part of a real
    order --

        {"orderType": "LIMIT", "price": "NaN", "quantity": NaN, ...}

    -- which is the wrong place to discover the problem. Note the bare ``NaN``:
    ``json.dumps`` emits that by default and it is not valid JSON, so such an
    order is not merely wrong, it is untransmittable without a parser willing
    to accept a non-standard token.

    This has to run *before* the positivity checks rather than alongside them,
    because NaN compares False against everything: ``quantity <= 0`` and
    ``activation_price <= 0.0`` are both False for NaN, so those guards do not
    fire and the value passes straight through them.

    On a price field, strings are otherwise passed through untouched; only the
    spellings Python reads as non-finite are refused, because ``str()`` of a
    computed value is the obvious way to reach here.
    '''
    # Decimal first: float(Decimal('sNaN')) raises ValueError, which the string
    # branch below reads as "not a number at all" and lets through. A
    # signalling NaN would then reach the <= 0 comparisons in the callers and
    # raise a bare decimal.InvalidOperation whose message is a repr of its own
    # class -- the exact failure this function runs first to prevent.
    if _is_instance(price, decimal.Decimal):
        if price.is_nan() or price.is_infinite():
            raise ValueError(
                    '{} must be a finite number, got {!r}'.format(name, price))

        # Only the price fields take a Decimal, and they convert it before
        # reaching here, so anything still holding one is a numeric field --
        # quantity, the offsets, the activation price -- which Schwab types as
        # a number rather than a string. Refused here rather than at build(),
        # which knows the value but no longer knows which field it came from.
        raise ValueError(
                '{} is a number in Schwab\'s schema, not a price string, so '
                'it does not take a decimal.Decimal. Pass an int or a float. '
                'Got: {}'.format(name, format(price, 'f')))

    if _is_instance(price, str):
        # Same distinction the Decimal branch above draws, for the same
        # reason. A price field is a string in Schwab's schema and a numeric
        # field is not, and only the caller knows which one this is -- so it
        # says, rather than this guessing from the value.
        #
        # Without it the two offset setters would accept a string and build
        # it straight into the order: `set_stop_price_offset('abc')` would
        # produce `{"stopPriceOffset": "abc"}` and send it. They are the two
        # numeric setters with no comparison after this call to trip over the
        # type, so they would take it silently where `set_quantity` at least
        # raises something.
        if not price_field:
            # Deliberately not justified as "Schwab's schema types this a
            # number". It does -- and it types `price` and `stopPrice` as
            # `number($double)` too, which this library sends as strings
            # anyway because that is what the venue takes. The schema does not
            # separate the two groups and nothing here establishes that a
            # string quantity is refused by Schwab.
            #
            # The reason is internal consistency, which is checkable. Of the
            # four numeric setters, `set_quantity` and `set_activation_price`
            # cannot take a string anyway -- their `<= 0` comparison raises
            # TypeError on one. The two offsets have no range check, so
            # without this `set_price_offset('abc')` would build
            # `{"priceOffset": "abc"}`. Whether a setter takes a string should
            # not depend on whether it happens to have a range check, so all
            # four refuse one the same way here.
            raise ValueError(
                    '{} does not take a str; the numeric order fields take an '
                    'int or a float. Got: {!r}'.format(name, price))

        try:
            value = float(price)
        except ValueError:
            # Not a number at all. That is between the caller and Schwab.
            return
    else:
        # Asked as "is this a number", not as "can float() parse it" -- by
        # catching TypeError and returning -- which is the wrong question
        # twice over:
        #
        #  * things that are not numbers answer no, and returning would let
        #    them through. `set_quantity(None)` would reach `quantity <= 0`
        #    and raise a bare `TypeError: '<=' not supported between instances
        #    of 'NoneType' and 'int'` -- naming neither the field nor the
        #    value, which is the failure this function exists to prevent.
        #    Worse, the two offset setters have no comparison after this call,
        #    so they would *accept* it: `_build_object` drops a None, so
        #    `set_stop_price_offset(None)` would silently build an order with
        #    no offset on it and no complaint. The docstring names a trailing
        #    stop offset divided by a size that turned out to be zero, which
        #    gives NaN; a lookup that simply missed gives None just as readily.
        #
        #  * things that are not numbers answer *yes*. `float(b'1')` is 1.0,
        #    so a bytes would pass and be stored as bytes, and `json.dumps`
        #    refuses those -- an order that is not wrong so much as
        #    unsendable, discovered at the point of sending.
        #
        # `(int, float)` rather than `numbers.Real`, and the pair is copied
        # from `_build_object`'s own first line on purpose: the question this
        # answers is "can the order carry this", so the right predicate is the
        # serializer's, not a mathematical one. `numbers.Real` is wider --
        # `fractions.Fraction` and the numpy scalar types that do not subclass
        # `int` or `float` are all Real -- and every one of those falls through
        # to `vars()` in the builder and raises `vars() argument must have
        # __dict__ attribute`, naming neither the field nor the value. That is
        # the same ending as the bytes case above, reached by a different
        # route, and a quantity read off a dataframe column is the realistic
        # way to arrive at it.
        # `bool` first, because it is an `int` subclass and so satisfies the
        # test below -- and `_build_object` passes it through unchanged, so
        # `set_quantity(True)` would produce `{"quantity": true}`. A JSON
        # `true` is not a number in Schwab's schema, which is the same ending
        # as the bytes case: not wrong so much as unsendable. It arrives
        # realistically from a column pandas inferred as bool dtype, or a flag
        # threaded into the wrong argument.
        if _is_instance(price, bool) or not _is_instance(price, (int, float)):
            raise ValueError(
                    '{} must be a number, got {!r}'.format(name, price))

        try:
            value = float(price)
        except OverflowError:
            # An int too large for a double. `json.dumps` serializes one
            # happily, so this is not the unsendable case -- but `float()`
            # raises `OverflowError: int too large to convert to float`,
            # naming neither the field nor the value, which is the ending
            # every other branch here exists to replace. Absurd as a
            # quantity and cheap to name.
            # `bit_length()`, not a digit count. Since 3.11 `int.__str__`
            # itself refuses anything over `sys.get_int_max_str_digits()`, so
            # at 10**5000 a digit count such as `len(str(abs(price)))` would
            # raise `Exceeds the limit (4300 digits) for integer string
            # conversion` inside the message builder, chained off the
            # OverflowError and naming neither the field nor the value.
            raise ValueError(
                    '{} is too large to be a price or a size: an integer of '
                    '{} bits'.format(name, price.bit_length())) from None

    if math.isnan(value) or math.isinf(value):
        raise ValueError(
                '{} must be a finite number, got {!r}'.format(name, price))


def _render_decimal(name, value):
    '''Renders a ``Decimal`` to the string a price field holds, and passes
    anything else through untouched.

    Used by the ``copy_`` setters, which validate nothing by contract. This is
    serialization rather than validation: the field holds a string either way,
    and a ``Decimal`` cannot survive to ``build()`` without becoming one.
    '''
    if _is_instance(value, decimal.Decimal):
        # Refused even here, where nothing else is checked. The float escape
        # hatch is not equivalent: copy_price(float('nan')) yields a bare NaN,
        # which json.dumps emits as invalid JSON and a strict parser rejects,
        # so it cannot quietly become an order. Rendering a non-finite Decimal
        # produces the well-formed string "NaN", which transmits cleanly. That
        # is worse than an error, and it is not what skipping validation is
        # supposed to buy.
        if value.is_nan() or value.is_infinite():
            raise ValueError(
                    '{} must be a finite number, got {!r}'.format(name, value))
        return format(value, 'f')
    return value


def _require_price_string(name, price):
    '''
    Prices are strings, or ``decimal.Decimal``. See :ref:`price_strings`.

    A float is refused rather than converted. The conversion is lossy in the
    direction that costs money -- truncating the binary value sends a price a
    tick below the one asked for -- and even done correctly, how to round a
    price is a decision belonging to the caller, who knows what the order is
    for. There is no rounding this could pick which is right for everyone.

    ``Decimal`` is accepted because it is the type that avoids the problem
    rather than one that hides it: it carries the precision the caller chose,
    and rendering it here decides nothing.

    Depth is deliberately not policed. ``Decimal(0.1)`` carries a float's
    binary expansion and renders 57 characters, which is ugly, but Schwab types
    both price fields ``number($double)``, so it parses to the same double a
    short spelling would -- it is unreadable, not wrong. And no digit count
    separates it from honest arithmetic: measured over realistic inputs,
    float-derived values span 0 to 53 decimal places and string arithmetic
    spans 1 to 9, which overlap. A guard on depth therefore refuses valid
    computed limits at order-placement time to prevent a payload that is
    merely long. ``Decimal(str(value))`` is still the right habit, and the
    docs say so.
    '''
    if _is_instance(price, decimal.Decimal):
        # format() renders NaN and the infinities as 'sNaN'/'Infinity', and
        # _assert_finite reads a signalling NaN as "not a number at all" and
        # lets it past -- so without this, set_price(Decimal('sNaN')) reaches
        # an order.
        if price.is_nan() or price.is_infinite():
            raise ValueError(
                    '{} must be a finite number, got {!r}'.format(name, price))

        # format(d, 'f') rather than str(d): str(Decimal('1E+2')) is '1E+2',
        # which is not a price. Neither form loses anything.
        price = format(price, 'f')

    if not _is_instance(price, str):
        raise ValueError(
                '{} must be a string or a decimal.Decimal, got {!r}. How to '
                'round it is yours to decide -- note that '
                "'{{:.2f}}'.format(value) rounds to the nearest cent and does "
                'not truncate toward zero, so it may not round the way the '
                'order needs. '
                'copy_{} sets the field with no validation at all.'.format(
                    name, price, name.replace(' ', '_')))

    _assert_finite(name, price, price_field=True)
    return price


class OrderBuilder(EnumEnforcer):
    '''
    Helper class to create arbitrarily complex orders. Note this class simply
    implements the order schema defined in the `documentation
    <https://developer.schwab.com/products/trader-api--individual/details/
    specifications/Retail%20Trader%20API%20Production>`__, with no attempts to
    validate the result.
    Orders created using this class may be rejected or may never fill. Use at
    your own risk.
    '''

    def __init__(self, *, enforce_enums=True):
        super().__init__(enforce_enums)

        self._session = None
        self._duration = None
        self._cancelTime = None
        self._orderType = None
        self._complexOrderStrategyType = None
        self._quantity = None
        self._destinationLinkName = None
        self._stopPrice = None
        self._stopPriceLinkBasis = None
        self._stopPriceLinkType = None
        self._stopPriceOffset = None
        self._stopType = None
        self._priceLinkBasis = None
        self._priceLinkType = None
        self._priceOffset = None
        self._price = None
        self._orderLegCollection = None
        self._activationPrice = None
        self._specialInstruction = None
        self._orderStrategyType = None
        self._childOrderStrategies = None

    # Session
    def set_session(self, session):
        '''
        Set the order session. See :class:`~schwaby.orders.common.Session` for
        details.
        '''
        session = self.convert_enum(session, common.Session)
        self._session = session
        return self

    def clear_session(self):
        '''
        Clear the order session.
        '''
        self._session = None
        return self

    # Duration
    def set_duration(self, duration):
        '''
        Set the order duration. See :class:`~schwaby.orders.common.Duration` for
        details.
        '''
        duration = self.convert_enum(duration, common.Duration)
        self._duration = duration
        return self

    def clear_duration(self):
        '''
        Clear the order duration.
        '''
        self._duration = None
        return self

    # CancelTime
    def set_cancel_time(self, cancel_time):
        '''
        Set the order's ``cancelTime`` field. Takes a ``datetime.datetime``
        that carries a timezone; a naive datetime, a ``datetime.date`` or a
        string raises ``ValueError`` here, when it is set. It is sent in UTC,
        to the second.

        Schwab documents the field on its order schema. It has not been tested
        against a live order here, so how Schwab treats it, including whether
        it acts on it at all, is not established. See
        :ref:`the note on cancel times <cancel_time>`.
        '''
        self._cancelTime = _cancel_time_string(cancel_time)
        return self

    def clear_cancel_time(self):
        '''
        Clear the order cancel time.
        '''
        self._cancelTime = None
        return self

    # OrderType
    def set_order_type(self, order_type):
        '''
        Set the order type. See :class:`~schwaby.orders.common.OrderType` for
        details.
        '''
        order_type = self.convert_enum(order_type, common.OrderType)
        self._orderType = order_type
        return self

    def clear_order_type(self):
        '''
        Clear the order type.
        '''
        self._orderType = None
        return self

    # ComplexOrderStrategyType
    def set_complex_order_strategy_type(self, complex_order_strategy_type):
        '''
        Set the complex order strategy type. See
        :class:`~schwaby.orders.common.ComplexOrderStrategyType` for details.
        '''
        complex_order_strategy_type = self.convert_enum(
            complex_order_strategy_type, common.ComplexOrderStrategyType)
        self._complexOrderStrategyType = complex_order_strategy_type
        return self

    def clear_complex_order_strategy_type(self):
        '''
        Clear the complex order strategy type.
        '''
        self._complexOrderStrategyType = None
        return self

    # Quantity
    def set_quantity(self, quantity):
        '''
        Exact semantics unknown. See :ref:`undocumented_quantity` for a
        discussion.
        '''
        _assert_finite('quantity', quantity)
        if quantity <= 0:
            raise ValueError('quantity must be positive')
        self._quantity = quantity
        return self

    def clear_quantity(self):
        '''
        Clear the order-level quantity. Note this does not affect order legs.
        '''
        self._quantity = None
        return self

    # RequestedDestination
    def set_requested_destination(self, requested_destination):
        '''
        Ask for the order to be routed to a specific venue. See
        :class:`~schwaby.orders.common.Destination` for the values Schwab
        accepts. Omit it and Schwab routes the order itself, which is what
        almost everyone wants.
        '''
        requested_destination = self.convert_enum(
            requested_destination, common.Destination)
        self._requestedDestination = requested_destination
        return self

    def clear_requested_destination(self):
        '''
        Clear the requested destination, returning the order to Schwab's own
        routing.
        '''
        self._requestedDestination = None
        return self

    # DestinationLinkName
    def set_destination_link_name(self, destination_link_name):
        '''
        Set the destination link name, which Schwab's schema types as a free
        string rather than an enumeration.

        **This is probably not the method you want.** To route an order to a
        particular venue, use
        :meth:`~schwaby.orders.generic.OrderBuilder.set_requested_destination`.
        This field takes the string Schwab's schema says it takes. It is not
        the venue selector, and :class:`~schwaby.orders.common.Destination`
        does not apply to it --- those values belong to
        ``requestedDestination``.
        '''
        self._destinationLinkName = destination_link_name
        return self

    def clear_destination_link_name(self):
        '''
        Clear the destination link name
        '''
        self._destinationLinkName = None
        return self

    # StopPrice
    def set_stop_price(self, stop_price):
        '''
        Set the stop price, as a string or a ``decimal.Decimal``. See
        :ref:`price_strings`.
        '''
        self._stopPrice = _require_price_string('stop price', stop_price)
        return self

    def copy_stop_price(self, stop_price):
        '''
        Directly set the stop price, skipping the type check
        :func:`set_stop_price` applies. As with :func:`copy_price`, a
        non-finite ``decimal.Decimal`` is still refused.
        '''
        self._stopPrice = _render_decimal('stop price', stop_price)
        return self

    def clear_stop_price(self):
        '''
        Clear the stop price.
        '''
        self._stopPrice = None
        return self

    # StopPriceLinkBasis
    def set_stop_price_link_basis(self, stop_price_link_basis):
        '''
        Set the stop price link basis. See
        :class:`~schwaby.orders.common.StopPriceLinkBasis` for details.
        '''
        stop_price_link_basis = self.convert_enum(
            stop_price_link_basis, common.StopPriceLinkBasis)
        self._stopPriceLinkBasis = stop_price_link_basis
        return self

    def clear_stop_price_link_basis(self):
        '''
        Clear the stop price link basis.
        '''
        self._stopPriceLinkBasis = None
        return self

    # StopPriceLinkType
    def set_stop_price_link_type(self, stop_price_link_type):
        '''
        Set the stop price link type. See
        :class:`~schwaby.orders.common.StopPriceLinkType` for details.
        '''
        stop_price_link_type = self.convert_enum(
            stop_price_link_type, common.StopPriceLinkType)
        self._stopPriceLinkType = stop_price_link_type
        return self

    def clear_stop_price_link_type(self):
        '''
        Clear the stop price link type.
        '''
        self._stopPriceLinkType = None
        return self

    # StopPriceOffset
    def set_stop_price_offset(self, stop_price_offset):
        '''
        Set the stop price offset.
        '''
        _assert_finite('stop price offset', stop_price_offset)
        self._stopPriceOffset = stop_price_offset
        return self

    def clear_stop_price_offset(self):
        '''
        Clear the stop price offset.
        '''
        self._stopPriceOffset = None
        return self

    # StopType
    def set_stop_type(self, stop_type):
        '''
        Set the stop type. See
        :class:`~schwaby.orders.common.StopType` for more details.
        '''
        stop_type = self.convert_enum(stop_type, common.StopType)
        self._stopType = stop_type
        return self

    def clear_stop_type(self):
        '''
        Clear the stop type.
        '''
        self._stopType = None
        return self

    # PriceLinkBasis
    def set_price_link_basis(self, price_link_basis):
        '''
        Set the price link basis. See
        :class:`~schwaby.orders.common.PriceLinkBasis` for details.
        '''
        price_link_basis = self.convert_enum(
            price_link_basis, common.PriceLinkBasis)
        self._priceLinkBasis = price_link_basis
        return self

    def clear_price_link_basis(self):
        '''
        Clear the price link basis.
        '''
        self._priceLinkBasis = None
        return self

    # PriceLinkType
    def set_price_link_type(self, price_link_type):
        '''
        Set the price link type. See
        :class:`~schwaby.orders.common.PriceLinkType` for more details.
        '''
        price_link_type = self.convert_enum(
            price_link_type, common.PriceLinkType)
        self._priceLinkType = price_link_type
        return self

    def clear_price_link_type(self):
        '''
        Clear the price link basis.
        '''
        self._priceLinkType = None
        return self

    # PriceOffset
    def set_price_offset(self, price_offset):
        '''
        Set the price offset. Together with
        :meth:`set_price_link_basis` and :meth:`set_price_link_type`, this
        expresses a price relative to something else -- for instance the limit
        price of a child order in a ``TRIGGER`` strategy, set as an offset from
        the price its parent filled at.

        It is the price-linked counterpart of
        :meth:`set_stop_price_link_basis`, :meth:`set_stop_price_link_type`
        and :meth:`set_stop_price_offset`.
        '''
        _assert_finite('price offset', price_offset)
        self._priceOffset = price_offset
        return self

    def clear_price_offset(self):
        '''
        Clear the price offset.
        '''
        self._priceOffset = None
        return self

    # Price
    def set_price(self, price):
        '''
        Set the order price, as a string or a ``decimal.Decimal``. See
        :ref:`price_strings`.
        '''
        self._price = _require_price_string('price', price)
        return self

    def copy_price(self, price):
        '''
        Directly set the price, skipping the type check
        :func:`set_price` applies -- a float, an int or a string all pass
        through as given. It is there for reconstructing an order from a
        historical response, where the price Schwab reported is the price you
        mean and converting it would change the order. The prebuilt templates
        use :func:`set_price` and take the same string it does.

        One thing is still refused: a non-finite ``decimal.Decimal``. Rendering
        one produces the string ``"NaN"``, which is well-formed JSON and
        transmits. ``float('nan')`` is accepted here because it does not --
        ``json.dumps`` emits a bare ``NaN`` that a strict parser rejects, so it
        cannot quietly become an order.
        '''
        self._price = _render_decimal('price', price)
        return self

    def clear_price(self):
        '''
        Clear the order price
        '''
        self._price = None
        return self

    # ActivationPrice
    def set_activation_price(self, activation_price):
        '''
        Set the activation price.
        '''
        _assert_finite('activation price', activation_price)
        if activation_price <= 0.0:
            raise ValueError('activation price must be positive')
        self._activationPrice = activation_price
        return self

    def clear_activation_price(self):
        '''
        Clear the activation price.
        '''
        self._activationPrice = None
        return self

    # TaxLotMethod
    def set_tax_lot_method(self, tax_lot_method):
        '''
        Set the tax lot selection method for a closing order. See
        :class:`~schwaby.orders.common.TaxLotMethod` for details.
        '''
        tax_lot_method = self.convert_enum(
            tax_lot_method, common.TaxLotMethod)
        self._taxLotMethod = tax_lot_method
        return self

    def clear_tax_lot_method(self):
        '''
        Clear the tax lot selection method. The order then carries no
        ``taxLotMethod``, and what Schwab applies then has not been measured.
        '''
        self._taxLotMethod = None
        return self

    # SpecialInstruction
    def set_special_instruction(self, special_instruction):
        '''
        Set the special instruction. See
        :class:`~schwaby.orders.common.SpecialInstruction` for details.
        '''
        special_instruction = self.convert_enum(
            special_instruction, common.SpecialInstruction)
        self._specialInstruction = special_instruction
        return self

    def clear_special_instruction(self):
        '''
        Clear the special instruction.
        '''
        self._specialInstruction = None
        return self

    # OrderStrategyType
    def set_order_strategy_type(self, order_strategy_type):
        '''
        Set the order strategy type. See
        :class:`~schwaby.orders.common.OrderStrategyType` for more details.
        '''
        order_strategy_type = self.convert_enum(
            order_strategy_type, common.OrderStrategyType)
        self._orderStrategyType = order_strategy_type
        return self

    def clear_order_strategy_type(self):
        '''
        Clear the order strategy type.
        '''
        self._orderStrategyType = None
        return self

    # ChildOrderStrategies
    def add_child_order_strategy(self, child_order_strategy):
        '''
        Attach an order that Schwab handles relative to this one.

        What "relative" means is this order's strategy type, set with
        :meth:`set_order_strategy_type`: under ``TRIGGER`` this order's
        execution places the child, and under ``OCO`` the execution of one
        child cancels the others --- the wording of
        :class:`~schwaby.orders.common.OrderStrategyType`, which says nothing
        about partial fills, and neither is measured here.
        :func:`~schwaby.orders.common.first_triggers_second` and
        :func:`~schwaby.orders.common.one_cancels_other` build both halves,
        and are usually what you want.

        :param child_order_strategy: an :class:`OrderBuilder` or an order
                                     dict. The response from placing an order
                                     is refused, since it is not an order.
        '''
        if _is_instance(child_order_strategy, httpx2.Response):
            raise ValueError(
                    'Child order cannot be a response. See here for '
                    'details: https://github.com/tom-hirt/schwaby/blob/'
                    'main/docs/order-templates.rst')

        if (not _is_instance(child_order_strategy, OrderBuilder)
                and not _is_instance(child_order_strategy, dict)):
            raise ValueError('child order must be OrderBuilder or dict')

        if self._childOrderStrategies is None:
            self._childOrderStrategies = []

        self._childOrderStrategies.append(child_order_strategy)
        return self

    def clear_child_order_strategies(self):
        '''
        Remove every child order attached with
        :meth:`add_child_order_strategy`.
        '''
        self._childOrderStrategies = None
        return self

    # OrderLegCollection
    def __add_order_leg(self, instruction, instrument, quantity,
                        quantity_type=None):
        # instruction and quantity_type are assumed to have been verified

        _assert_finite('quantity', quantity)
        if quantity <= 0:
            raise ValueError('quantity must be positive')

        if self._orderLegCollection is None:
            self._orderLegCollection = []

        leg = {
            'instruction': instruction,
            'instrument': instrument,
            'quantity': quantity,
        }
        # Added only when asked for, so a leg built without one carries no
        # `quantityType` key at all.
        if quantity_type is not None:
            leg['quantityType'] = quantity_type
        self._orderLegCollection.append(leg)

        return self

    def add_equity_leg(self, instruction, symbol, quantity, *,
                       quantity_type=None):
        '''
        Add an equity order leg.

        :param instruction: Instruction for the leg. See
                            :class:`~schwaby.orders.common.EquityInstruction` for
                            valid options.
        :param symbol: Equity symbol
        :param quantity: Number of shares for the order. Required and positive
                         whatever ``quantity_type`` is, and always sent --- a
                         leg asking for 500 dollars asks for 500 shares too,
                         and which one Schwab fills is not established.
        :param quantity_type: Sent as the leg's ``quantityType``. See
                              :class:`~schwaby.orders.common.QuantityType`.
                              If unset, the leg carries no ``quantityType``.
        '''
        instruction = self.convert_enum(instruction, common.EquityInstruction)
        quantity_type = self.convert_enum(quantity_type, common.QuantityType)
        return self.__add_order_leg(
            instruction, common.EquityInstrument(symbol), quantity,
            quantity_type)

    def add_option_leg(self, instruction, symbol, quantity, *,
                       quantity_type=None):
        '''
        Add an option order leg.

        :param instruction: Instruction for the leg. See
                            :class:`~schwaby.orders.common.OptionInstruction` for
                            valid options.
        :param symbol: Option symbol
        :param quantity: Number of contracts for the order. Required and
                         positive whatever ``quantity_type`` is, and always
                         sent --- a leg asking for 500 dollars asks for 500
                         contracts too, and which one Schwab fills is not
                         established.
        :param quantity_type: Sent as the leg's ``quantityType``. See
                              :class:`~schwaby.orders.common.QuantityType`.
                              If unset, the leg carries no ``quantityType``.
        '''
        instruction = self.convert_enum(instruction, common.OptionInstruction)
        quantity_type = self.convert_enum(quantity_type, common.QuantityType)
        return self.__add_order_leg(
            instruction, common.OptionInstrument(symbol), quantity,
            quantity_type)

    def clear_order_legs(self):
        '''
        Clear all order legs.
        '''
        self._orderLegCollection = None
        return self

    # Build

    def build(self):
        '''
        The order as the dict Schwab's API takes, which is what
        :meth:`~schwaby.client.Client.place_order` sends when given a
        builder. A field that was never set, or was cleared, is absent rather
        than null, and child orders are built recursively.
        '''
        return _build_object(self)
