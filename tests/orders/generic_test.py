import datetime
import decimal
import os
import fractions
import httpx2
import unittest

from schwaby.orders.generic import *
from schwaby.orders.common import *
from ..utils import has_diff, no_duplicates


class OrderBuilderTest(unittest.TestCase):

    def setUp(self):
        self.maxDiff = None
        self.order_builder = OrderBuilder()

    ##########################################################################
    # Session

    @no_duplicates
    def test_session_success(self):
        self.order_builder.set_session(Session.NORMAL)
        self.assertFalse(has_diff({
            'session': 'NORMAL'
        }, self.order_builder.build()))

        self.order_builder.clear_session()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_session_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.Session.NORMAL'):
            self.order_builder.set_session('NORMAL')

    @no_duplicates
    def test_session_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)
        self.order_builder.set_session('NORMAL')
        self.assertFalse(has_diff({
            'session': 'NORMAL'
        }, self.order_builder.build()))

    ##########################################################################
    # Duration

    @no_duplicates
    def test_duration_success(self):
        self.order_builder.set_duration(Duration.DAY)
        self.assertFalse(has_diff({
            'duration': 'DAY'
        }, self.order_builder.build()))

        self.order_builder.clear_duration()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_duration_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.Duration.DAY'):
            self.order_builder.set_duration('DAY')

    @no_duplicates
    def test_duration_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)
        self.order_builder.set_duration('DAY')
        self.assertFalse(has_diff({
            'duration': 'DAY'
        }, self.order_builder.build()))

    ##########################################################################
    # Cancel time

    @no_duplicates
    def test_cancel_time_is_sent_in_utc(self):
        # 16:00 in New York in June is 20:00 UTC. A non-UTC zone, so a builder
        # which formatted the wall clock without converting it would send
        # 16:00:00Z -- four hours early -- and this would catch it.
        new_york = datetime.timezone(datetime.timedelta(hours=-4))
        self.order_builder.set_cancel_time(
                datetime.datetime(2027, 6, 18, 16, 0, 0, tzinfo=new_york))
        self.assertFalse(has_diff({
            'cancelTime': '2027-06-18T20:00:00Z'
        }, self.order_builder.build()))

        self.order_builder.clear_cancel_time()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_cancel_time_accepts_a_datetime_under_the_class_patch(self):
        # Tests elsewhere replace datetime.datetime with a subclass to fix the
        # current time, and the replacement lands on the module itself. A type
        # check reading the name at call time would then refuse a real
        # datetime, so the check reads the class captured at import.
        from unittest.mock import patch

        class Patched(datetime.datetime):
            pass

        aware = datetime.datetime(2027, 6, 18, 20, 0, 0,
                                  tzinfo=datetime.timezone.utc)
        with patch.object(datetime, 'datetime', Patched):
            self.order_builder.set_cancel_time(aware)
        self.assertFalse(has_diff({
            'cancelTime': '2027-06-18T20:00:00Z'
        }, self.order_builder.build()))

    @no_duplicates
    def test_cancel_time_refuses_a_naive_datetime(self):
        with self.assertRaisesRegex(ValueError, 'must carry a timezone'):
            self.order_builder.set_cancel_time(
                    datetime.datetime(2027, 6, 18, 16, 0, 0))
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_cancel_time_refuses_a_tzinfo_that_gives_no_offset(self):
        # Set, but naive by Python's definition: utcoffset() is None. Checked
        # on tzinfo alone this would pass, and the UTC conversion would then
        # read it as the host's local time.
        class NoOffset(datetime.tzinfo):
            def utcoffset(self, dt):
                return None

            def dst(self, dt):
                return None

        with self.assertRaisesRegex(ValueError, 'must carry a timezone'):
            self.order_builder.set_cancel_time(
                    datetime.datetime(2027, 6, 18, 16, 0, 0,
                                      tzinfo=NoOffset()))

    @no_duplicates
    def test_cancel_time_refuses_a_date(self):
        with self.assertRaisesRegex(
                ValueError, "must be a datetime.datetime.*'datetime.date'"):
            self.order_builder.set_cancel_time(datetime.date(2027, 6, 18))

    @no_duplicates
    def test_cancel_time_refuses_a_string(self):
        with self.assertRaisesRegex(
                ValueError, "must be a datetime.datetime.*'builtins.str'"):
            self.order_builder.set_cancel_time('2027-06-18T20:00:00Z')

    ##########################################################################
    # OrderType

    @no_duplicates
    def test_order_type_success(self):
        self.order_builder.set_order_type(OrderType.MARKET)
        self.assertFalse(has_diff({
            'orderType': 'MARKET'
        }, self.order_builder.build()))

        self.order_builder.clear_order_type()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_order_type_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.OrderType.MARKET'):
            self.order_builder.set_order_type('MARKET')

    @no_duplicates
    def test_order_type_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)
        self.order_builder.set_order_type('MARKET')
        self.assertFalse(has_diff({
            'orderType': 'MARKET'
        }, self.order_builder.build()))

    ##########################################################################
    # ComplexOrderStrategyType

    @no_duplicates
    def test_complex_order_strategy_type_success(self):
        self.order_builder.set_complex_order_strategy_type(
            ComplexOrderStrategyType.IRON_CONDOR)
        self.assertFalse(has_diff({
            'complexOrderStrategyType': 'IRON_CONDOR'
        }, self.order_builder.build()))

        self.order_builder.clear_complex_order_strategy_type()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test__wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 
                'schwaby.orders.common.ComplexOrderStrategyType.IRON_CONDOR'):
            self.order_builder.set_complex_order_strategy_type('IRON_CONDOR')

    @no_duplicates
    def test__wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)
        self.order_builder.set_complex_order_strategy_type('IRON_CONDOR')
        self.assertFalse(has_diff({
            'complexOrderStrategyType': 'IRON_CONDOR'
        }, self.order_builder.build()))

    ##########################################################################
    # Quantity

    @no_duplicates
    def test_quantity_success(self):
        self.order_builder.set_quantity(12)
        self.assertFalse(has_diff({
            'quantity': 12
        }, self.order_builder.build()))

        self.order_builder.clear_quantity()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_quantity_negative(self):
        with self.assertRaises(ValueError):
            self.order_builder.set_quantity(-12)

    @no_duplicates
    def test_quantity_zero(self):
        with self.assertRaises(ValueError):
            self.order_builder.set_quantity(0)

    ##########################################################################
    # RequestedDestination

    @no_duplicates
    def test_requested_destination_success(self):
        self.order_builder.set_requested_destination(Destination.INET)
        self.assertFalse(has_diff({
            'requestedDestination': 'INET'
        }, self.order_builder.build()))

        self.order_builder.clear_requested_destination()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_requested_destination_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.Destination.INET'):
            self.order_builder.set_requested_destination('INET')

    @no_duplicates
    def test_requested_destination_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)
        self.order_builder.set_requested_destination('INET')
        self.assertFalse(has_diff({
            'requestedDestination': 'INET'
        }, self.order_builder.build()))

    @no_duplicates
    def test_the_venue_enum_lands_on_the_field_schwab_enumerates(self):
        # Until 3.0.0 the Destination enum was applied to destinationLinkName,
        # which Schwab's schema types as a free string; requestedDestination is
        # the field whose enumeration is exactly these twelve values. An order
        # asking for NYSE therefore carried the venue in a field that does not
        # select one, and nothing rejected it -- the request was well-formed,
        # so the only symptom was routing you did not choose.
        built = (OrderBuilder()
                 .set_requested_destination(Destination.NYSE)
                 .build())
        self.assertEqual({'requestedDestination': 'NYSE'}, built)
        self.assertNotIn('destinationLinkName', built)

    ##########################################################################
    # DestinationLinkName

    @no_duplicates
    def test_destination_link_name_takes_a_free_string(self):
        # Schwab's schema says `destinationLinkName: string`, with no
        # enumeration. Validating it against Destination refused every legal
        # value and accepted only twelve that belong to another field.
        self.order_builder.set_destination_link_name('anything at all')
        self.assertFalse(has_diff({
            'destinationLinkName': 'anything at all'
        }, self.order_builder.build()))

        self.order_builder.clear_destination_link_name()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    ##########################################################################
    # TaxLotMethod

    @no_duplicates
    def test_tax_lot_method_success(self):
        # The enum was exported from schwaby.orders.common from the beginning
        # and had no setter, so the field could not be sent at all. It matters
        # on a closing order: FIFO and LIFO realise different gains.
        self.order_builder.set_tax_lot_method(TaxLotMethod.LIFO)
        self.assertFalse(has_diff({
            'taxLotMethod': 'LIFO'
        }, self.order_builder.build()))

        self.order_builder.clear_tax_lot_method()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_tax_lot_method_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.TaxLotMethod.FIFO'):
            self.order_builder.set_tax_lot_method('FIFO')

    @no_duplicates
    def test_every_tax_lot_method_schwab_lists_is_available(self):
        # Schwab's order schema enumerates these seven. An enum that advertises
        # values the venue rejects, or omits ones it accepts, is the defect
        # this asserts against -- it has happened here before.
        self.assertEqual(
                ['AVERAGE_COST', 'FIFO', 'HIGH_COST', 'LIFO', 'LOSS_HARVESTER',
                 'LOW_COST', 'SPECIFIC_LOT'],
                sorted(m.value for m in TaxLotMethod))

    ##########################################################################
    # StopPrice

    @no_duplicates
    def test_stop_price_success(self):
        self.order_builder.set_stop_price('42.90')
        self.assertFalse(has_diff({
            'stopPrice': '42.90'
        }, self.order_builder.build()))

        self.order_builder.clear_stop_price()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_stop_price_as_string(self):
        self.order_builder.set_stop_price('invalid')
        self.assertFalse(has_diff({
            'stopPrice': 'invalid'
        }, self.order_builder.build()))

    @no_duplicates
    def test_stop_price_negative(self):
        self.order_builder.set_stop_price('-1.31')
        self.assertFalse(has_diff({
            'stopPrice': '-1.31'
        }, self.order_builder.build()))

    @no_duplicates
    def test_stop_price_zero(self):
        self.order_builder.set_stop_price('0.00')
        self.assertFalse(has_diff({
            'stopPrice': '0.00'
        }, self.order_builder.build()))

    @no_duplicates
    def test_stop_price_precision_is_not_second_guessed(self):
        # Not truncated to two places: whatever precision the caller wrote is
        # what Schwab is asked for.
        for value in ('1.99999', '2.00001'):
            builder = OrderBuilder()
            builder.set_stop_price(value)
            self.assertFalse(has_diff({'stopPrice': value}, builder.build()))

    @no_duplicates
    def test_copy_stop_price(self):
        self.order_builder.copy_stop_price(2.00001)
        self.assertFalse(has_diff({
            'stopPrice': 2.00001
        }, self.order_builder.build()))

    @no_duplicates
    def test_copy_stop_price_hopelessly_invalid(self):
        self.order_builder.copy_stop_price(['hopelessly invalid'])
        self.assertFalse(has_diff({
            'stopPrice': ['hopelessly invalid']
        }, self.order_builder.build()))

    ##########################################################################
    # StopPriceLinkBasis

    @no_duplicates
    def test_stop_price_link_basis_success(self):
        self.order_builder.set_stop_price_link_basis(StopPriceLinkBasis.ASK)
        self.assertFalse(has_diff({
            'stopPriceLinkBasis': 'ASK'
        }, self.order_builder.build()))

        self.order_builder.clear_stop_price_link_basis()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_stop_price_link_basis_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.StopPriceLinkBasis.ASK'):
            self.order_builder.set_stop_price_link_basis('ASK')

    @no_duplicates
    def test_stop_price_link_basis_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)
        self.order_builder.set_stop_price_link_basis('ASK')
        self.assertFalse(has_diff({
            'stopPriceLinkBasis': 'ASK'
        }, self.order_builder.build()))

    ##########################################################################
    # StopPriceLinkType

    @no_duplicates
    def test_stop_price_link_type_success(self):
        self.order_builder.set_stop_price_link_type(StopPriceLinkType.VALUE)
        self.assertFalse(has_diff({
            'stopPriceLinkType': 'VALUE'
        }, self.order_builder.build()))

        self.order_builder.clear_stop_price_link_type()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_stop_price_link_type_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.StopPriceLinkType.VALUE'):
            self.order_builder.set_stop_price_link_type('VALUE')

    @no_duplicates
    def test_stop_price_link_type_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)
        self.order_builder.set_stop_price_link_type('VALUE')
        self.assertFalse(has_diff({
            'stopPriceLinkType': 'VALUE'
        }, self.order_builder.build()))

    ##########################################################################
    # StopPriceOffset

    @no_duplicates
    def test_stop_price_offset_success(self):
        self.order_builder.set_stop_price_offset(12.98)
        self.assertFalse(has_diff({
            'stopPriceOffset': 12.98
        }, self.order_builder.build()))

        self.order_builder.clear_stop_price_offset()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    ##########################################################################
    # StopType

    @no_duplicates
    def test_stop_type_success(self):
        self.order_builder.set_stop_type(StopType.MARK)
        self.assertFalse(has_diff({
            'stopType': 'MARK'
        }, self.order_builder.build()))

        self.order_builder.clear_stop_type()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_stop_type_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.StopType.MARK'):
            self.order_builder.set_stop_type('MARK')

    @no_duplicates
    def test_stop_type_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)
        self.order_builder.set_stop_type('MARK')
        self.assertFalse(has_diff({
            'stopType': 'MARK'
        }, self.order_builder.build()))

    ##########################################################################
    # PriceLinkBasis

    @no_duplicates
    def test_price_link_basis_success(self):
        self.order_builder.set_price_link_basis(PriceLinkBasis.AVERAGE)
        self.assertFalse(has_diff({
            'priceLinkBasis': 'AVERAGE'
        }, self.order_builder.build()))

        self.order_builder.clear_price_link_basis()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_price_link_basis_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.PriceLinkBasis.AVERAGE'):
            self.order_builder.set_price_link_basis('AVERAGE')

    @no_duplicates
    def test_price_link_basis_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)
        self.order_builder.set_price_link_basis('AVERAGE')
        self.assertFalse(has_diff({
            'priceLinkBasis': 'AVERAGE'
        }, self.order_builder.build()))

    ##########################################################################
    # PriceLinkType

    @no_duplicates
    def test_price_link_type_success(self):
        self.order_builder.set_price_link_type(PriceLinkType.PERCENT)
        self.assertFalse(has_diff({
            'priceLinkType': 'PERCENT'
        }, self.order_builder.build()))

        self.order_builder.clear_price_link_type()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_price_link_type_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.PriceLinkType.PERCENT'):
            self.order_builder.set_price_link_type('PERCENT')

    @no_duplicates
    def test_price_link_type_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)
        self.order_builder.set_price_link_type('PERCENT')
        self.assertFalse(has_diff({
            'priceLinkType': 'PERCENT'
        }, self.order_builder.build()))

    ##########################################################################
    # PriceOffset

    @no_duplicates
    def test_price_offset_success(self):
        self.order_builder.set_price_offset(12.98)
        self.assertFalse(has_diff({
            'priceOffset': 12.98
        }, self.order_builder.build()))

        self.order_builder.clear_price_offset()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_price_offset_completes_the_price_link_trio(self):
        # The stop-price side already had basis, type and offset. The
        # price-linked side had a basis and a type but no offset, so a
        # price-linked order could not actually be expressed.
        self.order_builder.set_price_link_basis(PriceLinkBasis.BASE)
        self.order_builder.set_price_link_type(PriceLinkType.VALUE)
        self.order_builder.set_price_offset(1.5)
        self.assertFalse(has_diff({
            'priceLinkBasis': 'BASE',
            'priceLinkType': 'VALUE',
            'priceOffset': 1.5,
        }, self.order_builder.build()))

    ##########################################################################
    # Price

    @no_duplicates
    def test_price_success(self):
        self.order_builder.set_price('23.49')
        self.assertFalse(has_diff({
            'price': '23.49'
        }, self.order_builder.build()))

        self.order_builder.clear_price()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_price_success_as_string(self):
        self.order_builder.set_price('invalid')
        self.assertFalse(has_diff({
            'price': 'invalid'
        }, self.order_builder.build()))

    @no_duplicates
    def test_price_negative(self):
        self.order_builder.set_price('-1.23')
        self.assertFalse(has_diff({
            'price': '-1.23'
        }, self.order_builder.build()))

    @no_duplicates
    def test_price_zero(self):
        self.order_builder.set_price('0.00')
        self.assertFalse(has_diff({
            'price': '0.00'
        }, self.order_builder.build()))

    @no_duplicates
    def test_price_precision_is_not_second_guessed(self):
        # See test_stop_price_precision_is_not_second_guessed.
        for value in ('19.9999999', '20.00000001'):
            builder = OrderBuilder()
            builder.set_price(value)
            self.assertFalse(has_diff({'price': value}, builder.build()))

    @no_duplicates
    def test_copy_price(self):
        self.order_builder.copy_price(19.9999999)
        self.assertFalse(has_diff({
            'price': 19.9999999
        }, self.order_builder.build()))

    @no_duplicates
    def test_copy_price_hopelessly_invalid(self):
        self.order_builder.copy_price(['hopelessly invalid'])
        self.assertFalse(has_diff({
            'price': ['hopelessly invalid']
        }, self.order_builder.build()))

    ##########################################################################
    # ActivationPrice

    @no_duplicates
    def test_activation_price_success(self):
        self.order_builder.set_activation_price(54.03)
        self.assertFalse(has_diff({
            'activationPrice': 54.03
        }, self.order_builder.build()))

        self.order_builder.clear_activation_price()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_activation_price_negative(self):
        with self.assertRaises(ValueError):
            self.order_builder.set_activation_price(-3.94)

    @no_duplicates
    def test_activation_price_zero(self):
        with self.assertRaises(ValueError):
            self.order_builder.set_activation_price(0.0)

    ##########################################################################
    # SpecialInstruction

    @no_duplicates
    def test_special_instruction_success(self):
        self.order_builder.set_special_instruction(
            SpecialInstruction.DO_NOT_REDUCE)
        self.assertFalse(has_diff({
            'specialInstruction': 'DO_NOT_REDUCE'
        }, self.order_builder.build()))

        self.order_builder.clear_special_instruction()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_special_instruction_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError,
                'schwaby.orders.common.SpecialInstruction.DO_NOT_REDUCE'):
            self.order_builder.set_special_instruction('DO_NOT_REDUCE')

    @no_duplicates
    def test_special_instruction_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)
        self.order_builder.set_special_instruction('DO_NOT_REDUCE')
        self.assertFalse(has_diff({
            'specialInstruction': 'DO_NOT_REDUCE'
        }, self.order_builder.build()))

    ##########################################################################
    # OrderStrategyType

    @no_duplicates
    def test_order_strategy_type_success(self):
        self.order_builder.set_order_strategy_type(OrderStrategyType.OCO)
        self.assertFalse(has_diff({
            'orderStrategyType': 'OCO'
        }, self.order_builder.build()))

        self.order_builder.clear_order_strategy_type()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_order_strategy_type_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.OrderStrategyType.OCO'):
            self.order_builder.set_order_strategy_type('OCO')

    @no_duplicates
    def test_order_strategy_type_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)
        self.order_builder.set_order_strategy_type('OCO')
        self.assertFalse(has_diff({
            'orderStrategyType': 'OCO'
        }, self.order_builder.build()))

    ##########################################################################
    # ChildOrderStrategies

    @no_duplicates
    def test_add_child_order_strategy_success(self):
        self.order_builder.add_child_order_strategy(
            OrderBuilder().set_session(Session.NORMAL))
        self.assertFalse(has_diff({
            'childOrderStrategies': [{'session': 'NORMAL'}]
        }, self.order_builder.build()))

        self.order_builder.clear_child_order_strategies()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_add_child_order_strategy_dict(self):
        self.order_builder.add_child_order_strategy(
            {'session': 'NORMAL'})
        self.assertFalse(has_diff({
            'childOrderStrategies': [{'session': 'NORMAL'}]
        }, self.order_builder.build()))

    @no_duplicates
    def test_add_child_order_strategy_invalid_type(self):
        with self.assertRaises(ValueError):
            self.order_builder.add_child_order_strategy(10)

    @no_duplicates
    def test_add_child_order_strategy_httpx2_response(self):
        with self.assertRaisesRegex(
                ValueError, 'Child order cannot be a response'):
            self.order_builder.add_child_order_strategy(httpx2.Response(200))

    ##########################################################################
    # OrderLegCollection

    @no_duplicates
    def test_add_equity_leg_success(self):
        self.order_builder.add_equity_leg(EquityInstruction.BUY, 'GOOG', 10)
        self.order_builder.add_equity_leg(
            EquityInstruction.SELL_SHORT, 'MSFT', 1)
        self.assertFalse(has_diff({
            'orderLegCollection': [{
                'instruction': 'BUY',
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY'
                },
                'quantity': 10,
            }, {
                'instruction': 'SELL_SHORT',
                'instrument': {
                    'symbol': 'MSFT',
                    'assetType': 'EQUITY'
                },
                'quantity': 1,
            }]
        }, self.order_builder.build()))

        self.order_builder.clear_order_legs()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_add_equity_leg_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.EquityInstruction.BUY'):
            self.order_builder.add_equity_leg('BUY', 'GOOG', 10)

    @no_duplicates
    def test_add_equity_leg_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)

        self.order_builder.add_equity_leg('BUY', 'GOOG', 10)
        self.order_builder.add_equity_leg('SELL_TO_CLOSE', 'MSFT', 1)

        self.assertFalse(has_diff({
            'orderLegCollection': [{
                'instruction': 'BUY',
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY'
                },
                'quantity': 10,
            }, {
                'instruction': 'SELL_TO_CLOSE',
                'instrument': {
                    'symbol': 'MSFT',
                    'assetType': 'EQUITY'
                },
                'quantity': 1,
            }]
        }, self.order_builder.build()))

    @no_duplicates
    def test_add_equity_leg_negative_quantity(self):
        with self.assertRaises(ValueError):
            self.order_builder.add_equity_leg(
                EquityInstruction.BUY, 'GOOG', -1)

    @no_duplicates
    def test_add_equity_leg_zero_quantity(self):
        with self.assertRaises(ValueError):
            self.order_builder.add_equity_leg(
                EquityInstruction.BUY, 'GOOG', 0)

    @no_duplicates
    def test_add_option_leg_success(self):
        self.order_builder.add_option_leg(
            OptionInstruction.BUY_TO_OPEN, 'GOOG31433C1342', 10)
        self.order_builder.add_option_leg(
            OptionInstruction.BUY_TO_CLOSE, 'MSFT439132P35', 1)
        self.assertFalse(has_diff({
            'orderLegCollection': [{
                'instruction': 'BUY_TO_OPEN',
                'instrument': {
                    'symbol': 'GOOG31433C1342',
                    'assetType': 'OPTION'
                },
                'quantity': 10,
            }, {
                'instruction': 'BUY_TO_CLOSE',
                'instrument': {
                    'symbol': 'MSFT439132P35',
                    'assetType': 'OPTION'
                },
                'quantity': 1,
            }]
        }, self.order_builder.build()))

        self.order_builder.clear_order_legs()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_add_option_leg_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.OptionInstruction.BUY_TO_OPEN'):
            self.order_builder.add_option_leg(
                'BUY_TO_OPEN', 'GOOG31433C1342', 10)

    @no_duplicates
    def test_add_option_leg_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)

        self.order_builder.add_option_leg('BUY_TO_OPEN', 'GOOG31433C1342', 10)
        self.order_builder.add_option_leg('BUY_TO_CLOSE', 'MSFT439132P35', 1)
        self.assertFalse(has_diff({
            'orderLegCollection': [{
                'instruction': 'BUY_TO_OPEN',
                'instrument': {
                    'symbol': 'GOOG31433C1342',
                    'assetType': 'OPTION'
                },
                'quantity': 10,
            }, {
                'instruction': 'BUY_TO_CLOSE',
                'instrument': {
                    'symbol': 'MSFT439132P35',
                    'assetType': 'OPTION'
                },
                'quantity': 1,
            }]
        }, self.order_builder.build()))

        self.order_builder.clear_order_legs()
        self.assertFalse(has_diff({}, self.order_builder.build()))

    @no_duplicates
    def test_add_option_leg_negative_quantity(self):
        with self.assertRaises(ValueError):
            self.order_builder.add_option_leg(
                OptionInstruction.BUY_TO_OPEN, 'GOOG31433C1342', -1)

    @no_duplicates
    def test_add_option_leg_zero_quantity(self):
        with self.assertRaises(ValueError):
            self.order_builder.add_option_leg(
                OptionInstruction.BUY_TO_OPEN, 'GOOG31433C1342', 0)

    ##########################################################################
    # Leg quantityType

    @no_duplicates
    def test_add_equity_leg_quantity_type(self):
        self.order_builder.add_equity_leg(
            EquityInstruction.BUY, 'GOOG', 250,
            quantity_type=QuantityType.DOLLARS)
        self.assertFalse(has_diff({
            'orderLegCollection': [{
                'instruction': 'BUY',
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY'
                },
                'quantity': 250,
                'quantityType': 'DOLLARS',
            }]
        }, self.order_builder.build()))

    @no_duplicates
    def test_add_option_leg_quantity_type(self):
        # Tests that the value is passed through, not that DOLLARS means
        # anything on an option leg -- which is not established.
        self.order_builder.add_option_leg(
            OptionInstruction.SELL_TO_CLOSE, 'GOOG31433C1342', 3,
            quantity_type=QuantityType.DOLLARS)
        legs = self.order_builder.build()['orderLegCollection']
        self.assertEqual('DOLLARS', legs[0]['quantityType'])

    @no_duplicates
    def test_a_leg_without_a_quantity_type_carries_none(self):
        self.order_builder.add_equity_leg(EquityInstruction.BUY, 'GOOG', 10)
        self.order_builder.add_option_leg(
            OptionInstruction.BUY_TO_OPEN, 'GOOG31433C1342', 1)
        legs = self.order_builder.build()['orderLegCollection']
        # Both legs are there, so the loop below cannot pass over an empty or
        # truncated list.
        self.assertEqual(2, len(legs))
        for leg in legs:
            self.assertNotIn('quantityType', leg)

    @no_duplicates
    def test_add_equity_leg_quantity_type_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.QuantityType.DOLLARS'):
            self.order_builder.add_equity_leg(
                EquityInstruction.BUY, 'GOOG', 250, quantity_type='DOLLARS')

    @no_duplicates
    def test_add_option_leg_quantity_type_wrong_type(self):
        with self.assertRaisesRegex(
                ValueError, 'schwaby.orders.common.QuantityType.DOLLARS'):
            self.order_builder.add_option_leg(
                OptionInstruction.BUY_TO_OPEN, 'GOOG31433C1342', 1,
                quantity_type='DOLLARS')

    @no_duplicates
    def test_quantity_type_wrong_type_no_check(self):
        self.order_builder = OrderBuilder(enforce_enums=False)
        self.order_builder.add_equity_leg(
            'BUY', 'GOOG', 250, quantity_type='DOLLARS')
        legs = self.order_builder.build()['orderLegCollection']
        self.assertEqual('DOLLARS', legs[0]['quantityType'])

    @no_duplicates
    def test_quantity_is_still_required_whatever_the_quantity_type(self):
        # The builder's quantity validation does not know about quantityType,
        # so ALL_SHARES still needs a positive quantity, which is sent. What
        # Schwab does with it is not established; this pins that the builder
        # neither drops nor relaxes it, which the docs say. Every member, so a
        # relaxation for one of them alone cannot pass.
        for quantity_type in QuantityType:
            with self.subTest(quantity_type):
                builder = OrderBuilder()
                with self.assertRaisesRegex(
                        ValueError, 'quantity must be positive'):
                    builder.add_equity_leg(
                        EquityInstruction.SELL, 'GOOG', 0,
                        quantity_type=quantity_type)
                builder.add_equity_leg(
                    EquityInstruction.SELL, 'GOOG', 1,
                    quantity_type=quantity_type)
                leg = builder.build()['orderLegCollection'][0]
                self.assertEqual(
                    (1, quantity_type.value),
                    (leg['quantity'], leg['quantityType']))

    @no_duplicates
    def test_every_quantity_type_schwab_lists_is_available(self):
        # Schwab's order schema enumerates these three on each
        # orderLegCollection entry.
        self.assertEqual(
                ['ALL_SHARES', 'DOLLARS', 'SHARES'],
                sorted(m.value for m in QuantityType))


