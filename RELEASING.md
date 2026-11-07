# Releasing and maintaining `schwaby`

Notes for whoever works on this next, including future me. `schwaby` is a
standalone project: there is no upstream to track, no pull request queue, and no
compatibility to preserve with anything but its own published releases.

## Rules that have earned their place

**Write a test that fails before the fix.** Every defect found so far sat in code
with 100% line coverage. Coverage measures which lines ran, not whether the
result was right. Before accepting a test, revert the fix and watch it fail — a
test that passes both ways is worse than none, because it looks like protection.

**A negative result about an installed package is not a result until you know
which directory produced it.** `sys.path[0]` is the current directory, so
`import schwaby` run from the repository reads the working tree and ignores what
pip installed. A reproduction of an install problem run from inside the
repository inspects the source tree, and reports "does not reproduce" whatever
is installed. `cd` somewhere else first, and check `schwaby.__file__` before believing the answer.

**And an editable install goes stale on its own.** An editable install made
before a package directory was renamed goes on mapping the old name to a
directory that no longer exists, so `import schwaby` raises
`ModuleNotFoundError` from outside the repo while the suite passes --- pytest
runs from the root, where the working tree is on the path regardless. The rule
above sends you out of the repo to get a trustworthy answer and this is what
you find when you get there. `pip install -e .` after anything that moves a
package, and before believing an import failure you did not expect.

**A red-proof that greps for `FAILED` cannot see a `subTest`.** pytest reports
a failing subtest as `SUBFAILED(...)`, so a harness matching `FAILED .*::` calls
it green. So a mutation a subtest caught is reported as unnoticed, which is the
worst direction for that error: it invites deleting a guard that works.

**Use `tools/redproof.py` rather than a shell loop.** It checks pytest's
return code; a grep for `FAILED` misses `SUBFAILED`, and it requires the failure
to carry the message you named. The
entries live in `tools/redproof_cases.py`; add one when you fix something, and
run it with `python -I -B tools/redproof.py --name <part of its name>`. It
works on copies, so the working tree is never touched.

**A reworded assertion or a moved test breaks the entries that quote it.**
The release red-proof, which runs every entry, is where that is found. To see
it sooner, `python -I -B tools/redproof.py --touching main` runs only the
entries whose test file, or the file it mutates, is one the branch changes
under `tests/` (a test that imports another test module is not followed to
it), and the commit hook names them as each commit is made. A change to a
shared helper, any file under `tests/` that is not a test module (one named
as `setup.cfg`'s `python_files` says, other than `conftest.py`, that is gone
or defines a test pytest would run), such as `utils.py` or a data file, can
reach every test, so then it runs every entry.

**Check the mutation actually applied.** A `sed` expression spanning two lines
matches nothing, and a restore within the same second reuses stale bytecode
(CPython invalidates on mtime and size). Both have produced a false green here.
Clear `__pycache__`, and confirm the file changed before believing the result.

**The suite mocks the network.** It proves the library builds the request it
intended and says nothing about whether Schwab accepts it. Anything asserting
real API behaviour has to be established against a live account, and the
assertion should say so and give the date.

**Never send a price as a binary float.** `set_price` and `set_stop_price` take a
string or a `decimal.Decimal` and refuse a float, because scaling one and
truncating sent a price a tick low.

**When fixing a defect class, grep for the shape rather than the instance.**
`truncate_float` was fixed to truncate in decimal rather than binary. The
identical defect — `int(float(value) * 1000)` — sat untouched in
`OptionSymbol.build()` for another release and mis-encoded 590 of the 100,000
cent-granular strikes between `$0.01` and `$1000.00`, naming a different contract
on the order-placement path. Nobody looked, because the first fix felt complete.
The same shape recurred in 3.0.0: a fix removed one empty package from the wheel
and stopped, while `find_packages()` was also shipping `tests/`.

**Ask what the bug was covering for.** Several times now a correctness fix has
exposed something worse the defect had been masking. Truncating prices hid that a
`Decimal` built from a float renders its 57-character binary expansion. Migrating
to `httpx2` for correct types flipped the exception hierarchy under any retry
code catching `httpx` exceptions. The question belongs in the fix, not the postmortem.

**An assertion about an empty result needs a positive control.**
`assertEqual([], offenders)` holds when the guard works *and* when the input
never reached it. Prove the collection found something, in the same test.

