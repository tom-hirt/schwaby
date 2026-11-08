import atexit
import io
import json
import logging
import schwaby
import unittest

from schwaby.client import Client
from .utils import MockResponse, no_duplicates
from unittest.mock import Mock, patch


def isolate_bug_report_state(test):
    '''Give ``test`` a fresh redactor and collection flag, restored after it.

    Both are process-wide. Every test that builds a client from a token
    registers that token, and the auth tests' tokens are ``'a'`` and ``'r'``, so a test after them asserting
    on logged text would find ``'a line worth reporting'`` redacted letter by
    letter. What runs before a test depends on the order, which
    ``pytest -n auto`` does not keep, so a test that calls this starts from
    fresh state whatever ran before it.
    '''
    for patcher in (patch.object(schwaby, 'LOG_REDACTOR',
                                 schwaby.debug.LogRedactor()),
                    patch.object(schwaby.debug, '_COLLECT_RESPONSE_REDACTIONS',
                                 schwaby.debug._COLLECT_RESPONSE_REDACTIONS)):
        patcher.start()
        test.addCleanup(patcher.stop)


class IsolateBugReportStateTest(unittest.TestCase):

    @no_duplicates
    def test_both_globals_are_put_back(self):
        redactor = schwaby.LOG_REDACTOR
        collecting = schwaby.debug._COLLECT_RESPONSE_REDACTIONS
        inner = unittest.TestCase()
        self.addCleanup(inner.doCleanups)

        isolate_bug_report_state(inner)
        # Positive control: without a swap, "put back" holds trivially.
        self.assertIsNot(redactor, schwaby.LOG_REDACTOR)
        schwaby.debug._COLLECT_RESPONSE_REDACTIONS = not collecting

        inner.doCleanups()
        self.assertIs(redactor, schwaby.LOG_REDACTOR,
                      'the redactor was not put back')
        self.assertEqual(collecting, schwaby.debug._COLLECT_RESPONSE_REDACTIONS,
                         'the collection flag was not put back')


class RedactorTest(unittest.TestCase):

    def setUp(self):
        self.redactor = schwaby.debug.LogRedactor()

    @no_duplicates
    def test_no_redactions(self):
        self.assertEqual('test message', self.redactor.redact('test message'))

    @no_duplicates
    def test_simple_redaction(self):
        self.redactor.register('secret', 'SECRET')

        self.assertEqual(
            '<REDACTED SECRET> message',
            self.redactor.redact('secret message'))

    @no_duplicates
    def test_multiple_registrations_same_string(self):
        self.redactor.register('secret', 'SECRET')
        self.redactor.register('secret', 'SECRET')

        self.assertEqual(
            '<REDACTED SECRET> message',
            self.redactor.redact('secret message'))

    @no_duplicates
    def test_multiple_registrations_same_string_different_label(self):
        self.redactor.register('secret-A', 'SECRET')
        self.redactor.register('secret-B', 'SECRET')

        self.assertEqual(
            '<REDACTED SECRET-1> message <REDACTED SECRET-2>',
            self.redactor.redact('secret-A message secret-B'))


