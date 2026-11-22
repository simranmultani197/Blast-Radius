# Blast Radius: coverage-map test selection for sqlglot

For each patch, the selector finds the statements it changes in the pinned
sqlglot tree (`eb11aec`) and selects the tests whose execution can reach them.
Everything it knows comes from maps built once from the pinned commit; nothing
is learned from or keyed by patches.

## Maps (built offline from the pinned commit, `maps/map.json.gz`)

`tools/trace_tests.py` runs **every test in its own forked process** with a
`sys.monitoring` tracer (`tools/trace_site/sitecustomize.py`) that follows the
test into every Python process it spawns (`test_optimizer` and `test_executor`
use spawn pools; `test_dialect` runs `python -c`). Per test it records:

- executed lines in `sqlglot/` and `tests/`. Because each test imports
  everything itself, module- and class-level lines count for exactly the tests
  that import that module;
- modules imported (so empty `__init__.py` files still have importers);
- the classes (whole MRO) of every `self`/`cls` its sqlglot methods ran on,
  outside import time;
- lambdas it called (one-line lambdas in class-level tables share a line with
  import-time code);
- data files it opened and directories it listed (fixtures);
- which class each ALL_CAPS class attribute it read at runtime resolved from
  (an AST rewrite of `obj.NAME` loads, used only while tracing), and which were
  read at import time.

`tools/class_info.py` adds, per class, whether it is an Enum or an Expression,
its aliases (`MySQL.Parser` is `MySQLParser`) and the dialects it belongs to
(a class in the tokenizer/parser/generator MRO of dialect D). `tools/xrefs.py`
indexes import-time reads like `{**MySQL.Parser.FUNCTIONS, ...}`.

## Selecting (`br/select.py`)

Per changed file:

- **Python, same AST** after dropping comments, formatting and docstrings
  (no doctests are collected): nothing can change.
- **Does not parse**: every test that imports the module.
- **Changed statement inside a function**: tests that executed any line of
  that statement (whole statement, not just the line: `a and b` → `a or b`
  changes behaviour for tests that short-circuited before the edited line).
  Inside a lambda: tests that called that lambda. Signature edits: tests that
  ran the body. Insertions: tests that ran the enclosing function.
- **Class-level statement**: if it assigns ALL_CAPS attributes that are never
  read at import time, never read by name inside the class body and never
  named in a string literal, the tests that read that attribute from that
  class at runtime. Otherwise tests that used the class or a subclass, or any
  dialect it belongs to, plus import-time copies found by the xref index.
  Enums and unknown classes fall back to every importer.
- **Comparison operator swap** (`<` → `<=`, `is` → `==`, ...): the tracer
  evaluates every alternative operator at each comparison and records, per
  test, where the outcome would differ, so only those tests are selected. For
  comparisons that also run while modules are imported, it also records which
  alternatives would change an import-time outcome. If the new operator changes
  none, import is unaffected and the runtime outcomes decide. Otherwise every
  importer is selected.
- **Value edit in a class's `arg_types` dict** (e.g. `"this": True` →
  `False`): the values are read only by `Expr.__init_subclass__` (into
  `required_args`) and by `**Other.arg_types` spreads, and `required_args` only
  by `error_messages`. `tools/argtypes.py` checks this statically, and also
  checks that every runtime read of `arg_types` in `sqlglot/` and `tests/`
  uses keys only. A light trace (`BR_TRACE_ERRMSG=1`) records, per
  `error_messages` call, the class that defined the dict and each key missing
  from the instance's args. The rule selects the tests that validated an
  instance with that key missing, for the class and for any class spreading
  its dict. A key validated during an import falls back to the class rule.
- **Module-level statement**: every importer.
- **Fixtures and data files**: tests that opened them (or listed their
  directory, for added/removed files).
- **Pair-format SQL fixtures loaded in `setUpClass`** (the TPC-H/TPC-DS files
  in `TestExecutor`): the selector replays `load_sql_fixture_pairs` on the old
  and new text to find which elements (meta, input, expected) of which pairs
  changed. It then reads the pinned test module to see which element each
  test keeps from the class attribute holding the pairs (`for _, sql, _ in
  ...`, unpacking assignments, pass-through to a method), with line coverage
  and ternary-branch profiles saying which tests ran each use. Executor tests
  that never read the pairs drop out. Readers in other classes keep every
  element. A changed `#` meta line, a changed statement count, a loader error
  or any changed test code fall back to every reader.
- **pyproject.toml / setup.cfg**: every test if pytest's settings change or
  the file stops parsing.

Any analysis error falls back to running every test.

## The TPC-DS executor test (`br/tpcds_check.py`)

`test_execute_tpcds` (21.5 s) runs most of sqlglot, so it is selected for
about 18% of code edits, and fails only for some. When it is selected, the
selector drops it if all three hold:

1. the patch only edits statements inside function bodies of `sqlglot/*.py`,
   outside `sqlglot/executor/`;
2. none of those statements runs inside `PythonExecutor` (plan code generation
   and data processing) or at import time. `tools/tpcds_lines.py` traced those
   lines over the real fixture data (`maps/tpcds_lines.json`);
3. `br/tpcds_probe.py`, run in a child process on a `/tmp` copy of the tree with
   the patch applied, produces the same per-query fingerprint as the pinned
   tree (computed in the image at build time). The fingerprint covers parse and
   pickle without errors, the tables used, every plan step with its
   expressions, types, comments and meta, and the duckdb SQL. The probe runs
   in the test's own mode (`unittest` imported first) and stops at the first
   query that differs.

Everything the executor and duckdb receive is then identical, so the rows
compared are too. Checks share a 100 s budget per run.

## Packing (`br/pack.py`)

Longest-processing-time first onto k workers for every k in 1..8, keeping the
k with the smallest `max load + 2 s × k` (it meets the lower bound on every
sample and validation commit).

## Validation

`tools/gen_mutants.py` makes synthetic mutants of the pinned tree (operator and
constant swaps, negation, statement deletion, comment insertion, fixture edits,
multi-file combinations) and `tools/label.py` labels them by running the full
suite; `tools/eval_mutants.py` checks the selector never misses. These are for
testing only and are not shipped.

`tools/gen_fixture.py` makes targeted edits to the TPC fixtures (input and
expected statements, comment, blank and meta lines, `;` changes, and
sample-style token edits), labelled by running every reader of the file.

`make sample` scores the 30 sample commits (see `KIT_README.md`, `SCORING.md`).
