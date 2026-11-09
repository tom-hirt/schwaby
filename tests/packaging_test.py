'''Tests for setup.py's metadata.

setup.py is not imported by anything else in the suite, so a syntax error or a
mistake in the dependency lists is invisible until someone runs a build or a
`pip install`.

These assertions are about relationships between the lists rather than their
contents, so adding a package does not require editing a test.
'''

import ast
import collections
import contextlib
import enum
import importlib
import inspect
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import docutils.core
import docutils.nodes
import setuptools

from .utils import no_duplicates


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def cls_own_body(func):
    """Every node inside `func` except those inside a nested function.

    `ast.walk` has no notion of scope, so a test whose local helper returns a
    value looks identical to a test that returns one itself.
    """
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
    stack, out = list(func.body), []
    while stack:
        node = stack.pop()
        # Skipping the node itself, not just its children: dropping nested
        # `def`s from the *stack* while still appending the one being looked
        # at, and walking its body, would report a local helper's return as
        # the test's own.
        if isinstance(node, nested):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def display_path(path):
    '''Path relative to the repository, or absolute if it is not under one.

    os.path.relpath raises on Windows when the two paths are on different
    drives, and the control fixtures in this file live in a temp directory --
    D: on a GitHub runner, where the checkout is C:. Linux and macOS have a
    single root, so without this the failure would show only on the platform
    the matrix runs least often: pushes are Linux-only, and the full matrix
    runs on pull requests and tags.
    '''
    try:
        return os.path.relpath(path, REPO_ROOT)
    except ValueError:
        return path
SETUP_PY = os.path.join(REPO_ROOT, 'setup.py')


@contextlib.contextmanager
def in_repo_root():
    '''setup.py opens README.md and schwaby/version.py by relative path, the
    way pip runs it. Every other test here is independent of the working
    directory and this one has to be too, so run it where it expects to be
    rather than from wherever pytest was invoked.'''
    previous = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(previous)


def setup_kwargs():
    '''Runs setup.py with setuptools.setup captured rather than executed.'''
    captured = {}

    with in_repo_root():
        with open(SETUP_PY) as f:
            source = f.read()

        with patch.object(setuptools, 'setup', captured.update):
            exec(compile(source, SETUP_PY, 'exec'),
                 {'__name__': '__main__', 'setuptools': setuptools})

    return captured


def package_name(requirement):
    '''"websockets>=14.0" -> "websockets". Applied to both sides of every
    comparison below: normalising only one side would make
    test_no_extra_is_in_install_requires pass for exactly the case it exists to
    catch, since a pinned duplicate in an extra would not match a bare name.'''
    for separator in ('>', '<', '=', '!', '~', '[', ';', ' '):
        requirement = requirement.split(separator)[0]
    return requirement.strip()


class SetupPyTest(unittest.TestCase):

    def setUp(self):
        self.kwargs = setup_kwargs()

    @no_duplicates
    def test_setup_py_parses_and_calls_setup(self):
        self.assertIn('name', self.kwargs)
        self.assertEqual('schwaby', self.kwargs['name'])

    @no_duplicates
    def test_the_distribution_and_the_package_have_the_same_name(self):
        # A package called `schwab` is the same directory `schwab-py` ships,
        # so the two could not be installed alongside each other -- whichever
        # went in second would overwrite the other's files. Asserted because
        # reverting to a `schwab` package would reintroduce the collision
        # rather than restore compatibility, and would do it quietly.
        # Not `schwaby.__name__`, which is 'schwaby' by construction the
        # moment the import above succeeds and would hold no matter what
        # setup.py said. The two sides that can actually disagree are the
        # distribution name and the shipped package list.
        self.assertEqual('schwaby', self.kwargs['name'])
        self.assertIn(self.kwargs['name'], self.kwargs['packages'])
        self.assertNotIn('schwab', self.kwargs['packages'])

    @no_duplicates
    def test_the_version_matches_the_package(self):
        from schwaby.version import version
        self.assertEqual(version, self.kwargs['version'])

    @no_duplicates
    def test_install_requires_is_the_declared_set(self):
        # Anything here lands on every machine running this library, so it
        # should be a deliberate decision rather than a merge artifact. The
        # login packages are hard dependencies because anything that
        # authenticates can reach the login flow: easy_client re-runs it once
        # a token passes max_token_age, so an extra holding them would be one
        # everyone has to install.
        #
        # cryptography is here because the callback server runs with
        # ssl_context='adhoc' and werkzeug builds that certificate with it.
        # Nothing in flask's or werkzeug's metadata says so -- it arrives via
        # authlib today, which is a coincidence, not a guarantee.
        names = sorted(package_name(r)
                       for r in self.kwargs['install_requires'])
        self.assertEqual(
                ['authlib', 'cryptography', 'flask', 'httpx2', 'multiprocess',
                 'psutil', 'websockets'], names)

    @no_duplicates
    def test_dev_is_the_only_extra(self):
        # No `login` or `codegen` shim: pip treats an unknown extra as a
        # warning rather than an error, so an old pin naming one still
        # installs.
        self.assertEqual(['dev'], sorted(self.kwargs['extras_require']))

    @no_duplicates
    def test_dev_has_the_plugins_setup_cfg_needs(self):
        # CONTRIBUTING runs the suite with `-n auto`, and setup.cfg sets
        # `timeout`. Without pytest-xdist the first is an unknown argument;
        # without pytest-timeout the second is a warning and no test has a
        # limit. The light check needs pyflakes, and its tests and the
        # time-limit test skip where these are not installed, so [dev] must
        # install them for the skips to hide nothing.
        dev = [package_name(r) for r in self.kwargs['extras_require']['dev']]
        for plugin in ('pytest-xdist', 'pytest-timeout', 'pyflakes'):
            self.assertIn(plugin, dev, '%s is missing from [dev]' % plugin)

    # The two checks below are vacuous against setup.py as it stands, because
    # `dev` is the only extra and both loops skip it: a subset test and a
    # disjointness test over nothing hold whatever the other side contains.
    # They would pass on a `dev` list emptied by a bad merge, which is the
    # exact failure the first one exists to catch.
    #
    # So each is a function tested twice -- once against setup.py, and once
    # against a fixture that violates it. The second is the positive control.
    # Without it the assertions look like protection and are not, which is
    # worse than not having them. They are kept rather than deleted because the
    # extras are still declared and an extra with contents is one edit away.

    @staticmethod
    def extras_missing_from_dev(extras):
        dev = set(package_name(r) for r in extras.get('dev', ()))
        return sorted(name for name, packages in extras.items()
                      if name != 'dev'
                      and not set(package_name(r) for r in packages) <= dev)

    @staticmethod
    def extras_duplicating_install_requires(install_requires, extras):
        hard = set(package_name(r) for r in install_requires)
        return sorted(name for name, packages in extras.items()
                      if name != 'dev'
                      and set(package_name(r) for r in packages) & hard)

    @no_duplicates
    def test_dev_covers_every_extra(self):
        # The suite really starts flask servers through multiprocess, so a
        # package in an extra but missing from dev is a green local run against
        # a stale virtualenv and a red CI.
        self.assertEqual(
                [], self.extras_missing_from_dev(self.kwargs['extras_require']))

    @no_duplicates
    def test_dev_coverage_check_catches_a_gap(self):
        self.assertEqual(
                ['login'],
                self.extras_missing_from_dev({'login': ['flask'],
                                              'codegen': [],
                                              'dev': ['pytest']}))

    @no_duplicates
    def test_no_extra_is_in_install_requires(self):
        # An extra which repeats a hard dependency is a package that can never
        # be absent, advertised as optional.
        self.assertEqual(
                [],
                self.extras_duplicating_install_requires(
                    self.kwargs['install_requires'],
                    self.kwargs['extras_require']))

    @no_duplicates
    def test_duplicate_check_catches_a_duplicate(self):
        self.assertEqual(
                ['login'],
                self.extras_duplicating_install_requires(
                    ['flask', 'authlib>=1.8'],
                    {'login': ['flask'], 'codegen': [], 'dev': ['flask']}))


class ShippedFilesTest(unittest.TestCase):
    """What ends up in the wheel.

    `find_packages()` with no arguments matches `tests` as well as the
    package, so a wheel built that way ships a top-level `tests` package into
    users' site-packages -- colliding file-for-file with anything else that
    ships one, and answering `import tests` from outside a project root.
    """

    EXPECTED_PACKAGES = ['schwaby', 'schwaby.client', 'schwaby.contrib',
                         'schwaby.orders']

    @no_duplicates
    def test_only_schwaby_packages_are_shipped(self):
        with in_repo_root():
            found = setuptools.find_packages(include=['schwaby', 'schwaby.*'])
        self.assertEqual(self.EXPECTED_PACKAGES, sorted(found))

    @no_duplicates
    def test_setup_py_ships_that_package_list(self):
        # The assertion above is about find_packages. This one is about what
        # setup.py actually passes, which is the thing that ends up in a wheel.
        self.assertEqual(self.EXPECTED_PACKAGES,
                         sorted(setup_kwargs()['packages']))