**A statement is made where it lands, not where you stand.** This has cost twice
now: a diff range measured as `vPREV..HEAD` and asserted about `vPREV..vNEXT`,
and a patch written against the unreleased version and applied to the released
one, where `str(None)` quietly became the string `"None"`. Both
were true where they were written. Before sending or committing a claim, ask
which version, which range, and which machine it will be read on.

**A claim nothing checks will drift, silently.** A README, a changelog preamble
or a docstring asserting something no test covers goes stale without a symptom.
Prefer the weaker sentence you can verify.

**Do not bundle.** A commit that fixes one thing and tidies another cannot be
reverted, bisected to, or described in a changelog entry without dragging the
tidying along.

**Run the suite after merging into `main`, not just on the branch.** Two branches
that each pass can still fail together. It costs four seconds.

## Dependencies

`.github/dependabot.yml` watches the GitHub Actions used by the workflows and
opens a pull request when one goes stale. They had drifted five major versions
behind before anyone noticed, and the only symptom was a deprecation warning
inside a job annotation nobody reads.

**A `dependabot.yml` in the repository is not the mechanism, only half of it.**
Dependabot does nothing until the dependency graph is switched on in the
repository's own settings. Until then the file says it watches the CI actions
while they fall majors behind, and the only symptom is a deprecation
annotation. Same shape as every other guard here that looks present and does
nothing: check that the thing has run, not that the config exists.

**Dependabot offers one action per pull request, and two of these only work
in pairs.** `upload-artifact` and `download-artifact` are versioned separately
and their majors are offset by one from v5 onward, because download took a
breaking change upload did not. The versions that go together are the ones
released on the same day --- upload v7 with download v8. Dependabot opened a
pull request bumping upload to v7 and none for download, which would have left
`publish.yml` handing an artifact between mismatched majors on the one path
that only runs during a release. Merge both or neither, and prove the pair
round-trips a file before believing it.

It deliberately does not watch the Python dependencies. Those are floors rather
than pins, this library places trades, and upgrading one is a decision that wants
the verification below — not a bot's pull request merged on a quiet afternoon.

**There are no optional dependencies and there should not be any.** `dev` is the
only extra. An extra that everybody has to install is a hard dependency with a
way to get it wrong, and `pip freeze` silently drops extras — which is what
turned the 2.3.0 `login` split from a saving into three late failure modes.

## Cutting a release

1. `CHANGELOG.md` — a new section, written for someone deciding whether to
   upgrade.

2. `schwaby/version.py` — bump. **Major if anything public is removed or
   renamed**, minor for added surface or changed behaviour, patch for fixes
   alone.

   Since 2.6.0 this is on PyPI, so `schwaby` or `schwaby>=2.6` in a requirements
   file resolves to whatever is newest and upgrades into a breaking release by
   accident. A changelog banner does not reach someone who never opens one, so
   the version number has to carry it.

3. **Anything naming a version**, which goes stale silently:

   ```shell
   grep -rn 'schwaby@v\|schwaby==' README.md docs/ schwaby/ tests/
   grep -rnE '\b[0-9]+\.[0-9]+\.[0-9]+' README.md docs/*.rst schwaby/ tests/ \
       | grep -vE '127\.0\.0\.1|https?://'
   ```

   The first should be empty. Prose naming a version is worse than a pin,
   because it will not match a pin grep and the release may land under a
   different number than the one written. That is not hypothetical: "Since
   3.0.1 `import schwab` warns" was once written into `README.md` and
   `docs/getting-started.rst`, and this step, which then looked only for
   pins, could not catch it.

   `tests/` is in the walk because a test docstring named the release before
   the tag existed --- "4.1.0 made this raise", written while the number was
   still a proposal. It is prose about a version like any other, and the
   original walk covered only the three published paths.

   Hence the second grep, and note what it is *not*: an allowlist of lead-ins
   like `Since|As of|New in` catches the sentence that was found and misses
   `3.0.1 added a warning`, `starting with 3.0.1`, `from v3.0.1 onward`. A
   check justified by a class of problem has to match the shape --- any
   version-looking number --- not the phrasings already seen. It will have
   hits: read each one and keep only those describing something that already
   shipped and stays true. Say what changed, not which release changed it.

