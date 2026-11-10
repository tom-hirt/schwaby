'''A typed reading of one ``ACCT_ACTIVITY`` content item.

Schwab documents the three fields of an ``ACCT_ACTIVITY`` message and nothing
inside ``MESSAGE_DATA``. :func:`read_account_activity` reads one content item
into an :class:`AccountActivityEvent`, from the shapes the streaming
documentation records as observed. It reads only paths that have been
captured, and it is built around the two traps recorded there:

* a cancel emits ``ExecutionCreated`` carrying a non-zero quantity, on an order
  that filled nothing, so ``ExecutionCreated`` is never :attr:`FILL
  <AccountActivityKind.FILL>`;
* a fill carries three quantities, and only ``CumulativeQuantity`` is the
  running total.

It is pure: it neither logs nor keeps state. It never raises on anything
inside a content item. An item it cannot place is :attr:`UNKNOWN
<AccountActivityKind.UNKNOWN>`, and a doubtful one is never a fill.
'''

import collections.abc
import dataclasses
import enum

from .util import (UnparsableMessageData, _DECIMAL_KEYS, decode_decimal,
                   parse_message_data)


#: The most nodes of ``MESSAGE_DATA`` read, the deepest nesting, and the most
#: characters of keys and text. A payload past any is
#: :attr:`AccountActivityKind.UNKNOWN`, ``problem='too_large'``.
MAX_NODES = 4000
MAX_DEPTH = 64
MAX_TEXT = 1_000_000
#: The most symbols listed in :attr:`AccountActivityEvent.symbols`.
MAX_SYMBOLS = 32

_SYMBOL_KEYS = ('Symbol', 'PrimaryMarketSymbol', 'UnderlyingSymbol')
_RELATED_ORDER_KEYS = ('ParentSchwabOrderID', 'LifecycleSchwabOrderID',
                       'LegParentSchwabOrderID')
# Fragments of an ExecutionTransType that say the execution filled nothing.
_NOT_A_FILL = ('UROUT', 'CANCEL', 'BUST', 'REJECT')
# Keys that only a cancel's events have carried. On an item that says it is
# a fill, any of them anywhere, whatever its value, makes it doubtful. None
# appeared, not even empty, in any of 682 real fills a consumer counted.
_CANCEL_KEYS = frozenset(key.casefold() for key in (
        'CancelType', 'OutCancelType', 'CancelRequestType', 'CancelQuantity',
        'LegCancelRequestInfoList'))


class AccountActivityKind(str, enum.Enum):
    '''What an ``ACCT_ACTIVITY`` item is, as :func:`read_account_activity`
    reads it. A ``str``, whose values do not change. Moving any wire type
    from one kind to another, above all into or out of :attr:`FILL`, is a
    minor release with its own changelog entry.'''

    #: The acknowledgement of the subscription.
    SUBSCRIBED = 'subscribed'
    #: ``OrderCreated``.
    ORDER_CREATED = 'order_created'
    #: ``OrderAccepted``.
    ORDER_ACCEPTED = 'order_accepted'
    #: ``ExecutionRequested``, ``ExecutionRequestCreated`` and
    #: ``ExecutionRequestCompleted``: the order being routed.
    ROUTING = 'routing'
    #: ``ChangeCreated`` and ``ChangeAccepted``: a change to a working order.
    CHANGE = 'change'
    #: ``OrderMonitorCreated`` and ``OrderMonitorCompleted``.
    MONITOR = 'monitor'
    #: ``CancelAccepted``. **Not the end of the order**: it has been captured
    #: with the leg still open, followed by a new routing and only then
    #: ``OrderUROutCompleted``.
    CANCEL_ACCEPTED = 'cancel_accepted'
    #: ``OrderUROutCompleted``: the order came off the book. It does not say
    #: why; a cancel you issued yourself ends this way too.
    OUT = 'out'
    #: ``ExecutionCreated``, whatever its trans-type. **Never a fill**: a
    #: cancel emits one carrying a non-zero quantity, and a real fill is
    #: :attr:`FILL`. ``(order_id, execution.execution_sequence)`` joins the
    #: two where both arrive.
    EXECUTION = 'execution'
    #: ``OrderFillCompleted`` carrying every sign of a fill and no sign of
    #: anything else (see :func:`read_account_activity`): one per execution.
    #: A partial fill is one with ``execution.leaves_quantity`` above zero.
    FILL = 'fill'
    #: An empty type carrying text, such as ``"Feature not supported"``.
    NOTICE = 'notice'
    #: Anything else, and anything doubtful.
    UNKNOWN = 'unknown'

    __str__ = str.__str__


