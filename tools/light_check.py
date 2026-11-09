'''The light check every commit runs: what can be judged in a second, on what
is about to be committed.

    python tools/light_check.py            # the staged changes against HEAD

Run by tools/hooks/pre-commit and tools/hooks/pre-merge-commit; `git config
core.hooksPath tools/hooks` turns them on in a clone. The gate (every test,
once) and the red-proof are the heavy checks, run at a release rather than
on every commit.

It checks the index, not the working tree, because the index is what the
commit will contain:

 * every staged Python file parses, and gains no pyflakes warning it did not
   have at HEAD. The library has warnings it is built around -- the package
   __init__ files import names to re-export them -- so requiring none would
   fail every commit; requiring no new one catches the unused import or
   undefined name a change introduces;
 * `git diff --cached --check`: no whitespace errors or conflict markers;
 * no staged line signals a process it did not start and record: a pid
   found rather than started (its parent's, by any lookup, or one found by
   name or read from /proc), pid -1, which reaches every process the user
   owns, its own process group, which can hold whatever started it, unless
   the line says it leads that group (`own-group:` in a comment), or a
   user's, a group's or a name's processes. A parent found that way is
   whoever adopted an orphan, often the session manager, whose children are
   the whole desktop. Reading the parent's pid without signalling it, a
   signal to a pid or a group the code started and recorded, and signal 0,
   which only asks whether a process exists, are let through. Each command
   on a line is judged as the shell or Python would read it, every line of
   the file is read, a pid kept in a name and a call over several lines
   are judged too, and each line refused is named. Nor may a staged line
   delete in shared /tmp or the temp root itself. tests/testdata/
   blind_kill_shapes.txt is the list of shapes the check is tested on, and
   it and this file, whose patterns name every shape, are not judged;
 * no staged file, by its name or its content, carries a word on this
   clone's forbidden list, `git config --add lightcheck.forbidden WORD`.
   The list lives in the clone's own configuration, so the repository
   never carries the words it keeps out.
   The whole file, not only its added lines, and any case.

It names the touched sentinels, from the index's tools/reads.json: the test
modules whose reads include a file the commit changes. They are the tests to
run before the change merges.

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
    decoding it here would hide that.'''
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


# The words are built from pieces, so that this file, which names them,
# does not read as using them.
_K = 'k' + 'ill'
_P = 'p' + _K
_KA = _K + 'all'
#: The lint's own list of shapes, for its tests: data, not code, and not
#: judged.
SHAPES = 'tests/testdata/blind_kill_shapes.txt'
#: What the blind-kill check does not judge: that list, and this file, whose
#: patterns and their docs name every shape and signal nothing.
UNJUDGED = (SHAPES, 'tools/light_check.py')
#: A line that signals its own group as that group's leader says so.
OWN_GROUP_MARK = 'own-group' + ':'

#: A word that sends a signal.
_SENDS = re.compile(r'\b(?:%s(?:pg)?|%s|%s|terminate|send_signal)\b'
                    % (_K, _P, _KA))
#: A pid found other than by starting it: the parent's, in any spelling,
#: a group number read back, or a pid looked up by name or from /proc.
_FOUND = re.compile(
    r'\bget' + 'pp' + r'id\b|\$\{?' + 'PP' + r'ID\b|\b' + 'pp' + r'id\b'
    r'|\b' + 'PP' + r'id\b|\.parent\(\)|\bparent_pid\b|\bpg(?:id|rp)='
    r'|\b(?:pgrep|pidof|ps|lsof|fuser)\b|\bprocess_iter\b'
    r'|/proc/\S*/stat(?:us)?\b')
#: The shell's three kill commands, by name or by path; not a method,
#: an option, a variable, or part of a longer name.
_SHELL = re.compile(r'(?<![\w.$-])(?:/[\w./-]*/)?(%s|%s|%s)(?![\w(-])'
                    % (_KA, _P, _K))
#: The shell's commands that kill by what they find, never by a recorded
#: pid: every process, and those using a file or a port.
_FINDERS = re.compile(r'(?<![\w.$-])(?:/[\w./-]*/)?(%s5|fuser)(?![\w(-])'
                      % _KA)
#: Python's kill and killpg, from os or imported bare: not another
#: object's method.
_PY = re.compile(r'(?:\bos\.|(?<![\w.]))%s(pg)?\(' % _K)
#: Node's process.kill, whose first argument is the target.
_NODE = re.compile(r'\bprocess\.%s\(\s*([^,)]*)' % _K)
#: An argv list or tuple holding the command as a word, first or after a
#: wrapper (sudo, env, ...).
_ARGV = re.compile(r'(?:\[|(?<![\w)\]])\()\s*(?:([\'"])[^\'"]*\1\s*,\s*)*?'
                   r'([\'"])(?:[^\'"\s]*/)?(%s|%s|%s)\2\s*,'
                   % (_KA, _P, _K))