class QuantityValidationTest(unittest.TestCase):
    """Two tests that were dead, and wrong, for at least a release.

    This class was called `OrderBuilderExamplesTest`, the same name as the one
    below it, which shadowed it -- so neither of these ran. `no_duplicates`
    could not see it: it keys on `__qualname__`, and the two qualnames were
    identical.

    Neither survived being woken up as written. `test_quantity_negative` used
    `self.order_builder`, which this class's `setUp` never created, so it
    raised AttributeError rather than exercising anything. And
    `test_quantity_wrong_type_no_check` asserted that
    `OrderBuilder(enforce_enums=False).set_quantity('')` builds
    `{'quantity': ''}` -- measured against the released v4.0.0, that already
    raised TypeError, so the test documented a contract the library had
    stopped honouring before anyone renamed the package.

    That is the shape worth keeping: a test can be dead *and* stale, and the
    duplicate name hid a check that would otherwise have failed during the
    change it was relevant to.
    """

    def setUp(self):
        self.maxDiff = None
        self.order_builder = OrderBuilder()

    @no_duplicates
    def test_quantity_negative(self):
        with self.assertRaises(ValueError):
            self.order_builder.set_quantity(-12)

    @no_duplicates
    def test_enforce_enums_false_does_not_disable_the_numeric_checks(self):
        # Replaces `test_quantity_wrong_type_no_check`, which asserted the
        # opposite. `enforce_enums` governs enum arguments; `_assert_finite`
        # never consulted it, so a builder with enforcement off still refuses
        # a quantity that is not a number.
        builder = OrderBuilder(enforce_enums=False)

        with self.assertRaises(ValueError):
            builder.set_quantity('')
        with self.assertRaises(ValueError):
            builder.set_quantity(None)

        # And still accepts one that is, so the assertions above are about
        # the value rather than about enforcement being off.
        builder.set_quantity(7)
        self.assertEqual({'quantity': 7}, builder.build())


