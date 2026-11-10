'''Tests for schwaby.contrib.account_activity: a typed reading of one
ACCT_ACTIVITY content item.

Every fixture is synthetic, built from the key paths the streaming
documentation records as observed. Ids are placeholders.
'''

import collections.abc
import dataclasses
import decimal
import json
import logging
import unittest

from schwaby.contrib import account_activity as aa
from schwaby.contrib.account_activity import (
        AccountActivityKind as K, read_account_activity)

from ..utils import no_duplicates


def dec(mantissa, scale=12):
    return {'lo': str(mantissa), 'signScale': scale}


ONE = dec(1000000)          # 1
ZERO = {'signScale': 12}    # zero, no mantissa


def item(message_type, data, seq=1):
    return {'seq': seq, 'key': 'Account Activity', 'ACCOUNT': '<ACCOUNT>',
            'MESSAGE_TYPE': message_type,
            'MESSAGE_DATA': data if isinstance(data, str) or data is None
            else json.dumps(data)}


def fill(*, sequence=1, cumulative=ONE, leaves=ZERO, execution=ONE,
         price=dec(150250000), average=dec(150250000), trans='Fill',
         extra_info=None, extra_block=None, order_id='<ORDER_ID>'):
    info = {
        'ExecutionSequenceNumber': sequence,
        'ExecutionId': '<EXEC_ID_%d>' % sequence,
        'ExecutionQuantity': execution,
        'ExecutionPrice': price,
        'ExecutionTransType': trans,
        'PrincipalAmmount': dec(150250000),
        'PriceImprovement': dec(10000),
        'ActualChargedCommissionAmount': ZERO,
        'ActualChargedFeesCommissionAndTax': {
            'SECFees': dec(10000), 'TAF': ZERO, 'ORF': {}},
        'ExecutionTimeStamp': {'DateTimeString': '2026-07-28 10:01:02.345'},
        'VenuExecutionTimeStamp': {
            'DateTimeString': '2026-07-28 10:01:02.111'},
        'RouteName': '<ROUTE>',
        'ExecutionBroker': '<BROKER>',
        'VenueExecutionID': '<VENUE_ID>',
    }
    if trans is None:
        del info['ExecutionTransType']
    info.update(extra_info or {})
    quantity = {'AveragePrice': average, 'LeavesQuantity': leaves}
    if cumulative is not None:
        quantity['CumulativeQuantity'] = cumulative
    block = {
        'LegStatus': 'LegClosed' if leaves == ZERO else 'LegOpen',
        'LegSubStatus': 'LegSubStatusFilled',
        'QuantityInfo': quantity,
        'ExecutionInfo': info,
        'OrderInfoForTransactionPosting': {
            'BuySellCode': 'Buy', 'AssetType': 'Equity',
            'AccountingRuleCode': 'Cash', 'Quantity': dec(2000000)},
        'Symbol': 'F',
    }
    block.update(extra_block or {})
    return {'SchwabOrderID': order_id, 'AccountNumber': '<ACCOUNT>',
            'BaseEvent': {
                'EventType': 'OrderFillCompleted',
                'OrderFillCompletedEventOrderLegQuantityInfo': block}}


def cancel_execution():
    return {'SchwabOrderID': '<ORDER_ID>', 'AccountNumber': '<ACCOUNT>',
            'BaseEvent': {
                'EventType': 'ExecutionCreated',
                'ExecutionCreatedEventExecutionInfo': {
                    'LegId': '<ORDER_ID>',
                    'ExecutionInfo': {
                        'ExecutionSequenceNumber': 1,
                        'ExecutionId': '<EXEC_ID>',
                        'ExecutionQuantity': ONE,
                        'ExecutionTransType': 'UROut',
                        'CancelType': 'ClientCancel',
                        'ExecutionTimeStamp': {'DateTimeString': '<T>'},
                        'VenuExecutionTimeStamp': {'DateTimeString': '<T>'},
                        'RouteName': '<ROUTE>'}}}}