#: What a target that names every process, or the caller's own group, is.
_EVERY = re.compile(r'-0*1')
_OWN = re.compile(r'-?0+|-\$\$|-\$\{\$\}')
#: The caller's own group, as killpg's target; negated, as kill's.
_OWN_CALL = (r'(?:os\.)?(?:getpgrp\(\)|getpid\(\)'
             r'|getpgid\((?:0|(?:os\.)?getpid\(\))\)'
             r'|getsid\((?:0|(?:os\.)?getpid\(\))\))')
_OWN_GROUP_CALL = re.compile(_OWN_CALL)
_OWN_NEGATED_CALL = re.compile('-' + _OWN_CALL)
#: The signals the shell's commands take as an option, by number or name.
_SIGNAL = re.compile(
    r'-(?:\d+|(?:SIG)?(?:HUP|INT|QUIT|ILL|TRAP|ABRT|IOT|BUS|FPE|KILL|USR1'
    r'|SEGV|USR2|PIPE|ALRM|TERM|STKFLT|CHLD|CONT|STOP|TSTP|TTIN|TTOU|URG'
    r'|XCPU|XFSZ|VTALRM|PROF|WINCH|IO|POLL|PWR|SYS|RTMIN|RTMAX)(?:[+-]\d+)?)')


#: How far a command's words are read: past this, a line is not one command.
_COMMAND_LIMIT = 500


def _words(text):
    """The words of the command that starts text, as the shell would split
    them, up to the end of the command: a separator, a closing quote, a
    comment or the end of the line. Quotes and backslashes are removed;
    a redirection is left out."""
    words, word, k = [], None, 0
    text = text[:_COMMAND_LIMIT]
    while k < len(text):
        c = text[k]
        if c in ' \t':
            if word is not None:
                words.append(word)
                word = None
        elif c == '\\' and k + 1 < len(text):
            word = (word or '') + text[k + 1]
            k += 1
        elif c == '&' and word and word[-1] in '<>':
            word += c
        elif c in ';&|)`\n' or (c == '#' and word is None):
            break
        elif c in '\'"':
            # $'...' is a quoted word too.
            if word == '$':
                word = ''
            opens = not word or word.endswith(('-', '='))
            end = text.find(c, k + 1) if opens else -1
            if end < 0:
                break
            word = (word or '') + text[k + 1:end]
            k = end
        else:
            word = (word or '') + c
        k += 1
    if word is not None:
        words.append(word)
    out = []
    for w in words:
        if re.match(r'\d*[<>]', w):
            continue
        out.append(re.split(r'[<>]', w)[0])
    return out


def _kill_words(words, marked):
    """What a kill with these words reaches that it should not, or None."""
    signal_set = zero = listing = False
    targets, i = [], 0
    while i < len(words):
        w = words[i]
        if w == '--' and not targets:
            targets.extend(words[i + 1:])
            break
        if not signal_set and not targets and w in ('-s', '-n'):
            zero = i + 1 < len(words) and words[i + 1] == '0'
            signal_set, i = True, i + 2
            continue
        if not signal_set and not targets and w.startswith('--signal'):
            value = (w.split('=', 1)[1] if '=' in w else
                     words[i + 1] if i + 1 < len(words) else '')
            zero, signal_set = value == '0', True
            i += 1 if '=' in w else 2
            continue
        if not signal_set and not targets and w in ('-l', '-L', '--list',
                                                    '--table'):
            listing = True
            break
        if not signal_set and not targets and w.startswith('-') and len(w) > 1:
            zero, signal_set = w == '-0', True
        else:
            targets.append(w)
        i += 1
    if zero or listing:
        return None
    for t in targets:
        t = _computed(t)
        if _EVERY.fullmatch(t):
            return 'every process'
        if not marked and (_OWN.fullmatch(t)
                           or _OWN_NEGATED_CALL.fullmatch(t)):
            return 'its own group'
    return None


def _computed(word):
    """A word as the target it computes to, for an argv word that is an
    expression: str(-1) is -1, and an f-string or a sum giving '-' and the
    caller's own pid or group is that call negated."""
    w = re.sub(r'^[fF]?([\'"])(.*)\1$', r'\2', word.strip())
    w = re.sub(r'[\'"{}+\s]', '', w)
    while True:
        unwrapped = re.sub(r'^(-?)(?:str|int)\((.*)\)$', r'\1\2', w)
        if unwrapped == w:
            return w
        w = unwrapped


