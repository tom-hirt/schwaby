'''Each test module's direct surface, the repository files it reads
itself: the map that chooses the tests a change reaches.

    python -I -B tools/reads.py                  # rewrite tools/reads.json
    python -I -B tools/reads.py --check          # exit 1, naming them, if stale
    python -I -B tools/reads.py --touching main  # the tests a branch reaches

A test module reads:

 * itself, setup.cfg, and the conftest.py and package __init__.py files
   pytest loads for it, whether or not they exist yet, with the modules a
   conftest.py imports, since its fixtures run for the test;
 * every repository module it imports itself;
 * every repository file it names in a string, or in a piece of one with no
   space split at colons, commas and the like: a tracked file's path, its name (README.md, preferences.json), a Python
   file's name without .py (load('redproof')), or a directory's name or
   path (docs), kept as the directory, `docs/`, so a file added under it
   later is read too.

That is its direct surface: what it imports or names itself, not what
those import in turn, which the full suite at a release covers. `closure()`
follows the imports, for a check that wants the whole reach.

An entry ending in / is a directory, one holding *, ? or [ a pattern, and
anything else a file. It is read from the source, not measured by running
the tests, so it is recomputed exactly and checked by a test. What that
cannot see -- an import or a path built at run time from pieces -- is in
DECLARED, with its reason. Naming a file a test does not read costs a test
run; missing one of its direct reads is the failure this exists to prevent,
so it errs towards naming.

The light check names the test modules a commit's changes reach from this
map, --touching names a branch's, and tools/redproof.py --touching chooses
red-proof entries from it too.
'''

import ast
import fnmatch
import importlib.util
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP = 'tools/reads.json'

#: What a test module reads that its source cannot show: test module to
#: patterns over repository paths, each with its reason.
DECLARED = {
    'tests/light_check_test.py': {
        '*': 'its forbidden-word scan reads every tracked file',
    },
    'tests/reads_test.py': {
        '*': 'it rebuilds this map from every tracked file',
    },
    'tests/entrypoints_test.py': {
        '*': 'it runs reads.py --check, which reads every tracked file',
    },
    'tests/streaming_test.py': {
        'schwaby/utils.py': 'it tests behaviour streaming.py takes from '
                            'utils.py, which its red-proof entries mutate',
    },
    'tests/packaging_test.py': {
        '*.md': 'it lists the repository root for documents',
        '*.rst': 'it lists the repository root for documents',
    },
}

#: Not the repository: what a walk of a copy that is not a checkout skips.
NOT_THE_TREE = {'.git', '__pycache__', '.venv', 'venv', '.uv-python',
                'build', 'dist', '.pytest_cache'}

#: Where a string with no space in it is split into the pieces that can
#: name a file, as in 'light_check_test.py::LightCheckTest::test_x'.
PIECES = re.compile(r'[:,;()\'"=]+')


