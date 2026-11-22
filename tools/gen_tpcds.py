"""Validation mutants: sample-style edits on lines test_execute_tpcds runs (testing only).

    python3 tools/gen_tpcds.py .work/sqlglot .work/pmut N SEED
cands.json = [test_execute_tpcds] so labelling runs only that test.
"""
import io, json, os, random, sys, tokenize, difflib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br.select import CovMap, analyze
root, out, n, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
cm = CovMap("maps/map.json.gz")
TP = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
ti = cm.tests.index(TP)
SW = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">", "and": "or", "or": "and", "True": "False", "False": "True", "is": "=="}
rng = random.Random(seed); os.makedirs(out, exist_ok=True)
sites = []
for dp, _, fns in os.walk(os.path.join(root, "sqlglot")):
    for fn in fns:
        if not fn.endswith(".py"): continue
        rel = os.path.relpath(os.path.join(dp, fn), root)
        cov = cm.files.get(rel, {})
        src = open(os.path.join(root, rel)).read()
        for t in tokenize.generate_tokens(io.StringIO(src).readline):
            sid = cov.get(str(t.start[0]))
            if sid is None or ti not in cm.set(sid): continue
            s = t.string
            if s in SW and t.type in (tokenize.OP, tokenize.NAME): sites.append((rel, t.start, t.end, s, SW[s]))
            elif t.type == tokenize.NAME and s == "not": sites.append((rel, t.start, (t.end[0], t.end[1] + 1), s, ""))
            elif t.type == tokenize.NUMBER and s.isdigit():
                v = int(s); sites.append((rel, t.start, t.end, s, str(v + 1 if v > 1 else 1 - v)))
rng.shuffle(sites)
made = 0
for rel, (r, c0), (_, c1), s, rep in sites:
    if made >= n: break
    src = open(os.path.join(root, rel)).read(); L = src.split("\n"); line = L[r - 1]
    if s == "is" and line[c1:c1 + 5].startswith(" not "): c1 += 4; rep = "!="
    L[r - 1] = line[:c0] + rep + line[c1:]
    d = "".join(difflib.unified_diff(src.splitlines(True), "\n".join(L).splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    p = f"diff --git a/{rel} b/{rel}\nindex 1111111..2222222 100644\n" + d
    if ti not in analyze(p, root, cm).tests: continue
    mid = f"p{seed:02d}{made:04d}"
    open(f"{out}/{mid}.patch", "w").write(p)
    json.dump({"kind": "code", "ops": [f"{rel}:{r}: {s}->{rep or '(drop)'} | {line.strip()[:60]}"]}, open(f"{out}/{mid}.meta.json", "w"))
    json.dump([TP], open(f"{out}/{mid}.cands.json", "w"))
    made += 1
print("sites", len(sites), "made", made)
