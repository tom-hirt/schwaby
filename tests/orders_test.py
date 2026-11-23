import unittest

from schwaby.orders.common import *
from schwaby.orders.equities import *
from .utils import has_diff, no_duplicates

from unittest.mock import patch


class BuilderTemplates(unittest.TestCase):

    def test_equity_buy_market(self):
        self.assertFalse(has_diff({
            'orderType': 'MARKET',
            'session': 'NORMAL',
            'duration': 'DAY',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_market('GOOG', 10).build()))

    def test_equity_buy_limit(self):
        self.assertFalse(has_diff({
            'orderType': 'LIMIT',
            'session': 'NORMAL',
            'duration': 'DAY',
            'price': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_limit('GOOG', 10, '199.99').build()))

    def test_equity_sell_market(self):
        self.assertFalse(has_diff({
            'orderType': 'MARKET',
            'session': 'NORMAL',
            'duration': 'DAY',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_market('GOOG', 10).build()))

    def test_equity_sell_limit(self):
        self.assertFalse(has_diff({
            'orderType': 'LIMIT',
            'session': 'NORMAL',
            'duration': 'DAY',
            'price': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_limit('GOOG', 10, '199.99').build()))

    def test_equity_sell_short_market(self):
        self.assertFalse(has_diff({
            'orderType': 'MARKET',
            'session': 'NORMAL',
            'duration': 'DAY',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL_SHORT',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_short_market('GOOG', 10).build()))

    def test_equity_sell_short_limit(self):
        self.assertFalse(has_diff({
            'orderType': 'LIMIT',
            'session': 'NORMAL',
            'duration': 'DAY',
            'price': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL_SHORT',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_short_limit('GOOG', 10, '199.99').build()))

    def test_equity_buy_to_cover_market(self):
        self.assertFalse(has_diff({
            'orderType': 'MARKET',
            'session': 'NORMAL',
            'duration': 'DAY',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY_TO_COVER',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_to_cover_market('GOOG', 10).build()))

    def test_equity_buy_to_cover_limit(self):
        self.assertFalse(has_diff({
            'orderType': 'LIMIT',
            'session': 'NORMAL',
            'duration': 'DAY',
            'price': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY_TO_COVER',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_to_cover_limit('GOOG', 10, '199.99').build()))

    def test_equity_buy_stop(self):
        self.assertFalse(has_diff({
            'orderType': 'STOP',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPrice': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_stop('GOOG', 10, '199.99').build()))

    def test_equity_buy_stop_limit(self):
        self.assertFalse(has_diff({
            'orderType': 'STOP_LIMIT',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPrice': '199.99',
            'price': '200.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_stop_limit('GOOG', 10, '199.99', '200.99').build()))

    def test_equity_buy_trailing_stop(self):
        self.assertFalse(has_diff({
            'orderType': 'TRAILING_STOP',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPriceLinkBasis': 'LAST',
            'stopPriceLinkType': 'PERCENT',
            'stopPriceOffset': 2.5,
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_trailing_stop('GOOG', 10, 2.5, StopPriceLinkType.PERCENT).build()))

    def test_equity_buy_trailing_stop_limit(self):
        self.assertFalse(has_diff({
            'orderType': 'TRAILING_STOP_LIMIT',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPriceLinkBasis': 'LAST',
            'stopPriceLinkType': 'VALUE',
            'stopPriceOffset': 2.5,
            'stopType': 'STANDARD',
            'priceLinkBasis': 'MANUAL',
            'price': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_trailing_stop_limit('GOOG', 10, 2.5, StopPriceLinkType.VALUE, '199.99').build()))

    def test_equity_buy_market_on_close(self):
        self.assertFalse(has_diff({
            'orderType': 'MARKET_ON_CLOSE',
            'session': 'NORMAL',
            'duration': 'DAY',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_market_on_close('GOOG', 10).build()))

    def test_equity_buy_limit_on_close(self):
        self.assertFalse(has_diff({
            'orderType': 'LIMIT_ON_CLOSE',
            'session': 'NORMAL',
            'duration': 'DAY',
            'price': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_limit_on_close('GOOG', 10, '199.99').build()))

    def test_equity_sell_stop(self):
        self.assertFalse(has_diff({
            'orderType': 'STOP',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPrice': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_stop('GOOG', 10, '199.99').build()))

    def test_equity_sell_stop_limit(self):
        self.assertFalse(has_diff({
            'orderType': 'STOP_LIMIT',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPrice': '199.99',
            'price': '200.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_stop_limit('GOOG', 10, '199.99', '200.99').build()))

    def test_equity_sell_trailing_stop(self):
        self.assertFalse(has_diff({
            'orderType': 'TRAILING_STOP',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPriceLinkBasis': 'LAST',
            'stopPriceLinkType': 'PERCENT',
            'stopPriceOffset': 2.5,
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_trailing_stop('GOOG', 10, 2.5, StopPriceLinkType.PERCENT).build()))

    def test_equity_sell_trailing_stop_limit(self):
        self.assertFalse(has_diff({
            'orderType': 'TRAILING_STOP_LIMIT',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPriceLinkBasis': 'LAST',
            'stopPriceLinkType': 'VALUE',
            'stopPriceOffset': 2.5,
            'stopType': 'STANDARD',
            'priceLinkBasis': 'MANUAL',
            'price': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_trailing_stop_limit('GOOG', 10, 2.5, StopPriceLinkType.VALUE, '199.99').build()))

    def test_equity_sell_market_on_close(self):
        self.assertFalse(has_diff({
            'orderType': 'MARKET_ON_CLOSE',
            'session': 'NORMAL',
            'duration': 'DAY',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_market_on_close('GOOG', 10).build()))

    def test_equity_sell_limit_on_close(self):
        self.assertFalse(has_diff({
            'orderType': 'LIMIT_ON_CLOSE',
            'session': 'NORMAL',
            'duration': 'DAY',
            'price': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_limit_on_close('GOOG', 10, '199.99').build()))

    def test_equity_sell_short_stop(self):
        self.assertFalse(has_diff({
            'orderType': 'STOP',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPrice': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL_SHORT',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_short_stop('GOOG', 10, '199.99').build()))

    def test_equity_sell_short_stop_limit(self):
        self.assertFalse(has_diff({
            'orderType': 'STOP_LIMIT',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPrice': '199.99',
            'price': '200.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL_SHORT',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_short_stop_limit('GOOG', 10, '199.99', '200.99').build()))

    def test_equity_sell_short_trailing_stop(self):
        self.assertFalse(has_diff({
            'orderType': 'TRAILING_STOP',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPriceLinkBasis': 'LAST',
            'stopPriceLinkType': 'PERCENT',
            'stopPriceOffset': 2.5,
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL_SHORT',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_short_trailing_stop('GOOG', 10, 2.5, StopPriceLinkType.PERCENT).build()))

    def test_equity_sell_short_trailing_stop_limit(self):
        self.assertFalse(has_diff({
            'orderType': 'TRAILING_STOP_LIMIT',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPriceLinkBasis': 'LAST',
            'stopPriceLinkType': 'VALUE',
            'stopPriceOffset': 2.5,
            'stopType': 'STANDARD',
            'priceLinkBasis': 'MANUAL',
            'price': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL_SHORT',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_short_trailing_stop_limit('GOOG', 10, 2.5, StopPriceLinkType.VALUE, '199.99').build()))

    def test_equity_sell_short_market_on_close(self):
        self.assertFalse(has_diff({
            'orderType': 'MARKET_ON_CLOSE',
            'session': 'NORMAL',
            'duration': 'DAY',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL_SHORT',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_short_market_on_close('GOOG', 10).build()))

    def test_equity_sell_short_limit_on_close(self):
        self.assertFalse(has_diff({
            'orderType': 'LIMIT_ON_CLOSE',
            'session': 'NORMAL',
            'duration': 'DAY',
            'price': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'SELL_SHORT',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_sell_short_limit_on_close('GOOG', 10, '199.99').build()))

    def test_equity_buy_to_cover_stop(self):
        self.assertFalse(has_diff({
            'orderType': 'STOP',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPrice': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY_TO_COVER',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_to_cover_stop('GOOG', 10, '199.99').build()))

    def test_equity_buy_to_cover_stop_limit(self):
        self.assertFalse(has_diff({
            'orderType': 'STOP_LIMIT',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPrice': '199.99',
            'price': '200.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY_TO_COVER',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_to_cover_stop_limit('GOOG', 10, '199.99', '200.99').build()))

    def test_equity_buy_to_cover_trailing_stop(self):
        self.assertFalse(has_diff({
            'orderType': 'TRAILING_STOP',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPriceLinkBasis': 'LAST',
            'stopPriceLinkType': 'PERCENT',
            'stopPriceOffset': 2.5,
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY_TO_COVER',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_to_cover_trailing_stop('GOOG', 10, 2.5, StopPriceLinkType.PERCENT).build()))

    def test_equity_buy_to_cover_trailing_stop_limit(self):
        self.assertFalse(has_diff({
            'orderType': 'TRAILING_STOP_LIMIT',
            'session': 'NORMAL',
            'duration': 'DAY',
            'stopPriceLinkBasis': 'LAST',
            'stopPriceLinkType': 'VALUE',
            'stopPriceOffset': 2.5,
            'stopType': 'STANDARD',
            'priceLinkBasis': 'MANUAL',
            'price': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY_TO_COVER',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_to_cover_trailing_stop_limit('GOOG', 10, 2.5, StopPriceLinkType.VALUE, '199.99').build()))

    def test_equity_buy_to_cover_market_on_close(self):
        self.assertFalse(has_diff({
            'orderType': 'MARKET_ON_CLOSE',
            'session': 'NORMAL',
            'duration': 'DAY',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY_TO_COVER',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_to_cover_market_on_close('GOOG', 10).build()))

    def test_equity_buy_to_cover_limit_on_close(self):
        self.assertFalse(has_diff({
            'orderType': 'LIMIT_ON_CLOSE',
            'session': 'NORMAL',
            'duration': 'DAY',
            'price': '199.99',
            'orderStrategyType': 'SINGLE',
            'orderLegCollection': [{
                'instruction': 'BUY_TO_COVER',
                'quantity': 10,
                'instrument': {
                    'symbol': 'GOOG',
                    'assetType': 'EQUITY',
                }
            }]
        }, equity_buy_to_cover_limit_on_close('GOOG', 10, '199.99').build()))