def out():
    return {'SchwabOrderID': '<ORDER_ID>', 'AccountNumber': '<ACCOUNT>',
            'BaseEvent': {
                'EventType': 'OrderUROutCompleted',
                'OrderUROutCompletedEvent': {
                    'LegId': '<ORDER_ID>', 'ExecutionId': '<EXEC_ID>',
                    'LeavesQuantity': ZERO, 'CancelQuantity': ONE,
                    'LegStatus': 'LegClosed',
                    'LegSubStatus': 'LegSubStatusCancelled',
                    'OutCancelType': 'ClientCancel'}}}


def cancel_accepted():
    return {'SchwabOrderID': '<ORDER_ID>', 'AccountNumber': '<ACCOUNT>',
            'BaseEvent': {
                'EventType': 'CancelAccepted',
                'CancelAcceptedEvent': {
                    'CancelRequestType': 'ClientCancel',
                    'LegCancelRequestInfoList': [{
                        'LegID': '<ORDER_ID>', 'LegStatus': 'LegOpen',
                        'LegSubStatus': 'LegSubStatusCancelled'}]}}}


def order_created():
    return {'SchwabOrderID': '<ORDER_ID>', 'AccountNumber': '<ACCOUNT>',
            'BaseEvent': {
                'EventType': 'OrderCreated',
                'OrderCreatedEventEquityOrder': {
                    'Order': {'OrderLegs': [{
                        'BuySellCode': 'Buy', 'Quantity': dec(2000000),
                        'Security': {'Symbol': 'F',
                                     'PrimaryMarketSymbol': 'F',
                                     'UnderlyingSymbol': 'F',
                                     'MajorAssetType': 'Equity'},
                        'QuoteOnOrderAcceptance': {'Symbol': 'F'}}]}}}}