_KINDS = {
    'SUBSCRIBED': AccountActivityKind.SUBSCRIBED,
    'ORDERCREATED': AccountActivityKind.ORDER_CREATED,
    'ORDERACCEPTED': AccountActivityKind.ORDER_ACCEPTED,
    'EXECUTIONREQUESTED': AccountActivityKind.ROUTING,
    'EXECUTIONREQUESTCREATED': AccountActivityKind.ROUTING,
    'EXECUTIONREQUESTCOMPLETED': AccountActivityKind.ROUTING,
    'CHANGECREATED': AccountActivityKind.CHANGE,
    'CHANGEACCEPTED': AccountActivityKind.CHANGE,
    'ORDERMONITORCREATED': AccountActivityKind.MONITOR,
    'ORDERMONITORCOMPLETED': AccountActivityKind.MONITOR,
    'CANCELACCEPTED': AccountActivityKind.CANCEL_ACCEPTED,
    'ORDERUROUTCOMPLETED': AccountActivityKind.OUT,
    'EXECUTIONCREATED': AccountActivityKind.EXECUTION,
    'ORDERFILLCOMPLETED': AccountActivityKind.FILL,
}


@dataclasses.dataclass(frozen=True)
class Execution:
    '''One execution, on a :attr:`~AccountActivityKind.FILL` or an
    :attr:`~AccountActivityKind.EXECUTION`. Every number is a
    :class:`~decimal.Decimal` or None, never a float, and None means absent
    or unreadable, never zero.'''

    #: ``ExecutionInfo/ExecutionId``. Present on every real fill a consumer
    #: has counted; optional so that a shape without it is still read.
    #: De-duplicate on it first.
    execution_id: object = None
    #: ``ExecutionInfo/ExecutionSequenceNumber``, an int: with the order id,
    #: a key for one execution.
    execution_sequence: object = None
    #: ``ExecutionInfo/ExecutionTransType``, as sent: ``"Fill"`` on a fill,
    #: ``"UROut"`` on a cancel's execution.
    trans_type: object = None
    #: This execution alone.
    execution_quantity: object = None
    #: This execution's price.
    execution_price: object = None
    #: ``QuantityInfo/CumulativeQuantity``: how much of the order has filled
    #: so far, on this item. Never filled in from another quantity, and never
    #: summed across items: an order can fill in many.
    cumulative_quantity: object = None
    #: ``LeavesQuantity``: zero when Schwab sent zero, None when it sent
    #: nothing or an empty object.
    leaves_quantity: object = None
    #: ``QuantityInfo/AveragePrice``: the price to book a fill in several
    #: executions at.
    average_price: object = None
    #: ``ExecutionInfo/PrincipalAmmount`` (Schwab's spelling).
    principal: object = None
    #: ``PriceImprovement``, directly under the event block where the
    #: observations put it, else under ``ExecutionInfo``. Its unit is not
    #: established.
    price_improvement: object = None
    #: ``ExecutionInfo/ActualChargedCommissionAmount``.
    commission: object = None
    #: ``SECFees``, ``TAF`` and ``ORF``, under
    #: ``ExecutionInfo/ActualChargedFeesCommissionAndTax``.
    sec_fee: object = None
    taf: object = None
    orf: object = None
    #: ``ExecutionInfo/ExecutionTimeStamp/DateTimeString``, as sent, with no
    #: zone attached.
    executed_at: object = None
    #: The venue's timestamp, under either spelling Schwab has used.
    venue_executed_at: object = None
    route: object = None
    broker: object = None
    venue_execution_id: object = None
    accounting_rule_code: object = None
    leg_status: object = None
    leg_sub_status: object = None


@dataclasses.dataclass(frozen=True)
class Cancel:
    '''What a cancel carried, on :attr:`~AccountActivityKind.CANCEL_ACCEPTED`,
    :attr:`~AccountActivityKind.OUT`, and an
    :attr:`~AccountActivityKind.EXECUTION` that is a cancel's.'''

    #: ``CancelType``, ``OutCancelType`` or ``CancelRequestType``, as sent.
    cancel_type: object = None
    cancel_quantity: object = None
    leaves_quantity: object = None
    leg_status: object = None
    leg_sub_status: object = None


