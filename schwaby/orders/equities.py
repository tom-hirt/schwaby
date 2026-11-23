from enum import Enum

from schwaby.orders.common import Duration, Session, StopPriceLinkBasis


def _equity_order(instruction, order_type, symbol, quantity, *, price=None,
                  stop_price=None, stop_price_offset=None,
                  stop_price_link_type=None, stop_price_link_basis=None):
    '''
    Builds a single-leg equity order in the normal session, good for the day.

    This is an implementation detail of the templates below. It exists so the
    required fields for each order type are expressed in exactly one place.
    '''
    from schwaby.orders.common import (
            OrderStrategyType, OrderType, PriceLinkBasis, StopType)
    from schwaby.orders.generic import OrderBuilder

    builder = (OrderBuilder()
               .set_order_type(order_type)
               .set_session(Session.NORMAL)
               .set_duration(Duration.DAY)
               .set_order_strategy_type(OrderStrategyType.SINGLE)
               .add_equity_leg(instruction, symbol, quantity))

    if price is not None:
        builder.set_price(price)
        if order_type == OrderType.TRAILING_STOP_LIMIT:
            # A trailing stop-limit's fixed limit. In schwab-py PR #321's live
            # previews, a sell's fixed price without a priceLinkBasis was
            # refused ("Price link basis must be populated for trailing
            # ...") and accepted with priceLinkBasis MANUAL, no
            # priceLinkType and no priceOffset; stopType STANDARD was in both
            # requests. The other three instructions were not previewed.
            builder.set_stop_type(StopType.STANDARD)
            builder.set_price_link_basis(PriceLinkBasis.MANUAL)
    if stop_price is not None:
        builder.set_stop_price(stop_price)
    if stop_price_offset is not None:
        builder.set_stop_price_offset(stop_price_offset)
    if stop_price_link_type is not None:
        builder.set_stop_price_link_type(stop_price_link_type)
    if stop_price_link_basis is not None:
        builder.set_stop_price_link_basis(stop_price_link_basis)

    return builder


##########################################################################
# Buy orders


def equity_buy_market(symbol, quantity):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy market order.
    '''
    from schwaby.orders.common import Duration, EquityInstruction
    from schwaby.orders.common import OrderStrategyType, OrderType, Session
    from schwaby.orders.generic import OrderBuilder

    return (OrderBuilder()
            .set_order_type(OrderType.MARKET)
            .set_session(Session.NORMAL)
            .set_duration(Duration.DAY)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_equity_leg(EquityInstruction.BUY, symbol, quantity))


def equity_buy_limit(symbol, quantity, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy limit order.
    '''
    from schwaby.orders.common import Duration, EquityInstruction
    from schwaby.orders.common import OrderStrategyType, OrderType, Session
    from schwaby.orders.generic import OrderBuilder

    return (OrderBuilder()
            .set_order_type(OrderType.LIMIT)
            .set_price(price)
            .set_session(Session.NORMAL)
            .set_duration(Duration.DAY)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_equity_leg(EquityInstruction.BUY, symbol, quantity))


def equity_buy_stop(symbol, quantity, stop_price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy stop order. Places a market order once ``stop_price`` is reached.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.BUY, OrderType.STOP, symbol, quantity,
            stop_price=stop_price)


def equity_buy_stop_limit(symbol, quantity, stop_price, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy stop-limit order. Places a limit order at ``price`` once ``stop_price``
    is reached.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.BUY, OrderType.STOP_LIMIT, symbol, quantity,
            stop_price=stop_price, price=price)


def equity_buy_trailing_stop(
        symbol, quantity, stop_price_offset, stop_price_link_type,
        stop_price_link_basis=StopPriceLinkBasis.LAST):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy trailing stop order.

    :param stop_price_offset: Size of the trailing offset. Interpreted according
                              to ``stop_price_link_type``.
    :param stop_price_link_type: Whether ``stop_price_offset`` is a percentage,
                                 an absolute value, or a number of ticks. See
                                 :class:`~schwaby.orders.common.StopPriceLinkType`.
                                 This parameter is required because an offset of
                                 ``2.5`` means a 2.5% trail under ``PERCENT``
                                 and a $2.50 trail under ``VALUE``, and Schwab
                                 accepts both without complaint.
    :param stop_price_link_basis: Which price the offset is applied to. See
                                  :class:`~schwaby.orders.common.StopPriceLinkBasis`.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.BUY, OrderType.TRAILING_STOP, symbol, quantity,
            stop_price_offset=stop_price_offset,
            stop_price_link_type=stop_price_link_type,
            stop_price_link_basis=stop_price_link_basis)


