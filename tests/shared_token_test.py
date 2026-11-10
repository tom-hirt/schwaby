'''Tests for a shared token file: the lock, newest-wins, and taking the token
another client refreshed.

The network is replaced with schwaby.testing's fake token endpoint, inside
block_network(). A lock held by another process is held by a child that
releases it when its stdin closes, so nothing is signalled.
'''

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

import schwaby
import schwaby.testing as st
import schwaby._shared_token as _shared_token
from schwaby import auth

from .utils import no_duplicates


API_KEY = 'a' * 32
APP_SECRET = 'b' * 16
CALLBACK_URL = 'https://127.0.0.1:8182'
QUOTES = '/marketdata/v1/AAPL/quotes'
HOLD = textwrap.dedent('''
    import fcntl, os, sys, time
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
    # Polled, with a deadline, so a child that never gets the lock ends
    # by itself rather than waiting for it forever.
    deadline = time.monotonic() + 20
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() > deadline:
                print('never held', flush=True)
                sys.exit(1)
            time.sleep(0.01)
    print('held', flush=True)
    sys.stdin.read()
''')


def _skip_without_flock():
    return unittest.skipIf(os.name == 'nt', 'the child holds an flock')


class SharedTokenTestCase(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name
        self.path = os.path.join(self.dir, 'token.json')
        self.lock = os.path.realpath(self.path) + '.lock'
        self.endpoint = st.FakeTokenEndpoint()
        self.endpoint.on_api(lambda request: {'ok': True})
        blocked = st.block_network()
        blocked.__enter__()
        self.addCleanup(blocked.__exit__, None, None, None)

    def client(self, asyncio_=False, endpoint=None, **kwargs):
        return auth.client_from_token_file(
                self.path, API_KEY, APP_SECRET, asyncio=asyncio_,
                shared=True, transport=endpoint or self.endpoint, **kwargs)

    def quote(self, client):
        result = client.get_quote('AAPL')
        if asyncio.iscoroutine(result):
            return asyncio.run(result)
        return result

    def stored(self):
        with open(self.path) as f:
            return json.load(f)

    def hold_the_lock(self, path=None):
        '''A child process holding the lock, until the returned function
        lets it go.'''
        import select
        child = subprocess.Popen(
                [sys.executable, '-I', '-c', HOLD, path or self.lock],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        ready, _, _ = select.select([child.stdout], [], [], 30)
        self.assertTrue(ready, 'the child never said whether it held the lock')
        self.assertEqual('held', child.stdout.readline().strip())

        def release():
            if child.poll() is None:
                child.stdin.close()
                child.wait(10)
            child.stdout.close()
        self.addCleanup(release)
        return release


class LockTest(SharedTokenTestCase):

    @no_duplicates
    def test_a_refresh_makes_the_lock_file_beside_the_real_one(self):
        real = os.path.join(self.dir, 'real.json')
        st.write_token_file(real, expires_in=-60)
        os.symlink(real, self.path)
        self.endpoint.answer_refresh(st.token())
        self.quote(self.client())
        self.assertFalse(os.path.exists(self.path + '.lock'),
                         'the lock is beside the link, not the real file')
        self.assertTrue(os.path.exists(real + '.lock'))
        if os.name != 'nt':
            self.assertEqual(0o600, os.stat(real + '.lock').st_mode & 0o777)

    @no_duplicates
    def test_a_load_takes_no_lock(self):
        st.write_token_file(self.path)
        self.quote(self.client())
        self.assertFalse(os.path.exists(self.lock),
                         'a load made the lock file')

    @no_duplicates
    def test_lock_path_names_the_lock(self):
        st.write_token_file(self.path, expires_in=-60)
        self.endpoint.answer_refresh(st.token())
        mine = os.path.join(self.dir, 'mine.lock')
        self.quote(self.client(lock_path=mine))
        self.assertTrue(os.path.exists(mine))
        self.assertFalse(os.path.exists(self.lock))

    @no_duplicates
    def test_without_shared_there_is_no_lock(self):
        st.write_token_file(self.path, expires_in=-60)
        self.endpoint.answer_refresh(st.token())
        client = auth.client_from_token_file(
                self.path, API_KEY, APP_SECRET, transport=self.endpoint)
        self.quote(client)
        self.assertFalse(os.path.exists(self.lock))
        self.assertNotIn('received_at', self.stored())

    @no_duplicates
    @_skip_without_flock()
    def test_a_refresh_waits_for_another_process_and_then_refreshes(self):
        st.write_token_file(self.path, expires_in=-60)
        self.endpoint.answer_refresh(st.token(access_token='NEW'))
        release = self.hold_the_lock()
        client = self.client(lock_timeout=10)
        threading.Timer(0.3, release).start()
        started = time.monotonic()
        self.quote(client)
        self.assertGreater(time.monotonic() - started, 0.2,
                           'the refresh did not wait for the lock')
        self.assertEqual('NEW', self.stored()['token']['access_token'])

    @no_duplicates
    @_skip_without_flock()
    def test_a_lock_held_past_the_timeout_fails_the_refresh(self):
        for asyncio_ in (False, True):
            with self.subTest(asyncio=asyncio_):
                self.endpoint = st.FakeTokenEndpoint()
                self.endpoint.on_api(lambda request: {'ok': True})
                st.write_token_file(self.path, expires_in=-60)
                release = self.hold_the_lock()
                try:
                    client = self.client(asyncio_, lock_timeout=0.2)
                    with self.assertRaises(schwaby.TokenRefreshError) as cm:
                        self.quote(client)
                finally:
                    release()
                self.assertIs(schwaby.TokenErrorKind.TRANSIENT,
                              cm.exception.kind)
                self.assertIsInstance(cm.exception.__cause__,
                                      schwaby.TokenFileLockTimeout)
                self.assertEqual([], self.endpoint.requests,
                                 'something was sent past a lock timeout')

    @no_duplicates
    def test_the_lock_excludes_another_thread(self):
        st.write_token_file(self.path)
        a = _shared_token.SharedTokenFile(self.path, lock_timeout=0.1)
        b = _shared_token.SharedTokenFile(self.path, lock_timeout=0.1)
        inside, leave = threading.Event(), threading.Event()

        def hold():
            with a.lock():
                inside.set()
                leave.wait(10)
        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(inside.wait(10))
        try:
            with self.assertRaises(schwaby.TokenFileLockTimeout):
                with b.lock():
                    pass
        finally:
            leave.set()
            holder.join(10)
        with b.lock():
            pass

    @no_duplicates
    def test_an_async_wait_cancelled_gives_the_lock_back(self):
        st.write_token_file(self.path)
        shared = _shared_token.SharedTokenFile(self.path, lock_timeout=5)
        inside, leave = threading.Event(), threading.Event()

        def hold():
            with shared.lock():
                inside.set()
                leave.wait(10)
        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(inside.wait(10))

        async def wait_and_cancel():
            async def take():
                async with shared.alock():
                    pass
            task = asyncio.ensure_future(take())
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            leave.set()
            # The worker thread takes the lock once it is free, and gives it
            # back for the cancelled task.
            await asyncio.sleep(0.3)
        asyncio.run(wait_and_cancel())
        holder.join(10)
        other = _shared_token.SharedTokenFile(self.path, lock_timeout=1)
        with other.lock():
            pass

    @no_duplicates
    def test_a_wait_cancelled_after_the_worker_took_the_lock_gives_it_back(
            self):
        # The worker has the lock and has handed it over, but the task has
        # not resumed when it is cancelled: the task gives it back.
        st.write_token_file(self.path)
        shared = _shared_token.SharedTokenFile(self.path, lock_timeout=5)
        started, took = threading.Event(), threading.Event()
        acquire_for = shared._acquire_for

        def watched(key, claim):
            started.set()
            fd = acquire_for(key, claim)
            took.set()
            return fd
        shared._acquire_for = watched

        async def cancel_late():
            async def take():
                async with shared.alock():
                    pass
            task = asyncio.ensure_future(take())
            while not started.is_set():
                await asyncio.sleep(0)
            # Blocks the loop until the worker has the lock, so the task
            # cannot resume before the cancel.
            self.assertTrue(took.wait(10))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(cancel_late())
        other = _shared_token.SharedTokenFile(self.path, lock_timeout=1)
        with other.lock():
            pass

    @no_duplicates
    def test_a_wait_whose_event_loop_has_closed_gives_the_lock_back(self):
        # The task gives up, and its event loop closes, before the worker
        # thread gets the lock: the worker gives it back itself.
        st.write_token_file(self.path)
        shared = _shared_token.SharedTokenFile(self.path, lock_timeout=5)
        inside, leave = threading.Event(), threading.Event()

        def hold():
            with shared.lock():
                inside.set()
                leave.wait(10)
        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(inside.wait(10))
        threading.Timer(0.3, leave.set).start()

        async def give_up():
            async def take():
                async with shared.alock():
                    pass
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(take(), 0.1)
        # asyncio.run waits for the worker as it shuts down, so the lock is
        # taken, by a task long gone, before this returns.
        asyncio.run(give_up())
        holder.join(10)
        other = _shared_token.SharedTokenFile(self.path, lock_timeout=1)
        with other.lock():
            pass

    @no_duplicates
    def test_a_login_write_raises_the_timeout_itself(self):
        if os.name == 'nt':
            self.skipTest('the child holds an flock')
        release = self.hold_the_lock()
        self.endpoint.answer_exchange(st.token())
        context = auth.get_auth_context(API_KEY, CALLBACK_URL)
        writer = auth.token_file_writer(self.path, shared=True,
                                        lock_timeout=0.2)
        with self.assertRaises(schwaby.TokenFileLockTimeout):
            auth.client_from_received_url(
                    API_KEY, APP_SECRET, context, st.received_url(context),
                    writer, transport=self.endpoint)
        release()


@unittest.skipIf(os.name == 'nt', 'Windows has no owner or mode to check')
class SafeLockFileTest(SharedTokenTestCase):

    def refused(self, **kwargs):
        st.write_token_file(self.path, expires_in=-60)
        # Answered, so a lock let through goes on to refresh and the test
        # fails on the refusal it expected, not on an unanswered request.
        self.endpoint.answer_refresh(st.token())
        with self.assertRaises(schwaby.UnsafeTokenLockFile) as cm:
            # Refused when the client is built, before anything is sent or
            # a sign-in spent, or at the latest when the lock is taken.
            self.quote(self.client(**kwargs))
        self.assertEqual([], self.endpoint.requests,
                         'something was sent past an unsafe lock')
        return cm.exception

    @no_duplicates
    def test_a_directory_others_can_write_is_refused(self):
        for mode in (0o770, 0o707, 0o777):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.dir, mode)
                try:
                    self.refused()
                finally:
                    os.chmod(self.dir, 0o700)

    @no_duplicates
    def test_the_directory_is_refused_before_anything_starts(self):
        os.chmod(self.dir, 0o775)
        try:
            for build in (
                    lambda: auth.token_file_writer(self.path, shared=True),
                    lambda: auth.shared_token_file(self.path),
                    lambda: self.client()):
                with self.subTest(build=build):
                    st.write_token_file(self.path)
                    with self.assertRaises(schwaby.UnsafeTokenLockFile):
                        build()
        finally:
            os.chmod(self.dir, 0o700)
        self.assertEqual([], self.endpoint.requests)

    @no_duplicates
    def test_a_directory_another_user_owns_is_refused(self):
        with patch('os.getuid', return_value=os.getuid() + 1):
            with self.assertRaises(schwaby.UnsafeTokenLockFile) as cm:
                auth.shared_token_file(self.path)
        self.assertIn('owned by uid', str(cm.exception))

    @no_duplicates
    def test_a_symlinked_lock_file_is_refused(self):
        elsewhere = os.path.join(self.dir, 'elsewhere')
        open(elsewhere, 'w').close()
        os.symlink(elsewhere, self.lock)
        self.refused()

    @no_duplicates
    def test_a_lock_in_a_symlinked_directory_is_refused(self):
        real = os.path.join(self.dir, 'real')
        os.mkdir(real, 0o700)
        linked = os.path.join(self.dir, 'linked')
        os.symlink(real, linked)
        self.refused(lock_path=os.path.join(linked, 'token.lock'))

    @no_duplicates
    def test_a_safe_lock_is_taken(self):
        st.write_token_file(self.path, expires_in=-60)
        self.endpoint.answer_refresh(st.token())
        self.quote(self.client())
        self.assertEqual(0o600, os.stat(self.lock).st_mode & 0o777)


class SignInTest(SharedTokenTestCase):

    @no_duplicates
    def test_a_sign_in_replaces_a_token_dated_in_the_future(self):
        future = st.write_token_file(self.path, created_days_ago=0)
        stored = self.stored()
        stored['creation_timestamp'] = future['creation_timestamp'] + 86400
        with open(self.path, 'w') as f:
            json.dump(stored, f)
        self.endpoint.answer_exchange(st.token(access_token='SIGNED-IN'))
        context = auth.get_auth_context(API_KEY, CALLBACK_URL)
        auth.client_from_received_url(
                API_KEY, APP_SECRET, context, st.received_url(context),
                auth.token_file_writer(self.path, shared=True),
                transport=self.endpoint)
        self.assertEqual('SIGNED-IN', self.stored()['token']['access_token'],
                         'a token dated in the future swallowed a sign-in')

    @no_duplicates
    def test_a_writer_takes_sign_in(self):
        newer = st.write_token_file(self.path, created_days_ago=0)
        older = {'creation_timestamp': newer['creation_timestamp'] - 86400,
                 'token': st.token(access_token='OLDER')}
        write = auth.token_file_writer(self.path, shared=True)
        write(older)
        self.assertNotEqual('OLDER', self.stored()['token']['access_token'])
        write(older, sign_in=True)
        self.assertEqual('OLDER', self.stored()['token']['access_token'])


class NewestWinsTest(SharedTokenTestCase):

    @staticmethod
    def wrapped(created, expires_at, received_at=None):
        stored = {'creation_timestamp': created,
                  'token': {'expires_at': expires_at}}
        if received_at is not None:
            stored['received_at'] = received_at
        return stored

    @no_duplicates
    def test_the_order_of_two_tokens(self):
        w, newer = self.wrapped, _shared_token.is_at_least_as_new
        for ours, theirs, expected in (
                (w(2, 1), w(1, 9), True),         # a later login wins
                (w(1, 9), w(2, 1), False),
                (w(1, 5), w(1, 4), True),         # then the later expiry
                (w(1, 4), w(1, 5), False),
                (w(1, 5, 7.5), w(1, 5, 7.0), True),   # then received_at
                (w(1, 5, 7.0), w(1, 5, 7.5), False),
                (w(1, 5, 7.0), w(1, 5), True),    # only when both have it
                (w(1, 5), w(1, 5, 9.0), True),
                (w(1, 5), w(1, 5), True),         # a tie is written
                # A login dated more than an hour ahead is a wrong clock's:
                # it ranks oldest, so it outranks nothing, ever.
                (w(1, 5), w(time.time() + 7200, 9), True),
                (w(time.time() + 7200, 9), w(1, 5), False),
                (w(1, 5), None, True),            # no file
                (w(1, 5), 'not a token', True)):
            with self.subTest(ours=ours, theirs=theirs):
                self.assertEqual(expected, newer(ours, theirs),
                                 'two tokens were ordered wrongly')

    @no_duplicates
    def test_an_older_token_is_not_written_over_a_newer_one(self):
        newer = st.write_token_file(self.path, created_days_ago=0)
        shared = _shared_token.SharedTokenFile(
                self.path, default_write=auth._write_token_bytes)
        older = {'creation_timestamp': newer['creation_timestamp'] - 86400,
                 'token': st.token()}
        self.assertFalse(shared.write(older),
                         'an older token was written over a newer one')
        self.assertEqual(newer['token']['access_token'],
                         self.stored()['token']['access_token'])
        # A newer one is written.
        newest = {'creation_timestamp': newer['creation_timestamp'] + 1,
                  'token': st.token(access_token='NEWEST')}
        self.assertTrue(shared.write(newest))
        self.assertEqual('NEWEST', self.stored()['token']['access_token'])

    @no_duplicates
    def test_a_fresh_login_wins_over_the_old_token(self):
        st.write_token_file(self.path, expires_in=3600, created_days_ago=5)
        self.endpoint.answer_exchange(st.token(access_token='LOGIN'))
        context = auth.get_auth_context(API_KEY, CALLBACK_URL)
        auth.client_from_received_url(
                API_KEY, APP_SECRET, context, st.received_url(context),
                auth.token_file_writer(self.path, shared=True),
                transport=self.endpoint)
        self.assertEqual('LOGIN', self.stored()['token']['access_token'])
        self.assertIn('received_at', self.stored())


class AdoptTest(SharedTokenTestCase):

    @no_duplicates
    def test_a_client_takes_the_token_another_refreshed(self):
        for asyncio_ in (False, True):
            with self.subTest(asyncio=asyncio_):
                st.write_token_file(self.path, expires_in=-60)
                endpoint = st.FakeTokenEndpoint()
                endpoint.on_api(lambda request: {'ok': True})
                endpoint.answer_refresh(st.token(access_token='FIRST'))
                # Answered, so a second refresh is counted, not refused.
                endpoint.answer_refresh(st.token(), repeat=True)
                one = self.client(asyncio_, endpoint=endpoint, lock_timeout=2)
                two = self.client(asyncio_, endpoint=endpoint, lock_timeout=2)
                self.quote(one)
                self.quote(two)
                self.assertEqual(1, endpoint.posts('refresh_token'),
                                 'the second client refreshed again')
                self.assertEqual('Bearer FIRST',
                                 endpoint.requests[-1].headers[
                                     'Authorization'])
                # The token a stream logs in with is the one taken too.
                self.assertEqual('FIRST',
                                 two.token_metadata.token['access_token'],
                                 "the client's own view kept the old token")

    @no_duplicates
    def test_ensure_token_fresh_says_fresh_when_it_took_a_token(self):
        # It sent nothing, so it did not refresh.
        for asyncio_ in (False, True):
            with self.subTest(asyncio=asyncio_):
                st.write_token_file(self.path, expires_in=-60)
                endpoint = st.FakeTokenEndpoint()
                endpoint.answer_refresh(st.token(access_token='FIRST'))
                one = self.client(asyncio_, endpoint=endpoint)
                two = self.client(asyncio_, endpoint=endpoint)
                outcomes = []
                for client in (one, two):
                    result = client.ensure_token_fresh()
                    if asyncio.iscoroutine(result):
                        result = asyncio.run(result)
                    outcomes.append(result)
                self.assertEqual([schwaby.TokenFreshness.REFRESHED,
                                  schwaby.TokenFreshness.FRESH], outcomes,
                                 'a token taken was called a refresh')
                self.assertEqual(1, endpoint.posts('refresh_token'))

    @no_duplicates
    def test_a_token_taken_inside_the_margin_is_refreshed_once_more(self):
        # ensure_token_fresh(600) finds another client's newer token in the
        # file, with less than 600 s left: it takes it, then refreshes it.
        for asyncio_ in (False, True):
            with self.subTest(asyncio=asyncio_):
                st.write_token_file(self.path, expires_in=-60,
                                    created_days_ago=1)
                endpoint = st.FakeTokenEndpoint()
                endpoint.answer_refresh(st.token(access_token='MINE'))
                client = self.client(asyncio_, endpoint=endpoint)
                st.write_token_file(self.path, expires_in=400,
                                    created_days_ago=0,
                                    token=st.token(refresh_token='NEWER'))
                result = client.ensure_token_fresh(600)
                if asyncio.iscoroutine(result):
                    result = asyncio.run(result)
                self.assertIs(schwaby.TokenFreshness.REFRESHED, result,
                              'a token inside the margin was called fresh')
                self.assertEqual(1, endpoint.posts('refresh_token'))
                self.assertIn(b'refresh_token=NEWER',
                              endpoint.requests[-1].content)

    @no_duplicates
    def test_a_token_taken_that_is_still_due_is_refreshed_with_its_own(self):
        st.write_token_file(self.path, expires_in=-60, created_days_ago=1)
        client = self.client()
        newer = st.write_token_file(
                self.path, expires_in=-30, created_days_ago=0,
                token=st.token(refresh_token='NEWER-REFRESH'))
        self.endpoint.answer_refresh(st.token())
        self.quote(client)
        self.assertEqual(1, self.endpoint.posts('refresh_token'))
        self.assertIn(b'refresh_token=NEWER-REFRESH',
                      self.endpoint.requests[0].content)
        # And the client's age is now the newer login's.
        self.assertAlmostEqual(0, client.token_age(), delta=5)
        self.assertNotEqual(newer, None)

    @no_duplicates
    def test_a_stored_token_that_does_not_check_out_is_not_taken(self):
        st.write_token_file(self.path, expires_in=-60, created_days_ago=1)
        client = self.client()
        with open(self.path, 'w') as f:
            json.dump({'creation_timestamp': time.time() + 10,
                       'token': {'access_token': 5}}, f)
        self.endpoint.answer_refresh(st.token(access_token='MINE'))
        self.quote(client)
        self.assertEqual(1, self.endpoint.posts('refresh_token'))

    @no_duplicates
    def test_no_token_is_logged(self):
        st.write_token_file(self.path, expires_in=-60)
        client = self.client()
        other = st.write_token_file(self.path, expires_in=3600,
                                    created_days_ago=0)
        with self.assertLogs('schwaby', logging.DEBUG) as logs:
            self.quote(client)
        text = '\n'.join(logs.output)
        self.assertIn('Took the newer token', text)
        for secret in (other['token']['access_token'],
                       other['token']['refresh_token']):
            self.assertNotIn(secret, text)


class AccessFunctionsTest(SharedTokenTestCase):

    @no_duplicates
    def test_a_reader_and_writer_of_your_own_bytes(self):
        store = {}
        st.write_token_file(self.path, expires_in=-60)
        with open(self.path, 'rb') as f:
            store[self.path] = f.read()
        read, write = auth.shared_token_file(
                self.path, read_bytes=lambda path: store[path],
                write_bytes=store.__setitem__)
        client = auth.client_from_access_functions(
                API_KEY, APP_SECRET, read, write, transport=self.endpoint)
        self.endpoint.answer_refresh(st.token(access_token='BYTES'))
        self.quote(client)
        self.assertEqual('BYTES', json.loads(store[self.path])['token']
                         ['access_token'])
        self.assertTrue(os.path.exists(self.lock))


class ArgumentsTest(SharedTokenTestCase):

    @no_duplicates
    def test_what_cannot_be_used_is_refused(self):
        st.write_token_file(self.path)
        for kwargs, error in (({'shared': 'yes'}, TypeError),
                              ({'shared': False, 'lock_path': 'x'},
                               ValueError),
                              ({'shared': True, 'lock_timeout': -1},
                               ValueError),
                              ({'shared': True, 'lock_timeout': '5'},
                               TypeError),
                              # Refused unshared too, not taken unread.
                              ({'lock_timeout': 'x'}, TypeError),
                              ({'lock_timeout': -1}, ValueError)):
            with self.subTest(**kwargs):
                with self.assertRaises(error):
                    auth.client_from_token_file(
                            self.path, API_KEY, APP_SECRET, **kwargs)
        with self.assertRaises(ValueError):
            auth.client_from_manual_flow(
                    API_KEY, APP_SECRET, CALLBACK_URL, self.path,
                    token_write_func=lambda *a, **k: None, shared=True)

    @no_duplicates
    def test_when_the_login_was_and_when_its_refresh_token_ends(self):
        st.write_token_file(self.path, created_days_ago=2)
        created = auth.token_file_created_at(self.path)
        self.assertAlmostEqual(time.time() - 2 * 86400, created, delta=5)
        self.assertEqual(created + 7 * 86400,
                         auth.refresh_token_expires_at(self.path))
        # Or from the stored token itself, read by the caller.
        with open(self.path) as f:
            stored = json.load(f)
        self.assertEqual(created, auth.token_file_created_at(stored))
        self.assertEqual(created + 7 * 86400,
                         auth.refresh_token_expires_at(stored))
