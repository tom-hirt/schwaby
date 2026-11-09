import setuptools

# encoding= is not optional. Without it Python decodes with the locale's
# preferred encoding, which on Windows is a codepage rather than UTF-8, and
# every em dash in the README becomes a replacement character in the
# long_description this builds. A wheel built on Linux is fine and one built on
# Windows is quietly corrupt, which is the worst way for it to be wrong.
with open('README.md', 'r', encoding='utf-8') as f:
    long_description = f.read()

with open('schwaby/version.py', 'r', encoding='utf-8') as f:
    '''Version looks like `version = '1.2.3'`'''
    version = [s.strip() for s in f.read().strip().split('=')][1]
    version = version[1:-1]

setuptools.setup(
    # Distribution and package are both `schwaby`. A package named `schwab`
    # would be the same directory `schwab-py` ships, which would make the two
    # impossible to install alongside each other.
    name='schwaby',
    version=version,
    # Authorship stays with the original author of the code this began from.
    # His contact details are deliberately not carried over: support for this
    # project should not land in his inbox.
    #
    # No maintainer_email either, and that is deliberate rather than an
    # omission. An address in package metadata is permanent, scraped, and a
    # worse place to report a bug than the tracker -- where the report is
    # searchable, other people can see it has already been raised, and it
    # cannot be lost in a mailbox. The Tracker project URL below is the
    # supported route and the README says so.
    author='Tom Hirt and Alex Golec',
    maintainer='Tom Hirt',
    description=('Unofficial Python client for the Charles Schwab API, built '
                 'for systematic trading against live accounts'),
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/tom-hirt/schwaby',
    # include= rather than a bare find_packages(), which would also match
    # `tests` and ship a top-level `tests` module into every user's
    # site-packages. Anything else installing a top-level `tests` would then
    # collide with it file-for-file, and `import tests` from outside a project
    # root would resolve here.
    packages=setuptools.find_packages(include=['schwaby', 'schwaby.*']),
    classifiers=[
        'Programming Language :: Python :: 3',
        # Named individually as well as generically: shields.io and PyPI's own
        # sidebar read these, and with only the bare '3' the version badge says
        # "3" rather than the range actually supported and tested.
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
        'Programming Language :: Python :: 3.14',
        'Operating System :: OS Independent',
        'Intended Audience :: Developers',
        'Development Status :: 5 - Production/Stable',
        'Natural Language :: English',
        'Operating System :: OS Independent',
        'Topic :: Office/Business :: Financial :: Investment',
    ],
    python_requires='>=3.10',
    install_requires=[
        # Each bounded below by the oldest the suite passes on, and above by
        # its next major: authlib's choice between httpx and httpx2 is the
        # kind of change a major can make silently.
        'authlib>=1.8,<2',
        'httpx2>=2.12.0,<3',
        'websockets>=14.0,<18',

        # The interactive login flow runs a callback server in a separate
        # process. These are hard dependencies rather than an optional extra.
        # An extra would save about a dozen packages for almost nobody:
        # easy_client re-runs the login flow once a stored token is too old,
        # so even a program with a token file needs them. It would also cost
        # two silent failure modes -- pip freeze drops extras, and a deploy
        # check that reads requirement lines may not evaluate one carrying
        # extras -- plus an error path and tests of its own.
        'flask',
        'multiprocess',
        'psutil',
        # Not a transitive freebie. The callback server runs with
        # ssl_context='adhoc', which makes werkzeug call
        # generate_adhoc_ssl_pair -> `from cryptography import x509`; neither
        # flask nor werkzeug declares it. It is installed today only because
        # authlib happens to require it, and if that ever stops the child dies
        # inside app.run and the parent reports RedirectServerExitedError --
        # the misdiagnosis the parent-side imports exist to prevent.
        'cryptography',
    ],
    extras_require={
        # `dev` is the only extra, and there is deliberately no empty `login`
        # or `codegen` shim beside it. A pin naming an unknown extra gets a
        # warning from `pip`, which installs anyway, so a shim would only
        # silence that warning.
        #
        # dev does not list flask or multiprocess, which the suite needs to
        # start its callback servers, because install_requires already
        # installs them; if either leaves install_requires, add it here.
        #
        # autopep8 is here because `make fix` runs it, and that is a
        # maintainer's tool, not a feature of the library.
        'dev': [
            'autopep8',
            # `build` sits beside `twine` on purpose: CONTRIBUTING tells a
            # contributor to run `python -m build` after installing `[dev]`,
            # and without this that instruction fails on a missing module.
            'build',
            'callee',
            'colorama',
            'coverage',
            # packaging_test parses the documentation with it. sphinx_rtd_theme
            # brings it in too, but a test should not rest on another
            # package's dependency.
            'docutils',
            # The commit hooks' light check (tools/light_check.py) runs it.
            'pyflakes',
            'pytest',
            # For `pytest -n auto`, which CONTRIBUTING runs the suite with.
            'pytest-xdist',
            # Not optional either: setup.cfg sets `timeout`, which pytest
            # warns about as an unknown option without it, and the limit is
            # what turns a hung test into a failure that names it.
            'pytest-timeout',
            # packaging_test builds a small project with the API index
            # extension, so it needs sphinx itself rather than the copy
            # sphinx_rtd_theme happens to bring.
            'sphinx',
            'requests',
            'pytz',
            'setuptools',
            'sphinx_rtd_theme',
            'readme_renderer[md]',
            'twine',
            'wheel',
        ]
    },
    keywords='finance trading equities bonds options research',
    project_urls={
        'Documentation': 'https://schwaby.readthedocs.io/',
        'Source': 'https://github.com/tom-hirt/schwaby',
        'Tracker': 'https://github.com/tom-hirt/schwaby/issues',
        'Upstream': 'https://github.com/alexgolec/schwab-py',
    },
    license='MIT',
)

