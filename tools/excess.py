import glob, json, sys, collections, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from br.select import CovMap, analyze
from br.pack import pack
cm = CovMap("maps/map.json.gz")
tests = json.load(open("sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}; ms = {t["number"]: round(t["seconds"]*1000) for t in tests}
to = [num[t] for t in cm.tests]
P = lambda s: pack(sorted(s), ms)[1]/1000
agg = collections.defaultdict(lambda: [0, 0.0, 0.0])
def add(name, patch, truth):
    ch = analyze(patch, ".work/sqlglot", cm)
    C = {to[i] for i in ch.tests}
    reasons = set(ch.tests.values())
    cat = set()
    for r in reasons:
        if "attr " in r: cat.add("class attr rule")
        elif "class " in r: cat.add("class rule")
        elif "importers" in r or "near" in r or "unplaced" in r: cat.add("module/importers")
        elif "lambda" in r: cat.add("lambda")
        elif "reads " in r or "lists " in r: cat.add("fixture")
        else: cat.add("function stmt")
    key = "+".join(sorted(cat)) or "empty"
    a = agg[key]; a[0] += 1; a[1] += P(C); a[2] += P(truth & C)
    if P(C) - P(truth & C) > 6: print(f"{name:10} {key:30} cost={P(C):5.1f} oracle={P(truth&C):5.1f} cand={len(C)}")
for cid, v in json.load(open("sample/answers.json"))["commits"].items():
    add("S"+cid, open(f"sample/commits/{cid}.patch").read(), set(v["failing"]))
for tf in glob.glob(".work/mutants/*.truth.json"):
    tr = json.load(open(tf))
    if tr.get("failing") is None: continue
    mid = Path(tf).name.split(".")[0]
    add(mid, open(f".work/mutants/{mid}.patch").read(), {num[n] for n in tr["failing"] if n in num})
print()
for k, (n, c, o) in sorted(agg.items(), key=lambda x: -(x[1][1]-x[1][2])):
    print(f"{k:40} n={n:3} cost={c:6.1f} oracle={o:6.1f} excess={c-o:6.1f}")
