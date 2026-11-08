.. py:module:: schwaby.orders.generic


.. _order_builder:

==========================
``OrderBuilder`` Reference
==========================

The :meth:`Client.place_order() <schwaby.client.Client.place_order>` method
expects a rather complex JSON object that describes the desired order. Schwab
provides some `example order specs
<https://developer.schwab.com/products/trader-api--individual/details/documentation/Retail%20Trader%20API%20Production>`__
to
illustrate the process and provides a schema in the `place order documentation
<https://developer.schwab.com/products/trader-api--individual/details/specifications/Retail%20Trader%20API%20Production>`__,
but beyond that we're on our own.  ``schwaby`` aims to be useful to everyone,
from users who want to easily place common equities and options trades, to
advanced users who want to place complex multi-leg, multi-asset type trades.

For users interested in simple trades, ``schwaby`` supports pre-built
:ref:`order_templates` that allow fast construction of many common trades.
Advanced users can modify these trades however they like, and can even build
trades from scratch.

This page describes the features of the complete order schema in all their
complexity. It is aimed at advanced users who want to create complex orders.
Less advanced users can use the :ref:`order templates <order_templates>` to
create orders. If they find themselves wanting to go beyond those templates,
they can return to this page to learn how.


------------------------------------------
Optional: Order Specification Introduction
------------------------------------------

Before we dive in to creating order specs, let's briefly introduce their
structure. This section is optional, although users wanting to use more advanced
features like stop prices and complex options orders will likely want to read it.

Here is an example of a spec that places a limit order to buy one share of
``MSFT`` for no more than $190.90. It is exactly what
``equity_buy_limit('MSFT', 1, '190.90')`` returns:

.. code-block:: JSON

  {
      "session": "NORMAL",
      "duration": "DAY",
      "orderType": "LIMIT",
      "price": "190.90",
      "orderLegCollection": [
          {
              "instruction": "BUY",
              "instrument": {
                  "assetType": "EQUITY",
                  "symbol": "MSFT"
              },
              "quantity": 1
          }
      ],
      "orderStrategyType": "SINGLE"
  }

Some key points are:

 * The ``LIMIT`` order type notifies Schwab that you'd like to place a limit
   order.
 * The order strategy type is ``SINGLE``, meaning this order is not a composite
   order.
 * The order leg collection contains a single leg to purchase the equity.
 * The price is specified *outside* the order leg. This may seem
   counterintuitive, but it's important when placing composite options orders.

If this seems like a lot of detail to specify a rather simple order, it is. The
thing about the order spec object is that it can express *every* order that can
be made through the Schwab API. For an advanced example, here is an order
spec for a standing order to enter a long position in ``GOOG`` at $1310 or less
that triggers a one-cancels-other order that exits the position if the price
rises to $1400 or falls below $1250:

.. code-block:: JSON

  {
      "session": "NORMAL",
      "duration": "GOOD_TILL_CANCEL",
      "orderType": "LIMIT",
      "price": "1310.00",
      "orderLegCollection": [
          {
              "instruction": "BUY",
              "instrument": {
                  "assetType": "EQUITY",
                  "symbol": "GOOG"
              },
              "quantity": 1
          }
      ],
      "orderStrategyType": "TRIGGER",
      "childOrderStrategies": [
          {
              "orderStrategyType": "OCO",
              "childOrderStrategies": [
                  {
                      "session": "NORMAL",
                      "duration": "GOOD_TILL_CANCEL",
                      "orderType": "LIMIT",
                      "price": "1400.00",
                      "orderLegCollection": [
                          {
                              "instruction": "SELL",
                              "instrument": {
                                  "assetType": "EQUITY",
                                  "symbol": "GOOG"
                              },
                              "quantity": 1
                          }
                      ]
                  },
                  {
                      "session": "NORMAL",
                      "duration": "GOOD_TILL_CANCEL",
                      "orderType": "STOP_LIMIT",
                      "stopPrice": "1250.00",
                      "orderLegCollection": [
                          {
                              "instruction": "SELL",
                              "instrument": {
                                  "assetType": "EQUITY",
                                  "symbol": "GOOG"
                              },
                              "quantity": 1
                          }
                      ]
                  }
              ]
          }
      ]
  }

