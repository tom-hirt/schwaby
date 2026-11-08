'''The rules the always-on log redaction follows, one at a time.

`secret_logging_test` proves the whole: a session logs no secret. These pin the
parts, so that each rule has a test that fails when it alone is broken.
'''
import importlib
import io
import json
import logging
import pkgutil
import unittest
import uuid

import schwaby
from schwaby import _redact
from schwaby import streaming
from schwaby.streaming import _prepare_connect_args

_register_stream_accounts = _redact.register_stream_accounts

from .utils import no_duplicates


def fresh(prefix='S'):
    '''A value no other test has registered.'''
    return prefix + uuid.uuid4().hex[:14]


def emit(logger, *args, **kwargs):
    '''What a handler on ``logger`` writes for one record. ``logger`` can be
    an adapter, which is logged through; the handler goes on the logger
    beneath.'''
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.setFormatter(logging.Formatter('%(message)s'))
    beneath = logger
    while isinstance(beneath, logging.LoggerAdapter):
        beneath = beneath.logger
    saved = beneath.level, beneath.propagate
    beneath.addHandler(handler)
    beneath.setLevel(logging.DEBUG)
    beneath.propagate = False
    try:
        logger.debug(*args, **kwargs)
    finally:
        beneath.removeHandler(handler)
        beneath.setLevel(saved[0])
        beneath.propagate = saved[1]
    return out.getvalue()


