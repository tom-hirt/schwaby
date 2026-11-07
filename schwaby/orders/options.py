import decimal
import math
import datetime
import re

from schwaby.orders.generic import OrderBuilder
from schwaby.utils import _is_instance


# The symbol encodes the strike as eight digits of thousandths, so the largest
# it can carry is $99,999.999.
_MAX_ENCODABLE_STRIKE = decimal.Decimal('99999.999')


def _scale_strike(strike_price):
    '''Scales a strike to the thousandths the symbol encodes, exactly.

    In a local context because ``Decimal.__mul__`` rounds to
    ``decimal.getcontext().prec`` significant digits, which is process-global
    and something a money-handling application may well have set. At the
    default 28 any realistic strike survives, but at ``prec=5``
    ``Decimal('1234.5678') * 1000`` comes back as ``1.2346E+6`` and the symbol
    names a different contract. The point of this function is that the scaling
    is exact, so it should not depend on a setting made elsewhere.
    '''
    # A fresh Context, not localcontext() with prec set: localcontext copies
    # the ambient one, so Emax, Emin and the traps come along. Under
    # Context(prec=28, Emax=3, Emin=-3) a strike of '500' would raise
    # decimal.Overflow out of the constructor, and with Overflow untrapped it
    # would return Infinity, which is integral -- so the granularity check
    # would pass it and build() die on `cannot convert Infinity to integer`.
    with decimal.localcontext(decimal.Context(prec=28)):
        return decimal.Decimal(strike_price) * 1000


# An option symbol is read from fixed positions: a root of one to six
# characters padded with spaces to six, six digits of expiration, the contract
# type, and eight digits of strike in thousandths.
_SYMBOL_ROOT = re.compile(r'[^\s]{1,6} *')
_SYMBOL_DIGITS = re.compile(r'[0-9]+')


def _parse_expiration_date(expiration_date):
    date = None
    try:
        date = datetime.datetime.strptime(expiration_date, '%y%m%d')
        return datetime.date(year=date.year, month=date.month, day=date.day)
    except ValueError:
        pass

    raise ValueError(
        'expiration date must follow format ' +
        '[two digit year][Month with leading zero]' +
        '[Day with leading zero]')