While this looks complex, it can be broken down into the same components as the
simpler buy order:

 * This time, the ``LIMIT`` order type applies to the top-level order.
 * The order strategy type is ``TRIGGER``, which tells Schwab to hold off
   placing the second order until the first one completes.
 * The order leg collection still contains a single leg, and the price is still
   defined outside the order leg. This is typical for equities orders.

There are also a few things that aren't there in the simple buy order:

 * The ``childOrderStrategies`` contains the ``OCO`` order that is triggered
   when the first ``LIMIT`` order is executed.
 * If you look carefully, you'll notice that the inner ``OCO`` is a
   fully-featured suborder in itself.

This order is large and complex, and it takes a lot of reading to understand
what's going on here. Fortunately for you, you don't have to; ``schwaby`` cuts
down on this complexity by providing templates and helpers to make building
orders easy:

.. code-block:: python

  from schwaby.orders.common import (
      OrderType, first_triggers_second, one_cancels_other)
  from schwaby.orders.equities import equity_buy_limit, equity_sell_limit

  first_triggers_second(
      equity_buy_limit('GOOG', 1, '1310.00'),
      one_cancels_other(
          equity_sell_limit('GOOG', 1, '1400.00'),
          equity_sell_limit('GOOG', 1, '1240.00')
              .set_order_type(OrderType.STOP_LIMIT)
              .clear_price()
              .set_stop_price('1250.00')
      ))

You can find the full listing of order templates and utility functions
:ref:`here <order_templates>`.

Now that you have some background on how orders are structured, let's dive into
the order builder itself.


--------------------------
``OrderBuilder`` Reference
--------------------------

This section provides a detailed reference of the generic order builder. You can
use it to help build your own custom orders, or you can modify the pre-built
orders generated by ``schwaby``'s order templates.

Unfortunately, this reference is largely reverse-engineered. It is based on
the schema provided in the `official API documents
<https://developer.schwab.com/products/trader-api--individual/details/specifications/Retail%20Trader%20API%20Production>`__,
but many of the finer points, such as which fields should be populated for which
order types, etc. are best guesses.  If you find something is inaccurate or
missing, please `let us know <https://github.com/tom-hirt/schwaby/issues>`__.

That being said, experienced traders who understand how various order types and
complex strategies work should find this builder easy to use, at least for the
order types with which they are familiar. Here are some resources you can use to
learn more, courtesy of the Securites and Exchange Commission:

 * `Trading Basics: Understanding the Different Ways to Buy and Sell Stock
   <https://www.sec.gov/investor/alerts/trading101basics.pdf>`__
 * `Trade Execution: What Every Investor Should Know
   <https://www.sec.gov/reportspubs/investor-publications/investorpubstradexechtm.html>`__
 * `Investor Bulletin: An Introduction to Options
   <https://www.sec.gov/oiea/investor-alerts-bulletins/ib_introductionoptions.html>`__

.. autoclass:: schwaby.orders.generic.OrderBuilder
.. automethod:: schwaby.orders.generic.OrderBuilder.build


+++++++++++
Order Types
+++++++++++

Here are the order types that can be used:

.. autoclass:: schwaby.orders.common::OrderType
  :members:
  :undoc-members:
.. automethod:: schwaby.orders.generic.OrderBuilder.set_order_type
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_order_type


++++++++++++++++++++
Session and Duration
++++++++++++++++++++

Together, these fields control when the order will be placed and how long it
will remain active. Note ``schwaby``'s :ref:`templates <order_templates>`
place orders that are active for the duration of the current normal trading
session.  If you want to modify the default session and duration, you can use
these methods to do so.

