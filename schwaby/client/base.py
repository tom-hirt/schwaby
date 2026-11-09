'''Defines the basic client and methods for creating one. This client is
completely unopinionated, and provides an easy-to-use wrapper around the Schwab
HTTP API.'''

from authlib.integrations.base_client.errors import (
        InvalidTokenError, MissingTokenError, OAuthError,
        UnsupportedTokenTypeError)
from enum import Enum

import contextlib
import datetime
import email.utils
import inspect
import json
import math
import os
import re
import schwaby
import threading
import time
import warnings

from schwaby.orders.generic import OrderBuilder

from ..utils import (EnumEnforcer, TokenRefreshError, _expiry_authlib_acts_on,
                     _format_iso_instant, _is_instance, _qualified_type_name,
                     _refresh_kind, _refusal_text, _sent_code,
                     _token_endpoint_answer)
from .. import _redact
from .. import utils as _utils


def get_logger():
    return _redact.get_logger(__name__)


def _retry_after_seconds(value):
    '''The seconds a ``Retry-After`` header asks for, or None when there is
    none or it cannot be read. It is either a number of seconds or an HTTP
    date; a date already past asks for none.'''
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r'[0-9]{1,9}', value):
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None or when.tzinfo is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (when - now).total_seconds())



##########################################################################
# Client

