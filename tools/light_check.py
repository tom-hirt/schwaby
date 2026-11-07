'''The light check every commit runs: what can be judged in a second, on what
is about to be committed.

    python tools/light_check.py            # the staged changes against HEAD

Run by tools/hooks/pre-commit and tools/hooks/pre-merge-commit; `git config
core.hooksPath tools/hooks` turns them on in a clone. The full suite and the
red-proof are the heavy checks, run at a release rather than on every
commit.

It checks the index, not the working tree, because the index is what the
commit will contain:

 * every staged Python file parses, and gains no pyflakes warning it did not
   have at HEAD. The library has warnings it is built around -- the package
   __init__ files import names to re-export them -- so requiring none would
   fail every commit; requiring no new one catches the unused import or
   undefined name a change introduces;
 * `git diff --cached --check`: no whitespace errors or conflict markers;
 * no staged file, by its name or its content, carries a word on this
   clone's forbidden list, `git config --add lightcheck.forbidden WORD`.
   The list lives in the clone's own configuration, so the repository
   never carries the words it keeps out.
   The whole file, not only its added lines, and any case.

It names the test modules the commit reaches, from the index's
tools/reads.json: those whose reads include a file it changes. They are the
tests to run before the change merges.

When the commit changes a file under tests/, it names the red-proof entries
it reaches too, by tools/redproof.py's own rule: the release red-proof runs them, and
`tools/redproof.py --touching main` runs the whole branch's set, these among
them, to see early. That is a note, not a failure, and so is not being able
to work it out.

A warning is compared by its text without its line number, as a count per
file, so moving code does not read as adding a warning, and a renamed file is
compared with its old path at HEAD. A syntax error is never subtracted: a
file that does not parse, or that parses and does not compile, fails, whatever
HEAD had.
'''

import collections
import importlib.util
import os
import re
import subprocess
import sys
import tempfile

try:
    from pyflakes import api
except ImportError:
    api = None


class _Collect:
    '''A pyflakes reporter that keeps the messages.'''

    def __init__(self):
        self.messages = []

    def unexpectedError(self, filename, msg):
        self.messages.append('error: %s' % msg)

    def syntaxError(self, filename, msg, lineno, offset, text):
        self.messages.append('syntax error: %s' % msg)

    def flake(self, message):
        # Some messages name a line ("redefinition of unused 'x' from line
        # 20"): without it a warning moved by an edit above it is the same
        # warning, not a new one.
        self.messages.append(re.sub(r'\bline \d+', 'line N',
                                    message.message % message.message_args))


def warnings_in(source, filename):
    '''The pyflakes messages for source, without line numbers.'''
    collect = _Collect()
    api.check(source, filename, collect)
    return collections.Counter(collect.messages)


def git(*args, check=True):
    return subprocess.run(['git', *args], capture_output=True, check=check)


def blob(spec):
    '''The bytes of a blob, or None if there is none. Bytes, not text: a
    stray byte that is not UTF-8 makes a file Python refuses to import, and
    decoding it here hid that.'''
    out = git('show', spec, check=False)
    return out.stdout if out.returncode == 0 else None


def staged_python():
    '''(path, its path at HEAD) for each staged Python file. A renamed file
    is compared with what it was, which is not a new file with every warning
    new; a symlink is not code.'''
    out = git('diff', '--cached', '--raw', '-z', '-M', '--diff-filter=ACMRT')
    fields = out.stdout.split(b'\0')
    staged = []
    i = 0
    while i < len(fields) and fields[i].startswith(b':'):
        # :old-mode new-mode old-sha new-sha status, then one path, or two
        # for a rename or a copy
        meta = fields[i].decode('ascii').split()
        if meta[4][0] in 'RC':
            before, path = os.fsdecode(fields[i + 1]), os.fsdecode(fields[i + 2])
            i += 3
        else:
            before = path = os.fsdecode(fields[i + 1])
            i += 2
        if path.endswith('.py') and meta[1] != '120000':
            staged.append((path, before))
    return staged


def staged_paths():
    '''Every staged path that the commit adds or changes.'''
    out = git('diff', '--cached', '--name-only', '-z', '--diff-filter=ACMRT')
    return [os.fsdecode(p) for p in out.stdout.split(b'\0') if p]


def forbidden_words():
    '''This clone's forbidden list, `git config lightcheck.forbidden`, in
    lower case. A clone that configures none forbids nothing. (A
    configuration git cannot read fails every git command here, and the
    check with them.)'''
    out = git('config', '--get-all', 'lightcheck.forbidden', check=False)
    return sorted({w.strip().lower() for w in
                   out.stdout.decode('utf-8', 'replace').splitlines()
                   if w.strip()})


def forbidden_in(path, words):
    '''The forbidden words the staged file carries, in its name or its
    content, in any case.'''
    # Stage 0 named outright: ':0:x' would read as stage 0 of 'x'.
    content = (blob(':0:' + path) or b'').lower()
    name = path.lower()
    return [word for word in words
            if word in name or word.encode('utf-8') in content]


def compile_error(source, path):
    '''The error CPython raises compiling source, or None. pyflakes only
    parses, and some code parses and still cannot be compiled: `await`
    outside an async function, `nonlocal` at module level, a value returned
    from an async generator.'''
    try:
        compile(source, path, 'exec', dont_inherit=True)
    except (SyntaxError, ValueError) as e:
        return 'syntax error: %s' % (getattr(e, 'msg', None) or e)
    return None


