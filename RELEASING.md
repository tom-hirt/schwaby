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
repository inspects the source tree, and can report "does not reproduce"
while the installed copy is broken. `cd` somewhere else first, and check `schwaby.__file__` before believing the answer.

**And an editable install goes stale on its own.** An editable install made
before a package directory was renamed goes on mapping only the old name, to a
directory that no longer exists, and maps the new one not at all, so
`import schwaby` raises
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
works on copies, so the working tree is never touched. An entry whose test
signals processes (the worker tests, listed in `SIGNALLING`) is refused
there and runs only as a queued job, where `REDPROOF_JOBS_ALL=1`: its test
kills only what it started and recorded, but the code it mutates is the
code that decides what is killed.

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
matches nothing, so the mutant never runs and the test stays green. A restore
within the same second can reuse the mutant's bytecode (CPython invalidates on
mtime and size), so a passing test looks failed.
Clear `__pycache__`, and confirm the file changed before believing the result.

**The suite mocks the network.** It proves the library builds the request it
intended and says nothing about whether Schwab accepts it. Anything asserting
real API behaviour has to be established against a live account, and the
assertion should say so and give the date.

**Never send a price as a binary float.** `set_price` and `set_stop_price` take a
string or a `decimal.Decimal` and refuse a float, because scaling one and
truncating can send a price a tick low.

**When fixing a defect class, grep for the shape rather than the instance.**
The same mistake is usually written in more than one place, and fixing the one
that was reported feels complete while the others go on shipping. A float
scaled and truncated in one helper, such as `int(float(value) * 1000)`, is
likely scaled and truncated somewhere else too --- and in an option symbol, a
mis-encoded strike names a different contract. A packaging mechanism that
ships one directory it should not may be shipping others: ask what else
`find_packages()` matches.
Search for the expression or the mechanism, not for the function in the report.

**Ask what the bug was covering for.** A correctness fix can expose something
worse that the defect had been masking. Truncating a price hides that a
`Decimal` built from a float renders its 57-character binary expansion. Moving
to `httpx2` for correct types changes the exception hierarchy under any retry
code catching `httpx` exceptions. The question belongs in the fix, not the postmortem.

**An assertion about an empty result needs a positive control.**
`assertEqual([], offenders)` holds when the guard works *and* when the input
never reached it. Prove the collection found something, in the same test.

**A statement is made where it lands, not where you stand.** A claim true where
it was written can be false where it is read: a diff range measured as
`vPREV..HEAD` says nothing about `vPREV..vNEXT`, and a patch written against an
unreleased version can behave differently applied to the released one. Before
sending or committing a claim, ask which version, which range, and which
machine it will be read on.

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
opens a pull request when one goes stale. Without it they drift major versions
behind, and the only symptom is a deprecation warning inside a job annotation
nobody reads.

**A `dependabot.yml` in the repository is not the mechanism, only half of it.**
Dependabot opened nothing here while the dependency graph was switched off in
the repository's settings; in that state the file says it watches the CI
actions while they fall majors behind, and the only symptom is a deprecation
annotation. As with any guard that can look present and do nothing, check that
the thing has run, not that the config exists.

**Dependabot offers one action per pull request, and two of these only work
in pairs.** `upload-artifact` and `download-artifact` are versioned separately
and their majors are offset by one from v5 onward, because download took a
breaking change upload did not. The versions that go together are the ones
released on the same day --- upload v7 with download v8. Dependabot can open a
pull request bumping one and none for the other, which leaves `publish.yml`
handing an artifact between mismatched majors on the one path that only runs
during a release. Merge both or neither, and prove the pair
round-trips a file before believing it.

**An action pinned to a commit names its release beside the pin, and a
moved pin is checked against that release's tag.** Every use of one action
must carry the same commit and the same `# vX.Y.Z` (the suite checks that),
and the commit must be the one the tag names:
`gh api repos/OWNER/NAME/git/ref/tags/vX.Y.Z --jq .object.sha`, dereferenced
once more if that is an annotated tag. A bump that moves the commit but not
its comment, or reaches some uses and not others, is the shape this catches.