class RedactTest(unittest.TestCase):

    @no_duplicates
    def test_before_cut_reads_a_str_subclass_as_plain_text(self):
        # Every bounded value passes through here, and venue text can be a
        # subclass whose own length and slicing run if anything uses them.
        class Hostile(str):
            def __len__(self):
                raise RuntimeError('boom')

            def __getitem__(self, index):
                raise RuntimeError('boom')

            def __contains__(self, other):
                raise RuntimeError('boom')
        text = _redact.before_cut(Hostile('some venue text'), 200)
        self.assertIs(str, type(text))
        self.assertEqual('some venue text', text)

    @no_duplicates
    def test_a_registered_value_is_replaced_with_its_label(self):
        secret = fresh()
        _redact.register(secret, 'ACCESS_TOKEN')
        self.assertEqual('token <REDACTED ACCESS_TOKEN> sent',
                         _redact.redact('token {} sent'.format(secret)))

    @no_duplicates
    def test_its_url_encoded_spellings_are_replaced_too(self):
        # A token in a URL or a form body arrives percent-encoded.
        secret = fresh() + '+/='
        _redact.register(secret, 'REFRESH_TOKEN')
        encoded = secret.replace('+', '%2B').replace('/', '%2F').replace(
            '=', '%3D')
        self.assertEqual('<REDACTED REFRESH_TOKEN>', _redact.redact(encoded))

    @no_duplicates
    def test_a_short_value_is_not_registered(self):
        # Replacing every occurrence of a short string would mangle the log.
        _redact.register('abc12', 'X')
        self.assertEqual('abc12', _redact.redact('abc12'))

    @no_duplicates
    def test_an_account_number_does_not_take_digits_out_of_a_longer_number(
            self):
        number = str(10 ** 7 + uuid.uuid4().int % (9 * 10 ** 7))
        _redact.register(number, 'ACCOUNT_NUMBER')
        self.assertEqual('acct <REDACTED ACCOUNT_NUMBER>, ts 1{}9'.format(
            number), _redact.redact('acct {}, ts 1{}9'.format(number, number)))

    @no_duplicates
    def test_the_longest_registered_value_wins(self):
        short = fresh('P')
        _redact.register(short, 'SHORT')
        _redact.register(short + 'tail', 'LONG')
        self.assertEqual('<REDACTED LONG>', _redact.redact(short + 'tail'))

    @no_duplicates
    def test_a_secret_url_parameter_is_masked_unregistered(self):
        self.assertEqual('GET /cb?code=<REDACTED>&session=s HTTP/1.1',
                         _redact.redact('GET /cb?code=zz9q&session=s HTTP/1.1'))
        self.assertEqual('x?access_token=<REDACTED>',
                         _redact.redact('x?access_token=abc'))
        # A parameter whose name only ends in a secret one is left alone.
        for text in ('zipcode=12345', 'error_code=5', 'statuscode=200'):
            self.assertEqual(text, _redact.redact(text))

    @no_duplicates
    def test_a_redaction_is_not_redacted_again(self):
        # The registered code inside a URL is replaced first; the pattern for
        # `code=` must leave that label whole rather than cut it at its space.
        code = fresh('C')
        _redact.register(code, 'AUTHORIZATION_CODE')
        self.assertEqual('/cb?code=<REDACTED AUTHORIZATION_CODE>&state=s',
                         _redact.redact('/cb?code={}&state=s'.format(code)))

    @no_duplicates
    def test_a_secret_json_value_is_masked_unregistered(self):
        self.assertEqual(
            '{"Authorization": "<REDACTED>", "SchwabClientChannel": "N9"}',
            _redact.redact(
                '{"Authorization": "tok3n", "SchwabClientChannel": "N9"}'))
        self.assertEqual('{"access_token":"<REDACTED>"}',
                         _redact.redact('{"access_token":"tok3n"}'))
        # `code` is Schwab's response code in a frame, and is left alone.
        self.assertEqual('{"code": "0"}', _redact.redact('{"code": "0"}'))

    @no_duplicates
    def test_the_registry_forgets_old_values_per_label(self):
        label = 'CAPPED-' + fresh()
        values = [fresh('V') for _ in range(_redact._PER_LABEL + 10)]
        for value in values:
            _redact.register(value, label)
        # The newest are still masked; the oldest have been dropped.
        self.assertEqual('<REDACTED {}>'.format(label),
                         _redact.redact(values[-1]))
        self.assertEqual(values[0], _redact.redact(values[0]))

    @no_duplicates
    def test_a_token_label_keeps_only_its_newest_few(self):
        # A token is dead within the hour, and each registered value is a cost
        # on every line logged.
        values = [fresh('A') for _ in range(5)]
        for value in values:
            _redact.register(value, 'ACCESS_TOKEN')
        self.assertEqual('<REDACTED ACCESS_TOKEN>', _redact.redact(values[-1]))
        self.assertEqual('<REDACTED ACCESS_TOKEN>', _redact.redact(values[1]))
        self.assertEqual(values[0], _redact.redact(values[0]))

    @no_duplicates
    def test_mask_replaces_secret_keys_by_name_whatever_their_case(self):
        masked = _redact.mask({'requests': [{'parameters': {
            'Authorization': 'tok', 'SchwabClientChannel': 'N9'}}],
            'ACCESS_TOKEN': 'a', 'code': 0})
        self.assertEqual({'requests': [{'parameters': {
            'Authorization': '<REDACTED Authorization>',
            'SchwabClientChannel': 'N9'}}],
            'ACCESS_TOKEN': '<REDACTED ACCESS_TOKEN>',
            # A response code is an integer, and is not a secret.
            'code': 0}, masked)

    @no_duplicates
    def test_mask_handles_a_namedtuple(self):
        # A custom decoder can return one, and a tuple subclass cannot be
        # rebuilt from a single iterable.
        import collections
        Pair = collections.namedtuple('Pair', 'a b')
        self.assertEqual({'x': [1, {'Authorization': '<REDACTED '
                                    'Authorization>'}]},
                         _redact.mask({'x': Pair(1, {'Authorization': 't'})}))

    @no_duplicates
    def test_no_account_number_is_ever_dropped(self):
        numbers = [str(10 ** 7 + uuid.uuid4().int % (9 * 10 ** 7))
                   for _ in range(_redact._PER_LABEL + 10)]
        _redact.register_account_numbers(
            [{'accountNumber': n} for n in numbers])
        self.assertEqual('<REDACTED ACCOUNT_NUMBER>',
                         _redact.redact(numbers[0]))

    @no_duplicates
    def test_mask_does_not_change_its_argument(self):
        original = {'parameters': {'Authorization': 'tok'}}
        _redact.mask(original)
        self.assertEqual({'parameters': {'Authorization': 'tok'}}, original)

    @no_duplicates
    def test_account_numbers_are_found_anywhere_in_a_response(self):
        text_number = str(70000000 + uuid.uuid4().int % 10 ** 7)
        int_number = 80000000 + uuid.uuid4().int % 10 ** 7
        _redact.register_account_numbers([
            {'accountNumber': text_number, 'hashValue': 'HASH'},
            {'securitiesAccount': {'AccountNumber': int_number}}])
        self.assertEqual(
            '<REDACTED ACCOUNT_NUMBER> <REDACTED ACCOUNT_NUMBER> HASH',
            _redact.redact('{} {} HASH'.format(text_number, int_number)))

    @no_duplicates
    def test_an_account_activity_frame_registers_its_account(self):
        number = str(60000000 + uuid.uuid4().int % 10 ** 7)
        _register_stream_accounts({'data': [{
            'service': 'ACCT_ACTIVITY', 'content': [{'1': number}]}]})
        self.assertEqual('<REDACTED ACCOUNT_NUMBER>', _redact.redact(number))

    @no_duplicates
    def test_a_malformed_item_does_not_stop_the_items_after_it(self):
        for bad in (None, 'x', [1]):
            with self.subTest(bad=bad):
                number = str(10 ** 7 + uuid.uuid4().int % (9 * 10 ** 7))
                _register_stream_accounts({'data': [
                    'not an entry',
                    {'service': 'ACCT_ACTIVITY',
                     'content': [bad, {'1': number}]}]})
                self.assertEqual('<REDACTED ACCOUNT_NUMBER>',
                                 _redact.redact(number))

    @no_duplicates
    def test_a_quote_frame_registers_nothing(self):
        # Field 1 of a quote is a bid price, not an account.
        value = str(50000000 + uuid.uuid4().int % 10 ** 7)
        _register_stream_accounts({'data': [{
            'service': 'LEVELONE_EQUITIES', 'content': [{'1': value}]}]})
        self.assertEqual(value, _redact.redact(value))

    @no_duplicates
    def test_a_malformed_frame_does_not_raise(self):
        for frame in ({'data': 'x'}, {'data': [{'content': [None]}]}, [],
                      None, {'data': [{'service': 'ACCT_ACTIVITY',
                                       'content': 'x'}]}):
            _register_stream_accounts(frame)