4. **No dates in shipped documentation.**

   ```shell
   grep -rnE '\b20[0-9]{2}-[0-9]{2}-[0-9]{2}\b' README.md docs/*.rst schwaby/
   ```

   Should be empty. A finding measured against a live account is worth
   recording as a finding; the date it was measured is not. Documentation
   describes how the API behaves in this version, and a reader hitting a
   measurement date has to decide whether it still holds --- which is a
   question they cannot answer and which the sentence invites. Say what the
   behaviour is, and say it was measured rather than inferred if that matters.

   Dates belong in `CHANGELOG.md`, which is a dated record by construction.

5. **For a documentation-only release, prove it rather than asserting it.**

   ```shell
   python - <<'EOF'
   import ast, subprocess, pathlib
   PREV = 'vPREV'
   PKG = 'schwaby/'
   # A pathspec that matches nothing returns an empty list, which reads as
   # "no library file changed" -- so a wrong path passes this check for any
   # release at all. 4.0.0 renamed the package and left this pointing at
   # `schwab/`; assert the path before trusting its silence.
   assert pathlib.Path(PKG).is_dir(), PKG + ' is not the package directory'
   changed = subprocess.run(['git', 'diff', '--name-only', PREV + '..HEAD',
                             '--', PKG],
                            capture_output=True, text=True).stdout.split()
   def strip(src):
       t = ast.parse(src)
       for n in ast.walk(t):
           if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
               b = n.body
               if (b and isinstance(b[0], ast.Expr)
                       and isinstance(b[0].value, ast.Constant)
                       and isinstance(b[0].value.value, str)):
                   b.pop(0)
       return ast.dump(t)
   for f in changed:
       old = subprocess.run(['git', 'show', PREV + ':' + f],
                            capture_output=True, text=True).stdout
       new = pathlib.Path(f).read_text()
       print(f, strip(old) == strip(new))
   EOF
   ```

   A text diff cannot answer this. 3.0.3 changed 139 lines across six files,
   including 66 in `client/base.py` and 41 in `streaming.py`, and a careful
   reader looking at that diff would reasonably go hunting for a behaviour
   change that is not there. Comparing the parsed trees with docstrings
   stripped separates prose from behaviour, and leaves `version.py` as the
   only executable difference --- which is the safest shape a bump can have.

   Say which method established it in the release notes, not just the verdict.
   Anyone pinning a release should be able to re-run the check rather than
   trust the claim.

6. Verify, on **3.11, 3.12 and 3.14** — 3.14 is the newest Python supported,
   and `asyncio` semantics differ below 3.12 as well as above it:

   ```shell
   pytest tests/ -q -n auto
   python -m build
   python -m twine check --strict dist/*
   python -m sphinx -W docs/ /tmp/docs-build
   ```

   **`twine check`'s output must be read, not glanced at.** It once reported a
   failure that was missed in truncated output, which would have published a
   release with no description. Note it is a much weaker gate for a markdown
   README than it was for reStructuredText: measured, it rejects only an empty or
   whitespace-only document. `tests/packaging_test.py::LongDescriptionTest`
   asserts the real property, and `readme_renderer[md]` must be installed or the
   check silently passes having rendered nothing.

   `python -m build` earns its place: `setup.py` is not imported by the suite, so
   an edit leaving it unparseable is invisible to `pytest`.

   Then the red-proof, at the commit you will tag:

   ```shell
   python -I -B tools/redproof.py
   ```

   It runs every entry: no verdict is kept from one run to the next. Every
   entry must be PROVEN, SUPERSEDED or REMOVED, and no run may write to a
   tracked file or go unchecked for one.

   **Nothing is tagged until the release is signed off.** The maintainer
   approves it having seen the results above for the exact commit that will be tagged. A result from
   an earlier commit does not count: the release commit itself is the one that
   changes `version.py`. The same approval covers the PyPI upload, which
   waits at the publish workflow's `pypi` environment until someone approves
   it there.

7. Commit, then `git tag -a vX.Y.Z`. Write the message from a file — backticks in
   `git tag -m` are executed as command substitution, which silently swallowed a
   word from the v2.5.0 tag.

8. `git push origin main && git push origin vX.Y.Z`