.. warning::

  **The** ``Duration`` **enum lists more values than Schwab accepts, and
  nothing here will tell you which.** Probed against a live account, an equity
  order takes ``DAY``, ``GOOD_TILL_CANCEL`` and ``FILL_OR_KILL``.
  ``IMMEDIATE_OR_CANCEL``, ``END_OF_WEEK``, ``END_OF_MONTH`` and
  ``NEXT_END_OF_MONTH`` come back ``HTTP 400``, ``Invalid value
  'IMMEDIATE_OR_CANCEL'``.

  The enum mirrors Schwab's schema rather than what the equity endpoint takes,
  and **the rejection happens when the order is placed, not when it is built**
  --- so :class:`~schwaby.orders.generic.OrderBuilder` builds an
  ``IMMEDIATE_OR_CANCEL`` order without complaint and it fails on the live
  path, at the moment you least want to find out.

  A time-in-force table built from this enum will be wrong. Probe a value against your own account before relying on it, and note these
  results are equities only --- they may well be valid for options, which has
  not been tested.

.. note::

  **What a trading interface shows as one "time in force" dropdown is two
  fields here.** ``duration`` alone does not reproduce it --- the extended
  hours part lives in ``session``:

  ===================================  ==========================  ============
  what the interface calls it          ``duration``                ``session``
  ===================================  ==========================  ============
  Day                                  ``DAY``                     ``NORMAL``
  Day + extended hours                 ``DAY``                     ``SEAMLESS``
  Pre-market only                      ``DAY``                     ``AM``
  Post-market only                     ``DAY``                     ``PM``
  GTC + extended hours                 ``GOOD_TILL_CANCEL``        ``SEAMLESS``
  ===================================  ==========================  ============

  Set only ``duration`` and you get a different order from the one the
  interface would have placed under the same label. Venue-observed in live
  trading.

.. autoclass:: schwaby.orders.common::Session
  :members:
  :undoc-members:
.. autoclass:: schwaby.orders.common::Duration
  :members:
  :undoc-members:

.. automethod:: schwaby.orders.generic.OrderBuilder.set_duration
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_duration
.. automethod:: schwaby.orders.generic.OrderBuilder.set_session
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_session

.. _cancel_time:

An order's ``cancelTime`` is set separately from its duration:

.. automethod:: schwaby.orders.generic.OrderBuilder.set_cancel_time
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_cancel_time

.. note::

  **A cancel time must carry a timezone.** A naive ``datetime``, a
  ``datetime.date`` and a string are all refused when the cancel time is set,
  rather than sent. A cancel time without a timezone names a different instant
  on every machine that reads it, and a date alone would go out as midnight
  UTC, which is the previous evening in New York.

  The value is sent in UTC, to the second, the same form the client's other
  ISO date parameters use. Schwab's schema types ``cancelTime`` only as a
  date-time string, without saying which form it expects.

  Schwab documents the field, but it has not been tested against a live order
  here. Whether Schwab acts on it at all, for which durations, and at what
  granularity, is not established. Reading an order's
  ``cancelTime`` back after placing it shows what Schwab stored, not that it
  will act on it.


+++++
Price
+++++

Price is the amount you'd like to pay for each unit of the position you're
taking:

 * For equities and simple options limit orders, this is the price which you'd
   like to pay/receive.
 * For complex options limit orders (net debit/net credit), this is the total
   credit or debit you'd like to receive.

In other words, the price is the sum of the prices of the :ref:`order_legs`.
This is particularly powerful for complex multi-leg options orders, which
support complex stop and/or limit orders that trigger when the price of a
position reaches certain levels. In those cases, the price of an order can drop
below the specified price as a result of movements in multiple legs of the
trade.

.. _price_strings:

~~~~~~~~~~~~~~~~
Note on Prices
~~~~~~~~~~~~~~~~

The Schwab API expects prices as strings, and so does ``schwaby``. Whatever
you pass is what Schwab is asked for:

.. code-block:: python

   order.set_price('199.99')
   order.set_price('0.1869')