class RegisterRedactionsTest(unittest.TestCase):

    def setUp(self):
        isolate_bug_report_state(self)
        self.captured = io.StringIO()
        self.logger = logging.getLogger('test')
        self.dump_logs = schwaby.debug._enable_bug_report_logging(
            output=self.captured, loggers=[self.logger])

    @no_duplicates
    def test_empty_string(self):
        schwaby.debug.register_redactions('')

    @no_duplicates
    def test_empty_dict(self):
        schwaby.debug.register_redactions({})

    @no_duplicates
    def test_empty_list(self):
        schwaby.debug.register_redactions([])

    @no_duplicates
    def test_dict(self):
        schwaby.debug.register_redactions(
            {'BadNumber': '100001'},
            bad_patterns=['bad'])
        schwaby.debug.register_redactions(
            {'OtherBadNumber': '200002'},
            bad_patterns=['bad'])

        self.logger.info('Bad Number: 100001')
        self.logger.info('Other Bad Number: 200002')

        self.dump_logs()
        self.assertRegex(
            self.captured.getvalue(),
            r'\[.*\] Bad Number: <REDACTED BadNumber>\n' +
            r'\[.*\] Other Bad Number: <REDACTED OtherBadNumber>\n')

    @no_duplicates
    def test_list_of_dict(self):
        schwaby.debug.register_redactions(
            [{'GoodNumber': '900009'},
             {'BadNumber': '100001'},
             {'OtherBadNumber': '200002'}],
            bad_patterns=['bad'])

        self.logger.info('Good Number: 900009')
        self.logger.info('Bad Number: 100001')
        self.logger.info('Other Bad Number: 200002')

        self.dump_logs()
        self.assertRegex(
            self.captured.getvalue(),
            r'\[.*\] Good Number: 900009\n' +
            r'\[.*\] Bad Number: <REDACTED 1-BadNumber>\n' +
            r'\[.*\] Other Bad Number: <REDACTED 2-OtherBadNumber>\n')

    @no_duplicates
    def test_whitelist(self):
        schwaby.debug.register_redactions(
            [{'GoodNumber': '900009'},
             {'BadNumber': '100001'},
             {'OtherBadNumber': '200002'}],
            bad_patterns=['bad'],
            whitelisted=['otherbadnumber'])

        self.logger.info('Good Number: 900009')
        self.logger.info('Bad Number: 100001')
        self.logger.info('Other Bad Number: 200002')

        self.dump_logs()
        self.assertRegex(
            self.captured.getvalue(),
            r'\[.*\] Good Number: 900009\n' +
            r'\[.*\] Bad Number: <REDACTED 1-BadNumber>\n' +
            r'\[.*\] Other Bad Number: 200002\n')

    @no_duplicates
    def test_schwab_account_identifiers_are_redacted(self):
        # accountNumber and hashValue are how Schwab names the two identifiers
        # a user is least likely to want in a bug report they paste into a
        # public issue. Patterns written for another API's names for them,
        # accountId and displayName, would miss both.
        schwaby.debug.register_redactions({
            'accounts': [{'accountNumber': '12345678',
                          'hashValue': 'ABCDEF0123456789'}]})

        self.logger.info('account 12345678 has hash ABCDEF0123456789')

        self.dump_logs()
        logged = self.captured.getvalue()
        self.assertNotIn('12345678', logged)
        self.assertNotIn('ABCDEF0123456789', logged)

    @no_duplicates
    def test_redaction_does_not_swallow_ordinary_values(self):
        # The redactor replaces registered values by plain string substitution
        # anywhere they appear, so a pattern which matches short or common
        # values corrupts the rest of the log. Balances, colours and account
        # types must not be caught.
        schwaby.debug.register_redactions({
            'securitiesAccount': {
                'accountNumber': '12345678',
                'type': 'MARGIN',
                'currentBalances': {'accountValue': 100.0}},
            'accountColor': 'Green'})

        self.logger.info('MARGIN order, value 100.0, colour Green')

        self.dump_logs()
        logged = self.captured.getvalue()
        self.assertIn('MARGIN', logged)
        self.assertIn('100.0', logged)
        self.assertIn('Green', logged)

    @no_duplicates
    @patch('schwaby.debug.register_redactions', new_callable=Mock)
    def test_register_from_request_success(self, register_redactions):
        resp = MockResponse({'success': 1}, 200)
        schwaby.debug.register_redactions_from_response(resp)
        register_redactions.assert_called_with({'success': 1})

    @no_duplicates
    @patch('schwaby.debug.register_redactions', new_callable=Mock)
    def test_register_from_request_not_okay(self, register_redactions):
        resp = MockResponse({'success': 1}, 403)
        schwaby.debug.register_redactions_from_response(resp)
        register_redactions.assert_not_called()

    @no_duplicates
    @patch('schwaby.debug._COLLECT_RESPONSE_REDACTIONS', False)
    @patch('schwaby.debug.register_redactions', new_callable=Mock)
    def test_register_from_request_does_nothing_when_not_collecting(
            self, register_redactions):
        # Walking every response is only worth paying for when the logs are
        # going to be shared, which is what enable_bug_report_logging signals.
        resp = MockResponse({'success': 1}, 200)
        schwaby.debug.register_redactions_from_response(resp)
        register_redactions.assert_not_called()

    @no_duplicates
    @patch('schwaby.debug.register_redactions', new_callable=Mock)
    def test_register_unparseable_json(self, register_redactions):
        class MR(MockResponse):
            def json(self):
                raise json.decoder.JSONDecodeError('e243rschwabgew', '', 0)

        resp = MR({'success': 1}, 200)
        schwaby.debug.register_redactions_from_response(resp)
        register_redactions.assert_not_called()

