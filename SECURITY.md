# Security Policy

`schwaby` holds OAuth credentials for brokerage accounts and can place, replace
and cancel orders with real money. A defect here is not an availability problem
— it is somebody's positions. Please treat reports accordingly, and we will.

## Reporting a vulnerability

**Do not open a public issue.**

Use [private vulnerability
reporting](https://github.com/tom-hirt/schwaby/security/advisories/new), which
opens a report only the maintainer can see.

Useful reports say what an attacker ends up able to do, how to reproduce it, and
which version you were on. A proof of concept is welcome and never required — a
clear description of the mechanism is worth more than a working exploit, and if
you are unsure whether something counts, send it anyway.

**Never include your own token file, app secret or account numbers**, in a
report or anywhere else. If reproducing needs credentials, say so and use
obviously fake ones.

This is a one-maintainer project, so the honest expectation is: acknowledged
within a few days, fixed as fast as the fix can be verified, and credited in the
changelog unless you would rather not be. There is no bounty.

## What is in scope

Anything that could expose a token, an app secret, an account number or an
account hash — or that could cause an order to be placed, modified or cancelled
other than as the caller asked. Concretely:

- credentials written somewhere readable by other users, or left recoverable
  after they should be gone
- secrets reaching logs, exception messages or tracebacks unredacted
- **an order built or sent with values the caller did not specify**

That last one carries the weight, and it is not theoretical. Every example below
shipped, passed its tests, and was found later:

| Release | What it did |
|---|---|
| 1.6.0 | Limit prices passed as floats were truncated to a **cent low** |
| 1.7.0 | Response redaction never ran — the function was imported and then shadowed by a no-op of the same name |
| 1.9.0 | Bug-report logs did not redact Schwab's account identifiers |
| 2.1.0 | `OptionSymbol.build()` mis-encoded 590 strikes, naming **a different contract** on the order-placement path |
| 3.0.0 | The venue enum was written to a field that does not select a venue, so an order asking for a specific exchange was routed elsewhere with nothing to say so |

Note the shape they share. None raised, none failed a test, and none produced an
error anybody could see. **A silent wrong value is the most serious class of bug
this library has**, and a report that one exists is worth sending even when you
cannot say how an attacker would trigger it deliberately.

## What is not in scope

- Schwab's own API, its authentication, or its rate limits — report those to
  [Schwab](https://developer.schwab.com/)
- Anything that requires an attacker to already have your token file. That file
  is a bearer credential for a brokerage account; if someone else has it, they
  have the account, and no change here helps
- Losing money on a trade the library placed exactly as instructed
- Dependency advisories with no path to exploitation through this library — send
  them as an ordinary issue, they are still welcome

## Supported versions

The latest release, and nothing else. There are no backports and no long-term
branches: a fix ships in the next version and you upgrade to get it. Practically
that means pinning an exact version is fine, but pinning one forever is not a
security position.

## Handling your token

Most reports in this area turn out to be configuration rather than defects, so
these are worth stating plainly:

- **The token file is a credential.** Anyone holding it can trade your account
  until it expires. It is written mode `0600`, which stops other users on the
  machine reading it and does nothing about backups, container images, or a
  laptop somebody else can unlock.
- **Never commit it.** Never paste it into an issue, a gist, or a chat.
- **One token, one machine.** Copying a token to a second machine and using both
  is a good way to invalidate it at an unpredictable moment. Copy it, then
  delete the original.
- **`enable_bug_report_logging()` is best effort**, and says so in its own
  documentation. It exists to make logs safe enough to share, not to guarantee
  it. Read what you are about to post.
- **The seven-day window is not a security control**, but it is worth knowing:
  Schwab's refresh token expires seven days after the original authorization and
  refreshing does not extend it. A leaked token stops working within a week,
  which bounds the damage without limiting it.
