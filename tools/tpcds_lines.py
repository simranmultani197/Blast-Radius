"""Lines whose edits the TPC-DS check cannot see (build step, dev image).

    python /tools/tpcds_lines.py exec   /src OUT.json   # lines run inside PythonExecutor
    python /tools/tpcds_lines.py import /src OUT.json MODULES.json   # lines run at import

`exec` replays test_execute_tpcds's worker side on the real fixture data in one
process, with LINE events on only while PythonExecutor.__init__ / .execute run
(code generation for the plan and the data processing). It also writes the
sqlglot modules that ended up imported. `import` imports the test module and
those modules in a fresh process and records every line run while importing.
"""
import ast
import csv
import gzip
import json
import os
import pickle
import sys

import unittest  # noqa: E402,F401  (as in the test: sqlglot's UNITTEST mode)

mode, src, out = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, src)
os.chdir(src)
PKG = os.path.join(src, "sqlglot") + os.sep
mon = sys.monitoring
TOOL = 3
mon.use_tool_id(TOOL, "br-lines")
lines = set()


def on_line(code, line):
    if code.co_filename.startswith(PKG):
        lines.add((code.co_filename[len(src) + 1:], line))
    return mon.DISABLE


mon.register_callback(TOOL, mon.events.LINE, on_line)


def dump(extra=None):
    d = {}
    for f, ln in lines:
        d.setdefault(f, []).append(ln)
    rec = {"lines": {f: sorted(v) for f, v in sorted(d.items())}}
    if extra:
        rec.update(extra)
    json.dump(rec, open(out, "w"))
    print(mode, sum(len(v) for v in d.values()), "lines in", len(d), "files")


if mode == "import":
    mon.set_events(TOOL, mon.events.LINE)
    import importlib

    importlib.import_module("tests.test_executor")
    for m in json.load(open(sys.argv[4]))["modules"]:
        importlib.import_module(m)
    mon.set_events(TOOL, 0)
    dump()
    sys.exit()

from sqlglot import find_tables, parse_one  # noqa: E402
import sqlglot.executor as ex  # noqa: E402
from sqlglot.executor.python import PythonExecutor  # noqa: E402
from sqlglot.executor.table import Table  # noqa: E402
from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs  # noqa: E402

depth = [0]


def wrap(fn):
    def inner(*a, **k):
        depth[0] += 1
        if depth[0] == 1:
            mon.set_events(TOOL, mon.events.LINE)
        try:
            return fn(*a, **k)
        finally:
            depth[0] -= 1
            if depth[0] == 0:
                mon.set_events(TOOL, 0)
    return inner


PythonExecutor.__init__ = wrap(PythonExecutor.__init__)
PythonExecutor.execute = wrap(PythonExecutor.execute)

# tables exactly as TestExecutor.setUpClass builds them
tables = {}
for table, columns in TPCDS_SCHEMA.items():
    with gzip.open(f"tests/fixtures/optimizer/tpc-ds/{table}.csv.gz", "rt", newline="") as f:
        reader = csv.reader(f, delimiter="|")
        next(reader)
        rows, ctypes = [], []
        for row in reader:
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    tables[table] = Table(columns=columns, rows=rows)

n = 0
for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
    e = pickle.loads(pickle.dumps(parse_one(sql)))
    if not meta.get("execute"):
        continue
    ex.execute(e, schema=TPCDS_SCHEMA, tables={t.name: tables[t.name] for t in find_tables(e)})
    n += 1
print("executed", n, "queries")
dump({"modules": sorted(m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot."))})