``decimal.Decimal`` is also accepted, and is the better choice if you are
computing prices rather than writing them down -- it carries the precision you
chose, and rendering it here decides nothing:

.. code-block:: python

   order.set_price(decimal.Decimal('199.99'))

**There are two rules here, not one, and they are close to opposites.** A
price field takes a string or a ``decimal.Decimal``. A numeric field takes an
``int`` or a ``float``. Each refuses what the other requires:

.. _accepted_types:

.. table:: What each setter accepts
   :widths: auto

   ========================= ======= ======= ========= =========== ======== ========
   setter                    ``str`` ``int`` ``float`` ``Decimal`` ``bool`` ``None``
   ========================= ======= ======= ========= =========== ======== ========
   ``set_price``             yes     no      no        yes         no       no
   ``set_stop_price``        yes     no      no        yes         no       no
   ``set_activation_price``  no      yes     yes       no          no       no
   ``set_quantity``          no      yes     yes       no          no       no
   ``set_price_offset``      no      yes     yes       no          no       no
   ``set_stop_price_offset`` no      yes     yes       no          no       no
   ``set_cancel_time``       no      no      no        no          no       no
   ========================= ======= ======= ========= =========== ======== ========

That table is checked against the validators by the test suite, so it cannot
quietly stop being true.

``set_cancel_time`` is the row that takes neither kind: it accepts only a
timezone-aware ``datetime``, and refuses everything above. See
:ref:`setting a cancel time <cancel_time>`.

.. warning::

  **A normalizer in front of these setters will defeat the** ``bool``
  **rejection.** ``bool`` is refused because ``True`` is an ``int`` subclass
  and would otherwise build ``{"quantity": true}`` --- a value Schwab's schema does not
  call a number, accepted in silence.

  It is easy to re-open by accident while trying to be *compatible* with that
  rule. A helper that turns a ``numpy.int64`` quantity into a plain ``int``,
  since those are refused too, may well test ``float(x).is_integer()``
  --- and ``float(True)`` is ``1.0``, so it turns ``True`` into ``1`` and hands
  it over laundered, rebuilding the one-share order this rejection exists to
  prevent, in code written to accommodate the rejection. Nothing on either
  side can see it: ``1`` is a perfectly good quantity by the time it arrives.

  If you normalise numeric input before it reaches a setter, exclude ``bool``
  first.

Everything a setter refuses raises immediately and names the field, rather than
being serialized as the wrong JSON type or dropped.

**The split is not Schwab's schema.** Schwab types ``price`` and ``stopPrice``
as ``number($double)`` as well, and this library sends those as strings anyway
because that is what the venue accepts --- so the schema does not separate the
two groups and nothing here establishes that it would refuse the other shape.

The two halves have two different reasons. A price takes a string or a
``Decimal`` because a float cannot carry one exactly, which is what
:ref:`price_strings` is about. A numeric field takes an ``int`` or a ``float``
so that all four numeric setters apply one rule; there is no venue reason for
any of them to accept a string.

Worth stating plainly because the reasons are checkable and the schema is not:
a later reader reconciling these setters "to match Schwab's schema" would be
working from a premise this page has just contradicted. And the practical
consequence stands either way --- passing ``'1.50'`` to
``set_activation_price`` and ``7.35`` to ``set_price`` are both natural things
to write, and both are refused.

That includes the quantity argument of every prebuilt template, which builds
its order leg through the same check:

.. code-block:: python

   equity_buy_market('AAPL', '10')     # raises: quantity does not take a str

The price arguments are unaffected --- those are price fields, and they still
take a string or a ``Decimal``.

**Build it from a string, not from a float.** ``decimal.Decimal(0.1)`` is not
``0.1``; it is that float's binary expansion, ``0.1000000000000000055511...``,
to 55 decimal places, and since the point of accepting ``Decimal`` is to render
it exactly, all 57 characters reach Schwab. That is unreadable rather than
wrong -- Schwab types both price fields ``number($double)``, so it parses to
the same double a short spelling would -- but ``decimal.Decimal(str(value))``
is the habit to keep, particularly when the value came from parsed JSON where a
quote field is already a float.