def equity_buy_trailing_stop_limit(
        symbol, quantity, stop_price_offset, stop_price_link_type, price,
        stop_price_link_basis=StopPriceLinkBasis.LAST):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy trailing stop-limit order. Places a limit order at ``price`` once the
    trailing stop condition is met. The fixed limit is sent with
    ``priceLinkBasis`` ``MANUAL`` and ``stopType`` ``STANDARD``: a bare one
    was refused when previewed (see :ref:`the note on stop orders
    <stop_order_configuration>`).

    See :func:`equity_buy_trailing_stop` for a description of the trailing
    parameters.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.BUY, OrderType.TRAILING_STOP_LIMIT, symbol,
            quantity, stop_price_offset=stop_price_offset,
            stop_price_link_type=stop_price_link_type,
            stop_price_link_basis=stop_price_link_basis, price=price)


def equity_buy_market_on_close(symbol, quantity):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy market-on-close order.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.BUY, OrderType.MARKET_ON_CLOSE, symbol, quantity)


def equity_buy_limit_on_close(symbol, quantity, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy limit-on-close order.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.BUY, OrderType.LIMIT_ON_CLOSE, symbol, quantity,
            price=price)

##########################################################################
# Sell orders


def equity_sell_market(symbol, quantity):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    sell market order.
    '''
    from schwaby.orders.common import Duration, EquityInstruction
    from schwaby.orders.common import OrderStrategyType, OrderType, Session
    from schwaby.orders.generic import OrderBuilder

    return (OrderBuilder()
            .set_order_type(OrderType.MARKET)
            .set_session(Session.NORMAL)
            .set_duration(Duration.DAY)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_equity_leg(EquityInstruction.SELL, symbol, quantity))


def equity_sell_limit(symbol, quantity, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    sell limit order.
    '''
    from schwaby.orders.common import Duration, EquityInstruction
    from schwaby.orders.common import OrderStrategyType, OrderType, Session
    from schwaby.orders.generic import OrderBuilder

    return (OrderBuilder()
            .set_order_type(OrderType.LIMIT)
            .set_price(price)
            .set_session(Session.NORMAL)
            .set_duration(Duration.DAY)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_equity_leg(EquityInstruction.SELL, symbol, quantity))

def equity_sell_stop(symbol, quantity, stop_price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    sell stop order. Places a market order once ``stop_price`` is reached.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.SELL, OrderType.STOP, symbol, quantity,
            stop_price=stop_price)


def equity_sell_stop_limit(symbol, quantity, stop_price, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    sell stop-limit order. Places a limit order at ``price`` once ``stop_price``
    is reached.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.SELL, OrderType.STOP_LIMIT, symbol, quantity,
            stop_price=stop_price, price=price)


def equity_sell_trailing_stop(
        symbol, quantity, stop_price_offset, stop_price_link_type,
        stop_price_link_basis=StopPriceLinkBasis.LAST):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    sell trailing stop order.

    :param stop_price_offset: Size of the trailing offset. Interpreted according
                              to ``stop_price_link_type``.
    :param stop_price_link_type: Whether ``stop_price_offset`` is a percentage,
                                 an absolute value, or a number of ticks. See
                                 :class:`~schwaby.orders.common.StopPriceLinkType`.
                                 This parameter is required because an offset of
                                 ``2.5`` means a 2.5% trail under ``PERCENT``
                                 and a $2.50 trail under ``VALUE``, and Schwab
                                 accepts both without complaint.
    :param stop_price_link_basis: Which price the offset is applied to. See
                                  :class:`~schwaby.orders.common.StopPriceLinkBasis`.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.SELL, OrderType.TRAILING_STOP, symbol, quantity,
            stop_price_offset=stop_price_offset,
            stop_price_link_type=stop_price_link_type,
            stop_price_link_basis=stop_price_link_basis)


def equity_sell_trailing_stop_limit(
        symbol, quantity, stop_price_offset, stop_price_link_type, price,
        stop_price_link_basis=StopPriceLinkBasis.LAST):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    sell trailing stop-limit order. Places a limit order at ``price`` once the
    trailing stop condition is met. The fixed limit is sent with
    ``priceLinkBasis`` ``MANUAL`` and ``stopType`` ``STANDARD``: a bare one
    was refused when previewed (see :ref:`the note on stop orders
    <stop_order_configuration>`).

    See :func:`equity_sell_trailing_stop` for a description of the trailing
    parameters.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.SELL, OrderType.TRAILING_STOP_LIMIT, symbol,
            quantity, stop_price_offset=stop_price_offset,
            stop_price_link_type=stop_price_link_type,
            stop_price_link_basis=stop_price_link_basis, price=price)