class LinkTest(unittest.TestCase):
    """Every URL a reader can follow, from shipped code and from the docs.

    Links rot in two ways.

    The host is wrong or dead. A link to `schwab-py.readthedocs.io` is the
    *original* project's docs, so a reader following it reads about a
    different codebase. Hosts from the TD Ameritrade era, such as
    `developer.schwabmeritrade.com`, and `beta-developer.schwab.com` do not
    resolve. A dead link to register an app breaks the first click in the
    whole onboarding flow.

    A denylist of hosts already known would be a record, not a check: it
    cannot catch the next one, which may be anywhere a reader's link can come
    from, `docs/` above all. So this is an allowlist, over every such file.

    Or the host survives and the target moves. Some of our own links are
    `github.com/.../blob/main/<path>` deep links, and some carry an anchor into
    a section title. Renaming the file or retitling the section leaves the host
    unchanged, so a host check stays green while the link 404s or lands
    mid-page. Those are resolved against the working tree instead.
    """

    ALLOWED_HOSTS = frozenset((
        'schwaby.readthedocs.io',           # our published documentation
        'github.com',                       # ours, plus httpx2's changelog
        'img.shields.io',                   # README badges
        'api.schwabapi.com',                # the API itself
        'developer.schwab.com',             # Schwab's own portal
        'www.schwab.com',
        'docs.python.org',
        'pypi.org',
        'pandas.pydata.org',
        'websockets.readthedocs.io',        # dependencies' own docs
        'requests-oauthlib.readthedocs.io',
        'virtualenv.pypa.io',
        'www.sphinx-doc.org',               # a comment in docs/conf.py
        'www.investopedia.com',             # order-type explanations
        'optionstradingiq.com',
        'www.cboe.com',
        'www.sec.gov',
        'www.businessinsider.com',
    ))

    # Hostnames that appear only inside example code, where they stand for a
    # value the reader replaces. They are not links and nobody clicks them.
    # RFC 2606 reserves example.com for documentation, and loopback is the
    # callback server rather than a link. A real registrable domain, such as
    # `callback.com`, does not belong here: standing in for one the reader
    # supplies, it is someone else's host to send a reader to.
    EXAMPLE_HOSTS = frozenset(('127.0.0.1', 'localhost'))
    EXAMPLE_SUFFIXES = ('.example.com', '.example.org', '.example.net')

    OURS = 'https://github.com/tom-hirt/schwaby/blob/main/'

    # `\s*` after the slashes is load-bearing. RST wraps long links, as in
    # `<https://\nvirtualenv.pypa.io/...>`, and a host anchored straight to
    # `//` never sees one.
    URL = re.compile(r'https?://\s*([^/\s\'"`)>,]+)')
    OUR_LINK = re.compile(
            r'https://github\.com/tom-hirt/schwaby/blob/main/'
            r'([A-Za-z0-9_./-]+?)(?:#([a-z0-9-]+))?(?=[\s\'"`)>,]|$)')

    # `'…schwaby/' + 'blob/main/…'` is one URL to a reader and two string
    # literals to a regex. Both ValueError links in this library are written
    # that way, so without joining, the deep-link check would silently cover
    # neither. Joining adjacent literals first can merge two unrelated strings
    # into something URL-shaped, but that direction fails loudly; the other one
    # is a guard that quietly does nothing.
    ADJACENT_LITERALS = re.compile(r'[\'"]\s*\+?\s*[\'"]')

    @classmethod
    def read_joined(cls, path):
        with open(path, encoding='utf-8') as f:
            contents = f.read()
        if path.endswith('.py'):
            contents = cls.ADJACENT_LITERALS.sub('', contents)
        return contents

    @staticmethod
    def linkable_files():
        """Everything a reader's link can come out of: shipped code and docs.

        The docs tree is included because it is where links are actually
        written. The roots are asserted to exist, so a removed one fails
        loudly rather than quietly reducing what is checked.
        """
        found = []
        for directory in ('schwaby', 'docs'):
            root = os.path.join(REPO_ROOT, directory)
            # os.walk on a path that does not exist yields nothing and raises
            # nothing, so a directory that is renamed or removed silently
            # shrinks what this check covers.
            assert os.path.isdir(root), 'walk root is gone: %s' % directory
            for dirpath, _, filenames in os.walk(root):
                if '__pycache__' in dirpath or '_build' in dirpath:
                    continue
                for name in filenames:
                    if name.endswith(('.py', '.rst')):
                        found.append(os.path.join(dirpath, name))
        for name in sorted(os.listdir(REPO_ROOT)):
            if name.endswith(('.rst', '.md')):
                found.append(os.path.join(REPO_ROOT, name))
        # setup.py carries the Documentation URL PyPI shows in its sidebar,
        # which is as reader-facing as anything in the docs.
        found.append(os.path.join(REPO_ROOT, 'setup.py'))
        return found

    @classmethod
    def disallowed_hosts_in(cls, files):
        """Returns (path, host) for every URL host outside the allowlist.

        `files` is a parameter rather than a lookup so the positive control can
        run this same collection code over a fixture. A control that
        re-implements the predicate against a string literal proves only that
        the literal matches; it says nothing about whether the walk feeding the
        real assertion found anything at all.
        """
        offenders = []
        for path in files:
            contents = cls.read_joined(path)
            for host in cls.URL.findall(contents):
                bare = host.split(':')[0]
                if (bare in cls.EXAMPLE_HOSTS
                        or bare == 'example.com'
                        or bare.endswith(cls.EXAMPLE_SUFFIXES)):
                    continue
                if host not in cls.ALLOWED_HOSTS:
                    offenders.append((display_path(path), host))
        return sorted(set(offenders))

    @staticmethod
    def rst_titles(path):
        """The section titles in an .rst file.

        A title is a line with a punctuation underline of at least its own
        length beneath it. Overlined titles fall out of this too: the title is
        still the line above its underline.
        """
        lines = open(path, encoding='utf-8').read().split('\n')
        titles = []
        for i, line in enumerate(lines[:-1]):
            title, under = line.strip(), lines[i + 1].strip()
            if (title and len(under) >= len(title) and len(under) > 2
                    and len(set(under)) == 1
                    and under[0] in '=-~+^"\'`#*_:.'):
                titles.append(title)
        return titles

    @classmethod
    def rst_anchors(cls, path):
        """GitHub's slugs: drop non-word characters, spaces become hyphens."""
        out = set()
        for title in cls.rst_titles(path):
            slug = re.sub(r'[^\w\s-]', '', title.lower())
            out.add(re.sub(r'[\s_]+', '-', slug).strip('-'))
        return out

    @classmethod
    def sphinx_anchors(cls, path):
        """Sphinx's slugs, which are NOT GitHub's.

        Sphinx replaces every non-alphanumeric run with a single hyphen;
        GitHub deletes the punctuation instead. So the heading
        "Browser Warnings About Invalid/Self-Signed Certificates" is
        ...invalid-self-signed... on Read the Docs and ...invalidself-signed...
        on GitHub. One slugifier for both renderers would report a live anchor
        as missing.
        """
        out = set()
        for title in cls.rst_titles(path):
            out.add(re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-'))
        # Explicit `.. _label:` targets become anchors too.
        for label in re.findall(r'^\.\. _([\w-]+):', 
                                open(path, encoding='utf-8').read(), re.M):
            out.add(label.replace('_', '-'))
        return out

    @classmethod
    def broken_own_links_in(cls, files):
        """Returns (path, url, why) for our own deep links that do not resolve.

        A host check cannot see these: rename docs/auth.rst or retitle a
        section and the host is still github.com.
        """
        broken = []
        for path in files:
            contents = cls.read_joined(path)
            for target, fragment in cls.OUR_LINK.findall(contents):
                where = display_path(path)
                absolute = os.path.join(REPO_ROOT, target)
                if not os.path.exists(absolute):
                    broken.append((where, target, 'no such file'))
                elif fragment and target.endswith('.rst'):
                    if fragment not in cls.rst_anchors(absolute):
                        broken.append(
                                (where, target + '#' + fragment,
                                 'no section with that anchor'))
        return sorted(set(broken))

    @no_duplicates
    def test_no_disallowed_url_hosts(self):
        files = self.linkable_files()

        # Positive control for the walk. assertEqual([], offenders) holds just
        # as well when linkable_files() found nothing -- a mistyped root, or a
        # future directory that is not named in it.
        self.assertGreater(len(files), 20)
        for expected in (os.path.join(REPO_ROOT, 'schwaby', 'auth.py'),
                         os.path.join(REPO_ROOT, 'docs', 'getting-started.rst'),
                         os.path.join(REPO_ROOT, 'README.md')):
            self.assertIn(expected, files)

        self.assertEqual([], self.disallowed_hosts_in(files))

    @no_duplicates
    def test_the_host_check_catches_a_foreign_host(self):
        # Through the same collection code, not a re-implementation of the
        # predicate. The disallowed hosts are another project's docs and two
        # that do not resolve.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'shipped.py')
            with open(path, 'w') as f:
                f.write('See https://schwab-py.readthedocs.io/en/latest/ and\n'
                        'https://developer.schwabmeritrade.com/orders and\n'
                        'https://github.com/tom-hirt/schwaby and\n'
                        'https://127.0.0.1:8182\n')

            wrapped = os.path.join(tmp, 'wrapped.rst')
            with open(wrapped, 'w') as f:
                # RST breaking a long link immediately after the slashes.
                f.write('`the console <https://\n'
                        'beta-developer.schwab.com/dashboard>`__\n')

            offenders = self.disallowed_hosts_in([path, wrapped])

        self.assertEqual(['beta-developer.schwab.com',
                          'developer.schwabmeritrade.com',
                          'schwab-py.readthedocs.io'],
                         sorted(host for _, host in offenders))

    @no_duplicates
    def test_our_own_deep_links_resolve(self):
        files = self.linkable_files()
        self.assertGreater(len(files), 20)

        # Positive control: these assertions are about a list that is empty
        # when everything is fine, so prove the links were actually found.
        # auth.py carries the ValueError's anchored link.
        found = collections.Counter()
        for path in files:
            for target, _ in self.OUR_LINK.findall(self.read_joined(path)):
                found[target] += 1
        self.assertGreater(sum(found.values()), 3)

        # Named rather than counted. order-templates is written as
        # concatenated string literals in orders/generic.py, which only the
        # joining above can see; a count alone would stay green. Links to Read
        # the Docs are covered by the check below instead.
        self.assertIn('docs/order-templates.rst', found)

        self.assertEqual([], self.broken_own_links_in(files))

    # Read the Docs links cannot be resolved against the working tree the way
    # a blob link can -- the page does not exist until Sphinx builds it. But
    # the anchor is derived from a section title in docs/*.rst, so retitling
    # that section is exactly the rot this catches, without a network call.
    RTD_LINK = re.compile(
            r'https://schwaby\.readthedocs\.io/en/stable/'
            r'([a-z0-9-]+)\.html(?:#([a-z0-9-]+))?')

    @classmethod
    def broken_rtd_anchors_in(cls, files):
        broken = []
        for path in files:
            contents = cls.read_joined(path)
            for page, anchor in cls.RTD_LINK.findall(contents):
                where = display_path(path)
                source = os.path.join(REPO_ROOT, 'docs', page + '.rst')
                if not os.path.exists(source):
                    broken.append((where, page, 'no docs/%s.rst' % page))
                elif anchor and anchor not in cls.sphinx_anchors(source):
                    broken.append((where, page + '#' + anchor,
                                   'no section with that anchor'))
        return sorted(set(broken))

    @no_duplicates
    def test_our_readthedocs_links_match_a_real_section(self):
        files = self.linkable_files()

        # Positive control: these assertions are about an empty list, so prove
        # the links were found. auth.py alone carries four.
        found = sum(len(self.RTD_LINK.findall(self.read_joined(p)))
                    for p in files)
        self.assertGreater(found, 3)

        self.assertEqual([], self.broken_rtd_anchors_in(files))

    @no_duplicates
    def test_the_rtd_check_catches_a_retitled_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'shipped.py')
            with open(path, 'w') as f:
                f.write('https://schwaby.readthedocs.io/en/stable/'
                        'auth.html#callback-url-requirements\n'
                        'https://schwaby.readthedocs.io/en/stable/'
                        'auth.html#no-such-section\n'
                        'https://schwaby.readthedocs.io/en/stable/gone.html\n')

            broken = self.broken_rtd_anchors_in([path])

        self.assertEqual(
                [('auth#no-such-section', 'no section with that anchor'),
                 ('gone', 'no docs/gone.rst')],
                [(t, why) for _, t, why in broken])

    @no_duplicates
    def test_the_link_check_catches_a_moved_target_and_a_retitled_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'shipped.py')
            with open(path, 'w') as f:
                f.write(self.OURS + 'docs/auth.rst#callback-url-requirements\n'
                        + self.OURS + 'docs/auth.rst#no-such-section\n'
                        + self.OURS + 'docs/deleted-file.rst\n')

            broken = self.broken_own_links_in([path])

        self.assertEqual(
                [('docs/auth.rst#no-such-section',
                  'no section with that anchor'),
                 ('docs/deleted-file.rst', 'no such file')],
                [(target, why) for _, target, why in broken])

    @no_duplicates
    def test_the_anchor_the_callback_error_points_at_exists(self):
        # Named on its own because it is the one a user reaches while already
        # in trouble: client_from_login_flow refused their callback URL.
        anchors = self.rst_anchors(os.path.join(REPO_ROOT, 'docs', 'auth.rst'))
        self.assertIn('callback-url-requirements', anchors)