def _redproof():
    spec = importlib.util.spec_from_file_location(
        'redproof', os.path.join(ROOT, 'tools', 'redproof.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def from_git(root=ROOT):
    '''The files git tracks, by repository path, or None when git cannot
    say: no checkout here, no git, or git refusing to read it.'''
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    try:
        out = subprocess.run(['git', 'ls-files', '-z'], cwd=root, env=env,
                             capture_output=True)
    except OSError:
        return None
    if out.returncode != 0 or not os.path.exists(os.path.join(root, '.git')):
        return None
    return sorted(p for p in out.stdout.decode('utf-8').split('\0') if p)


def tracked(root=ROOT):
    '''The files git tracks, by repository path; where git cannot say, as in
    a red-proof's copy, every file under it but bytecode and what
    NOT_THE_TREE names.'''
    files = from_git(root)
    if files is not None:
        return files
    found = []
    for top, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in NOT_THE_TREE
                         and not d.endswith('.egg-info'))
        rel = os.path.relpath(top, root).replace(os.sep, '/')
        found += [n if rel == '.' else rel + '/' + n for n in names
                  if not n.endswith('.pyc')]
    return sorted(found)


def module_path(name, files):
    '''The tracked file a dotted module name imports, or None.'''
    base = name.replace('.', '/')
    for path in (base + '.py', base + '/__init__.py'):
        if path in files:
            return path
    return None


def imports_of(path, source, files):
    '''The tracked files a module's imports load: each module named, the
    packages above it, and for `from x import y`, x.y when it is a module.'''
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    package = os.path.dirname(path).replace('/', '.')
    found = set()

    def add(name):
        parts = name.split('.')
        for i in range(1, len(parts) + 1):
            hit = module_path('.'.join(parts[:i]), files)
            if hit:
                found.add(hit)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                up = package.split('.') if package else []
                up = up[:len(up) - (node.level - 1)] if node.level > 1 else up
                base = '.'.join(up + ([node.module] if node.module else []))
            else:
                base = node.module or ''
            if base:
                add(base)
            for alias in node.names:
                add((base + '.' if base else '') + alias.name)
    found.discard(path)
    return found


def named_in(source, files):
    '''What a module's string constants name: tracked files, and
    directories as `dir/`.'''
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    # Prose names no file: a string with a space in it is taken whole,
    # never split into its words.
    words = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            pieces = [node.value]
            if not any(c.isspace() for c in node.value):
                pieces += PIECES.split(node.value)
            words |= {p.strip('/') for p in pieces if p.strip('/')}
    found = set()
    dirs = set()
    for f in files:
        d = os.path.dirname(f)
        while d:
            dirs.add(d)
            d = os.path.dirname(d)
        name = os.path.basename(f)
        stem = name[:-3] if name.endswith('.py') else None
        if f in words or name in words or (stem and stem in words):
            found.add(f)
    found |= {d + '/' for d in dirs
              if d in words or os.path.basename(d) in words}
    return found


def pytest_loads(path):
    '''setup.cfg, and the conftest.py and __init__.py files beside the test
    module and in every directory above it: those there now, and those that
    would be loaded if one were added.'''
    found = {'setup.cfg'}
    d = os.path.dirname(path)
    while d:
        found |= {d + '/conftest.py', d + '/__init__.py'}
        d = os.path.dirname(d)
    return found


def reads_of(path, read, files):
    '''The direct surface of the test module at `path`, `read` giving a
    file's bytes: itself, what pytest loads for it, the modules it imports,
    what it names (files, `dir/` directories) and DECLARED's patterns.'''
    files = list(files)
    loads = pytest_loads(path)
    found = {path} | loads
    for conftest in sorted(f for f in loads
                           if f.endswith('/conftest.py') and f in files):
        source = read(conftest)
        if source is not None:
            found |= imports_of(conftest, source, files)
    source = read(path)
    if source is not None:
        found |= imports_of(path, source, files) | named_in(source, files)
    found |= set(DECLARED.get(path, {}))
    return sorted(found)


def closure(entries, read, files):
    '''Map entries with the Python files among them followed through their
    imports, and a test helper's named files with them, to the end: the
    whole reach the direct surface leads to.'''
    files = list(files)
    found = set(entries)
    todo = sorted(f for f in found if f.endswith('.py') and f in files)
    seen = set(todo)
    while todo:
        current = todo.pop()
        source = read(current)
        if source is None:
            continue
        reached = imports_of(current, source, files)
        if current.startswith('tests/'):
            reached |= named_in(source, files)
        found |= reached
        for f in reached:
            if f.endswith('.py') and f not in seen:
                seen.add(f)
                todo.append(f)
    return found


def build(read, files):
    '''The map: each test module under tests/ to its direct surface.'''
    redproof = _redproof()
    patterns = redproof.test_patterns(read)
    modules = [f for f in files if f.startswith('tests/') and f.endswith('.py')
               and redproof.is_test_module(f, read, patterns)]
    return {m: reads_of(m, read, files) for m in modules}


def reached(reads, changed, patterns):
    '''The test modules whose reads include a changed path. A changed file
    named as a test module that the map does not have yet, a new one, is
    one of them too.'''
    redproof = _redproof()
    changed = set(changed)
    found = {m for m, entries in reads.items()
             if redproof.reads_any(entries, changed)}
    found |= {p for p in changed - set(reads)
              if p.startswith('tests/') and p.endswith('.py')
              and os.path.basename(p) != 'conftest.py'
              and any(fnmatch.fnmatch(os.path.basename(p), pattern)
                      for pattern in patterns)}
    return sorted(found)


def render(reads):
    return json.dumps(reads, indent=1, sort_keys=True) + '\n'


def read_tree(path):
    try:
        with open(os.path.join(ROOT, path), 'rb') as f:
            return f.read()
    except OSError:
        return None


def main(argv):
    if argv[:1] == ['--touching'] and len(argv) == 2:
        redproof = _redproof()
        reads = redproof.reads_map(read_tree)
        if reads is None:
            print('reads: %s is missing or not a map' % MAP)
            return 2
        try:
            changed = redproof.changed_since(argv[1], ROOT)
        except (OSError, subprocess.SubprocessError) as e:
            print('reads: --touching %s: %s' % (argv[1], e))
            return 2
        tests = [t for t in reached(reads, changed,
                                    redproof.test_patterns(read_tree))
                 if os.path.isfile(os.path.join(ROOT, t))]
        print('python -m pytest ' + ' '.join(tests) if tests else
              'reads: no test module reads a file the branch changes')
        return 0
    built = build(read_tree, tracked())
    text = render(built)
    target = os.path.join(ROOT, MAP)
    if argv[:1] == ['--check']:
        try:
            with open(target, encoding='utf-8') as f:
                current = json.load(f)
        except (OSError, ValueError):
            current = None
        if current == json.loads(text):
            return 0
        stale = sorted(m for m in set(built) | set(current or {})
                       if built.get(m) != (current or {}).get(m))
        print('reads: %s is out of date for %s; run python -I -B '
              'tools/reads.py' % (MAP, ', '.join(stale) or 'all of it'))
        return 1
    with open(target, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
