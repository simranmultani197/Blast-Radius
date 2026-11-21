"""How often do expensive candidates fail while every cheap candidate passes?"""
import glob, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from br.select import CovMap, analyze
T = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
cmap = CovMap("maps/map.json.gz")
tests = json.load(open("sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}
sec = {t["number"]: t["seconds"] for t in tests}
to = [num[t] for t in cmap.tests]
rows = []
def handle(name, patch, truth):
    ch = analyze(patch, ".work/sqlglot", cmap)
    cand = {to[i] for i in ch.tests}
    exp = {t for t in cand if sec[t] >= T}
    if not exp: return
    cheap = cand - exp
    rows.append((name, len(cheap), bool(truth & cheap), sorted(truth & exp), sorted(exp)))
ans = json.load(open("sample/answers.json"))["commits"]
for cid, v in ans.items():
    handle("S" + cid, open(f"sample/commits/{cid}.patch").read(), set(v["failing"]))
for d in sys.argv[1:3]:
    for tf in glob.glob(f"{d}/*.truth.json"):
        mid = Path(tf).name.split(".")[0]
        tr = json.load(open(tf))
        if tr.get("failing") is None: continue
        truth = {num[n] for n in tr["failing"] if n in num and not n.startswith("tests/test_sqlglotc")}
        for ce in tr.get("collect_errors", []):
            truth |= {n for i, n in num.items() if i.startswith(ce)}
        handle(mid, open(f"{d}/{mid}.patch").read(), truth)
bad = [r for r in rows if not r[2] and r[3]]
print(f"commits with expensive candidates: {len(rows)}; cheap-all-pass: {sum(1 for r in rows if not r[2])}; "
      f"of those, expensive failed: {len(bad)}")
for r in bad: print("  EXPENSIVE-ONLY", r)
for r in rows:
    if r[2] is False and not r[3]: print("  equiv", r[0], "cheap", r[1], "exp", r[4])
