'''The suite's own configuration: how many workers `-n auto` starts, the
limit every test runs under, the Pythons CI runs, the actions the workflows
pin, and the publish workflow's check that main holds the release commit.'''

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_conftest():
    spec = importlib.util.spec_from_file_location(
        'conftest_under_test', os.path.join(ROOT, 'tests', 'conftest.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_os(environ=None, **calls):
    # Only what the hook may consult; a missing attribute is a Python
    # without it.
    return types.SimpleNamespace(environ=environ or {}, **calls)


class AutoWorkersTest(unittest.TestCase):

    def setUp(self):
        self.conftest = load_conftest()

    def workers(self, os_module):
        self.conftest.os = os_module
        return self.conftest.pytest_xdist_auto_num_workers(None)

    def test_auto_is_the_usable_core_count_not_the_machines(self):
        self.assertEqual(7, self.workers(fake_os(
            process_cpu_count=lambda: 7, sched_getaffinity=lambda pid: {0},
            cpu_count=lambda: 99)),
            '-n auto did not start one worker per usable core')

    def test_before_3_13_the_affinity_mask_decides(self):
        self.assertEqual(3, self.workers(fake_os(
            sched_getaffinity=lambda pid: {0, 1, 2}, cpu_count=lambda: 99)),
            '-n auto ignored the affinity mask')

    def test_a_jobs_core_cap_bounds_the_workers(self):
        # A run under a CPU quota may use fewer cores than
        # process_cpu_count() shows; SUITE_CORES says how many.
        for value, expected in (('4', 4), (' 4 ', 4),
                                # Above the usable count it does not raise it.
                                ('16', 7),
                                ('', 7), ('four', 7), ('4.5', 7), ('0', 7),
                                ('-2', 7)):
            with self.subTest(SUITE_CORES=value):
                self.assertEqual(expected, self.workers(fake_os(
                    environ={'SUITE_CORES': value},
                    process_cpu_count=lambda: 7)))

    def test_the_environment_override_still_wins(self):
        self.assertIsNone(self.workers(fake_os(
            environ={'PYTEST_XDIST_AUTO_NUM_WORKERS': '3'},
            process_cpu_count=lambda: 7)),
            'the hook answered over PYTEST_XDIST_AUTO_NUM_WORKERS')


# Installed but not loaded still fails: only a venv without [dev], which
# installs the plugin, skips, and never in CI, which sets CI=true.
@unittest.skipIf(importlib.util.find_spec('pytest_timeout') is None
                 and os.environ.get('CI') != 'true',
                 'pytest-timeout not installed in this venv; '
                 '[dev] installs it')
def test_every_test_runs_under_a_time_limit(pytestconfig):
    # Asked of the running pytest rather than read from setup.cfg: the limit
    # exists only if the plugin that reads it is loaded.
    assert pytestconfig.pluginmanager.hasplugin('timeout'), (
        'pytest-timeout is not loaded, so no test has a time limit')
    limit = float(pytestconfig.getini('timeout') or 0)
    # Under the login flow's five-minute callback timeout, so a test hung on
    # a lost port fails by name before that timeout says something vaguer.
    assert 0 < limit < 300, 'the per-test time limit is %r' % limit


class PythonPinsTest(unittest.TestCase):
    '''.python-versions pins one exact release of every supported Python, so
    every machine that runs the suite runs the same builds.'''

    def read(self, *path):
        with open(os.path.join(ROOT, *path), encoding='utf-8') as f:
            return f.read()

    def pins(self):
        return self.read('.python-versions').split()

    def supported(self):
        import re
        return sorted(set(re.findall(
            r"'Programming Language :: Python :: (3\.\d+)'",
            self.read('setup.py'))), key=lambda v: tuple(map(int, v.split('.'))))

    def test_every_supported_python_is_pinned_exactly_once(self):
        import re
        pins = self.pins()
        self.assertEqual([], [p for p in pins
                              if not re.fullmatch(r'\d+\.\d+\.\d+', p)],
                         'a pin is not an exact release')
        minors = [p.rsplit('.', 1)[0] for p in pins]
        self.assertEqual(self.supported(), minors,
                         'the pinned Pythons are not the supported ones')
        self.assertIn("python_requires='>=%s'" % minors[0],
                      self.read('setup.py'))

    def test_no_workflow_runs_on_a_schedule(self):
        # CI runs when code it tests changes, never on a timer.
        import re
        directory = os.path.join(ROOT, '.github', 'workflows')
        workflows = sorted(os.listdir(directory))
        self.assertIn('python.yml', workflows)
        for workflow in workflows:
            with self.subTest(workflow=workflow):
                text = self.read('.github', 'workflows', workflow)
                self.assertIsNone(re.search(r'^\s*schedule\s*:', text, re.M),
                                  '%s runs on a schedule' % workflow)
        # Nor does a bot open pull requests, each running CI, on one.
        dependabot = os.path.join(ROOT, '.github', 'dependabot.yml')
        if os.path.exists(dependabot):
            self.assertIsNone(
                    re.search(r'^\s*schedule\s*:',
                              self.read('.github', 'dependabot.yml'), re.M),
                    'dependabot.yml opens update pull requests on a schedule')

    def test_ci_tests_the_pinned_pythons(self):
        import re
        minors = [p.rsplit('.', 1)[0] for p in self.pins()]
        for workflow in ('python.yml', 'publish.yml', 'docs.yml'):
            text = self.read('.github', 'workflows', workflow)
            matrices = re.findall(r'python: \[([^\]]*)\]', text)
            self.assertTrue(matrices, workflow)
            for matrix in matrices:
                versions = re.findall(r'"([0-9.]+)"', matrix)
                self.assertLessEqual(set(versions), set(minors),
                                     '%s tests an unpinned Python' % workflow)
            # The docs build on one; the tests run on every one.
            if workflow != 'docs.yml':
                self.assertEqual(
                    minors, re.findall(r'"([0-9.]+)"', matrices[0]),
                    '%s does not test every pinned Python' % workflow)
            self.assertIsNone(
                re.search(r'uses:\s*["\']?actions/setup-python', text),
                '%s installs a Python the pin does not choose' % workflow)
            # Every uv setup takes the pinned build, and checks it got it.
            uv = text.count('uses: astral-sh/setup-uv@')
            self.assertEqual(
                uv, text.count('python-version: ${{ steps.pin.outputs.version }}'),
                '%s sets up a Python other than the pinned one' % workflow)
            self.assertEqual(uv, text.count('The interpreter is the pinned build'),
                             '%s does not check its interpreter' % workflow)


#: An action pinned to a commit, and the release the comment beside it names:
#: `uses: owner/name@<40 hex>  # v1.2.3`. A live `uses:` only, quoted or not;
#: one inside a comment is not a pin.
PINNED = re.compile(r'^[ \t]*(?:-[ \t]*)?uses:[ \t]*["\']?'
                    r'([\w.-]+/[\w./-]+)@([0-9a-fA-F]{40})\b([^\n]*)',
                    re.MULTILINE)
VERSION = re.compile(r'#\s*(v\d+(?:\.\d+)*)\b')


def pin_problems(workflows):
    '''What is wrong with the commit pins in `workflows`, a name-to-text
    mapping: a pin with no release named beside it, and an action pinned to
    more than one commit, or one commit named as more than one release.'''
    problems, seen = [], {}
    for name, text in sorted(workflows.items()):
        for n, line in enumerate(text.splitlines(), 1):
            m = PINNED.search(line)
            if not m:
                continue
            path, sha, rest = m.groups()
            # owner/name/sub is the same release as owner/name: its init and
            # analyze steps must move together.
            action = '/'.join(path.split('/')[:2])
            sha = sha.lower()
            version = VERSION.search(rest)
            if not version:
                problems.append('%s:%d: %s@%s names no release'
                                % (name, n, action, sha[:7]))
                continue
            seen.setdefault(action, set()).add((sha, version.group(1)))
    for action, pins in sorted(seen.items()):
        if len(pins) > 1:
            problems.append('%s is pinned more than one way: %s' % (
                action, ', '.join('%s as %s' % (sha[:7], v)
                                  for sha, v in sorted(pins))))
    return problems


class ActionPinsTest(unittest.TestCase):
    '''A workflow that pins an action to a commit names the release beside
    it, and every use of that action agrees on both, so a bump that reaches
    some uses and not others, or moves the commit and not its comment,
    fails here. Whether the commit is that release only GitHub can say, so
    this test does not check it.'''

    def workflows(self):
        root = os.path.join(ROOT, '.github', 'workflows')
        found = {}
        for name in sorted(os.listdir(root)):
            if name.endswith(('.yml', '.yaml')):
                with open(os.path.join(root, name), encoding='utf-8') as f:
                    found[name] = f.read()
        return found

    def test_every_pin_names_its_release_and_agrees(self):
        workflows = self.workflows()
        pins = sum(len(PINNED.findall(text)) for text in workflows.values())
        # Positive control: the workflows do pin actions to commits.
        self.assertGreater(pins, 3, 'no commit pins were found')
        self.assertEqual([], pin_problems(workflows))

    def test_a_pin_with_no_release_named_is_refused(self):
        sha = 'a' * 40
        self.assertEqual(
            ['w.yml:1: o/a@aaaaaaa names no release'],
            pin_problems({'w.yml': 'uses: o/a@%s\n' % sha}))

    def test_a_partial_bump_is_refused(self):
        old, new = 'a' * 40, 'b' * 40
        for shape, texts in (
                ('the commit moved, not its comment',
                 {'w.yml': 'uses: o/a@%s  # v1.0.0\n' % old,
                  'x.yml': 'uses: o/a@%s  # v1.0.0\n' % new}),
                ('one use left behind',
                 {'w.yml': 'uses: o/a@%s  # v1.0.0\n' % old,
                  'x.yml': 'uses: o/a@%s  # v1.0.1\n' % new}),
                ('the comment moved, not its commit',
                 {'w.yml': 'uses: o/a@%s  # v1.0.0\n' % old,
                  'x.yml': 'uses: o/a@%s  # v1.0.1\n' % old})):
            with self.subTest(shape=shape):
                problems = pin_problems(texts)
                self.assertEqual(1, len(problems), problems)
                self.assertIn('o/a is pinned more than one way', problems[0])

    def test_every_spelling_of_a_pin_is_read(self):
        a, b = 'a' * 40, 'b' * 40
        for shape, texts in (
                ('upper case', {'w.yml': 'uses: o/a@%s  # v1.0.0\n' % a,
                                'x.yml': 'uses: o/a@%s  # v1.0.1\n'
                                         % b.upper()}),
                ('quoted', {'w.yml': 'uses: o/a@%s  # v1.0.0\n' % a,
                            'x.yml': 'uses: "o/a@%s"  # v1.0.1\n' % b}),
                ('a subpath of one action',
                 {'w.yml': 'uses: o/a/init@%s  # v1.0.0\n' % a,
                  'x.yml': 'uses: o/a/analyze@%s  # v1.0.1\n' % b})):
            with self.subTest(shape=shape):
                problems = pin_problems(texts)
                self.assertEqual(1, len(problems), problems)
                self.assertIn('o/a is pinned more than one way', problems[0])

    def test_a_commented_out_pin_is_not_a_pin(self):
        self.assertEqual([], pin_problems({
            'w.yml': '# uses: o/a@%s  # v0.9\n'
                     '  - uses: o/a@%s  # v1.0.0\n' % ('a' * 40, 'b' * 40)}))

    def test_agreeing_pins_and_tag_pins_pass(self):
        sha = 'a' * 40
        self.assertEqual([], pin_problems({
            'w.yml': 'uses: o/a@%s  # v1.0.0: why\n'
                     'uses: actions/checkout@v7\n' % sha,
            'x.yml': '  - uses: o/a@%s   # v1.0.0\n' % sha}))


def publish_workflow():
    with open(os.path.join(ROOT, '.github', 'workflows', 'publish.yml'),
              encoding='utf-8') as f:
        return f.read()


def step_script(step):
    # A publish step's own run block, as the workflow runs it.
    lines = publish_workflow().splitlines()
    at = lines.index('      - name: %s' % step)
    while lines[at].strip() != 'run: |':
        at += 1
    body = []
    for line in lines[at + 1:]:
        if line.strip() and not line.startswith(' ' * 10):
            break
        body.append(line[10:])
    return '\n'.join(body) + '\n'


def run_step(script, cwd, env):
    # As GitHub runs a step with `shell: bash`.
    return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script],
                          cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=60)


