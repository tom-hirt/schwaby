<div align="center">

# schwaby

**A Python client for the Charles Schwab API, built for code that trades real money.**

[![tests](https://github.com/tom-hirt/schwaby/workflows/tests/badge.svg)](https://github.com/tom-hirt/schwaby/actions?query=workflow%3Atests)
[![PyPI](https://img.shields.io/pypi/v/schwaby.svg)](https://pypi.org/project/schwaby/)
[![Python](https://img.shields.io/pypi/pyversions/schwaby.svg)](https://pypi.org/project/schwaby/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

```shell
pip install schwaby
```

Every endpoint Schwab publishes, an order builder that produces JSON Schwab
accepts, and a streaming client that turns this:

```python
{'key': 'AAPL', '1': 421.55, '2': 421.60, '3': 421.58}
```

into this:

```python
{'key': 'AAPL', 'BID_PRICE': 421.55, 'ASK_PRICE': 421.60, 'LAST_PRICE': 421.58}
```

Synchronous and `asyncio` over the same interface. Python 3.10+.

---

> [!CAUTION]
>
> ## This places real orders with real money, and it has bugs
>
> Not "may have". Every defect ever found in this library was in code with 100%
> test coverage, and the ones that mattered were **silent** — a limit price a
> cent low, an option symbol naming a different contract, an order routed to a
> venue nobody asked for, an accepted order whose id was thrown away. None
> raised. None failed a test. The [changelog](CHANGELOG.md) lists them, and the
> next one is in there somewhere too, unfound.
>
> **You are responsible for every order your code places.** Not the author, not
> the maintainer, not anyone who has ever contributed. The MIT licence puts it
> in legal terms — no warranty of any kind, express or implied, and no liability
> for any claim or damages — and it means what it says: if this library loses
> you money, the loss is yours.
>
> Nothing here is financial advice and nothing here is a guarantee of
> correctness. There is no undo. A wrong order is filled before you know it was
> wrong.
>
> So: start with size you can afford to lose entirely, and stay there longer
> than feels necessary. Reconcile against the broker rather than trusting what
> this library tells you happened. Read the code on every path that places,
> replaces or cancels an order — all of it is [right
> here](https://github.com/tom-hirt/schwaby/tree/main/schwaby). And assume the
> bug you have not found is on the path you did not read.
>
> If that is not a trade you want to make, use Schwab's own interfaces instead.
> That is a completely reasonable choice.

---

## What is `schwaby`?

Charles Schwab publishes a trading API: REST endpoints for quotes, option chains,
price history, accounts and orders, plus a websocket for real-time data. It is a
capable API, and a raw one.

Raw in specific ways:

| | |
|---|---|
| **You run the OAuth flow yourself** | …against a refresh token that expires seven days after authorization, so an unattended program eventually just stops |
| **The streamer numbers its fields** | …and the same number means different things on different services. Field `2` is the ask price on `LEVELONE_EQUITIES` and the open price on `CHART_EQUITY` |
| **Order JSON is deeply nested** | …and a malformed order comes back rejected with little hint as to what was wrong |
| **Parameters are server-validated strings** | …so a typo becomes an HTTP 400 halfway through a session rather than an error where you wrote it |
| **The developer portal is behind a login** | …so there is not much to read while you work any of this out |

`schwaby` is the Python layer over all of that. One method per endpoint, with the
legal parameter values as enums. An order builder that assembles Schwab's JSON from
named parts, plus ready-made templates for the orders and option strategies most
people place. A streaming client that logs in, matches responses to requests, and
relabels every message before your handler sees it. Token refresh handled and
written to disk.

Everywhere it can, it gets out of the way: you pass raw values and get back the raw
`httpx2` response, to interpret however you like. Anything you can do with
hand-rolled HTTP you can do here, with less of it.

---

## Why use it

Wrapping the tedious parts is table stakes. What follows is what `schwaby` does
*differently* — and, more to the point, what each one costs you when it is missing.

| What you get | What happens without it |
|---|---|
| **Prices go out exactly as written** | A float is rounded somewhere, and your order goes in a cent low with nothing to say so |
| **A token file that survives a crash** | A process killed mid-refresh leaves a corrupt token that only a browser can repair |
| **A stream that reports what it swallowed** | A feed that died an hour ago looks exactly like a quiet market |
| **Errors that name the actual fault** | You debug the callback port when the real problem was a missing package |
| **Enums for every parameter** | A typo returns HTTP 400 from the venue instead of failing on the line you wrote |

The detail behind each:

### Prices go out exactly as written

`set_price` takes a string or a `decimal.Decimal` and **refuses a `float`**, because
a float cannot represent most prices exactly and converting one has to round
somewhere. `8.2 * 100` is `819.9999999999999`; truncate that and you have sent an
order a cent low, with nothing anywhere to say so. Making the caller decide the
rounding is the only version of this that cannot be silently wrong.

### A token file that survives a crash

Token writes go to a temporary file and are renamed into place, so a process killed
mid-refresh leaves either the old token or the new one — never a half-written file
that can only be repaired by sitting down at a browser.

`client.token_age()` tells you how long you have before Schwab's seven-day refresh
window closes, counted from the original authorization rather than the last refresh,
which is the number that actually governs expiry.

### A stream that reports what it swallowed

A websocket client has to absorb some failures: a handler that raised, a response to
a request nobody is waiting for any more, a frame that will not parse. Absorbing them
quietly makes a broken feed look like a slow one.

`add_error_handler` hands you every one of them as an exception object, so a stalled
strategy can tell the difference between a quiet market and a stream that stopped
working an hour ago.

### Errors that name the actual fault

A rejected order carries Schwab's own explanation, not just a status code. A missing
package is reported as a missing package, not as a callback server that exited. A
stream request the server accepts but never answers raises `ResponseTimeoutError`
instead of waiting forever behind a keepalive that is still cheerfully replying to
pings.

### Enums for every parameter

Every endpoint Schwab publishes has a method, and every endpoint's legal parameter
values are enums on the client — so a misspelled projection or an invalid order
duration fails immediately in Python, where you can see which line did it.

---

## Sixty seconds in

**1. Authenticate.** A browser opens, you log in, and the token is written to disk
and refreshed for you from then on.

```python
from schwaby.auth import easy_client

c = easy_client(
        api_key='YOUR_API_KEY',
        app_secret='YOUR_APP_SECRET',
        callback_url='https://127.0.0.1:8182',
        token_path='/path/to/token.json')
```

**2. Ask for data.** You get back the raw `httpx2` response.

```python
r = c.get_price_history_every_day('AAPL')
r.raise_for_status()
candles = r.json()['candles']
```

**3. Place an order** from a template, without touching Schwab's order JSON.

```python
from schwaby.orders.equities import equity_buy_limit
from schwaby.utils import find_account_hash

account_hash = find_account_hash(c.get_account_numbers().json(), '123456789')
c.place_order(account_hash, equity_buy_limit('AAPL', 10, '210.50'))
```

The price is a string on purpose — see [Prices go out exactly as
written](#prices-go-out-exactly-as-written).

**4. Stream quotes**, with fields you can read.

```python
import asyncio
from schwaby.streaming import StreamClient

async def main():
    stream = StreamClient(c)
    await stream.login()

    stream.add_level_one_equity_handler(
            lambda msg: print(msg['content'][0]['ASK_PRICE']))
    await stream.level_one_equity_subs(['AAPL', 'MSFT'])

    while True:
        await stream.handle_message()

asyncio.run(main())
```

New here? The **[getting started guide](https://schwaby.readthedocs.io/en/stable/getting-started.html)** walks through
registering an app with Schwab and getting your first token.

---

## What it covers

| | |
|---|---|
| **Authentication** | Browser login, a manual flow for headless boxes and notebooks, a two-step flow for web backends, and token refresh handled for you |
| **Market data** | Quotes, fundamentals, option chains, price history at seven granularities, movers, market hours, instrument lookup |
| **Streaming** | Fourteen real-time services over one websocket: level one equities, options, futures and forex, order book depth from Nasdaq and NYSE, chart and screener feeds, news headlines, and your own account activity |
| **Orders** | Construction, placement, replacement, cancellation and preview, with templates for the common equity orders and option strategies |
| **Accounts** | Balances, positions, orders and transaction history |
| **Sync and async** | Swap `Client` for `AsyncClient`, add `await`, change nothing else |

The **[documentation](https://schwaby.readthedocs.io/en/stable/)** is worth reading even if you end up calling
the API directly. Schwab's own portal is behind a login, so for much of this API
those pages are the most accessible description of how it actually behaves.

<details>
<summary><b>What it does not do</b></summary>

<br>

A few things people ask for that Schwab's API does not offer:

- **No paper trading.** Orders placed through this API are real.
- **No historical options pricing.** Current chains only.
- **No thinkorswim.** Schwab owns
  [thinkorswim](https://www.schwab.com/trading/thinkorswim/desktop), but this API is
  unaffiliated with it. You can trade the same accounts; some of what TOS does has no
  API equivalent.

</details>

---

## Installing

```shell
pip install schwaby
```

That is the whole install. Python 3.10 and up, no extras to remember.

**The distribution and the package are both `schwaby`.**

```python
import schwaby
```

`schwaby` and `schwab-py` install side by side without interfering. They ship
different packages, so you can have both installed and import whichever you
mean --- useful if you want to compare the two against the same account.

One exception: if you are upgrading from `schwaby` 3.0.3 or earlier and
`schwab-py` is also installed, run `pip install --force-reinstall schwab-py`
afterwards. Those versions shipped a package called `schwab` as well, so
uninstalling one takes files the other needs --- silently, and `import schwab`
goes on succeeding until the first submodule you use.

---

## Getting help and contributing

Bug reports, questions, suggestions and patches all go to the repository: open an
[issue](https://github.com/tom-hirt/schwaby/issues) or a [pull
request](https://github.com/tom-hirt/schwaby/pulls).

## Where this came from

`schwaby` began from [alexgolec/schwab-py](https://github.com/alexgolec/schwab-py),
an excellent MIT-licensed library by Alex Golec that gave this project its shape —
the endpoint coverage, the order builder, the streaming field tables. Most of the
code here is still his, his copyright notice is retained unchanged, and the
licence is the same MIT one he chose.

It became a separate project for a practical reason rather than a philosophical one.
Code that places real orders imposes requirements a general-purpose wrapper has no
particular reason to prioritise — the
guarantees under [Why use it](#why-use-it) above, most of which needed changes to
existing behaviour rather than additions on top of it.

## License

`schwaby` is released under the [MIT license](LICENSE).

Copyright is shared: © 2023 Alex Golec for the original work, © 2026 Tom Hirt for
the changes since. Alex's notice is retained in full, as the licence requires and
as the work deserves.

**Disclaimer.** `schwaby` is an unofficial API wrapper, in no way endorsed by or
affiliated with Charles Schwab or any associated organization. Read and
understand the terms of service of the underlying API before using it.

The software is provided **as is, without warranty of any kind**. The author,
the maintainer and every contributor accept no responsibility or liability for
any loss, damage, missed trade, unintended order, or any other consequence
whatsoever arising from its use — financial or otherwise, foreseeable or not.
Using it against a funded account is entirely at your own risk. See
[LICENSE](LICENSE) for the binding text, and the caution at the top of this file
for what it means in practice.
