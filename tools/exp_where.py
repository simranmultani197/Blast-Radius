import glob, json, sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from br.select import CovMap, analyze
from br import patch as P
cm = CovMap("maps/map.json.gz")
tests = json.load(open("sample/tests.json"))["tests"]; num = {t["id"]: t["number"] for t in tests}
to = [num[t] for t in cm.tests]
WATCH = {1106: "exec_tpcds", 1544: "?", }
WATCH = {num["tests/test_executor.py::TestExecutor::test_execute_tpcds"]: "exec_tpcds",
         num["tests/test_parser.py::TestParser::test_max_nodes"]: "max_nodes",
         num["tests/test_generator.py::TestGenerator::test_generator_memory"]: "gen_memory",
         num["tests/test_executor.py::TestExecutor::test_execute_tpch"]: "exec_tpch",
         num["tests/test_executor.py::TestExecutor::test_optimized_tpch"]: "opt_tpch"}
OPT_TPCDS = num["tests/test_optimizer.py::TestOptimizer::test_tpcds"]
agg = collections.defaultdict(lambda: [0, 0])
rows = []
def handle(name, patch, truth):
    ch = analyze(patch, ".work/sqlglot", cm)
    C = {to[i] for i in ch.tests}
    files = [fp.path for fp in P.parse(patch) if fp.path.endswith(".py")]
    area = "/".join(files[0].split("/")[:2]) if files else "data"
    if area.startswith("sqlglot/") and area.endswith(".py"): area = files[0]
    for t, nm in WATCH.items():
        if t in C:
            a = agg[(nm, area)]; a[0] += 1; a[1] += t in truth
            if t in truth: rows.append((nm, name, area, OPT_TPCDS in truth, len(truth)))
for cid, v in json.load(open("sample/answers.json"))["commits"].items():
    handle("S" + cid, open(f"sample/commits/{cid}.patch").read(), set(v["failing"]))
for d in (".work/mutants", ".work/tmut", ".work/smut", ".work/dmut", ".work/kmut", ".work/bmut"):
    for tf in glob.glob(f"{d}/*.truth.json"):
        tr = json.load(open(tf))
        if tr.get("failing") is None: continue
        mid = Path(tf).name.split(".")[0]
        if mid in ("m010032", "m010034"): continue
        truth = {num[n] for n in tr["failing"] if n in num}
        handle(mid, open(f"{d}/{mid}.patch").read(), truth)
for (nm, area), (s, f) in sorted(agg.items()):
    print(f"{nm:11} {area:45} selected={s:3} failed={f}")
print()
for r in rows: print("FAIL", r)
