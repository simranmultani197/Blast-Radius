"""Run br/tpcds_probe.py on patched copies of the tree (validation, dev image).

    python /tools/tpcds_probe_batch.py /src OUT.json DIR[,DIR...] [JOBS]
For every <id>.patch in the dirs: build a symlink farm with the patch applied,
probe it, and record which TPC-DS queries' fingerprints differ from the pinned
tree. Writes {dir/id: {"differs": [...query indexes], "error": ...}}.
"""
import glob, json, os, shutil, subprocess, sys, tempfile
sys.path.insert(0, "/repo")
from br import patch as P

src, out_path, dirs = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 4
env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")


def probe(root):
    r = subprocess.run([sys.executable, "-s", "/repo/br/tpcds_probe.py", root, src], capture_output=True, text=True,
                       env=env, timeout=300, cwd="/tmp")
    if r.returncode:
        return None, r.stderr[-300:]
    return json.loads(r.stdout), None


def farm(patch_text):
    root = tempfile.mkdtemp(prefix="farm", dir="/tmp")
    for dp, dns, fns in os.walk(os.path.join(src, "sqlglot")):
        dns[:] = [d for d in dns if d != "__pycache__"]
        rel = os.path.relpath(dp, src)
        os.makedirs(os.path.join(root, rel), exist_ok=True)
        for fn in fns:
            os.symlink(os.path.join(dp, fn), os.path.join(root, rel, fn))
    for fp in P.parse(patch_text):
        if not fp.path.startswith("sqlglot/"):
            continue
        old = open(os.path.join(src, fp.old_path), encoding="utf-8", newline="").read() if fp.old_path else None
        new = P.apply(old, fp)
        dst = os.path.join(root, fp.path)
        if os.path.lexists(dst):
            os.remove(dst)
        if new is not None:
            open(dst, "w", encoding="utf-8", newline="").write(new)
    return root


pinned, err = probe(src)
assert pinned, err
todo = [p for d in dirs for p in sorted(glob.glob(f"{d}/*.patch"))]
results = json.load(open(out_path)) if os.path.exists(out_path) else {}
todo = [p for p in todo if p not in results]
from concurrent.futures import ThreadPoolExecutor


def one(p):
    root = farm(open(p).read())
    try:
        got, err = probe(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if got is None:
        return p, {"error": err}
    return p, {"differs": [i for i, (a, b) in enumerate(zip(pinned, got)) if a != b] + (["len"] if len(got) != len(pinned) else [])}


with ThreadPoolExecutor(jobs) as pool:
    for i, (p, r) in enumerate(pool.map(one, todo)):
        results[p] = r
        if i % 10 == 9:
            json.dump(results, open(out_path, "w"))
            print(i + 1, "done", flush=True)
json.dump(results, open(out_path, "w"))
print("all done", len(results))
