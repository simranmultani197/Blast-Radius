"""Check the verifier against labels: run every candidate of each commit locally.

    python /repo/tools/verify_check.py /data [answers.json | mutant_dir] [max_ms]
"""
import glob
import json
import os
import sys
import time

sys.path.insert(0, "/repo")
from br import verify as V  # noqa: E402
from br.select import CovMap, analyze  # noqa: E402

data = sys.argv[1]
truth_src = sys.argv[2]
max_ms = int(sys.argv[3]) if len(sys.argv) > 3 else 5000
tests = json.load(open(f"{data}/tests.json"))["tests"]
by_num = {t["number"]: t for t in tests}
num_by_id = {t["id"]: t["number"] for t in tests}
cmap = CovMap("/repo/maps/map.json.gz")
to_num = [num_by_id[t] for t in cmap.tests]

if truth_src.endswith(".json"):
    ans = json.load(open(truth_src))["commits"]
    commits = {cid: (open(f"{data}/commits/{cid}.patch").read(), set(v["failing"])) for cid, v in ans.items()}
else:
    commits = {}
    for tf in sorted(glob.glob(f"{truth_src}/*.truth.json")):
        mid = os.path.basename(tf).split(".")[0]
        tr = json.load(open(tf))
        truth = {num_by_id[n] for n in tr["failing"] if n in num_by_id}
        for ce in tr.get("collect_errors", []):
            truth |= {num for tid, num in num_by_id.items() if tid.startswith(ce)}
        commits[mid] = (open(f"{truth_src}/{mid}.patch").read(), truth)

t0 = time.time()
V.preload(heavy=True)
print(f"preload {time.process_time():.2f}s cpu", flush=True)
bad = 0
total_cpu = 0
for cid, (patch, truth) in sorted(commits.items()):
    ch = analyze(patch, "/opt/sqlglot", cmap)
    cand = sorted({to_num[i] for i in ch.tests})
    rec = sum(by_num[n]["seconds"] for n in cand)
    if not cand or rec * 1000 > max_ms:
        print(f"{cid}: skip cand={len(cand)} rec={rec:.2f}s")
        continue
    nodeids = [by_num[n]["id"] for n in cand]
    slow = float(os.environ.get("BR_SLOW", "1"))
    budgets = {by_num[n]["id"]: slow * max(2.0, 5 * by_num[n]["seconds"] + 1) for n in cand}
    job = V.launch(cid, "/opt/sqlglot", ch.new_files, nodeids, budgets, wall_limit=60)
    fail, unknown, cpu = V.collect(job)
    total_cpu += cpu
    got = {num_by_id[n] for n in fail if n in num_by_id} | {num_by_id[n] for n in unknown if n in num_by_id}
    expect = truth & set(cand)
    ok = expect <= got  # extra (timeouts/unknown) only costs time; missing would be a miss
    bad += not ok
    print(f"{cid}: cand={len(cand)} rec={rec:.2f}s cpu={cpu:.2f}s fail={len(fail)} unknown={len(unknown)} "
          f"truth={len(expect)} {'OK' if ok else 'DIFF missing=' + str(sorted(expect - got)) + ' extra=' + str(sorted(got - expect))}",
          flush=True)
print(f"diffs={bad} total child cpu={total_cpu:.1f}s wall={time.time() - t0:.1f}s")
