"""Per-test CPU when verified inline (clean tree), plus per-file import overhead.
    python /repo/tools/measure_verify.py /data /out/vcost.json
"""
import collections, json, sys, time
sys.path.insert(0, "/repo")
sys.path.insert(0, "/repo/tools"); import verify as V
tests = json.load(open(f"{sys.argv[1]}/tests.json"))["tests"]
by_file = collections.defaultdict(list)
for t in tests:
    if t["seconds"] < 2.0:
        by_file[t["file"]].append(t["id"])
V.preload(heavy=True)
out = {"tests": {}, "files": {}}
for f, ids in sorted(by_file.items()):
    budgets = {i: 300.0 for i in ids}
    job = V.launch("m", "/opt/sqlglot", {}, ids, budgets, wall_limit=900)
    while not V.poll(job, 0.5):
        pass
    verdict, passed, cpu = V.finish(job)
    per = {e[1]: e[2] for e in job.events if e[0] == "pass"}
    setup = [e[2] for e in job.events if e[0] == "setup"]
    tot = sum(per.values())
    out["files"][f] = {"total_cpu": round(cpu, 3), "tests_cpu": round(tot, 3), "import_cpu": setup[0] if setup else None, "verdict": verdict}
    out["tests"].update(per)
    print(f"{f}: verdict={verdict} n={len(ids)} cpu={cpu:.2f} tests={tot:.2f} import={setup}", flush=True)
json.dump(out, open(sys.argv[2], "w"))