class FillTest(unittest.TestCase):

    @no_duplicates
    def test_a_fill_in_one_execution(self):
        event = read_account_activity(item('OrderFillCompleted', fill()))
        self.assertIs(K.FILL, event.kind)
        self.assertEqual('<ORDER_ID>', event.order_id)
        self.assertEqual(('F', 'Buy', 'Equity'),
                         (event.symbol, event.side, event.asset_type))
        x = event.execution
        self.assertEqual('<EXEC_ID_1>', x.execution_id)
        self.assertEqual(1, x.execution_sequence)
        self.assertEqual(decimal.Decimal(1), x.execution_quantity)
        self.assertEqual(decimal.Decimal(1), x.cumulative_quantity)
        self.assertEqual(decimal.Decimal(0), x.leaves_quantity)
        self.assertEqual(decimal.Decimal('150.25'), x.execution_price)
        self.assertEqual(decimal.Decimal('150.25'), x.average_price)
        self.assertEqual(decimal.Decimal('0.01'), x.sec_fee)
        self.assertEqual(decimal.Decimal(0), x.taf)
        self.assertEqual('2026-07-28 10:01:02.345', x.executed_at)
        self.assertEqual('2026-07-28 10:01:02.111', x.venue_executed_at)
        self.assertEqual(('<ROUTE>', '<BROKER>', '<VENUE_ID>', 'Cash'),
                         (x.route, x.broker, x.venue_execution_id,
                          x.accounting_rule_code))

    @no_duplicates
    def test_a_fill_in_two_executions_is_two_fills_each_its_own(self):
        first = read_account_activity(item('OrderFillCompleted', fill(
                sequence=1, cumulative=ONE, leaves=ONE, execution=ONE,
                average=dec(150000000)), seq=5))
        second = read_account_activity(item('OrderFillCompleted', fill(
                sequence=2, cumulative=dec(2000000), leaves=ZERO,
                execution=ONE, price=dec(151000000),
                average=dec(150500000)), seq=6))
        self.assertEqual([K.FILL, K.FILL], [first.kind, second.kind])
        self.assertEqual(decimal.Decimal(1), first.execution.leaves_quantity)
        self.assertEqual(decimal.Decimal(2),
                         second.execution.cumulative_quantity)
        self.assertEqual(decimal.Decimal(1),
                         second.execution.execution_quantity,
                         'the second tranche read as the running total')
        self.assertEqual(decimal.Decimal('150.5'),
                         second.execution.average_price)
        self.assertEqual([1, 2], [first.execution.execution_sequence,
                                  second.execution.execution_sequence])

    @no_duplicates
    def test_a_cumulative_quantity_absent_is_never_filled_in(self):
        event = read_account_activity(item('OrderFillCompleted',
                                           fill(cumulative=None)))
        self.assertIs(K.FILL, event.kind)
        self.assertIsNone(event.execution.cumulative_quantity,
                          'cumulative was filled in from another quantity')
        self.assertEqual(decimal.Decimal(1),
                         event.execution.execution_quantity)

    @no_duplicates
    def test_an_empty_object_is_not_zero(self):
        event = read_account_activity(item('OrderFillCompleted',
                                           fill(leaves={})))
        self.assertIsNone(event.execution.leaves_quantity,
                          'an empty object read as nothing left')
        self.assertIsNone(event.execution.orf)

    @no_duplicates
    def test_no_float_anywhere(self):
        event = read_account_activity(item('OrderFillCompleted', fill()))
        for field in dataclasses.fields(event.execution):
            value = getattr(event.execution, field.name)
            self.assertNotIsInstance(value, float, field.name)

    @no_duplicates
    def test_price_improvement_and_leaves_under_the_block(self):
        x = read_account_activity(item('OrderFillCompleted', fill())).execution
        self.assertEqual(decimal.Decimal('0.01'), x.price_improvement)
        data = fill(extra_info={'PriceImprovement': None})
        block = data['BaseEvent']['OrderFillCompletedEventOrderLegQuantityInfo']
        block['PriceImprovement'] = dec(20000)
        del block['QuantityInfo']['LeavesQuantity']
        block['LeavesQuantity'] = ONE
        x = read_account_activity(item('OrderFillCompleted', data)).execution
        self.assertEqual(decimal.Decimal('0.02'), x.price_improvement)
        self.assertEqual(decimal.Decimal(1), x.leaves_quantity,
                         "the block's own leaves were not read")
        both = fill()
        both['BaseEvent']['OrderFillCompletedEventOrderLegQuantityInfo'][
                'PriceImprovement'] = dec(30000)
        x = read_account_activity(item('OrderFillCompleted', both)).execution
        self.assertEqual(decimal.Decimal('0.03'), x.price_improvement,
                         "the block's own price improvement did not win")

    @no_duplicates
    def test_a_fills_side_is_never_another_legs(self):
        data = fill()
        block = data['BaseEvent']['OrderFillCompletedEventOrderLegQuantityInfo']
        del block['OrderInfoForTransactionPosting']['BuySellCode']
        del block['OrderInfoForTransactionPosting']['AssetType']
        data['Order'] = {'OrderLegs': [
                {'BuySellCode': 'Buy',
                 'Security': {'MajorAssetType': 'Equity'}},
                {'BuySellCode': 'Sell'}]}
        event = read_account_activity(item('OrderFillCompleted', data))
        self.assertIs(K.FILL, event.kind)
        self.assertIsNone(event.side, "a fill took the first leg's side")
        self.assertIsNone(event.asset_type)

    @no_duplicates
    def test_the_other_spellings(self):
        data = fill(extra_info={'PrincipalAmmount': None,
                                'PrincipalAmount': dec(5000000),
                                'ActualChargedCommissionAmount': None,
                                'CommissionAmount': dec(1000000),
                                'VenuExecutionTimeStamp': None,
                                'VenueExecutionTimeStamp': {
                                    'DateTimeString': '<VENUE_T>'}})
        x = read_account_activity(item('OrderFillCompleted', data)).execution
        self.assertEqual(decimal.Decimal(5), x.principal)
        self.assertEqual(decimal.Decimal(1), x.commission)
        self.assertEqual('<VENUE_T>', x.venue_executed_at)


