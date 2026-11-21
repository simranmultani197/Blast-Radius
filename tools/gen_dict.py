"""Validation mutants editing one entry of class/module-level dict literals (testing only)."""
import ast, difflib, json, os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br.select import CovMap, analyze
root, out, n, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed); os.makedirs(out, exist_ok=True)
cm = CovMap("maps/map.json.gz")
assert cm.dkeys is None, "generate candidates with a map that has no dict data"
cands = []
for dp, _, fns in os.walk(os.path.join(root, "sqlglot")):
    for fn in fns:
        if not fn.endswith(".py"): continue
        rel = os.path.relpath(os.path.join(dp, fn), root)
        tree = ast.parse(open(os.path.join(root, rel)).read())
        covered = cm.files.get(rel, {})
        def scan(body):
            for st in body:
                if isinstance(st, ast.ClassDef): scan(st.body)
                elif isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(st.value, ast.Dict) and str(st.lineno) in covered:
                    for k, v in zip(st.value.keys, st.value.values):
                        if k is None: continue
                        for part, node in (("key", k), ("value", v)):
                            for c in ast.walk(node):
                                if isinstance(c, ast.Constant) and c.lineno == c.end_lineno and not isinstance(c, ast.Lambda):
                                    cands.append((rel, c, part))
        scan(tree.body)
rng.shuffle(cands)
made = 0
for rel, c, part in cands:
    if made >= n: break
    if part == "key" and rng.random() < 0.6: continue
    src = open(os.path.join(root, rel)).read(); lines = src.split("\n"); line = lines[c.lineno-1].encode()
    tok = line[c.col_offset:c.end_col_offset].decode()
    if isinstance(c.value, bool): rep = "False" if c.value else "True"
    elif isinstance(c.value, int): rep = str(c.value + 1)
    elif isinstance(c.value, str) and tok[:1] in "'\"" and len(tok) >= 2: rep = tok[0] + tok[1:-1] + "x" + tok[-1]
    else: continue
    lines[c.lineno-1] = (line[:c.col_offset] + rep.encode() + line[c.end_col_offset:]).decode()
    new = "\n".join(lines)
    try: ast.parse(new)
    except SyntaxError: continue
    d = "".join(difflib.unified_diff(src.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    mid = f"k{seed:02d}{made:04d}"
    open(f"{out}/{mid}.patch", "w").write(f"diff --git a/{rel} b/{rel}\nindex 1111111..2222222 100644\n" + d)
    json.dump({"kind": "code", "ops": [f"{rel}: dict {part} {tok}->{rep}"]}, open(f"{out}/{mid}.meta.json", "w"))
    ch = analyze(open(f"{out}/{mid}.patch").read(), root, cm)
    json.dump(sorted(cm.tests[i] for i in ch.tests), open(f"{out}/{mid}.cands.json", "w"))
    made += 1
print(len(cands), "dict constants; made", made)
