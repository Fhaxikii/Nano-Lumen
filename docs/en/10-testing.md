# 10 · Testing

**What this page covers**: how to run the tests, how they are organized, and
how to add one.
**After reading it you can**: verify before submitting a change, and add
tests for new features.
**Prerequisites**: [01-getting-started.md](01-getting-started.md).

> Language: [中文](../zh/10-testing.md) · English

---

## Running the full suite

```
bash run_tests.sh
```

Expected output:

```
OK 真·全量 0 失败
```

Any other output means there are failures; the script lists each failing
file with its summary line.

## Running a single test

```
python tests/t_xxx.py
```

Test files run standalone; no test framework is needed.

## Organization

All tests live in `tests/`, named `t_*`. Each file covers one functional
area, runs standalone, and shares no state.

There is no pytest or any framework — tests are plain scripts: a `check()`
function collects assertion results, and the end prints a
`passed/total passed` summary line.

Skipping a framework is deliberate: running the tests should not add
dependencies, and crash-recovery cases need to spawn subprocesses and
`os._exit` at real transition points — plain scripts beat fighting a
framework's output capture.

`_console.py` and `_win_window.py` in this directory are helpers, not
tests; `_console.py` provides console output protection (see step 2 of the
next section).

`run_tests.sh` judges success by parsing that summary line. It accepts two
summary formats and checks that passed equals total — checking only "0
failures" would let "77/79 passed" slip through.

## Adding a test

1. Create `tests/t_<area>.py`. Naming only requires the `t_` prefix;
   put what the test verifies into the file's header docstring.
2. First line: `import tests._console`. On Windows the default console
   encoding is GBK; emoji or special symbols in assertion names make
   `print` raise `UnicodeEncodeError` — the case aborts and the assertion
   is never counted. It looks like "ran halfway then crashed", but in fact
   the assertion never existed. `_console.py` replaces unencodable
   characters with `?` so the worst case stays readable. Keep assertion
   names themselves to console-safe characters.
3. Follow the existing shape: define `check(ok, name, note)`, assert item by
   item, print the summary line at the end.
4. The summary line must be parseable by `run_tests.sh`, or the run is
   judged "no summary" and fails.
5. The test must run standalone, not depend on other tests having run first.

## What to test

Prioritize properties that "fail silently" — defects that throw no exception,
just quietly do the wrong thing, and are nearly impossible to catch reliably
by manual acceptance. Examples:

- Whether a piece of data has exactly one write entry point.
- Whether an ordering constraint holds.
- Whether a degradation path is actually taken.

One assertion guards exactly the property it was written for. If a function
has two constraints and you pin only one, the other can still be broken.

Three disciplines from hard-won experience:

- **For fault injection, determinism beats realism.** The goal is to verify
  the pipeline works, not to reproduce a real error.
- **A case that expects something NOT to happen must also assert that the
  precondition DID happen.** Otherwise a failed injection disguises itself
  as a pass — the subprocess died before reaching the injection point while
  the expectation happened to be "nothing was persisted". Assert the
  subprocess exit code, and print the tail of its stderr on mismatch.
- **To check "an identifier is fully removed", use `ast.walk`, never
  `in src`.** Text matching also hits comments and docstrings — the more
  diligently the removal is explained in the source, the higher the chance
  of a false hit. Add a precondition assertion too: have the AST find a name
  that certainly still exists in the same tree, proving the analyzer works.

## Known failure

`tests/t_f5_live.py` crashes with return code 139 when memory is
insufficient. It loads the embedding model, which commits about 2.3 GB at
once. This is an environment problem, not a code defect. How to tell: every
other test passes and available system memory is below that value.

---

## How to verify you got it right

1. The new test passes standalone.
2. Deliberately break the property under test: the test fails. A test that
   can never fail is meaningless.
3. `bash run_tests.sh` prints `OK 真·全量 0 失败`.