@dataclasses.dataclass(frozen=True, repr=False, eq=False)
class AccountActivityEvent:
    '''One ``ACCT_ACTIVITY`` content item, read. Its ``repr`` and ``str``
    leave out the account, ``data`` and ``raw``, which carry account numbers
    and the stream key; they are attributes only.'''

    #: An :class:`AccountActivityKind`.
    kind: AccountActivityKind
    #: The item's ``MESSAGE_TYPE``, as sent, or None.
    message_type: object = None
    #: The item's ``seq``, an int, or None.
    seq: object = None
    #: The envelope ``ACCOUNT``, as sent.
    account: object = None
    #: The top-level ``SchwabOrderID`` of ``MESSAGE_DATA``, as a ``str``:
    #: never a nested copy, never a key that only ends in it.
    order_id: object = None
    #: ``ParentSchwabOrderID``, ``LifecycleSchwabOrderID`` and
    #: ``LegParentSchwabOrderID``, those present, as ``str``.
    related_order_ids: object = dataclasses.field(default_factory=dict)
    #: The order's symbol, where the item names one plainly (see
    #: :func:`read_account_activity`), else None.
    symbol: object = None
    #: Every ``Symbol``, ``PrimaryMarketSymbol`` and ``UnderlyingSymbol`` in
    #: the item, as ``(path, value)`` pairs, the path a tuple of keys: at most
    #: :data:`MAX_SYMBOLS` of them.
    symbols: tuple = ()
    #: ``BuySellCode``, as sent, or None.
    side: object = None
    #: ``MajorAssetType`` or ``AssetType``, as sent, or None.
    asset_type: object = None
    #: An :class:`Execution`, on a fill or an execution.
    execution: object = None
    #: A :class:`Cancel`, on a cancel.
    cancel: object = None
    #: The text, on a :attr:`~AccountActivityKind.NOTICE`.
    notice: object = None
    #: Why an :attr:`~AccountActivityKind.UNKNOWN` could not be read:
    #: ``'unparsable_message_data'``, ``'too_large'``, ``'unlabelled'`` (an
    #: item with only numeric keys) or ``'unreadable'``. Never set on a fill.
    problem: object = None
    #: ``MESSAGE_DATA``, parsed.
    data: object = None
    #: The content item, as received.
    raw: object = None

    def __repr__(self):
        parts = ['kind={!r}'.format(self.kind.value)]
        for name in ('message_type', 'seq', 'order_id', 'symbol', 'side',
                     'problem'):
            value = getattr(self, name)
            if value is not None:
                parts.append('{}={!r}'.format(name, value))
        return 'AccountActivityEvent({})'.format(', '.join(parts))

    __str__ = __repr__


def _text(value):
    return value if isinstance(value, str) and value else None


def _id(value):
    '''An id as a ``str``: a non-empty ``str``, or an int, else None.'''
    if isinstance(value, str):
        return value or None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _int(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if (isinstance(value, str) and value.isascii() and value.isdigit()
            and len(value) <= 18):
        return int(value)
    return None


def _decimal(value):
    '''A decimal Schwab sent, or None. An empty object is not zero: it says
    nothing, as an absent key does, so a caller asks the REST API rather than
    reading it as nothing left.'''
    if value is None or value == {}:
        return None
    if isinstance(value, dict) and any(key not in _DECIMAL_KEYS
                                       for key in value):
        # Refused here, as decode_decimal would refuse it, but without the
        # warning it logs: this reader logs nothing.
        return None
    try:
        result = decode_decimal(value)
    except Exception:
        return None
    try:
        return result if result is not None and result.is_finite() else None
    except Exception:
        return None


def _get(mapping, *path):
    for key in path:
        if not isinstance(mapping, dict):
            return None
        mapping = mapping.get(key)
    return mapping


def _within_bounds(value):
    '''Whether ``value`` has at most MAX_NODES nodes, MAX_DEPTH levels and
    MAX_TEXT characters of keys and text.'''
    stack = [(value, 0)]
    nodes = text = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if isinstance(node, str):
            text += len(node)
        if nodes > MAX_NODES or depth > MAX_DEPTH or text > MAX_TEXT:
            return False
        if isinstance(node, dict):
            for key, child in node.items():
                text += len(key) if isinstance(key, str) else 0
                stack.append((child, depth + 1))
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)
    return True


def _walk(value, path=()):
    '''Every (path, key, value) in ``value``, breadth first. Bounded by the
    caller's check.'''
    queue = [(value, path)]
    while queue:
        node, at = queue.pop(0)
        if isinstance(node, dict):
            for key, child in node.items():
                yield at, key, child
                queue.append((child, at + (str(key),)))
        elif isinstance(node, list):
            for i, child in enumerate(node):
                queue.append((child, at + (str(i),)))