class NeverAFillTest(unittest.TestCase):

    @no_duplicates
    def test_a_cancels_execution_is_never_a_fill(self):
        event = read_account_activity(item('ExecutionCreated',
                                           cancel_execution()))
        self.assertIs(K.EXECUTION, event.kind)
        self.assertEqual(decimal.Decimal(1),
                         event.execution.execution_quantity)
        self.assertEqual('ClientCancel', event.cancel.cancel_type)

    @no_duplicates
    def test_an_out_execution_without_a_cancel_type_is_a_cancels(self):
        data = cancel_execution()
        del data['BaseEvent']['ExecutionCreatedEventExecutionInfo'][
                'ExecutionInfo']['CancelType']
        event = read_account_activity(item('ExecutionCreated', data))
        self.assertIs(K.EXECUTION, event.kind)
        self.assertIsNotNone(event.cancel,
                             "an out's execution was not read as a cancel's")

    @no_duplicates
    def test_an_execution_with_a_cancel_type_is_a_cancels(self):
        data = cancel_execution()
        data['BaseEvent']['ExecutionCreatedEventExecutionInfo'][
                'ExecutionInfo']['ExecutionTransType'] = 'Fill'
        event = read_account_activity(item('ExecutionCreated', data))
        self.assertIsNotNone(event.cancel,
                             'a cancel type did not make it a cancel')

    @no_duplicates
    def test_an_execution_saying_fill_is_still_not_a_fill(self):
        # A real fill is OrderFillCompleted; were a fill also to emit an
        # ExecutionCreated, reading both as fills would count it twice.
        data = cancel_execution()
        info = data['BaseEvent']['ExecutionCreatedEventExecutionInfo'][
                'ExecutionInfo']
        info['ExecutionTransType'] = 'Fill'
        del info['CancelType']
        event = read_account_activity(item('ExecutionCreated', data))
        self.assertIs(K.EXECUTION, event.kind,
                      'an execution was read as a fill')
        self.assertIsNone(event.cancel)

    @no_duplicates
    def test_a_fill_with_any_sign_of_a_cancel_is_unknown(self):
        for name, data in (
                ('a cancel type', fill(extra_info={
                    'CancelType': 'ClientCancel'})),
                ('an out', fill(trans='UROut')),
                ('a bust', fill(trans='Bust')),
                ('a reject', fill(trans='Reject')),
                ('another trans-type', fill(trans='Correction')),
                ('no event block', {'SchwabOrderID': '<ORDER_ID>',
                                    'BaseEvent': {}})):
            with self.subTest(name):
                event = read_account_activity(
                        item('OrderFillCompleted', data))
                self.assertIs(K.UNKNOWN, event.kind,
                              'a doubtful fill was read as a fill')
                self.assertIsNone(event.execution)

    @no_duplicates
    def test_a_sign_of_a_cancel_anywhere_keeps_a_fill_off(self):
        def with_(change):
            data = fill()
            change(data)
            return data
        block = 'OrderFillCompletedEventOrderLegQuantityInfo'
        for name, data in (
                ('a cancel type on the block', with_(
                    lambda d: d['BaseEvent'][block].update(
                        CancelType='ClientCancel'))),
                ('a trans-type at the top', with_(
                    lambda d: d.update(ExecutionTransType='UROut'))),
                ('a cancel type at the top', with_(
                    lambda d: d.update(CancelType='ClientCancel'))),
                ('a lowercase cancel type', with_(
                    lambda d: d.update(cancelType='ClientCancel'))),
                ('a lowercase trans-type', with_(
                    lambda d: d.update(Other={
                        'executionTransType': 'UROut'}))),
                ('an empty execution info', with_(
                    lambda d: d['BaseEvent'][block].update(
                        ExecutionInfo={}))),
                ('a zero quantity', with_(
                    lambda d: d['BaseEvent'][block]['ExecutionInfo'].update(
                        ExecutionQuantity=ZERO))),
                ('a second block without an execution info', with_(
                    lambda d: d['BaseEvent'].update(
                        OrderFillCompletedEventOther={'Note': 'x'}))),
                ('the execution info outside the block', with_(
                    lambda d: d.update(ExecutionInfo=d['BaseEvent'][
                        block].pop('ExecutionInfo')))),
                ('a second execution info', with_(
                    lambda d: d.update(Other={'ExecutionInfo': {
                        'ExecutionTransType': 'Fill'}}))),
                ('an event type deeper down', with_(
                    lambda d: d['BaseEvent'][block].update(
                        EventType='ExecutionCreated'))),
                ('a cancel quantity', with_(
                    lambda d: d['BaseEvent'][block].update(
                        CancelQuantity=ONE))),
                ('a cancelled leg', with_(
                    lambda d: d['BaseEvent'][block].update(
                        LegSubStatus='LegSubStatusCancelled'))),
                ('a negative quantity', with_(
                    lambda d: d['BaseEvent'][block]['ExecutionInfo'].update(
                        ExecutionQuantity=dec(1000000, 13)))),
                ('a second block', with_(
                    lambda d: d['BaseEvent'].update(
                        OrderFillCompletedEventExecutionInfo={
                            'ExecutionInfo': {
                                'ExecutionTransType': 'Fill'}}))),
                ('another event type', with_(
                    lambda d: d['BaseEvent'].update(
                        EventType='ExecutionCreated'))),
                ('a null cancel type', with_(
                    lambda d: d['BaseEvent'][block]['ExecutionInfo'].update(
                        CancelType=None))),
                ('an out cancel type', with_(
                    lambda d: d.update(OutCancelType='ClientCancel'))),
                ('no execution info', with_(
                    lambda d: d['BaseEvent'][block].pop('ExecutionInfo')))):
            with self.subTest(name):
                event = read_account_activity(
                        item('OrderFillCompleted', data))
                self.assertIs(K.UNKNOWN, event.kind,
                              'a fill showing a cancel was read as a fill')

    @no_duplicates
    def test_a_type_outside_ascii_is_never_a_fill(self):
        for message_type in ('OrderF\u0131llCompleted',
                             'Order\ufb01llCompleted'):
            with self.subTest(message_type):
                data = fill()
                base = data['BaseEvent']
                base[message_type + 'EventOrderLegQuantityInfo'] = base.pop(
                        'OrderFillCompletedEventOrderLegQuantityInfo')
                base['EventType'] = message_type
                self.assertIs(K.UNKNOWN, read_account_activity(
                        item(message_type, data)).kind)

    @no_duplicates
    def test_an_execution_of_any_cancel_trans_type_carries_the_cancel(self):
        for trans in ('Cancel', 'Bust', 'Reject', 'UROut'):
            with self.subTest(trans):
                data = cancel_execution()
                info = data['BaseEvent'][
                        'ExecutionCreatedEventExecutionInfo']['ExecutionInfo']
                del info['CancelType']
                info['ExecutionTransType'] = trans
                event = read_account_activity(item('ExecutionCreated', data))
                self.assertIsNotNone(event.cancel,
                                     'a cancel trans-type was not a cancel')

    @no_duplicates
    def test_what_real_fills_carry_keeps_them_fills(self):
        # Every real fill counted carried EventType twice, its own both
        # times, and a LegSubStatus of filled or partially filled.
        for sub in ('LegSubStatusFilled', 'LegSubStatusPartiallyFilled'):
            with self.subTest(sub):
                data = fill(extra_block={'LegSubStatus': sub,
                                         'EventType': 'OrderFillCompleted'})
                self.assertIs(K.FILL, read_account_activity(
                        item('OrderFillCompleted', data)).kind,
                        'a real shape of fill was refused')

    @no_duplicates
    def test_a_fill_missing_a_sign_of_one_is_unknown(self):
        # Every real fill counted carried each of these: an item without one
        # lacks the signal that tells a fill from a cancel.
        block = 'OrderFillCompletedEventOrderLegQuantityInfo'
        no_event = fill()
        del no_event['BaseEvent']['EventType']
        no_leg = fill()
        del no_leg['BaseEvent'][block]['LegSubStatus']
        for name, data in (('no trans-type', fill(trans=None)),
                           ('no event type', no_event),
                           ('no leg sub-status', no_leg)):
            with self.subTest(name):
                self.assertIs(K.UNKNOWN, read_account_activity(
                        item('OrderFillCompleted', data)).kind,
                        'an item missing a sign of a fill was read as one')

    @no_duplicates
    def test_a_leg_that_ended_otherwise_is_not_a_fill(self):
        for sub in ('LegSubStatusRejected', 'LegSubStatusBusted',
                    'LegSubStatusUROut', 'LegSubStatusExpired',
                    'LegSubStatusReplaced', 'LegSubStatusCancelledFilled'):
            with self.subTest(sub):
                data = fill(extra_block={'LegSubStatus': sub})
                self.assertIs(K.UNKNOWN, read_account_activity(
                        item('OrderFillCompleted', data)).kind,
                        'a leg that ended otherwise was read as a fill')
        data = fill(extra_block={'LegSubStatus': {'Value': 'Filled'}})
        self.assertIs(K.UNKNOWN, read_account_activity(
                item('OrderFillCompleted', data)).kind)

    @no_duplicates
    def test_no_fragment_makes_a_fill(self):
        for message_type in ('OrderPartialFill', 'OrderFilled',
                             'OrderDoneForDay', 'OrderExpired',
                             'OrderRejected', 'SomeFillLikeThing'):
            with self.subTest(message_type):
                # Its event block named after it, as a real one's would be,
                # so only the type decides.
                data = fill()
                base = data['BaseEvent']
                base[message_type + 'EventOrderLegQuantityInfo'] = base.pop(
                        'OrderFillCompletedEventOrderLegQuantityInfo')
                base['EventType'] = message_type
                event = read_account_activity(item(message_type, data))
                self.assertIs(K.UNKNOWN, event.kind,
                              'a fragment made a fill')


