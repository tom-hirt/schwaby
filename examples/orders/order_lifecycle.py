"""Placing an order and finding out what happened to it.

Four steps, and the interesting parts are the failures rather than the happy
path:

  1. place the order
  2. get its id back out of the response
  3. poll until it reaches a terminal state
  4. cancel it if it never does

The documentation covers each call. What it cannot show is that step 2 has four
outcomes wanting four different responses: Schwab rejected it and nothing was
placed; Schwab accepted it and gave back no id, so something is probably live
and untracked; the response belongs to another account, so an order is live
there; or you got an id.
"""

import time

import httpx2

import schwaby
from schwaby.orders.equities import equity_buy_limit
from schwaby.utils import (
    AccountHashMismatchException,
    OrderIdNotFoundError,
    UnsuccessfulOrderException,
    Utils,
    find_account_hash,
)

API_KEY = 'XXXXXX'
APP_SECRET = 'XXXXXX'
TOKEN_PATH = './token.json'

# The account to trade, as a string. A token can cover several accounts, and
# the order of the account list does not tell you which one you mean. The
# placeholder is not digits, so an unedited run is refused as a bad number
# rather than reported as an account the token does not cover.
ACCOUNT_NUMBER = 'XXXXXXXX'

# Deliberately far from the market so this does not fill while you read it.
SYMBOL = 'AAPL'
QUANTITY = 1
LIMIT_PRICE = '1.00'      # a string, never a float: a float is refused

# Anything not in this set is still live and worth waiting on.
TERMINAL = {'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'REPLACED'}

POLL_EVERY = 2.0
GIVE_UP_AFTER = 60.0


def place(client, account_hash):
    """Returns the order id, or None when there is nothing to follow.

    Re-raises on a hash mismatch: that one is not a placement outcome, it is
    the wrong account, and continuing would poll the wrong book.
    """
    order = equity_buy_limit(SYMBOL, QUANTITY, LIMIT_PRICE).build()

    response = client.place_order(account_hash, order)

    try:
        return Utils(client, account_hash).extract_order_id(response)
    except UnsuccessfulOrderException as exc:
        # Nothing was placed. Schwab's own words for the rejection are in the
        # message, and the whole response is on the exception -- a status code
        # alone does not separate a malformed order from one the account cannot
        # afford.
        print('rejected, nothing placed: %s' % exc)
        return None
    except OrderIdNotFoundError as exc:
        # Something almost certainly WAS placed and you have no id for it.
        # This is the branch worth writing properly: a live order nobody is
        # watching is worse than a rejected one.
        print('PLACED BUT UNTRACKED: %s' % exc)
        print('reconcile against get_orders_for_account before trading again')
        return None
    except AccountHashMismatchException as exc:
        # The order IS live -- this is raised only after a successful response
        # and a parsed order id -- just on an account you were not expecting to
        # trade. Everything needed to go and cancel it is on the exception, so
        # do not re-derive it from the message.
        print('WRONG ACCOUNT, ORDER IS LIVE: order %s on account %s'
              % (exc.order_id, exc.account_hash))
        raise


def wait_for(client, account_hash, order_id):
    """Polls until the order reaches a terminal state, or the deadline passes."""
    deadline = time.time() + GIVE_UP_AFTER

    while time.time() < deadline:
        response = client.get_order(order_id, account_hash)
        response.raise_for_status()
        status = response.json()['status']

        print('status: %s' % status)
        if status in TERMINAL:
            return status

        time.sleep(POLL_EVERY)

    return None


def main():
    client = schwaby.auth.client_from_token_file(
            TOKEN_PATH, api_key=API_KEY, app_secret=APP_SECRET)

    accounts = client.get_account_numbers()
    accounts.raise_for_status()
    account_hash = find_account_hash(accounts.json(), ACCOUNT_NUMBER)

    order_id = place(client, account_hash)

    if order_id is None:
        # `place` has already reported which of the two happened, and they are
        # very different situations -- see the handlers there.
        print('no order id; nothing to follow')
        return

    print('placed order %s' % order_id)

    status = wait_for(client, account_hash, order_id)

    if status is None:
        print('still live after %.0fs; cancelling' % GIVE_UP_AFTER)
        cancelled = client.cancel_order(order_id, account_hash)
        # Cancelling an order that filled a moment ago is not an error in your
        # code, it is a race you lost. Check rather than assume.
        if cancelled.status_code == httpx2.codes.OK:
            print('cancelled')
        else:
            print('cancel refused: %s' % cancelled.text)


if __name__ == '__main__':
    main()