``schwaby`` does not refuse a price for being deep. A depth limit cannot
work: no digit count separates a float's expansion from honest arithmetic. Over
realistic inputs, float-derived values span 0 to 53 decimal places while string
arithmetic spans 1 to 9, so any threshold either misses contamination or
refuses a valid computed limit at order-placement time. Rounding a computed
price is your decision -- ``value.quantize(decimal.Decimal('0.01'))`` -- and
this library does not make it for you.

**Passing a number raises** ``ValueError`` -- integers included, so
``set_price(1250)`` raises exactly as ``set_price(1250.0)`` does. Nothing here
converts a number to a price string for you, because whether a limit price
should round up, down, or to the nearest tick is a trading decision and not a
formatting one. Making it yourself is how you keep it.

.. danger::

   If you are formatting a computed price, note that ``'{:.2f}'.format(value)``
   **rounds**. Rounding a buy limit up gives you a price one tick higher than
   the one you meant.

   Those are the two places that matter, and they are not arbitrary: under
   Reg NMS Rule 612 a US equity or ETF quoting at or above $1.00 is priced in
   pennies, and below $1.00 in hundredths of a cent. So two decimals above a
   dollar and four below it is the venue's grid, not a convention.

   To truncate toward zero instead, at two decimal places or four below one:

   .. code-block:: python

      import decimal

      def truncate(value):
          places = (decimal.Decimal('0.0001')
                    if abs(value) < 1 and value != 0.0
                    else decimal.Decimal('0.01'))
          return str(decimal.Decimal(str(value)).quantize(
              places, rounding=decimal.ROUND_DOWN))

   The difference is not cosmetic: ``19.9999999`` truncates to ``19.99`` and
   rounds to ``20.00``; ``0.186992`` truncates to ``0.1869`` where ``'{:.2f}'``
   gives ``0.19``.

:meth:`~schwaby.orders.generic.OrderBuilder.copy_price` still sets the field
without the type check, so a float or an int passes through as given. The one
thing it refuses is a non-finite ``decimal.Decimal``, which would render as the
transmittable string ``"NaN"``. It is there for rebuilding an order from a
historical response, where the price Schwab reported is the price you mean and
converting it would change the order. The prebuilt templates on the
:ref:`order_templates` page are not an exception to any of this -- they call
:meth:`~schwaby.orders.generic.OrderBuilder.set_price` and take the same string
it does.

.. automethod:: schwaby.orders.generic.OrderBuilder.set_price
.. automethod:: schwaby.orders.generic.OrderBuilder.copy_price
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_price


.. _order_legs:

++++++++++
Order Legs
++++++++++

Order legs are where the actual assets being bought or sold are specified. For
simple equity or single-options orders, there is just one leg. However, for
complex multi-leg options trades, there can be more than one leg.

Note that order legs often do not execute all at once. Order legs can be
executed over the specified :class:`~schwaby.orders.common.Duration` of the order.
What's more, if order legs request a large number of shares, legs themselves can
be partially filled. You can control this setting using the
:class:`~schwaby.orders.common.SpecialInstruction` value ``ALL_OR_NONE``.

With all that out of the way, order legs are relatively simple to specify.
``schwaby`` currently supports equity and option order legs:

.. automethod:: schwaby.orders.generic.OrderBuilder.add_equity_leg
.. autoclass:: schwaby.orders.common::EquityInstruction
  :members:
  :undoc-members:

.. automethod:: schwaby.orders.generic.OrderBuilder.add_option_leg
.. autoclass:: schwaby.orders.common::OptionInstruction
  :members:
  :undoc-members:

.. automethod:: schwaby.orders.generic.OrderBuilder.clear_order_legs

Either kind of leg takes a ``quantity_type``, sent as the leg's
``quantityType``:

