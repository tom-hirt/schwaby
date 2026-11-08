import decimal
import asyncio
import datetime
import httpx2
import inspect
import logging
import os
from types import SimpleNamespace
import pytest
import pytz
import sys
import time
import unittest
import warnings
from unittest.mock import ANY, MagicMock, Mock, patch

from authlib.integrations.base_client.errors import OAuthError
import schwaby.client.base
from schwaby.client import AsyncClient, Client
from schwaby.orders.generic import OrderBuilder
from schwaby.utils import TokenRefreshError

from .utils import AsyncMagicMock, ResyncProxy, no_duplicates

# Constants

API_KEY = '1234567890'
ACCOUNT_ID = 100000
ACCOUNT_HASH = '0x0x0x0x10000'
ORDER_ID = 200000
SAVED_ORDER_ID = 300000
CUSIP = '000919239'
MARKET = 'EQUITY'
INDEX = '$SPX.X'
SYMBOL = 'AAPL'
TRANSACTION_ID = 400000
WATCHLIST_ID = 5000000

# The library's default start date, which is UTC. A naive value here and in
# the library would both be read as local time and agree, so the expected
# value would depend on the timezone of whatever machine ran the suite.
# Pinning it to UTC on both sides makes the assertion mean the same thing
# everywhere.
MIN_DATETIME = datetime.datetime(year=1971, month=1, day=1,
                                 tzinfo=datetime.timezone.utc)
MIN_ISO = '1971-01-01T00:00:00+0000'
MIN_TIMESTAMP_MILLIS = int(MIN_DATETIME.timestamp()) * 1000

NOW_DATETIME = datetime.datetime(2020, 1, 2, 3, 4, 5)
NOW_DATE = datetime.date(2020, 1, 2)
NOW_DATETIME_ISO = '2020-01-02T03:04:05Z'
NOW_DATETIME_TRUNCATED_ISO = '2020-01-02T00:00:00Z'
NOW_DATE_ISO = '2020-01-02'

NOW_DATETIME_MINUS_60_DAYS = NOW_DATE - datetime.timedelta(days=60)
NOW_DATETIME_MINUS_60_DAYS_ISO = '2019-11-03T03:04:05Z'

NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS = \
        int((NOW_DATETIME + datetime.timedelta(days=7)).timestamp()) * 1000


class mockdatetime(datetime.datetime):
    @classmethod
    def utcnow(cls):
        return NOW_DATETIME

    @classmethod
    def now(cls, timezone):
        return NOW_DATETIME


EARLIER_DATETIME = datetime.datetime(2001, 1, 2, 3, 4, 5,
                                     tzinfo=pytz.timezone('America/New_York'))
EARLIER_ISO = '2001-01-02T03:04:05-0456'
EARLIER_MILLIS = 978422405000
EARLIER_DATE_STR = '2001-01-02'

