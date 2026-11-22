"""Validation mutants for the duckdb-SQL part of the TPC-DS check (testing only).

    python3 tools/gen_duck.py .work/sqlglot OUT N SEED [FILE,FILE...]

Edits on lines test_execute_tpcds runs in the given files (default: the duckdb
dialect): operator/constant swaps like tools/gen_tpcds.py, plus edits to string
literals of generated SQL (case of a keyword or function name, spacing), which
change the duckdb SQL without necessarily changing its result.
cands.json = [test_execute_tpcds] so labelling runs only that test.
"""
import difflib
import io
import json
import os
import random
import sys
import tokenize

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br.select import CovMap, analyze  # noqa: E402

root, out, n, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
files = sys.argv[5].split(",") if len(sys.argv) > 5 else ["sqlglot/dialects/duckdb.py"]
cm = CovMap("maps/map.json.gz")
TP = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
ti = cm.tests.index(TP)
SW = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">", "and": "or", "or": "and",
      "True": "False", "False": "True"}
rng = random.Random(seed)
os.makedirs(out, exist_ok=True)


def str_edits(s):
    """Alternative spellings of a SQL string literal token."""
    q = s[-1]
    if q not in "'\"" or s[0] not in "'\"fFrR" and not s.startswith(q):
        return []
    body_start = s.index(q)
    pre, body = s[:body_start + 1], s[body_start + 1:-1]
    if not body or "\\" in body or len(body) > 60:
        return []
    alts = []
    if any(c.isupper() for c in body) and "{" not in body:
        alts.append(pre + body.lower() + q)
    if " " in body:
        i = body.index(" ")
        alts.append(pre + body[:i] + "  " + body[i + 1:] + q)
    return alts


import ast  # noqa: E402

BODY_ONLY = os.environ.get("BR_GEN_BODY", "1") == "1"
sites = []
for rel in files:
    cov = cm.files.get(rel, {})
    src = open(os.path.join(root, rel)).read()
    body = set()
    for f in ast.walk(ast.parse(src)):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body.update(range(f.body[0].lineno, f.end_lineno + 1))
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        sid = cov.get(str(t.start[0]))
        if sid is None or ti not in cm.set(sid) or t.start[0] != t.end[0]:
            continue
        if BODY_ONLY and t.start[0] not in body:
            continue
        s = t.string
        if s in SW and t.type in (tokenize.OP, tokenize.NAME):
            sites.append((rel, t.start, t.end, s, SW[s]))
        elif t.type == tokenize.NUMBER and s.isdigit():
            v = int(s)
            sites.append((rel, t.start, t.end, s, str(v + 1 if v > 1 else 1 - v)))
        elif t.type == tokenize.STRING:
            for alt in str_edits(s):
                sites.append((rel, t.start, t.end, s, alt))
                sites.append((rel, t.start, t.end, s, alt))  # weight string edits up
rng.shuffle(sites)
made, seen = 0, set()
for rel, (r, c0), (_, c1), s, rep in sites:
    if made >= n:
        break
    if (rel, r, c0, rep) in seen:
        continue
    seen.add((rel, r, c0, rep))
    src = open(os.path.join(root, rel)).read()
    L = src.split("\n")
    line = L[r - 1]
    L[r - 1] = line[:c0] + rep + line[c1:]
    d = "".join(difflib.unified_diff(src.splitlines(True), "\n".join(L).splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    p = f"diff --git a/{rel} b/{rel}\nindex 1111111..2222222 100644\n" + d
    if ti not in analyze(p, root, cm).tests:
        continue
    mid = f"d{seed:02d}{made:04d}"
    open(f"{out}/{mid}.patch", "w").write(p)
    json.dump({"kind": "code", "ops": [f"{rel}:{r}: {s}->{rep} | {line.strip()[:60]}"]},
              open(f"{out}/{mid}.meta.json", "w"))
    json.dump([TP], open(f"{out}/{mid}.cands.json", "w"))
    made += 1
print("sites", len(sites), "made", made)