def _first(value, key, *, outside=()):
    '''The shallowest value under ``key``, outside any of the keys
    ``outside``, or None.'''
    for at, name, child in _walk(value):
        if name == key and not any(part in outside for part in at):
            return child
    return None


def _first_leg(data):
    legs = _first(data, 'OrderLegs')
    if isinstance(legs, list) and legs and isinstance(legs[0], dict):
        return legs[0]
    return None


def _event_blocks(data, message_type):
    '''The event's own blocks under ``BaseEvent``: those whose key begins
    with the event's type, such as
    ``OrderFillCompletedEventOrderLegQuantityInfo``. One is expected.'''
    base = _get(data, 'BaseEvent')
    if not isinstance(base, dict) or not isinstance(message_type, str):
        return []
    folded = message_type.casefold().replace(' ', '')
    return [value for key, value in base.items()
            if isinstance(key, str) and isinstance(value, dict)
            and key.casefold().replace(' ', '').startswith(folded)]


def _doubtful_fill(data, message_type, blocks):
    '''Whether an item that says it is a fill shows any sign that it is not
    one, anywhere in it: so that a doubtful item is never a fill.

    Each rule held on every one of 682 real fills a consumer counted: one
    event block and one ``ExecutionInfo`` each, ``EventType`` present (twice)
    and always the fill's own, ``ExecutionTransType`` present and ``Fill``, a
    positive ``ExecutionQuantity``, no cancel key, and a ``LegSubStatus`` of
    filled (616) or partially filled (66).'''
    if len(blocks) != 1 or not isinstance(_get(blocks[0], 'ExecutionInfo'),
                                         dict):
        return True
    quantity = _decimal(_get(blocks[0], 'ExecutionInfo', 'ExecutionQuantity'))
    if quantity is None or quantity <= 0:
        return True
    own = _fold(message_type)
    infos = events = trans = legs = 0
    for _, key, child in _walk(data):
        folded = key.casefold() if isinstance(key, str) else None
        if folded in _CANCEL_KEYS:
            return True
        if folded == 'executioninfo':
            infos += 1
        elif folded == 'eventtype':
            if not (isinstance(child, str) and _fold(child) == own):
                return True
            events += 1
        elif folded == 'executiontranstype':
            if not (isinstance(child, str) and _fold(child) == 'FILL'):
                return True
            trans += 1
        elif folded == 'legsubstatus':
            if not _filled_leg(child):
                return True
            legs += 1
    # Each was present on every real fill counted: required, not just not
    # contradicted, so that an item missing the one signal that tells a
    # fill from a cancel is never a fill.
    return infos != 1 or not events or not trans or not legs


#: The two ``LegSubStatus`` values seen on real fills, folded: 616 and 66 of
#: 682. Anything else, ``Unfilled`` and ``Overfilled`` included, is doubtful.
_FILLED_LEGS = frozenset(('LEGSUBSTATUSFILLED', 'LEGSUBSTATUSPARTIALLYFILLED'))


def _filled_leg(value):
    '''Whether a ``LegSubStatus`` is one of the two seen on real fills.'''
    return isinstance(value, str) and _fold(value) in _FILLED_LEGS


def _fold(text):
    return text.upper().replace(' ', '')


def _cancel_signal(info):
    '''Whether an ``ExecutionInfo`` says its execution filled nothing.'''
    if not isinstance(info, dict):
        return False
    if info.get('CancelType') is not None:
        return True
    trans = info.get('ExecutionTransType')
    if isinstance(trans, str):
        folded = trans.upper().replace(' ', '')
        return any(fragment in folded for fragment in _NOT_A_FILL)
    return False