class OptionSymbol:
    '''Construct an option symbol from its constituent parts.

    :param underlying_symbol: Symbol of the underlying. Not validated.
    :param expiration_date: Expiration date. Accepts ``datetime.date``,
                            ``datetime.datetime``, or strings with the
                            format ``[Two digit year][Two digit month][Two
                            digit day]``.
    :param contract_type: ``P`` or ``PUT`` for put and ``C`` or ``CALL`` for 
                          call.
    :param strike_price_as_string: Strike price represented as a decimal string.

    Note while each of the individual parts is validated by itself, the
    option symbol itself may not represent a traded option:

     * Some underlyings do not support options.
     * Not all dates have valid option expiration dates.
     * Not all strike prices are valid options strikes.

    You can use :meth:`~schwaby.client.Client.get_option_chain` to obtain real
    option symbols for an underlying, as well as extensive data in pricing,
    bid/ask spread, volume, etc.

    For those interested in the details, options symbols have the following 
    format: ``[Underlying left justified with spaces to 6 positions] [Two digit 
    year][Two digit month][Two digit day]['P' or 'C'][Strike price]``

    The format of the strike price is modified based on its amount:
     * If less than 1000, Strike Price is multiple by 1000 and pre-pended with
       two zeroes
     * If greater than 1000, it's prepended with one zero.

    Examples include:
     * ``QQQ   240420P00500000``: QQQ Apr 20, 2024 500 Put (note the two zeroes
       in front because strike is less than 1000)
     * ``SPXW  240420C05040000``: SPX Weekly Apr 20, 2024 5040 Call (note the
       one zero in front because strike is greater than 1000)

    '''

    def __init__(self, underlying_symbol, expiration_date, contract_type,
                 strike_price_as_string):
        self.underlying_symbol = underlying_symbol

        if contract_type in ('C', 'CALL'):
            self.contract_type = 'C'
        elif contract_type in ('P', 'PUT'):
            self.contract_type = 'P'
        else:
            raise ValueError(
                'Contract type must be one of \'C\', \'CALL\', \'P\' or \'PUT\'')

        if _is_instance(expiration_date, str):
            self.expiration_date = _parse_expiration_date(expiration_date)
        elif _is_instance(expiration_date, datetime.datetime):
            self.expiration_date = datetime.date(
                year=expiration_date.year,
                month=expiration_date.month,
                day=expiration_date.day)
        elif _is_instance(expiration_date, datetime.date):
            self.expiration_date = expiration_date
        else:
            raise ValueError(
                'expiration_date must be a string with format %y%m%d ' +
                '(e.g. 240614) or one of datetime.date or ' +
                'datetime.datetime')

        assert(isinstance(self.expiration_date, datetime.date))

        strike = None
        try:
            strike = float(strike_price_as_string)
        except (TypeError, ValueError):
            # TypeError as well as ValueError: a strike that is not a string
            # at all -- None from a chain lookup that missed, most often --
            # raises TypeError out of float() with a message about float()'s
            # arguments, naming neither the strike nor the symbol.
            pass
        # math.isfinite as well as the positivity check: float('nan') parses,
        # and `nan <= 0` is False, so a strike of 'nan' or 'inf' would
        # otherwise pass here and fail later inside build() -- as `cannot
        # convert NaN to integer`, naming neither the strike nor the symbol.
        if (strike is None or not _is_instance(strike_price_as_string, str)
                or not math.isfinite(strike) or strike <= 0):
            raise ValueError(
                'strike price must be a string representing a positive ' +
                'float')

        # Remove extraneous zeroes at the end
        strike_copy = strike_price_as_string
        while strike_copy[-1] == '0':
            strike_copy = strike_copy[:-1]
        if strike_copy[-1] == '.':
            strike_price_as_string = strike_copy[:-1]

        # The symbol encodes the strike in thousandths, so anything finer than
        # a tenth of a cent cannot be represented, and int() would drop it
        # silently: '2.0019' would become 00002001, a $2.001 contract rather
        # than the one asked for, and '0.0005' 00000000, a strike of zero. Both
        # name a different contract or none, and neither says so. Refused here
        # rather than in build(), where the strike is no longer in hand.
        # Read off the parsed value's own digits rather than the scaled
        # product. Multiplying consults the decimal context, and no fixed
        # precision is enough: a strike carrying more significant digits than
        # the context holds is rounded by the multiply, so the product comes
        # out integral and the check passes something it should have caught --
        # '1.0000000000000000000000000000004' encoded as $1.000 that way.
        # Decimal(str) and as_tuple() consult nothing, so this is exact for any
        # input.
        #
        # The zero-stripping above only rewrites the string when what is left
        # ends in '.', so '2.0010' arrives here with four declared places and
        # three real ones. Hence looking at whether the digits past the
        # thousandths place are actually zero, rather than at the exponent.
        as_decimal = decimal.Decimal(strike_price_as_string)
        _, digits, exponent = as_decimal.as_tuple()
        excess = -exponent - 3
        if excess > 0 and any(digits[max(0, len(digits) - excess):]):
            raise ValueError(
                'strike price {} is finer than a tenth of a cent, which an '
                'option symbol cannot represent. Round it to at most three '
                'decimal places.'.format(strike_price_as_string))

        # The symbol's strike field is eight digits of thousandths, so it tops
        # out just under $100,000. Beyond that the format silently widens:
        # '700000' produces a 22-character symbol with a nine-digit strike.
        # The realistic way to arrive here is a strike already expressed in
        # cents or thousandths -- '250000' meaning $250.00 -- which deserves
        # the same clear refusal as the too-fine case rather than a malformed
        # symbol.
        # Comparison, not multiplication: Decimal comparisons are exact and
        # consult no context either.
        if as_decimal > _MAX_ENCODABLE_STRIKE:
            raise ValueError(
                'strike price {} does not fit an option symbol, whose strike '
                'field is eight digits of thousandths and so stops just below '
                '$100,000. If this is already in cents or thousandths, pass '
                'it in dollars.'.format(strike_price_as_string))

        self.strike_price = strike_price_as_string

    @classmethod
    def parse_symbol(cls, symbol):
        '''
        Parse a string option symbol of the form ``[Underlying left justified
        with spaces to 6 positions][Two digit year][Two digit month][Two digit
        day]['P' or 'C'][Strike price in thousandths, eight digits]``, 21
        characters in all.

        :raises ValueError: The symbol is not exactly that layout, or its
                            expiration date or contract type is not valid.
        '''
        format_error_str = (
            'option symbol must have format ' +
            '[Underlying left justified with spaces to 6 positions][Expiration][P/C][Strike]')

        # Every field is read from a fixed position. A symbol of another
        # length, or with anything but spaces padding the root, shifts a digit
        # from one field into the next and parses as a different contract: one
        # space of padding turns a 2026 expiration into 2061, and a
        # seven-digit strike turns 125 into 12.5.
        layout_error_str = format_error_str + ', 21 characters in all'
        # The type through type(), which a class cannot fake the way it can
        # fake __class__ for isinstance; then a plain str, so that a subclass
        # cannot answer the length check for characters it does not have.
        if not issubclass(type(symbol), str):
            raise ValueError(layout_error_str)
        symbol = str.__str__(symbol)
        if len(symbol) != 21:
            raise ValueError(layout_error_str)
        if not _SYMBOL_ROOT.fullmatch(symbol[:6]):
            raise ValueError(layout_error_str)
        if not (_SYMBOL_DIGITS.fullmatch(symbol[6:12])
                and _SYMBOL_DIGITS.fullmatch(symbol[13:])):
            raise ValueError(layout_error_str)

        underlying = symbol[:6].rstrip()
        expiration_date = symbol[6:12]
        contract_type = symbol[12]
        if contract_type not in ('C', 'P'):
            raise ValueError(
                "option must have contract type 'C' or 'P', " +
                format_error_str)

        strike = str(int(symbol[13:]) / 1000.0)

        expiration_date = _parse_expiration_date(expiration_date)

        return OptionSymbol(underlying, expiration_date, contract_type, strike)

    def build(self):
        '''
        Returns the option symbol represented by this builder.
        '''
        return '{:<6}{}{}{:08d}'.format(
            self.underlying_symbol,
            self.expiration_date.strftime('%y%m%d'),
            self.contract_type,
            # Decimal, not float: scaling the binary value and truncating with
            # int() truncates the representation error along with the value.
            # 2.01 * 1000 is 2009.9999999999998, so int() gives 2009 and the
            # symbol names a strike a tenth of a cent low -- a different
            # contract, or none. 590 of the 100,000 cent-granular strikes
            # between $0.01 and $1000.00 come out wrong that way.
            # strike_price is validated as a string on the way in, so
            # Decimal() reads it exactly as written.
            int(_scale_strike(self.strike_price))
        )