class RedactingFilterTest(unittest.TestCase):

    def setUp(self):
        self.logger = _redact.get_logger('schwaby.test.' + fresh())
        self.secret = fresh()
        _redact.register(self.secret, 'APP_SECRET')

    @no_duplicates
    def test_an_argument_is_redacted_before_the_handler_formats_it(self):
        self.assertEqual('key <REDACTED APP_SECRET>\n',
                         emit(self.logger, 'key %s', self.secret))

    @no_duplicates
    def test_a_traceback_is_redacted(self):
        try:
            raise ValueError('refused ' + self.secret)
        except ValueError:
            out = emit(self.logger, 'failed', exc_info=True)
        self.assertIn('ValueError: refused <REDACTED APP_SECRET>', out)
        self.assertNotIn(self.secret, out)

    @no_duplicates
    def test_a_stack_is_redacted(self):
        # A stack shows function names and source lines, never values, so
        # the secret is put where a stack does show it: a function's name.
        namespace = {'emit': emit, 'logger': self.logger}
        exec('def frame_{}():\n'
             '    return emit(logger, "here", stack_info=True)\n'.format(
                 self.secret), namespace)
        out = namespace['frame_' + self.secret]()
        self.assertIn('Stack', out)
        self.assertIn('frame_<REDACTED APP_SECRET>', out)
        self.assertNotIn(self.secret, out)

    @no_duplicates
    def test_the_exception_is_dropped_once_its_text_is_redacted(self):
        # A formatter that renders exc_info itself -- a JSON one, an error
        # tracker's -- would otherwise print the exception unredacted.
        try:
            raise KeyError(self.secret)
        except KeyError:
            out = io.StringIO()
            handler = logging.StreamHandler(out)
            seen = []
            handler.emit = lambda record: seen.append(
                (record.exc_info, record.exc_text))
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.DEBUG)
            try:
                self.logger.exception('failed')
            finally:
                self.logger.removeHandler(handler)
        exc_info, exc_text = seen[0]
        self.assertIsNone(exc_info)
        self.assertIn('KeyError', exc_text)
        self.assertNotIn(self.secret, exc_text)

    @no_duplicates
    def test_a_record_that_cannot_be_formatted_is_left_to_logging(self):
        # logging's own error report names the call site and prints the
        # message and its arguments; what it prints is redacted.
        import contextlib
        stderr = io.StringIO()
        handler = logging.StreamHandler(io.StringIO())
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        try:
            with contextlib.redirect_stderr(stderr):
                self.logger.debug('%d', self.secret)
        finally:
            self.logger.removeHandler(handler)
            self.logger.propagate = True
        self.assertIn('--- Logging error ---', stderr.getvalue())
        self.assertIn('<REDACTED APP_SECRET>', stderr.getvalue())
        self.assertNotIn(self.secret, stderr.getvalue())

    @no_duplicates
    def test_a_callers_filter_put_there_first_sees_it_redacted(self):
        # A dictConfig naming one of this library's loggers creates it, with
        # its filters, before this library asks for it.
        logger = logging.getLogger('schwaby.test.' + fresh())
        seen = []
        theirs = logging.Filter()
        theirs.filter = lambda record: seen.append(record.getMessage()) or True
        logger.addFilter(theirs)
        _redact.protect(logger)
        self.assertIs(_redact._FILTER, logger.filters[0])
        emit(logger, 'key %s', self.secret)
        self.assertEqual(['key <REDACTED APP_SECRET>'], seen)

    @no_duplicates
    def test_a_pickled_record_carries_only_redacted_text(self):
        # A record sent to another process is rendered there, against a
        # registry that holds nothing -- and a LazyLog does not pickle.
        import pickle
        import websockets.frames
        from schwaby.utils import LazyLog
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)
        try:
            self.logger.debug('key %s', LazyLog(lambda: self.secret))
        finally:
            self.logger.removeHandler(handler)
        frame = _redact._RedactedFrame(websockets.frames.Frame(
            websockets.frames.Opcode.TEXT,
            '{{"Authorization": "{}"}}'.format(self.secret).encode()))
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with self.subTest(protocol=protocol):
                data = pickle.dumps(records[0], protocol)
                self.assertNotIn(self.secret.encode(), data)
                self.assertEqual('key <REDACTED APP_SECRET>',
                                 pickle.loads(data).getMessage())
                data = pickle.dumps(frame, protocol)
                self.assertNotIn(self.secret.encode(), data)
                self.assertIn('Authorization', pickle.loads(data))

    @no_duplicates
    def test_a_record_rendered_outside_a_handler_is_redacted(self):
        # A filter, or an error tracker reading records, can render a record
        # that no handler's level accepts. Nothing decides in advance which
        # records will be written, so that one is redacted too.
        handler = logging.StreamHandler(io.StringIO())
        handler.setLevel(logging.INFO)
        rendered = []
        tap = logging.Filter()
        tap.filter = lambda record: rendered.append(record.getMessage()) or True
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addFilter(tap)
        try:
            self.logger.debug('key %s', self.secret)
        finally:
            self.logger.removeFilter(tap)
            self.logger.removeHandler(handler)
            self.logger.propagate = True
        self.assertEqual(['key <REDACTED APP_SECRET>'], rendered)

    @no_duplicates
    def test_a_warning_with_no_handler_anywhere_is_still_redacted(self):
        # With no handler in the chain, logging's last resort prints a warning
        # to stderr, which renders the record like any handler.
        import contextlib
        stderr = io.StringIO()
        self.logger.propagate = False
        try:
            with contextlib.redirect_stderr(stderr):
                self.logger.warning('key %s', self.secret)
        finally:
            self.logger.propagate = True
        self.assertIn('key <REDACTED APP_SECRET>', stderr.getvalue())

    @no_duplicates
    def test_a_record_no_handler_writes_is_not_rendered(self):
        # Root at DEBUG with every handler at INFO enables the logger and
        # writes nothing; rendering then would evaluate a lazy argument for a
        # line that goes nowhere.
        rendered = []

        class Lazy:
            def __str__(self):
                rendered.append(1)
                return 'x'

        handler = logging.StreamHandler(io.StringIO())
        handler.setLevel(logging.INFO)
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        try:
            self.logger.debug('%s', Lazy())
            self.assertEqual([], rendered)
            # Positive control: a level the handler writes is rendered.
            self.logger.info('%s', Lazy())
            self.assertEqual([1], rendered)
        finally:
            self.logger.removeHandler(handler)
            self.logger.propagate = True

    @no_duplicates
    def test_protect_adds_the_filter_once(self):
        logger = logging.getLogger('schwaby.test.' + fresh())
        _redact.protect(logger)
        _redact.protect(logger)
        self.assertEqual(1, sum(isinstance(f, _redact.RedactingFilter)
                                for f in logger.filters))