It deliberately does not watch the Python dependencies. Those are floors rather
than pins, this library places trades, and upgrading one is a decision that wants
the verification below — not a bot's pull request merged on a quiet afternoon.

**There are no optional dependencies and there should not be any.** `dev` is the
only extra. An extra that everybody has to install is a hard dependency with a
way to get it wrong. `pip freeze` writes `schwaby==X.Y.Z` without its extras,
so a requirements line taken from it installs the library without the extra's
packages, and the environment looks complete until code needing them first
runs.

## Cutting a release

A release is made on a release branch of its own, `release/vX.Y.Z`, from a
fixed base commit on `main`. The cut, the release gate, the release red-proof
and the tag all happen there, and only fixes the release needs are added;
then the branch merges back into `main`, and only after that is the release
created and published.

**Every release has a tier, named when it is asked for, and the tier sets
its proof.** It is FULL when anything it ships changes sign-in, tokens or
credentials (`schwaby/auth.py`), the order path (`schwaby/orders/`,
`schwaby/client/`, and the order ids `schwaby/utils.py` reads), the data
callers trade on (`schwaby/streaming.py`, the Decimal decoding in
`schwaby/contrib/util.py`), secret redaction (`schwaby/_redact.py`), or how
the package installs. Everything else, words, docs, tooling and tests, is
LIGHT. No proof runs until the maintainer has called the release, and at
most one batched release goes out a week; a FULL fix to trading or security
may go any day.

1. **The release branch**, from the base the release is of:

   ```shell
   git fetch origin
   git rev-parse origin/main          # the base: name it in the sign-off
   git switch -c release/vX.Y.Z origin/main
   ```

   Everything from here to the tag happens on this branch.

2. `CHANGELOG.md` — a new section, written for someone deciding whether to
   upgrade.

3. `schwaby/version.py` — bump. **Major if anything public is removed or
   renamed**, minor for added surface or changed behaviour, patch for fixes
   alone.

   This is on PyPI, so `schwaby` or `schwaby>=2.6` in a requirements
   file resolves to whatever is newest and upgrades into a breaking release by
   accident. A changelog banner does not reach someone who never opens one, so
   the version number has to carry it.

4. **Anything naming a version**, which goes stale silently:

   ```shell
   grep -rn 'schwaby@v\|schwaby==' README.md docs/ schwaby/ tests/
   grep -rnE '\b[0-9]+\.[0-9]+\.[0-9]+' README.md docs/*.rst schwaby/ tests/ \
       | grep -vE '127\.0\.0\.1|https?://'
   ```

   The first should be empty. Prose naming a version is worse than a pin,
   because it will not match a pin grep and the release may land under a
   different number than the one written.

   `tests/` is in the walk because a test docstring can name a release before
   its tag exists, while the number is still a proposal. It is prose about a
   version like any other.

   Hence the second grep, and note what it is *not*: an allowlist of lead-ins
   like `Since|As of|New in` catches some phrasings and misses
   `3.0.1 added a warning`, `starting with 3.0.1`, `from v3.0.1 onward`. A
   check justified by a class of problem has to match the shape --- any
   version-looking number --- not the phrasings already seen. It will have
   hits: read each one and keep only those describing something that already
   shipped and stays true. Say what changed, not which release changed it.

5. **No dates in shipped documentation.**

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