def new_warnings(path, before_path):
    staged = blob(':0:' + path) or b''
    now = warnings_in(staged, path)
    if not any(m.startswith(('syntax error:', 'error:')) for m in now):
        error = compile_error(staged, path)
        if error:
            now[error] += 1
    before_text = blob('HEAD:' + before_path)
    before = (warnings_in(before_text, before_path)
              if before_text is not None else collections.Counter())
    new = now - before
    # A file that does not parse fails whatever HEAD had: that it did not
    # parse before either is no reason to commit it. On 3.10 a NUL byte is
    # reported as an `error:`, not a syntax error.
    for message in now:
        if message.startswith(('syntax error:', 'error:')):
            new[message] = now[message]
    return sorted(new.elements())


def changed_entries():
    '''The names of the red-proof entries whose test file or mutated file is
    a test file this commit changes, and the shared helpers among those, by
    tools/redproof.py's rule and from the index's entries. Nothing when the
    commit changes no test file, or the checkout has no red-proof.'''
    out = git('diff', '--cached', '--name-only', '--no-renames', '-z')
    changed = {os.fsdecode(p) for p in out.stdout.split(b'\0') if p}
    tool = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'redproof.py')
    if not any(p.startswith('tests/') for p in changed) or not os.path.isfile(
            tool):
        return [], []
    spec = importlib.util.spec_from_file_location('redproof', tool)
    redproof = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(redproof)
    entries = blob(':0:' + redproof.CASES)
    if entries is None:
        return [], []
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, os.path.dirname(redproof.CASES)))
        with open(os.path.join(tmp, redproof.CASES), 'wb') as f:
            f.write(entries)
        cases = redproof.load_cases(tmp)
    selected, helpers = redproof.changed_set(
        cases, changed, lambda path: blob(':0:' + path))
    return [c.name for c in selected], helpers


def _load(path):
    spec = importlib.util.spec_from_file_location(
        os.path.basename(path)[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reached_tests():
    '''The test modules this commit reaches: those whose reads, by the
    index's tools/reads.json, include a file it changes, and a new test
    module. They are the tests to run before the change merges.
    Nothing, with a note, when the index has no map or one that is not a
    map; nothing when the checkout has no tools to read it with.'''
    here = os.path.dirname(os.path.abspath(__file__))
    tools = [os.path.join(here, name) for name in ('redproof.py', 'reads.py')]
    if not all(map(os.path.isfile, tools)):
        return []
    redproof, reads = (_load(path) for path in tools)

    def read(path):
        return blob(':0:' + path)
    found = redproof.reads_map(read)
    if found is None:
        print('light check: %s is missing from the index or is not a map, '
              'so no tests were named; python -I -B tools/reads.py writes '
              'it' % redproof.READS)
        return []
    names = git('diff', '--cached', '-z', '--name-only', '--no-renames')
    changed = {os.fsdecode(p) for p in names.stdout.split(b'\0') if p}
    # A test module the commit deletes is not one to run.
    return [t for t in reads.reached(found, changed,
                                     redproof.test_patterns(read))
            if read(t) is not None]


def main():
    if api is None:
        print('light check: pyflakes is not installed for %s; '
              "`pip install -e '.[dev]'` in your virtualenv installs it"
              % sys.executable)
        return 1
    failed = False
    for path, before in staged_python():
        for message in new_warnings(path, before):
            print('light check: %s: %s' % (path, message))
            failed = True
    words = forbidden_words()
    for path in staged_paths():
        for word in forbidden_in(path, words):
            print('light check: %s: carries %r, a word on the forbidden '
                  'list' % (path, word))
            failed = True
    check = git('diff', '--cached', '--check', check=False)
    if check.returncode != 0:
        sys.stdout.write(re.sub(r'(?m)^(?=.)', 'light check: ',
                                check.stdout.decode('utf-8', 'replace')))
        failed = True
    try:
        tests = reached_tests()
    except (Exception, SystemExit) as e:
        print('light check: could not name the tests this commit reaches '
              '(%s: %s); tools/reads.json is the map' % (type(e).__name__, e))
        tests = []
    if tests:
        print('light check: %d test module%s read a file this commit changes; '
              'run them before the change merges: python -m pytest %s' % (
                  len(tests), '' if len(tests) == 1 else 's',
                  ' '.join(tests)))
    try:
        names, helpers = changed_entries()
    except (Exception, SystemExit) as e:
        # Executing the entries can fail on what the commit stages; that is
        # the red-proof's to report, not a reason to refuse the commit, and
        # an exit there must not decide the check's answer either way.
        print('light check: could not name the red-proof entries this '
              "commit's test changes reach (%s: %s); `tools/redproof.py "
              "--touching main` will say" % (type(e).__name__, e))
        names, helpers = [], []
    if helpers:
        print('light check: this commit changes a shared test helper (%s), '
              'which every red-proof entry may read: the release red-proof '
              'runs them, and --touching would be a release-sized run'
              % ', '.join(helpers))
    elif names:
        print('light check: %d red-proof %s a file this commit changes; '
              'the release red-proof runs them, and '
              "`tools/redproof.py --touching main` runs the branch's set:" % (
                  len(names),
                  'entry reaches' if len(names) == 1 else 'entries reach'))
        for name in names:
            print('light check:   ' + name)
    if failed:
        print('light check: failed, so nothing was committed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