def _pkill_words(words):
    """What the pattern-matching kill with these words reaches that it
    should not, or None: a user's or a group's processes, or processes
    found by name. Only the children of a recorded parent (-P) are its
    own."""
    i = 0
    while i < len(words):
        w = words[i]
        if w == '--':
            return 'processes found by name' if words[i + 1:] else None
        if w.startswith('--'):
            name = w[2:].split('=', 1)[0]
            if name in ('uid', 'euid', 'user', 'group', 'pgroup',
                        'session'):
                return "a user's or a group's processes"
            if name in ('parent', 'signal') and '=' not in w:
                i += 1
        elif w.startswith('-') and len(w) > 1:
            if _SIGNAL.fullmatch(w):
                i += 1
                continue
            body = w[1:].lstrip('0123456789')
            for j, c in enumerate(body):
                if c in 'uUgGs':
                    return "a user's or a group's processes"
                if c == 'P':
                    value = body[j + 1:]
                    if not value:
                        value = words[i + 1] if i + 1 < len(words) else ''
                        i += 1
                    # The children of init are every orphan on the machine,
                    # and a positional argument is a pid read, not started.
                    parts = value.split(',')
                    if '1' in parts:
                        return 'every orphan'
                    if any(re.fullmatch(r'\$\{?\d+\}?', v) for v in parts):
                        return 'a pid it did not start'
                    break
        else:
            return 'processes found by name'
        i += 1
    return None


def _argv_words(text):
    """The words of an argv list or tuple that starts text, past its first:
    a quoted word as it is, anything else as the expression it is."""
    depth, items, item, k = 0, [], '', 0
    quote = None
    while k < len(text):
        c = text[k]
        if quote:
            item += c
            if c == quote:
                quote = None
        elif c in '\'"':
            quote = c
            item += c
        elif c in '([{':
            depth += 1
            item += c
        elif c in ')]}':
            if depth == 0:
                break
            depth -= 1
            item += c
        elif c == ',' and depth == 0:
            items.append(item.strip())
            item = ''
        else:
            item += c
        k += 1
    if item.strip():
        items.append(item.strip())
    return [w[1:-1] if len(w) >= 2 and w[0] == w[-1] and w[0] in '\'"'
            else w for w in items]


def _py_args(text):
    """The arguments of the call that starts text, split at its top-level
    commas."""
    return _argv_words(text)


def _code(line):
    """The line without a trailing comment: a # at the start of a word,
    outside quotes."""
    quote = None
    for k, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in '\'"':
            quote = c
        elif c == '#' and (k == 0 or line[k - 1] in ' \t'):
            return line[:k]
    return line


def _balanced(text, k):
    """The text inside the brackets that open at text[k], quotes kept."""
    pairs = {'(': ')', '[': ']', '{': '}'}
    depth, quote, j = 0, None, k
    while j < len(text):
        c = text[j]
        if quote:
            if c == quote:
                quote = None
        elif c in '\'"':
            quote = c
        elif c in pairs:
            depth += 1
        elif c in ')]}':
            depth -= 1
            if depth == 0:
                return text[k + 1:j]
        j += 1
    return text[k + 1:]


def _receiver(text, k):
    """The expression a method is called on, ending just before text[k]."""
    opens = {')': '(', ']': '['}
    depth, j = 0, k - 1
    while j >= 0:
        c = text[j]
        if c in opens:
            depth += 1
        elif c in '([':
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and not (c.isalnum() or c in '_.'):
            break
        j -= 1
    return text[j + 1:k]


def _command_text(text):
    """A shell command's text up to the end of the command, with what is in
    $(...) and quotes."""
    depth, quote = 0, None
    for j, c in enumerate(text[:_COMMAND_LIMIT]):
        if quote:
            if c == quote:
                quote = None
        elif c in '\'"':
            quote = c
        elif c == '(':
            depth += 1
        elif c == ')':
            if depth == 0:
                return text[:j]
            depth -= 1
        elif depth == 0 and c in ';|&\n':
            return text[:j]
    return text[:_COMMAND_LIMIT]