6. **For a documentation-only release, prove it rather than asserting it.**

   ```shell
   python - <<'EOF'
   import ast, subprocess, pathlib
   PREV = 'vPREV'
   PKG = 'schwaby/'
   # A pathspec that matches nothing returns an empty list, which reads as
   # "no library file changed" -- so a wrong path passes this check for any
   # release at all. Assert the path before trusting its silence.
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

   A text diff cannot answer this. A docstring change can touch a hundred
   lines across several files of the package, and a careful reader looking at
   that diff would reasonably go hunting for a behaviour change that is not
   there. Comparing the parsed trees with docstrings
   stripped separates prose from behaviour, and leaves `version.py` as the
   only executable difference --- which is the safest shape a bump can have.

   Say which method established it in the release notes, not just the verdict.
   Anyone pinning a release should be able to re-run the check rather than
   trust the claim.

7. **The cut:** every edit of steps 2 to 6 (the changelog section, the
   version bump, and any stale version prose step 4 found) in one commit,
   `release: vX.Y.Z — headline`, pushed to the release branch:

   ```shell
   git add -A && git status --short   # read it: only the release's edits
   git commit -m 'release: vX.Y.Z — headline'
   git push -u origin release/vX.Y.Z
   ```

8. **The proof, by tier.** A LIGHT release runs no release gate and no
   release red-proof: one pass of the suite on 3.14 and the build, `twine`
   and docs checks below, on the release branch's head, with CI's run for
   that commit. That run may be on a commit that differs from the one to be
   tagged only in the changelog, the README's status or the version string,
   such as the head before the cut, with no second run. A FULL release runs the release gate, on the release
   branch's head, on **3.11, 3.12 and 3.14**, one pass on each — 3.14 is the
   newest Python supported, and `asyncio` semantics differ below 3.12 as
   well as above it:

   ```shell
   pytest tests/ -q -n auto
   python -m build
   python -m twine check --strict dist/*
   python -m sphinx -W docs/ /tmp/docs-build
   ```

   **`twine check`'s output must be read, not glanced at.** A failure is easy
   to miss in truncated output, and a missed one publishes a release with no
   description. Note it is a much weaker gate for a markdown
   README than for reStructuredText: measured, it rejects only an empty or
   whitespace-only document. `tests/packaging_test.py::LongDescriptionTest`
   asserts the real property, and `readme_renderer[md]` must be installed or the
   check silently passes having rendered nothing.

   `python -m build` earns its place: `setup.py` is not imported by the suite, so
   an edit leaving it unparseable is invisible to `pytest`.

   CI runs the whole matrix, every Python on all three platforms, on each
   push to a release branch. Read that run for the same commit, and wait for
   it to pass: it is the only run on Windows and macOS before the release,
   since the publish workflow tests on Linux alone.

   Then, for a FULL release, the release red-proof at the commit you will
   tag, of only the entries whose code changed since the last proven
   release:

   ```shell
   python -I -B tools/redproof.py --touching vPREV
   ```

   Every entry it runs must be RED, GREEN (declared), RED (declared GREEN)
   or REMOVED, and no run may write to a tracked file or go unchecked for
   one. A change to how the red-proof runs (`tools/redproof*.py`) is proven
   on a sample of about twenty entries (`--shard 1/44`) and its own entries,
   never by running them all again. A change to how it judges a verdict
   that could turn a RED into a GREEN re-proves the entries it affects
   (`--name`), named with their cost before they run.

   **Nothing is tagged until the release is signed off.** The maintainer, or
   whoever they have delegated release approval to, approves it having seen the results above for the exact commit that will be tagged. A result from
   an earlier commit does not count: the release commit itself is the one that
   changes `version.py`. The same approval covers the PyPI upload, which
   waits at the publish workflow's `pypi` environment until someone approves
   it there.

   **A fix the release needs goes onto the release branch, and nothing
   else does.** Push it at once, so CI's full matrix runs on the new head.
   Re-prove only the fix's own scope there: the sentinels it touches and,
   for a FULL release, the red-proof entries whose reads it changes and the
   release gate once more if it changes anything under `schwaby/`. A proof
   that fails re-proves only what failed, never the whole run. The new head, with its CI
   run passed, is the commit that is signed off and tagged. Work on `main`
   goes on meanwhile and is not taken in: the base stays fixed.

9. **The tag, on the proven head of the release branch:** `git tag -a vX.Y.Z`
   on the commit the release gate and the release red-proof passed. Write the
   message from a file — backticks in `git tag -m` are executed as command
   substitution, which silently swallows words from the message.

10. **Push the tag, then merge the release branch back into `main`,** in a
    local merge, never a squash or a rebase, which would leave `main`
    without the tagged commit and the release unable to publish:

    ```shell
    git push origin vX.Y.Z
    git switch main && git pull --ff-only origin main
    git merge release/vX.Y.Z    # a fast-forward if main has not moved
    git push origin main
    ```

    If `main` moved since the base, this is a merge commit, made through the
    hooks like any other, and the gate runs once more on the merged tree
    before the push: two branches that each pass can still fail together. If
    that gate fails, the fault is in the combination, not in the release:
    the tag stays where it is, and the fix goes onto `main` as an ordinary
    change before the merge is pushed. If the push is refused because `main`
    moved again, pull, merge and run the gate again.

    The tag stays on the release branch's head, which `main` now holds.
    Delete the release branch only once that is so:

    ```shell
    git fetch origin
    git merge-base --is-ancestor vX.Y.Z origin/main \
        && git push origin --delete release/vX.Y.Z
    ```

11. Create the release, only once `main` on GitHub holds the commit GitHub's
    tag names, which it does once step 10 has merged the release branch back:

    ```shell
    git fetch origin
    tagged=$(git ls-remote origin 'refs/tags/vX.Y.Z^{}' | cut -f1)
    test -n "$tagged" && git merge-base --is-ancestor "$tagged" origin/main \
        && gh release create vX.Y.Z -R tom-hirt/schwaby --notes-file ...
    ```

    GitHub's tag, not the local one: the release is made from GitHub's, and
    step 12 says why the two can differ. A tag made without `-a` has no
    `^{}` line and is refused here; re-make it as step 9 does.

    A tag can name a commit `main` does not hold: a release branch not yet
    merged back, or a tag pushed apart from either. The publish workflow
    refuses such a commit too, but only once the release exists; this check
    stops it first.

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

12. **If you move a tag, say so — a normal `git fetch` will not follow it.**

    Deleting and re-creating a pushed tag is sometimes right, for instance to
    fold a late documentation change into a release before it is published. But
    git will not move a
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

13. **Re-check any claim about the release against the tag, after tagging.**

    The range available while preparing a release is `vPREV..HEAD`, which excludes
    the commit that bumps `version.py` — so the convenient measurement is
    systematically the one that flatters the claim. "This release touches nothing
    under the package", measured over `vPREV..HEAD`, is false over
    `vPREV..vNEXT`, which includes `version.py`.

## The distribution name and the import name

Both are `schwaby`. `pip install schwaby` then `import schwaby`, and there is
nothing further to explain to a reader.

Keep them that way. An import name another distribution also ships is a
directory both write to: whichever pip installs second silently overwrites the
other's files --- no warning, no failure at install time, and a version banner
naming whichever project won. An import-time check cannot guard against that,
because the collision can overwrite the check along with everything else.

`schwaby` and `schwab-py` install side by side, so **the install documentation
must not tell anyone to uninstall `schwab-py` first.** `schwab-py` owns
`schwab/`, and an instruction to uninstall it will strand anyone who still
imports it.

`tests/packaging_test.py::…test_the_distribution_and_the_package_have_the_same_name`
holds the names together. If a release wants them to diverge, that test is the
thing to argue with first.

A git install works for testing an unreleased commit:

```shell
pip install "schwaby @ git+https://github.com/tom-hirt/schwaby@<sha>"
```

Pin a commit, never a branch. Tags before the 2.6.0 rename carry `schwab-py` in
their metadata, so `pip` refuses `schwaby @ git+...@v2.5.1`.

**Never write `pip install schwab-py` as an instruction for this project.** That
installs a different, much older codebase, and it will appear to work because the
importable package has the same name either way.