class DocsConfigTest(unittest.TestCase):
    """`docs/conf.py`'s project name, which titles every rendered page.

    A wrong name here puts it on every page title, browser tab and sidebar
    while the text on the page says to install `schwaby`. Nothing else
    catches a name in a config file: the docs build does not care, and the
    link checks look at URLs.
    """

    @no_duplicates
    def test_sphinx_project_matches_the_distribution_name(self):
        namespace = {'__file__': os.path.join(REPO_ROOT, 'docs', 'conf.py')}
        with in_repo_root():
            with open(namespace['__file__']) as f:
                exec(compile(f.read(), 'conf.py', 'exec'), namespace)

        self.assertEqual(setup_kwargs()['name'], namespace['project'])

    @no_duplicates
    def test_the_author_credit_is_deliberately_not_checked(self):
        # `author` and `copyright` name Alex Golec, which is correct and
        # must not be swept along by the assertion above. Stated as a test so
        # that changing it is a deliberate act rather than a tidy-up.
        namespace = {'__file__': os.path.join(REPO_ROOT, 'docs', 'conf.py')}
        with in_repo_root():
            with open(namespace['__file__']) as f:
                exec(compile(f.read(), 'conf.py', 'exec'), namespace)

        self.assertIn('Alex Golec', namespace['author'])
        self.assertIn('Alex Golec', namespace['copyright'])

    @no_duplicates
    def test_an_unresolved_cross_reference_fails_the_build(self):
        # Without nitpicky an unresolved reference renders as plain text and
        # sphinx says nothing. CI builds with -W,
        # so this setting is what turns each into a failure; nothing else in
        # the suite reads it.
        namespace = {'__file__': os.path.join(REPO_ROOT, 'docs', 'conf.py')}
        with in_repo_root():
            with open(namespace['__file__']) as f:
                exec(compile(f.read(), 'conf.py', 'exec'), namespace)

        self.assertIs(True, namespace['nitpicky'])
        self.assertIn('api_index', namespace['extensions'])
        # Only the standard library may be excused, since there is no
        # intersphinx to resolve it; a name of ours in this list would be a
        # broken link let through.
        self.assertEqual([], self.not_standard_library(
                namespace['nitpick_ignore']))
        # A regex form would bypass the check above entirely.
        self.assertEqual([], namespace.get('nitpick_ignore_regex', []))

    @staticmethod
    def not_standard_library(ignored):
        # Enumerated by what is allowed rather than what was seen: an
        # unqualified `StreamClient` would pass a check for a `schwaby.`
        # prefix, and hide a broken link just the same.
        bad = []
        for kind, target in ignored:
            module, _, name = target.rpartition('.')
            try:
                ok = (module.split('.')[0] in sys.stdlib_module_names
                      and hasattr(importlib.import_module(module), name))
            except ImportError:
                ok = False
            if not ok:
                bad.append(target)
        return bad

    @no_duplicates
    def test_the_ignore_check_refuses_names_of_ours(self):
        # The shapes the check exists for, since the real list holds none.
        self.assertEqual(
                ['StreamClient', 'schwaby.client.Client', 'decimal.NoSuch'],
                self.not_standard_library([
                    ('py:class', 'StreamClient'),
                    ('py:class', 'schwaby.client.Client'),
                    ('py:class', 'decimal.NoSuch'),
                    ('py:class', 'decimal.Decimal')]))


class ScreenerVocabularyTest(unittest.TestCase):
    """The REST enum and the streaming prose describe the same vocabulary.

    `Client.Movers.Index` and the prefix list in the screener section of
    `docs/streaming.rst` are the same set of identifiers -- Schwab uses them
    for the REST `symbol_id` and as the first component of a streaming
    subscription key. Nothing else keeps them in step, and a drift between
    them is worse than a typo. `$SPX.X`, the spelling from the pre-Schwab API,
    is the example: over REST it is an HTTP 400, which is at least loud. On
    the stream Schwab replies `code: 0, "SUBS command succeeded"` and then
    sends nothing, forever -- measured 2026-09-08 -- so a reader following
    prose that said `$SPX.X` would get a success acknowledgement and silence,
    with no error anywhere to explain it.

    **The tokens are compared exactly, against the bullet list alone.** A
    test asking `value in section` passes with `$SPX.X` in the list, because
    `'$SPX' in '$SPX.X'` is true and because the section also contains a
    paragraph naming the wrong spelling on purpose. A substring test against
    prose that discusses its own counter-example cannot fail.
    """

    def prefixes(self):
        """The identifiers named in the screener section's bullet list."""
        with in_repo_root():
            with open('docs/streaming.rst', encoding='utf-8') as f:
                doc = f.read()

        start = doc.index('(PREFIX)_(SORTFIELD)_(FREQUENCY)')
        end = doc.index('and sortField is:', start)

        found = set()
        for line in doc[start:end].split('\n'):
            line = line.strip()
            if not line.startswith('*') or ':' not in line:
                continue
            for token in line.split(':', 1)[1].split(','):
                token = token.strip()
                if token:
                    found.add(token)
        return found

    @no_duplicates
    def test_the_bullet_list_is_exactly_the_enum(self):
        from schwaby.client import Client

        documented = self.prefixes()
        known = {m.value for m in Client.Movers.Index}

        self.assertEqual(
                set(), known - documented,
                'Movers.Index values missing from the screener bullet list')
        self.assertEqual(
                set(), documented - known,
                'the screener bullet list names prefixes that are not '
                'Movers.Index values -- `$SPX.X` was exactly this, and Schwab '
                'accepts it on the stream without ever delivering a frame')

    @no_duplicates
    def test_the_comparison_is_exact_not_substring(self):
        # The positive control. `$SPX.X`
        # contains `$SPX`, so a membership test against the raw text passes
        # with the defect present. Prove the comparison rejects it.
        documented = self.prefixes()
        self.assertIn('$SPX', documented)
        self.assertNotIn('$SPX.X', documented)


class SplitLinkTest(unittest.TestCase):
    """No hyperlink target is broken across a line.

    RST allows a link target to wrap, and sphinx renders it correctly by
    joining the lines. Everything that reads the source instead sees a
    truncated URL, and that is most things: `LinkTest` below, a grep, a
    reviewer skimming a diff.

    The half left behind is plausible enough to pass unnoticed ---
    `https://www.sec.gov/`, say, or
    `https://www.investopedia.com/articles/active-trading/032614/` --- so
    `LinkTest` reports the host fine and never sees the article.

    Catching it needs the raw text rather than the parsed document, because
    by the time sphinx has parsed it the defect is gone.
    """

    @no_duplicates
    def test_no_link_target_wraps_across_a_line(self):
        import pathlib
        import re

        with in_repo_root():
            offenders = []
            for path in sorted(pathlib.Path('docs').glob('*.rst')):
                text = path.read_text(encoding='utf-8')
                for m in re.finditer(r'<(https?://[^>]*)>', text, re.S):
                    if '\n' in m.group(1):
                        line = text[:m.start()].count('\n') + 1
                        offenders.append('%s:%d %r' % (
                                path, line, m.group(1)[:60]))

        self.assertEqual(
                [], offenders,
                'link targets split across a line -- sphinx joins them, but '
                'anything reading the source sees only the first half: %s'
                % offenders)


class DocReferenceTest(unittest.TestCase):
    """Every name the documentation points at, resolved against the code.

    A worked example calling a method that does not exist --
    `Client.search_instruments()`, say, for `get_instruments` -- hands anyone
    copying it an AttributeError, and nothing else notices, because
    `sphinx -W` does not resolve a `:meth:` target to a real attribute: an
    unresolvable one renders as plain text and the build succeeds.

    Two forms have to be handled, and handling only the first misses the
    second:

        :meth:`schwaby.client.Client.get_quote`
        :meth:`Client.get_quote() <schwaby.client.Client.get_quote>`

    The second puts the real target inside the angle brackets, so a pattern
    that reads the visible label sees `Client.get_quote()` -- which does not
    start with `schwaby`, gets skipped as somebody else's name, and the broken
    target behind it is never looked at.
    """

    ROLE = re.compile(r':(?:func|meth|class|attr|obj|data|exc):`([^`]+)`')
    AUTODOC = re.compile(
            r'\.\.\s+auto(?:function|class|method|attribute|data|exception)::'
            r'\s+([\w.:]+)')

    # Bare class names the docs use as shorthand, and where they live.
    SHORTHAND = {
        'Client': 'schwaby.client',
        'AsyncClient': 'schwaby.client',
        'StreamClient': 'schwaby.streaming',
        'OrderBuilder': 'schwaby.orders.generic',
    }

    @staticmethod
    def doc_files():
        found = [os.path.join(REPO_ROOT, 'docs', n)
                 for n in sorted(os.listdir(os.path.join(REPO_ROOT, 'docs')))
                 if n.endswith('.rst')]
        found.append(os.path.join(REPO_ROOT, 'README.md'))
        return found

    @classmethod
    def references_in(cls, files):
        """Returns {dotted name: {'file:line', ...}} for every code reference."""
        refs = {}
        for path in files:
            with open(path, encoding='utf-8') as f:
                contents = f.read()
            for pattern, take_target in ((cls.ROLE, True),
                                         (cls.AUTODOC, False)):
                for m in pattern.finditer(contents):
                    body = m.group(1)
                    if take_target:
                        inner = re.search(r'<([^>]+)>', body)
                        body = inner.group(1) if inner else body
                    name = body.strip().lstrip('~').replace('()', '')
                    name = name.replace('::', '.')
                    line = contents[:m.start()].count('\n') + 1
                    refs.setdefault(name, set()).add(
                            '%s:%d' % (os.path.basename(path), line))
        return refs

    @classmethod
    def unresolvable(cls, refs):
        broken = []
        for name, where in sorted(refs.items()):
            parts = name.split('.')
            if parts[0] in cls.SHORTHAND:
                obj = importlib.import_module(cls.SHORTHAND[parts[0]])
                rest = parts
            elif parts[0] == 'schwaby':
                obj, rest = None, None
                for i in range(len(parts), 0, -1):
                    try:
                        obj = importlib.import_module('.'.join(parts[:i]))
                    except ImportError:
                        continue
                    rest = parts[i:]
                    break
                if obj is None:
                    broken.append((name, sorted(where)))
                    continue
            else:
                # Not one of ours -- a python.org type, or prose in a role.
                continue
            for attr in rest:
                if not hasattr(obj, attr):
                    broken.append((name, sorted(where)))
                    break
                obj = getattr(obj, attr)
        return broken

    @no_duplicates
    def test_every_documented_name_resolves(self):
        files = self.doc_files()

        # Positive control for the collection, twice over: an empty file list
        # and a pattern that matches nothing both produce an empty broken list.
        self.assertGreater(len(files), 8)
        refs = self.references_in(files)
        self.assertGreater(len(refs), 150)
        self.assertIn('schwaby.client.Client.get_quote', refs)

        broken = self.unresolvable(refs)
        self.assertEqual([], broken)

    @no_duplicates
    def test_the_check_reads_the_target_not_the_label(self):
        # The visible label is not the target, and a checker reading the
        # label skips it as foreign.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'doc.rst')
            with open(path, 'w') as f:
                f.write(
                    'Use :meth:`Client.search_instruments() '
                    '<schwaby.client.Client.search_instruments>` for this.\n'
                    'And :meth:`schwaby.client.Client.get_quote` for that.\n'
                    '.. autoclass:: schwaby.streaming::StreamClient.NoSuchEnum\n')

            refs = self.references_in([path])
            broken = self.unresolvable(refs)

        self.assertEqual(
                ['schwaby.client.Client.search_instruments',
                 'schwaby.streaming.StreamClient.NoSuchEnum'],
                sorted(name for name, _ in broken))