class PublishBuildsTheTagTest(unittest.TestCase):
    '''A release and a retried publish both build the release's tag, and
    nothing else is published: a retry run from main once main has moved on
    would otherwise upload newer code under the tag's version.'''

    def blocks(self, start):
        # Each step starting with this line, as the lines under it.
        lines = publish_workflow().splitlines()
        found = []
        for at, line in enumerate(lines):
            if line == start:
                block = []
                for under in lines[at + 1:]:
                    if under.strip() and not under.startswith(' ' * 8):
                        break
                    block.append(under.strip())
                found.append(block)
        return found

    def test_every_checkout_builds_the_runs_own_commit(self):
        # No ref: checkout then takes the run's commit, and fails if a
        # release's tag has moved since the run started.
        checkouts = self.blocks('      - uses: actions/checkout@v7')
        self.assertEqual(publish_workflow().count('actions/checkout@'),
                         len(checkouts), 'a checkout this test cannot read')
        self.assertTrue(checkouts, 'publish.yml checks nothing out')
        for block in checkouts:
            self.assertEqual([], [line for line in block
                                  if line.startswith('ref:')],
                             'a checkout builds something other than the '
                             "run's commit")

    def test_the_version_check_runs_on_every_trigger(self):
        [step] = self.blocks('      - name: Version must match the tag')
        self.assertEqual([], [line for line in step if line.startswith('if:')],
                         'the version check is skipped on some trigger')

    @unittest.skipIf(os.name == 'nt', 'the step runs under bash on Linux')
    def test_only_a_published_release_tag_is_published(self):
        script = step_script("The run is a published release's tag")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_dir = os.path.join(tmp.name, 'bin')
        os.mkdir(bin_dir)
        log = os.path.join(tmp.name, 'gh.log')
        # Stands in for gh: answers isDraft, or that there is no release.
        with open(os.path.join(bin_dir, 'gh'), 'w') as f:
            f.write('#!/bin/sh\necho "$*" >> "$GH_LOG"\n'
                    'if [ "$FAKE_DRAFT" = missing ]; then\n'
                    '  echo "release not found" >&2; exit 1\nfi\n'
                    'echo "$FAKE_DRAFT"\n')
        os.chmod(os.path.join(bin_dir, 'gh'), 0o755)
        for ref, draft, says in (
                ('refs/tags/v5.3.0', 'false', None),
                ('refs/heads/main', 'false', 'is not a release tag'),
                ('refs/tags/x5.3.0', 'false', 'is not a release tag'),
                ('refs/tags/v5.3.0', 'true', 'has no published release'),
                ('refs/tags/v5.3.0', 'missing', 'has no published release')):
            with self.subTest(ref=ref, draft=draft):
                open(log, 'w').close()
                env = dict(os.environ, GITHUB_REF=ref,
                           GITHUB_REF_NAME=ref.rsplit('/', 1)[1],
                           GITHUB_REPOSITORY='owner/repo', FAKE_DRAFT=draft,
                           GH_LOG=log,
                           PATH=bin_dir + os.pathsep + os.environ['PATH'])
                run = run_step(script, tmp.name, env)
                self.assertEqual(says is not None, run.returncode != 0,
                                 '%s, isDraft %s: exited %d\n%s' % (
                                     ref, draft, run.returncode,
                                     run.stdout + run.stderr))
                if says:
                    self.assertIn(says, run.stdout)
                else:
                    # Positive control: it asked about this tag's release.
                    with open(log) as f:
                        self.assertEqual(
                            'release view v5.3.0 -R owner/repo --json '
                            'isDraft -q .isDraft\n', f.read(),
                            'it asked gh something other than whether the '
                            "tag's release is a draft")

    @unittest.skipIf(os.name == 'nt', 'the step runs under bash on Linux')
    def test_the_built_version_must_equal_the_tag(self):
        script = step_script('Version must match the tag')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        empty = os.path.join(tmp.name, 'empty')
        os.makedirs(os.path.join(empty, 'dist'))
        built = os.path.join(tmp.name, 'built')
        os.makedirs(os.path.join(built, 'dist'))
        open(os.path.join(built, 'dist', 'schwaby-5.3.0.tar.gz'), 'w').close()
        # A version that reads as empty: only a tag that is not empty
        # tells it apart from no tag at all.
        unnamed = os.path.join(tmp.name, 'unnamed')
        os.makedirs(os.path.join(unnamed, 'dist'))
        open(os.path.join(unnamed, 'dist', 'schwaby-.tar.gz'), 'w').close()
        for cwd, tag, refused in ((built, 'v5.3.0', False),
                                  (built, 'v5.2.0', True),
                                  (built, '', True),
                                  (empty, 'v5.3.0', True),
                                  (unnamed, '', True)):
            what = os.path.basename(cwd)
            with self.subTest(dist=what, tag=tag):
                run = run_step(script, cwd,
                               dict(os.environ, GITHUB_REF_NAME=tag))
                self.assertEqual(refused, run.returncode != 0,
                                 'tag %r, dist %s: exited %d\n%s' % (
                                     tag, what, run.returncode,
                                     run.stdout + run.stderr))


