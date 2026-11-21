"""Validation mutants for the compare and module rules (testing only).
    python3 tools/gen_special.py .work/sqlglot OUT N_CMP N_MOD SEED
"""
import ast, difflib, json, os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br.select import CovMap, analyze

root, out, ncmp, nmod, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
rng = random.Random(seed)
os.makedirs(out, exist_ok=True)
cm = CovMap("maps/map.json.gz")
SYM = {"Eq": "==", "NotEq": "!=", "Is": "is", "IsNot": "is not", "Lt": "<", "LtE": "<=", "Gt": ">", "GtE": ">="}
SWAP = {"Is": ["Eq"], "IsNot": ["NotEq"], "Lt": ["LtE"], "LtE": ["Lt"], "Gt": ["GtE"], "GtE": ["Gt"],
        "Eq": ["Is", "NotEq"], "NotEq": ["IsNot", "Eq"]}

def patch_for(path, old, new):
    d = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}", n=3))
    return f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n" + d

sites, mods = [], []
for dp, _, fns in os.walk(os.path.join(root, "sqlglot")):
    for fn in fns:
        if not fn.endswith(".py"): continue
        rel = os.path.relpath(os.path.join(dp, fn), root)
        src = open(os.path.join(root, rel)).read()
        tree = ast.parse(src)
        covered = cm.files.get(rel, {})
        for n in ast.walk(tree):
            if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]).__name__ in SWAP \
                    and n.lineno == n.end_lineno and str(n.lineno) in covered:
                sites.append((rel, n))
        for st in tree.body:
            if isinstance(st, (ast.Assign, ast.AnnAssign)) and str(st.lineno) in covered:
                for c in ast.walk(st):
                    if isinstance(c, ast.Constant) and c.lineno == c.end_lineno and (
                            isinstance(c.value, (int, str)) and not isinstance(c.value, bool) or isinstance(c.value, bool)):
                        mods.append((rel, c))

def emit(mid, rel, old, new, kind, op):
    open(f"{out}/{mid}.patch", "w").write(patch_for(rel, old, new))
    json.dump({"kind": kind, "ops": [f"{rel}: {op}"]}, open(f"{out}/{mid}.meta.json", "w"))
    nocmp = CovMap.__new__(CovMap); nocmp.__dict__.update(cm.__dict__); nocmp.cmp = None; nocmp.du = None
    ch = analyze(open(f"{out}/{mid}.patch").read(), root, nocmp)
    json.dump(sorted(cm.tests[i] for i in ch.tests), open(f"{out}/{mid}.cands.json", "w"))

rng.shuffle(sites)
made = 0
for rel, n in sites:
    if made >= ncmp: break
    src = open(os.path.join(root, rel)).read()
    lines = src.split("\n")
    line = lines[n.lineno - 1]
    a0 = n.left.end_col_offset; b0 = n.comparators[0].col_offset
    seg = line.encode()[a0:b0].decode()
    old = type(n.ops[0]).__name__
    new = rng.choice(SWAP[old])
    if SYM[old] not in seg: continue
    newseg = seg.replace(SYM[old], SYM[new], 1) if old not in ("Is",) else seg.replace(" is ", " == ", 1) if new == "Eq" else seg
    if old == "IsNot": newseg = seg.replace("is not", "!=", 1)
    if old in ("Eq", "NotEq") and new in ("Is", "IsNot"):
        newseg = seg.replace(SYM[old], SYM[new], 1)
        if not newseg.startswith(" "): newseg = " " + newseg.strip() + " "
    b = line.encode()
    nl = (b[:a0] + newseg.encode() + b[b0:]).decode()
    if nl == line: continue
    lines[n.lineno - 1] = nl
    newsrc = "\n".join(lines)
    try: ast.parse(newsrc)
    except SyntaxError: continue
    emit(f"c{seed:02d}{made:04d}", rel, src, newsrc, "code", f"{old}->{new}")
    made += 1
rng.shuffle(mods)
m2 = 0
for rel, c in mods:
    if m2 >= nmod: break
    src = open(os.path.join(root, rel)).read()
    lines = src.split("\n"); line = lines[c.lineno - 1].encode()
    tok = line[c.col_offset:c.end_col_offset].decode()
    if isinstance(c.value, bool): rep = "False" if c.value else "True"
    elif isinstance(c.value, int): rep = str(c.value + 1)
    else:
        if tok[:1] not in "'\"" or len(tok) < 3: continue
        rep = tok[0] + tok[1:-1] + "x" + tok[-1]
    lines[c.lineno - 1] = (line[:c.col_offset] + rep.encode() + line[c.end_col_offset:]).decode()
    newsrc = "\n".join(lines)
    try: ast.parse(newsrc)
    except SyntaxError: continue
    emit(f"d{seed:02d}{m2:04d}", rel, src, newsrc, "code", f"module const {tok}->{rep}")
    m2 += 1
print(f"{len(sites)} compare sites, {len(mods)} module constants; made {made} + {m2}")