class DocCoverageTest(unittest.TestCase):
    """Every public class and callable the package exports has an entry.

    The API index page is generated from the entries, so a name with none is
    missing from the index as well as from the reference, and nothing else
    says so.

    Three rules decide what counts, and each is a way a check like this goes
    wrong:

    * By identity, not by path. `Client` and `AsyncClient` share `get_quote`
      through `BaseClient`, and `StreamJsonDecoder` is documented at its
      `contrib.util` re-export, so an object is covered if it has an entry
      anywhere. A classmethod reached by `getattr` is a new bound method on
      every access, so the function is what gets compared; comparing the
      method would report `OptionSymbol.parse_symbol` as missing while its
      entry sits on the page.
    * Where it is defined, not wherever it is inherited, or every field enum
      would need its own `relabel_message`. An override of a documented method
      is covered by the method it overrides.
    * The walk starts from the exported modules, not the defining ones.
      `Client` is defined in `schwaby.client.synchronous` and exported as
      `schwaby.client.Client`; a walk keyed on `__module__` would skip both
      clients entirely.

    Class-level data, enum members and namespace classes that only hold other
    classes (`Client.Quote`) are not required. `INTERNAL` lists public-looking
    names that are deliberately undocumented, each with the reason.
    """

    MODULES = ('schwaby.auth', 'schwaby.client', 'schwaby.streaming',
               'schwaby.orders.generic', 'schwaby.orders.common',
               'schwaby.orders.equities', 'schwaby.orders.options',
               'schwaby.utils', 'schwaby.debug', 'schwaby.contrib.util',
               'schwaby.testing')

    # Every other module in the package, and why it is not walked. A module
    # in neither list fails the suite, so a new one has to be classified.
    NOT_WALKED = {
        'schwaby': 're-exports its submodules',
        'schwaby.client.asynchronous': 'exported through schwaby.client',
        'schwaby.client.base':
            'its methods are reached through schwaby.client.Client',
        'schwaby.client.synchronous': 'exported through schwaby.client',
        'schwaby.contrib': 'a package with no contents of its own',
        'schwaby.orders': 'a package with no contents of its own',
        'schwaby._redact': 'private: the redaction every logger carries',
        'schwaby._shared_token': 'private: the shared token file the auth '
                                 'functions build on',
        'schwaby.version': 'holds the version string',
    }

    INTERNAL = {
        'schwaby.auth.TokenMetadata':
            'wraps the token file; its age reaches callers as '
            'Client.token_age',
        'schwaby.auth.TokenMetadata.from_loaded_token':
            'rebuilds the metadata from a token already loaded',
        'schwaby.auth.TokenMetadata.token_age':
            'reaches callers as Client.token_age',
        'schwaby.auth.TokenMetadata.wrap_token_in_metadata':
            'writes the creation timestamp beside the token',
        'schwaby.auth.TokenMetadata.wrapped_token_write_func':
            'the write callback the OAuth session is handed',
        'schwaby.auth.get_logger': 'module logger accessor',
        'schwaby.client.base.get_logger': 'module logger accessor',
        'schwaby.client.base.BaseClient':
            'the shared base; its methods are documented on Client',
        'schwaby.contrib.util.get_logger': 'module logger accessor',
        'schwaby.debug.get_logger': 'module logger accessor',
        'schwaby.streaming.get_logger': 'module logger accessor',
        'schwaby.utils.get_logger': 'module logger accessor',
        'schwaby.debug.LogRedactor':
            'redaction behind enable_bug_report_logging',
        'schwaby.debug.LogRedactor.redact':
            'strips registered secrets from the bug-report log',
        'schwaby.debug.LogRedactor.register':
            'records a secret for redact; the client and responses call it',
        'schwaby.streaming._BaseFieldEnum.key_mapping':
            'builds the table relabeling uses',
        # The handlers call it; nobody else needs to. The account-activity
        # enum overrides it, and that override shows on the enum's own page
        # through :undoc-members: -- it is documented, not exempt.
        'schwaby.streaming._BaseFieldEnum.relabel_message':
            'what the handlers call to relabel',
        'schwaby.debug.register_redactions':
            'redaction behind enable_bug_report_logging',
        'schwaby.debug.register_redactions_from_response':
            'redaction behind enable_bug_report_logging',
        'schwaby.orders.common.EquityInstrument':
            'built by add_equity_leg; not constructed by callers',
        'schwaby.orders.common.OptionInstrument':
            'built by add_option_leg; not constructed by callers',
        'schwaby.streaming.StreamClient.req_num':
            'numbers the debug log lines for sends and receives',
        # Listed member by member rather than as a class, because
        # set_enforce_enums is also defined there and is not internal:
        # exempting the class would hide it, while the docs tell readers to
        # call it.
        'schwaby.utils.EnumEnforcer':
            'base class of the enum checking',
        'schwaby.utils.EnumEnforcer.convert_enum':
            'the enum checking itself',
        'schwaby.utils.EnumEnforcer.convert_enum_iterable':
            'the enum checking itself',
        'schwaby.utils.EnumEnforcer.type_error':
            'the enum checking itself',
        'schwaby.utils.LazyLog': 'defers formatting a log argument',
        'schwaby.utils.class_fullname': 'formats a type name for messages',
    }

    DIRECTIVE = re.compile(
            r'^(?P<indent>[ \t]*)\.\.\s+(?:auto|py:)(?P<kind>function|class|'
            r'method|attribute|data|exception)::\s+(?P<target>[\w.:]+)[^\n]*\n'
            r'(?P<options>(?:[ \t]+:[\w-]+:[^\n]*\n)*)', re.M)

    @staticmethod
    def resolve(dotted):
        parts = dotted.replace('::', '.').split('.')
        for i in range(len(parts), 0, -1):
            try:
                obj = importlib.import_module('.'.join(parts[:i]))
            except ImportError:
                continue
            try:
                for part in parts[i:]:
                    obj = getattr(obj, part)
            except AttributeError:
                return None
            return obj.__func__ if inspect.ismethod(obj) else obj
        return None

    @staticmethod
    def own_members(cls):
        for name, value in vars(cls).items():
            if name.startswith('_'):
                continue
            if isinstance(value, (staticmethod, classmethod)):
                value = value.__func__
            yield name, value

    @staticmethod
    def is_ours(obj):
        # With the separator: `startswith('schwaby')` alone also matches a
        # sibling package called schwabytools.
        home = getattr(obj, '__module__', '') or ''
        return home == 'schwaby' or home.startswith('schwaby.')

    @staticmethod
    def defined_as(obj):
        return '{}.{}'.format(getattr(obj, '__module__', ''),
                              getattr(obj, '__qualname__', ''))

    @classmethod
    def documented_ids(cls, texts):
        ids = set()
        for text in texts:
            enclosing = None
            for m in cls.DIRECTIVE.finditer(text):
                target, kind = m.group('target'), m.group('kind')
                if not m.group('indent') and kind in ('class', 'exception'):
                    enclosing = target
                # `.. automethod:: set_account_hash` indented under an
                # autoclass is relative to it.
                if m.group('indent') and enclosing and not re.search(
                        r'[.:]', target):
                    target = enclosing.replace('::', '.') + '.' + target
                obj = cls.resolve(target)
                if obj is None:
                    continue
                ids.add(id(obj))
                options = m.group('options')
                if inspect.isclass(obj) and ':members:' in options:
                    every = ':undoc-members:' in options
                    for _, value in cls.own_members(obj):
                        if every or getattr(value, '__doc__', None):
                            ids.add(id(value))
        return ids

    @classmethod
    def public_api(cls):
        """(dotted path, object) for every class and callable required."""
        required, seen = [], set()

        def visit_class(path, klass):
            if id(klass) in seen or not cls.is_ours(klass):
                return
            seen.add(id(klass))
            members = [v for _, v in cls.own_members(klass)
                       if not isinstance(v, enum.Enum)]
            namespace = members and all(inspect.isclass(v) for v in members)
            if not namespace:
                required.append((path, klass))
            # Private bases are walked too: a public method defined on one is
            # public on every class that inherits it. Skipping them would
            # hide all_fields on every field enum.
            for base in klass.__mro__:
                if not cls.is_ours(base):
                    continue
                for name, value in cls.own_members(base):
                    if isinstance(value, enum.Enum):
                        continue
                    if isinstance(value, property):
                        value = value.fget
                    if inspect.isclass(value):
                        visit_class(path + '.' + name, value)
                    elif inspect.isfunction(value) and id(value) not in seen:
                        seen.add(id(value))
                        required.append((path + '.' + name, value))

        # Where an object is defined names it better than a package that
        # re-exports it from a submodule, which names it better than a
        # module that merely imported it.
        best = {}
        for modname in cls.MODULES:
            for name, obj in vars(importlib.import_module(modname)).items():
                if name.startswith('_') or not cls.is_ours(obj):
                    continue
                if not (inspect.isclass(obj) or inspect.isfunction(obj)):
                    continue
                home = obj.__module__
                score = (2 if home == modname else
                         1 if home.startswith(modname + '.') else 0)
                if id(obj) not in best or score > best[id(obj)][0]:
                    best[id(obj)] = (score, modname + '.' + name, obj)
        # Shorter first, so a member shared through a base is reported under
        # Client rather than AsyncClient.
        for _, path, obj in sorted(best.values(),
                                   key=lambda t: (len(t[1]), t[1])):
            if inspect.isclass(obj):
                visit_class(path, obj)
            elif id(obj) not in seen:
                seen.add(id(obj))
                required.append((path, obj))
        return required

    @classmethod
    def missing(cls, texts):
        ids = cls.documented_ids(texts)

        def overrides_documented(path, obj):
            owner = cls.resolve(path.rsplit('.', 1)[0])
            name = path.rsplit('.', 1)[1]
            return (inspect.isclass(owner) and inspect.isfunction(obj)
                    and any(id(vars(b).get(name)) in ids
                            for b in owner.__mro__[1:] if name in vars(b)))

        def internal(path, obj):
            # Exactly, by the path it was reached through or by where it is
            # defined -- never by prefix. A prefix would let an internal
            # class take its public members with it, and which member got
            # hidden would depend on the order of the walk.
            return path in cls.INTERNAL or cls.defined_as(obj) in cls.INTERNAL

        return sorted(p for p, o in cls.public_api()
                      if id(o) not in ids and not internal(p, o)
                      and not overrides_documented(p, o))

    @staticmethod
    def doc_texts():
        docs = os.path.join(REPO_ROOT, 'docs')
        texts = []
        for name in sorted(os.listdir(docs)):
            if name.endswith('.rst'):
                with open(os.path.join(docs, name), encoding='utf-8') as f:
                    texts.append(f.read())
        return texts

    @no_duplicates
    def test_every_public_name_has_a_documentation_entry(self):
        texts = self.doc_texts()
        required = dict(self.public_api())

        # Positive control: the walk has to reach the names this test was
        # written for, or an empty result means nothing.
        self.assertGreater(len(required), 250)
        for name in ('schwaby.client.Client.get_quote',
                     'schwaby.streaming.StreamClient.handle_message',
                     'schwaby.orders.generic.OrderBuilder.build',
                     'schwaby.orders.options.OptionSymbol.parse_symbol',
                     'schwaby.streaming.UnexpectedResponseCode'):
            self.assertIn(name, required)

        self.assertEqual([], self.missing(texts))

    @no_duplicates
    def test_a_deleted_entry_is_reported(self):
        # The other half of the control: removing real entries, including a
        # classmethod and an exception, must surface exactly those names.
        cut = ('.. automethod:: schwaby.streaming.StreamClient'
               '.handle_message\n',
               '.. automethod:: schwaby.orders.options.OptionSymbol'
               '.parse_symbol\n',
               '.. autoexception:: schwaby.streaming.UnexpectedResponseCode\n',
               # Defined on an internal base class, which must not take it
               # along: exempting that class would hide it while the docs
               # say to call it.
               '.. automethod:: schwaby.client.Client.set_enforce_enums',
               # Defined on a private base, _BaseFieldEnum, and public on
               # every field enum; skipping private bases would hide it.
               '.. automethod:: schwaby.streaming::StreamClient'
               '.LevelOneEquityFields.all_fields')
        texts = self.doc_texts()
        for line in cut:
            self.assertTrue(any(line in t for t in texts), line)
        mutated = []
        for text in texts:
            for line in cut:
                text = text.replace(line, '')
            mutated.append(text)

        missing = self.missing(mutated)
        # set_enforce_enums is reached through several classes that inherit
        # it, and which one names it depends on the walk; the method is what
        # matters, not the route to it.
        inherited = [p for p in missing
                     if p.endswith(('.set_enforce_enums', '.all_fields'))]
        self.assertEqual(2, len(inherited), missing)
        self.assertEqual(
                ['schwaby.orders.options.OptionSymbol.parse_symbol',
                 'schwaby.streaming.StreamClient.handle_message',
                 'schwaby.streaming.UnexpectedResponseCode'],
                [p for p in missing if p not in inherited])

    @no_duplicates
    def test_listing_a_class_does_not_exempt_its_members(self):
        # The shape the exact rule exists for, since no documented public
        # member of an internal class exists today: with OptionSymbol listed,
        # its undocumented parse_symbol must still be reported.
        cut = ('.. automethod:: schwaby.orders.options.OptionSymbol'
               '.parse_symbol\n')
        texts = [t.replace(cut, '') for t in self.doc_texts()]
        with patch.dict(self.INTERNAL,
                        {'schwaby.orders.options.OptionSymbol': 'test'}):
            missing = self.missing(texts)
        self.assertIn('schwaby.orders.options.OptionSymbol.parse_symbol',
                      missing)

    @no_duplicates
    def test_a_sibling_package_is_not_ours(self):
        sibling = type('Tool', (), {'__module__': 'schwabytools.x'})
        own = type('Tool', (), {'__module__': 'schwaby.x'})
        self.assertFalse(self.is_ours(sibling))
        self.assertTrue(self.is_ours(own))

    @no_duplicates
    def test_every_module_is_walked_or_says_why_not(self):
        import pkgutil
        import schwaby
        found = {'schwaby'} | {m.name for m in pkgutil.walk_packages(
                schwaby.__path__, 'schwaby.')}
        self.assertEqual(sorted(found),
                         sorted(set(self.MODULES) | set(self.NOT_WALKED)))
        # And the unwalked ones really do define nothing of their own, or
        # "exported through" is a claim nothing checks.
        for modname in self.NOT_WALKED:
            # A private module is internal throughout, so what it defines is
            # not public surface to account for.
            if modname.rsplit('.', 1)[-1].startswith('_'):
                continue
            mod = importlib.import_module(modname)
            own = [name for name, obj in vars(mod).items()
                   if not name.startswith('_')
                   and (inspect.isclass(obj) or inspect.isfunction(obj))
                   and obj.__module__ == modname]
            exported = modname.rsplit('.', 1)[0]
            if exported in self.MODULES:
                reached = vars(importlib.import_module(exported)).values()
                own = [n for n in own if getattr(mod, n) not in reached]
            own = [n for n in own
                   if '{}.{}'.format(modname, n) not in self.INTERNAL]
            self.assertEqual([], own, modname)

    @no_duplicates
    def test_the_internal_list_is_current(self):
        # An allowlist entry that has since been documented, or no longer
        # exists, is a blind spot waiting for the next name to fall into it.
        ids = self.documented_ids(self.doc_texts())
        for name in self.INTERNAL:
            obj = self.resolve(name)
            self.assertIsNotNone(obj, '{} no longer exists'.format(name))
            self.assertNotIn(id(obj), ids,
                             '{} is documented now; drop it from INTERNAL'
                             .format(name))