def _execution(block):
    info = _get(block, 'ExecutionInfo')
    quantity = _get(block, 'QuantityInfo')
    fees = _get(info, 'ActualChargedFeesCommissionAndTax')
    posting = _get(block, 'OrderInfoForTransactionPosting')
    leaves = _get(quantity, 'LeavesQuantity')
    if leaves is None:
        leaves = _get(block, 'LeavesQuantity')
    principal = _get(info, 'PrincipalAmmount')
    if principal is None:
        principal = _get(info, 'PrincipalAmount')
    commission = _get(info, 'ActualChargedCommissionAmount')
    if commission is None:
        commission = _get(info, 'CommissionAmount')
    venue_at = _get(info, 'VenuExecutionTimeStamp', 'DateTimeString')
    if venue_at is None:
        venue_at = _get(info, 'VenueExecutionTimeStamp', 'DateTimeString')
    return Execution(
            execution_id=_id(_get(info, 'ExecutionId')),
            execution_sequence=_int(_get(info, 'ExecutionSequenceNumber')),
            trans_type=_text(_get(info, 'ExecutionTransType')),
            execution_quantity=_decimal(_get(info, 'ExecutionQuantity')),
            execution_price=_decimal(_get(info, 'ExecutionPrice')),
            cumulative_quantity=_decimal(
                _get(quantity, 'CumulativeQuantity')),
            leaves_quantity=_decimal(leaves),
            average_price=_decimal(_get(quantity, 'AveragePrice')),
            principal=_decimal(principal),
            price_improvement=_decimal(
                _get(block, 'PriceImprovement')
                if _get(block, 'PriceImprovement') is not None
                else _get(info, 'PriceImprovement')),
            commission=_decimal(commission),
            sec_fee=_decimal(_get(fees, 'SECFees')),
            taf=_decimal(_get(fees, 'TAF')),
            orf=_decimal(_get(fees, 'ORF')),
            executed_at=_text(
                _get(info, 'ExecutionTimeStamp', 'DateTimeString')),
            venue_executed_at=_text(venue_at),
            route=_text(_get(info, 'RouteName')),
            broker=_text(_get(info, 'ExecutionBroker')),
            venue_execution_id=_id(_get(info, 'VenueExecutionID')),
            accounting_rule_code=_text(_get(posting, 'AccountingRuleCode')),
            leg_status=_text(_get(block, 'LegStatus')),
            leg_sub_status=_text(_get(block, 'LegSubStatus')))


def _cancel(block):
    info = _get(block, 'ExecutionInfo')
    request = _get(block, 'LegCancelRequestInfoList')
    leg = request[0] if isinstance(request, list) and request and \
        isinstance(request[0], dict) else {}
    cancel_type = _get(info, 'CancelType')
    for key in ('OutCancelType', 'CancelRequestType'):
        if cancel_type is None:
            cancel_type = _get(block, key)
    return Cancel(
            cancel_type=_text(cancel_type),
            cancel_quantity=_decimal(_get(block, 'CancelQuantity')),
            leaves_quantity=_decimal(_get(block, 'LeavesQuantity')),
            leg_status=_text(_get(block, 'LegStatus')
                             or leg.get('LegStatus')),
            leg_sub_status=_text(_get(block, 'LegSubStatus')
                                 or leg.get('LegSubStatus')))


def read_account_activity(item):
    '''Reads one ``ACCT_ACTIVITY`` content item, as a handler receives it in
    ``msg['content']``, into an :class:`AccountActivityEvent`.

    It never raises on anything inside the item; it raises ``TypeError``
    only when ``item`` is not a mapping. Whatever it cannot place is
    :attr:`AccountActivityKind.UNKNOWN`.

    **A fill is** ``OrderFillCompleted`` **and nothing else**, and then only
    with every sign of one and no sign anywhere in the item that it is not
    one. It must have exactly one event block of that type, carrying an
    ``ExecutionInfo`` with an ``ExecutionQuantity`` above zero, and exactly
    one ``ExecutionInfo`` in the item. At least one ``EventType``,
    ``ExecutionTransType`` and ``LegSubStatus`` must be present, and every one
    of them must be the fill's own type, ``Fill``, and filled or partially
    filled. No ``CancelType``, ``OutCancelType``, ``CancelRequestType``,
    ``CancelQuantity`` or ``LegCancelRequestInfoList`` may appear at all; those
    keys are matched whatever their case, while the paths read for values are
    matched as Schwab spells them. A fragment of a type never makes anything a
    fill, and ``ExecutionCreated``
    is :attr:`~AccountActivityKind.EXECUTION` whatever it carries, so summing
    ``execution.execution_quantity`` over fills counts each execution once,
    once an item delivered twice, as a replay is, has been dropped by its
    ``execution.execution_id``.

    ``symbol`` is the first order leg's ``Security/Symbol`` on
    ``OrderCreated``, and otherwise the shallowest ``Symbol`` in the event
    outside any ``QuoteOnOrderAcceptance``, when it is a non-empty ``str``.
    ``side`` and ``asset_type`` come from the order leg on ``OrderCreated``
    only: on a fill they are the fill's own, or None, since an order's first
    leg need not be the leg that filled. ``symbols`` lists the symbols in the
    item, for a caller that must refuse an ambiguous one.
    '''
    if not isinstance(item, collections.abc.Mapping):
        raise TypeError('an ACCT_ACTIVITY content item is a mapping, not '
                        '{}'.format(type(item).__name__))
    try:
        return _read(item)
    except Exception:
        return AccountActivityEvent(AccountActivityKind.UNKNOWN,
                                    problem='unreadable', raw=item)