def _signals_found(code, names):
    """Whether code signals a pid found rather than started: the receiver
    of a method that signals, the arguments of a call that does, a kill
    command's own words, or what is piped into xargs kill."""
    def found(text):
        return bool(_FOUND.search(text) or names.uses_found(text))
    for m in _SENDS.finditer(code):
        start, end = m.span()
        rest = code[end:]
        args = _balanced(code, end) if rest[:1] == '(' else None
        # Signal 0 only asks whether the pid exists.
        if args is not None and _py_args(args + ')')[-1:] == ['0']:
            continue
        if start and code[start - 1] == '.':
            if found(_receiver(code, start - 1)) or (args and found(args)):
                return True
            continue
        if args is not None:
            if found(args):
                return True
            continue
        if found(_command_text(rest)):
            return True
        before = code[:start]
        if re.search(r'\bxargs\b[^|]*$', before):
            pipeline = re.split(r'[;&\n]', before)[-1]
            if found(pipeline):
                return True
    return False


#: A word that deletes.
_DELETES = re.compile(r'\b(?:rmtree|remove|unlink|rmdir|removedirs)\('
                      r'|(?<![\w.$/-])rm\s+-\w*[rRf]|\s-delete\b'
                      r'|-exec\s+rm\b')
#: A place every process shares: /tmp, or the temp root itself.
_SHARED = re.compile(r'(?<![\w.~-])/tmp\b|\bgettempdir\(\)'
                     r'|\$\{?TMPDIR\b')


def _deletes_shared(code, names):
    """Why code deletes in a place every process shares, or None: /tmp,
    or the temp root itself, by path, by glob, or through a name holding
    one. Its own run's temp directory is its own."""
    if not _DELETES.search(code):
        return None
    if _SHARED.search(code) or names.uses_shared(code):
        return 'a delete in shared /tmp or the temp root'
    return None


class Names:
    """The names a file assigns a found pid, or its own pid or group, to,
    so that a kill of one on a later line is judged as a kill of what it
    holds."""

    _PY_SET = re.compile(r'(?:^|[\s;(])(?:[\w.]+\.)?([A-Za-z_]\w*)\s*'
                         r'(?::[^=]*)?(?<![=!<>])=(?!=)(.*)')
    _SH_SET = re.compile(r'(?:^|[\s;])(?:export\s+|local\s+|readonly\s+)?'
                         r'([A-Za-z_]\w*)=(\S*)')
    _OWN_PID = re.compile(r'["\']?\$(?:\$|\{\$\}|BASHPID)["\']?$')

    _FOR = re.compile(r'\bfor\s+([A-Za-z_]\w*)\s+in\s+(.*)')

    def __init__(self, lines=()):
        self.found, self.own_pid, self.own_group = set(), set(), set()
        self.shared = set()
        for line in lines:
            code = _code(line)
            for m in self._PY_SET.finditer(code):
                # One statement's value: a ; starts the next.
                self.note(m.group(1), m.group(2).split(';', 1)[0])
            for m in self._SH_SET.finditer(code):
                self.note(m.group(1), m.group(2).rstrip(';'))
            for m in self._FOR.finditer(code):
                self.note(m.group(1), m.group(2))

    def note(self, name, value):
        value = value.strip()
        if _SHARED.search(value) or self.uses_shared(value):
            self.shared.add(name)
        if _FOUND.search(value) or self.uses_found(value):
            self.found.add(name)
        elif self._OWN_PID.match(value) or re.fullmatch(
                r'(?:os\.)?getpid\(\)', value):
            self.own_pid.add(name)
        elif _OWN_GROUP_CALL.fullmatch(value.replace(' ', '')):
            self.own_group.add(name)

    def uses_found(self, code):
        return any(re.search(r'(?<![\w.$])\$?\{?%s\b' % re.escape(n), code)
                   for n in self.found)

    def uses_shared(self, code):
        return any(re.search(r'(?<![\w.$])\$?\{?%s\b' % re.escape(n), code)
                   for n in self.shared)

    def own(self, target, negated):
        """Whether target, a word or an expression, names the caller's own
        group through a name: its group, or its own pid negated."""
        t = target.replace(' ', '').replace('"', '').replace("'", '')
        name = re.fullmatch(r'(-?)\$?\{?([A-Za-z_]\w*)\}?', t)
        if not name:
            return False
        minus, n = name.groups()
        if n in self.own_group:
            return bool(minus) == negated
        return n in self.own_pid and bool(minus)