def __base_builder():
    from schwaby.orders.common import Duration, Session

    return (OrderBuilder()
            .set_session(Session.NORMAL)
            .set_duration(Duration.DAY))


################################################################################
# Single options

# Buy to Open

def option_buy_to_open_market(symbol, quantity):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for a
    buy-to-open market order.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.MARKET)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(OptionInstruction.BUY_TO_OPEN, symbol, quantity))


def option_buy_to_open_limit(symbol, quantity, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for a
    buy-to-open limit order.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.LIMIT)
            .set_price(price)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(OptionInstruction.BUY_TO_OPEN, symbol, quantity)
            )


# Sell to Open

def option_sell_to_open_market(symbol, quantity):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for a
    sell-to-open market order.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.MARKET)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(OptionInstruction.SELL_TO_OPEN, symbol, quantity))


def option_sell_to_open_limit(symbol, quantity, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for a
    sell-to-open limit order.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.LIMIT)
            .set_price(price)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(OptionInstruction.SELL_TO_OPEN, symbol, quantity)
            )


# Buy to Close


def option_buy_to_close_market(symbol, quantity):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for a
    buy-to-close market order.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.MARKET)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(OptionInstruction.BUY_TO_CLOSE, symbol, quantity))


def option_buy_to_close_limit(symbol, quantity, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for a
    buy-to-close limit order.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.LIMIT)
            .set_price(price)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(OptionInstruction.BUY_TO_CLOSE, symbol, quantity)
            )


# Sell to Close


def option_sell_to_close_market(symbol, quantity):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for a
    sell-to-close market order.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.MARKET)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(OptionInstruction.SELL_TO_CLOSE, symbol, quantity))


def option_sell_to_close_limit(symbol, quantity, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for a
    sell-to-close limit order.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.LIMIT)
            .set_price(price)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(OptionInstruction.SELL_TO_CLOSE, symbol, quantity)
            )


################################################################################
# Verticals

# Bull Call

def bull_call_vertical_open(
        long_call_symbol, short_call_symbol, quantity, net_debit):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` that opens a
    bull call vertical position. See :ref:`vertical_spreads` for details.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType
    from schwaby.orders.common import ComplexOrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.NET_DEBIT)
            .set_complex_order_strategy_type(ComplexOrderStrategyType.VERTICAL)
            .set_price(net_debit)
            .set_quantity(quantity)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(
                OptionInstruction.BUY_TO_OPEN, long_call_symbol, quantity)
            .add_option_leg(
                OptionInstruction.SELL_TO_OPEN, short_call_symbol, quantity))


def bull_call_vertical_close(
        long_call_symbol, short_call_symbol, quantity, net_credit):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` that closes a
    bull call vertical position. See :ref:`vertical_spreads` for details.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType
    from schwaby.orders.common import ComplexOrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.NET_CREDIT)
            .set_complex_order_strategy_type(ComplexOrderStrategyType.VERTICAL)
            .set_price(net_credit)
            .set_quantity(quantity)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(
                OptionInstruction.SELL_TO_CLOSE, long_call_symbol, quantity)
            .add_option_leg(
                OptionInstruction.BUY_TO_CLOSE, short_call_symbol, quantity))