.. autoclass:: schwaby.orders.common::QuantityType
  :members:
  :undoc-members:

.. danger::

  **If Schwab ignores this field, the leg is sized in shares.** A leg always
  carries a positive ``quantity``, whatever its ``quantity_type``, because
  the builder requires one and sends both. So a leg asking for 500 dollars of
  a stock is also a leg asking for 500 shares of it, and which one Schwab
  fills has not been established here. At a share price of 200 those differ
  by a factor of 200, and the order comes back filled either way.

  Schwab's order schema lists ``quantityType`` on each
  ``orderLegCollection`` entry as ``ALL_SHARES``, ``DOLLARS`` or ``SHARES``,
  with no description of any of them, and the field appears in none of its
  order examples. Which asset types and instructions accept each is not
  established either. ``ALL_SHARES`` has the same shape: the number you had
  to pass is a real share count if the field is dropped.

  Until you have placed one and seen what came back, place it small enough
  that both readings are acceptable. A leg built without a ``quantity_type``
  carries no ``quantityType`` at all.



+++++++++++++++++++++
Requested Destination
+++++++++++++++++++++

By default, Schwab routes an order to whichever venue offers the best price.
To ask for a particular one, set ``requestedDestination``. Whether the order
actually executes there remains Schwab's decision.

.. autoclass:: schwaby.orders.common::Destination
  :members:
  :undoc-members:
.. automethod:: schwaby.orders.generic.OrderBuilder.set_requested_destination
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_requested_destination

``destinationLinkName`` is a different field and is **not** how you choose a
venue, despite the similar name. Schwab's schema types it as a free string and
:class:`~schwaby.orders.common.Destination` does not apply to it.

.. automethod:: schwaby.orders.generic.OrderBuilder.set_destination_link_name
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_destination_link_name


++++++++++++++++++++
Special Instructions
++++++++++++++++++++

Trades can contain special instructions which handle some edge cases:

.. autoclass:: schwaby.orders.common::SpecialInstruction
  :members:
  :undoc-members:
.. automethod:: schwaby.orders.generic.OrderBuilder.set_special_instruction
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_special_instruction


+++++++++++++++
Tax Lot Method
+++++++++++++++

A closing order can say which lots it sells, which decides the gain it
realises. Leave it unset and the order carries no ``taxLotMethod``; what Schwab
then applies has not been measured here.

.. autoclass:: schwaby.orders.common::TaxLotMethod
  :members:
  :undoc-members:
.. automethod:: schwaby.orders.generic.OrderBuilder.set_tax_lot_method
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_tax_lot_method


++++++++++++++++++++++++++
Complex Options Strategies
++++++++++++++++++++++++++

Schwab supports a number of complex options strategies. These strategies are
complex affairs, with each leg of the trade specified in the order legs. Schwab
performs additional validation on these strategies, so they are somewhat
complicated to place. However, the benefit is more flexibility, as trades like
trailing stop orders based on net debit/credit can be specified.

Unfortunately, due to the complexity of these orders and the lack of any real
documentation, we cannot definitively say how to structure these orders. A few
things have been observed, however:

 * The legs of the order can be placed by adding them as option order legs using
   :meth:`~schwaby.orders.generic.OrderBuilder.add_option_leg`.
 * For spreads resulting in a new debit/credit, the price represents the overall
   debit or credit desired.

If you use these strategies successfully, please say so on the `issue tracker
<https://github.com/tom-hirt/schwaby/issues>`__. What works is worth more here
than what the schema permits.

.. autoclass:: schwaby.orders.common::ComplexOrderStrategyType
  :members:
  :undoc-members:
.. automethod:: schwaby.orders.generic.OrderBuilder.set_complex_order_strategy_type
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_complex_order_strategy_type


++++++++++++++++
Composite Orders
++++++++++++++++

