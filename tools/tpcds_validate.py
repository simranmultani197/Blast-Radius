"""End-to-end: br.tpcds_check.droppable on labelled patches (dev image, validation).

    python /tools/tpcds_validate.py /src OUT.json DIR[,DIR...] [JOBS]
"""
import glob, json, os, sys
sys.path.insert(0, "/repo")
from concurrent.futures import ThreadPoolExecutor
from br import patch as P, tpcds_check as T
from br.select import CovMap

prof = CovMap("/repo/maps/map.json.gz").tpcds_process_profile

src, out, dirs = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 4


def one(p):
    txt = open(p).read()
    new = {}
    for fp in P.parse(txt):
        old = open(os.path.join(src, fp.old_path), encoding="utf-8", newline="").read() if fp.old_path else None
        new[fp.path] = P.apply(old, fp)
    g1 = T.guard(txt, src)
    gd = T._duckdb_module_guard(txt, new, src, prof)
    return p, {"guard": bool(g1), "gd": bool(gd), "drop": bool((g1 or gd) and T.unchanged(new, src, timeout=300))}


todo = [p for d in dirs for p in sorted(glob.glob(f"{d}/*.patch"))]
with ThreadPoolExecutor(jobs) as pool:
    res = dict(pool.map(one, todo))
json.dump(res, open(out, "w"))
print("checked", len(res), "guard ok", sum(v["guard"] for v in res.values()), "gd ok", sum(v["gd"] for v in res.values()),
      "droppable", sum(v["drop"] for v in res.values()))
