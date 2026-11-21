"""Validation mutants: swap the operator of a comparison that also runs at import (testing only).

    python3 tools/gen_cmpimport.py .work/sqlglot .work/cmut N SEED     # make candidates
    python3 tools/gen_cmpimport.py .work/sqlglot .work/cmut --keep K   # after the map rebuild
--keep keeps the first K mutants the import-outcome refinement narrows and
writes cands.json = old selection minus new (the only tests that can be missed).
"""
import ast, difflib, glob, json, os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import br.select as S
from br.select import CovMap, analyze

root, out = sys.argv[1], sys.argv[2]
cm = CovMap("maps/map.json.gz")
TXT = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Is: "is", ast.IsNot: "is not"}
SWAP = {ast.Eq: ["!="], ast.NotEq: ["=="], ast.Lt: ["<="], ast.LtE: ["<"], ast.Gt: [">="], ast.GtE: [">"],
        ast.Is: ["=="], ast.IsNot: ["!=", "is"]}

if sys.argv[3] == "--keep":
    k = int(sys.argv[4]); kept = 0
    for p in sorted(glob.glob(f"{out}/*.patch")):
        txt = open(p).read()
        S.CMP_IMPORT_RULE = False; old = set(analyze(txt, root, cm).tests)
        S.CMP_IMPORT_RULE = True; new = set(analyze(txt, root, cm).tests)
        if kept < k and new < old:
            json.dump(sorted(cm.tests[i] for i in old - new), open(p[:-6] + ".cands.json", "w"))
            kept += 1
        else:
            for f in glob.glob(p[:-6] + ".*"):
                os.remove(f)
    print("kept", kept)
    sys.exit()

n, seed = int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed); os.makedirs(out, exist_ok=True)
sites = []
for dp, _, fns in os.walk(os.path.join(root, "sqlglot")):
    for fn in fns:
        if not fn.endswith(".py"): continue
        rel = os.path.relpath(os.path.join(dp, fn), root)
        for c in ast.walk(ast.parse(open(os.path.join(root, rel)).read())):
            if isinstance(c, ast.Compare) and len(c.ops) == 1 and f"{rel}:{c.lineno}:{c.col_offset}" in cm.cmp_import \
                    and c.left.end_lineno == c.comparators[0].lineno == c.lineno and type(c.ops[0]) in SWAP:
                sites.append((rel, c))
rng.shuffle(sites)
made = 0
for rel, c in sites[:n]:
    src = open(os.path.join(root, rel)).read(); L = src.split("\n"); line = L[c.lineno - 1]
    b = line.encode(); a0, a1 = c.left.end_col_offset, c.comparators[0].col_offset
    mid_txt = b[a0:a1].decode(); op = TXT[type(c.ops[0])]
    if mid_txt.strip() != op: continue
    rep = rng.choice(SWAP[type(c.ops[0])])
    L[c.lineno - 1] = (b[:a0] + mid_txt.replace(op, rep, 1).encode() + b[a1:]).decode()
    d = "".join(difflib.unified_diff(src.splitlines(True), "\n".join(L).splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    mid = f"c{seed:02d}{made:04d}"
    open(f"{out}/{mid}.patch", "w").write(f"diff --git a/{rel} b/{rel}\nindex 1111111..2222222 100644\n" + d)
    json.dump({"kind": "code", "ops": [f"{rel}:{c.lineno}: {op} -> {rep} (import-evaluated)"]}, open(f"{out}/{mid}.meta.json", "w"))
    made += 1
print("import-evaluated single comparisons", len(sites), "made", made)