``schwaby`` supports composite order strategies, in which execution of one
order has an effect on another:

 * ``OCO``, or "one cancels other" orders, consist of a pair of orders where
   execution of one immediately cancels the other.
 * ``TRIGGER`` orders consist of a pair of orders where execution of one
   immediately results in placement of the other.

``schwaby`` provides helpers to specify these easily:
:func:`~schwaby.orders.common.one_cancels_other` and
:func:`~schwaby.orders.common.first_triggers_second`. This is almost certainly
easier than specifying these orders manually. However, if you still want to
create them yourself, you can specify these composite order strategies like so:

.. autoclass:: schwaby.orders.common::OrderStrategyType
  :members:
  :undoc-members:
.. automethod:: schwaby.orders.generic.OrderBuilder.set_order_strategy_type
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_order_strategy_type
.. automethod:: schwaby.orders.generic.OrderBuilder.add_child_order_strategy
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_child_order_strategies


+++++++++++++++++++
Undocumented Fields
+++++++++++++++++++

Schwab's order schema contains fields whose behaviour is not documented and
has not been established by experiment. They are collected here rather than
described inaccurately elsewhere. If you know how one of them behaves, please
say so on the `issue tracker
<https://github.com/tom-hirt/schwaby/issues>`__.


.. _undocumented_quantity:

~~~~~~~~
Quantity
~~~~~~~~

This one seems obvious: doesn't the quantity mean the number of stock I want to
buy? The trouble is that the order legs also have a ``quantity`` field, which
suggests this field means something else. The leading hypothesis is that it
outlines the number of copies of the order to place, although we have yet to
verify that.

.. automethod:: schwaby.orders.generic.OrderBuilder.set_quantity
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_quantity


~~~~~~~~~~~~~~~~~~~~~~~~
Stop Order Configuration
~~~~~~~~~~~~~~~~~~~~~~~~

Stop orders and their variants (stop limit, trailing stop, trailing stop limit)
support some rather complex configuration. Both stop prices and the limit
prices of the resulting order can be configured to follow the market in a
dynamic fashion. The market dimensions that they follow can also be configured
differently, and it appears that which dimensions are supported varies by order
type.

We have unfortunately not yet done a thorough analysis of what's supported, nor
have we made the effort to make it simple and easy. While we're *pretty* sure we
understand how these fields work, they've been temporarily placed into the
"undocumented" section, pending a followup. Users are invited to experiment with
these fields at their own risk.


.. automethod:: schwaby.orders.generic.OrderBuilder.set_stop_price
.. automethod:: schwaby.orders.generic.OrderBuilder.copy_stop_price
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_stop_price

.. autoclass:: schwaby.orders.common::StopPriceLinkBasis
  :members:
  :undoc-members:
.. automethod:: schwaby.orders.generic.OrderBuilder.set_stop_price_link_basis
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_stop_price_link_basis

.. autoclass:: schwaby.orders.common::StopPriceLinkType
  :members:
  :undoc-members:
.. automethod:: schwaby.orders.generic.OrderBuilder.set_stop_price_link_type
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_stop_price_link_type

.. automethod:: schwaby.orders.generic.OrderBuilder.set_stop_price_offset
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_stop_price_offset

.. autoclass:: schwaby.orders.common::StopType
  :members:
  :undoc-members:
.. automethod:: schwaby.orders.generic.OrderBuilder.set_stop_type
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_stop_type

.. autoclass:: schwaby.orders.common::PriceLinkBasis
  :members:
  :undoc-members:
.. automethod:: schwaby.orders.generic.OrderBuilder.set_price_link_basis
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_price_link_basis

.. autoclass:: schwaby.orders.common::PriceLinkType
  :members:
  :undoc-members:
.. automethod:: schwaby.orders.generic.OrderBuilder.set_price_link_type
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_price_link_type
.. automethod:: schwaby.orders.generic.OrderBuilder.set_price_offset
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_price_offset

.. automethod:: schwaby.orders.generic.OrderBuilder.set_activation_price
.. automethod:: schwaby.orders.generic.OrderBuilder.clear_activation_price
