"""What tests/test_executor.py::TestExecutor::test_execute_tpcds hands to code
that runs on data, for every TPC-DS query, under a given sqlglot tree.

    python tpcds_probe.py <sqlglot root> <pinned root>      -> JSON on stdout

<sqlglot root> holds the sqlglot package to import (the pinned tree, or a copy
with a patch applied); <pinned root> supplies tests/helpers.py and the TPC-DS
fixture, which a patch the caller accepts never touches.

Per query, like the test: parse_one in the main process and a pickle round
trip to the worker; for `# execute` queries, find_tables, then execute() up to
the moment it builds a PythonExecutor (optimize + Plan with the same schema
and table stubs), and the duckdb SQL the test compares against. Expressions
are serialised with their args, types, comments and meta; plans step by step.
"""
import importlib.util
import json
import os
import pickle
import sys


def main():
    root, pinned = sys.argv[1], sys.argv[2]
    # the test imports unittest before sqlglot, and sqlglot validates expressions
    # more strictly when it sees it (expressions/core.py UNITTEST)
    import unittest  # noqa: F401

    sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location("br_helpers", os.path.join(pinned, "tests", "helpers.py"))
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)

    import sqlglot.executor as ex
    from sqlglot import exp, find_tables, parse_one, transpile
    from sqlglot.executor.table import Table

    schema = helpers.TPCDS_SCHEMA
    stubs = {name: Table(columns=columns, rows=[]) for name, columns in schema.items()}
    captured = []

    class Stop(Exception):
        pass

    class Capture:
        def __init__(self, env=None, tables=None):
            self.tables = tables

        def execute(self, plan):
            captured.append(plan)
            raise Stop

    real = ex.PythonExecutor
    ex.PythonExecutor = Capture

    kinds = {}

    def kind(c):
        k = kinds.get(c)
        if k is None:
            k = kinds[c] = ("e" if issubclass(c, exp.Expr) else "d" if issubclass(c, dict) else
                            "l" if issubclass(c, (list, tuple)) else "s" if issubclass(c, (set, frozenset)) else
                            "t" if issubclass(c, type) else "step" if hasattr(c, "dependencies") else "v")
        return k

    def ser(v, ref=None):
        k = kind(type(v))
        if k == "e":
            t = v.type
            # children must point back at this node (the executor reads .parent)
            linked = all(c.parent is v for c in v.iter_expressions())
            return [type(v).__name__, [[a, ser(x, ref)] for a, x in v.args.items()],
                    ser(t, ref) if t is not None and t is not v else None,
                    v.comments or None, ser(v.meta, ref) if v.meta else None, linked]
        if k == "v":
            return [type(v).__name__, repr(v)] if v is not None else None
        if k == "l":
            return ["l", [ser(x, ref) for x in v]]
        if k == "d":
            return ["d", [[ser(a, ref), ser(x, ref)] for a, x in v.items()]]
        if k == "s":
            return ["s", sorted((ser(x, ref) for x in v), key=json.dumps)]
        if k == "step":
            return ["step", ref(v) if ref else str(v.name)]
        return ["t", v.__module__, v.__qualname__]

    def ser_plan(plan):
        # steps are numbered in a DFS from the root that visits dependencies in
        # the order of their own content, never in set (memory address) order
        def own(s):
            return json.dumps([type(s).__name__, {k: ser(v) for k, v in sorted(vars(s).items())
                                                  if k not in ("dependencies", "dependents")}])

        index, stack = {}, [plan.root]
        while stack:
            s = stack.pop()
            if id(s) in index:
                continue
            index[id(s)] = len(index)
            stack.extend(sorted(s.dependencies, key=own, reverse=True))
        ordered = sorted(((i, s) for s in _all_steps(plan.root) for i in [index[id(s)]]), key=lambda x: x[0])
        ref = lambda s: index.get(id(s), -1)  # noqa: E731
        body = []
        for i, s in ordered:
            attrs = {k: (ser(v, ref) if k not in ("dependencies", "dependents") else sorted(ref(d) for d in v))
                     for k, v in sorted(vars(s).items())}
            body.append([i, type(s).__name__, attrs])
        # PythonExecutor reads the steps and plan.ctes, never plan.expression
        return [ser(plan.ctes), body]

    def _all_steps(root):
        seen, todo = {}, [root]
        while todo:
            s = todo.pop()
            if id(s) not in seen:
                seen[id(s)] = s
                todo.extend(s.dependencies)
        return list(seen.values())

    import ast
    import csv
    import gzip
    import hashlib
    import time

    def hh(v):
        return hashlib.sha256(json.dumps(v, separators=(",", ":")).encode()).hexdigest()[:24]

    data = {}

    def table(name):
        # exactly as TestExecutor.setUpClass loads the fixture data
        if name not in data:
            path = os.path.join(pinned, "tests", "fixtures", "optimizer", "tpc-ds", f"{name}.csv.gz")
            with gzip.open(path, "rt", newline="") as f:
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
            data[name] = Table(columns=schema[name], rows=rows)
        return data[name]

    def run_query(e2):
        """The worker's result for one query, with the real executor and data."""
        ex.PythonExecutor = real
        try:
            res = ex.execute(e2, schema=schema, tables={t.name: table(t.name) for t in find_tables(e2)})
        finally:
            ex.PythonExecutor = Capture
        return hh([list(res.columns), [repr(r) for r in res.rows]])

    mode = sys.argv[3] if len(sys.argv) > 3 else "fp"
    expect = rows_expect = None
    if mode not in ("fp", "rows"):
        expect = json.load(open(sys.argv[3]))
        rows_expect = json.load(open(sys.argv[4])) if len(sys.argv) > 4 else {}
    max_runs = int(os.environ.get("BR_PROBE_MAX_RUNS", "5"))
    max_secs = float(os.environ.get("BR_PROBE_MAX_SECS", "3"))
    out, pending = [], []
    for qi, (meta, sql, _) in enumerate(helpers.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")):
        rec = {}
        e2 = None
        try:
            # main process parses, the worker gets a pickled copy; for queries
            # without `# execute` the worker returns before using it
            e2 = pickle.loads(pickle.dumps(parse_one(sql)))
            rec["ok"] = True
            if meta.get("execute"):
                tables = {}
                for t in find_tables(e2):
                    tables[t.name] = stubs[t.name]
                rec["tables"] = sorted(tables)
                captured.clear()
                try:
                    ex.execute(e2, schema=schema, tables=tables)
                    rec["plan"] = "returned"
                except Stop:
                    rec["plan"] = ser_plan(captured[0])
                rec["duckdb"] = transpile(sql, write="duckdb")[0]
        except Exception as err:  # noqa: BLE001
            rec["error"] = f"{type(err).__name__}: {err}"
        fields = {k: hh(v) for k, v in rec.items()}
        if mode == "rows":
            if meta.get("execute"):
                t0 = time.process_time()
                h = run_query(e2)
                out.append([qi, h, round(time.process_time() - t0, 3)])
            continue
        out.append(fields)
        if expect is not None:
            want = expect[qi] if qi < len(expect) else None
            if fields == want:
                continue
            diff = {k for k in set(fields) | set(want or {}) if fields.get(k) != (want or {}).get(k)}
            # only the plan changed: settle it by running the query on the data
            est = rows_expect.get(str(qi), [None, 1e9])[1]
            if diff == {"plan"} and str(qi) in rows_expect and len(pending) < max_runs \
                    and sum(p[2] for p in pending) + est <= max_secs:
                pending.append((qi, e2, est))
                continue
            print(json.dumps({"differs": qi, "fields": sorted(diff)}))
            return
    if mode == "rows":
        json.dump({str(q): [h, t] for q, h, t in out}, sys.stdout)
        return
    if expect is not None:
        if len(out) != len(expect):
            print(json.dumps({"differs": len(out)}))
            return
        for qi, e2, _ in pending:
            try:
                same = run_query(e2) == rows_expect[str(qi)][0]
            except Exception:  # noqa: BLE001
                same = False
            if not same:
                print(json.dumps({"differs": qi, "fields": ["rows"]}))
                return
        print(json.dumps({"differs": None, "ran": [p[0] for p in pending]}))
        return
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
