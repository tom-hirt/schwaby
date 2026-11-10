'''A token file several clients share, in one process or several.

A write, or a refresh with the write it makes, holds an exclusive lock: a
process-wide lock keyed by the token file's real path, and an ``flock`` on
a lock file opened afresh (``msvcrt.locking`` on Windows). On a local disk
an ``flock`` on a second open of the file excludes another thread of the
same process too; the process-wide lock is for where it does not, as over
NFS, where ``flock`` becomes a POSIX lock, which a process holds for all
its threads. A load takes no lock.

Under the lock the file is read first, and a token is written only if it is
at least as new as the file's, so a fresh sign-in is never overwritten by a
refresh of the old token. Before refreshing, a client takes the file's token
instead when another client has refreshed already, and sends nothing.

A sign-in is known by its creation time, which its refreshes keep. Each
sign-in written here records the ones it replaced, and a client's refreshes
carry that record too. A token whose sign-in another has replaced is older
than it, whatever the clocks say: a sign-in dated ahead by a clock since set
back cannot outrank the one after it. Tokens with no record, as older
versions write, are ordered by date.

No token, nor any part of one, is logged here.
'''

import asyncio
import contextlib
import contextvars
import json
import math
import os
import stat
import threading
import time

from . import _redact
from .utils import (TokenErrorKind, TokenFileLockTimeout, TokenRefreshError,
                    UnsafeTokenLockFile)


def get_logger():
    return _redact.get_logger(__name__)


# The real paths whose lock this thread or asyncio task holds, so that the
# write a refresh makes, inside the lock, does not take it again.
_held = contextvars.ContextVar('schwaby_token_lock_held', default=frozenset())

# Set by a caller that wants to know whether its refresh took another
# client's token instead of sending one: a list the refresh appends to. Only
# the client's own refresh sets it -- ensure_token_fresh's, or a paced
# request's -- and it asks for the refresh: one is sent even when the token
# has not expired, as authlib's own call for a request would not be.
_taken = contextvars.ContextVar('schwaby_token_taken', default=None)


@contextlib.contextmanager
def watching_for_a_taken_token():
    '''Around a call of a session's refresh_token: yields a list that is not
    empty afterwards when the refresh took another client's newer token from
    the file and sent nothing. The refresh is asked for: it is sent even when
    the token has not expired.'''
    seen = []
    token = _taken.set(seen)
    try:
        yield seen
    finally:
        _taken.reset(token)


def _asked_for():
    '''Whether this refresh was asked for, by ensure_token_fresh, rather than
    made by a request that found the token expired.'''
    return _taken.get() is not None


def _note_taken():
    seen = _taken.get()
    if seen is not None:
        seen.append(True)


# One lock per token file in this process, by its real path.
_process_locks = {}
_process_locks_guard = threading.Lock()

# How long a wait for another process's lock sleeps between tries.
_POLL = 0.01


def _process_lock(key):
    with _process_locks_guard:
        lock = _process_locks.get(key)
        if lock is None:
            lock = _process_locks[key] = threading.Lock()
        return lock


def _try_lock_fd(fd):
    '''Takes the lock on ``fd`` without waiting; whether it did.'''
    if os.name == 'nt':  # pragma: no cover
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_fd(fd):
    if os.name == 'nt':  # pragma: no cover
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)


