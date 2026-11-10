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

import dataclasses
import enum

from .util import UnparsableMessageData, decode_decimal, parse_message_data


#: The most nodes of ``MESSAGE_DATA`` read, and the deepest nesting. A payload
#: past either is :attr:`AccountActivityKind.UNKNOWN`, ``problem='too_large'``.
MAX_NODES = 4000
MAX_DEPTH = 64

_SYMBOL_KEYS = ('Symbol', 'PrimaryMarketSymbol', 'UnderlyingSymbol')
_RELATED_ORDER_KEYS = ('ParentSchwabOrderID', 'LifecycleSchwabOrderID',
                       'LegParentSchwabOrderID')
# Fragments of an ExecutionTransType that say the execution filled nothing.
_NOT_A_FILL = ('UROUT', 'CANCEL', 'BUST', 'REJECT')


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
    #: ``OrderFillCompleted``, with no sign of a cancel: one per execution. A
    #: partial fill is one with ``execution.leaves_quantity`` above zero.
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
    #: ``ExecutionInfo/PriceImprovement``, as sent. Its unit is not
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


@dataclasses.dataclass(frozen=True, repr=False)
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
    #: the item, as ``(path, value)`` pairs.
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
    if isinstance(value, str) and value.isdigit() and value.isascii():
        return int(value)
    return None


def _decimal(value):
    '''A decimal Schwab sent, or None. An empty object is not zero: it says
    nothing, as an absent key does, so a caller asks the REST API rather than
    reading it as nothing left.'''
    if value is None or value == {}:
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
    '''Whether ``value`` has at most MAX_NODES nodes and MAX_DEPTH levels.'''
    stack = [(value, 0)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            return False
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
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


def _event_block(data, message_type):
    '''The event's own block under ``BaseEvent``: the one whose key begins
    with the event's type, such as
    ``OrderFillCompletedEventOrderLegQuantityInfo``.'''
    base = _get(data, 'BaseEvent')
    if not isinstance(base, dict) or not isinstance(message_type, str):
        return None
    folded = message_type.casefold().replace(' ', '')
    for key, value in base.items():
        if (isinstance(key, str) and isinstance(value, dict)
                and key.casefold().replace(' ', '').startswith(folded)):
            return value
    return None


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
            price_improvement=_decimal(_get(info, 'PriceImprovement')),
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
    with no sign of a cancel: no ``ExecutionTransType`` of UROut, Cancel,
    Bust or Reject, and no ``CancelType``. A fragment of a type never makes
    anything a fill, and ``ExecutionCreated`` is
    :attr:`~AccountActivityKind.EXECUTION` whatever it carries, so summing
    ``execution.execution_quantity`` over fills counts each execution once.

    ``symbol`` is the first order leg's ``Security/Symbol`` on
    ``OrderCreated``, and otherwise the shallowest ``Symbol`` in the event
    outside any ``QuoteOnOrderAcceptance``, when it is a non-empty ``str``.
    ``symbols`` lists every symbol in the item, for a caller that must refuse
    an ambiguous one.
    '''
    if not isinstance(item, dict):
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
    folded = (message_type.upper().replace(' ', '')
              if isinstance(message_type, str) else None)
    if isinstance(data, str):
        if not folded:
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

    block = _event_block(data, message_type)
    info = _get(block, 'ExecutionInfo')
    if kind is AccountActivityKind.FILL:
        trans = _get(info, 'ExecutionTransType')
        if (block is None or _cancel_signal(info)
                or (trans is not None and not (
                    isinstance(trans, str)
                    and trans.upper().replace(' ', '') == 'FILL'))):
            kind = AccountActivityKind.UNKNOWN

    leg = _first_leg(data)
    if kind is AccountActivityKind.ORDER_CREATED:
        symbol = _text(_get(leg, 'Security', 'Symbol'))
    else:
        symbol = _text(_first(block, 'Symbol',
                              outside=('QuoteOnOrderAcceptance',)))
    symbols = tuple(('/'.join(at + (name,)), child)
                    for at, name, child in _walk(data)
                    if name in _SYMBOL_KEYS and _text(child))
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