class KindsTest(unittest.TestCase):

    @no_duplicates
    def test_each_captured_type(self):
        for message_type, kind in (
                ('OrderCreated', K.ORDER_CREATED),
                ('OrderAccepted', K.ORDER_ACCEPTED),
                ('ExecutionRequested', K.ROUTING),
                ('ExecutionRequestCreated', K.ROUTING),
                ('ExecutionRequestCompleted', K.ROUTING),
                ('ChangeCreated', K.CHANGE), ('ChangeAccepted', K.CHANGE),
                ('OrderMonitorCreated', K.MONITOR),
                ('OrderMonitorCompleted', K.MONITOR),
                ('orderFILLcompleted', K.FILL),
                ('Order Fill Completed', K.FILL)):
            with self.subTest(message_type):
                data = fill() if kind is K.FILL else {
                        'SchwabOrderID': '<ORDER_ID>',
                        'BaseEvent': {'EventType': message_type}}
                self.assertIs(kind, read_account_activity(
                        item(message_type, data)).kind)

    @no_duplicates
    def test_the_subscription_ack(self):
        event = read_account_activity(
                {'seq': 2, 'key': '<UUID>', 'ACCOUNT': '',
                 'MESSAGE_TYPE': 'SUBSCRIBED', 'MESSAGE_DATA': ''})
        self.assertIs(K.SUBSCRIBED, event.kind)
        self.assertEqual(2, event.seq)

    @no_duplicates
    def test_a_cancel_accepted_is_not_the_end(self):
        event = read_account_activity(item('CancelAccepted',
                                           cancel_accepted()))
        self.assertIs(K.CANCEL_ACCEPTED, event.kind)
        self.assertEqual(('ClientCancel', 'LegOpen'),
                         (event.cancel.cancel_type, event.cancel.leg_status))

    @no_duplicates
    def test_an_out(self):
        event = read_account_activity(item('OrderUROutCompleted', out()))
        self.assertIs(K.OUT, event.kind)
        self.assertEqual(decimal.Decimal(0), event.cancel.leaves_quantity)
        self.assertEqual(decimal.Decimal(1), event.cancel.cancel_quantity)
        self.assertEqual('ClientCancel', event.cancel.cancel_type)

    @no_duplicates
    def test_a_notice(self):
        event = read_account_activity(
                {'seq': 9, 'key': 'Account Activity', 'ACCOUNT': 'a|b|c',
                 'MESSAGE_TYPE': '', 'MESSAGE_DATA': 'Feature not supported'})
        self.assertIs(K.NOTICE, event.kind)
        self.assertEqual('Feature not supported', event.notice)

    @no_duplicates
    def test_what_cannot_be_read_is_unknown_and_says_why(self):
        deep = {}
        node = deep
        for _ in range(aa.MAX_DEPTH + 5):
            node['n'] = {}
            node = node['n']
        wide = {'SchwabOrderID': '<ORDER_ID>',
                'x': list(range(aa.MAX_NODES + 5))}
        for name, given, problem in (
                ('unlabelled', {'1': 'OrderFill'}, 'unlabelled'),
                ('not JSON', item('OrderFillCompleted', '{not json'),
                 'unparsable_message_data'),
                ('too deep', item('OrderFillCompleted', deep), 'too_large'),
                ('too wide', item('OrderFillCompleted', wide), 'too_large')):
            with self.subTest(name):
                event = read_account_activity(given)
                self.assertIs(K.UNKNOWN, event.kind)
                self.assertEqual(problem, event.problem)

    @no_duplicates
    def test_it_never_raises_on_what_an_item_holds(self):
        for name, data in (
                ('a bad decimal', fill(execution={'lo': 'x', 'signScale': 99})),
                ('a list for a block', {'BaseEvent': {
                    'OrderFillCompletedEventOrderLegQuantityInfo': [1, 2]}}),
                ('an int for a type', 8),
                ('nested lists', {'BaseEvent': [[[[]]]]})):
            with self.subTest(name):
                given = item('OrderFillCompleted', data)
                read_account_activity(given)
        for odd in ({'MESSAGE_TYPE': 7, 'MESSAGE_DATA': {'a': 1}},
                    {'MESSAGE_TYPE': 'OrderFillCompleted',
                     'MESSAGE_DATA': 3.5},
                    {}):
            read_account_activity(odd)
        with self.assertRaises(TypeError):
            read_account_activity('not an item')

    @no_duplicates
    def test_text_with_a_type_is_not_a_notice(self):
        for message_type in ('OrderCreated', None, 5):
            with self.subTest(message_type=message_type):
                given = {'seq': 1, 'MESSAGE_DATA': 'some text'}
                if message_type is not None:
                    given['MESSAGE_TYPE'] = message_type
                self.assertIs(K.UNKNOWN, read_account_activity(given).kind)

    @no_duplicates
    def test_numbers_that_are_not_ints_are_none(self):
        for seq in (3.0, '9' * 5000, True):
            with self.subTest(seq=str(seq)[:10]):
                given = item('OrderFillCompleted', fill())
                given['seq'] = seq
                event = read_account_activity(given)
                self.assertIs(K.FILL, event.kind)
                self.assertIsNone(event.seq)

    @no_duplicates
    def test_a_long_payload_is_bounded(self):
        node = data = {}
        for i in range(60):
            # 60 keys of 20,000 characters: past MAX_TEXT.
            node['k%d' % i + 'x' * 20000] = {}
            node = node['k%d' % i + 'x' * 20000]
        node['leaves'] = [{'Symbol': 'X'} for _ in range(300)]
        event = read_account_activity(item('OrderFillCompleted', data))
        self.assertIs(K.UNKNOWN, event.kind)
        self.assertEqual('too_large', event.problem)
        many = fill()
        many['more'] = [{'Symbol': 'S%d' % i} for i in range(100)]
        event = read_account_activity(item('OrderFillCompleted', many))
        self.assertEqual(aa.MAX_SYMBOLS, len(event.symbols))
        self.assertIsInstance(event.symbols[0][0], tuple)

    @no_duplicates
    def test_a_mapping_that_cannot_be_read_is_unknown(self):
        class Broken(collections.abc.Mapping):
            def __getitem__(self, key):
                raise KeyError(key)

            def __len__(self):
                return 1

            def __iter__(self):
                raise RuntimeError('cannot iterate')
        event = read_account_activity(Broken())
        self.assertIs(K.UNKNOWN, event.kind)
        self.assertEqual('unreadable', event.problem)

    @no_duplicates
    def test_any_mapping_is_read_and_the_event_hashes(self):
        import types
        given = types.MappingProxyType(item('OrderFillCompleted', fill()))
        event = read_account_activity(given)
        self.assertIs(K.FILL, event.kind)
        hash(event)

    @no_duplicates
    def test_it_does_not_log(self):
        with self.assertNoLogs(level=logging.DEBUG):
            for given in (item('OrderFillCompleted', fill()),
                          item('OrderFillCompleted', fill(price={
                              'lo': '1', 'SignScale': 12})),
                          item('SomethingNew', {}), {'1': 'x'}):
                read_account_activity(given)

    @no_duplicates
    def test_a_decimal_with_an_unknown_key_is_none(self):
        event = read_account_activity(item('OrderFillCompleted', fill(
                price={'lo': '1', 'SignScale': 12})))
        self.assertIs(K.FILL, event.kind)
        self.assertIsNone(event.execution.execution_price)

    @no_duplicates
    def test_a_long_text_value_is_bounded(self):
        data = fill()
        data['note'] = 'x' * (aa.MAX_TEXT + 1)
        event = read_account_activity(item('OrderFillCompleted', data))
        self.assertEqual('too_large', event.problem)


