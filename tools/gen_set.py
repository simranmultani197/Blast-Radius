"""Validation mutants: swap one member of a class/module-level set display (testing only)."""
import ast, difflib, json, os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br.select import CovMap, analyze
root, out, n, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed); os.makedirs(out, exist_ok=True)
cm = CovMap("maps/map.json.gz")
src_tok = open(os.path.join(root, "sqlglot/tokenizer_core.py")).read()
TT = sorted({n.targets[0].id for c in ast.walk(ast.parse(src_tok)) if isinstance(c, ast.ClassDef) and c.name == "TokenType"
             for n in c.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)})
cands = []
for dp, _, fns in os.walk(os.path.join(root, "sqlglot")):
    for fn in fns:
        if not fn.endswith(".py"): continue
        rel = os.path.relpath(os.path.join(dp, fn), root)
        tree = ast.parse(open(os.path.join(root, rel)).read()); cov = cm.files.get(rel, {})
        def scan(body):
            for st in body:
                if isinstance(st, ast.ClassDef): scan(st.body)
                elif isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value is not None and str(st.lineno) in cov:
                    for s_ in ast.walk(st.value):
                        if isinstance(s_, ast.Set):
                            for e in s_.elts:
                                if isinstance(e, ast.Attribute) and e.lineno == e.end_lineno:
                                    cands.append((rel, e))
        scan(tree.body)
rng.shuffle(cands)
made = 0
for rel, e in cands:
    if made >= n: break
    src = open(os.path.join(root, rel)).read(); lines = src.split("\n"); b = lines[e.lineno - 1].encode()
    seg = b[e.col_offset:e.end_col_offset].decode()
    if seg.split(".")[-2:-1] == ["TokenType"] or seg.startswith("TokenType."):
        rep = seg.rsplit(".", 1)[0] + "." + rng.choice(TT)
    else:
        continue
    if rep == seg: continue
    lines[e.lineno - 1] = (b[:e.col_offset] + rep.encode() + b[e.end_col_offset:]).decode()
    new = "\n".join(lines)
    try: ast.parse(new)
    except SyntaxError: continue
    d = "".join(difflib.unified_diff(src.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    mid = f"t{seed:02d}{made:04d}"
    open(f"{out}/{mid}.patch", "w").write(f"diff --git a/{rel} b/{rel}\nindex 1111111..2222222 100644\n" + d)
    json.dump({"kind": "code", "ops": [f"{rel}: set member {seg}->{rep}"]}, open(f"{out}/{mid}.meta.json", "w"))
    ch = analyze(open(f"{out}/{mid}.patch").read(), root, cm)
    json.dump(sorted(cm.tests[i] for i in ch.tests), open(f"{out}/{mid}.cands.json", "w"))
    made += 1
print(len(cands), "set members; made", made)
