"""Check narrowed selections against candidate-set labels (truth ⊆ old candidates)."""
import glob, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from br.select import CovMap, analyze
from br.pack import pack
cm = CovMap(sys.argv[2] if len(sys.argv) > 2 else "maps/map.json.gz")
tests = json.load(open("sample/tests.json"))["tests"]; num = {t["id"]: t["number"] for t in tests}
ms = {t["number"]: round(t["seconds"]*1000) for t in tests}
to = [num[t] for t in cm.tests]
agg = {}
for d in sys.argv[1].split(","):
    for tf in sorted(glob.glob(f"{d}/*.truth.json")):
        tr = json.load(open(tf))
        if tr.get("failing") is None: continue
        mid = Path(tf).name.split(".")[0]
        truth = {num[n] for n in tr["failing"] if n in num and not n.startswith("tests/test_sqlglotc")}
        candf = f"{d}/{mid}.cands.json"
        old = {num[t] for t in json.load(open(candf))} if Path(candf).exists() else None
        ch = analyze(open(f"{d}/{mid}.patch").read(), ".work/sqlglot", cm)
        C = {to[i] for i in ch.tests}
        rule = next((n.split()[0] + " " + n.split()[1] for n in ch.notes if " rule " in n), "statement")
        a = agg.setdefault(rule, [0, 0, 0, 0.0, 0.0])
        a[0] += 1; a[1] += bool(truth); a[2] += bool(truth - C)
        a[3] += pack(sorted(old), ms)[1]/1000 if old is not None else 0; a[4] += pack(sorted(C), ms)[1]/1000
        if truth - C:
            print("MISS", mid, json.load(open(f"{d}/{mid}.meta.json"))["ops"], sorted(truth - C)[:5], ch.notes[:2])
for k, (n, f, m, a, b) in sorted(agg.items()):
    print(f"{k:16} n={n:3} failing={f:3} misses={m} cost {a:7.1f} -> {b:7.1f}")