def blind_kill(line, names=None):
    """Why a line of code signals what it did not start and record, or
    None. A line that only reads its parent's pid, or signals a pid or a
    group it started and recorded, is not one. names, from the rest of the
    file, says which names hold a found pid or its own pid or group."""
    names = names or Names([line])
    marked = OWN_GROUP_MARK in line
    code = _code(line)
    if _signals_found(code, names):
        return 'a pid it did not start'
    why = _deletes_shared(code, names)
    if why:
        return why
    for m in _FINDERS.finditer(line):
        if m.group(1) != 'fuser' or any(
                w.startswith('-') and not w.startswith('--') and 'k' in w
                for w in _words(line[m.end():])):
            return 'processes found by what they use'
    for m in _PY.finditer(line):
        args = _py_args(line[m.end():])
        target = args[0].replace(' ', '') if args else ''
        if len(args) > 1 and args[1].strip() == '0':
            continue
        if _EVERY.fullmatch(target):
            return 'every process'
        own = (_OWN_GROUP_CALL if m.group(1) else _OWN_NEGATED_CALL)
        if not marked and (target == '0' or own.fullmatch(target)
                           or names.own(target, negated=not m.group(1))):
            return 'its own group'
    for m in _NODE.finditer(line):
        target = m.group(1).replace(' ', '')
        if _EVERY.fullmatch(target):
            return 'every process'
        if not marked and target in ('0', '-process.pid'):
            return 'its own group'
    for m in _ARGV.finditer(line):
        words = _argv_words(line[m.end():])
        why = (_kill_words(words, marked) if m.group(3) == _K else
               "a user's or processes found by name" if m.group(3) == _KA
               else _pkill_words(words))
        if why:
            return why
    for m in _SHELL.finditer(line):
        words = _words(line[m.end():])
        if m.group(1) == _K:
            why = _kill_words(words, marked)
            if not why and not marked and any(
                    names.own(w, negated=True) for w in words):
                why = 'its own group'
        elif m.group(1) == _KA:
            why = "a user's processes or processes found by name"
        else:
            why = _pkill_words(words)
        if why:
            return why
    return None


#: How many lines one call or command may run over and still be read as one.
_JOIN_LIMIT = 30


def _depth(line):
    """How many brackets the line leaves open, outside quotes."""
    depth, quote = 0, None
    for c in _code(line):
        if quote:
            if c == quote:
                quote = None
        elif c in '\'"':
            quote = c
        elif c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
    return depth


def _logical(lines):
    """(first line number, the text) of each call or command that runs
    over more than one line: one left open by a bracket, or continued by a
    backslash."""
    out, n = [], 0
    while n < len(lines):
        depth, joined, k = _depth(lines[n]), lines[n], n
        while (k + 1 < len(lines) and k - n < _JOIN_LIMIT
               and (depth > 0 or lines[k].rstrip().endswith('\\'))):
            k += 1
            joined = joined.rstrip().rstrip('\\') + ' ' + lines[k].strip()
            depth += _depth(lines[k])
        if k > n:
            out.append((n + 1, joined))
        n = k + 1
    return out


def blind_kill_lines(content, path=None):
    """The line numbers at which content signals what it did not start
    and record: each line alone, each call or command that runs over
    several lines as one, and a kill of a name the file assigned a found
    pid to. The lint's own list of shapes is not judged."""
    if path in UNJUDGED:
        return []
    lines = content.decode('utf-8', 'replace').split('\n')
    logical = _logical(lines)
    names = Names(lines + [text for _, text in logical])
    found = {n for n, line in enumerate(lines, 1)
             if blind_kill(line, names)}
    found |= {n for n, text in logical if blind_kill(text, names)}
    return sorted(found)


def blind_kills_in(path):
    """The line numbers at which the staged file does what
    blind_kill_lines finds."""
    return blind_kill_lines(blob(':0:' + path) or b'', path)


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
    '''The touched sentinels: the test modules whose reads, by the
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
        for line in blind_kills_in(path):
            print('light check: %s:%d: signals a process it did not start '
                  'and record (its parent, pid -1, its own group, a user\'s '
                  'or a name\'s processes), or deletes in shared /tmp or the '
                  'temp root; signal only a pid or group it started and '
                  'recorded, and delete only its own temp' % (path, line))
            failed = True
    check = git('diff', '--cached', '--check', check=False)
    if check.returncode != 0:
        sys.stdout.write(re.sub(r'(?m)^(?=.)', 'light check: ',
                                check.stdout.decode('utf-8', 'replace')))
        failed = True
    try:
        tests = reached_tests()
    except (Exception, SystemExit) as e:
        print('light check: could not name the touched sentinels '
              '(%s: %s); tools/reads.json is the map' % (type(e).__name__, e))
        tests = []
    if tests:
        print('light check: %s; run %s before the change merges: '
              'python -m pytest %s' % (
                  '1 touched sentinel (a test module that reads a file this '
                  'commit changes)' if len(tests) == 1 else
                  '%d touched sentinels (test modules that read a file this '
                  'commit changes)' % len(tests),
                  'it' if len(tests) == 1 else 'them', ' '.join(tests)))
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