class ApiIndexExtensionTest(unittest.TestCase):
    """The `api-index` directive, built for real on a small project.

    It generates a page nobody writes, so nothing else would notice it
    listing the wrong thing: enum members one by one (the streaming field
    enums would bury everything else), a method outside its class, a module
    heading missing, or a link to an anchor on the wrong page. Every check
    reads the index's own entries -- the internal links wrapping a literal --
    because autodoc's signature lines on the same page contain the same
    names, and a check against the whole page would pass with the index
    broken.
    """

    @staticmethod
    def build(pages):
        # In a subprocess, not in-process. Sphinx registers its roles into
        # docutils' global registry, and after that the plain-docutils parse
        # DocBoldLiteralTest runs fails with "'Values' object has no
        # attribute 'env'" -- only when this test happens to run first.
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, 'src')
            os.mkdir(src)
            with open(os.path.join(src, 'conf.py'), 'w') as f:
                f.write(
                    'import sys\n'
                    'sys.path.insert(0, {!r})\n'
                    'sys.path.insert(0, {!r})\n'
                    "extensions = ['sphinx.ext.autodoc', 'api_index']\n"
                    .format(REPO_ROOT,
                            os.path.join(REPO_ROOT, 'docs', '_ext')))
            for name, rst in pages.items():
                with open(os.path.join(src, name + '.rst'), 'w') as f:
                    f.write(rst)
            out = os.path.join(tmp, 'out')
            result = subprocess.run(
                    [sys.executable, '-m', 'sphinx', '-q', '-b', 'html',
                     src, out], capture_output=True, text=True)
            if result.returncode != 0:
                raise AssertionError('sphinx failed: ' + result.stderr[-2000:])
            with open(os.path.join(out, 'index.html'), encoding='utf-8') as f:
                return f.read()

    PAGES = {
        'index': (
            'Page\n====\n\n'
            '.. toctree::\n\n   other\n\n'
            '.. autoclass:: schwaby.client.Client\n\n'
            '.. automethod:: schwaby.client.Client.get_quote\n\n'
            '.. autoclass:: schwaby.client.Client.Quote.Fields\n'
            '  :members:\n  :undoc-members:\n\n'
            '.. autoclass:: schwaby.client.Client.Account.Fields\n'
            '  :members:\n  :undoc-members:\n\n'
            '.. api-index::\n'),
        'other': (
            'Other\n=====\n\n'
            '.. automethod:: schwaby.client.Client.get_quotes\n'),
    }

    @staticmethod
    def index_region(page):
        """The index's own list, which begins at its first module heading."""
        start = page.index('<li><p><code class="docutils literal notranslate">'
                           '<span class="pre">schwaby.client</span>')
        return page[start:]

    @staticmethod
    def entries(region):
        return {re.sub(r'<[^>]+>', '', m.group(2)): m.group(1)
                for m in re.finditer(
                    r'<a class="reference internal" href="([^"]+)">'
                    r'(<code.*?</code>)</a>', region)}

    @no_duplicates
    def test_the_index_lists_classes_callables_and_enum_counts(self):
        page = self.build(self.PAGES)
        region = self.index_region(page)
        entries = self.entries(region)
        text = re.sub(r'<[^>]+>', '', region)

        # Positive control: the method is listed, as a callable, linked to
        # its own anchor on this page.
        self.assertEqual('#schwaby.client.Client.get_quote',
                         entries.get('Client.get_quote()'))
        # One on another page links there, not here.
        self.assertEqual('other.html#schwaby.client.Client.get_quotes',
                         entries.get('Client.get_quotes()'))
        # The enum is listed once, with its count, and none of its members;
        # a count of one is singular.
        self.assertIn('Client.Quote.Fields', entries)
        self.assertIn('Client.Quote.Fields (5 members)', text)
        self.assertIn('Client.Account.Fields (1 member)', text)
        self.assertEqual([], [e for e in entries if '.QUOTE' in e])

    @no_duplicates
    def test_an_incremental_build_updates_the_index(self):
        # The index depends on every page, which Sphinx does not know, so
        # without a re-read it keeps the list from the first build. The index
        # sits on a page of its own, as docs/api.rst does: a page holding the
        # toctree is rewritten whenever a child changes anyway, so putting it
        # there tests nothing.
        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, 'src'), os.path.join(tmp, 'out')
            os.mkdir(src)
            with open(os.path.join(src, 'conf.py'), 'w') as f:
                f.write('import sys\nsys.path.insert(0, {!r})\n'
                        'sys.path.insert(0, {!r})\n'
                        "extensions = ['sphinx.ext.autodoc', 'api_index']\n"
                        .format(REPO_ROOT,
                                os.path.join(REPO_ROOT, 'docs', '_ext')))
            pages = {'index': 'I\n=\n\n.. toctree::\n\n   api\n   other\n',
                     'api': 'A\n=\n\n.. api-index::\n',
                     'other': 'O\n=\n\n.. automethod:: '
                              'schwaby.client.Client.get_quote\n'}

            written = {}

            def build():
                # Only pages whose text changed are written. Rewriting all of
                # them would touch the index page too, so Sphinx would re-read
                # it anyway and the test would pass with the extension's
                # forced re-read removed. A changed page is
                # dated forward, so this does not rest on the clock ticking
                # between the two builds.
                for name, rst in pages.items():
                    if written.get(name) == rst:
                        continue
                    path = os.path.join(src, name + '.rst')
                    with open(path, 'w') as f:
                        f.write(rst)
                    if name in written:
                        later = os.path.getmtime(path) + 10
                        os.utime(path, (later, later))
                    written[name] = rst
                done = subprocess.run(
                        [sys.executable, '-m', 'sphinx', '-q', '-b', 'html',
                         src, out], capture_output=True, text=True)
                self.assertEqual(0, done.returncode, done.stderr[-2000:])
                with open(os.path.join(out, 'api.html'),
                          encoding='utf-8') as f:
                    return f.read()

            first = build()
            # Positive control: the first build lists what is there.
            self.assertIn('Client.get_quote()', first)
            self.assertNotIn('Client.get_quotes()', first)
            pages['other'] += ('\n.. automethod:: '
                               'schwaby.client.Client.get_quotes\n')
            self.assertIn('Client.get_quotes()', build())

    @no_duplicates
    def test_members_are_nested_under_their_class(self):
        region = self.index_region(self.build(self.PAGES))
        # The list directly under the Client entry.
        m = re.search(r'<span class="pre">Client</span></code></a></p>\s*'
                      r'<ul[^>]*>(.*?)</ul>', region, re.S)
        self.assertIsNotNone(m, 'Client has no nested list')
        nested = self.entries(m.group(1))
        self.assertIn('Client.get_quote()', nested)
        # Client.Quote is not documented, so its Fields hangs off Client.
        self.assertIn('Client.Quote.Fields', nested)


class DocBoldLiteralTest(unittest.TestCase):
    """An inline literal inside bold text renders as raw double backticks.

    reStructuredText does not nest inline markup, so a literal written inside
    a bold span is not a literal: the page shows its backticks. Sphinx builds
    it without a warning. The house style closes the bold around the literal
    instead.

    Each source is parsed with docutils rather than matched with a regular
    expression. A `**kwargs` in a code block, or a bold span broken across
    lines, pairs the asterisks wrongly for a pattern.

    It reads docstrings and the pages, and not the `#:` autodoc comments on
    enum members, which render onto those same pages: associating a comment
    block with its member is parsing this does not do. Those are checked by
    reading them. Extending the walk is worth doing on its own, with a case
    that nests a literal inside one and proves the walk goes red.
    """

    @staticmethod
    def literals_in_bold(text):
        # Reports are silenced because plain docutils does not know the roles
        # and directives sphinx adds. Bold text is still parsed around them.
        doctree = docutils.core.publish_doctree(
                text, settings_overrides={
                    'report_level': 5, 'halt_level': 5,
                    'warning_stream': False,
                    'file_insertion_enabled': False})
        return [node.astext()
                for node in doctree.findall(docutils.nodes.strong)
                if '``' in node.astext()]

    @no_duplicates
    def test_the_check_sees_a_literal_inside_bold(self):
        self.assertEqual(['Use ``x`` here'],
                         self.literals_in_bold('**Use ``x`` here** now.'))
        self.assertEqual(['Use ``x``\nhere'],
                         self.literals_in_bold('**Use ``x``\nhere** now.'))
        self.assertEqual([],
                         self.literals_in_bold('**Use** ``x`` **here** now.'))

    @no_duplicates
    def test_no_documentation_page_puts_a_literal_inside_bold(self):
        pages = [p for p in DocReferenceTest.doc_files() if p.endswith('.rst')]
        pages.append(os.path.join(REPO_ROOT, 'CONTRIBUTING.rst'))
        self.assertGreaterEqual(len(pages), 11)
        # By name: the count alone holds without CONTRIBUTING, so dropping it
        # from the scan would change nothing the test could see.
        self.assertIn(os.path.join(REPO_ROOT, 'CONTRIBUTING.rst'), pages,
                      'CONTRIBUTING.rst is not among the pages checked')

        found = []
        for path in pages:
            with open(path, encoding='utf-8') as f:
                for hit in self.literals_in_bold(f.read()):
                    found.append('%s: %s' % (
                        os.path.relpath(path, REPO_ROOT), hit))
        self.assertEqual([], found)

    @no_duplicates
    def test_no_docstring_puts_a_literal_inside_bold(self):
        # Autodoc renders these onto the same pages.
        kinds = (ast.Module, ast.ClassDef, ast.FunctionDef,
                 ast.AsyncFunctionDef)
        found = []
        read = 0
        for dirpath, _, names in os.walk(os.path.join(REPO_ROOT, 'schwaby')):
            for name in sorted(names):
                if not name.endswith('.py'):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding='utf-8') as f:
                    tree = ast.parse(f.read())
                for node in ast.walk(tree):
                    doc = (ast.get_docstring(node)
                           if isinstance(node, kinds) else None)
                    if not doc:
                        continue
                    read += 1
                    for hit in self.literals_in_bold(doc):
                        found.append('%s:%d: %s' % (
                            os.path.relpath(path, REPO_ROOT),
                            getattr(node, 'lineno', 1), hit))
        self.assertGreaterEqual(read, 300)
        self.assertEqual([], found)