class IdsAndSymbolsTest(unittest.TestCase):

    @no_duplicates
    def test_the_order_id_is_the_top_level_one_only(self):
        nested = fill(order_id=None)
        del nested['SchwabOrderID']
        nested['BaseEvent']['QuoteOnOrderAcceptance'] = {
                'SchwabOrderID': '<NESTED>'}
        self.assertIsNone(read_account_activity(
                item('OrderFillCompleted', nested)).order_id,
                'a nested copy was read as the order id')
        self.assertEqual('12345', read_account_activity(
                item('OrderFillCompleted', fill(order_id=12345))).order_id)
        ending = fill()
        del ending['SchwabOrderID']
        ending['ParentSchwabOrderID'] = '<PARENT>'
        self.assertIsNone(read_account_activity(
                item('OrderFillCompleted', ending)).order_id,
                'a key that only ends in SchwabOrderID was read as the id')

    @no_duplicates
    def test_a_fill_holding_two_legs_symbols_names_none(self):
        data = fill()
        block = data['BaseEvent']['OrderFillCompletedEventOrderLegQuantityInfo']
        del block['Symbol']
        block['Legs'] = [{'Symbol': 'AAPL_C200'}, {'Symbol': 'AAPL_C210'}]
        event = read_account_activity(item('OrderFillCompleted', data))
        self.assertIsNone(event.symbol, "a fill took one leg's symbol of two")

    @no_duplicates
    def test_a_quotes_symbol_is_not_the_events(self):
        data = fill()
        block = data['BaseEvent']['OrderFillCompletedEventOrderLegQuantityInfo']
        del block['Symbol']
        block['QuoteOnOrderAcceptance'] = {'Symbol': 'QUOTED'}
        event = read_account_activity(item('OrderFillCompleted', data))
        self.assertIsNone(event.symbol, "a quote's symbol was the event's")

    @no_duplicates
    def test_an_order_created_names_its_legs_symbol_side_and_asset(self):
        event = read_account_activity(item('OrderCreated', order_created()))
        self.assertEqual(('F', 'Buy', 'Equity'),
                         (event.symbol, event.side, event.asset_type))
        paths = [path for path, _ in event.symbols]
        self.assertEqual(4, len(paths))
        self.assertTrue(any('QuoteOnOrderAcceptance' in p for p in paths))

    @no_duplicates
    def test_a_symbol_that_is_not_text_is_none(self):
        for bad in ({'nested': 'F'}, 7, ''):
            with self.subTest(bad=bad):
                event = read_account_activity(item(
                        'OrderFillCompleted', fill(extra_block={
                            'Symbol': bad})))
                self.assertIsNone(event.symbol)

    @no_duplicates
    def test_related_order_ids(self):
        data = fill(extra_block={'ParentSchwabOrderID': '<PARENT>',
                                 'LifecycleSchwabOrderID': 777})
        event = read_account_activity(item('OrderFillCompleted', data))
        self.assertEqual({'ParentSchwabOrderID': '<PARENT>',
                          'LifecycleSchwabOrderID': '777'},
                         event.related_order_ids)

    @no_duplicates
    def test_its_repr_leaves_out_the_account_and_the_data(self):
        given = item('OrderFillCompleted', fill())
        given['ACCOUNT'] = 'ACCT-SECRET-123'
        given['key'] = 'KEY-SECRET-456'
        event = read_account_activity(given)
        for text in (repr(event), str(event)):
            self.assertNotIn('ACCT-SECRET-123', text)
            self.assertNotIn('KEY-SECRET-456', text)
            self.assertNotIn('AccountNumber', text)
        self.assertIn("kind='fill'", repr(event))