class BaseClient(EnumEnforcer):
    # This docstring will appears as documentation for __init__
    '''A basic, completely unopinionated client. This client provides the most
    direct access to the API possible. All methods return the raw response which
    was returned by the underlying API call, and the user is responsible for
    checking status codes. For methods which support responses, they can be
    found in the response object's ``json()`` method.'''

    def __init__(self, api_key, session, *, enforce_enums=True,
                 token_metadata=None):
        '''Create a new client with the given API key and session. Set
        `enforce_enums=False` to disable strict input type checking.'''
        super().__init__(enforce_enums)

        self.api_key = api_key
        self.session = session

        # Logging-related fields
        self.logger = get_logger()
        self.request_number = 0

        schwaby.LOG_REDACTOR.register(api_key, 'API_KEY')
        _redact.register(api_key, 'APP_KEY')
        _redact.protect_dependencies()

        self.token_metadata = token_metadata

        # Set the default timeout configuration
        self.set_timeout(30.0)

        # ensure_token_fresh's, so two calls of it refresh once.
        self._refresh_lock = threading.Lock()
        # A rate-limited request is not retried unless asked for.
        self._rate_limit_retries = 0
        self._rate_limit_max_wait = 60.0

    # XXX: This class's tests perform monkey patching to inject synthetic values
    # of utcnow(). To avoid being confused by this, these are the values
    # captured before any patch, taken from utils so there is one capture
    # rather than two that could disagree.
    _DATETIME = _utils._DATETIME
    _DATE = _utils._DATE

    def _log_response(self, resp, req_num):
        # Full account numbers are registered before the body is logged, at
        # every level, so this line and any later one -- a stream frame, an
        # error -- carries them masked. Only a body that names one is parsed,
        # spelled as Schwab spells it in every response that carries one; a
        # case-insensitive test would copy every body, option chains included.
        try:
            if b'ccountNumber' in resp.content:
                _redact.register_account_numbers(resp.json())
        except Exception:
            pass
        self.logger.debug('Req %s: GET response: %s, content=%s',
            req_num, resp.status_code, resp.text)

    def _req_num(self):
        self.request_number += 1
        return self.request_number

    @staticmethod
    def _refresh_token_is_invalid(error):
        '''Whether ``error`` means no amount of retrying will get a new token.

        Two ways that happens. The stored token may be unusable before anything
        is sent -- authlib raises MissingTokenError or InvalidTokenError
        locally, and those are handled first below. (UnsupportedTokenTypeError
        is terminal only when authlib will not replace the stored token by
        itself, which only the client can see, so ``_translate_token_errors``
        decides that one.) Otherwise Schwab may have rejected the grant, which
        is the rest of this.

        RFC 6749 section 5.2 defines ``invalid_grant`` as the token endpoint's
        answer when the grant presented -- here, the refresh token -- is
        invalid, expired or revoked. It is the one failure on this path which
        retrying cannot fix, because a new refresh token only comes from the
        full authorization_code flow.

        Schwab does not put that code where the standard says it goes. It
        answers with an outer error of ``unsupported_token_type``, which RFC
        7009 defines for the *revocation* endpoint and which describes nothing
        that happened here, and nests the real response as a JSON string inside
        the description::

            unsupported_token_type: 400 Bad Request: {"error_description":
            "Refresh token is invalid, expired or revoked","error":"invalid_grant"}

        Observed on a live account by letting a refresh token
        reach its seven day expiry deliberately. Schwab does not document this
        response, so both spellings are accepted: the standard placement, in
        case it is ever corrected, and the nested one it actually uses.

        Anything unrecognized returns False. A failure wrongly called terminal
        stops an application which only needed to retry, which is worse than
        one wrongly called transient.
        '''
        # Raised locally by authlib, before anything is sent: the stored
        # token has no refresh token, or cannot be used as it stands. Schwab
        # has said nothing, but the remedy is the same one and retrying is
        # equally futile.
        if isinstance(error, (MissingTokenError, InvalidTokenError)):
            return True

        code = getattr(error, 'error', None)
        if code == 'invalid_grant':
            return True

        description = getattr(error, 'description', None)
        if not isinstance(description, str):
            return False

        # The nested body is embedded in a larger string, so find the object
        # rather than trying to parse the whole description.
        start = description.find('{')
        end = description.rfind('}')
        if start < 0 or end < start:
            return False

        try:
            nested = json.loads(description[start:end + 1])
        except ValueError:
            return False

        return isinstance(nested, dict) and nested.get('error') == 'invalid_grant'

    @contextlib.contextmanager
    def _translate_token_errors(self):
        '''Presents a failed token refresh as a schwaby exception.

        authlib raises OAuthError when Schwab rejects a refresh. That happens
        in the middle of an ordinary request, because the session refreshes the
        token on the way past, so an application which never touches the token
        directly still has to catch it -- and to catch it, has to import
        authlib and know a type this library does not document anywhere.

        Only OAuthError is translated. It never comes from a data request: one
        which fails does not raise at all, since httpx2 reports status through
        the response. Network errors are left as the httpx2 exceptions they are,
        because a connection failure while refreshing and one while fetching a
        quote are the same problem and are not distinguishable from here.

        Not every OAuthError comes from Schwab, though. MissingTokenError and
        InvalidTokenError are subclasses of it which authlib raises locally,
        before any HTTP call, when the stored token has no refresh token or is
        otherwise unusable. Nothing about those improves with time, so they are
        reported as needing a new login rather than as something to retry.

        UnsupportedTokenTypeError is raised locally too, when the stored token
        has no access token of a type authlib can send. It is terminal unless
        authlib will replace that token by itself, which takes an expiry it
        acts on -- one it reads as an int, near enough to be reached -- and a
        refresh token to refresh with. Without both, every call fails the same
        way and nothing is ever sent. With both, the token is refreshed as the
        expiry nears, so it stays retryable. It is told apart by class, not by
        its ``unsupported_token_type`` code, which Schwab uses for its own
        rejections -- one of those has been seen to recover.

        ``unusable_token_response`` does not come from Schwab either. This
        library raises it when the token endpoint answers with something that
        is not a usable token, so that it is refused before authlib stores it,
        and it is reported as retryable: nothing was stored, and the next call
        refreshes again.
        '''
        try:
            yield
        except OAuthError as e:
            age = None
            if self.token_metadata is not None:
                age = self.token_metadata.token_age()

            # float(): a token store such as DynamoDB returns the creation
            # timestamp as a Decimal, and a Decimal cannot be divided by a
            # float.
            detail = ('The token is {:.1f} days old; Schwab documents a '
                      'refresh token as valid for 7 days after it is '
                      'authorized, and refreshing does not extend that.'
                      ).format(float(age) / 86400.0) if age is not None else (
                    'This client was built without token metadata, so the '
                    'token\'s age is unknown.')

            invalid = self._refresh_token_is_invalid(e)
            # A token authlib cannot send, raised locally, is terminal unless
            # authlib will replace it by itself; otherwise every call fails the
            # same way and nothing is ever sent.
            local = isinstance(e, (MissingTokenError, InvalidTokenError)) or (
                    isinstance(e, UnsupportedTokenTypeError)
                    and not self._stored_token_will_be_refreshed())
            if local:
                invalid = True
                advice = ('The stored token cannot be used to refresh -- it '
                          'has no refresh token, or is not in a usable state. '
                          'Nothing was sent to Schwab, and retrying will not '
                          'help: the login flow has to be completed again.')
            elif invalid:
                advice = ('Schwab says the refresh token is invalid, expired '
                          'or revoked, so retrying will not help: the login '
                          'flow has to be completed again.')
            elif isinstance(e, UnsupportedTokenTypeError):
                advice = ('The stored token cannot be sent as it stands, so '
                          'nothing was sent to Schwab. It has an expiry and a '
                          'refresh token, so it is refreshed as that expiry '
                          'nears, and this may clear on its own.')
            else:
                advice = ('Schwab did not say the refresh token itself is '
                          'invalid, so this may be transient.')
            if not invalid and age is not None and age > 7 * 24 * 60 * 60:
                # Still retryable: Schwab has not held exactly to its seven
                # days, and stopping an application that could have recovered
                # is the worse mistake. But someone should know.
                advice += (' The token is past the seven days Schwab '
                           'documents for a refresh token, so alert someone: '
                           'if this keeps failing, the login flow has to be '
                           'completed again.')

            # The chained error prints the text its constructor built from the
            # endpoint's code and description, raw, and this message repeats
            # it; a traceback puts both in logs. Both carry that text escaped
            # and cut, as a login's refusal does. The error keeps its class,
            # attributes and identity, which callers classify a refresh by.
            text = '{}: {}'.format(*_refusal_text(e))
            e.args = (text,)
            answered, status = _token_endpoint_answer(e)
            raise TokenRefreshError(
                    'Failed to refresh the Schwab token: {}. {} {}'.format(
                        text, detail, advice),
                    token_age=age,
                    refresh_token_invalid=invalid,
                    status_code=status,
                    endpoint_answered=answered,
                    kind=_refresh_kind(e, invalid, status),
                    server_error_code=_sent_code(e) if answered else None
                    ) from e

    def _stored_token_will_be_refreshed(self):
        '''Whether authlib will replace the session's token by itself. It
        refreshes one whose expiry it acts on -- asked exactly as the check on
        a token response asks it -- and only with a refresh token. Taken as
        yes when the token cannot be read: calling a failure terminal wrongly
        is the worse mistake.'''
        try:
            token = self.session.token
            refresh_token = token.get('refresh_token')
            # A refresh token authlib can send: a non-empty string, as the
            # check on a token response requires. Anything else raises from
            # authlib, or is sent as whatever bytes() makes of it.
            return bool(_expiry_authlib_acts_on(token.get('expires_at'))
                        and issubclass(type(refresh_token), str)
                        and refresh_token)
        except Exception:
            return True

    def _assert_type(self, name, value, exp_types):
        # Named without trusting the value's metaclass: a class whose
        # __name__ raises must still get the ValueError below.
        value_type_name = _qualified_type_name(value)
        exp_type_names = ['{}.{}'.format(
            t.__module__, t.__name__) for t in exp_types]
        if not any(_is_instance(value, t) for t in exp_types):
            if len(exp_types) == 1:
                error_str = "expected type '{}' for {}, got '{}'".format(
                    exp_type_names[0], name, value_type_name)
            else:
                error_str = "expected type in ({}) for {}, got '{}'".format(
                    ', '.join(exp_type_names), name, value_type_name)
            raise ValueError(error_str)

    #: Directory containing this package, used to find the first stack frame
    #: which is not ours when attributing a warning.
    _PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    @classmethod
    def _caller_stacklevel(cls):
        '''How many frames up the caller's own code is.

        A fixed ``stacklevel`` cannot be right here, because the endpoints do
        not all sit at the same depth: ``get_price_history_every_day`` calls
        ``get_price_history``, and ``get_orders_for_account`` goes through
        ``_make_order_query``, so those are one frame deeper than the methods
        which format their dates directly. A warning attributed to a line
        inside this library tells the caller nothing they can act on, so the
        depth is counted rather than guessed.

        Never returns 0. ``warnings.warn`` reads that as "blame the frame which
        called warn", which is this library -- the exact failure this exists to
        avoid. It would otherwise happen the first time a frame's
        ``co_filename`` cannot be resolved under the package root, which a
        zipimport or a frozen build can manage.

        The separator matters in the prefix test: without it a sibling package
        whose path merely starts the same way, ``.../schwabytools`` against a
        root of ``.../schwaby``, is mistaken for one of ours and skipped, and
        the blame lands on whoever called *that*.

        The count starts at 0 because ``warnings.warn`` is called one frame up,
        by ``_warn_if_naive``, and interprets ``stacklevel`` relative to
        itself. Counting from this frame instead would land one frame beyond
        the caller, on whatever called *them*.
        '''
        frame = inspect.currentframe()
        try:
            level = 0
            while frame is not None:
                filename = os.path.abspath(frame.f_code.co_filename)
                if not filename.startswith(cls._PACKAGE_ROOT + os.sep):
                    return max(level, 1)
                frame = frame.f_back
                level += 1
            return 1
        finally:
            # Frames hold references to their own locals, which includes this
            # one. Drop it rather than leaving a cycle for the collector.
            del frame

    def _warn_if_naive(self, var_name, dt):
        '''Warns that a datetime carrying no timezone is ambiguous.

        Schwab's date parameters name an instant, and a datetime with no
        timezone does not. Naive means ``utcoffset()`` returns ``None``,
        Python's own definition: a ``tzinfo`` that returns no offset is naive
        despite being set, and keying on ``tzinfo`` alone would let such a
        value pass silently and be read as the host's local time.

        The two encodings resolve the ambiguity
        differently -- the epoch-millisecond parameters read a naive datetime
        as the host's local time, the ISO ones send its wall clock labelled as
        UTC; a ``tzinfo`` set but returning no offset raises on the
        millisecond path rather than being read either way -- so the same
        code sends different requests on different machines.
        Rather than pick one and silently reinterpret what the caller wrote,
        say so and let them attach the timezone they meant.
        '''
        if _is_instance(dt, self._DATETIME) and dt.utcoffset() is None:
            warnings.warn(
                    ('{} was given a datetime with no timezone. Schwab\'s date '
                     'parameters identify an instant, so the request depends on '
                     'the timezone of the machine it is sent from. Attach one '
                     'explicitly, for example '
                     'datetime.datetime(..., tzinfo=datetime.timezone.utc) or '
                     'zoneinfo.ZoneInfo(\'America/New_York\'), to get the same '
                     'request everywhere.').format(var_name),
                    stacklevel=self._caller_stacklevel())

    def _format_date_as_iso(self, var_name, dt):
        '''Formats datetime or date objects as yyyy-MM-dd'T'HH:mm:ssZ'''
        self._assert_type(var_name, dt, [self._DATE, self._DATETIME])
        self._warn_if_naive(var_name, dt)

        return _format_iso_instant(dt)

    def _format_date_as_day(self, var_name, dt):
        '''Formats datetime or date objects as YYYY-MM-DD'''
        self._assert_type(var_name, dt, [self._DATE])

        return dt.strftime('%Y-%m-%d')


    def _format_date_as_millis(self, var_name, dt):
        'Converts datetime objects to compatible millisecond values'
        self._assert_type(var_name, dt, [self._DATETIME])
        self._warn_if_naive(var_name, dt)

        # timestamp() reads a naive datetime as local time, which is what
        # _warn_if_naive is about. An aware one converts unambiguously.
        return int(dt.timestamp() * 1000)


    def set_timeout(self, timeout):
        '''Sets the timeout configuration for this client. Applies to all HTTP 
        calls.

        :param timeout: ``httpx2`` timeout configuration. Passed directly to
                        underlying ``httpx2`` library. See
                        `here <https://github.com/pydantic/httpx2/blob/main/
                        docs/advanced/timeouts.md>`__ for examples.'''
        self.session.timeout = timeout

    @staticmethod
    def _check_margin(margin):
        if (not isinstance(margin, (int, float))
                or isinstance(margin, bool)):
            raise TypeError('margin must be a number of seconds, got '
                            '{}'.format(_qualified_type_name(margin)))
        if not math.isfinite(margin) or margin < 0:
            raise ValueError('margin must be a finite number of seconds, '
                             'zero or more, got {!r}'.format(margin))
        return margin

    def _token_is_fresh(self, margin):
        '''Whether the session's token has longer than ``margin`` seconds
        left, by an expiry authlib reads, and longer than the leeway within
        which the session refreshes it itself. One whose expiry cannot be
        read is not.'''
        leeway = getattr(self.session, 'leeway', 0)
        if not isinstance(leeway, (int, float)) or isinstance(leeway, bool):
            leeway = 0
        try:
            expires_at = self.session.token.get('expires_at')
        except Exception:
            return False
        return bool(_expiry_authlib_acts_on(expires_at)
                    and expires_at - time.time() > max(margin, leeway))

    def _refresh_token_to_send(self):
        '''The refresh token to refresh with. A stored token with none is
        refused here as authlib refuses one, before anything is sent, so it is
        reported as the dead token it is.'''
        try:
            refresh_token = self.session.token.get('refresh_token')
        except Exception:
            refresh_token = None
        if not (issubclass(type(refresh_token), str) and refresh_token):
            raise MissingTokenError()
        return refresh_token

    def set_rate_limit_retry(self, retries, *, max_wait=60.0):
        '''Retries a request Schwab rate-limits, up to ``retries`` times. Off
        until this is called with ``retries`` above zero, and off again with
        zero.

        Only what is safe to send again is retried:

        * a GET Schwab answers with HTTP 429. The wait is the ``Retry-After``
          its answer gives, in seconds or as a date; without one, 1 second,
          then 2, 4 and so on, never more than ``max_wait``. When
          ``Retry-After`` asks for longer than ``max_wait``, the 429 is
          returned instead, rather than asking again sooner than Schwab
          said.
        * any request whose token refresh, which happens before the request
          is sent, is answered with HTTP 429. Nothing was sent, so even an
          order is then sent once, after the wait. The refresh's answer
          carries no ``Retry-After`` here, so the wait is the backoff.

        A POST, PUT or DELETE Schwab answers with 429 is never sent again,
        so an order cannot be placed, replaced or cancelled twice by this.
        When the retries run out, the last 429 response is returned, or the
        last :class:`~schwaby.utils.TokenRefreshError` raised, as without
        them. Each wait is logged as a warning, and the thread or, for
        :class:`AsyncClient`, the coroutine waits through it.

        :param retries: how many times to send again after a 429; ``0``
                        turns this off.
        :param max_wait: the longest single wait, in seconds.'''
        if not isinstance(retries, int) or isinstance(retries, bool):
            raise TypeError('retries must be an int, got {}'.format(
                _qualified_type_name(retries)))
        if retries < 0:
            raise ValueError('retries must be zero or more, got {}'.format(
                retries))
        if (not isinstance(max_wait, (int, float))
                or isinstance(max_wait, bool)):
            raise TypeError('max_wait must be a number of seconds, got '
                            '{}'.format(_qualified_type_name(max_wait)))
        if not math.isfinite(max_wait) or max_wait <= 0:
            raise ValueError('max_wait must be a finite number of seconds '
                             'above zero, got {!r}'.format(max_wait))
        self._rate_limit_retries = retries
        self._rate_limit_max_wait = float(max_wait)

    def _retry_wait(self, attempt, retry_after=None):
        '''The seconds to wait before sending again after the 429 that ended
        send number ``attempt``, or None not to send again.'''
        if attempt > self._rate_limit_retries:
            return None
        asked = _retry_after_seconds(retry_after)
        if asked is not None:
            return asked if asked <= self._rate_limit_max_wait else None
        return min(self._rate_limit_max_wait, 2.0 ** (attempt - 1))

    def _response_retry_wait(self, resp, attempt):
        '''The wait before sending a GET again, or None: only after a 429,
        and only when retries were asked for. The response is not read when
        they were not.'''
        if not self._rate_limit_retries or resp.status_code != 429:
            return None
        return self._retry_wait(attempt, resp.headers.get('Retry-After'))

    def _refresh_retry_wait(self, error, attempt):
        '''The wait before sending again after a token refresh the token
        endpoint answered with 429, or None. A refresh that failed any other
        way is not retried here.'''
        if not (error.rate_limited and error.endpoint_answered):
            return None
        return self._retry_wait(attempt)

    def _log_retry(self, req_num, what, wait, attempt):
        self.logger.warning(
                'Req %s: %s rate limited (HTTP 429); sending again in %.1f s, '
                'retry %d of %d', req_num, what, wait, attempt,
                self._rate_limit_retries)

    def token_age(self):
        '''Get the age of the token used to create this client, in seconds. For 
        users who prefer to proactively delete their token files before the 
        become expired, this method can offer a hint for when to do so.

        Note that the actual expiration is governed by Schwab's internal 
        implementation, and the token might become expired sooner *or* later 
        than the documented seven day term.'''
        return self.token_metadata.token_age()


    ##########################################################################
    # Accounts

    class Account:
        class Fields(Enum):
            '''Account fields passed to :meth:`get_account` and
            :meth:`get_accounts`'''
            POSITIONS = 'positions'

    def get_account(self, account_hash, *, fields=None):
        '''Account balances, positions, and orders for a given account hash..

        :param fields: Balances displayed by default, additional fields can be
                       added here by adding values from :class:`Account.Fields`.
        '''
        fields = self.convert_enum_iterable(fields, self.Account.Fields)

        params = {}
        if fields:
            params['fields'] = ','.join(fields)

        path = '/trader/v1/accounts/{}'.format(account_hash)
        return self._get_request(path, params)

    def get_account_numbers(self):
        '''
        Fetches the accounts this token can use. The response's JSON body is a
        list pairing each ``accountNumber`` with the ``hashValue`` to pass
        whenever referring to that account in API calls. Schwab documents no
        order for the list, so
        use :func:`~schwaby.utils.find_account_hash` to find the hash for a
        number rather than taking the first entry.
        '''
        path = '/trader/v1/accounts/accountNumbers'
        return self._get_request(path, {})

    def get_accounts(self, *, fields=None):
        '''Account balances, positions, and orders for all linked accounts. Note 
        this method does not return account hashes. See 
        :ref:`this method <account_hashes_method>` for more detail.

        :param fields: Balances displayed by default, additional fields can be
                       added here by adding values from :class:`Account.Fields`.
        '''
        fields = self.convert_enum_iterable(fields, self.Account.Fields)

        params = {}
        if fields:
            params['fields'] = ','.join(fields)

        path = '/trader/v1/accounts'
        return self._get_request(path, params)


    ##########################################################################
    # Orders

    def get_order(self, order_id, account_hash):
        '''Get a specific order for a specific account by its order ID'''
        path = '/trader/v1/accounts/{}/orders/{}'.format(account_hash, order_id)
        return self._get_request(path, {})

    def cancel_order(self, order_id, account_hash):
        '''Cancel a specific order for a specific account'''
        path = '/trader/v1/accounts/{}/orders/{}'.format(account_hash, order_id)
        return self._delete_request(path)

    class Order:
        class Status(Enum):
            '''Order statuses passed to :meth:`get_orders_for_account` and
            :meth:`get_orders_for_all_linked_accounts`'''
            AWAITING_PARENT_ORDER = 'AWAITING_PARENT_ORDER'
            AWAITING_CONDITION = 'AWAITING_CONDITION'
            AWAITING_STOP_CONDITION = 'AWAITING_STOP_CONDITION'
            AWAITING_MANUAL_REVIEW = 'AWAITING_MANUAL_REVIEW'
            ACCEPTED = 'ACCEPTED'
            AWAITING_UR_OUT = 'AWAITING_UR_OUT'
            PENDING_ACTIVATION = 'PENDING_ACTIVATION'
            QUEUED = 'QUEUED'
            WORKING = 'WORKING'
            REJECTED = 'REJECTED'
            PENDING_CANCEL = 'PENDING_CANCEL'
            CANCELED = 'CANCELED'
            PENDING_REPLACE = 'PENDING_REPLACE'
            REPLACED = 'REPLACED'
            FILLED = 'FILLED'
            EXPIRED = 'EXPIRED'
            NEW = 'NEW'
            AWAITING_RELEASE_TIME = 'AWAITING_RELEASE_TIME'
            PENDING_ACKNOWLEDGEMENT = 'PENDING_ACKNOWLEDGEMENT'
            PENDING_RECALL = 'PENDING_RECALL'
            UNKNOWN = 'UNKNOWN'

        #: The ``Status`` values after which an order's ``status`` stops
        #: changing.
        #:
        #: Values, not members. ``Status`` is a plain ``Enum``, so
        #: ``Status.FILLED == 'FILLED'`` is False, and a set of members would
        #: report the raw string a REST response carries in
        #: ``order['status']`` as not terminal -- silently, with no error.
        #: These are that string, derived from the members so the two
        #: cannot drift apart.
        #:
        #: Schwab's documentation lists every status and never says which are
        #: terminal, so this is a reading rather than a contract. ``FILLED``,
        #: ``CANCELED`` and ``REJECTED`` have been observed ending an order,
        #: and ``REPLACED`` ending each id a price change retired --- five of
        #: five, on one option order; no equity order's replacement has been
        #: captured.
        #: ``EXPIRED`` is :ref:`Unconfirmed <confidence_tags>`: included by
        #: reading, and never captured.
        #:
        #: ``REPLACED`` is terminal for *that order id*. The order continues
        #: under a new id, so anything tracking the old one sees it end while
        #: the work goes on --- and no key captured on the retired order names
        #: its successor. The link is on the stream; see
        #: :ref:`price changes <account_activity_price_change>`.
        #:
        #: This describes the REST ``status`` field. The ``MESSAGE_TYPE``
        #: tokens on ``ACCT_ACTIVITY`` are a different vocabulary and do not
        #: map onto these one for one: the same token ends both a cancel and
        #: a rejection.
        TERMINAL_STATUSES = frozenset(status.value for status in (
            Status.FILLED, Status.REJECTED, Status.CANCELED,
            Status.EXPIRED, Status.REPLACED))

    def _make_order_query(self,
                          *,
                          max_results=None,
                          from_entered_datetime=None,
                          to_entered_datetime=None,
                          status=None):
        status = self.convert_enum(status, self.Order.Status)

        if from_entered_datetime is None:
            from_entered_datetime = (
                    datetime.datetime.now(datetime.timezone.utc) -
                    datetime.timedelta(days=60))
        if to_entered_datetime is None:
            to_entered_datetime = datetime.datetime.now(datetime.timezone.utc)

        params = {
            'fromEnteredTime': self._format_date_as_iso(
                'from_entered_datetime', from_entered_datetime),
            'toEnteredTime': self._format_date_as_iso(
                'to_entered_datetime', to_entered_datetime),
        }

        if max_results:
            params['maxResults'] = max_results

        if status:
            params['status'] = status

        return params

    def get_orders_for_account(self,
                               account_hash,
                               *,
                               max_results=None,
                               from_entered_datetime=None,
                               to_entered_datetime=None,
                               status=None):
        '''Orders for a specific account. Optionally specify a single status on 
        which to filter.

        :param max_results: The maximum number of orders to retrieve.
        :param from_entered_datetime: Specifies that no orders entered before
                                      this time should be returned. Date must
                                      be within 60 days from today's date.
                                      ``toEnteredTime`` must also be set.
        :param to_entered_datetime: Specifies that no orders entered after this
                                    time should be returned. ``fromEnteredTime``
                                    must also be set.
        :param status: Restrict query to orders with this status. See
                       :class:`Order.Status` for options. Schwab accepts only
                       one, so passing a collection raises ``ValueError`` --
                       unless the client was built with ``enforce_enums=False``,
                       which turns off that check along with every other one
                       and forwards whatever it is given.
        '''
        path = '/trader/v1/accounts/{}/orders'.format(account_hash)
        return self._get_request(path, self._make_order_query(
            max_results=max_results,
            from_entered_datetime=from_entered_datetime,
            to_entered_datetime=to_entered_datetime,
            status=status))

    def get_orders_for_all_linked_accounts(self,
                                           *,
                                           max_results=None,
                                           from_entered_datetime=None,
                                           to_entered_datetime=None,
                                           status=None):
        '''Orders for all linked accounts. Optionally specify a single status on 
        which to filter.

        :param max_results: The maximum number of orders to retrieve.
        :param from_entered_datetime: Specifies that no orders entered before
                                      this time should be returned. Date must
                                      be within 60 days from today's date.
                                      ``toEnteredTime`` must also be set.
        :param to_entered_datetime: Specifies that no orders entered after this
                                    time should be returned. ``fromEnteredTime``
                                    must also be set.
        :param status: Restrict query to orders with this status. See
                       :class:`Order.Status` for options. Schwab accepts only
                       one, so passing a collection raises ``ValueError`` --
                       unless the client was built with ``enforce_enums=False``,
                       which turns off that check along with every other one
                       and forwards whatever it is given.
        '''
        path = '/trader/v1/orders'
        return self._get_request(path, self._make_order_query(
            max_results=max_results,
            from_entered_datetime=from_entered_datetime,
            to_entered_datetime=to_entered_datetime,
            status=status))

    def place_order(self, account_hash, order_spec):
        '''Place an order for a specific account. If order creation was
        successful, the response will contain the ID of the generated order. See
        :meth:`schwaby.utils.Utils.extract_order_id` for more details. Note unlike
        most methods in this library, responses for successful calls to this
        method typically do not contain ``json()`` data, and attempting to
        extract it will likely result in an exception.'''
        if isinstance(order_spec, OrderBuilder):
            order_spec = order_spec.build()

        path = '/trader/v1/accounts/{}/orders'.format(account_hash)
        return self._post_request(path, order_spec)

    def replace_order(self, account_hash, order_id, order_spec):
        '''Replace an existing order for an account. The existing order stops
        working and a new order, with a new ID, takes its place. The old ID
        does not carry over, and what it reads afterwards is not necessarily
        ``CANCELED``.'''
        if isinstance(order_spec, OrderBuilder):
            order_spec = order_spec.build()

        path = '/trader/v1/accounts/{}/orders/{}'.format(account_hash, order_id)
        return self._put_request(path, order_spec)

    def preview_order(self, account_hash, order_spec):
        '''Preview an order, i.e. test whether an order would be accepted by the 
        API and see the structure it would result in.'''
        if isinstance(order_spec, OrderBuilder):
            order_spec = order_spec.build()

        path = '/trader/v1/accounts/{}/previewOrder'.format(account_hash)
        return self._post_request(path, order_spec)


    ##########################################################################
    # Transaction History

    class Transactions:
        class TransactionType(Enum):
            TRADE = 'TRADE'
            RECEIVE_AND_DELIVER = 'RECEIVE_AND_DELIVER'
            DIVIDEND_OR_INTEREST = 'DIVIDEND_OR_INTEREST'
            ACH_RECEIPT = 'ACH_RECEIPT'
            ACH_DISBURSEMENT = 'ACH_DISBURSEMENT'
            CASH_RECEIPT = 'CASH_RECEIPT'
            CASH_DISBURSEMENT = 'CASH_DISBURSEMENT'
            ELECTRONIC_FUND = 'ELECTRONIC_FUND'
            WIRE_OUT = 'WIRE_OUT'
            WIRE_IN = 'WIRE_IN'
            JOURNAL = 'JOURNAL'
            MEMORANDUM = 'MEMORANDUM'
            MARGIN_CALL = 'MARGIN_CALL'
            MONEY_MARKET = 'MONEY_MARKET'
            SMA_ADJUSTMENT = 'SMA_ADJUSTMENT'

    def get_transactions(
            self,
            account_hash,
            *,
            start_date=None,
            end_date=None,
            transaction_types=None,
            symbol=None):
        '''Transaction for a specific account.

        :param account_hash: Account hash corresponding to the account whose 
                             transactions should be returned.
        :param start_date: Only transactions after this date will be returned.
                           Date must be within 60 days of the current date. If 
                           this parameter is not set, it will be set to 60 days 
                           prior to now.
                           Accepts ``datetime.date`` and ``datetime.datetime``.
        :param end_date: Only transactions before this date will be returned. If 
                         this parameter is not set, it will be set to the 
                         current time.
                         Accepts ``datetime.date`` and ``datetime.datetime``.
        :param transaction_types: Only transactions with one of the specified 
                                  types will be returned.
        :param symbol: Only transactions with the specified symbol will be
                        returned.
        '''
        # Transaction types
        if transaction_types is None:
            transaction_types = [
                    t.value for t in self.Transactions.TransactionType]
        else:
            transaction_types = self.convert_enum_iterable(
                transaction_types, self.Transactions.TransactionType)

        # Start date
        if start_date is None:
            start_date = self._format_date_as_iso(
                    'start_date',
                    datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(days=60))
        else:
            start_date = self._format_date_as_iso('start_date', start_date)

        # End date
        if end_date is None:
            end_date = self._format_date_as_iso(
                    'end_date', datetime.datetime.now(datetime.timezone.utc))
        else:
            end_date = self._format_date_as_iso('end_date', end_date)

        params = {
                'types':  ','.join(transaction_types),
                'startDate': start_date,
                'endDate': end_date,
        }

        if symbol is not None:
            params['symbol'] = symbol

        path = '/trader/v1/accounts/{}/transactions'.format(account_hash)
        return self._get_request(path, params)

    def get_transaction(self, account_hash, transaction_id):
        '''Transaction for a specific account.

        :param account_hash: Account hash corresponding to the account whose 
                             transactions should be returned.
        :param transaction_id: ID of the transaction for which to return to 
                               return data.
        '''
        path = '/trader/v1/accounts/{}/transactions/{}'.format(
            account_hash, transaction_id)
        return self._get_request(path, {})


    ##########################################################################
    # User Info and Preferences

    def get_user_preferences(self):
        '''Preferences for the logged in account, including all linked
        accounts.'''
        path = '/trader/v1/userPreference'
        return self._get_request(path, {})


    ##########################################################################
    # Quotes

    class Quote:
        class Fields(Enum):
            QUOTE = 'quote'
            FUNDAMENTAL = 'fundamental'
            EXTENDED = 'extended'
            REFERENCE = 'reference'
            REGULAR = 'regular'

    def get_quote(self, symbol, *, fields=None):
        '''
        Get quote for a symbol. Note due to limitations in URL encoding, this
        method is not recommended for instruments with symbols symbols
        containing non-alphanumeric characters, for example as futures like
        ``/ES``. To get quotes for those symbols, use :meth:`Client.get_quotes`.

        :param symbol: Single symbol to fetch
        :param fields: Fields to request. If unset, return all available data. 
                       i.e. all fields. See
                       :class:`Client.Quote.Fields
                       <schwaby.client.Client.Quote.Fields>` for options.
        '''
        fields = self.convert_enum_iterable(fields, self.Quote.Fields)
        if fields:
            params = {'fields': ','.join(fields)}
        else:
            params = {}

        path = '/marketdata/v1/{}/quotes'.format(symbol)
        return self._get_request(path, params)

    def get_quotes(self, symbols, *, fields=None, indicative=None):
        '''Get quote for a symbol. This method supports all symbols, including
        those containing non-alphanumeric characters like ``/ES``.

        :param symbols: Iterable of symbols to fetch.
        :param fields: Fields to request. If unset, return all available data. 
                       i.e. all fields. See
                       :class:`Client.Quote.Fields
                       <schwaby.client.Client.Quote.Fields>` for options.
        '''
        if _is_instance(symbols, str):
            symbols = [symbols]

        params = {
            'symbols': ','.join(symbols)
        }

        fields = self.convert_enum_iterable(fields, self.Quote.Fields)
        if fields:
            params['fields'] = ','.join(fields)

        if indicative is not None:
            if type(indicative) is not bool:
                raise ValueError(
                        'value of \'indicative\' must be either True or False')
            params['indicative'] = 'true' if indicative else 'false'

        path = '/marketdata/v1/quotes'
        return self._get_request(path, params)


    ##########################################################################
    # Option Chains

    class Options:
        class ContractType(Enum):
            CALL = 'CALL'
            PUT = 'PUT'
            ALL = 'ALL'

        class Strategy(Enum):
            SINGLE = 'SINGLE'
            ANALYTICAL = 'ANALYTICAL'
            COVERED = 'COVERED'
            VERTICAL = 'VERTICAL'
            CALENDAR = 'CALENDAR'
            STRANGLE = 'STRANGLE'
            STRADDLE = 'STRADDLE'
            BUTTERFLY = 'BUTTERFLY'
            CONDOR = 'CONDOR'
            DIAGONAL = 'DIAGONAL'
            COLLAR = 'COLLAR'
            ROLL = 'ROLL'

        class StrikeRange(Enum):
            IN_THE_MONEY = 'ITM'
            NEAR_THE_MONEY = 'NTM'
            OUT_OF_THE_MONEY = 'OTM'
            STRIKES_ABOVE_MARKET = 'SAK'
            STRIKES_BELOW_MARKET = 'SBK'
            STRIKES_NEAR_MARKET = 'SNK'
            ALL = 'ALL'

        class Type(Enum):
            STANDARD = 'S'
            NON_STANDARD = 'NS'
            ALL = 'ALL'

        class ExpirationMonth(Enum):
            JANUARY = 'JAN'
            FEBRUARY = 'FEB'
            MARCH = 'MAR'
            APRIL = 'APR'
            MAY = 'MAY'
            JUNE = 'JUN'
            JULY = 'JUL'
            AUGUST = 'AUG'
            SEPTEMBER = 'SEP'
            OCTOBER = 'OCT'
            NOVEMBER = 'NOV'
            DECEMBER = 'DEC'
            ALL = 'ALL'

        class Entitlement(Enum):
            PAYING_PRO = 'PP'
            NON_PRO = 'NP'
            NON_PAYING_PRO = 'PN'

    def get_option_chain(
            self,
            symbol,
            *,
            contract_type=None,
            strike_count=None,
            include_underlying_quote=None,
            strategy=None,
            interval=None,
            strike=None,
            strike_range=None,
            from_date=None,
            to_date=None,
            volatility=None,
            underlying_price=None,
            interest_rate=None,
            days_to_expiration=None,
            exp_month=None,
            option_type=None,
            entitlement=None):
        '''Get option chain for an optionable Symbol.

        :param contract_type: Type of contracts to return in the chain. See
                              :class:`Options.ContractType` for choices.
        :param strike_count: The number of strikes to return above and below
                             the at-the-money price.
        :param include_underlying_quote: Include a quote for the underlying 
                                         alongside the options chain?
        :param strategy: If passed, returns a Strategy Chain. See
                        :class:`Options.Strategy` for choices.
        :param interval: Strike interval for spread strategy chains (see
                         ``strategy`` param).
        :param strike: Return options only at this strike price.
        :param strike_range: Return options for the given range. See
                             :class:`Options.StrikeRange` for choices.
        :param from_date: Only return expirations after this date. For
                          strategies, expiration refers to the nearest term
                          expiration in the strategy. Accepts ``datetime.date``.
        :param to_date: Only return expirations before this date. For
                        strategies, expiration refers to the nearest term
                        expiration in the strategy. Accepts ``datetime.date``.
        :param volatility: Volatility to use in calculations. Applies only to
                           ``ANALYTICAL`` strategy chains.
        :param underlying_price: Underlying price to use in calculations.
                                 Applies only to ``ANALYTICAL`` strategy chains.
        :param interest_rate: Interest rate to use in calculations. Applies only
                              to ``ANALYTICAL`` strategy chains.
        :param days_to_expiration: Days to expiration to use in calculations.
                                   Applies only to ``ANALYTICAL`` strategy
                                   chains
        :param exp_month: Return only options expiring in the specified month. See
                          :class:`Options.ExpirationMonth` for choices.
        :param option_type: Types of options to return. See
                            :class:`Options.Type` for choices.
        :param entitlement: Entitlement of the client.
        '''
        contract_type = self.convert_enum(
            contract_type, self.Options.ContractType)
        strategy = self.convert_enum(strategy, self.Options.Strategy)
        strike_range = self.convert_enum(
            strike_range, self.Options.StrikeRange)
        option_type = self.convert_enum(option_type, self.Options.Type)
        exp_month = self.convert_enum(exp_month, self.Options.ExpirationMonth)
        entitlement = self.convert_enum(entitlement, self.Options.Entitlement)

        params = {
            'symbol': symbol,
        }

        if contract_type is not None:
            params['contractType'] = contract_type
        if strike_count is not None:
            params['strikeCount'] = strike_count
        if include_underlying_quote is not None:
            params['includeUnderlyingQuote'] = include_underlying_quote
        if strategy is not None:
            params['strategy'] = strategy
        if interval is not None:
            params['interval'] = interval
        if strike is not None:
            params['strike'] = strike
        if strike_range is not None:
            params['range'] = strike_range
        if from_date is not None:
            params['fromDate'] = self._format_date_as_day('from_date', from_date)
        if to_date is not None:
            params['toDate'] = self._format_date_as_day('to_date', to_date)
        if volatility is not None:
            params['volatility'] = volatility
        if underlying_price is not None:
            params['underlyingPrice'] = underlying_price
        if interest_rate is not None:
            params['interestRate'] = interest_rate
        if days_to_expiration is not None:
            params['daysToExpiration'] = days_to_expiration
        if exp_month is not None:
            params['expMonth'] = exp_month
        if option_type is not None:
            params['optionType'] = option_type
        if entitlement is not None:
            params['entitlement'] = entitlement

        path = '/marketdata/v1/chains'
        return self._get_request(path, params)


    ##########################################################################
    # Option Expiration Chain

    def get_option_expiration_chain(self, symbol):
        '''Returns the expiration dates available for options on ``symbol``,
        with the type and settlement of each.

        Cheaper than :meth:`get_option_chain` when you only need to know *when*
        contracts expire rather than what they cost, because it returns the
        expiration list alone rather than every strike at every date.

        :param symbol: Underlying symbol, e.g. ``AAPL``.
        '''
        path = '/marketdata/v1/expirationchain'
        return self._get_request(path, {'symbol': symbol})

    ##########################################################################
    # Price History

    class PriceHistory:
        class PeriodType(Enum):
            DAY = 'day'
            MONTH = 'month'
            YEAR = 'year'
            YEAR_TO_DATE = 'ytd'

        class Period(Enum):
            # Daily
            ONE_DAY = 1
            TWO_DAYS = 2
            THREE_DAYS = 3
            FOUR_DAYS = 4
            FIVE_DAYS = 5
            TEN_DAYS = 10

            # Monthly
            ONE_MONTH = 1
            TWO_MONTHS = 2
            THREE_MONTHS = 3
            SIX_MONTHS = 6

            # Year
            ONE_YEAR = 1
            TWO_YEARS = 2
            THREE_YEARS = 3
            FIVE_YEARS = 5
            TEN_YEARS = 10
            FIFTEEN_YEARS = 15
            TWENTY_YEARS = 20

            # Year to date
            YEAR_TO_DATE = 1

        class FrequencyType(Enum):
            MINUTE = 'minute'
            DAILY = 'daily'
            WEEKLY = 'weekly'
            MONTHLY = 'monthly'

        class Frequency(Enum):
            # Minute
            EVERY_MINUTE = 1
            EVERY_FIVE_MINUTES = 5
            EVERY_TEN_MINUTES = 10
            EVERY_FIFTEEN_MINUTES = 15
            EVERY_THIRTY_MINUTES = 30

            # Other frequencies
            DAILY = 1
            WEEKLY = 1
            MONTHLY = 1

    def get_price_history(
            self,
            symbol,
            *,
            period_type=None,
            period=None,
            frequency_type=None,
            frequency=None,
            start_datetime=None,
            end_datetime=None,
            need_extended_hours_data=None,
            need_previous_close=None):
        '''Get price history for a symbol.

        :param period_type: The type of period to show.
        :param period: The number of periods to show. Schwab uses this only to
                       derive a start date when ``start_datetime`` is not given
                       -- its documentation says "if not specified startDate
                       will be (endDate - period)". Supplying both is
                       harmless, since ``start_datetime`` is what takes effect,
                       but the period is then doing nothing.

                       Valid values depend on ``period_type``: ``DAY`` accepts
                       1, 2, 3, 4, 5 and 10; ``MONTH`` accepts 1, 2, 3 and 6;
                       ``YEAR`` accepts 1, 2, 3, 5, 10, 15 and 20; ``YEAR_TO_DATE``
                       accepts 1.
        :param frequency_type: The type of frequency with which a new candle
                               is formed.
        :param frequency: The number of the frequencyType to be included in each
                          candle.
        :param start_datetime: Start date.
        :param end_datetime: End date. Default is previous trading day.
        :param need_extended_hours_data: If true, return extended hours data.
                                         Default is true.
        :param need_previous_close: If true, return the previous close price and 
                                    date.
        '''
        period_type = self.convert_enum(
            period_type, self.PriceHistory.PeriodType)
        period = self.convert_enum(period, self.PriceHistory.Period)
        frequency_type = self.convert_enum(
            frequency_type, self.PriceHistory.FrequencyType)
        frequency = self.convert_enum(
            frequency, self.PriceHistory.Frequency)

        params = {
                'symbol': symbol,
        }

        if period_type is not None:
            params['periodType'] = period_type
        if period is not None:
            params['period'] = period
        if frequency_type is not None:
            params['frequencyType'] = frequency_type
        if frequency is not None:
            params['frequency'] = frequency
        if start_datetime is not None:
            params['startDate'] = self._format_date_as_millis(
                'start_datetime', start_datetime)
        if end_datetime is not None:
            params['endDate'] = self._format_date_as_millis(
                'end_datetime', end_datetime)
        if need_extended_hours_data is not None:
            params['needExtendedHoursData'] = need_extended_hours_data
        if need_previous_close is not None:
            params['needPreviousClose'] = need_previous_close

        path = '/marketdata/v1/pricehistory'
        return self._get_request(path, params)


    ##########################################################################
    # Price history utilities

    def __normalize_start_and_end_datetimes(self, start_datetime, end_datetime):
        '''
        Fills in whichever end of the range the caller left out, and reports
        whether they asked for a range at all.

        The third return value decides whether ``period`` is sent. Schwab
        honours ``period`` and ignores the date range when both are present, so
        a caller who gives a range and still gets a period's worth of data has
        been quietly ignored. When no range is asked for, the synthesized one
        spans decades and is not something to request, so ``period`` is what
        should describe the request instead.
        '''
        requested_range = (start_datetime is not None
                           or end_datetime is not None)

        if start_datetime is None:
            # Explicitly UTC. A naive default would warn the caller about a
            # datetime they never passed -- every call which omits the dates,
            # which is most of them -- and would put the epoch millis this
            # turns into at the mercy of the host's offset, the defect the
            # end_datetime default below avoids the same way.
            start_datetime = datetime.datetime(
                    year=1971, month=1, day=1, tzinfo=datetime.timezone.utc)
        if end_datetime is None:
            # Aware, not utcnow(): utcnow() is deprecated and returns a naive
            # datetime holding a UTC wall time, which _format_date_as_millis
            # (dt.timestamp()) would read as local time, off by the host's UTC
            # offset. An aware datetime converts correctly, as in
            # _make_order_query.
            end_datetime = (datetime.datetime.now(datetime.timezone.utc) +
                    datetime.timedelta(days=7))

        return start_datetime, end_datetime, requested_range


    def get_price_history_every_minute(
            self, symbol, *, start_datetime=None, end_datetime=None, 
            need_extended_hours_data=None, need_previous_close=None):
        '''
        Fetch price history for a stock or ETF symbol at a per-minute
        granularity. This endpoint currently appears to return up to 48 days of
        data.
        '''

        start_datetime, end_datetime, requested_range = \
                self.__normalize_start_and_end_datetimes(
                        start_datetime, end_datetime)

        return self.get_price_history(
                symbol,
                period_type=self.PriceHistory.PeriodType.DAY,
                period=(None if requested_range
                        else self.PriceHistory.Period.ONE_DAY),
                frequency_type=self.PriceHistory.FrequencyType.MINUTE,
                frequency=self.PriceHistory.Frequency.EVERY_MINUTE,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                need_extended_hours_data=need_extended_hours_data, 
                need_previous_close=need_previous_close)


    def get_price_history_every_five_minutes(
            self, symbol, *, start_datetime=None, end_datetime=None, 
            need_extended_hours_data=None, need_previous_close=None):
        '''
        Fetch price history for a stock or ETF symbol at a per-five-minutes
        granularity. This endpoint currently appears to return approximately
        nine months of data.
        '''

        start_datetime, end_datetime, requested_range = \
                self.__normalize_start_and_end_datetimes(
                        start_datetime, end_datetime)

        return self.get_price_history(
                symbol,
                period_type=self.PriceHistory.PeriodType.DAY,
                period=(None if requested_range
                        else self.PriceHistory.Period.ONE_DAY),
                frequency_type=self.PriceHistory.FrequencyType.MINUTE,
                frequency=self.PriceHistory.Frequency.EVERY_FIVE_MINUTES,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                need_extended_hours_data=need_extended_hours_data, 
                need_previous_close=need_previous_close)


    def get_price_history_every_ten_minutes(
            self, symbol, *, start_datetime=None, end_datetime=None, 
            need_extended_hours_data=None, need_previous_close=None):
        '''
        Fetch price history for a stock or ETF symbol at a per-ten-minutes
        granularity. This endpoint currently appears to return approximately
        nine months of data.
        '''

        start_datetime, end_datetime, requested_range = \
                self.__normalize_start_and_end_datetimes(
                        start_datetime, end_datetime)

        return self.get_price_history(
                symbol,
                period_type=self.PriceHistory.PeriodType.DAY,
                period=(None if requested_range
                        else self.PriceHistory.Period.ONE_DAY),
                frequency_type=self.PriceHistory.FrequencyType.MINUTE,
                frequency=self.PriceHistory.Frequency.EVERY_TEN_MINUTES,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                need_extended_hours_data=need_extended_hours_data, 
                need_previous_close=need_previous_close)


    def get_price_history_every_fifteen_minutes(
            self, symbol, *, start_datetime=None, end_datetime=None, 
            need_extended_hours_data=None, need_previous_close=None):
        '''
        Fetch price history for a stock or ETF symbol at a per-fifteen-minutes
        granularity. This endpoint currently appears to return approximately
        nine months of data.
        '''

        start_datetime, end_datetime, requested_range = \
                self.__normalize_start_and_end_datetimes(
                        start_datetime, end_datetime)

        return self.get_price_history(
                symbol,
                period_type=self.PriceHistory.PeriodType.DAY,
                period=(None if requested_range
                        else self.PriceHistory.Period.ONE_DAY),
                frequency_type=self.PriceHistory.FrequencyType.MINUTE,
                frequency=self.PriceHistory.Frequency.EVERY_FIFTEEN_MINUTES,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                need_extended_hours_data=need_extended_hours_data, 
                need_previous_close=need_previous_close)


    def get_price_history_every_thirty_minutes(
            self, symbol, *, start_datetime=None, end_datetime=None, 
            need_extended_hours_data=None, need_previous_close=None):
        '''
        Fetch price history for a stock or ETF symbol at a per-thirty-minutes
        granularity. This endpoint currently appears to return approximately
        nine months of data.
        '''

        start_datetime, end_datetime, requested_range = \
                self.__normalize_start_and_end_datetimes(
                        start_datetime, end_datetime)

        return self.get_price_history(
                symbol,
                period_type=self.PriceHistory.PeriodType.DAY,
                period=(None if requested_range
                        else self.PriceHistory.Period.ONE_DAY),
                frequency_type=self.PriceHistory.FrequencyType.MINUTE,
                frequency=self.PriceHistory.Frequency.EVERY_THIRTY_MINUTES,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                need_extended_hours_data=need_extended_hours_data, 
                need_previous_close=need_previous_close)


    def get_price_history_every_day(
            self, symbol, *, start_datetime=None, end_datetime=None, 
            need_extended_hours_data=None, need_previous_close=None):
        '''
        Fetch price history for a stock or ETF symbol at a daily granularity. 
        The exact period of time over which this endpoint returns data is 
        unclear, although it has been observed returning data as far back as 
        1985 (for ``AAPL``).
        '''

        start_datetime, end_datetime, requested_range = \
                self.__normalize_start_and_end_datetimes(
                        start_datetime, end_datetime)

        return self.get_price_history(
                symbol,
                period_type=self.PriceHistory.PeriodType.YEAR,
                period=(None if requested_range
                        else self.PriceHistory.Period.TWENTY_YEARS),
                frequency_type=self.PriceHistory.FrequencyType.DAILY,
                frequency=self.PriceHistory.Frequency.EVERY_MINUTE,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                need_extended_hours_data=need_extended_hours_data, 
                need_previous_close=need_previous_close)


    def get_price_history_every_week(
            self, symbol, *, start_datetime=None, end_datetime=None, 
            need_extended_hours_data=None, need_previous_close=None):
        '''
        Fetch price history for a stock or ETF symbol at a weekly granularity.
        The exact period of time over which this endpoint returns data is 
        unclear, although it has been observed returning data as far back as 
        1985 (for ``AAPL``).
        '''

        start_datetime, end_datetime, requested_range = \
                self.__normalize_start_and_end_datetimes(
                        start_datetime, end_datetime)

        return self.get_price_history(
                symbol,
                period_type=self.PriceHistory.PeriodType.YEAR,
                period=(None if requested_range
                        else self.PriceHistory.Period.TWENTY_YEARS),
                frequency_type=self.PriceHistory.FrequencyType.WEEKLY,
                frequency=self.PriceHistory.Frequency.EVERY_MINUTE,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                need_extended_hours_data=need_extended_hours_data, 
                need_previous_close=need_previous_close)


    ##########################################################################
    # Movers

    class Movers:
        class Index(Enum):
            DJI = '$DJI'
            COMPX = '$COMPX'
            SPX = '$SPX'
            NYSE = 'NYSE'
            NASDAQ = 'NASDAQ'
            OTCBB = 'OTCBB'
            INDEX_ALL = 'INDEX_ALL'
            EQUITY_ALL = 'EQUITY_ALL'
            OPTION_ALL = 'OPTION_ALL'
            OPTION_PUT = 'OPTION_PUT'
            OPTION_CALL = 'OPTION_CALL'

        class SortOrder(Enum):
            '''Sort by a particular attribute'''
            VOLUME = 'VOLUME'
            TRADES = 'TRADES'
            PERCENT_CHANGE_UP = 'PERCENT_CHANGE_UP'
            PERCENT_CHANGE_DOWN = 'PERCENT_CHANGE_DOWN'

        class Frequency(Enum):
            '''To return movers with the specified directions of up or down'''
            ZERO = 0
            ONE = 1
            FIVE = 5
            TEN = 10
            THIRTY = 30
            SIXTY = 60
        

    def get_movers(self, index, *, sort_order=None, frequency=None):
        '''Get a list of the top ten movers for a given index.

        .. warning::

          **Schwab ignores** ``sort_order`` **and** ``frequency`` **on this
          endpoint.** Both are sent --- they appear in the request URL --- and
          the response is identical whichever values you pass: always the top
          ten by *share* volume, always for the whole session.
          ``PERCENT_CHANGE_DOWN`` does not invert the ranking, and rows come
          back monotonically descending in ``volume`` while
          ``netPercentChange`` is unordered. Measured against a live account
          by polling eleven indices every five minutes for a whole session:
          two different ``sort`` values returned identical lists in all 495
          index-cycles.

          The streaming screener does honour both. If you need a ranking other
          than volume, or a time-resolved bucket, use
          :meth:`~schwaby.streaming.StreamClient.screener_equity_subs`; a
          subscription at frequency ``0`` reproduces this endpoint's output.
          See :ref:`screener`.

        Two fields in the response are easy to misread, and Schwab's own
        documentation does not describe them accurately:

        * ``totalVolume`` is the **index** total, not the instrument's. It is
          identical on every row of a response --- in all 990 responses of the
          session above, without exception. Summing it across rows sums the
          same number ten times.
        * ``marketShare`` is derived, not measured: ``volume / totalVolume *
          100`` exactly. Because it is relative to the index queried, the same
          instrument reports a different ``marketShare`` under ``OPTION_ALL``
          than under ``OPTION_PUT`` while its ``volume`` is unchanged.

        ``volume`` is the instrument's own share volume, and ``trades`` its
        own trade count.

        Two things about the values themselves, from the same session:

        * **The membership barely moves.** Across four hours of five-minute
          polls, the median change between consecutive polls was zero names
          and the maximum was one, with only twelve or thirteen distinct
          symbols appearing all session. That follows from ranking on
          cumulative session volume, which only grows. Polling this endpoint
          frequently gets you the same list.
        * ``INDEX_ALL`` **does not return indices.** It returns equities, it
          is not the same set as ``EQUITY_ALL``, and none of its members
          appeared in the ``NYSE`` list at any point while eight or nine of
          ten appeared in ``NASDAQ``. What it actually selects is not
          documented and has not been established here.

        :param index: Category of mover. See :class:`Movers.Index` for valid
                      values.
        :param sort_order: Requested ranking. See :class:`Movers.SortOrder`
                           for valid values. **Ignored by Schwab** --- see the
                           warning above.
        :param frequency: Requested time bucket, in minutes, ``0`` meaning the
                          whole session. See :class:`Movers.Frequency` for
                          valid values. **Ignored by Schwab** --- see the
                          warning above. (A bucket, not a threshold: it does
                          not select movers that saw this magnitude or
                          greater.)
        '''
        index = self.convert_enum(index, self.Movers.Index)
        sort_order = self.convert_enum(sort_order, self.Movers.SortOrder)
        frequency = self.convert_enum(frequency, self.Movers.Frequency)

        path = '/marketdata/v1/movers/{}'.format(index)

        params = {}
        if sort_order is not None:
            params['sort'] = sort_order
        if frequency is not None:
            params['frequency'] = str(frequency)

        return self._get_request(path, params)


    ##########################################################################
    # Market Hours

    class MarketHours:
        class Market(Enum):
            EQUITY = 'equity'
            OPTION = 'option'
            BOND = 'bond'
            FUTURE = 'future'
            FOREX = 'forex'

    def get_market_hours(self, markets, *, date=None):
        '''Retrieve market hours for specified markets

        :param markets: Markets for which to return trading hours.
        :param date: Date for which to return market hours. Accepts values up to 
                     one year from today. Accepts ``datetime.date``.
        '''
        markets = self.convert_enum_iterable(markets, self.MarketHours.Market)

        params = {
                'markets': ','.join(markets)
        }
        if date is not None:
            params['date'] = self._format_date_as_day('date', date)

        return self._get_request('/marketdata/v1/markets', params)


    ##########################################################################
    # Instrument

    class Instrument:
        class Projection(Enum):
            SYMBOL_SEARCH = 'symbol-search'
            SYMBOL_REGEX = 'symbol-regex'
            DESCRIPTION_SEARCH = 'desc-search'
            DESCRIPTION_REGEX = 'desc-regex'
            SEARCH = 'search'
            FUNDAMENTAL = 'fundamental'

    def get_instruments(self, symbols, projection):
        '''Get instrument details by using different search methods. Also used 
        to get fundamental instrument data by use of the ``FUNDAMENTAL`` 
        projection.

        :param symbols: For ``FUNDAMENTAL`` projection, the symbol for which
                        to get fundamentals. For other projections, a search
                        term. See below for details.
        :param projection: Search mode, or ``FUNDAMENTAL`` for instrument 
                           fundamentals. See :class:`Instrument.Projection`.

        This method is a bit of a chimera because it supports both symbol 
        search and fundamentals lookup. The format and interpretation of the 
        ``symbol`` parameter differs according ot the value of the 
        ``projection`` parameter:

        .. list-table::
           :widths: 25 25 50
           :header-rows: 1

           * - ``projection`` value
             - Accepted values of ``symbol``
             - ``symbol`` interpretation
           * - ``SYMBOL_SEARCH``
             - String, or array of strings
             - Symbols for which to search results
           * - ``SYMBOL_REGEX``
             - Single string
             - Regular expression to match against symbols
           * - ``DESCRIPTION_SEARCH``
             - Single string
             - String to search for in symbol description
           * - ``DESCRIPTION_REGEX``
             - Single string
             - Regex to search for in symbol description
           * - ``SEARCH``
             - Single string
             - Regular expression to match against symbols
           * - ``FUNDAMENTAL``
             - String, or array of strings
             - Symbols for which to return fundamentals. Exact match.
        '''
        if _is_instance(symbols, str):
            symbols = [symbols]

        projection = self.convert_enum(projection, self.Instrument.Projection)

        params = {
                'symbol': ','.join(symbols),
                'projection': projection,
        }

        return self._get_request('/marketdata/v1/instruments', params)


    def get_instrument_by_cusip(self, cusip):
        '''Get instrument information for a single instrument by CUSIP.

        :param cusip: String representing CUSIP of instrument for which to fetch 
                      data. Note leading zeroes must be preserved.
        '''
        if not _is_instance(cusip, str):
            raise ValueError('cusip must be passed as str')

        return self._get_request(
                '/marketdata/v1/instruments/{}'.format(cusip), {})