def equity_sell_market_on_close(symbol, quantity):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    sell market-on-close order.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.SELL, OrderType.MARKET_ON_CLOSE, symbol, quantity)


def equity_sell_limit_on_close(symbol, quantity, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    sell limit-on-close order.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.SELL, OrderType.LIMIT_ON_CLOSE, symbol, quantity,
            price=price)

##########################################################################
# Short sell orders


def equity_sell_short_market(symbol, quantity):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    short sell market order.
    '''
    from schwaby.orders.common import Duration, EquityInstruction
    from schwaby.orders.common import OrderStrategyType, OrderType, Session
    from schwaby.orders.generic import OrderBuilder

    return (OrderBuilder()
            .set_order_type(OrderType.MARKET)
            .set_session(Session.NORMAL)
            .set_duration(Duration.DAY)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_equity_leg(EquityInstruction.SELL_SHORT, symbol, quantity))


def equity_sell_short_limit(symbol, quantity, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    short sell limit order.
    '''
    from schwaby.orders.common import Duration, EquityInstruction
    from schwaby.orders.common import OrderStrategyType, OrderType, Session
    from schwaby.orders.generic import OrderBuilder

    return (OrderBuilder()
            .set_order_type(OrderType.LIMIT)
            .set_price(price)
            .set_session(Session.NORMAL)
            .set_duration(Duration.DAY)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_equity_leg(EquityInstruction.SELL_SHORT, symbol, quantity))

def equity_sell_short_stop(symbol, quantity, stop_price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    short sell stop order. Places a market order once ``stop_price`` is reached.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.SELL_SHORT, OrderType.STOP, symbol, quantity,
            stop_price=stop_price)


def equity_sell_short_stop_limit(symbol, quantity, stop_price, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    short sell stop-limit order. Places a limit order at ``price`` once ``stop_price``
    is reached.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.SELL_SHORT, OrderType.STOP_LIMIT, symbol, quantity,
            stop_price=stop_price, price=price)


def equity_sell_short_trailing_stop(
        symbol, quantity, stop_price_offset, stop_price_link_type,
        stop_price_link_basis=StopPriceLinkBasis.LAST):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    short sell trailing stop order.

    :param stop_price_offset: Size of the trailing offset. Interpreted according
                              to ``stop_price_link_type``.
    :param stop_price_link_type: Whether ``stop_price_offset`` is a percentage,
                                 an absolute value, or a number of ticks. See
                                 :class:`~schwaby.orders.common.StopPriceLinkType`.
                                 This parameter is required because an offset of
                                 ``2.5`` means a 2.5% trail under ``PERCENT``
                                 and a $2.50 trail under ``VALUE``, and Schwab
                                 accepts both without complaint.
    :param stop_price_link_basis: Which price the offset is applied to. See
                                  :class:`~schwaby.orders.common.StopPriceLinkBasis`.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.SELL_SHORT, OrderType.TRAILING_STOP, symbol, quantity,
            stop_price_offset=stop_price_offset,
            stop_price_link_type=stop_price_link_type,
            stop_price_link_basis=stop_price_link_basis)


def equity_sell_short_trailing_stop_limit(
        symbol, quantity, stop_price_offset, stop_price_link_type, price,
        stop_price_link_basis=StopPriceLinkBasis.LAST):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    short sell trailing stop-limit order. Places a limit order at ``price`` once the
    trailing stop condition is met. The fixed limit is sent with
    ``priceLinkBasis`` ``MANUAL`` and ``stopType`` ``STANDARD``: a bare one
    was refused when previewed (see :ref:`the note on stop orders
    <stop_order_configuration>`).

    See :func:`equity_sell_short_trailing_stop` for a description of the trailing
    parameters.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.SELL_SHORT, OrderType.TRAILING_STOP_LIMIT, symbol,
            quantity, stop_price_offset=stop_price_offset,
            stop_price_link_type=stop_price_link_type,
            stop_price_link_basis=stop_price_link_basis, price=price)


def equity_sell_short_market_on_close(symbol, quantity):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    short sell market-on-close order.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.SELL_SHORT, OrderType.MARKET_ON_CLOSE, symbol, quantity)