9. Create the release, only once `main` on GitHub holds the commit GitHub's
   tag names:

   ```shell
   git fetch origin
   tagged=$(git ls-remote origin 'refs/tags/vX.Y.Z^{}' | cut -f1)
   test -n "$tagged" && git merge-base --is-ancestor "$tagged" origin/main \
       && gh release create vX.Y.Z -R tom-hirt/schwaby --notes-file ...
   ```

   GitHub's tag, not the local one: the release is made from GitHub's, and
   step 10 says why the two can differ. A tag made without `-a` has no
   `^{}` line and is refused here; re-make it as step 7 does.

   A tag can name a commit `main` does not hold: one cut on a branch, or
   pushed apart from `main`. The publish workflow refuses such a commit too,
   but only once the release exists; this check stops it first.

   **Creating the release is what publishes to PyPI.**
   `.github/workflows/publish.yml` runs on a published release, re-runs the suite
   on all five Pythons, checks `main` holds the commit, runs `twine check
   --strict`, verifies the built version matches the tag, and uploads via
   trusted publishing. Pushing a tag alone
   publishes nothing, so a tag can be moved before the release is created. After
   it, the version is permanent: PyPI refuses a re-upload even after a delete.

   If the publish fails, retry it without cutting another release, on the
   release's tag itself:

   ```shell
   gh workflow run publish -R tom-hirt/schwaby --ref vX.Y.Z
   ```

   The run then builds that tag's commit, with the tag's own copy of the
   workflow, and runs every check a release does. Run from a branch, or on a
   tag with no published release, it stops before building: the branch's tip
   would be newer code under the same version. The `pypi` environment also
   deploys only from a `v*` tag. That holds for tags from v5.3.0 on. Older
   tags carry a workflow without these checks, and every version they name
   is already on PyPI, which refuses a re-upload.

10. **If you move a tag, say so — a normal `git fetch` will not follow it.**

   Deleting and re-creating a pushed tag is sometimes right; v3.0.0 was re-cut
   before publishing to fold in a documentation change. But git will not move a
   tag ref a client already holds, and says nothing while declining to. So
   anyone who fetched the old one keeps it silently, and the check most likely
   to care — does this artifact match the tag I audited — is exactly the one
   that gets a confident wrong answer.

   Anyone who audited the old tag will conclude, confidently and wrongly, that
   the published wheel does not match its own git tag. The fetch that fixes it:

   ```shell
   git fetch origin --prune-tags --force --tags
   ```

   Tell anyone downstream when a tag moves. A stale ref plus a confident tool is
   worse than a wrong answer, because a wrong answer invites a second look.

11. **Re-check any claim about the release against the tag, after tagging.**

   The range available while preparing a release is `vPREV..HEAD`, which excludes
   the commit that bumps `version.py` — so the convenient measurement is
   systematically the one that flatters the claim. "This release touches nothing
   under the package", measured over `vPREV..HEAD`, is false over
   `vPREV..vNEXT`, which includes `version.py`.

## The distribution name and the import name

Both are `schwaby`, as of 4.0.0. `pip install schwaby` then `import schwaby`,
and there is nothing further to explain to a reader.

They used to differ: the distribution was renamed to `schwaby` at 2.6.0 and the
package stayed `schwab`, so that moving over meant changing one line of
`requirements.txt` and nothing else. The cost was that `schwaby` and `schwab-py`
both provided a directory called `schwab`, so whichever pip installed second
silently overwrote the other's files --- no warning, no failure at install time,
and a version banner that named whichever project had won. 3.0.1 shipped an
import-time check for it, which could itself be overwritten by the collision it
detected, and 3.0.2 withdrew that. 4.0.0 renamed the package instead.

So: the two install side by side now, and **the install documentation must not
tell anyone to uninstall `schwab-py` first.** It was correct through 3.0.3 and
is wrong from 4.0.0 --- an instruction to uninstall the package that owns
`schwab/` will strand anyone who still imports it.

`tests/packaging_test.py::…test_the_distribution_and_the_package_have_the_same_name`
holds the names together. If a future release wants them to diverge again, that
test is the thing to argue with first.

A git install works for testing an unreleased commit:

```shell
pip install "schwaby @ git+https://github.com/tom-hirt/schwaby@<sha>"
```

Pin a commit, never a branch. Tags before the 2.6.0 rename carry `schwab-py` in
their metadata, so `pip` refuses `schwaby @ git+...@v2.5.1`.

**Never write `pip install schwab-py` as an instruction for this project.** That
installs a different, much older codebase, and it will appear to work because the
importable package has the same name either way.