@unittest.skipIf(os.name == 'nt', 'the step runs under bash on Linux')
class ReleaseCommitOnMainTest(unittest.TestCase):

    def script(self):
        return step_script('The release commit is on main')

    def git(self, cwd, *args):
        return subprocess.run(['git', *args], cwd=cwd, env=self.env,
                              capture_output=True, text=True,
                              check=True).stdout.strip()

    def commit(self, cwd, name):
        with open(os.path.join(cwd, name), 'w') as f:
            f.write(name)
        self.git(cwd, 'add', name)
        self.git(cwd, 'commit', '-q', '-m', name)
        return self.git(cwd, 'rev-parse', 'HEAD')

    def test_a_release_commit_main_does_not_hold_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith('GIT_')}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        origin = os.path.join(tmp.name, 'origin')
        os.mkdir(origin)
        self.git(origin, 'init', '-q', '-b', 'main')
        self.git(origin, 'config', 'user.name', 'test')
        self.git(origin, 'config', 'user.email', 'test@example.invalid')
        tagged = self.commit(origin, 'tagged')
        # Main has moved on since, as it does between a tag and its release.
        self.commit(origin, 'later')
        work = os.path.join(tmp.name, 'work')
        self.git(tmp.name, 'clone', '-q', origin, work)
        self.git(work, 'config', 'user.name', 'test')
        self.git(work, 'config', 'user.email', 'test@example.invalid')
        unmerged = self.commit(work, 'unmerged')
        script = self.script()
        # The commit judged is the one checked out.
        for sha, code in ((tagged, 0), (unmerged, 1)):
            with self.subTest(on_main=not code):
                self.git(work, 'checkout', '-q', '--detach', sha)
                run = run_step(script, work, self.env)
                self.assertEqual(code, run.returncode,
                                 run.stdout + run.stderr)
                if code:
                    self.assertIn('%s is not on main' % sha, run.stdout)


class ProgressLineTest(unittest.TestCase):
    '''The suite prints `progress: DONE/TOTAL tests`, each on a line of
    its own, ending at the total, so a scheduler can show how far it is.'''

    def run_part(self, choice):
        env = dict(os.environ, SUITE_PROGRESS=choice)
        return subprocess.run(
                [sys.executable, '-I', '-m', 'pytest', '-q',
                 '-p', 'no:cacheprovider', '-p', 'no:xdist',
                 os.path.join('tests', 'reads_test.py')],
                cwd=ROOT, env=env, capture_output=True, text=True,
                timeout=120).stdout

    def test_the_progress_lines(self):
        out = self.run_part('1')
        lines = [line for line in out.splitlines() if 'progress:' in line]
        self.assertTrue(lines, out[-2000:])
        for line in lines:
            self.assertRegex(line, r'^progress: \d+/\d+ tests$')
        done, total = map(int, re.findall(r'\d+', lines[-1]))
        self.assertEqual(done, total)
        self.assertEqual('progress: 0/{} tests'.format(total), lines[0])
        # And none when turned off.
        self.assertNotIn('progress:', self.run_part('0'))