def equity_sell_short_limit_on_close(symbol, quantity, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    short sell limit-on-close order.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.SELL_SHORT, OrderType.LIMIT_ON_CLOSE, symbol, quantity,
            price=price)

##########################################################################
# Buy to cover orders


def equity_buy_to_cover_market(symbol, quantity):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy-to-cover market order.
    '''
    from schwaby.orders.common import Duration, EquityInstruction
    from schwaby.orders.common import OrderStrategyType, OrderType, Session
    from schwaby.orders.generic import OrderBuilder

    return (OrderBuilder()
            .set_order_type(OrderType.MARKET)
            .set_session(Session.NORMAL)
            .set_duration(Duration.DAY)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_equity_leg(EquityInstruction.BUY_TO_COVER, symbol, quantity))


def equity_buy_to_cover_limit(symbol, quantity, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy-to-cover limit order.
    '''
    from schwaby.orders.common import Duration, EquityInstruction
    from schwaby.orders.common import OrderStrategyType, OrderType, Session
    from schwaby.orders.generic import OrderBuilder

    return (OrderBuilder()
            .set_order_type(OrderType.LIMIT)
            .set_price(price)
            .set_session(Session.NORMAL)
            .set_duration(Duration.DAY)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_equity_leg(EquityInstruction.BUY_TO_COVER, symbol, quantity))


def equity_buy_to_cover_stop(symbol, quantity, stop_price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy-to-cover stop order. Places a market order once ``stop_price`` is reached.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.BUY_TO_COVER, OrderType.STOP, symbol, quantity,
            stop_price=stop_price)


def equity_buy_to_cover_stop_limit(symbol, quantity, stop_price, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy-to-cover stop-limit order. Places a limit order at ``price`` once ``stop_price``
    is reached.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.BUY_TO_COVER, OrderType.STOP_LIMIT, symbol, quantity,
            stop_price=stop_price, price=price)


def equity_buy_to_cover_trailing_stop(
        symbol, quantity, stop_price_offset, stop_price_link_type,
        stop_price_link_basis=StopPriceLinkBasis.LAST):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy-to-cover trailing stop order.

    :param stop_price_offset: Size of the trailing offset. Interpreted according
                              to ``stop_price_link_type``.
    :param stop_price_link_type: Whether ``stop_price_offset`` is a percentage,
                                 an absolute value, or a number of ticks. See
                                 :class:`~schwaby.orders.common.StopPriceLinkType`.
                                 This parameter is required because an offset of
                                 ``2.5`` means a 2.5% trail under ``PERCENT``
                                 and a $2.50 trail under ``VALUE``, and Schwab
                                 accepts both without complaint.
    :param stop_price_link_basis: Which price the offset is applied to. See
                                  :class:`~schwaby.orders.common.StopPriceLinkBasis`.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.BUY_TO_COVER, OrderType.TRAILING_STOP, symbol, quantity,
            stop_price_offset=stop_price_offset,
            stop_price_link_type=stop_price_link_type,
            stop_price_link_basis=stop_price_link_basis)


def equity_buy_to_cover_trailing_stop_limit(
        symbol, quantity, stop_price_offset, stop_price_link_type, price,
        stop_price_link_basis=StopPriceLinkBasis.LAST):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy-to-cover trailing stop-limit order. Places a limit order at ``price`` once the
    trailing stop condition is met. The fixed limit is sent with
    ``priceLinkBasis`` ``MANUAL`` and ``stopType`` ``STANDARD``: a bare one
    was refused when previewed (see :ref:`the note on stop orders
    <stop_order_configuration>`).

    See :func:`equity_buy_to_cover_trailing_stop` for a description of the trailing
    parameters.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.BUY_TO_COVER, OrderType.TRAILING_STOP_LIMIT, symbol,
            quantity, stop_price_offset=stop_price_offset,
            stop_price_link_type=stop_price_link_type,
            stop_price_link_basis=stop_price_link_basis, price=price)


def equity_buy_to_cover_market_on_close(symbol, quantity):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy-to-cover market-on-close order.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.BUY_TO_COVER, OrderType.MARKET_ON_CLOSE, symbol, quantity)


def equity_buy_to_cover_limit_on_close(symbol, quantity, price):
    '''
    Returns a pre-filled :class:`~schwaby.orders.generic.OrderBuilder` for an equity
    buy-to-cover limit-on-close order.
    '''
    from schwaby.orders.common import EquityInstruction, OrderType

    return _equity_order(
            EquityInstruction.BUY_TO_COVER, OrderType.LIMIT_ON_CLOSE, symbol, quantity,
            price=price)
