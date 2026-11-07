'''Which repository files each test module reads: the map that chooses the
tests a change reaches.

    python -I -B tools/reads.py            # rewrite tools/reads.json
    python -I -B tools/reads.py --check    # exit 1, naming them, if stale

A test module reads:

 * itself, setup.cfg, and the conftest.py and package __init__.py files
   pytest loads for it;
 * every repository module it imports, and what those import, followed
   to the end;
 * every repository file it, or a test helper it imports, names in a
   string: a tracked file's path, its name (README.md, preferences.json),
   a Python file's name without .py (load('redproof')), or a directory's
   name or path (docs), which stands for every tracked file under it.

It is read from the source, not measured by running the tests, so it can be
recomputed exactly and checked by a test. What that cannot see -- an import
or a file name built at run time from pieces -- goes in DECLARED. Naming a
file it does not read costs a test run; missing one it does is the failure
this exists to prevent, so it errs towards naming.

The light check names the test modules a commit's changes reach from this
map, and tools/redproof.py --touching chooses entries from it too.
'''

import ast
import fnmatch
import importlib.util
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP = 'tools/reads.json'

#: What a test module reads that its source cannot show: test module to
#: glob patterns over repository paths, each with its reason.
DECLARED = {
    'tests/light_check_test.py': {
        '*': 'its forbidden-word scan reads every tracked file',
    },
    'tests/reads_test.py': {
        '*': 'it rebuilds this map from every tracked file',
    },
}


def _redproof():
    spec = importlib.util.spec_from_file_location(
        'redproof', os.path.join(ROOT, 'tools', 'redproof.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tracked(root=ROOT):
    '''The files git tracks, by repository path; in a copy that is not a
    checkout, as a red-proof's is, every file under it but bytecode.'''
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    out = subprocess.run(['git', 'ls-files', '-z'], cwd=root, env=env,
                         capture_output=True)
    if out.returncode == 0 and os.path.exists(os.path.join(root, '.git')):
        return sorted(p for p in out.stdout.decode('utf-8').split('\0') if p)
    found = []
    for top, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in ('.git', '__pycache__'))
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
    '''The tracked files a module's string constants name.'''
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    words = {node.value.strip('/') for node in ast.walk(tree)
             if isinstance(node, ast.Constant) and isinstance(node.value, str)
             and node.value.strip('/')}
    found = set()
    dirs = {}
    for f in files:
        d = os.path.dirname(f)
        while d:
            dirs.setdefault(d, []).append(f)
            d = os.path.dirname(d)
    for f in files:
        name = os.path.basename(f)
        stem = name[:-3] if name.endswith('.py') else None
        if f in words or name in words or (stem and stem in words):
            found.add(f)
    for d, under in dirs.items():
        if d in words or os.path.basename(d) in words:
            found.update(under)
    return found


def pytest_loads(path, files):
    '''setup.cfg, and the conftest.py and __init__.py files beside the test
    module and in every directory above it.'''
    found = {'setup.cfg'} & set(files)
    d = os.path.dirname(path)
    while d:
        for name in ('conftest.py', '__init__.py'):
            if d + '/' + name in files:
                found.add(d + '/' + name)
        d = os.path.dirname(d)
    return found


def reads_of(path, read, files):
    '''Every tracked file the test module at `path` reads, `read` giving a
    file's bytes.'''
    files = list(files)
    seen = {path}
    todo = [path]
    found = {path} | pytest_loads(path, files)
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
    for pattern in DECLARED.get(path, {}):
        found |= {f for f in files if fnmatch.fnmatch(f, pattern)}
    return sorted(found)


def build(read, files):
    '''The map: each test module under tests/ to the files it reads.'''
    redproof = _redproof()
    patterns = redproof.test_patterns(read)
    modules = [f for f in files if f.startswith('tests/') and f.endswith('.py')
               and redproof.is_test_module(f, read, patterns)]
    return {m: reads_of(m, read, files) for m in modules}


def reached(reads, changed, patterns):
    '''The test modules whose reads include a changed path. A changed file
    named as a test module that the map does not have yet, a new one, is
    one of them too.'''
    changed = set(changed)
    found = {m for m, rs in reads.items() if changed & set(rs)}
    found |= {p for p in changed - set(reads)
              if p.startswith('tests/') and p.endswith('.py')
              and os.path.basename(p) != 'conftest.py'
              and any(fnmatch.fnmatch(os.path.basename(p), pattern)
                      for pattern in patterns)}
    return sorted(found)


def render(reads):
    return json.dumps(reads, indent=1, sort_keys=True) + '\n'


def main(argv):
    files = tracked()

    def read(path):
        try:
            with open(os.path.join(ROOT, path), 'rb') as f:
                return f.read()
        except OSError:
            return None
    text = render(build(read, files))
    target = os.path.join(ROOT, MAP)
    if argv[:1] == ['--check']:
        try:
            with open(target, encoding='utf-8') as f:
                current = f.read()
        except OSError:
            current = ''
        if current == text:
            return 0
        print('reads: %s is out of date; run python -I -B tools/reads.py'
              % MAP)
        return 1
    with open(target, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