class OrderBuilderExamplesTest(unittest.TestCase):

    @no_duplicates
    def setUp(self):
        self.maxDiff = None

    ##########################################################################
    # Functional tests from here:
    # Adapted from the order samples in the retired TDAmeritrade docs.

    @no_duplicates
    def test_buy_market_stock(self):
        builder = (
            OrderBuilder()
            .set_order_type(OrderType.MARKET)
            .set_session(Session.NORMAL)
            .set_duration(Duration.DAY)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_equity_leg(EquityInstruction.BUY, 'XYZ', 15))

        expected = {
            'orderType': 'MARKET',
            'session': 'NORMAL',
            'duration': 'DAY',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [
                {
                    'instruction': 'BUY',  # The original says 'Buy'
                    'quantity': 15,
                    'instrument': {
                        'symbol': 'XYZ',
                        'assetType': 'EQUITY'
                    }
                }
            ]
        }

        self.assertFalse(has_diff(expected, builder.build()))

    @no_duplicates
    def test_buy_limit_single_option(self):
        builder = (
            OrderBuilder()
            .set_complex_order_strategy_type(ComplexOrderStrategyType.NONE)
            .set_order_type(OrderType.LIMIT)
            .set_session(Session.NORMAL)
            .set_price('6.45')
            .set_duration(Duration.DAY)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(OptionInstruction.BUY_TO_OPEN, 'XYZ_032015C49', 10))

        expected = {
            'complexOrderStrategyType': 'NONE',
            'orderType': 'LIMIT',
            'session': 'NORMAL',
            'price': '6.45',
            'duration': 'DAY',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [
                {
                    'instruction': 'BUY_TO_OPEN',
                    'quantity': 10,
                    'instrument': {
                        'symbol': 'XYZ_032015C49',
                        'assetType': 'OPTION'
                    }
                }
            ]
        }

        self.assertFalse(has_diff(expected, builder.build()))

    @no_duplicates
    def test_buy_limit_vertical_call_spread(self):
        builder = (
            OrderBuilder()
            .set_order_type(OrderType.NET_DEBIT)
            .set_session(Session.NORMAL)
            .set_price('1.20')
            .set_duration(Duration.DAY)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_option_leg(OptionInstruction.BUY_TO_OPEN, 'XYZ_011516C40', 10)
            .add_option_leg(
                OptionInstruction.SELL_TO_OPEN, 'XYZ_011516C42.5', 10))

        expected = {
            'orderType': 'NET_DEBIT',
            'session': 'NORMAL',
            'price': '1.20',
            'duration': 'DAY',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [
                {
                    'instruction': 'BUY_TO_OPEN',
                    'quantity': 10,
                    'instrument': {
                        'symbol': 'XYZ_011516C40',
                        'assetType': 'OPTION'
                    }
                },
                {
                    'instruction': 'SELL_TO_OPEN',
                    'quantity': 10,
                    'instrument': {
                        'symbol': 'XYZ_011516C42.5',
                        'assetType': 'OPTION'
                    }
                }
            ]
        }

        self.assertFalse(has_diff(expected, builder.build()))

    @no_duplicates
    def test_custom_option_spread(self):
        builder = (
            OrderBuilder()
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .set_order_type(OrderType.MARKET)
            .add_option_leg(OptionInstruction.SELL_TO_OPEN, 'XYZ_011819P45', 1)
            .add_option_leg(OptionInstruction.BUY_TO_OPEN, 'XYZ_011720P43', 2)
            .set_complex_order_strategy_type(ComplexOrderStrategyType.CUSTOM)
            .set_duration(Duration.DAY)
            .set_session(Session.NORMAL))

        expected = {
            'orderStrategyType': 'SINGLE',
            'orderType': 'MARKET',
            'orderLegCollection': [
                {
                    'instrument': {
                        'assetType': 'OPTION',
                        'symbol': 'XYZ_011819P45'
                    },
                    'instruction': 'SELL_TO_OPEN',
                    'quantity': 1
                },
                {
                    'instrument': {
                        'assetType': 'OPTION',
                        'symbol': 'XYZ_011720P43'
                    },
                    'instruction': 'BUY_TO_OPEN',
                    'quantity': 2
                }
            ],
            'complexOrderStrategyType': 'CUSTOM',
            'duration': 'DAY',
            'session': 'NORMAL'
        }

        self.assertFalse(has_diff(expected, builder.build()))

    @no_duplicates
    def test_conditional_order_one_triggers_another(self):
        builder = (
            OrderBuilder()
            .set_order_type(OrderType.LIMIT)
            .set_session(Session.NORMAL)
            .set_price('34.97')
            .set_duration(Duration.DAY)
            .set_order_strategy_type(OrderStrategyType.TRIGGER)
            .add_equity_leg(EquityInstruction.BUY, 'XYZ', 10)
            .add_child_order_strategy(
                OrderBuilder()
                .set_order_type(OrderType.LIMIT)
                .set_session(Session.NORMAL)
                .set_price('42.03')
                .set_duration(Duration.DAY)
                .set_order_strategy_type(OrderStrategyType.SINGLE)
                .add_equity_leg(EquityInstruction.SELL, 'XYZ', 10)))

        expected = {
            'orderType': 'LIMIT',
            'session': 'NORMAL',
            'price': '34.97',
            'duration': 'DAY',
            'orderStrategyType': 'TRIGGER',
            'orderLegCollection': [
                {
                    'instruction': 'BUY',
                    'quantity': 10,
                    'instrument': {
                        'symbol': 'XYZ',
                        'assetType': 'EQUITY'
                    }
                }
            ],
            'childOrderStrategies': [
                {
                    'orderType': 'LIMIT',
                    'session': 'NORMAL',
                    'price': '42.03',
                    'duration': 'DAY',
                    'orderStrategyType': 'SINGLE',
                    'orderLegCollection': [
                        {
                            'instruction': 'SELL',
                            'quantity': 10,
                            'instrument': {
                                'symbol': 'XYZ',
                                'assetType': 'EQUITY'
                            }
                        }
                    ]
                }
            ]
        }

        self.assertFalse(has_diff(expected, builder.build()))

    @no_duplicates
    def test_conditional_order_one_cancels_another(self):
        builder = (
            OrderBuilder()
            .set_order_strategy_type(OrderStrategyType.OCO)
            .add_child_order_strategy(
                OrderBuilder()
                .set_order_type(OrderType.LIMIT)
                .set_session(Session.NORMAL)
                .set_price('45.97')
                .set_duration(Duration.DAY)
                .set_order_strategy_type(OrderStrategyType.SINGLE)
                .add_equity_leg(EquityInstruction.SELL, 'XYZ', 2)
            )
            .add_child_order_strategy(
                OrderBuilder()
                .set_order_type(OrderType.STOP_LIMIT)
                .set_session(Session.NORMAL)
                .set_price('37.00')
                .set_stop_price('37.03')
                .set_duration(Duration.DAY)
                .set_order_strategy_type(OrderStrategyType.SINGLE)
                .add_equity_leg(EquityInstruction.SELL, 'XYZ', 2)))

        expected = {
            'orderStrategyType': 'OCO',
            'childOrderStrategies': [
                {
                    'orderType': 'LIMIT',
                    'session': 'NORMAL',
                    'price': '45.97',
                    'duration': 'DAY',
                    'orderStrategyType': 'SINGLE',
                    'orderLegCollection': [
                        {
                            'instruction': 'SELL',
                            'quantity': 2,
                            'instrument': {
                                'symbol': 'XYZ',
                                'assetType': 'EQUITY'
                            }
                        }
                    ]
                },
                {
                    'orderType': 'STOP_LIMIT',
                    'session': 'NORMAL',
                    'price': '37.00',
                    'stopPrice': '37.03',
                    'duration': 'DAY',
                    'orderStrategyType': 'SINGLE',
                    'orderLegCollection': [
                        {
                            'instruction': 'SELL',
                            'quantity': 2,
                            'instrument': {
                                'symbol': 'XYZ',
                                'assetType': 'EQUITY'
                            }
                        }
                    ]
                }
            ]
        }

        self.assertFalse(has_diff(expected, builder.build()))

    @no_duplicates
    def test_conditional_order_one_triggers_a_one_cancels_other(self):
        builder = (
            OrderBuilder()
            .set_order_strategy_type(OrderStrategyType.TRIGGER)
            .set_session(Session.NORMAL)
            .set_duration(Duration.DAY)
            .set_order_type(OrderType.LIMIT)
            .set_price('14.97')
            .add_equity_leg(EquityInstruction.BUY, 'XYZ', 5)
            .add_child_order_strategy(
                OrderBuilder()
                .set_order_strategy_type(OrderStrategyType.OCO)
                .add_child_order_strategy(
                    OrderBuilder()
                    .set_order_strategy_type(OrderStrategyType.SINGLE)
                    .set_session(Session.NORMAL)
                    .set_duration(Duration.GOOD_TILL_CANCEL)
                    .set_order_type(OrderType.LIMIT)
                    .set_price('15.27')
                    .add_equity_leg(EquityInstruction.SELL, 'XYZ', 5))
                .add_child_order_strategy(
                    OrderBuilder()
                    .set_order_strategy_type(OrderStrategyType.SINGLE)
                    .set_session(Session.NORMAL)
                    .set_duration(Duration.GOOD_TILL_CANCEL)
                    .set_order_type(OrderType.STOP)
                    .set_stop_price('11.27')
                    .add_equity_leg(EquityInstruction.SELL, 'XYZ', 5))))

        expected = {
            'orderStrategyType': 'TRIGGER',
            'session': 'NORMAL',
            'duration': 'DAY',
            'orderType': 'LIMIT',
            'price': '14.97',
            'orderLegCollection': [
                {
                    'instruction': 'BUY',
                    'quantity': 5,
                    'instrument': {
                        'assetType': 'EQUITY',
                        'symbol': 'XYZ'
                    }
                }
            ],
            'childOrderStrategies': [
                {
                    'orderStrategyType': 'OCO',
                    'childOrderStrategies': [
                        {
                            'orderStrategyType': 'SINGLE',
                            'session': 'NORMAL',
                            'duration': 'GOOD_TILL_CANCEL',
                            'orderType': 'LIMIT',
                            'price': '15.27',
                            'orderLegCollection': [
                                {
                                    'instruction': 'SELL',
                                    'quantity': 5,
                                    'instrument': {
                                        'assetType': 'EQUITY',
                                        'symbol': 'XYZ'
                                    }
                                }
                            ]
                        },
                        {
                            'orderStrategyType': 'SINGLE',
                            'session': 'NORMAL',
                            'duration': 'GOOD_TILL_CANCEL',
                            'orderType': 'STOP',
                            'stopPrice': '11.27',
                            'orderLegCollection': [
                                {
                                    'instruction': 'SELL',
                                    'quantity': 5,
                                    'instrument': {
                                        'assetType': 'EQUITY',
                                        'symbol': 'XYZ'
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        self.assertFalse(has_diff(expected, builder.build()))

    @no_duplicates
    def test_sell_trailing_stop_stock(self):
        builder = (
            OrderBuilder()
            .set_complex_order_strategy_type(ComplexOrderStrategyType.NONE)
            .set_order_type(OrderType.TRAILING_STOP)
            .set_session(Session.NORMAL)
            .set_stop_price_link_basis(StopPriceLinkBasis.BID)
            .set_stop_price_link_type(StopPriceLinkType.VALUE)
            .set_stop_price_offset(10)
            .set_duration(Duration.DAY)
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_equity_leg(EquityInstruction.SELL, 'XYZ', 10))

        expected = {
            'complexOrderStrategyType': 'NONE',
            'orderType': 'TRAILING_STOP',
            'session': 'NORMAL',
            'stopPriceLinkBasis': 'BID',
            'stopPriceLinkType': 'VALUE',
            'stopPriceOffset': 10,
            'duration': 'DAY',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [
                {
                    'instruction': 'SELL',
                    'quantity': 10,
                    'instrument': {
                        'symbol': 'XYZ',
                        'assetType': 'EQUITY'
                    }
                }
            ]
        }

        self.assertFalse(has_diff(expected, builder.build()))


class PricesAreStringsTest(unittest.TestCase):
    '''Prices are strings or `Decimal`, never floats, and are not converted
    here. Converting a float means choosing a rounding on the caller's behalf
    for a value denominated in money -- and truncating its binary value sends
    roughly one in twenty cent-granular prices a tick low, silently. The
    caller knows whether their price should round up, down or to the nearest
    tick, and this cannot.'''

    def setUp(self):
        self.order_builder = OrderBuilder()

    @no_duplicates
    def test_a_float_price_is_refused(self):
        for setter in ('set_price', 'set_stop_price'):
            for value in (12.34, 0, 199, 8.2):
                builder = OrderBuilder()
                with self.assertRaises(ValueError, msg='{}({!r})'.format(
                        setter, value)) as cm:
                    getattr(builder, setter)(value)
                self.assertIn('must be a string', str(cm.exception))

    @no_duplicates
    def test_the_error_says_what_to_do_instead(self):
        # A caller hitting this is mid-migration and wants the fix, not a
        # restatement of the type.
        with self.assertRaises(ValueError) as cm:
            self.order_builder.set_price(8.2)
        message = str(cm.exception)
        self.assertIn('format', message)
        self.assertIn('copy_price', message)

    @no_duplicates
    def test_a_string_price_is_stored_exactly_as_given(self):
        # The whole point: what the caller wrote is what Schwab receives. 8.2
        # is the value the old binary truncation turned into '8.19'.
        for value in ('8.20', '8.2', '0.1869', '199.99', '0.00'):
            builder = OrderBuilder()
            builder.set_price(value)
            self.assertEqual(value, builder.build()['price'])

    @no_duplicates
    def test_a_decimal_price_is_accepted_exactly(self):
        # Decimal is the type that avoids the float problem rather than hiding
        # it, so it is taken as given -- rendering it decides nothing.
        for value, expected in (('19.99', '19.99'), ('0.1869', '0.1869'),
                                ('-5.00', '-5.00'), ('0.00', '0.00')):
            builder = OrderBuilder()
            builder.set_price(decimal.Decimal(value))
            self.assertEqual(expected, builder.build()['price'])

    @no_duplicates
    def test_a_decimal_never_renders_in_scientific_notation(self):
        # str(Decimal('1E+2')) is '1E+2', which is not a price. format(d, 'f')
        # is what keeps that out of an order.
        builder = OrderBuilder()
        builder.set_price(decimal.Decimal('1E+2'))
        self.assertEqual('100', builder.build()['price'])

    @no_duplicates
    def test_a_decimal_with_real_price_precision_is_kept(self):
        # The guard must not catch prices anyone actually sends. Schwab's
        # sub-penny prices and option strikes stop at four places.
        for value in ('19.99', '0.1869', '1234.5678', '0.00012345', '100'):
            builder = OrderBuilder()
            builder.set_price(decimal.Decimal(value))
            self.assertEqual(value, builder.build()['price'])

    @no_duplicates
    def test_a_non_finite_decimal_is_refused_including_the_signalling_nan(self):
        # NaN and the infinities have a non-integer Decimal exponent, so they
        # skip the decimal-places guard. format() renders them as text and
        # _assert_finite reads a signalling NaN as "not a number at all" and
        # lets it past, so unchecked, Decimal('sNaN') would reach an order as
        # the string 'sNaN'. So it is checked explicitly.
        for spelling in ('NaN', 'sNaN', 'Infinity', '-Infinity'):
            for setter in ('set_price', 'set_stop_price'):
                builder = OrderBuilder()
                with self.assertRaises(ValueError, msg=spelling) as cm:
                    getattr(builder, setter)(decimal.Decimal(spelling))
                self.assertIn('finite', str(cm.exception))

    @no_duplicates
    def test_copy_price_accepts_a_decimal(self):
        # copy_* skip validation, so a Decimal reaches _build_object raw. It
        # has no __dict__, so without a branch there it fell through to vars()
        # and raised `vars() argument must have __dict__ attribute` at build()
        # time -- far from the call, naming neither field nor type.
        for setter, field in (('copy_price', 'price'),
                              ('copy_stop_price', 'stopPrice')):
            builder = OrderBuilder()
            getattr(builder, setter)(decimal.Decimal('19.99'))
            self.assertEqual('19.99', builder.build()[field])

    @no_duplicates
    def test_copy_price_refuses_a_non_finite_decimal(self):
        # copy_* validate nothing by contract, but rendering a non-finite
        # Decimal produces the well-formed string "NaN", which serializes and
        # transmits. The float escape hatch is not equivalent: a bare NaN is
        # invalid JSON that json.dumps refuses, so it cannot quietly become an
        # order. This one could.
        for spelling in ('NaN', 'sNaN', 'Infinity', '-Infinity'):
            for setter in ('copy_price', 'copy_stop_price'):
                builder = OrderBuilder()
                with self.assertRaises(ValueError, msg=spelling):
                    getattr(builder, setter)(decimal.Decimal(spelling))

    @no_duplicates
    def test_a_decimal_in_a_numeric_field_is_refused_at_the_setter(self):
        # Refused where the field is still known. Rejecting it at build()
        # named the value but not which of several numeric fields held it.
        for setter in ('set_quantity', 'set_stop_price_offset',
                       'set_activation_price', 'set_price_offset'):
            builder = OrderBuilder()
            with self.assertRaises(ValueError, msg=setter) as cm:
                getattr(builder, setter)(decimal.Decimal('10'))
            self.assertIn('number in Schwab', str(cm.exception))

    @no_duplicates
    def test_the_builder_refuses_a_decimal_it_cannot_serialize(self):
        # The setters catch this first, so this exercises the backstop
        # directly. It exists because a Decimal has no __dict__: without a
        # branch for it, the object builder falls to its reflection path and
        # raises `vars() argument must have __dict__ attribute`, naming
        # neither the field nor the type. Rendering every Decimal as a string
        # instead was worse -- Schwab types quantity, stopPriceOffset and
        # activationPrice as numbers, so they went out as "10" and "0.50".
        builder = OrderBuilder()
        builder._quantity = decimal.Decimal('10')
        with self.assertRaises(ValueError) as cm:
            builder.build()
        self.assertIn('Only prices', str(cm.exception))

    @no_duplicates
    def test_no_setter_lets_a_non_finite_decimal_through(self):
        # _assert_finite read float(Decimal('sNaN')) raising ValueError as
        # "not a number at all" and returned, so every numeric setter passed a
        # signalling NaN to a `<= 0` comparison that raised a bare
        # decimal.InvalidOperation -- the failure it runs first to prevent.
        for setter in ('set_price', 'set_stop_price', 'set_quantity',
                       'set_activation_price', 'set_stop_price_offset',
                       'set_price_offset'):
            for spelling in ('sNaN', 'NaN', 'Infinity', '-Infinity'):
                builder = OrderBuilder()
                with self.assertRaises(
                        ValueError, msg='{}({})'.format(setter, spelling)):
                    getattr(builder, setter)(decimal.Decimal(spelling))

    @no_duplicates
    def test_decimal_depth_is_not_policed(self):
        # A guard on decimal places was tried and removed. Schwab types both
        # price fields number($double), so a long spelling parses to the same
        # double a short one does -- unreadable, not wrong -- and no digit
        # count separates a float's expansion from honest arithmetic: measured
        # over realistic inputs, float-derived values span 0 to 53 places and
        # string arithmetic spans 1 to 9. Policing depth therefore refused
        # valid computed limits to prevent a merely long payload.
        cases = (
            # an ordinary computed limit: a 0.025% offset on a four-place quote
            decimal.Decimal('123.4567') * decimal.Decimal('1.00025'),
            # two four-place factors, which leaves trailing zeros
            decimal.Decimal('100.00') * decimal.Decimal('1.0500')
            * decimal.Decimal('1.0500'),
            # the float expansion the guard existed to catch
            decimal.Decimal(0.1),
        )

        for value in cases:
            builder = OrderBuilder()
            builder.set_price(value)
            sent = builder.build()['price']
            self.assertEqual(
                    float(value), float(sent),
                    '{} was altered on the way through'.format(value))

    @no_duplicates
    def test_copy_stop_price_names_its_own_field(self):
        # Both copy_ setters share a renderer. It hardcoded 'price', so an
        # order setting both told the caller the wrong one had been rejected.
        with self.assertRaises(ValueError) as cm:
            OrderBuilder().copy_stop_price(decimal.Decimal('NaN'))
        self.assertIn('stop price', str(cm.exception))

    @no_duplicates
    def test_the_error_does_not_recommend_a_rounding_conversion(self):
        # '{:.2f}'.format rounds where the removed conversion truncated, so
        # recommending it as a drop-in would move prices by a tick. The message
        # has to say so rather than suggest it bare.
        with self.assertRaises(ValueError) as cm:
            OrderBuilder().set_price(8.2)
        self.assertIn('rounds', str(cm.exception))
        self.assertIn('truncate', str(cm.exception))

    @no_duplicates
    def test_copy_price_still_takes_anything(self):
        # The documented escape hatch, for rebuilding a historical order
        # exactly as the venue reported it.
        self.order_builder.copy_price(12.34)
        self.assertEqual(12.34, self.order_builder.build()['price'])


class NonFinitePriceTest(unittest.TestCase):
    '''A price names a finite number of currency units. NaN and the infinities
    do not, and they arrive by computation rather than by typing -- dividing by
    a size that turned out to be zero, or deriving a limit from a quote that
    was missing. The builder already refuses a non-positive quantity, so
    refusing these is the same check applied to the other half of the order.'''

    def setUp(self):
        self.order_builder = OrderBuilder()

    @no_duplicates
    def test_non_finite_price_is_refused_whichever_check_catches_it(self):
        # As a float it is refused for being a float; as a string it is refused
        # for not naming a finite number of dollars. Neither reaches an order.
        for setter in ('set_price', 'set_stop_price'):
            for value in (float('nan'), float('inf'), float('-inf'),
                          'nan', 'inf', '-inf'):
                builder = OrderBuilder()
                with self.assertRaises(ValueError, msg='{}({!r})'.format(
                        setter, value)):
                    getattr(builder, setter)(value)

    @no_duplicates
    def test_non_finite_price_as_string_rejected(self):
        # str(float('nan')) is 'nan', so passing a computed price through str()
        # -- the documented way to pass prices -- reaches here just as easily.
        for value in ('nan', 'NaN', 'inf', '-inf', 'Infinity'):
            with self.assertRaises(ValueError) as cm:
                self.order_builder.set_price(value)
            self.assertIn('finite', str(cm.exception))

    @no_duplicates
    def test_ordinary_string_prices_still_pass_through_untouched(self):
        for value in ('0.07', '421.35', '1.0050', '-5.00', '0'):
            self.order_builder.set_price(value)
            self.assertEqual(value, self.order_builder.build()['price'])

    @no_duplicates
    def test_non_numeric_strings_are_still_the_callers_business(self):
        # This library does not validate that a price is well formed; Schwab
        # rejects what it does not like. Only values which are definitely not
        # a number of dollars are refused here.
        self.order_builder.set_price('abc')
        self.assertEqual('abc', self.order_builder.build()['price'])

    @no_duplicates
    def test_every_numeric_setter_refuses_non_finite(self):
        # A price is not the only number in an order which can arrive by
        # computation. A trailing stop offset derived from a quote that came
        # back empty reaches set_stop_price_offset the same way.
        # set_price and set_stop_price take strings, so a non-finite float
        # trips the type check first; they are swept as strings instead.
        numeric_setters = ('set_activation_price', 'set_stop_price_offset',
                           'set_price_offset', 'set_quantity')
        string_setters = ('set_price', 'set_stop_price')

        for name in numeric_setters:
            for value in (float('nan'), float('inf'), float('-inf')):
                builder = OrderBuilder()
                with self.assertRaises(ValueError, msg='{}({!r})'.format(
                        name, value)) as cm:
                    getattr(builder, name)(value)
                self.assertIn('finite', str(cm.exception))

        for name in string_setters:
            for value in ('nan', 'inf', '-inf'):
                builder = OrderBuilder()
                with self.assertRaises(ValueError, msg='{}({!r})'.format(
                        name, value)) as cm:
                    getattr(builder, name)(value)
                self.assertIn('finite', str(cm.exception))

    @no_duplicates
    def test_nan_defeats_the_positivity_guards(self):
        # NaN compares False against everything, so `quantity <= 0` and
        # `activation_price <= 0.0` do not fire for it. The finite check has to
        # run first or those guards are simply bypassed.
        self.assertFalse(float('nan') <= 0)

        for name in ('set_quantity', 'set_activation_price'):
            builder = OrderBuilder()
            with self.assertRaises(ValueError):
                getattr(builder, name)(float('nan'))

    @no_duplicates
    def test_a_built_order_is_always_serializable(self):
        # json.dumps emits a bare NaN, which is not valid JSON, so an order
        # carrying one is not merely wrong -- it cannot be transmitted as JSON
        # at all without a parser willing to accept a non-standard token.
        import json

        builder = OrderBuilder()
        for name in ('set_quantity', 'set_activation_price',
                     'set_stop_price_offset'):
            try:
                getattr(builder, name)(float('nan'))
            except ValueError:
                pass

        encoded = json.dumps(builder.build(), allow_nan=False)
        self.assertNotIn('NaN', encoded)

    @no_duplicates
    def test_ordinary_values_still_pass_through(self):
        builder = OrderBuilder()
        builder.set_quantity(10)
        builder.set_activation_price(42.35)
        builder.set_stop_price_offset(1.5)
        built = builder.build()
        self.assertEqual(10, built['quantity'])
        self.assertEqual(42.35, built['activationPrice'])
        self.assertEqual(1.5, built['stopPriceOffset'])

    @no_duplicates
    def test_copy_price_still_bypasses_validation(self):
        # copy_price documents itself as skipping the validation, and callers
        # reconstructing a historical order rely on that.
        self.order_builder.copy_price(float('nan'))
        self.assertNotEqual(None, self.order_builder.build()['price'])


class NonNumericOrderFieldTest(unittest.TestCase):
    '''The numeric order fields, given something that is not a number.

    `_assert_finite` used to answer "can float() parse it" and return quietly
    when the answer was no. That is the wrong question in both directions, and
    the two directions failed differently:

    Things that are not numbers answer no. `set_quantity(None)` then reached
    `quantity <= 0` and raised a bare TypeError naming neither the field nor
    the value. The two offset setters have no comparison after the call at all,
    so they *accepted* it -- and `_build_object` drops a None, so
    `set_stop_price_offset(None)` built an order with no offset on it and said
    nothing. A trailing stop with no offset is a different order from the one
    that was asked for, placed without an error.

    And things that are not numbers answer yes. `float(b'1')` is 1.0, so a
    bytes passed and was stored as bytes; `json.dumps` refuses those, so the
    order was not wrong so much as unsendable, discovered at the point of
    sending it.
    '''

    SETTERS = ('set_quantity', 'set_stop_price_offset', 'set_price_offset',
               'set_activation_price')

    # Each of these is here for a reason a smaller list would miss:
    #
    #  * None and [] answer "no" to `float()` -- the original hole.
    #  * b'1' answers *yes*: float(b'1') is 1.0, so it passed and was stored
    #    as bytes, which json.dumps then refuses.
    #  * object() answers no to both, so a fix keyed on either question alone
    #    still has to handle it.
    #  * Fraction is a `numbers.Real`, so a guard written as
    #    `isinstance(price, numbers.Real)` accepts it -- and it then reaches
    #    `vars()` in `_build_object` and dies there. The predicate has to be
    #    the serializer's, `(int, float)`, not a mathematical one. Without
    #    this entry the two spellings are indistinguishable and the case went
    #    GREEN under mutation.
    #  * True is an `int` subclass, so it satisfies `isinstance(price,
    #    (int, float))` -- and `_build_object` passes it through, so
    #    `set_quantity(True)` built `{"quantity": true}`. The serializer's
    #    predicate is the right one to copy and this is the single type it
    #    lets through that Schwab's schema does not call a number.
    NOT_NUMBERS = (None, [], {}, b'1', object(), fractions.Fraction(3, 2),
                   True, False)

    # Strings are refused too, but for a different stated reason -- Schwab
    # types these fields as numbers, so a string is the wrong type rather than
    # a missing value -- and by a different branch. Kept separate so a fix to
    # one branch cannot make the other's assertion pass by accident.
    NOT_NUMBERS_BUT_STRINGS = ('1.5', 'abc')

    @no_duplicates
    def test_a_non_number_is_refused_and_the_message_names_the_field(self):
        for setter in self.SETTERS:
            for value in self.NOT_NUMBERS:
                with self.subTest(setter=setter, value=value):
                    builder = OrderBuilder()
                    with self.assertRaises(ValueError) as ctx:
                        getattr(builder, setter)(value)
                    # The field name is the point. A bare TypeError from the
                    # comparison two lines later says only that NoneType and
                    # int do not compare.
                    field = setter[len('set_'):].replace('_', ' ')
                    self.assertIn(field, str(ctx.exception))

    @no_duplicates
    def test_a_string_is_refused_on_a_field_schwab_types_as_a_number(self):
        # These reached `_assert_finite`'s string branch, which exists for the
        # price fields and returns quietly for anything it cannot parse. The
        # offsets have no comparison after the call, so `set_price_offset('abc')`
        # built `{"priceOffset": "abc"}` and sent it.
        for setter in self.SETTERS:
            for value in self.NOT_NUMBERS_BUT_STRINGS:
                with self.subTest(setter=setter, value=value):
                    builder = OrderBuilder()
                    with self.assertRaises(ValueError) as ctx:
                        getattr(builder, setter)(value)
                    self.assertIn('does not take a str', str(ctx.exception))
                    self.assertEqual({}, builder.build())

    @no_duplicates
    def test_the_price_fields_still_take_a_string(self):
        # The other half of the same change, and the reason `_assert_finite`
        # takes a flag rather than deciding from the value: a price field is a
        # string in Schwab's schema and a numeric field is not.
        builder = OrderBuilder()
        builder.set_price('12.34')
        builder.set_stop_price(decimal.Decimal('5.6'))
        self.assertEqual({'price': '12.34', 'stopPrice': '5.6'},
                         builder.build())

    @no_duplicates
    def test_the_offsets_do_not_silently_accept_one(self):
        # Separate from the assertion above because these two are the ones
        # that raised nothing at all: the failure was an order that built
        # cleanly with the field missing, not an exception of the wrong type.
        for setter in ('set_stop_price_offset', 'set_price_offset'):
            with self.subTest(setter=setter):
                builder = OrderBuilder()
                with self.assertRaises(ValueError):
                    getattr(builder, setter)(None)
                self.assertEqual({}, builder.build())

    @no_duplicates
    def test_numbers_are_still_accepted(self):
        # The positive control. Every assertion above passes just as well
        # against a setter that refuses everything.
        builder = OrderBuilder()
        builder.set_quantity(10)
        builder.set_stop_price_offset(1.5)
        builder.set_price_offset(2)
        builder.set_activation_price(42.35)
        self.assertEqual(
                {'quantity': 10, 'stopPriceOffset': 1.5, 'priceOffset': 2,
                 'activationPrice': 42.35},
                builder.build())

    @no_duplicates
    def test_the_guard_is_what_keeps_a_built_order_serialisable(self):
        # Building an order from two good values and checking json.dumps
        # would pass with or without the guard, over a path no bytes reach.
        #
        # The claim worth making is that the refusal is what prevents the
        # unserialisable order: b'1' would pass a `float()` check, since
        # float(b'1') is 1.0, and go in as bytes, which json.dumps refuses.
        import json
        builder = OrderBuilder()
        builder.set_quantity(10)

        with self.assertRaises(ValueError):
            builder.set_stop_price_offset(b'1')

        # Nothing was stored on the way out, so what is left still serialises.
        built = builder.build()
        self.assertEqual({'quantity': 10}, built)
        json.dumps(built, allow_nan=False)

        # And the positive control: had it been stored, this is what would
        # have happened.
        with self.assertRaises(TypeError):
            json.dumps({'stopPriceOffset': b'1'})

    @no_duplicates
    def test_the_prebuilt_templates_refuse_a_string_quantity_too(self):
        # `__add_order_leg` calls the same validator, so this reaches every
        # template in the package -- which is the most-used entry point here
        # and the place existing callers are most likely to break.
        # `equity_buy_market('AAPL', '10')` built `{"quantity": "10"}` and
        # sent a string where Schwab's schema says number.
        from schwaby.orders.equities import (
                equity_buy_market, equity_sell_limit)

        with self.assertRaises(ValueError) as ctx:
            equity_buy_market('AAPL', '10')
        self.assertIn('quantity', str(ctx.exception))

        with self.assertRaises(ValueError):
            equity_sell_limit('AAPL', '10', '25.50')

        with self.assertRaises(ValueError):
            equity_buy_market('AAPL', None)

    @no_duplicates
    def test_the_prebuilt_templates_still_take_a_number(self):
        # The control. Everything above passes against a template that refuses
        # every quantity, and the price stays a string -- that half is
        # unchanged and is the reason the two kinds of field are told apart.
        from schwaby.orders.equities import equity_sell_limit

        built = equity_sell_limit('AAPL', 10, '25.50').build()
        self.assertEqual(10, built['orderLegCollection'][0]['quantity'])
        self.assertEqual('25.50', built['price'])


class AcceptedTypesTableTest(unittest.TestCase):
    """The table in `docs/order-builder.rst`, checked against the validators.

    Time-in-force handling built from an enum, and type handling built from a
    prose sentence, can both be wrong in ways nothing raises until an order is
    placed. So the accepted types are stated per setter, in a table checked
    against the code, so it cannot quietly stop being true.

    The builder has two rules that are close to opposites -- a price field
    takes a string or a Decimal, a numeric field takes an int or a float --
    which is exactly the shape a single sentence gets wrong.

    The validator names the walk below matches are themselves sized against
    what existed when it was written. `set_cancel_time` validates through
    `_cancel_time_string`, so it was invisible here and absent from the table
    while this test asserted the table omits nothing. A fourth validator will
    need adding to that set too.
    """

    @staticmethod
    def validated_setters():
        """Every OrderBuilder setter that runs a type validator.

        Derived from the call sites rather than listed. A hardcoded tuple is
        sized against the setters that existed when it was written -- add a
        numeric one later and it is absent from this list *and* from the
        documented table, and nothing fails while the page claims to be
        complete. The docs say that table cannot quietly stop being true, so
        the list it is checked against has to come from the code.
        """
        import ast
        path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__)))),
                'schwaby', 'orders', 'generic.py')
        with open(path, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == 'OrderBuilder')
        found = []
        for item in cls.body:
            if not isinstance(item, ast.FunctionDef) \
                    or not item.name.startswith('set_'):
                continue
            called = {n.func.id for n in ast.walk(item)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            if called & {'_assert_finite', '_require_price_string',
                         '_cancel_time_string'}:
                found.append(item.name)
        return tuple(sorted(found))

    VALUES = {'str': '1.50', 'int': 7, 'float': 7.35,
              'Decimal': decimal.Decimal('1.50'), 'bool': True, 'None': None}

    @staticmethod
    def documented_table():
        """Parses the simple-table under `.. _accepted_types:` back out."""
        path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__)))), 'docs', 'order-builder.rst')
        with open(path, encoding='utf-8') as f:
            lines = f.read().split('\n')
        start = next(i for i, l in enumerate(lines)
                     if l.strip() == '.. _accepted_types:')
        # A simple-table rule is `===== ==== ====`, so it is `=` and spaces --
        # not `set(...) == {'='}`, which would match nothing because of the
        # column gaps.
        rules = [i for i, l in enumerate(lines[start:], start)
                 if l.strip() and set(l.strip()) <= {'=', ' '}]
        assert len(rules) >= 3, 'accepted-types table not found'
        header = lines[rules[0] + 1].split()
        types = [c.strip('`') for c in header[1:]]
        table = {}
        for line in lines[rules[1] + 1:rules[2]]:
            cells = line.split()
            table[cells[0].strip('`')] = dict(zip(types, cells[1:]))
        return types, table

    @no_duplicates
    def test_the_documented_table_matches_the_validators(self):
        types, documented = self.documented_table()

        # Positive control: the parse must actually have found the table.
        # The documented table must list exactly the setters that
        # validate -- neither a row for something that does not, nor a
        # silent omission of one that does.
        self.assertEqual(sorted(self.validated_setters()),
                         sorted(documented))
        self.assertEqual(sorted(self.VALUES), sorted(types))

        wrong = []
        for setter in self.validated_setters():
            for name in types:
                builder = OrderBuilder()
                try:
                    getattr(builder, setter)(self.VALUES[name])
                    actual = 'yes'
                except Exception:
                    actual = 'no'
                if documented[setter][name] != actual:
                    wrong.append('%s / %s: docs say %s, is %s'
                                 % (setter, name, documented[setter][name],
                                    actual))
        self.assertEqual([], wrong)

    @no_duplicates
    def test_the_two_rules_really_are_opposites(self):
        # The claim the prose makes above the table, asserted rather than
        # asserted-in-prose: a price field refuses what a numeric field needs
        # and the other way round. If these ever converge the sentence has to
        # go, and this is what will say so.
        _, documented = self.documented_table()
        self.assertEqual('yes', documented['set_price']['str'])
        self.assertEqual('no', documented['set_price']['float'])
        self.assertEqual('no', documented['set_quantity']['str'])
        self.assertEqual('yes', documented['set_quantity']['float'])

        # And bool is refused by every one of them -- True was accepted as a
        # quantity through 4.0.0 and became a silent one-share order.
        for setter in self.validated_setters():
            self.assertEqual('no', documented[setter]['bool'], setter)