class _TestClient:
    """
    Test suite used for both Client and AsyncClient
    """

    def setUp(self):
        self.mock_session = self.magicmock_class()
        self.client = self.client_class(API_KEY, self.mock_session)

        # Set the logging level to DEBUG to force all lazily-evaluated messages
        # to be evaluated
        self.client.logger.setLevel('DEBUG')

    def make_url(self, path):
        path = path.format(
            accountId=ACCOUNT_ID,
            accountHash=ACCOUNT_HASH,
            orderId=ORDER_ID,
            savedOrderId=SAVED_ORDER_ID,
            cusip=CUSIP,
            market=MARKET,
            index=INDEX,
            symbol=SYMBOL,
            transactionId=TRANSACTION_ID,
            watchlistId=WATCHLIST_ID)
        return 'https://api.schwabapi.com' + path


    # Generic functionality


    def test_set_timeout(self):
        timeout = 'dummy'
        self.client.set_timeout(timeout)
        self.assertEqual(timeout, self.client.session.timeout)


    def test_token_age(self):
        token_metadata = MagicMock()
        token_metadata.token_age.return_value = 1000
        client = self.client_class(
                API_KEY, self.mock_session, token_metadata=token_metadata)

        self.assertEqual(client.token_age(), 1000)



    # get_account


    def test_get_account(self):
        self.client.get_account(ACCOUNT_HASH)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}'), params={})

    
    def test_get_account_fields(self):
        self.client.get_account(ACCOUNT_HASH, fields=[
            self.client_class.Account.Fields.POSITIONS])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}'),
            params={'fields': 'positions'})


    def test_get_account_fields_scalar(self):
        self.client.get_account(
                ACCOUNT_HASH, fields=self.client_class.Account.Fields.POSITIONS)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}'),
            params={'fields': 'positions'})


    def test_get_account_fields_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_account(ACCOUNT_HASH, fields=['positions'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}'),
            params={'fields': 'positions'})


    # get_account_numbers

    def test_get_account_numbers(self):
        self.client.get_account_numbers()
        self.mock_session.get.assert_called_with(
                self.make_url('/trader/v1/accounts/accountNumbers'), params={})


    # get_accounts


    def test_get_accounts(self):
        self.client.get_accounts()
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts'), params={})


    def test_get_accounts_fields(self):
        self.client.get_accounts(fields=[
            self.client_class.Account.Fields.POSITIONS])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts'),
            params={'fields': 'positions'})


    def test_get_accounts_fields_scalar(self):
        self.client.get_accounts(fields=self.client_class.Account.Fields.POSITIONS)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts'),
            params={'fields': 'positions'})


    def test_get_accounts_fields_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_accounts(fields=['positions'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts'),
            params={'fields': 'positions'})

    # get_order

    
    def test_get_order(self):
        self.client.get_order(ORDER_ID, ACCOUNT_HASH)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'),
            params={})

    def test_get_order_str(self):
        self.client.get_order(str(ORDER_ID), str(ACCOUNT_HASH))
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'),
            params={})

    # cancel_order

    def test_cancel_order(self):
        self.client.cancel_order(ORDER_ID, ACCOUNT_HASH)
        self.mock_session.delete.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'))

    def test_cancel_order_str(self):
        self.client.cancel_order(str(ORDER_ID), str(ACCOUNT_HASH))
        self.mock_session.delete.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'))

    # get_orders_for_account

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_vanilla(self):
        self.client.get_orders_for_account(ACCOUNT_HASH)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_from_not_datetime(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_account(
                    ACCOUNT_HASH, from_entered_datetime='2020-01-02')
        self.assertEqual(
                str(cm.exception),
                "expected type in (datetime.date, datetime.datetime) for " +
                "from_entered_datetime, got 'builtins.str'")


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_to_not_datetime(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_account(
                    ACCOUNT_HASH, to_entered_datetime='2020-01-02')
        self.assertEqual(
                str(cm.exception),
                "expected type in (datetime.date, datetime.datetime) for " +
                "to_entered_datetime, got 'builtins.str'")


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_a_wrong_type_whose_name_raises_is_still_refused_by_name(self):
        class Meta(type):
            @property
            def __name__(cls):
                raise RuntimeError('metaclass raised')

        class Odd(metaclass=Meta):
            pass

        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_account(
                    ACCOUNT_HASH, from_entered_datetime=Odd())
        self.assertIn("got 'tests.client_test.Odd'", str(cm.exception))


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_a_wrong_type_whose_module_raises_is_still_refused_by_name(self):
        class Meta(type):
            @property
            def __module__(cls):
                raise RuntimeError('metaclass raised')

        class Odd(metaclass=Meta):
            pass

        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_account(
                    ACCOUNT_HASH, from_entered_datetime=Odd())
        self.assertIn("got 'tests.client_test.Odd'", str(cm.exception))


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_max_results(self):
        self.client.get_orders_for_account(ACCOUNT_HASH, max_results=100)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO,
                'maxResults': 100,
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_from_entered_datetime(self):
        self.client.get_orders_for_account(
                ACCOUNT_HASH, from_entered_datetime=datetime.datetime(
                    year=2024, month=6, day=5, hour=4, minute=3, second=2))
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': '2024-06-05T04:03:02Z',
                'toEnteredTime': NOW_DATETIME_ISO,
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_to_entered_datetime(self):
        self.client.get_orders_for_account(
                ACCOUNT_HASH, to_entered_datetime=datetime.datetime(
                    year=2024, month=6, day=5, hour=4, minute=3, second=2))
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': '2024-06-05T04:03:02Z',
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_status(self):
        self.client.get_orders_for_account(
                ACCOUNT_HASH, status=self.client_class.Order.Status.FILLED)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO,
                'status': 'FILLED'
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_multiple_statuses(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_account(
                    ACCOUNT_HASH,
                    status=[self.client_class.Order.Status.FILLED,
                            self.client_class.Order.Status.REJECTED])
        self.assertIn(
                'expected type "Status", got type "list"',
                str(cm.exception))


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_account_status_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_orders_for_account(ACCOUNT_HASH, status='NOT_A_STATUS')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO,
                'status': 'NOT_A_STATUS'
            })


    # get_orders_for_all_linked_accounts

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_vanilla(self):
        self.client.get_orders_for_all_linked_accounts()
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_from_not_datetime(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_all_linked_accounts(
                    from_entered_datetime='2020-01-02')
        self.assertEqual(
                str(cm.exception),
                "expected type in (datetime.date, datetime.datetime) for " +
                "from_entered_datetime, got 'builtins.str'")


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_to_not_datetime(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_all_linked_accounts(
                    to_entered_datetime='2020-01-02')
        self.assertEqual(
                str(cm.exception),
                "expected type in (datetime.date, datetime.datetime) for " +
                "to_entered_datetime, got 'builtins.str'")


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_max_results(self):
        self.client.get_orders_for_all_linked_accounts(max_results=100)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO,
                'maxResults': 100,
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_from_entered_datetime(self):
        self.client.get_orders_for_all_linked_accounts(
                from_entered_datetime=datetime.datetime(
                    year=2024, month=6, day=5, hour=4, minute=3, second=2))
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/orders'), params={
                'fromEnteredTime': '2024-06-05T04:03:02Z',
                'toEnteredTime': NOW_DATETIME_ISO,
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_to_entered_datetime(self):
        self.client.get_orders_for_all_linked_accounts(
                to_entered_datetime=datetime.datetime(
                    year=2024, month=6, day=5, hour=4, minute=3, second=2))
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': '2024-06-05T04:03:02Z',
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_status(self):
        self.client.get_orders_for_all_linked_accounts(
                status=self.client_class.Order.Status.FILLED)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO,
                'status': 'FILLED'
            })


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_multiple_statuses(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_orders_for_all_linked_accounts(
                    status=[self.client_class.Order.Status.FILLED,
                            self.client_class.Order.Status.REJECTED])
        self.assertIn(
                'expected type "Status", got type "list"',
                str(cm.exception))


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_orders_for_all_linked_accounts_status_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_orders_for_all_linked_accounts(status='NOT_A_STATUS')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/orders'), params={
                'fromEnteredTime': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'toEnteredTime': NOW_DATETIME_ISO,
                'status': 'NOT_A_STATUS'
            })


    # place_order

    
    def test_place_order(self):
        order_spec = {'order': 'spec'}
        self.client.place_order(ACCOUNT_HASH, order_spec)
        self.mock_session.post.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), json=order_spec)

    
    def test_place_order_order_builder(self):
        order_spec = OrderBuilder(enforce_enums=False).set_order_type('LIMIT')
        expected_spec = {'orderType': 'LIMIT'}
        self.client.place_order(ACCOUNT_HASH, order_spec)
        self.mock_session.post.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'),
            json=expected_spec)

    
    def test_place_order_str(self):
        order_spec = {'order': 'spec'}
        self.client.place_order(str(ACCOUNT_HASH), order_spec)
        self.mock_session.post.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), json=order_spec)

    # replace_order

    
    def test_replace_order(self):
        order_spec = {'order': 'spec'}
        self.client.replace_order(ACCOUNT_HASH, ORDER_ID, order_spec)
        self.mock_session.put.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'),
            json=order_spec)

    
    def test_replace_order_order_builder(self):
        order_spec = OrderBuilder(enforce_enums=False).set_order_type('LIMIT')
        expected_spec = {'orderType': 'LIMIT'}
        self.client.replace_order(ACCOUNT_HASH, ORDER_ID, order_spec)
        self.mock_session.put.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'),
            json=expected_spec)

    
    def test_replace_order_str(self):
        order_spec = {'order': 'spec'}
        self.client.replace_order(str(ACCOUNT_HASH), str(ORDER_ID), order_spec)
        self.mock_session.put.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders/{orderId}'),
            json=order_spec)


    # preview_order

    
    def test_preview_order(self):
        order_spec = {'order': 'spec'}
        self.client.preview_order(ACCOUNT_HASH, order_spec)
        self.mock_session.post.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/previewOrder'),
            json=order_spec)

    
    def test_preview_order_order_builder(self):
        order_spec = OrderBuilder(enforce_enums=False).set_order_type('LIMIT')
        expected_spec = {'orderType': 'LIMIT'}
        self.client.preview_order(ACCOUNT_HASH, order_spec)
        self.mock_session.post.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/previewOrder'),
            json=expected_spec)

    
    # get_transactions

    
    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions(self):
        self.client.get_transactions(ACCOUNT_HASH)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': ','.join(t.value for t in self.client.Transactions.TransactionType),
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': NOW_DATETIME_ISO})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_one_type(self):
        self.client.get_transactions(
                ACCOUNT_HASH, 
                transaction_types=self.client.Transactions.TransactionType.TRADE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': 'TRADE',
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': NOW_DATETIME_ISO})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_type_list(self):
        self.client.get_transactions(
                ACCOUNT_HASH, 
                transaction_types=[
                    self.client.Transactions.TransactionType.TRADE,
                    self.client.Transactions.TransactionType.JOURNAL])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': 'TRADE,JOURNAL',
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': NOW_DATETIME_ISO})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_type_list_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_transactions(
                ACCOUNT_HASH, transaction_types=['TRADE', 'JOURNAL'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': 'TRADE,JOURNAL',
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': NOW_DATETIME_ISO})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_symbol(self):
        self.client.get_transactions(ACCOUNT_HASH, symbol='AAPL')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': ','.join(t.value for t in self.client.Transactions.TransactionType),
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': NOW_DATETIME_ISO,
                'symbol': 'AAPL'})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_symbol_start_date_as_datetime(self):
        self.client.get_transactions(
                ACCOUNT_HASH, start_date=NOW_DATETIME)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': ','.join(t.value for t in self.client.Transactions.TransactionType),
                'startDate': NOW_DATETIME_ISO,
                'endDate': NOW_DATETIME_ISO})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_symbol_start_date_as_date(self):
        self.client.get_transactions(
                ACCOUNT_HASH, start_date=NOW_DATE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': ','.join(t.value for t in self.client.Transactions.TransactionType),
                'startDate': NOW_DATETIME_TRUNCATED_ISO,
                'endDate': NOW_DATETIME_ISO})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_symbol_end_date_as_datetime(self):
        self.client.get_transactions(
                ACCOUNT_HASH,
                # NOW_DATETIME is the default, use something different
                end_date=datetime.datetime(2020, 6, 7, 8, 9, 0))
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': ','.join(t.value for t in self.client.Transactions.TransactionType),
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': '2020-06-07T08:09:00Z'})


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_transactions_symbol_end_date_as_date(self):
        self.client.get_transactions(ACCOUNT_HASH, end_date=NOW_DATE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/transactions'),
            params={
                'types': ','.join(t.value for t in self.client.Transactions.TransactionType),
                'startDate': NOW_DATETIME_MINUS_60_DAYS_ISO,
                'endDate': NOW_DATETIME_TRUNCATED_ISO})


    # get_transaction
    
    def test_get_transaction(self):
        self.client.get_transaction(ACCOUNT_HASH, TRANSACTION_ID)
        self.mock_session.get.assert_called_once_with(
            self.make_url(
                '/trader/v1/accounts/{accountHash}/transactions/{transactionId}'),
            params={})


    # get_user_preference

    def test_get_user_preferences(self):
        self.client.get_user_preferences()
        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/userPreference'), params={})


    # get_quote

    def test_get_quote(self):
        self.client.get_quote(SYMBOL)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/{symbol}/quotes'), params={})


    def test_get_quote_fields_single(self):
        self.client.get_quote(SYMBOL, fields=self.client.Quote.Fields.QUOTE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/{symbol}/quotes'),
            params={'fields': 'quote'})


    def test_get_quote_fields_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_quote(SYMBOL, fields=['not-a-field'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/{symbol}/quotes'),
            params={'fields': 'not-a-field'})


    def test_get_quote_fields_multiple(self):
        self.client.get_quote(SYMBOL, fields=[
            self.client.Quote.Fields.QUOTE,
            self.client.Quote.Fields.FUNDAMENTAL])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/{symbol}/quotes'),
            params={'fields': 'quote,fundamental'})


    # get_quotes

    def test_get_quotes(self):
        self.client.get_quotes(['AAPL', 'MSFT'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/quotes'), params={
                'symbols': 'AAPL,MSFT'})

    
    def test_get_quotes_single_symbol(self):
        self.client.get_quotes('AAPL')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/quotes'), params={
                'symbols': 'AAPL'})

    def test_get_quotes_fields(self):
        self.client.get_quotes(
                ['AAPL', 'MSFT'],
                fields=self.client.Quote.Fields.QUOTE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/quotes'), params={
                'symbols': 'AAPL,MSFT',
                'fields': 'quote'})


    def test_get_quotes_fields_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_quotes(['AAPL', 'MSFT'], fields=['quote'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/quotes'), params={
                'symbols': 'AAPL,MSFT',
                'fields': 'quote'})


    def test_get_quotes_indicative(self):
        self.client.get_quotes(['AAPL', 'MSFT'], indicative=True)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/quotes'), params={
                'symbols': 'AAPL,MSFT',
                'indicative': 'true'})


    def test_get_quotes_indicative_not_bool(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_quotes(['AAPL', 'MSFT'], indicative='false')
        self.assertEqual(str(cm.exception),
                         "value of 'indicative' must be either True or False")


    # get_price_history
    
    def test_get_price_history_vanilla(self):
        self.client.get_price_history(SYMBOL)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL})

    
    def test_get_price_history_period_type(self):
        self.client.get_price_history(
            SYMBOL, period_type=self.client_class.PriceHistory.PeriodType.MONTH)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'periodType': 'month'})

    
    def test_get_price_history_period_type_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_price_history(SYMBOL, period_type='month')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'periodType': 'month'})

    
    def test_get_price_history_num_periods(self):
        self.client.get_price_history(
            SYMBOL, period=self.client_class.PriceHistory.Period.TEN_DAYS)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'period': 10})

    
    def test_get_price_history_num_periods_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_price_history(SYMBOL, period=10)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'period': 10})

    
    def test_get_price_history_frequency_type(self):
        self.client.get_price_history(
            SYMBOL,
            frequency_type=self.client_class.PriceHistory.FrequencyType.DAILY)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'frequencyType': 'daily'})

    
    def test_get_price_history_frequency_type_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_price_history(SYMBOL, frequency_type='daily')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'frequencyType': 'daily'})

    
    def test_get_price_history_frequency(self):
        self.client.get_price_history(
        SYMBOL,
        frequency=self.client_class.PriceHistory.Frequency.EVERY_FIVE_MINUTES)
        self.mock_session.get.assert_called_once_with(
        self.make_url('/marketdata/v1/pricehistory'), params={
            'symbol': SYMBOL,
            'frequency': 5})


    def test_get_price_history_frequency_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_price_history(SYMBOL, frequency=5)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'frequency': 5})

    
    def test_get_price_history_start_datetime(self):
        self.client.get_price_history(
            SYMBOL, start_datetime=EARLIER_DATETIME)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'startDate': EARLIER_MILLIS})

    
    def test_get_price_history_start_datetime_str(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_price_history(SYMBOL, start_datetime='2020-01-01')
        self.assertEqual(str(cm.exception),
                         "expected type 'datetime.datetime' for " +
                         "start_datetime, got 'builtins.str'")

    
    def test_get_price_history_end_datetime(self):
        self.client.get_price_history(SYMBOL, end_datetime=EARLIER_DATETIME)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'endDate': EARLIER_MILLIS})

    
    def test_get_price_history_end_datetime_str(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_price_history(SYMBOL, end_datetime='2020-01-01')
        self.assertEqual(str(cm.exception),
                         "expected type 'datetime.datetime' for " +
                         "end_datetime, got 'builtins.str'")

    
    def test_get_price_history_need_extended_hours_data(self):
        self.client.get_price_history(SYMBOL, need_extended_hours_data=True)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'needExtendedHoursData': True})


    def test_get_price_history_need_previous_close(self):
        self.client.get_price_history(SYMBOL, need_previous_close=True)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'), params={
                'symbol': SYMBOL,
                'needPreviousClose': True})


    # get_option_chain
    
    def test_get_option_chain_vanilla(self):
        self.client.get_option_chain('AAPL')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL'})

    def test_get_option_chain_contract_type(self):
        self.client.get_option_chain(
            'AAPL', contract_type=self.client_class.Options.ContractType.PUT)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'contractType': 'PUT'})

    
    def test_get_option_chain_contract_type_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_option_chain('AAPL', contract_type='PUT')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'contractType': 'PUT'})

    
    def test_get_option_chain_strike_count(self):
        self.client.get_option_chain('AAPL', strike_count=100)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'strikeCount': 100})

    
    def test_get_option_chain_include_underlyingquotes(self):
        self.client.get_option_chain('AAPL', include_underlying_quote=True)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'includeUnderlyingQuote': True})

    
    def test_get_option_chain_strategy(self):
        self.client.get_option_chain(
            'AAPL', strategy=self.client_class.Options.Strategy.STRANGLE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'strategy': 'STRANGLE'})

    
    def test_get_option_chain_strategy_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_option_chain('AAPL', strategy='STRANGLE')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'strategy': 'STRANGLE'})

    
    def test_get_option_chain_interval(self):
        self.client.get_option_chain('AAPL', interval=10.0)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'interval': 10.0})

    
    def test_get_option_chain_strike(self):
        self.client.get_option_chain('AAPL', strike=123)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'strike': 123})

    
    def test_get_option_chain_strike_range(self):
        self.client.get_option_chain(
            'AAPL', strike_range=self.client_class.Options.StrikeRange.IN_THE_MONEY)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'range': 'ITM'})

    
    def test_get_option_chain_strike_range_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_option_chain('AAPL', strike_range='ITM')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'range': 'ITM'})

    
    def test_get_option_chain_from_date_datetime(self):
        self.client.get_option_chain(
            'AAPL', from_date=NOW_DATETIME)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'fromDate': NOW_DATE_ISO})

    
    def test_get_option_chain_from_date_date(self):
        self.client.get_option_chain('AAPL', from_date=NOW_DATE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'fromDate': NOW_DATE_ISO})

    
    def test_get_option_chain_from_date_str(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_option_chain('AAPL', from_date='2020-01-01')
        self.assertEqual(str(cm.exception),
                         "expected type 'datetime.date' for " +
                         "from_date, got 'builtins.str'")

    
    def test_get_option_chain_to_date_datetime(self):
        self.client.get_option_chain('AAPL', to_date=NOW_DATETIME)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'toDate': NOW_DATE_ISO})

    
    def test_get_option_chain_to_date_date(self):
        self.client.get_option_chain('AAPL', to_date=NOW_DATE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'toDate': NOW_DATE_ISO})

    
    def test_get_option_chain_to_date_str(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_option_chain('AAPL', to_date='2020-01-01')
        self.assertEqual(str(cm.exception),
                         "expected type 'datetime.date' for " +
                         "to_date, got 'builtins.str'")

    
    def test_get_option_chain_volatility(self):
        self.client.get_option_chain('AAPL', volatility=40.0)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'volatility': 40.0})

    
    def test_get_option_chain_underlying_price(self):
        self.client.get_option_chain('AAPL', underlying_price=234.0)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'underlyingPrice': 234.0})

    
    def test_get_option_chain_interest_rate(self):
        self.client.get_option_chain('AAPL', interest_rate=0.07)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'interestRate': 0.07})

    
    def test_get_option_chain_days_to_expiration(self):
        self.client.get_option_chain('AAPL', days_to_expiration=12)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'daysToExpiration': 12})

    
    def test_get_option_chain_exp_month(self):
        self.client.get_option_chain(
            'AAPL', exp_month=self.client_class.Options.ExpirationMonth.JANUARY)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'expMonth': 'JAN'})

    
    def test_get_option_chain_exp_month_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_option_chain('AAPL', exp_month='JAN')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'expMonth': 'JAN'})

    
    def test_get_option_chain_option_type(self):
        self.client.get_option_chain(
            'AAPL', option_type=self.client_class.Options.Type.STANDARD)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'optionType': 'S'})

    
    def test_get_option_chain_option_type_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_option_chain('AAPL', option_type='S')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'optionType': 'S'})


    def test_get_option_chain_option_entitlement(self):
        self.client.get_option_chain(
            'AAPL', entitlement=self.client.Options.Entitlement.PAYING_PRO)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/chains'), params={
                'symbol': 'AAPL',
                'entitlement': 'PP'})


    # get_option_expiration_chain

    def test_get_option_expiration_chain(self):
        self.client.get_option_expiration_chain('AAPL')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/expirationchain'),
            params={'symbol': 'AAPL'})


    # Schwab honours `period` and ignores the date range when both are sent, so
    # a caller who asks for a range and still gets a period's worth of data has
    # been silently ignored. get_price_history's own docstring says period
    # "should not be provided if start_datetime and end_datetime".

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_price_history_helpers_omit_period_when_given_a_range(self):
        helpers = [name for name in dir(self.client)
                   if name.startswith('get_price_history_every')]
        self.assertEqual(7, len(helpers))

        for name in helpers:
            for kwargs in ({'start_datetime': EARLIER_DATETIME},
                           {'end_datetime': EARLIER_DATETIME},
                           {'start_datetime': EARLIER_DATETIME,
                            'end_datetime': NOW_DATETIME}):
                self.mock_session.get.reset_mock()
                getattr(self.client, name)('AAPL', **kwargs)
                params = self.mock_session.get.call_args[1]['params']

                self.assertNotIn(
                        'period', params,
                        '{} sent period alongside {}'.format(
                            name, sorted(kwargs)))
                self.assertIn('startDate', params)
                self.assertIn('endDate', params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_price_history_helpers_keep_period_without_a_range(self):
        # With no range asked for, the synthesized one spans decades and is not
        # something to request. period is what should describe the request.
        helpers = [name for name in dir(self.client)
                   if name.startswith('get_price_history_every')]

        for name in helpers:
            self.mock_session.get.reset_mock()
            getattr(self.client, name)('AAPL')
            params = self.mock_session.get.call_args[1]['params']

            self.assertIn('period', params,
                          '{} dropped period with no range'.format(name))

    # get_price_history_every_minute

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_vanilla(self):
        self.client.get_price_history_every_minute('AAPL')
        params = {
                'symbol': 'AAPL',
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_start_datetime(self):
        self.client.get_price_history_every_minute(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_end_datetime(self):
        self.client.get_price_history_every_minute(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_empty_extendedhours(self):
        self.client.get_price_history_every_minute(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_extendedhours(self):
        self.client.get_price_history_every_minute(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_empty_previous_close(self):
        self.client.get_price_history_every_minute(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_minute_previous_close(self):
        self.client.get_price_history_every_minute(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_MINUTE
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)



    # get_price_history_every_five_minutes


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_vanilla(self):
        self.client.get_price_history_every_five_minutes('AAPL')
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_start_datetime(self):
        self.client.get_price_history_every_five_minutes(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_end_datetime(self):
        self.client.get_price_history_every_five_minutes(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_empty_extendedhours(self):
        self.client.get_price_history_every_five_minutes(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_extendedhours(self):
        self.client.get_price_history_every_five_minutes(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_empty_previous_close(self):
        self.client.get_price_history_every_five_minutes(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_five_minutes_previous_close(self):
        self.client.get_price_history_every_five_minutes(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIVE_MINUTES
                'frequency': 5,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    # get_price_history_every_ten_minutes


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_vanilla(self):
        self.client.get_price_history_every_ten_minutes('AAPL')
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_start_datetime(self):
        self.client.get_price_history_every_ten_minutes(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_end_datetime(self):
        self.client.get_price_history_every_ten_minutes(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_empty_extendedhours(self):
        self.client.get_price_history_every_ten_minutes(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_extendedhours(self):
        self.client.get_price_history_every_ten_minutes(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_empty_previous_close(self):
        self.client.get_price_history_every_ten_minutes(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_ten_minutes_previous_close(self):
        self.client.get_price_history_every_ten_minutes(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_TEN_MINUTES
                'frequency': 10,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    # get_price_history_every_fifteen_minutes


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_vanilla(self):
        self.client.get_price_history_every_fifteen_minutes('AAPL')
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_start_datetime(self):
        self.client.get_price_history_every_fifteen_minutes(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_end_datetime(self):
        self.client.get_price_history_every_fifteen_minutes(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_empty_extendedhours(self):
        self.client.get_price_history_every_fifteen_minutes(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_extendedhours(self):
        self.client.get_price_history_every_fifteen_minutes(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_empty_previous_close(self):
        self.client.get_price_history_every_fifteen_minutes(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_fifteen_minutes_previous_close(self):
        self.client.get_price_history_every_fifteen_minutes(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_FIFTEEN_MINUTES
                'frequency': 15,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    # get_price_history_every_thirty_minutes


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_vanilla(self):
        self.client.get_price_history_every_thirty_minutes('AAPL')
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_start_datetime(self):
        self.client.get_price_history_every_thirty_minutes(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_end_datetime(self):
        self.client.get_price_history_every_thirty_minutes(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_empty_extendedhours(self):
        self.client.get_price_history_every_thirty_minutes(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_extendedhours(self):
        self.client.get_price_history_every_thirty_minutes(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_empty_previous_close(self):
        self.client.get_price_history_every_thirty_minutes(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_thirty_minutes_previous_close(self):
        self.client.get_price_history_every_thirty_minutes(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'day',
                # ONE_DAY
                'period': 1,
                'frequencyType': 'minute',
                # EVERY_THIRTY_MINUTES
                'frequency': 30,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    # get_price_history_every_day


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_vanilla(self):
        self.client.get_price_history_every_day('AAPL')
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_start_datetime(self):
        self.client.get_price_history_every_day(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_end_datetime(self):
        self.client.get_price_history_every_day(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_empty_extendedhours(self):
        self.client.get_price_history_every_day(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_extendedhours(self):
        self.client.get_price_history_every_day(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_empty_previous_close(self):
        self.client.get_price_history_every_day(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_day_previous_close(self):
        self.client.get_price_history_every_day(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'daily',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    # get_price_history_every_week


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_vanilla(self):
        self.client.get_price_history_every_week('AAPL')
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_start_datetime(self):
        self.client.get_price_history_every_week(
                'AAPL', start_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': EARLIER_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_end_datetime(self):
        self.client.get_price_history_every_week(
                'AAPL', end_datetime=EARLIER_DATETIME)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': EARLIER_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_empty_extendedhours(self):
        self.client.get_price_history_every_week(
            'AAPL', need_extended_hours_data=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_extendedhours(self):
        self.client.get_price_history_every_week(
            'AAPL', need_extended_hours_data=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needExtendedHoursData': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)

    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_empty_previous_close(self):
        self.client.get_price_history_every_week(
            'AAPL', need_previous_close=None)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    @patch('schwaby.client.base.datetime.datetime', mockdatetime)
    def test_get_price_history_every_week_previous_close(self):
        self.client.get_price_history_every_week(
            'AAPL', need_previous_close=True)
        params = {
                'symbol': SYMBOL,
                'periodType': 'year',
                # TWENTY_YEARS
                'period': 20,
                'frequencyType': 'weekly',
                # DAILY
                'frequency': 1,
                'startDate': MIN_TIMESTAMP_MILLIS,
                'endDate': NOW_DATETIME_PLUS_SEVEN_DAYS_TIMESTAMP_MILLIS,
                'needPreviousClose': True,
        }
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/pricehistory'),
            params=params)


    # get_movers

    
    def test_get_movers(self):
        self.client.get_movers(
                self.client.Movers.Index.DJI)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/movers/$DJI'), params={})


    def test_get_movers_index_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_movers('not-an-index')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/movers/not-an-index'), params={})


    def test_get_movers_sort_order(self):
        self.client.get_movers(
                self.client.Movers.Index.DJI,
                sort_order=self.client.Movers.SortOrder.VOLUME)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/movers/$DJI'),
            params={'sort': 'VOLUME'})


    def test_get_movers_sort_order_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_movers(
                self.client.Movers.Index.DJI,
                sort_order='not-a-sort-order')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/movers/$DJI'),
            params={'sort': 'not-a-sort-order'})


    def test_get_movers_frequency(self):
        self.client.get_movers(
                self.client.Movers.Index.DJI,
                frequency=self.client.Movers.Frequency.ZERO)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/movers/$DJI'),
            params={'frequency': '0'})


    def test_get_movers_frequency_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_movers(
                self.client.Movers.Index.DJI,
                frequency='999999')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/movers/$DJI'),
            params={'frequency': '999999'})


    # get_market_hours

    
    def test_get_market_hours_single_market(self):
        self.client.get_market_hours(
                self.client_class.MarketHours.Market.EQUITY)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/markets'), params={
                'markets': 'equity'})


    def test_get_market_hours_market_list(self):
        self.client.get_market_hours(
                [self.client_class.MarketHours.Market.EQUITY,
                 self.client_class.MarketHours.Market.OPTION])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/markets'), params={
                'markets': 'equity,option'})


    def test_get_market_hours_market_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_market_hours(['not-a-market'])
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/markets'), params={
                'markets': 'not-a-market'})


    def test_get_market_hours_date(self):
        self.client.get_market_hours(
                self.client_class.MarketHours.Market.EQUITY,
                date=NOW_DATE)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/markets'), params={
                'markets': 'equity',
                'date': NOW_DATE_ISO})


    def test_get_market_hours_date_str(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_market_hours(
                    self.client_class.MarketHours.Market.EQUITY,
                    date='2020-01-02')
        self.assertEqual(str(cm.exception),
                         "expected type 'datetime.date' for " +
                         "date, got 'builtins.str'")


    # get_instruments
    
    def test_get_instruments(self):
        self.client.get_instruments(
            ['AAPL', 'MSFT'], self.client_class.Instrument.Projection.FUNDAMENTAL)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/instruments'), params={
                'symbol': 'AAPL,MSFT',
                'projection': 'fundamental'})


    def test_get_instruments_string_args(self):
        self.client.get_instruments(
            'AAPL', self.client_class.Instrument.Projection.FUNDAMENTAL)
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/instruments'), params={
                'symbol': 'AAPL',
                'projection': 'fundamental'})


    def test_get_instruments_projection_unchecked(self):
        self.client.set_enforce_enums(False)
        self.client.get_instruments(
            ['AAPL', 'MSFT'], 'not-a-projection')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/instruments'), params={
                'symbol': 'AAPL,MSFT',
                'projection': 'not-a-projection'})


    # get_instrument_by_cusip
    
    def test_get_instrument_by_cusip(self):
        self.client.get_instrument_by_cusip('037833100')
        self.mock_session.get.assert_called_once_with(
            self.make_url('/marketdata/v1/instruments/037833100'), params={})


    def test_get_instrument_by_cusip_cusip_must_be_string(self):
        with self.assertRaises(ValueError) as cm:
            self.client.get_instrument_by_cusip(37833100)
        self.assertEqual(str(cm.exception), 'cusip must be passed as str')


    # ISO-8601 datetime parameters


    @no_duplicates
    def test_orders_aware_datetime_is_converted_to_utc(self):
        # Schwab documents fromEnteredTime/toEnteredTime as
        # yyyy-MM-dd'T'HH:mm:ss.SSSZ -- the trailing Z means UTC. A datetime
        # which carries a non-UTC timezone therefore has to be converted, not
        # just formatted, or the wall clock of another zone is sent labelled as
        # UTC.
        eastern = pytz.timezone('America/New_York')
        # 2024-06-05 04:03:02 UTC, expressed in New York as 00:03:02-04:00.
        aware = eastern.localize(
                datetime.datetime(year=2024, month=6, day=5,
                                  hour=0, minute=3, second=2))

        self.client.get_orders_for_account(
                ACCOUNT_HASH,
                from_entered_datetime=aware,
                to_entered_datetime=aware)

        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': '2024-06-05T04:03:02Z',
                'toEnteredTime': '2024-06-05T04:03:02Z',
            })


    @no_duplicates
    def test_transactions_aware_datetime_is_converted_to_utc(self):
        eastern = pytz.timezone('America/New_York')
        aware = eastern.localize(
                datetime.datetime(year=2024, month=6, day=5,
                                  hour=0, minute=3, second=2))

        self.client.get_transactions(
                ACCOUNT_HASH, start_date=aware, end_date=aware)

        self.mock_session.get.assert_called_once_with(
            self.make_url(
                '/trader/v1/accounts/{accountHash}/transactions'), params={
                'types': ','.join(
                    t.value for t in self.client.Transactions.TransactionType),
                'startDate': '2024-06-05T04:03:02Z',
                'endDate': '2024-06-05T04:03:02Z',
            })


    @no_duplicates
    @unittest.skipUnless(hasattr(time, 'tzset'),
                         'needs time.tzset to pin a non-UTC local zone')
    def test_a_naive_datetime_is_sent_as_written_not_as_local_time(self):
        # A naive datetime has no zone to convert from, so its wall clock is
        # sent as written. Converting it anyway reads it as the host's local
        # time, and on a host set to UTC that gives the identical string --
        # which is what CI runners are. So the zone is pinned here to one
        # that is not UTC, or a regression would pass everywhere it runs.
        naive = datetime.datetime(year=2024, month=6, day=5,
                                  hour=4, minute=3, second=2)

        original = os.environ.get('TZ')
        os.environ['TZ'] = 'America/New_York'
        time.tzset()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                self.client.get_orders_for_account(
                        ACCOUNT_HASH,
                        from_entered_datetime=naive,
                        to_entered_datetime=naive)
        finally:
            if original is None:
                del os.environ['TZ']
            else:
                os.environ['TZ'] = original
            time.tzset()

        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': '2024-06-05T04:03:02Z',
                'toEnteredTime': '2024-06-05T04:03:02Z',
            })


    @no_duplicates
    @unittest.skipUnless(hasattr(time, 'tzset'),
                         'needs time.tzset to pin a non-UTC local zone')
    def test_a_tzinfo_with_no_offset_is_naive(self):
        # Python calls a datetime naive when utcoffset() returns None, even
        # with a tzinfo set. Keying on tzinfo alone would skip the warning and
        # then convert from the host's zone, so the same code would send a
        # different instant from each machine. The zone is pinned for the
        # reason the naive test above pins it.
        class NoOffset(datetime.tzinfo):
            def utcoffset(self, dt):
                return None

            def dst(self, dt):
                return None

        value = datetime.datetime(year=2024, month=6, day=5, hour=4,
                                  minute=3, second=2, tzinfo=NoOffset())

        original = os.environ.get('TZ')
        os.environ['TZ'] = 'America/New_York'
        time.tzset()
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                self.client.get_orders_for_account(
                        ACCOUNT_HASH, from_entered_datetime=value)
        finally:
            if original is None:
                del os.environ['TZ']
            else:
                os.environ['TZ'] = original
            time.tzset()

        # Only the parameter under test: the other end of the range defaults
        # to now, so asserting the whole dict would compare against the clock.
        params = self.mock_session.get.call_args[1]['params']
        self.assertEqual('2024-06-05T04:03:02Z', params['fromEnteredTime'])
        messages = [str(w.message) for w in caught]
        self.assertEqual(
                1, len([m for m in messages if 'no timezone' in m]), messages)


    @no_duplicates
    def test_utc_aware_datetime_is_unchanged(self):
        # The same instant already expressed in UTC must serialize identically,
        # so the conversion cannot be doing something zone-specific.
        aware = datetime.datetime(year=2024, month=6, day=5,
                                  hour=4, minute=3, second=2,
                                  tzinfo=datetime.timezone.utc)

        self.client.get_orders_for_account(
                ACCOUNT_HASH,
                from_entered_datetime=aware,
                to_entered_datetime=aware)

        self.mock_session.get.assert_called_once_with(
            self.make_url('/trader/v1/accounts/{accountHash}/orders'), params={
                'fromEnteredTime': '2024-06-05T04:03:02Z',
                'toEnteredTime': '2024-06-05T04:03:02Z',
            })


    @no_duplicates
    def test_naive_datetime_warns_on_iso_parameters(self):
        naive = datetime.datetime(year=2024, month=6, day=5,
                                  hour=4, minute=3, second=2)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.client.get_orders_for_account(
                    ACCOUNT_HASH, from_entered_datetime=naive)

        messages = [str(w.message) for w in caught]
        self.assertTrue(
                any('no timezone' in m for m in messages),
                'expected a naive-datetime warning, got {}'.format(messages))
        self.assertTrue(
                any('from_entered_datetime' in m for m in messages),
                'the warning should name the parameter, got {}'.format(messages))


    @no_duplicates
    def test_naive_datetime_warns_on_millisecond_parameters(self):
        naive = datetime.datetime(year=2024, month=6, day=5,
                                  hour=4, minute=3, second=2)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.client.get_price_history(SYMBOL, start_datetime=naive)

        messages = [str(w.message) for w in caught]
        self.assertTrue(
                any('no timezone' in m for m in messages),
                'expected a naive-datetime warning, got {}'.format(messages))
        self.assertTrue(
                any('start_datetime' in m for m in messages),
                'the warning should name the parameter, got {}'.format(messages))


    @no_duplicates
    def test_naive_datetime_warning_points_at_the_caller(self):
        # A warning which names a line inside this library tells the caller
        # nothing they can act on -- they cannot find their own offending call.
        # The number of frames to skip is not constant: get_price_history_every_day
        # calls get_price_history, and get_orders_for_account goes through
        # _make_order_query, so the wrappers sit one frame deeper than the
        # endpoints they wrap.
        # The exact line matters, not just the file: an off-by-one lands on
        # whatever called this test, which is still this file and would pass a
        # filename-only assertion while being wrong.
        naive = datetime.datetime(year=2024, month=6, day=5, hour=4)
        here = inspect.currentframe()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            line = here.f_lineno + 1
            self.client.get_price_history(SYMBOL, start_datetime=naive)
        self._assert_blamed(caught, line)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            line = here.f_lineno + 1
            self.client.get_price_history_every_day(SYMBOL, start_datetime=naive)
        self._assert_blamed(caught, line)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            line = here.f_lineno + 1
            self.client.get_orders_for_account(ACCOUNT_HASH, from_entered_datetime=naive)
        self._assert_blamed(caught, line)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            line = here.f_lineno + 1
            self.client.get_transactions(ACCOUNT_HASH, start_date=naive)
        self._assert_blamed(caught, line)

    @no_duplicates
    def test_stacklevel_never_blames_the_library_itself(self):
        # warnings.warn treats stacklevel=0 as "blame the frame that called
        # warn", which is base.py -- the failure this machinery exists to
        # avoid. Reachable whenever a frame's co_filename does not resolve
        # under the package root, as a zipimport or a frozen build can manage.
        with patch.object(type(self.client), '_PACKAGE_ROOT',
                          '/nowhere/that/exists'):
            self.assertGreaterEqual(self.client._caller_stacklevel(), 1)

    @no_duplicates
    def test_a_stack_entirely_inside_the_package_still_blames_somebody(self):
        """The fallback after the walk runs out of frames.

        `_caller_stacklevel` never returns 0, because `warnings.warn` reads 0
        as "blame the frame which called warn" -- this library -- which is the
        exact failure the function exists to avoid. A zipimport or a frozen
        build is how a frame's `co_filename` stops resolving under the
        package root.

        Under pytest the stack always contains frames outside the package, so
        the loop always returns early and an ordinary call never reaches the
        fallback. Every filename is made to look internal instead, which is
        the same shape as every frame being unresolvable, and takes the walk
        to the end.
        """
        # Choosing a clever `_PACKAGE_ROOT` or patching `os.path.abspath`
        # does not make every frame look internal, each for its own reason:
        #
        #  * `''` gives a prefix of `\` on Windows, which no absolute path
        #    starts with, so the walk returns at the first frame.
        #  * this file's drive gives `D:\` on a GitHub runner, where the
        #    checkout is on D: and the interpreter under C:\hostedtoolcache,
        #    so the stdlib frames are foreign and the walk stops midway.
        #  * patching `os.path.abspath` reaches `posixpath` itself, since
        #    base.py does `import os` -- so it redirects every call in the
        #    process for the duration, including coverage's own
        #    `canonical_filename`, which would then record a newly traced file
        #    as living under `schwaby/`.
        #
        # The first two are green on Linux and red on `windows-latest`, which
        # runs on pull requests and tags.
        #
        # So supply the frames instead. `_caller_stacklevel` walks whatever
        # `inspect.currentframe()` gives it and reads `co_filename` off each
        # one, so a synthetic chain settles the question with no filesystem,
        # no path semantics, and nothing patched outside this module.
        internal = self.client._PACKAGE_ROOT + os.sep + 'inside.py'
        foreign = os.path.join('elsewhere', 'outside.py')

        def chain(*filenames):
            frame = None
            for name in reversed(filenames):
                frame = SimpleNamespace(
                        f_code=SimpleNamespace(co_filename=name), f_back=frame)
            return frame

        # Every frame internal: the walk runs off the end and takes the
        # fallback, which must never be 0 -- `warnings.warn` reads 0 as
        # "blame the frame which called warn", which is this library.
        with patch('schwaby.client.base.inspect.currentframe',
                   return_value=chain(*([internal] * 12))):
            self.assertEqual(1, self.client._caller_stacklevel())

        # The control. The same walk with a foreign frame in it returns that
        # frame's depth, so the 1 above is the fallback rather than the value
        # any stack happens to produce.
        with patch('schwaby.client.base.inspect.currentframe',
                   return_value=chain(internal, internal, internal, foreign)):
            self.assertEqual(3, self.client._caller_stacklevel())

    @no_duplicates
    def test_a_sibling_package_is_not_mistaken_for_ours(self):
        """A bare prefix test matches '.../schwabytools' against a root of
        '.../schwaby', so frames from an unrelated package are skipped as if
        they were ours and the blame lands a frame too far out.

        The sibling's name has to share the package's own name as a prefix.
        A sibling named after any other prefix collides with nothing, so the
        fixture would go quietly inert and the mutation would stop being
        caught, with the test still passing.

        This needs a real module in a real sibling directory. Patching
        _PACKAGE_ROOT cannot stand it up, because the walk starts at this
        library's own frame -- any root not covering base.py makes that frame
        foreign and the walk stops at once, whichever way the comparison is
        written. So build the layout on disk: symlink the package in as
        'schwaby', put 'schwabytools' beside it, and call from inside that.
        """
        import os
        import subprocess
        import tempfile

        package = os.path.dirname(os.path.dirname(os.path.abspath(
                schwaby.client.base.__file__)))

        with tempfile.TemporaryDirectory() as tmp:
            os.symlink(package, os.path.join(tmp, 'schwaby'))
            sibling = os.path.join(tmp, 'schwabytools')
            os.mkdir(sibling)

            with open(os.path.join(sibling, '__init__.py'), 'w') as f:
                f.write('')
            with open(os.path.join(sibling, 'caller.py'), 'w') as f:
                f.write(
                    'import datetime\n'
                    'def go(client):\n'
                    '    client.get_price_history(\n'
                    '            "AAPL",\n'
                    '            start_datetime=datetime.datetime(2024, 6, 5))\n')

            script = (
                'import sys, warnings\n'
                'sys.path.insert(0, {tmp!r})\n'
                'from schwaby.client import Client\n'
                'import schwabytools.caller as caller\n'
                'class S:\n'
                '    timeout = None\n'
                '    def get(self, *a, **k):\n'
                '        class R:\n'
                '            status_code = 200\n'
                '            text = "{{}}"\n'
                '        return R()\n'
                'with warnings.catch_warnings(record=True) as w:\n'
                '    warnings.simplefilter("always")\n'
                '    caller.go(Client("k", S()))\n'
                'for x in w:\n'
                '    if "no timezone" in str(x.message):\n'
                '        print(x.filename)\n'
            ).format(tmp=tmp)

            blamed = subprocess.run(
                    [sys.executable, '-c', script],
                    capture_output=True, text=True, cwd=tmp).stdout.strip()

        self.assertTrue(
                blamed.endswith(os.path.join('schwabytools', 'caller.py')),
                'the warning should name the sibling package that made the '
                'call, but named {!r}'.format(blamed))

    def _assert_blamed(self, caught, expected_line):
        naive_warnings = [w for w in caught if 'no timezone' in str(w.message)]
        self.assertTrue(naive_warnings, 'expected a naive-datetime warning')
        for w in naive_warnings:
            self.assertEqual(
                    (__file__, expected_line), (w.filename, w.lineno),
                    'warning blamed {}:{} rather than the calling line'.format(
                        w.filename, w.lineno))


    @no_duplicates
    def test_the_librarys_own_start_default_does_not_warn(self):
        # Omitting the dates is the common case. Warning there tells the caller
        # to attach a timezone to a datetime they never wrote, and points at
        # their line to do it.
        #
        # Scoped to start_datetime, the default this test is about. The
        # end_datetime default is timezone-aware as well.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.client.get_price_history_every_day(SYMBOL)
            self.client.get_price_history(SYMBOL)
            self.client.get_orders_for_account(ACCOUNT_HASH)
            self.client.get_transactions(ACCOUNT_HASH)

        blamed = [str(w.message) for w in caught
                  if 'no timezone' in str(w.message)
                  and 'start_datetime' in str(w.message)]
        self.assertEqual([], blamed)


    @no_duplicates
    def test_the_default_start_date_does_not_depend_on_the_host(self):
        # It becomes epoch milliseconds, so a naive default would shift with
        # whatever timezone the machine happens to be set to. Only the
        # per-frequency helpers fill the dates in; raw get_price_history sends
        # what it is given.
        self.client.get_price_history_every_day(SYMBOL)
        params = self.mock_session.get.call_args[1]['params']
        self.assertEqual(
                int(datetime.datetime(
                        1971, 1, 1,
                        tzinfo=datetime.timezone.utc).timestamp() * 1000),
                params['startDate'])


    @no_duplicates
    def test_aware_datetime_does_not_warn(self):
        aware = datetime.datetime(year=2024, month=6, day=5,
                                  hour=4, minute=3, second=2,
                                  tzinfo=datetime.timezone.utc)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.client.get_orders_for_account(
                    ACCOUNT_HASH,
                    from_entered_datetime=aware, to_entered_datetime=aware)
            self.client.get_price_history(
                    SYMBOL, start_datetime=aware, end_datetime=aware)

        naive_warnings = [str(w.message) for w in caught
                          if 'no timezone' in str(w.message)]
        self.assertEqual([], naive_warnings)


    @no_duplicates
    def test_date_object_does_not_warn(self):
        # A date has no time of day to be ambiguous about.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.client.get_orders_for_account(
                    ACCOUNT_HASH,
                    from_entered_datetime=datetime.date(2024, 6, 5))

        naive_warnings = [str(w.message) for w in caught
                          if 'no timezone' in str(w.message)]
        self.assertEqual([], naive_warnings)


    # Token refresh failures


    @no_duplicates
    def test_oauth_error_becomes_a_schwab_exception(self):
        # The refresh happens on the way past an ordinary request, so this is
        # what an application which never touches the token directly sees.
        self.mock_session.get.side_effect = OAuthError(
                error='unsupported_token_type',
                description='Bad refresh_token')

        with self.assertRaises(TokenRefreshError):
            self.client.get_quote(SYMBOL)


    @no_duplicates
    def test_token_refresh_error_preserves_the_original(self):
        original = OAuthError(error='unsupported_token_type',
                              description='Bad refresh_token')
        self.mock_session.get.side_effect = original

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertIs(original, cm.exception.__cause__)
        self.assertIn('Bad refresh_token', str(cm.exception))


    @no_duplicates
    def test_token_refresh_error_escapes_the_endpoints_text(self):
        # The endpoint's code and description reach this message and the
        # chained error's line in a traceback, and both end up in logs. The
        # chained error is the same object, its attributes as authlib set
        # them, since callers classify a refresh by walking the chain.
        import traceback
        description = 'line one\nline two \x1b[31m' + 'y' * 500
        original = OAuthError(error='invalid_grant\nFORGED',
                              description=description)
        self.mock_session.get.side_effect = original

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertIs(original, cm.exception.__cause__)
        self.assertEqual('invalid_grant\nFORGED', original.error)
        self.assertEqual(description, original.description)
        self.assertIn('invalid_grant\\nFORGED', str(cm.exception))
        logged = ''.join(traceback.format_exception(cm.exception))
        for raw in ('\nFORGED', 'line one\nline two', '\x1b', 'y' * 300):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, logged)


    @no_duplicates
    def test_token_refresh_error_keeps_this_librarys_own_description(self):
        # An unusable token response is refused with this library's text,
        # which is longer than the cut and says what a usable token needs.
        import httpx2
        from schwaby import auth
        request = httpx2.Request(
                'POST', 'https://api.schwabapi.com/v1/oauth/token')
        with self.assertRaises(OAuthError) as refused:
            auth._refuse_unusable_token_response(httpx2.Response(
                    200, json={'message': 'Unauthorized'}, request=request))
        self.mock_session.get.side_effect = refused.exception

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertIn('no refresh token that is empty or not a string',
                      str(cm.exception))


    @no_duplicates
    def test_token_refresh_error_reports_token_age(self):
        # The age is the signal worth reasoning about: Schwab documents the
        # seven day term but not what it returns when the term expires.
        eight_days = 8 * 60 * 60 * 24

        metadata = Mock()
        metadata.token_age.return_value = eight_days
        self.client.token_metadata = metadata

        self.mock_session.get.side_effect = OAuthError(
                error='unsupported_token_type',
                description='Bad refresh_token')

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertEqual(eight_days, cm.exception.token_age)
        self.assertIn('8.0 days old', str(cm.exception))


    @no_duplicates
    def test_token_refresh_error_reports_a_decimal_token_age(self):
        # A token store such as DynamoDB returns the creation timestamp as a
        # Decimal, so the age is one too, and the advice must not fail on it.
        eight_days = decimal.Decimal(8 * 60 * 60 * 24)

        metadata = Mock()
        metadata.token_age.return_value = eight_days
        self.client.token_metadata = metadata

        self.mock_session.get.side_effect = OAuthError(
                error='unsupported_token_type',
                description='Bad refresh_token')

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertEqual(eight_days, cm.exception.token_age)
        self.assertIn('8.0 days old', str(cm.exception))


    @no_duplicates
    def test_a_retryable_refusal_past_seven_days_says_to_alert(self):
        # Schwab has not held exactly to its seven days, so the verdict stays
        # retryable, and the message is what says someone should know.
        self.mock_session.get.side_effect = OAuthError(
                error='invalid_client', description='Client not recognized')
        for age, alerts in ((8 * 86400, True),
                            (decimal.Decimal(8 * 86400), True),
                            (7 * 86400, False), (86400, False), (None, False)):
            with self.subTest(age=age):
                if age is None:
                    self.client.token_metadata = None
                else:
                    metadata = Mock()
                    metadata.token_age.return_value = age
                    self.client.token_metadata = metadata

                with self.assertRaises(TokenRefreshError) as cm:
                    self.client.get_quote(SYMBOL)

                self.assertFalse(cm.exception.refresh_token_invalid)
                self.assertEqual(alerts, 'alert someone' in str(cm.exception))

        # A terminal refusal already says to log in again.
        metadata = Mock()
        metadata.token_age.return_value = 8 * 86400
        self.client.token_metadata = metadata
        self.mock_session.get.side_effect = OAuthError(
                error='invalid_grant', description='expired')
        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)
        self.assertTrue(cm.exception.refresh_token_invalid)
        self.assertNotIn('alert someone', str(cm.exception))

    @no_duplicates
    def test_token_refresh_error_without_metadata(self):
        # Clients built directly, as the tests do, have no token metadata.
        self.assertIsNone(self.client.token_metadata)

        self.mock_session.get.side_effect = OAuthError(
                error='unsupported_token_type',
                description='Bad refresh_token')

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertIsNone(cm.exception.token_age)
        self.assertIn('age is unknown', str(cm.exception))


    @no_duplicates
    def test_network_errors_are_not_relabelled_as_token_errors(self):
        # A connection failure while refreshing and one while fetching a quote
        # are the same problem, and are not distinguishable from here. Claiming
        # the token is at fault would be a guess.
        self.mock_session.get.side_effect = httpx2.ConnectError('no route')

        with self.assertRaises(httpx2.ConnectError):
            self.client.get_quote(SYMBOL)


    @no_duplicates
    def test_successful_requests_are_unaffected(self):
        self.client.get_quote(SYMBOL)
        self.mock_session.get.assert_called_once()


    # Is the refresh token dead, or was this just a bad day?


    #: Schwab's answer, observed 2026-08-02, to a refresh token past its
    #: seven day expiry. Note the outer
    #: code is unsupported_token_type, which RFC 7009 defines for the
    #: revocation endpoint and which describes nothing that happened, and that
    #: the real answer is a JSON string nested in the description.
    OBSERVED_DEAD_REFRESH_TOKEN = OAuthError(
            error='unsupported_token_type',
            description='400 Bad Request: {"error_description":"Refresh token '
                        'is invalid, expired or revoked","error":'
                        '"invalid_grant"}')

    @no_duplicates
    def test_observed_dead_refresh_token_is_recognized(self):
        self.mock_session.get.side_effect = self.OBSERVED_DEAD_REFRESH_TOKEN

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertTrue(cm.exception.refresh_token_invalid)
        self.assertIn('retrying will not help', str(cm.exception))


    @no_duplicates
    def test_a_description_that_is_not_a_string_is_not_terminal(self):
        # `description` is whatever the exception carries, and the two reads
        # after this one are `.find('{')` and `.rfind('}')`, which a non-string
        # does not have.
        #
        # False is the right answer rather than an exception, for the reason
        # the docstring gives: a failure wrongly called terminal stops an
        # application that only needed to retry, and this path runs when the
        # application is already in trouble.
        for description in (None, 400, {'error': 'invalid_grant'},
                            ['invalid_grant'], b'invalid_grant'):
            with self.subTest(description=description):
                error = OAuthError(error='some_other_code')
                error.description = description
                self.mock_session.get.side_effect = error

                with self.assertRaises(TokenRefreshError) as cm:
                    self.client.get_quote(SYMBOL)
                self.assertFalse(cm.exception.refresh_token_invalid)

    @no_duplicates
    def test_a_string_description_is_still_read(self):
        # The positive control. Every assertion above is satisfied by a
        # classifier that returns False for everything, including the observed
        # dead-token response the two tests around this one exist for.
        self.mock_session.get.side_effect = self.OBSERVED_DEAD_REFRESH_TOKEN
        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)
        self.assertTrue(cm.exception.refresh_token_invalid)

    @no_duplicates
    def test_standard_placement_is_also_recognized(self):
        # If Schwab ever puts the code where RFC 6749 says it goes.
        self.mock_session.get.side_effect = OAuthError(
                error='invalid_grant',
                description='Refresh token is invalid, expired or revoked')

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertTrue(cm.exception.refresh_token_invalid)


    @no_duplicates
    def test_locally_unusable_tokens_are_not_reported_as_retryable(self):
        # MissingTokenError and InvalidTokenError are OAuthError subclasses
        # raised by authlib itself, before any HTTP call, when the stored token
        # has no refresh_token or is otherwise unusable. Nothing about them
        # improves with time, so an unattended application told to retry would
        # retry forever on a permanent, purely local condition.
        from authlib.integrations.base_client.errors import (
                MissingTokenError, InvalidTokenError)

        for error in (MissingTokenError(), InvalidTokenError()):
            self.setUp()
            self.mock_session.get.side_effect = error

            with self.assertRaises(TokenRefreshError) as cm:
                self.client.get_quote(SYMBOL)

            self.assertTrue(
                    cm.exception.refresh_token_invalid,
                    '{} is permanent and must not be reported as '
                    'retryable'.format(type(error).__name__))
            self.assertIn('cannot be used', str(cm.exception))


    @no_duplicates
    def test_a_locally_raised_unsupported_token_type_is_not_confused(self):
        # authlib raises UnsupportedTokenTypeError locally with the same error
        # code Schwab uses as its outer wrapper. Without the nested body it is
        # not evidence the refresh token is dead, and must not be read as such
        # -- while authlib will replace the stored token by itself: it has an
        # expiry authlib acts on, and a refresh token to refresh with.
        import time
        from authlib.integrations.base_client.errors import (
                UnsupportedTokenTypeError)

        self.mock_session.token = {'access_token': 'a', 'token_type': 'mac',
                                   'expires_at': int(time.time()) + 3600,
                                   'refresh_token': 'r'}
        self.mock_session.get.side_effect = UnsupportedTokenTypeError()

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertFalse(cm.exception.refresh_token_invalid)
        # Nothing reached Schwab, so the message must not say what it said.
        self.assertIn('nothing was sent to Schwab', str(cm.exception))
        self.assertNotIn('Schwab did not say', str(cm.exception))
        # authlib refreshes 300 seconds early, not when the expiry passes.
        self.assertIn('as that expiry nears', str(cm.exception))

    @no_duplicates
    def test_a_local_unsupported_token_type_without_expiry_is_terminal(self):
        # With no expiry the stored token is never refreshed, so a token
        # authlib cannot send fails the same way on every call and nothing is
        # ever sent: retrying cannot help. A token file holding a refusal
        # body rather than a token looks exactly like this.
        from authlib.integrations.base_client.errors import (
                UnsupportedTokenTypeError)

        self.mock_session.get.side_effect = UnsupportedTokenTypeError()

        # authlib checks an expires_at only when it is an int, so a string
        # there is no expiry either -- and nor is one in milliseconds, which is
        # never reached. It refreshes only with a refresh token, so a good
        # expiry without one is never replaced either.
        import time
        for token in ({'message': 'Unauthorized', 'refresh_token': 'r'},
                      {'access_token': 'a', 'token_type': 'mac',
                       'expires_at': '1789073203.0', 'refresh_token': 'r'},
                      {'access_token': 'a', 'token_type': 'mac',
                       'expires_at': 1789073203000, 'refresh_token': 'r'},
                      # Past the seven days a refresh token lasts.
                      {'access_token': 'a', 'token_type': 'mac',
                       'expires_at': int(time.time()) + 8 * 86400,
                       'refresh_token': 'r'},
                      # A refresh token authlib cannot send: empty, as an
                      # erased one is, or not a string at all.
                      {'access_token': 'a', 'token_type': 'mac',
                       'expires_at': int(time.time()) + 3600,
                       'refresh_token': ''},
                      {'access_token': 'a', 'token_type': 'mac',
                       'expires_at': int(time.time()) + 3600,
                       'refresh_token': {'x': 1}},
                      {'access_token': 'a', 'token_type': 'mac',
                       'expires_at': int(time.time()) + 3600}):
            with self.subTest(token=token):
                self.mock_session.token = token
                with self.assertRaises(TokenRefreshError) as cm:
                    self.client.get_quote(SYMBOL)

                self.assertTrue(cm.exception.refresh_token_invalid)
                self.assertIn('login flow has to be completed again',
                              str(cm.exception))

    @no_duplicates
    def test_a_local_unsupported_token_type_with_an_unreadable_token_stays_retryable(self):
        # When the stored token cannot be read, whether it expires is unknown,
        # and calling a failure terminal wrongly is the worse mistake.
        from authlib.integrations.base_client.errors import (
                UnsupportedTokenTypeError)

        self.mock_session.token = None
        self.mock_session.get.side_effect = UnsupportedTokenTypeError()

        with self.assertRaises(TokenRefreshError) as cm:
            self.client.get_quote(SYMBOL)

        self.assertFalse(cm.exception.refresh_token_invalid)


    @no_duplicates
    def test_other_oauth_failures_are_not_called_terminal(self):
        # Claiming a recoverable failure is terminal stops an application which
        # only needed to try again, so anything unrecognized has to come back
        # False. These are the shapes most likely to be mistaken for the above.
        not_terminal = [
            OAuthError(error='server_error',
                       description='500 Internal Server Error'),
            OAuthError(error='temporarily_unavailable', description=''),
            OAuthError(error='invalid_client',
                       description='400 Bad Request: bad app key'),
            # Right words, wrong place: a description mentioning the phrase but
            # carrying no nested error code.
            OAuthError(error='unsupported_token_type',
                       description='400 Bad Request: refresh token trouble'),
            # Nested body, but a different code.
            OAuthError(error='unsupported_token_type',
                       description='400 Bad Request: {"error":"invalid_client"}'),
            # Nested body which is not JSON at all.
            OAuthError(error='unsupported_token_type',
                       description='400 Bad Request: {not json}'),
            OAuthError(error=None, description=None),
        ]

        for error in not_terminal:
            self.setUp()
            self.mock_session.get.side_effect = error

            with self.assertRaises(TokenRefreshError) as cm:
                self.client.get_quote(SYMBOL)

            self.assertFalse(
                    cm.exception.refresh_token_invalid,
                    '{!r} must not be reported as a dead refresh '
                    'token'.format(str(error)))
            self.assertIn('may be transient', str(cm.exception))


class OrderTerminalStatusesTest(unittest.TestCase):
    """The REST order statuses after which an order stops changing."""

    TERMINAL = ('FILLED', 'REJECTED', 'CANCELED', 'EXPIRED', 'REPLACED')

    @no_duplicates
    def test_a_raw_status_string_tests_directly(self):
        # The reason the set holds values. `Status` is a plain Enum, so
        # `Status.FILLED == 'FILLED'` is False, and a set of members would
        # call the string a REST response carries not terminal, silently.
        for status in self.TERMINAL:
            with self.subTest(status=status):
                self.assertIn(status, Client.Order.TERMINAL_STATUSES)

    @no_duplicates
    def test_a_working_order_is_not_terminal(self):
        # The other side. A set that contained everything would pass the test
        # above, so membership has to be shown to discriminate.
        for status in ('WORKING', 'QUEUED', 'ACCEPTED', 'NEW',
                       'PENDING_CANCEL', 'PENDING_REPLACE', 'AWAITING_UR_OUT'):
            with self.subTest(status=status):
                self.assertNotIn(status, Client.Order.TERMINAL_STATUSES)

    @no_duplicates
    def test_it_is_exactly_the_five(self):
        # Pinned on purpose. Unlike a set that mirrors other code, this is a
        # judgement about which statuses end an order, so widening it should
        # be a decision someone makes here rather than a side effect.
        self.assertEqual(set(self.TERMINAL),
                         set(Client.Order.TERMINAL_STATUSES))

    @no_duplicates
    def test_every_value_is_a_status_schwab_defines(self):
        values = {status.value for status in Client.Order.Status}
        self.assertLessEqual(Client.Order.TERMINAL_STATUSES, values)

    @no_duplicates
    def test_one_definition_serves_both_clients(self):
        self.assertIs(Client.Order.TERMINAL_STATUSES,
                      AsyncClient.Order.TERMINAL_STATUSES)


class ClientTest(_TestClient, unittest.TestCase):
    """
    Subclass set to use Client and MagicMock
    """
    client_class    = Client
    magicmock_class = MagicMock

    def test_close_session(self):
        # Without this the synchronous client would hold its connections
        # until it happened to be collected.
        self.client.close_session()
        self.mock_session.close.assert_called_once_with()

class AsyncClientTest(_TestClient, unittest.TestCase):
    """
    Subclass set to resync AsyncClient and use AsyncMagicMock
    """
    client_class    = ResyncProxy(AsyncClient)
    magicmock_class = AsyncMagicMock

    def test_async_close(self):
        self.client.close_async_session()