class DocErrorHandlerRecipeTest(unittest.TestCase):
    """The error-handler recipe on the streaming page, run as written.

    ``service`` is whatever Schwab sent, and a handler is the last place a
    failure can be seen. With ``%r`` a value nested deeply enough raises
    inside the handler, which the client logs and otherwise swallows, so the
    alert the recipe exists to send would be lost.
    """

    @staticmethod
    def recipe():
        path = os.path.join(REPO_ROOT, 'docs', 'streaming.rst')
        return [code for _, _, code in DocExampleTest.code_blocks_in([path])
                if 'def on_stream_error(service, exception, message)' in code
                and 'async def' not in code]

    @no_duplicates
    def test_the_recipe_escapes_and_survives_deep_nesting(self):
        recipes = self.recipe()
        self.assertEqual(1, len(recipes))

        alerts = []
        namespace = {'alert': alerts.append, 'stream_client': MagicMock()}
        exec(compile(recipes[0], 'streaming.rst', 'exec'), namespace)
        on_stream_error = namespace['on_stream_error']

        deep = []
        for _ in range(100000):
            deep = [deep]
        on_stream_error('A\nFORGED', ValueError('x'), None)
        on_stream_error(deep, ValueError('deep'), None)

        self.assertEqual(2, len(alerts))
        self.assertFalse(any('\n' in a for a in alerts))


class DocRefreshRecipeTest(unittest.TestCase):
    """The refresh-failure recipe on the auth page, run as written.

    A retryable refusal keeps retrying at any age, because Schwab has not held
    exactly to its seven days. Past them the recipe alerts as well, and a
    terminal refusal alerts without retrying.
    """

    @staticmethod
    def run_recipe(error):
        path = os.path.join(REPO_ROOT, 'docs', 'auth.rst')
        recipes = [code for _, _, code in DocExampleTest.code_blocks_in([path])
                   if 'except TokenRefreshError as e:' in code
                   and 'retry_later()' in code]
        assert len(recipes) == 1, len(recipes)

        alerts, retries = [], []
        client = MagicMock()
        client.get_quote.side_effect = error
        namespace = {'c': client, 'alert': alerts.append,
                     'retry_later': lambda: retries.append(True)}
        exec(compile(recipes[0], 'auth.rst', 'exec'), namespace)
        return alerts, retries

    @no_duplicates
    def test_the_recipe_alerts_past_seven_days_and_keeps_retrying(self):
        from schwaby.utils import TokenRefreshError

        alerts, retries = self.run_recipe(TokenRefreshError(
                'x', token_age=8 * 86400, refresh_token_invalid=False))
        self.assertEqual(1, len(alerts))
        self.assertEqual(1, len(retries))

        alerts, retries = self.run_recipe(TokenRefreshError(
                'x', token_age=86400, refresh_token_invalid=False))
        self.assertEqual(0, len(alerts))
        self.assertEqual(1, len(retries))

        alerts, retries = self.run_recipe(TokenRefreshError(
                'x', token_age=86400, refresh_token_invalid=True))
        self.assertEqual(1, len(alerts))
        self.assertEqual(0, len(retries))