def _check_lock_directory(path):
    """Refuses, before anything is sent or signed in, a lock file whose
    directory is not this user's alone or is a symlink: the check the lock
    makes each time, made once when the shared file is set up."""
    if os.name == 'nt':  # pragma: no cover
        return
    directory = os.path.dirname(os.path.abspath(path))
    try:
        dir_fd = os.open(directory,
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError as e:
        raise UnsafeTokenLockFile(
                'The lock file\'s directory {!r} cannot be opened as a '
                'directory without following a symlink: {}'.format(
                    directory, e.strerror)) from None
    try:
        _check_directory_fd(dir_fd, directory)
    finally:
        os.close(dir_fd)


def _check_directory_fd(dir_fd, directory):
    st = os.fstat(dir_fd)
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise UnsafeTokenLockFile(
                'The lock file\'s directory {!r} must belong to this user '
                'and be writable by no one else (chmod go-w); it is {:o}, '
                'owned by uid {}.'.format(
                    directory, st.st_mode & 0o777, st.st_uid))


def _open_lock_file(path):
    """Opens the lock file safely: inside a directory that is this user's
    alone, and never through a symlink. Raises UnsafeTokenLockFile
    otherwise. Windows has no owner ids or modes to check."""
    if os.name == 'nt':  # pragma: no cover
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    directory = os.path.dirname(os.path.abspath(path))
    try:
        dir_fd = os.open(directory,
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise UnsafeTokenLockFile(
                'The lock file\'s directory {!r} cannot be opened as a '
                'directory without following a symlink: {}'.format(
                    directory, e.strerror)) from None
    try:
        _check_directory_fd(dir_fd, directory)
        try:
            fd = os.open(os.path.basename(path),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
                         dir_fd=dir_fd)
        except OSError as e:
            raise UnsafeTokenLockFile(
                    'The lock file {!r} cannot be opened without following a '
                    'symlink: {}'.format(path, e.strerror)) from None
    finally:
        os.close(dir_fd)
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise UnsafeTokenLockFile(
                'The lock file {!r} must be a regular file belonging to this '
                'user.'.format(path))
    return fd


def _number(value):
    '''An ordering value: a finite real number, else lower than any.'''
    return value if _is_time(value) else -math.inf


#: How many earlier sign-ins a sign-in remembers having replaced.
_REPLACED_KEPT = 16


def _is_time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # An int too large for a float, as only a file not written here
        # holds.
        return False


def _sign_in_of(wrapped):
    '''A stored token's sign-in, as its creation time, and the sign-ins that
    one replaced: (None, ()) for a token with no creation time to know it by.
    '''
    if not isinstance(wrapped, dict):
        return None, ()
    sign_in = wrapped.get('creation_timestamp')
    if not _is_time(sign_in):
        return None, ()
    replaced = wrapped.get('replaces')
    if not isinstance(replaced, (list, tuple)):
        replaced = ()
    return sign_in, tuple(r for r in replaced[:_REPLACED_KEPT]
                          if _is_time(r))


def _replaces(current, own):
    '''What a sign-in created at ``own`` records on replacing the stored
    token ``current``: the current one's sign-in, then what that replaced,
    the newest kept. Never its own time, which a sign-in in the same second
    as the one it replaces would otherwise name.'''
    sign_in, replaced = _sign_in_of(current)
    if sign_in is None:
        return []
    return [r for r in (sign_in, *replaced) if r != own][:_REPLACED_KEPT]


def _succession(ours, theirs):
    '''True when ``ours`` comes of a sign-in that replaced ``theirs``'s,
    False when the other way round, None when neither says.'''
    our_id, ours_replaced = _sign_in_of(ours)
    their_id, theirs_replaced = _sign_in_of(theirs)
    if our_id is None or their_id is None or our_id == their_id:
        return None
    ours_after, theirs_after = (their_id in ours_replaced,
                                our_id in theirs_replaced)
    if ours_after == theirs_after:
        # Neither names the other, or each does, as only a clock set back
        # by exactly the time between them could make: the dates decide.
        return None
    return ours_after


#: A login dated further ahead than this is a wrong clock's, not newer.
_FUTURE_SLACK = 60 * 60


def _order(wrapped):
    '''What a stored token is ordered by: its login's creation time, then
    its access token's expiry. A login dated more than an hour ahead ranks
    oldest, so a wrong clock's token cannot outrank every sign-in after it,
    nor its own refreshes outrank a sign-in.'''
    if not isinstance(wrapped, dict):
        return (-math.inf, -math.inf)
    token = wrapped.get('token')
    expires_at = token.get('expires_at') if isinstance(token, dict) else None
    created = _number(wrapped.get('creation_timestamp'))
    if created > time.time() + _FUTURE_SLACK:
        return (-math.inf, -math.inf)
    return (created, _number(expires_at))


def is_at_least_as_new(ours, theirs):
    '''Whether stored token ``ours`` is at least as new as ``theirs``:
    ordered by the login's creation time, then the access token's expiry,
    and then, only when both carry one, the time each arrived. A token
    written without one, by an older version, is ordered by the first two
    alone, as that version orders them.'''
    if theirs is None:
        return True
    succession = _succession(ours, theirs)
    if succession is not None:
        return succession
    a, b = _order(ours), _order(theirs)
    if a != b:
        return a > b
    received = [w.get('received_at') if isinstance(w, dict) else None
                for w in (ours, theirs)]
    if all(isinstance(r, (int, float)) and not isinstance(r, bool)
           for r in received):
        return _number(received[0]) >= _number(received[1])
    return True


def _check_timeout(lock_timeout):
    if (isinstance(lock_timeout, bool)
            or not isinstance(lock_timeout, (int, float))):
        raise TypeError('lock_timeout must be a number of seconds')
    if not math.isfinite(lock_timeout) or lock_timeout < 0:
        raise ValueError('lock_timeout must be a finite number of seconds, '
                         'zero or more, got {!r}'.format(lock_timeout))
    return float(lock_timeout)


def _default_read_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


class _Claim:
    '''An asyncio task's wait for the lock, shared with the worker thread
    that takes it.'''

    def __init__(self):
        self.guard = threading.Lock()
        self.abandoned = False
        self.fd = None


class SharedTokenFile:
    '''One client's view of a shared token file: how it is read, written
    and locked.'''

    def __init__(self, token_path, *, read_bytes=None, write_bytes=None,
                 lock_path=None, lock_timeout=10.0, default_write=None):
        if not isinstance(token_path, (str, os.PathLike)):
            raise TypeError('token_path must be a str or an os.PathLike')
        token_path = os.fspath(token_path)
        if not isinstance(token_path, str):
            raise TypeError('token_path must be a str path, not bytes')
        if lock_path is not None:
            if not isinstance(lock_path, (str, os.PathLike)):
                raise TypeError('lock_path must be a str or an os.PathLike')
            lock_path = os.fspath(lock_path)
            if not isinstance(lock_path, str):
                raise TypeError('lock_path must be a str path, not bytes')
        for name, function in (('read_bytes', read_bytes),
                               ('write_bytes', write_bytes)):
            if function is not None and not callable(function):
                raise TypeError('{} must be callable or None'.format(name))
        self.token_path = token_path
        self.lock_path = lock_path
        self.lock_timeout = _check_timeout(lock_timeout)
        _check_lock_directory(self._lock_file(self._key()))
        self.read_bytes = read_bytes or _default_read_bytes
        self.write_bytes = write_bytes or default_write
        # When the client's current token arrived, as written or loaded.
        self.received_at = None
        # The sign-in the client's current token comes of, by its creation
        # time, and what that replaced, as written or loaded: its refreshes
        # are written with the record.
        self.sign_in = None
        self.replaced = ()

    # ---------------------------------------------------------- the lock

    def _key(self):
        return os.path.realpath(self.token_path)

    def _lock_file(self, key):
        return self.lock_path if self.lock_path is not None else key + '.lock'

    def _acquire(self, key):
        '''Takes both locks, within the timeout; returns the lock file's
        descriptor. Raises TokenFileLockTimeout past it.'''
        deadline = time.monotonic() + self.lock_timeout
        process_lock = _process_lock(key)
        if not process_lock.acquire(timeout=self.lock_timeout):
            raise self._timed_out()
        try:
            fd = _open_lock_file(self._lock_file(key))
        except BaseException:
            process_lock.release()
            raise
        try:
            while not _try_lock_fd(fd):
                if time.monotonic() >= deadline:
                    raise self._timed_out()
                time.sleep(_POLL)
        except BaseException:
            os.close(fd)
            process_lock.release()
            raise
        return fd

    def _acquire_for(self, key, claim):
        '''Takes the lock for an asyncio task, in a worker thread: when the
        task has given up waiting by then, gives it back here, since the
        task's event loop may be gone and nothing there would.'''
        fd = self._acquire(key)
        with claim.guard:
            if claim.abandoned:
                self._release(key, fd)
                return None
            claim.fd = fd
        return fd

    def _release(self, key, fd):
        try:
            _unlock_fd(fd)
        finally:
            os.close(fd)
            _process_lock(key).release()

    def _timed_out(self):
        return TokenFileLockTimeout(
                'The token file is locked by another client, and stayed '
                'locked for the {:g} seconds of lock_timeout.'.format(
                    self.lock_timeout))

    @contextlib.contextmanager
    def lock(self):
        '''Holds the lock, or does nothing when this thread or task holds it
        already.'''
        key = self._key()
        held = _held.get()
        if key in held:
            yield
            return
        fd = self._acquire(key)
        token = _held.set(held | {key})
        try:
            yield
        finally:
            _held.reset(token)
            self._release(key, fd)

    @contextlib.asynccontextmanager
    async def alock(self):
        '''The lock, taken in a worker thread so the event loop does not wait
        on it. A task cancelled while it waits gives back the lock it would
        have taken.'''
        key = self._key()
        held = _held.get()
        if key in held:
            yield
            return
        claim = _Claim()
        taking = asyncio.ensure_future(
                asyncio.to_thread(self._acquire_for, key, claim))
        try:
            fd = await asyncio.shield(taking)
        except asyncio.CancelledError:
            # Whichever of the two comes second gives the lock back: the
            # worker, if it takes the lock after this, or this, if the
            # worker took it already.
            with claim.guard:
                claim.abandoned = True
                fd, claim.fd = claim.fd, None
            if fd is not None:
                self._release(key, fd)
            raise
        token = _held.set(held | {key})
        try:
            yield
        finally:
            _held.reset(token)
            self._release(key, fd)

    # ---------------------------------------------- reading and writing

    def read(self):
        '''The stored token, as loaded; raises as loading a token file
        does. Takes no lock.'''
        data = self.read_bytes(self.token_path)
        try:
            stored = json.loads(data)
        except RecursionError:
            raise ValueError(
                    'The token file is nested too deeply to be a token this '
                    'library wrote. Delete it and create a new one.') from None
        if isinstance(stored, dict):
            received = stored.get('received_at')
            if isinstance(received, (int, float)) and not isinstance(
                    received, bool):
                self.received_at = received
            self.remember_sign_in(stored)
        return stored

    def remember_sign_in(self, stored):
        '''Makes the client's sign-in the one ``stored`` comes of.'''
        self.sign_in, self.replaced = _sign_in_of(stored)

    def _read_or_none(self):
        '''The stored token, or None when there is none to compare with: no
        file, or one that is not JSON.'''
        try:
            return json.loads(self.read_bytes(self.token_path))
        except FileNotFoundError:
            return None
        except (ValueError, RecursionError):
            return None

    def write(self, wrapped, *args, sign_in=False, **kwargs):
        '''Writes the stored token ``wrapped`` under the lock, if it is at
        least as new as the file's, or whatever the file holds when
        ``sign_in`` says it is a fresh sign-in's; whether it did. A sign-in
        records the sign-in it replaced; any other token is written with the
        record of the client's sign-in.'''
        wrapped = dict(wrapped)
        wrapped['received_at'] = time.time()
        with self.lock():
            if sign_in:
                # Read only for the record: a sign-in is the way out of a
                # file that cannot be read, so nothing here may stop it.
                try:
                    current = self._read_or_none()
                except Exception:
                    current = None
            else:
                current = self._read_or_none()
            if sign_in:
                wrapped['replaces'] = _replaces(
                        current, wrapped.get('creation_timestamp'))
            elif (self.replaced and self.sign_in is not None
                    and _sign_in_of(wrapped)[0] == self.sign_in):
                wrapped.setdefault('replaces', list(self.replaced))
            if not sign_in and not is_at_least_as_new(wrapped, current):
                get_logger().info(
                        'Kept the newer token in %s: the token it would have '
                        'replaced is older', self.token_path)
                return False
            data = json.dumps(wrapped).encode('utf-8')
            self.write_bytes(self.token_path, data)
            self.received_at = wrapped['received_at']
            self.remember_sign_in(wrapped)
            return True

    # ------------------------------------------------ the refresh hook

    def newer_stored(self, metadata, session):
        '''The file's token when it is newer than the client's, else None.
        Called under the lock.'''
        stored = self._read_or_none()
        if not isinstance(stored, dict) or not isinstance(
                stored.get('token'), dict):
            return None
        ours = {'creation_timestamp': metadata.creation_timestamp,
                'token': dict(session.token or {}),
                'received_at': self.received_at,
                'replaces': list(self.replaced)}
        if is_at_least_as_new(ours, stored):
            return None
        return stored


def _lock_timeout_error(error):
    return TokenRefreshError(
            'Failed to refresh the Schwab token: ' + str(error) + ' Nothing '
            'was sent.', kind=TokenErrorKind.TRANSIENT)


def share_refresh(session, shared, metadata, adopt, *, is_async):
    '''Makes ``session``'s refresh hold ``shared``'s lock, and take the
    file's token instead when it is newer. ``adopt(stored)`` makes the
    client's view of the token the file's, and says whether it could: a
    stored token that does not check out is not taken.'''
    refresh = session.refresh_token

    def taken(stored, kwargs):
        '''Takes the file's newer token; whether it needs no refresh.'''
        if not adopt(stored):
            return False
        received = stored.get('received_at')
        shared.received_at = (received if isinstance(received, (int, float))
                              and not isinstance(received, bool) else None)
        shared.remember_sign_in(stored)
        get_logger().info('Took the newer token another client wrote to %s',
                          shared.token_path)
        if not session.token.is_expired(leeway=getattr(session, 'leeway', 0)):
            return True
        # Still due: refreshed with the file's refresh token, not the old.
        kwargs['refresh_token'] = session.token.get('refresh_token')
        return False

    def refreshed_meanwhile():
        '''A request's refresh, made because the token had expired, that
        waited for the lock while another thread's refresh, or a token
        taken, made it fresh: it sends nothing. One asked for is sent.'''
        return not _asked_for() and session.token is not None and not (
                session.token.is_expired(leeway=getattr(session, 'leeway', 0)))

    if is_async:
        async def shared_refresh(*args, **kwargs):
            try:
                async with shared.alock():
                    stored = shared.newer_stored(metadata, session)
                    if stored is not None and taken(stored, kwargs):
                        _note_taken()
                        return session.token
                    if stored is None and refreshed_meanwhile():
                        return session.token
                    return await refresh(*args, **kwargs)
            except TokenFileLockTimeout as e:
                raise _lock_timeout_error(e) from e
    else:
        def shared_refresh(*args, **kwargs):
            try:
                with shared.lock():
                    stored = shared.newer_stored(metadata, session)
                    if stored is not None and taken(stored, kwargs):
                        _note_taken()
                        return session.token
                    if stored is None and refreshed_meanwhile():
                        return session.token
                    return refresh(*args, **kwargs)
            except TokenFileLockTimeout as e:
                raise _lock_timeout_error(e) from e
    session.refresh_token = shared_refresh
