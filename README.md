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
- **Module-level statement**: every importer.
- **Fixtures and data files**: tests that opened them (or listed their
  directory, for added/removed files).
- **pyproject.toml / setup.cfg**: every test if pytest's settings change or
  the file stops parsing.

Any analysis error falls back to running every test.

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

`make sample` scores the 30 sample commits (see `KIT_README.md`, `SCORING.md`).
