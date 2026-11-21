"""Validation mutants for the short-circuit rules (testing only)."""
import ast, difflib, json, os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br.select import CovMap, analyze
from br import analysis as A
root, out, n, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed); os.makedirs(out, exist_ok=True)
cm = CovMap("maps/map.json.gz")
base = CovMap.__new__(CovMap); base.__dict__.update(cm.__dict__); base.ev = None; base.cmp = None
cands = []
for dp, _, fns in os.walk(os.path.join(root, "sqlglot")):
    for fn in fns:
        if not fn.endswith(".py"): continue
        rel = os.path.relpath(os.path.join(dp, fn), root)
        tree = ast.parse(open(os.path.join(root, rel)).read()); cov = cm.files.get(rel, {})
        parents = {}
        for p in ast.walk(tree):
            for c in ast.iter_child_nodes(p): parents[id(c)] = p
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and len(node.values) == 2 and str(node.lineno) in cov \
                    and node.lineno == node.end_lineno and A.in_bool_context(node, parents.get(id(node))):
                cands.append(("swap", rel, node))
            if isinstance(node, (ast.BoolOp, ast.IfExp)) and str(node.lineno) in cov:
                ops = node.values[1:] if isinstance(node, ast.BoolOp) else [node.body, node.orelse]
                for o in ops:
                    for c in ast.walk(o):
                        if isinstance(c, ast.UnaryOp) and isinstance(c.op, ast.Not) and c.lineno == c.end_lineno:
                            cands.append(("dropnot", rel, c))
                        elif isinstance(c, ast.Constant) and isinstance(c.value, (bool, int)) and c.lineno == c.end_lineno:
                            cands.append(("const", rel, c))
rng.shuffle(cands)
made = 0
for kind, rel, node in cands:
    if made >= n: break
    src = open(os.path.join(root, rel)).read(); lines = src.split("\n"); b = lines[node.lineno - 1].encode()
    seg = b[node.col_offset:node.end_col_offset].decode()
    if kind == "swap":
        a0 = node.values[0].end_col_offset - node.col_offset; b0 = node.values[1].col_offset - node.col_offset
        mid = seg[a0:b0]; old = "and" if isinstance(node.op, ast.And) else "or"; new = "or" if old == "and" else "and"
        if old not in mid: continue
        rep = seg[:a0] + mid.replace(old, new, 1) + seg[b0:]
    elif kind == "dropnot":
        if not seg.startswith("not "): continue
        rep = seg[4:]
    else:
        v = node.value
        rep = ("False" if v else "True") if isinstance(v, bool) else str(v + 1)
    lines[node.lineno - 1] = (b[:node.col_offset] + rep.encode() + b[node.end_col_offset:]).decode()
    new_src = "\n".join(lines)
    try: ast.parse(new_src)
    except SyntaxError: continue
    d = "".join(difflib.unified_diff(src.splitlines(True), new_src.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    mid_ = f"b{seed:02d}{made:04d}"
    open(f"{out}/{mid_}.patch", "w").write(f"diff --git a/{rel} b/{rel}\nindex 1111111..2222222 100644\n" + d)
    json.dump({"kind": "code", "ops": [f"{rel}: {kind} {seg[:40]!r}"]}, open(f"{out}/{mid_}.meta.json", "w"))
    ch = analyze(open(f"{out}/{mid_}.patch").read(), root, base)
    json.dump(sorted(cm.tests[i] for i in ch.tests), open(f"{out}/{mid_}.cands.json", "w"))
    made += 1
import collections
print(len(cands), "sites", collections.Counter(k for k, _, _ in cands), "made", made)