class DocExampleTest(unittest.TestCase):
    """Keyword arguments in documentation examples, against real signatures.

    An example passing a keyword the callable does not take --
    `easy_client(..., webdriver_func=make_webdriver)`, say, a parameter from
    the TD Ameritrade era, when the login flow drove a real browser -- gives a
    reader following it a TypeError before they reach the part they came for.

    `DocReferenceTest` cannot see this: the reference role `:func:`easy_client``
    resolves perfectly well. What is wrong is the call written underneath it.
    This walks the python code blocks instead and checks that every keyword
    passed to a `schwaby` callable is one that callable accepts.

    Deliberately narrow. It resolves a call only when the name is one the
    documentation imported from `schwaby`, and it skips a callable that takes
    `**kwargs`. It is not a type checker; it catches an argument the callable
    does not accept, which is how an example rots when a signature changes.
    """

    CODE_BLOCK = re.compile(r'^([ \t]*)\.\.\s+code-block::\s*python\s*$')
    DIRECTIVE_OPTION = re.compile(r'^[ \t]+:[a-zA-Z-]+:.*$')

    @staticmethod
    def dedent(block):
        lines = block.split('\n')
        indents = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
        if not indents:
            return ''
        margin = min(indents)
        return '\n'.join(l[margin:] if l.strip() else '' for l in lines)

    @classmethod
    def code_blocks_in(cls, files):
        blocks = []
        for path in files:
            with open(path, encoding='utf-8') as f:
                contents = f.read()
            if path.endswith('.py'):
                # A file under examples/ is one big block. It is checked the
                # same way as a doc snippet because it rots the same way.
                blocks.append((display_path(path), 1, contents))
                continue
            extract = (cls.fenced_blocks if path.endswith('.md')
                       else cls.python_blocks)
            blocks.extend((os.path.basename(path), line, code)
                          for line, code in extract(contents))
        return blocks

    FENCE = re.compile(r'^```python\s*\n(.*?)^```', re.S | re.M)

    @classmethod
    def fenced_blocks(cls, contents):
        """Yields (line, source) for every ```python fence in a markdown file.

        `doc_files()` includes README.md, and an extractor that understood
        the reStructuredText directive only would leave every block in the
        file PyPI renders as the project's front page silently contributing
        nothing to any of these checks.
        """
        for m in cls.FENCE.finditer(contents):
            yield contents[:m.start()].count('\n') + 1, m.group(1)

    @classmethod
    def python_blocks(cls, contents):
        """Yields (line, source) for every ``code-block:: python`` in an rst file.

        A directive's body is every line indented further than the directive
        itself, blank lines included; it ends at the first non-blank line that
        is not. Written that way rather than as one regex running to the first
        *unindented* line, which would let a block nested inside an admonition
        swallow the paragraph after it -- which dedents to a shallower margin
        than the code and makes the whole thing an `unexpected indent`
        SyntaxError, which `bad_keywords_in` then skips, silently.
        """
        lines = contents.split('\n')
        i = 0
        while i < len(lines):
            m = cls.CODE_BLOCK.match(lines[i])
            if not m:
                i += 1
                continue
            base, j, body = len(m.group(1)), i + 1, []
            # A directive may carry options -- `:caption:`,
            # `:emphasize-lines:` -- on the lines right after it, indented
            # like the body. They are not Python, and appending them makes the
            # block a SyntaxError, so an ordinary docs edit would fail the
            # parse check. Nothing here uses one today, which is why it needs
            # handling now rather than the first time somebody adds one.
            while j < len(lines) and cls.DIRECTIVE_OPTION.match(lines[j]):
                j += 1
            while j < len(lines):
                stripped = lines[j].strip()
                if not stripped:
                    body.append('')
                elif len(lines[j]) - len(lines[j].lstrip()) > base:
                    body.append(lines[j])
                else:
                    break
                j += 1
            yield i + 1, cls.dedent('\n'.join(body))
            i = j

    @staticmethod
    def example_files():
        root = os.path.join(REPO_ROOT, 'examples')
        assert os.path.isdir(root), 'examples/ is gone'
        found = []
        for dirpath, _, filenames in os.walk(root):
            if '__pycache__' in dirpath:
                continue
            found.extend(os.path.join(dirpath, n)
                         for n in filenames if n.endswith('.py'))
        return found

    @staticmethod
    def resolve_callable(name, imported):
        """Resolve a called name to a schwaby callable, or None if not ours."""
        root = name.split('.')[0]
        if root not in imported:
            return None
        obj = imported[root]
        for attr in name.split('.')[1:]:
            obj = getattr(obj, attr, None)
            if obj is None:
                return None
        return obj if callable(obj) else None

    @classmethod
    def bad_keywords_in(cls, blocks):
        """Returns (where, call, keyword) for each keyword the callee rejects."""
        problems = []
        for where, line, code in blocks:
            try:
                tree = ast.parse(code)
            except SyntaxError:
                # Fragments and shell-ish snippets are not our business.
                continue

            imported = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and \
                        (node.module or '').startswith('schwaby'):
                    for alias in node.names:
                        try:
                            mod = importlib.import_module(node.module)
                        except ImportError:                # pragma: no cover
                            continue
                        target = getattr(mod, alias.name, None)
                        if target is not None:
                            imported[alias.asname or alias.name] = target
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith('schwaby'):
                            try:
                                imported[alias.asname or alias.name] = \
                                        importlib.import_module(alias.name)
                            except ImportError:            # pragma: no cover
                                pass

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                try:
                    name = ast.unparse(node.func)
                except Exception:                          # pragma: no cover
                    continue
                target = cls.resolve_callable(name, imported)
                if target is None:
                    continue
                try:
                    sig = inspect.signature(target)
                except (ValueError, TypeError):             # pragma: no cover
                    continue
                if any(p.kind is inspect.Parameter.VAR_KEYWORD
                       for p in sig.parameters.values()):
                    continue
                accepted = set(sig.parameters)
                for kw in node.keywords:
                    if kw.arg is not None and kw.arg not in accepted:
                        problems.append(
                                ('%s:%d' % (where, line), name, kw.arg))
        return sorted(set(problems))

    # The one block that names a module this project does not depend on, and
    # names it deliberately: `docs/client.rst` shows `import httpx` as the
    # mistake to avoid, because authlib resolves `httpx2` and the two share no
    # exception hierarchy. Keyed by the module, not by file and line, so
    # editing around it does not need this list updated.
    IMPORTS_NOT_INSTALLED = {'httpx': 'client.rst names it as the wrong module'}

    #: Order templates are called positionally in the documentation, and
    #: `bad_keywords_in` only inspects keyword arguments -- so a string
    #: quantity passed positionally, `equity_buy_market('AAPL', '10')`, is
    #: invisible to every other check here.
    ORDER_TEMPLATE_MODULES = ('schwaby.orders.equities', 'schwaby.orders.options')

    @classmethod
    def order_templates(cls):
        templates = {}
        for name in cls.ORDER_TEMPLATE_MODULES:
            module = importlib.import_module(name)
            for attr in dir(module):
                obj = getattr(module, attr)
                if callable(obj) and not attr.startswith('_') \
                        and getattr(obj, '__module__', None) == name:
                    templates[attr] = obj
        return templates

    @no_duplicates
    def test_every_documented_order_template_call_still_builds(self):
        """Documented template calls, executed rather than inspected.

        Building an order touches nothing outside the process, so the calls
        can simply be run -- and running them is the only way to catch a
        positional argument of the wrong type. `equity_buy_market('AAPL',
        '10')` raises, and no other check here would stop a documentation
        example doing that.

        No release number here on purpose: prose naming a version goes stale
        silently, and the release may land under a different number than the
        one written.

        Only calls whose arguments are all literals are run. A call written
        with names or an ellipsis is prose about the shape rather than
        something a reader can copy, and inventing values for it would test
        the invention.
        """
        templates = self.order_templates()
        self.assertGreater(len(templates), 5, 'no templates found to check')

        blocks = self.code_blocks_in(
                DocReferenceTest.doc_files() + self.example_files())

        checked, failures = [], []
        for where, line, code in blocks:
            try:
                tree = ast.parse(code)
            except SyntaxError:                            # pragma: no cover
                continue
            source_lines = code.split('\n')
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, 'id', None)
                if name not in templates or node.keywords:
                    continue
                try:
                    args = [ast.literal_eval(a) for a in node.args]
                except ValueError:
                    continue                 # a name, not something to run

                # `equity_buy_limit(...)` is prose about the shape rather
                # than a call, and `...` is a literal, so literal_eval is
                # happy with it and the filter above is not enough.
                if any(a is Ellipsis for a in args):
                    continue

                # A block may deliberately show what does *not* work.
                # Marked in the documentation the reader can see, rather
                # than by an allowlist of file and line here, which would
                # go stale on the next edit above it.
                #
                # Every line the call spans is searched, not just the one it
                # starts on: the marker naturally goes on the line carrying
                # the offending argument, which for a wrapped call is not the
                # first one.
                span = source_lines[node.lineno - 1:node.end_lineno]
                marked = any('raises' in l.partition('#')[2] for l in span)

                where_line = '%s:%d %s' % (where, line, name)
                checked.append(where_line)

                # Marked calls are run too, and required to raise. Skipping
                # them instead would let the documentation claim something
                # raises after it had stopped raising -- and would quietly
                # drop a working call whose comment happens to contain the
                # word for another reason.
                try:
                    templates[name](*args).build()
                except Exception as e:
                    if not marked:
                        failures.append('%s -> %s: %s'
                                        % (where_line, type(e).__name__, e))
                else:
                    if marked:
                        failures.append(
                                '%s is marked `# raises` and did not'
                                % where_line)

        self.assertEqual([], failures)

        # Positive control. The assertion above is satisfied by finding
        # nothing at all, which is precisely what a broken extractor produces.
        self.assertGreater(len(checked), 4,
                           'expected to have executed several documented '
                           'template calls, found {}'.format(checked))

    @no_duplicates
    def test_the_template_call_check_would_catch_a_string_quantity(self):
        # The control for the control: prove the thing being executed is
        # actually rejected, so the test above is not green because building
        # never fails.
        templates = self.order_templates()
        with self.assertRaises(ValueError):
            templates['equity_buy_market']('AAPL', '10').build()
        templates['equity_buy_market']('AAPL', 10).build()

    @no_duplicates
    def test_no_sentence_is_left_dangling_by_a_removed_date(self):
        """`As of` is only ever followed by a date, so it must not end a line.

        Removing a date from prose can leave its `As of` behind -- "for every
        asset type. As of\n Schwab accepts only" -- and a check that looks for
        dates cannot see what removing one leaves.

        Deliberately narrow. Lower-case `since` and `on` end wrapped lines all
        over this tree and are fine; `As of` is not, because nothing but a date
        follows it.
        """
        offenders = []
        roots = ['schwaby', 'docs', 'README.md', 'CHANGELOG.md', 'RELEASING.md']
        files = []
        for r in roots:
            path = os.path.join(REPO_ROOT, r)
            if os.path.isfile(path):
                files.append(path)
                continue
            for dp, _, fns in os.walk(path):
                if '__pycache__' in dp:
                    continue
                files += [os.path.join(dp, f) for f in fns
                          if f.endswith(('.py', '.rst', '.md'))]
        for path in files:
            with open(path, encoding='utf-8') as f:
                for n, line in enumerate(f, 1):
                    if re.search(r'\bAs of\s*$', line.rstrip('\n')):
                        offenders.append('%s:%d: %s'
                                         % (display_path(path), n, line.strip()))
        self.assertEqual([], offenders)

    @no_duplicates
    def test_no_collected_test_returns_a_value(self):
        """A helper named `test_*` is collected, called, and passes.

        It tests nothing, reports green, and raises the count. The
        `filterwarnings` rule in `setup.cfg` turns unittest's warning about it
        into a failure, but that warning only exists from CPython 3.11, and CI
        runs 3.10 as well: on that leg the trap is still silent.

        So this asks the question structurally instead, which no interpreter
        version can opt out of. Read from source, since a decorated test does
        not always expose a usable `__code__` at runtime.
        """
        offenders, seen = [], []
        for path in self.python_files_under_tests():
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                # Module-level `def test_*` too: pytest collects those from any
                # `*test*.py`, and the `filterwarnings` rule in setup.cfg keys
                # on unittest's message, which pytest's own
                # PytestReturnNotNoneWarning does not match. Without this the
                # trap closed for methods would stay open one scope up.
                if isinstance(node, ast.Module):
                    owner, body = '<module>', node.body
                elif isinstance(node, ast.ClassDef):
                    owner, body = node.name, node.body
                else:
                    continue
                for item in body:
                    if not isinstance(item, (ast.FunctionDef,
                                             ast.AsyncFunctionDef)):
                        continue
                    if not item.name.startswith('test'):
                        continue
                    seen.append(item.name)
                    # Only this function's own body. `ast.walk` descends
                    # into nested `def`s, and a test defining a local helper
                    # that returns something -- a `side_effect`, typically --
                    # is not what this is about.
                    # `test_running_on_collab_environment` is exactly that
                    # shape.
                    if any(isinstance(inner, ast.Return)
                           and inner.value is not None
                           for inner in cls_own_body(item)):
                        offenders.append(
                                '%s: %s.%s returns a value'
                                % (display_path(path), owner, item.name))
        self.assertEqual([], offenders)

        # A walk that found nothing satisfies the assertion above exactly as
        # well as a clean tree does, and this guard exists for silent test
        # loss -- so a silently empty scan is the one outcome that must not
        # read as green. Without this, pointing the root at a missing
        # directory leaves both guards passing.
        self.assertGreater(len(seen), 500)

    @no_duplicates
    def test_no_test_module_defines_a_class_name_twice(self):
        """A shadowed test class takes its tests with it, silently.

        When a module defines two classes of the same name, the second
        shadows the first, so the first's tests never run and nothing reports
        it -- including a test that would fail if it ran. `no_duplicates`
        cannot catch this: it keys on `__qualname__`, and duplicate classes
        have the same one.

        Read from the source rather than from the imported module, because by
        import time the first definition is already gone.
        """
        offenders, classes = [], []
        for path in self.python_files_under_tests():
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read())
            seen = collections.Counter(
                    n.name for n in tree.body if isinstance(n, ast.ClassDef))
            classes.extend(seen)
            offenders.extend(
                    '%s: %s defined %d times' % (display_path(path), name, n)
                    for name, n in sorted(seen.items()) if n > 1)
        self.assertEqual([], offenders)

        # Same reason as the positive control in the test above: an empty walk
        # passes this assertion, and this guard is here because a shadowed
        # class disappears quietly.
        self.assertGreater(len(classes), 20)

    @no_duplicates
    def test_no_login_flow_test_names_a_fixed_port(self):
        """A login-flow test's callback URL takes its port from a socket it
        holds, or from free_callback_port(), never a number written down.

        A fixed port makes any two runs of the suite on one machine fail each
        other, and makes login-flow tests on different xdist workers collide.
        Measured: on a fixed port every one of six paired runs lost a
        login-flow test, against none of ten on free ports; and with the
        login-flow tests spread across workers, five runs of six workers on a
        fixed port each failed four to six of them, against none of twenty on
        free ports. A fixed port would fail most runs but not all, so it could
        pass CI by chance; this looks for one in the source instead.
        """
        import re
        fixed = re.compile(r'^https?://(127\.0\.0\.1|localhost):\d')
        loopback = re.compile(r'^https?://(127\.0\.0\.1|localhost):')


        offenders, seen = [], set()
        for path in self.python_files_under_tests():
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read())
            for cls in self.classes_running_the_flow(tree):
                for node in ast.walk(cls):
                    if not (isinstance(node, ast.Constant)
                            and isinstance(node.value, str)):
                        continue
                    if loopback.match(node.value):
                        seen.add(cls.name)
                    if fixed.match(node.value):
                        offenders.append('%s: %s line %d names a fixed port: '
                                         '%r' % (display_path(path), cls.name,
                                                 node.lineno, node.value))
        self.assertEqual([], offenders, 'a login-flow test names a fixed '
                         'callback port')
        # The positive control: the scan saw each class's loopback URLs, so
        # its silence about fixed ports means something.
        self.assertLessEqual(
                {'ClientFromLoginFlowTest', 'ParentSideImportTest',
                 'ConnectErrorSiblingsAreAbsorbedTest'}, seen)

    @staticmethod
    def classes_running_the_flow(tree):
        '''The test classes in a module that start the login flow, calling
        it by its own name or by any name it is imported as. An alias is a
        bare name, so it matches only a bare call: one that is also a method
        name must not pull in a class calling that method.'''
        aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                aliases.update(alias.asname for alias in node.names
                               if alias.name == 'client_from_login_flow')

        def calls_the_flow(n):
            if not isinstance(n, ast.Call):
                return False
            if isinstance(n.func, ast.Attribute):
                return n.func.attr == 'client_from_login_flow'
            return (isinstance(n.func, ast.Name) and n.func.id in
                    aliases | {'client_from_login_flow'})
        return [cls for cls in ast.walk(tree)
                if isinstance(cls, ast.ClassDef)
                and any(calls_the_flow(n) for n in ast.walk(cls))]

    @no_duplicates
    def test_a_class_calling_the_flow_by_an_alias_runs_it(self):
        # A class whose only call is through an alias. tests/auth_test.py
        # imports the flow as one, and a class calling it only that way must
        # still be found.
        source = (
            'from schwaby.auth import client_from_login_flow as _flow\n'
            'from schwaby.auth import client_from_login_flow as run\n'
            'from schwaby.auth import client_from_login_flow\n'
            'from schwaby.auth import client_from_token_file as _other\n'
            'class Aliased:\n'
            '    def test(self):\n'
            '        _flow(1, 2, 3, 4)\n'
            'class Other:\n'
            '    def test(self):\n'
            '        _other(1, 2, 3)\n'
            'class ByAttribute:\n'
            '    def test(self):\n'
            '        auth.client_from_login_flow(1, 2, 3, 4)\n'
            'class Subscripted:\n'
            '    def test(self):\n'
            '        handlers[0]()\n'
            'class CallsAMethodNamedLikeAnAlias:\n'
            '    def test(self):\n'
            '        thread.run()\n')
        found = {cls.name for cls in
                 self.classes_running_the_flow(ast.parse(source))}
        self.assertEqual({'Aliased', 'ByAttribute'}, found)

    @staticmethod
    def python_files_under_tests():
        # Not `test_files`. pytest collects anything named `test_*`, so a
        # helper spelled that way is picked up, called, and passes -- a test
        # that tests nothing, inflating the count and reporting green.
        root = os.path.join(REPO_ROOT, 'tests')
        found = []
        for dirpath, _, filenames in os.walk(root):
            if '__pycache__' in dirpath:
                continue
            found.extend(os.path.join(dirpath, n)
                         for n in filenames if n.endswith('.py'))
        return found

    @no_duplicates
    def test_every_documentation_code_block_parses(self):
        """No block may be skipped for being unparseable.

        `bad_keywords_in` swallows a SyntaxError and moves on, which is right
        for a genuine fragment and wrong for a block the extractor mangled --
        a code block nested inside an admonition that runs on into the
        paragraph after it, say, which dedents to a shallower margin than the
        code and makes the whole thing an `unexpected indent`. Without this,
        the check would report a clean run over a smaller set than it claims.
        """
        blocks = self.code_blocks_in(
                DocReferenceTest.doc_files() + self.example_files())
        self.assertGreaterEqual(len(blocks), 54,
                                'fewer code blocks were found than the pages '
                                'hold')

        unparsed = []
        for where, line, code in blocks:
            if not code.strip():
                continue
            try:
                ast.parse(code)
            except SyntaxError as e:
                unparsed.append('%s:%d: %s' % (where, line, e))
        self.assertEqual([], unparsed)

    @no_duplicates
    def test_every_import_in_the_documentation_resolves(self):
        """Every module a documented example imports has to exist.

        `bad_keywords_in` cannot answer this. It resolves a call only when the
        name was imported from `schwaby`, so a block importing `schwab` is
        skipped rather than failed, and its wrong import conceals any wrong
        argument -- exactly what a package rename leaves behind. This one
        imports what the
        documentation says to import and fails if it is not there.
        """
        blocks = self.code_blocks_in(
                DocReferenceTest.doc_files() + self.example_files())
        missing, seen = [], set()
        for where, line, code in blocks:
            try:
                tree = ast.parse(code)
            except SyntaxError:                            # pragma: no cover
                continue                # test_every_..._parses owns this case
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module \
                        and not node.level:
                    names = [node.module]
                for name in names:
                    seen.add(name)
                    if name in self.IMPORTS_NOT_INSTALLED:
                        continue
                    try:
                        importlib.import_module(name)
                    except ImportError as e:
                        missing.append('%s:%d: import %s -- %s'
                                       % (where, line, name, e))
        self.assertEqual([], missing)

        # Positive control. The assertion above passes just as well over an
        # empty set, and an extractor that silently stopped finding blocks is
        # the failure this whole class exists to notice.
        self.assertIn('schwaby.auth', seen)
        self.assertIn('schwaby.streaming', seen)
        self.assertIn('schwaby.orders.equities', seen)

    @no_duplicates
    def test_the_documentation_imports_nothing_by_the_old_package_name(self):
        """`import schwab` in a snippet would resolve for anyone who also has
        `schwab-py` installed, and quietly demonstrate the wrong library."""
        blocks = self.code_blocks_in(
                DocReferenceTest.doc_files() + self.example_files())
        offenders = [
                '%s:%d' % (where, line)
                for where, line, code in blocks
                if re.search(r'(?<![\w.-])schwab(?![y\w-])', code)]
        self.assertEqual([], offenders)

    @no_duplicates
    def test_the_examples_directory_is_covered(self):
        # Named separately so that examples/ going unchecked is a failure
        # rather than a silently smaller number.
        files = self.example_files()
        self.assertGreater(len(files), 0)
        blocks = self.code_blocks_in(files)
        self.assertEqual(len(files), len(blocks))
        self.assertEqual([], self.bad_keywords_in(blocks))

    @no_duplicates
    def test_no_example_passes_an_argument_that_does_not_exist(self):
        blocks = DocReferenceTest.doc_files() + self.example_files()
        blocks = self.code_blocks_in(blocks)

        # Positive control for the collection: an empty block list makes the
        # assertion below hold for the wrong reason. Also prove a real call is
        # being resolved and checked, not merely parsed.
        self.assertGreater(len(blocks), 20)
        resolved = sum(
                1 for _, _, code in blocks
                if 'easy_client(' in code or 'client_from_login_flow(' in code)
        self.assertGreater(resolved, 1)

        self.assertEqual([], self.bad_keywords_in(blocks))

    @no_duplicates
    def test_the_check_catches_the_argument_that_was_removed(self):
        # webdriver_func, a parameter easy_client does not take.
        code = ('from schwaby.auth import easy_client\n'
                'c = easy_client(\n'
                "        token_path='/path/to/token.json',\n"
                "        api_key='api-key',\n"
                "        app_secret='app-secret',\n"
                "        callback_url='https://callback.example.com',\n"
                '        webdriver_func=make_webdriver)\n')
        problems = self.bad_keywords_in([('doc.rst', 1, code)])
        self.assertEqual([('doc.rst:1', 'easy_client', 'webdriver_func')],
                         problems)

    @no_duplicates
    def test_a_correct_call_is_not_flagged(self):
        # The same call, correctly written. Without this the test above
        # passes for a checker that flags everything.
        code = ('from schwaby.auth import easy_client\n'
                'c = easy_client(\n'
                "        api_key='api-key',\n"
                "        app_secret='app-secret',\n"
                "        callback_url='https://127.0.0.1:8182',\n"
                "        token_path='/path/to/token.json')\n")
        self.assertEqual([], self.bad_keywords_in([('doc.rst', 1, code)]))


