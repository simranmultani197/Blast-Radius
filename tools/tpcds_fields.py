"""Per-field probe diff for patches the TPC-DS check does not drop (validation, dev image)."""
import glob, json, os, shutil, subprocess, sys
sys.path.insert(0, "/repo")
from concurrent.futures import ThreadPoolExecutor
from br import patch as P, tpcds_check as T
src, out, dirs = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1", BR_PROBE_FIELDS="1")
def run(root):
    r = subprocess.run([sys.executable, "-s", "/repo/br/tpcds_probe.py", root, src], capture_output=True, text=True, env=env, cwd="/tmp")
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout else None
pinned = run(src)
def why_guard(txt):
    keep = T._load("tpcds_lines.json")["lines"]
    for fp in P.parse(txt):
        p = fp.path
        if p.startswith("sqlglot/executor/"): return "executor dir"
        if not (p.endswith(".py") and p.startswith("sqlglot/")): return "non-sqlglot file"
        old = open(os.path.join(src, p)).read(); fi = T.A.FileIndex(old, T.A.parse(old)); hot = set(keep.get(p, ()))
        removed, ins = P.changed_old_lines(fp)
        for ln in removed:
            u = fi.smallest_unit(ln)
            if u is None or u.scope != "function": return "not in a function body"
            if isinstance(u.node, (T.ast.FunctionDef, T.ast.AsyncFunctionDef, T.ast.ClassDef, T.ast.Lambda)): return "def/signature"
            hit = [l for l in range(u.start, u.end + 1) if l in hot]
            if hit:
                ex = set(json.load(open("/w/tpcds_exec_lines.json"))["lines"].get(p, []))
                return "runs in executor" if any(l in ex for l in hit) else "runs at import"
        if ins: return "insertion"
    return "?"
def one(p):
    txt = open(p).read()
    if not T.guard(txt, src): return p, {"guard": why_guard(txt)}
    root = T._farm(src, {fp.path: P.apply(open(os.path.join(src, fp.old_path)).read(), fp) for fp in P.parse(txt)})
    try: got = run(root)
    finally: shutil.rmtree(root, ignore_errors=True)
    if got is None: return p, {"differs": {"crash": ["crash"]}}
    d = {}
    for i, (a, b) in enumerate(zip(pinned, got)):
        if a != b: d[i] = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return p, {"differs": d}
todo = [p for d in dirs for p in sorted(glob.glob(f"{d}/*.patch"))]
with ThreadPoolExecutor(5) as pool: res = dict(pool.map(one, todo))
json.dump(res, open(out, "w")); print("done", len(res))