_LOGGING_AROUND = {'getLogger', 'Logger', 'debug', 'info', 'warning', 'warn',
                   'error', 'exception', 'critical', 'fatal', 'log'}


class EveryLoggerTest(unittest.TestCase):

    @no_duplicates
    def test_every_logger_this_library_creates_is_protected(self):
        # Every module logs through its own get_logger, which protects the
        # logger before returning it -- so protection holds whenever the
        # module logs, without creating loggers at import, which a default
        # dictConfig run afterwards would disable.
        loggers = {}
        for module in pkgutil.walk_packages(schwaby.__path__, 'schwaby.'):
            mod = importlib.import_module(module.name)
            if mod is not _redact and callable(getattr(mod, 'get_logger',
                                                       None)):
                loggers[module.name] = mod.get_logger()
        # Positive control: the walk found the modules that log.
        self.assertLessEqual({'schwaby.auth', 'schwaby.client.base',
                              'schwaby.streaming', 'schwaby.utils',
                              'schwaby.debug', 'schwaby.contrib.util'},
                             set(loggers))
        unprotected = [name for name, logger in loggers.items()
                       if _redact._FILTER not in logger.filters]
        self.assertEqual([], unprotected)

    @no_duplicates
    def test_nothing_in_this_library_logs_around_get_logger(self):
        # The test above holds only if nothing logs on a logger it found by
        # name. Read from the source, which is where a new call would appear.
        import ast
        import os
        root = os.path.dirname(schwaby.__file__)
        found, read = [], 0
        for directory, _, files in os.walk(root):
            for name in files:
                if not name.endswith('.py') or name == '_redact.py':
                    continue
                path = os.path.join(directory, name)
                read += 1
                with open(path) as f:
                    tree = ast.parse(f.read())
                for node in ast.walk(tree):
                    # A logger found by name or made directly, and the root
                    # logger's own functions, all log around get_logger.
                    if (isinstance(node, ast.Attribute)
                            and isinstance(node.value, ast.Name)
                            and node.value.id == 'logging'
                            and node.attr in _LOGGING_AROUND):
                        found.append('{}:{}'.format(
                            os.path.relpath(path, root), node.lineno))
                    if (isinstance(node, ast.ImportFrom)
                            and node.module == 'logging'
                            and any(a.name in _LOGGING_AROUND
                                    for a in node.names)):
                        found.append('{}:{}'.format(
                            os.path.relpath(path, root), node.lineno))
        self.assertGreater(read, 10)
        self.assertEqual([], found)

    @no_duplicates
    def test_a_default_dictconfig_after_import_leaves_logging_on(self):
        # dictConfig disables every logger that exists when it runs, unless
        # told otherwise, and the usual order is import, then configure.
        import subprocess
        import sys
        script = ('import logging, logging.config, schwaby\n'
                  'logging.config.dictConfig({"version": 1})\n'
                  'from schwaby import auth, streaming, utils\n'
                  'print([m.get_logger().disabled for m in '
                  '(auth, streaming, utils)])\n')
        out = subprocess.run([sys.executable, '-c', script], check=True,
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual('[False, False, False]', out)

    @no_duplicates
    def test_the_dependencies_loggers_are_protected_from_import(self):
        # In a fresh process, before any client exists, since making one
        # protects them too and would hide the import's protection.
        import subprocess
        import sys
        # Loggers only: the dict also holds placeholders for the names between
        # them, and asking for one would create an unprotected logger.
        script = ('import logging, schwaby\n'
                  'from schwaby import _redact\n'
                  'found = [v for n, v in logging.root.manager.loggerDict'
                  '.items() if isinstance(v, logging.Logger) and'
                  ' (n == "httpx2" or n.startswith("authlib."))]\n'
                  'print(len(found), sum(_redact._FILTER in v.filters'
                  ' for v in found))\n')
        out = subprocess.run([sys.executable, '-c', script], check=True,
                             capture_output=True, text=True).stdout.split()
        found, protected = int(out[0]), int(out[1])
        self.assertGreater(found, 1)
        self.assertEqual(found, protected)

    @no_duplicates
    def test_the_dependencies_loggers_are_protected(self):
        # httpcore2 creates its loggers when httpx2 builds its first
        # transport, after this library was imported. In use that is the
        # session a client is made from, so a client is made from one here
        # the same way, and the protection comes from the client, not from
        # anything this test calls.
        import httpx2
        from schwaby.client import Client
        Client(fresh('K'), httpx2.Client())
        names = [name for name, logger in
                 logging.root.manager.loggerDict.items()
                 if isinstance(logger, logging.Logger)
                 and name.split('.', 1)[0] in _redact.DEPENDENCY_LOGGERS]
        # Positive control: each dependency's loggers were found.
        for name in ('httpx2', 'httpcore2.http11', 'httpcore2.connection'):
            self.assertIn(name, names)
        self.assertTrue(any(name.startswith('authlib.') for name in names))
        unprotected = [
            name for name in names
            if _redact._FILTER not in logging.getLogger(name).filters]
        self.assertEqual([], unprotected)


class RegistrationTest(unittest.TestCase):
    """Each place a secret enters registers it, tested alone.

    The defences are layered -- a token is registered when it is loaded,
    written and sent, and the streamer's request is masked by key as well --
    so the whole-session test stays green with any one of them gone. These
    each fail when only theirs is.
    """

    def assertRedacted(self, value):
        # Through the module's own get_logger, as everything it logs is:
        # loggers are protected when a module asks for one, not at import.
        from schwaby import auth
        out = emit(auth.get_logger(), 'value %s', value)
        self.assertNotIn(value, out)
        self.assertIn('<REDACTED', out)

    @no_duplicates
    def test_an_auth_flow_registers_the_app_key_and_secret(self):
        from schwaby import auth
        key, secret = fresh('K'), fresh('Z')
        auth.get_auth_context(key, 'https://127.0.0.1:8182')
        self.assertRedacted(key)
        # The secret goes through the same normalization in every flow that
        # takes one.
        with self.assertRaises(Exception):
            auth.client_from_access_functions(
                key, secret, lambda: None, lambda *args, **kwargs: None)
        self.assertRedacted(secret)

    @no_duplicates
    def test_loading_a_token_registers_it(self):
        from schwaby.auth import TokenMetadata
        token = {'access_token': fresh('A'), 'refresh_token': fresh('R')}
        TokenMetadata(token, 1, lambda *args, **kwargs: None)
        self.assertRedacted(token['access_token'])
        self.assertRedacted(token['refresh_token'])

    @no_duplicates
    def test_writing_a_refreshed_token_registers_it_first(self):
        from schwaby import auth
        from schwaby.auth import TokenMetadata
        seen = []
        metadata = TokenMetadata(
            {'access_token': fresh('A')}, 1,
            lambda wrapped, *args, **kwargs: seen.append(
                emit(auth.get_logger(), 'writing %s',
                     wrapped['token']['access_token'])))
        new = {'access_token': fresh('N'), 'refresh_token': fresh('M')}
        metadata.wrapped_token_write_func()(new)
        # Registered before the write ran, so the write's own log line is
        # already masked.
        self.assertNotIn(new['access_token'], seen[0])
        self.assertRedacted(new['refresh_token'])

    @no_duplicates
    def test_a_code_exchange_registers_the_code_and_redirect(self):
        from unittest.mock import patch
        from authlib.integrations.httpx_client import OAuth2Client
        from schwaby import auth
        code, key, secret = fresh('C'), fresh('K'), fresh('Z')
        received = 'https://127.0.0.1:8182/?code={}&state=s'.format(code)
        ctx = auth.AuthContext('https://127.0.0.1:8182', 'x', 's')
        with patch.object(OAuth2Client, 'fetch_token',
                          side_effect=RuntimeError('stop')):
            with self.assertRaises(RuntimeError):
                auth.client_from_received_url(
                    key, secret, ctx, received,
                    lambda *args, **kwargs: None)
        # This entry point takes the key and secret without the
        # normalization that registers them in every other flow.
        self.assertRedacted(key)
        self.assertRedacted(secret)
        # The bare code, outside any URL, so the pattern for `code=` cannot be
        # what masks it. The redirect URL is registered too, but its only
        # secret is this code, so nothing can show that registration working
        # that the code's does not.
        self.assertRedacted(code)

    @no_duplicates
    def test_a_streamer_request_masks_its_authorization_by_key(self):
        # A value never registered, so only the masking by key can hide it.
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from schwaby.streaming import StreamClient
        stream = StreamClient(MagicMock())
        stream._socket = AsyncMock()
        token = fresh('T')
        logger = streaming.get_logger()
        out = io.StringIO()
        handler = logging.StreamHandler(out)
        saved = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            asyncio.run(stream._send({'requests': [{'parameters': {
                'Authorization': token}}]}))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(saved)
        self.assertIn('Authorization', out.getvalue())
        self.assertNotIn(token, out.getvalue())

    @no_duplicates
    def test_receiving_account_activity_registers_the_account_first(self):
        import asyncio
        import json
        from unittest.mock import AsyncMock, MagicMock
        from schwaby.streaming import StreamClient
        stream = StreamClient(MagicMock())
        number = str(10 ** 7 + uuid.uuid4().int % (9 * 10 ** 7))
        stream._socket = AsyncMock()
        stream._socket.recv.return_value = json.dumps({'data': [{
            'service': 'ACCT_ACTIVITY', 'content': [{'1': number}]}]})
        logger = streaming.get_logger()
        out = io.StringIO()
        handler = logging.StreamHandler(out)
        saved = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            asyncio.run(stream._receive_from_socket())
        finally:
            logger.removeHandler(handler)
            logger.setLevel(saved)
        self.assertIn('ACCT_ACTIVITY', out.getvalue())
        self.assertNotIn(number, out.getvalue())

    @no_duplicates
    def test_an_unparsable_frame_is_quoted_redacted(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from schwaby.streaming import StreamClient, UnparsableMessage
        secret = fresh('T')
        _redact.register(secret, 'ACCESS_TOKEN')
        stream = StreamClient(MagicMock())
        stream._socket = AsyncMock()
        stream._socket.recv.return_value = '{{"x": "{}"'.format(secret)
        with self.assertRaises(UnparsableMessage) as caught:
            asyncio.run(stream._receive_from_socket())
        self.assertIn('<REDACTED ACCESS_TOKEN>', str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))

    @no_duplicates
    def test_no_bounded_text_leaves_the_head_of_a_secret(self):
        # Each helper that bounds a value for a message cuts it. Cutting
        # before redacting would leave the digits before the cut.
        from schwaby import streaming, utils
        from schwaby.contrib import util as contrib
        number = str(10 ** 7 + uuid.uuid4().int % (9 * 10 ** 7))
        _redact.register(number, 'ACCOUNT_NUMBER')
        for name, helper, bound in (
                ('_safe_value', streaming._safe_value, 200),
                ('_safe_str', streaming._safe_str, 200),
                ('_venue_text', utils._venue_text, 200),
                ('contrib _safe_repr', contrib._safe_repr, 200),
                ('_safe_name', streaming._safe_name, 64),
                ('contrib _safe_key', contrib._safe_key, 64)):
            for offset in range(bound - 12, bound + 2):
                with self.subTest(helper=name, offset=offset):
                    # Longer than the window, so the window is what decides.
                    text = helper('x' * offset + number
                                  + 'y' * (_redact._WINDOW + 400))
                    self.assertNotIn(number[:4], text)

    @no_duplicates
    def test_redaction_shrinking_the_window_leaves_no_head(self):
        # A long secret replaced by a short label, several times over, shrinks
        # the window enough to pull the raw head of one its edge cut back
        # inside the bound.
        from schwaby import streaming, utils
        token = 'L' + uuid.uuid4().hex * 32
        _redact.register(token, 'ID_TOKEN')
        for name, helper in (('_venue_text', utils._venue_text),
                             ('_safe_value', streaming._safe_value)):
            for repeat in range(4, 9):
                with self.subTest(helper=name, repeat=repeat):
                    text = helper((token + ' ') * repeat)
                    self.assertIn('<REDACTED ID_TOKEN>', text)
                    self.assertNotIn(token[:12], text)

    @no_duplicates
    def test_an_http_errors_detail_leaves_no_head_of_a_secret(self):
        import httpx2
        from schwaby import utils
        number = str(10 ** 7 + uuid.uuid4().int % (9 * 10 ** 7))
        _redact.register(number, 'ACCOUNT_NUMBER')
        bound = utils._ERROR_DETAIL_LIMIT
        for offset in range(bound - 12, bound + 2):
            with self.subTest(offset=offset):
                detail = utils._describe_error(httpx2.Response(400, json={
                    'message': 'x' * offset + number + 'y' * 300}))
                self.assertIn('truncated', detail)
                self.assertNotIn(number[:4], detail)

    @no_duplicates
    def test_a_rest_response_registers_its_account_numbers_first(self):
        import httpx2
        from unittest.mock import MagicMock
        from schwaby.client import Client
        number = str(10 ** 7 + uuid.uuid4().int % (9 * 10 ** 7))
        client = Client(fresh('K'), MagicMock())
        response = httpx2.Response(200, json=[{'accountNumber': number,
                                               'hashValue': 'H'}])
        out = io.StringIO()
        handler = logging.StreamHandler(out)
        from schwaby.client import base
        logger = base.get_logger()
        saved = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            client._log_response(response, 1)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(saved)
        self.assertIn('hashValue', out.getvalue())
        self.assertNotIn(number, out.getvalue())


class WebsocketLoggerTest(unittest.TestCase):

    def frame(self, token):
        import websockets.frames
        return websockets.frames.Frame(
            websockets.frames.Opcode.TEXT,
            '{{"parameters": {{"Authorization": "{}"}}}}'.format(
                token).encode())

    @no_duplicates
    def test_websockets_is_given_a_protected_logger(self):
        # Over the logger websockets uses when given none, so its records still
        # come from that logger. No filter is put on it: every other
        # client in the process logs there, and the adapter covers the frames.
        logger = _prepare_connect_args({})['logger']
        self.assertIsInstance(logger, _redact.FrameRedactingAdapter)
        self.assertIs(logging.getLogger('websockets.client'), logger.logger)
        self.assertNotIn(_redact._FILTER, logger.logger.filters)

    @no_duplicates
    def test_configuration_of_websockets_client_still_applies(self):
        # A filter or `disabled` on websockets.client applies to the records
        # websockets makes, as it would if this library did not wrap the
        # logger.
        client = logging.getLogger('websockets.client')
        out = self.capture(client)
        drop = logging.Filter()
        drop.filter = lambda record: False
        client.addFilter(drop)
        try:
            _prepare_connect_args({})['logger'].debug(
                '> %s', self.frame(fresh('T')))
            self.assertEqual('', out.getvalue())
        finally:
            client.removeFilter(drop)
            client.handlers.clear()
            client.propagate = True
            client.setLevel(logging.NOTSET)
        # Positive control: without the filter, the line is written.
        out = self.capture(client)
        try:
            _prepare_connect_args({})['logger'].debug(
                '> %s', self.frame(fresh('T')))
            self.assertIn('Authorization', out.getvalue())
        finally:
            client.handlers.clear()
            client.propagate = True
            client.setLevel(logging.NOTSET)

    @no_duplicates
    def test_a_frames_attributes_reach_a_callers_filter(self):
        # A filter that drops pings by opcode keeps working; the payload is
        # handed over redacted.
        token = fresh('T')
        frame = self.frame(token)
        wrapped = _redact._RedactedFrame(frame)
        self.assertIs(frame.opcode, wrapped.opcode)
        self.assertTrue(wrapped.fin)
        self.assertIn(b'Authorization', wrapped.data)
        self.assertNotIn(token.encode(), wrapped.data)

    @no_duplicates
    def test_a_callers_own_logger_is_wrapped_not_replaced(self):
        mine = logging.getLogger('callers.' + fresh())
        logger = _prepare_connect_args({'logger': mine})['logger']
        self.assertIsInstance(logger, _redact.FrameRedactingAdapter)
        self.assertIs(mine, logger.logger)
        # Nothing is added to the caller's logger itself.
        self.assertEqual([], mine.filters)

    def capture(self, mine):
        out = io.StringIO()
        handler = logging.StreamHandler(out)
        mine.addHandler(handler)
        mine.setLevel(logging.DEBUG)
        mine.propagate = False
        return out

    @no_duplicates
    def test_a_logger_built_directly_is_covered(self):
        # Not the logger getLogger finds by its name, so nothing may look it
        # up by name to decide whether to redact.
        mine = logging.Logger('callers.' + fresh())
        out = self.capture(mine)
        token = fresh('T')
        _prepare_connect_args({'logger': mine})['logger'].debug(
            '> %s', self.frame(token))
        self.assertIn('Authorization', out.getvalue())
        self.assertNotIn(token, out.getvalue())

    @no_duplicates
    def test_a_callers_filter_that_renders_the_record_sees_it_redacted(self):
        mine = logging.getLogger('callers.' + fresh())
        out = self.capture(mine)

        def render(record):
            record.msg, record.args = record.getMessage(), ()
            return True
        mine.addFilter(render)
        token = fresh('T')
        _prepare_connect_args({'logger': mine})['logger'].debug(
            '> %s', self.frame(token))
        self.assertIn('Authorization', out.getvalue())
        self.assertNotIn(token, out.getvalue())

    @no_duplicates
    def test_an_adapter_of_an_adapter_is_covered(self):
        mine = logging.getLogger('callers.' + fresh())
        out = self.capture(mine)
        theirs = logging.LoggerAdapter(logging.LoggerAdapter(mine, {}), {})
        token = fresh('T')
        logger = _prepare_connect_args({'logger': theirs})['logger']
        self.assertIs(theirs, logger.logger)
        logger.debug('> %s', self.frame(token))
        self.assertIn('Authorization', out.getvalue())
        self.assertNotIn(token, out.getvalue())

    @no_duplicates
    def test_everything_but_a_frame_passes_through_untouched(self):
        mine = logging.getLogger('callers.' + fresh())
        seen = []
        handler = logging.Handler()
        handler.emit = seen.append
        mine.addHandler(handler)
        mine.setLevel(logging.DEBUG)
        mine.propagate = False
        theirs = logging.LoggerAdapter(mine, {'session': 'S1'})
        logger = _prepare_connect_args({'logger': theirs})['logger']
        logger.info({'event': 'mine'})
        logger.info('= connection is %s', 'OPEN', extra={'websocket': 'W'})
        self.assertEqual({'event': 'mine'}, seen[0].msg)
        self.assertEqual(('OPEN',), seen[1].args)
        # The caller's adapter still supplies its own extra.
        self.assertEqual('S1', seen[1].session)
        # And over a plain logger, the extra websockets passes arrives.
        _prepare_connect_args({'logger': mine})['logger'].info(
            'x', extra={'websocket': 'W'})
        self.assertEqual('W', seen[2].websocket)

    @no_duplicates
    def test_a_frame_no_handler_writes_is_not_redacted(self):
        # Redacting costs a decode and a pass per frame, on the event loop,
        # so it waits until something renders the frame, which nothing does
        # for a line no handler writes -- through an adapter of an adapter as
        # well.
        from unittest.mock import patch
        mine = logging.getLogger('callers.' + fresh())
        handler = logging.StreamHandler(io.StringIO())
        handler.setLevel(logging.INFO)
        mine.addHandler(handler)
        mine.setLevel(logging.DEBUG)
        mine.propagate = False
        theirs = logging.LoggerAdapter(logging.LoggerAdapter(mine, {}), {})
        logger = _prepare_connect_args({'logger': theirs})['logger']
        real = _redact._redact_frame
        calls = []
        with patch.object(_redact, '_redact_frame',
                          lambda f: calls.append(f) or real(f)):
            logger.debug('> %s', self.frame(fresh('T')))
            self.assertEqual([], calls)
            # Positive control: a level the handler writes is redacted.
            logger.info('> %s', self.frame(fresh('T')))
            self.assertEqual(1, len(calls))

    @no_duplicates
    def test_a_record_names_the_line_that_logged_it(self):
        # The adapter adds a frame of its own; the record must still point at
        # the code that called it, as it does for websockets' own lines.
        mine = logging.getLogger('callers.' + fresh())
        seen = []
        handler = logging.Handler()
        handler.emit = seen.append
        mine.addHandler(handler)
        mine.setLevel(logging.DEBUG)
        mine.propagate = False
        _prepare_connect_args({'logger': mine})['logger'].debug(
            '> %s', self.frame(fresh('T')))
        # And beneath a caller's adapter, which logging counted differently
        # before 3.11.
        _prepare_connect_args({'logger': logging.LoggerAdapter(mine, {})})[
            'logger'].debug('> %s', self.frame(fresh('T')))
        self.assertEqual([__file__.rstrip('c')] * 2,
                         [record.pathname for record in seen])

    @no_duplicates
    def test_a_fragment_is_not_logged(self):
        # Neither half of a value split across fragments matches it, and a
        # fragment of JSON does not parse, so nothing it names is known.
        import websockets.frames
        number = str(10 ** 7 + uuid.uuid4().int % (9 * 10 ** 7))
        payload = json.dumps({'data': [{'service': 'ACCT_ACTIVITY',
                                        'content': [{'1': number}]}]})
        cut = payload.index(number) + 4
        mine = logging.getLogger('callers.' + fresh())
        out = self.capture(mine)
        logger = _prepare_connect_args({'logger': mine})['logger']
        logger.debug('< %s', websockets.frames.Frame(
            websockets.frames.Opcode.TEXT, payload[:cut].encode(), fin=False))
        logger.debug('< %s', websockets.frames.Frame(
            websockets.frames.Opcode.CONT, payload[cut:].encode()))
        self.assertEqual(2, out.getvalue().count('fragment of'))
        self.assertNotIn(number[:4], out.getvalue())
        self.assertNotIn(number[4:], out.getvalue())


class FrameElisionTest(unittest.TestCase):
    '''websockets elides a long frame's middle when it is rendered.

    At some sizes the cut lands inside the token, and the tail it leaves is
    neither a registered value nor next to the key that names it -- so a frame
    has to be redacted before it is rendered, at every size.
    '''

    def sweep(self, payload, secrets):
        import websockets.frames
        from unittest.mock import patch
        logger = _redact.websocket_logger()
        frame = websockets.frames.Frame(websockets.frames.Opcode.TEXT,
                                        payload.encode())
        leaks, lines = [], 0
        for size in range(10, len(payload) + 40):
            with patch.object(websockets.frames.Frame, 'MAX_LOG_SIZE', size):
                out = emit(logger, '> %s', frame)
            lines += bool(out)
            for secret in secrets:
                if any(secret[i:i + 8] in out
                       for i in range(len(secret) - 7)):
                    leaks.append((size, out))
        self.assertTrue(lines)
        self.assertEqual([], leaks[:1])

    @no_duplicates
    def test_no_size_leaves_part_of_a_registered_token(self):
        token = 'I0.' + uuid.uuid4().hex + uuid.uuid4().hex[:11] + '@'
        _redact.register(token, 'ACCESS_TOKEN')
        self.sweep(json.dumps({'requests': [{
            'service': 'ADMIN', 'command': 'LOGIN', 'requestid': '0',
            'SchwabClientCustomerId': 'f' * 64, 'parameters': {
                'Authorization': token, 'SchwabClientChannel': 'N9',
                'SchwabClientFunctionId': 'APIAPP'}}]}), [token])

    @no_duplicates
    def test_no_size_leaves_part_of_an_unregistered_token(self):
        token = 'U0.' + uuid.uuid4().hex + uuid.uuid4().hex[:11] + '@'
        self.sweep(json.dumps({'requests': [{
            'service': 'ADMIN', 'command': 'LOGIN', 'requestid': '0',
            'parameters': {'Authorization': token,
                           'SchwabClientChannel': 'N9'}}]}), [token])

    @no_duplicates
    def test_an_arriving_frame_registers_its_account_before_rendering(self):
        number = str(10 ** 7 + uuid.uuid4().int % (9 * 10 ** 7))
        self.sweep(json.dumps({'data': [{
            'service': 'ACCT_ACTIVITY', 'timestamp': 1, 'command': 'SUBS',
            'content': [{'seq': 1, 'key': 'K', '1': number,
                         '2': 'OrderCreated'}]}]}), [number])


if __name__ == '__main__':
    unittest.main()