class EnableDebugLoggingTest(unittest.TestCase):

    def setUp(self):
        isolate_bug_report_state(self)

    @patch('atexit.register')
    @patch('logging.Logger.addHandler')
    def test_enable_doesnt_throw_exceptions(self, _, __):
        try:
            schwaby.debug.enable_bug_report_logging()
        except AttributeError:
            self.fail("debug.enable_bug_report_logging() raised AttributeError unexpectedly")

    @no_duplicates
    def test_writes_to_stderr_as_it_is_at_exit(self):
        # The logs are written at program exit, so the stream to write them to
        # is whatever sys.stderr is then -- not whatever it happened to be when
        # this module was imported.
        replacement = io.StringIO()
        dump_logs = schwaby.debug._enable_bug_report_logging(
                loggers=[logging.getLogger('test-late-binding')])
        self.addCleanup(atexit.unregister, dump_logs)
        logging.getLogger('test-late-binding').info('a line worth reporting')

        with patch('sys.stderr', replacement):
            dump_logs()

        self.assertIn('a line worth reporting', replacement.getvalue())

    @no_duplicates
    def test_does_not_write_to_a_stream_whose_reader_has_gone(self):
        # `python bot.py 2>&1 | head -20` is enough: head exits, the pipe
        # breaks, and at interpreter shutdown this handler would produce
        # exactly the traceback it exists to avoid. BrokenPipeError is an
        # OSError, not a ValueError.
        class BrokenPipe(io.StringIO):
            def write(self, s):
                raise BrokenPipeError(32, 'Broken pipe')

        dump_logs = schwaby.debug._enable_bug_report_logging(
                output=BrokenPipe(),
                loggers=[logging.getLogger('test-broken-pipe')])
        self.addCleanup(atexit.unregister, dump_logs)

        dump_logs()

    @no_duplicates
    def test_does_not_write_to_a_stream_closed_before_exit(self):
        # An application which replaced sys.stderr and then closed it should
        # not have a traceback out of an atexit handler as its last output.
        closed = io.StringIO()
        dump_logs = schwaby.debug._enable_bug_report_logging(
                loggers=[logging.getLogger('test-closed-stream')])
        self.addCleanup(atexit.unregister, dump_logs)
        closed.close()

        with patch('sys.stderr', closed):
            dump_logs()


class ClientRedactionWiringTest(unittest.TestCase):
    '''The clients call register_redactions_from_response after every request.
    A local definition of that name in either client module would shadow the
    import and silently turn all response redaction into a no-op.'''

    @no_duplicates
    def test_clients_use_the_real_redactor(self):
        from schwaby.client import asynchronous, synchronous

        for module in (synchronous, asynchronous):
            self.assertIs(
                    module.register_redactions_from_response,
                    schwaby.debug.register_redactions_from_response,
                    '{} does not use the real redactor'.format(module.__name__))
