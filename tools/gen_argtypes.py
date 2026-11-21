"""Validation mutants: flip one bool value in a class-level arg_types dict (testing only).

    python3 tools/gen_argtypes.py .work/sqlglot .work/amut N SEED
Half the sites are drawn from classes the class rule makes expensive (>= 5 s).
cands.json is filled later by `--cands`: old selection minus new selection,
i.e. exactly the tests the arg_types rule drops (a miss can only be among them).
"""
import ast, difflib, json, os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import br.select as S
from br.select import CovMap, analyze
from br.pack import pack

root, out = sys.argv[1], sys.argv[2]
cm = CovMap("maps/map.json.gz")
tests = json.load(open("sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}; ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
to = [num[t] for t in cm.tests]

if sys.argv[3] == "--cands":
    import glob
    for p in sorted(glob.glob(f"{out}/*.patch")):
        txt = open(p).read()
        S.ARGTYPES_RULE = False
        old = set(analyze(txt, root, cm).tests)
        S.ARGTYPES_RULE = True
        new = set(analyze(txt, root, cm).tests)
        json.dump(sorted(cm.tests[i] for i in old - new), open(p[:-6] + ".cands.json", "w"))
        print(os.path.basename(p), "old", len(old), "new", len(new), "dropped", len(old - new))
    sys.exit()

n, seed = int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed); os.makedirs(out, exist_ok=True)
sites = []
for dp, _, fns in os.walk(os.path.join(root, "sqlglot")):
    for fn in fns:
        if not fn.endswith(".py"): continue
        rel = os.path.relpath(os.path.join(dp, fn), root)
        for st in ast.walk(ast.parse(open(os.path.join(root, rel)).read())):
            if isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(st.value, ast.Dict):
                tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                if any(isinstance(t, ast.Name) and t.id == "arg_types" for t in tg):
                    for v in st.value.values:
                        if isinstance(v, ast.Constant) and isinstance(v.value, bool):
                            sites.append((rel, v.lineno, v.col_offset, v.value))
rng.shuffle(sites)
S.ARGTYPES_RULE = False
made, cheap = 0, 0
for rel, ln, col, val in sites:
    if made >= n: break
    src = open(os.path.join(root, rel)).read(); L = src.split("\n"); s = L[ln - 1]
    L[ln - 1] = s[:col] + str(not val) + s[col + len(str(val)):]
    d = "".join(difflib.unified_diff(src.splitlines(True), "\n".join(L).splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    p = f"diff --git a/{rel} b/{rel}\nindex 1111111..2222222 100644\n" + d
    cost = pack(sorted({to[i] for i in analyze(p, root, cm).tests}), ms)[1]
    if cost < 5000:
        if cheap >= n // 2: continue
        cheap += 1
    mid = f"a{seed:02d}{made:04d}"
    open(f"{out}/{mid}.patch", "w").write(p)
    json.dump({"kind": "code", "ops": [f"{rel}:{ln}: arg_types {s.strip()[:50]} {val}->{not val}"]}, open(f"{out}/{mid}.meta.json", "w"))
    made += 1
print("sites", len(sites), "made", made)