class LongDescriptionTest(unittest.TestCase):
    """The README as PyPI will render it.

    On reStructuredText, `twine check --strict` is a real gate: rst rejects
    malformed markup, such as a title underline one character short, which is
    enough to publish a release with a blank description.

    The README is Markdown, which is far more permissive. Measured against
    `readme_renderer`, it rejects an empty document and a whitespace-only one
    and accepts everything else, including unclosed HTML tags and broken link
    syntax. So `twine
    check` alone cannot catch a description that renders as nothing useful,
    and this asserts the property directly instead: the description renders,
    it is substantial, and it contains what the page has to carry.
    """

    @staticmethod
    def rendered():
        import readme_renderer.markdown
        with in_repo_root():
            with open('README.md', encoding='utf-8') as f:
                text = f.read()
        return readme_renderer.markdown.render(text)

    @no_duplicates
    def test_setup_py_declares_the_matching_content_type(self):
        # A markdown README declared as text/x-rst renders on PyPI as a wall of
        # unformatted text, and nothing fails to tell you.
        self.assertEqual('text/markdown',
                         setup_kwargs()['long_description_content_type'])

    @no_duplicates
    def test_the_readme_renders_to_a_real_page(self):
        html = self.rendered()
        self.assertIsNotNone(html)

        # Substantial, not merely non-empty: the failure being guarded is a
        # description that publishes as blank or near-blank.
        self.assertGreater(len(html), 4000)

        # Single tokens only. A code block comes back syntax-highlighted, so
        # `pip install schwaby` renders as `pip<span class="w"> </span>install`
        # -- asserting a multi-word literal against the HTML checks the
        # highlighter, not the README. An identifier survives because the
        # highlighter wraps it rather than splitting it, which is why
        # `easy_client` is here despite appearing only inside code. Anything
        # with a space in it goes against the raw file instead; see
        # `test_the_install_command_is_in_the_readme`.
        for required in ('schwaby', 'schwab-py', 'easy_client'):
            self.assertIn(required, html)

        # Structure, not just length: a description that lost its headings is
        # not a page even if it is long.
        self.assertGreater(html.count('<h2'), 5)
        self.assertIn('<table>', html)

    @no_duplicates
    def test_the_install_command_is_in_the_readme(self):
        # Against the source, where it is one string. The rendered form is
        # broken up by the syntax highlighter.
        with in_repo_root():
            with open('README.md', encoding='utf-8') as f:
                text = f.read()
        self.assertIn('pip install schwaby', text)
        self.assertNotIn('pip install schwab-py', text)

        # The package is `schwaby`, so this project and `schwab-py` install
        # side by side, and telling readers to uninstall `schwab-py` first
        # would be wrong -- it would strand anyone who still imports
        # `schwab`.
        #
        # Against the source for the same reason as the assertions above:
        # the highlighter splits `pip uninstall -y schwab-py` with `<span>`s,
        # so against the rendered HTML the substring is never there to find
        # and the assertion cannot fail. The README naming `schwab-py` at all
        # is checked in `test_the_readme_renders_to_a_real_page`; what is
        # checked here is that no instruction to uninstall `schwab-py`
        # appears.
        self.assertNotIn('uninstall schwab-py', text)
        self.assertNotIn('uninstall -y schwab-py', text)

    @no_duplicates
    def test_setup_py_names_an_encoding_when_it_reads_a_file(self):
        # test_setup_py_ships_what_was_rendered catches this only where the
        # locale is not UTF-8, which on Linux it is -- so without an encoding
        # that test would pass here and fail only on Windows runners, which
        # run on pull requests and tags, not on pushes. This one
        # is locale-independent: it reads the source.
        #
        # Without an encoding Python uses the locale's preferred one, so a
        # build on Windows decodes the README as a codepage and puts
        # replacement characters where the em dashes are. A wheel built on
        # Linux is fine and one built on Windows is quietly corrupt.
        with in_repo_root():
            with open('setup.py', encoding='utf-8') as f:
                source = f.read()

        # Parsed rather than grepped. `open\(([^)]*)\)` stops at the first
        # `)`, so `open(os.path.join(a, b), encoding='utf-8')` reads as
        # `os.path.join(a, b` -- no `encoding=` in it, and the guard fails on
        # correct code.
        #
        # A denylist rather than an allowlist, because the failure being
        # guarded is a file read *added* later, and an allowlist of two
        # spellings passes anything it has not heard of -- `Path(x).open()`
        # and `Path(x).read_text()` both read a file and both take an
        # `encoding`. The only names excluded are the two that would fail
        # this test with a message blaming the wrong thing: `os.open` takes
        # no `encoding` at all, and `webbrowser.open` opens nothing on disk.
        READS = ('open', 'read_text', 'write_text')
        NOT_FILE_IO = ('os', 'webbrowser', 'sys', 'subprocess')

        def is_open(node):
            if not isinstance(node, ast.Call):
                return False
            if isinstance(node.func, ast.Name):
                return node.func.id in READS
            if not isinstance(node.func, ast.Attribute):
                return False
            if node.func.attr not in READS:
                return False
            value = node.func.value
            return not (isinstance(value, ast.Name)
                        and value.id in NOT_FILE_IO)

        opens = [n for n in ast.walk(ast.parse(source)) if is_open(n)]
        self.assertGreater(len(opens), 1)       # it does read files

        # How many positional arguments a call must have before one of them
        # IS the encoding. The builtin and `io.open` take
        # `(file, mode, buffering, encoding)`, but `Path.open` takes
        # `(mode, buffering, encoding)` -- no `file`, because it is the
        # receiver -- so the same name needs different numbers depending on
        # how it was reached. `Path.read_text(encoding, errors)` and
        # `Path.write_text(data, encoding, errors)` are shorter still.
        #
        # Reporting a call as missing an encoding it did pass would be a
        # false failure.
        BUILTIN_POSITION = {'open': 4}
        METHOD_POSITION = {'open': 3, 'read_text': 1, 'write_text': 2}

        for call in opens:
            if isinstance(call.func, ast.Name):
                name, table = call.func.id, BUILTIN_POSITION
            elif getattr(call.func.value, 'id', None) == 'io':
                name, table = call.func.attr, BUILTIN_POSITION
            else:
                name, table = call.func.attr, METHOD_POSITION

            named = 'encoding' in [kw.arg for kw in call.keywords]
            # A spelling with no entry -- a bare `read_text` from
            # `from pathlib import Path` shadowing, say -- has no known
            # signature, so nothing can be concluded from its positionals.
            # `table[name]` there would raise `KeyError` and blame the wrong
            # thing, which is what this whole matcher exists to avoid.
            wanted = table.get(name)
            positional = wanted is not None and len(call.args) >= wanted
            self.assertTrue(
                    named or positional,
                    'setup.py reads a file without an encoding, at line %d'
                    % call.lineno)

    @no_duplicates
    def test_setup_py_ships_what_was_rendered(self):
        # The assertions above are about README.md on disk. This one is about
        # the string setup.py actually puts in the metadata, which is the thing
        # that reaches PyPI.
        self.assertEqual(self.rendered() is not None, True)
        with in_repo_root():
            with open('README.md', encoding='utf-8') as f:
                self.assertEqual(f.read(), setup_kwargs()['long_description'])
