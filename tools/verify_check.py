"""Check verifier verdicts against labels and measure child CPU.

    python /repo/tools/verify_check.py /data answers.json|mutdir [--cheap SECONDS] [--slow F]
"""
import glob, json, os, sys, time
sys.path.insert(0, "/repo")
sys.path.insert(0, "/repo/tools"); import verify as V
from br.select import CovMap, analyze

data, src = sys.argv[1], sys.argv[2]
cheap = float(sys.argv[sys.argv.index("--cheap") + 1]) if "--cheap" in sys.argv else 1e9
slow = float(sys.argv[sys.argv.index("--slow") + 1]) if "--slow" in sys.argv else 1.0
tests = json.load(open(f"{data}/tests.json"))["tests"]
by_num = {t["number"]: t for t in tests}
num = {t["id"]: t["number"] for t in tests}
cmap = CovMap("/repo/maps/map.json.gz")
to = [num[t] for t in cmap.tests]
commits = {}
if src.endswith(".json"):
    for cid, v in json.load(open(src))["commits"].items():
        commits[cid] = (open(f"{data}/commits/{cid}.patch").read(), set(v["failing"]))
else:
    for tf in sorted(glob.glob(f"{src}/*.truth.json")):
        tr = json.load(open(tf))
        if tr.get("failing") is None:
            continue
        mid = os.path.basename(tf).split(".")[0]
        truth = {num[n] for n in tr["failing"] if n in num and not n.startswith("tests/test_sqlglotc")}
        commits[mid] = (open(f"{src}/{mid}.patch").read(), truth)
V.preload(heavy=True)
print(f"preload cpu {time.process_time():.2f}s", flush=True)
bad = 0
for cid, (patch, truth) in sorted(commits.items()):
    ch = analyze(patch, "/opt/sqlglot", cmap)
    cand = sorted({to[i] for i in ch.tests})
    run = [n for n in cand if by_num[n]["seconds"] < cheap]
    if not run:
        continue
    nodeids = [by_num[n]["id"] for n in run]
    budgets = {by_num[n]["id"]: slow * max(2.0, 5 * by_num[n]["seconds"] + 1) for n in run}
    t0 = time.time()
    verdict, passed, cpu = V.run(cid, "/opt/sqlglot", ch.new_files, nodeids, budgets, wall_limit=120 * slow)
    expect = "fail" if truth & set(run) else "pass"
    ok = verdict == expect or (verdict in ("fail", "unknown") and expect == "fail") or verdict == "unknown"
    danger = verdict == "pass" and expect == "fail"
    bad += danger
    print(f"{cid}: ran={len(run)} rec={sum(by_num[n]['seconds'] for n in run):6.2f}s cpu={cpu:6.2f}s "
          f"wall={time.time()-t0:5.1f}s verdict={verdict:7} truth={expect} {'DANGER' if danger else ('ok' if verdict == expect else 'costly')}",
          flush=True)
print(f"dangerous={bad}")