# Bear Call

def bear_call_vertical_open(
        short_call_symbol, long_call_symbol, quantity, net_credit):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` that opens a
    bear call vertical position. See :ref:`vertical_spreads` for details.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType
    from schwaby.orders.common import ComplexOrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.NET_CREDIT)
            .set_complex_order_strategy_type(ComplexOrderStrategyType.VERTICAL)
            .set_price(net_credit)
            .set_quantity(quantity)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(
                OptionInstruction.SELL_TO_OPEN, short_call_symbol, quantity)
            .add_option_leg(
                OptionInstruction.BUY_TO_OPEN, long_call_symbol, quantity))


def bear_call_vertical_close(
        short_call_symbol, long_call_symbol, quantity, net_debit):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` that closes a
    bear call vertical position. See :ref:`vertical_spreads` for details.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType
    from schwaby.orders.common import ComplexOrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.NET_DEBIT)
            .set_complex_order_strategy_type(ComplexOrderStrategyType.VERTICAL)
            .set_price(net_debit)
            .set_quantity(quantity)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(
                OptionInstruction.BUY_TO_CLOSE, short_call_symbol, quantity)
            .add_option_leg(
                OptionInstruction.SELL_TO_CLOSE, long_call_symbol, quantity))


# Bull Put

def bull_put_vertical_open(
        long_put_symbol, short_put_symbol, quantity, net_credit):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` that opens a
    bull put vertical position. See :ref:`vertical_spreads` for details.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType
    from schwaby.orders.common import ComplexOrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.NET_CREDIT)
            .set_complex_order_strategy_type(ComplexOrderStrategyType.VERTICAL)
            .set_price(net_credit)
            .set_quantity(quantity)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(
                OptionInstruction.BUY_TO_OPEN, long_put_symbol, quantity)
            .add_option_leg(
                OptionInstruction.SELL_TO_OPEN, short_put_symbol, quantity))


def bull_put_vertical_close(
        long_put_symbol, short_put_symbol, quantity, net_debit):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` that closes a
    bull put vertical position. See :ref:`vertical_spreads` for details.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType
    from schwaby.orders.common import ComplexOrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.NET_DEBIT)
            .set_complex_order_strategy_type(ComplexOrderStrategyType.VERTICAL)
            .set_price(net_debit)
            .set_quantity(quantity)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(
                OptionInstruction.SELL_TO_CLOSE, long_put_symbol, quantity)
            .add_option_leg(
                OptionInstruction.BUY_TO_CLOSE, short_put_symbol, quantity))


# Bear Put

def bear_put_vertical_open(
        short_put_symbol, long_put_symbol, quantity, net_debit):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` that opens a
    bear put vertical position. See :ref:`vertical_spreads` for details.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType
    from schwaby.orders.common import ComplexOrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.NET_DEBIT)
            .set_complex_order_strategy_type(ComplexOrderStrategyType.VERTICAL)
            .set_price(net_debit)
            .set_quantity(quantity)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(
                OptionInstruction.SELL_TO_OPEN, short_put_symbol, quantity)
            .add_option_leg(
                OptionInstruction.BUY_TO_OPEN, long_put_symbol, quantity))


def bear_put_vertical_close(
        short_put_symbol, long_put_symbol, quantity, net_credit):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` that closes a
    bear put vertical position. See :ref:`vertical_spreads` for details.
    '''
    from schwaby.orders.common import OptionInstruction, OrderType, OrderStrategyType
    from schwaby.orders.common import ComplexOrderStrategyType

    return (__base_builder()
            .set_order_type(OrderType.NET_CREDIT)
            .set_complex_order_strategy_type(ComplexOrderStrategyType.VERTICAL)
            .set_price(net_credit)
            .set_quantity(quantity)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(
                OptionInstruction.BUY_TO_CLOSE, short_put_symbol, quantity)
            .add_option_leg(
                OptionInstruction.SELL_TO_CLOSE, long_put_symbol, quantity))