def _read(item):
    message_type = item.get('MESSAGE_TYPE')
    common = dict(message_type=message_type if isinstance(message_type, str)
                  else None,
                  seq=_int(item.get('seq')), account=item.get('ACCOUNT'),
                  raw=item)
    if 'MESSAGE_TYPE' not in item and item and all(
            isinstance(key, str) and key.isdigit() for key in item):
        return AccountActivityEvent(AccountActivityKind.UNKNOWN,
                                    problem='unlabelled', **common)
    try:
        data = parse_message_data(item.get('MESSAGE_DATA'))
    except UnparsableMessageData:
        return AccountActivityEvent(AccountActivityKind.UNKNOWN,
                                    problem='unparsable_message_data',
                                    **common)
    # A type outside ASCII folds unpredictably (a dotless i, a ligature), so
    # it is never matched against the known ones.
    folded = (_fold(message_type)
              if isinstance(message_type, str) and message_type.isascii()
              else None)
    if isinstance(data, str):
        if isinstance(message_type, str) and not message_type.strip():
            return AccountActivityEvent(AccountActivityKind.NOTICE,
                                        notice=data, data=data, **common)
        return AccountActivityEvent(AccountActivityKind.UNKNOWN, data=data,
                                    **common)
    if data is not None and not _within_bounds(data):
        return AccountActivityEvent(AccountActivityKind.UNKNOWN,
                                    problem='too_large', **common)
    kind = _KINDS.get(folded, AccountActivityKind.UNKNOWN)
    if not isinstance(data, dict):
        if kind is AccountActivityKind.SUBSCRIBED:
            return AccountActivityEvent(kind, data=data, **common)
        return AccountActivityEvent(AccountActivityKind.UNKNOWN, data=data,
                                    **common)

    blocks = _event_blocks(data, message_type)
    block = blocks[0] if blocks else None
    info = _get(block, 'ExecutionInfo')
    if kind is AccountActivityKind.FILL and _doubtful_fill(
            data, message_type, blocks):
        kind = AccountActivityKind.UNKNOWN

    leg = (_first_leg(data) if kind is AccountActivityKind.ORDER_CREATED
           else None)
    if kind is AccountActivityKind.ORDER_CREATED:
        symbol = _text(_get(leg, 'Security', 'Symbol'))
    else:
        # The event's own symbol, outside any quote; none when the event holds
        # more than one, as an order of several legs might, since then which
        # leg this is cannot be told.
        found = {child for at, name, child in _walk(block or {})
                 if name == 'Symbol' and _text(child)
                 and 'QuoteOnOrderAcceptance' not in at}
        symbol = found.pop() if len(found) == 1 else None
    symbols = []
    for at, name, child in _walk(data):
        if name in _SYMBOL_KEYS and _text(child):
            symbols.append((at + (name,), child))
            if len(symbols) == MAX_SYMBOLS:
                break
    symbols = tuple(symbols)
    related = {}
    for key in _RELATED_ORDER_KEYS:
        value = _id(_first(data, key))
        if value is not None:
            related[key] = value
    posting = _get(block, 'OrderInfoForTransactionPosting')
    side = _text(_get(posting, 'BuySellCode')) or _text(
            _get(leg, 'BuySellCode'))
    asset_type = (_text(_get(leg, 'Security', 'MajorAssetType'))
                  or _text(_get(posting, 'AssetType'))
                  or _text(_get(block, 'AssetOrderEquityOrderLeg',
                                'AssetType')))

    execution = cancel = None
    if kind in (AccountActivityKind.FILL, AccountActivityKind.EXECUTION):
        execution = _execution(block)
    if (kind in (AccountActivityKind.CANCEL_ACCEPTED,
                 AccountActivityKind.OUT)
            or (kind is AccountActivityKind.EXECUTION
                and _cancel_signal(info))):
        cancel = _cancel(block)
    return AccountActivityEvent(
            kind, order_id=_id(data.get('SchwabOrderID')),
            related_order_ids=related, symbol=symbol, symbols=symbols,
            side=side, asset_type=asset_type, execution=execution,
            cancel=cancel, data=data, **common)
