"""Per expensive test: how often selected by current rules, how often it actually fails."""
import glob, json, sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from br.select import CovMap, analyze
cm = CovMap("maps/map.json.gz")
tests = json.load(open("sample/tests.json"))["tests"]; num = {t["id"]: t["number"] for t in tests}
sec = {t["number"]: t["seconds"] for t in tests}; tid = {t["number"]: t["id"] for t in tests}
to = [num[t] for t in cm.tests]
EXP = [n for n in sec if sec[n] >= 1.0]
sel = collections.Counter(); fail = collections.Counter(); onlyfail = collections.Counter()
commits = 0
def handle(patch, truth, src):
    global commits
    commits += 1
    ch = analyze(patch, ".work/sqlglot", cm)
    C = {to[i] for i in ch.tests}
    cheap_fail = bool({t for t in truth & C if sec[t] < 1.0})
    for e in EXP:
        if e in C:
            sel[(e, src)] += 1
            if e in truth:
                fail[(e, src)] += 1
                if not cheap_fail: onlyfail[(e, src)] += 1
for cid, v in json.load(open("sample/answers.json"))["commits"].items():
    handle(open(f"sample/commits/{cid}.patch").read(), set(v["failing"]), "sample")
for d in (".work/mutants", ".work/tmut", ".work/smut", ".work/dmut", ".work/kmut", ".work/bmut"):
    for tf in glob.glob(f"{d}/*.truth.json"):
        tr = json.load(open(tf))
        if tr.get("failing") is None: continue
        mid = Path(tf).name.split(".")[0]
        if mid in ("m010032", "m010034"): continue
        truth = {num[n] for n in tr["failing"] if n in num and not n.startswith("tests/test_sqlglotc")}
        handle(open(f"{d}/{mid}.patch").read(), truth, "targ" if d.endswith("tmut") else "other")
print("commits", commits)
for e in sorted(EXP, key=lambda n: -sec[n]):
    s = {src: sel[(e, src)] for src in ("sample", "other", "targ")}
    f = {src: fail[(e, src)] for src in ("sample", "other", "targ")}
    o = {src: onlyfail[(e, src)] for src in ("sample", "other", "targ")}
    if sum(s.values()):
        print(f"{sec[e]:6.2f}s {tid[e][:70]:70} selected={s} failed={f} only-expensive-failed={o}")
