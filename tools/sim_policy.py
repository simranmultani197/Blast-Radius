"""Replay labelled commits under verification policies (offline)."""
import glob, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from br.select import CovMap, analyze
from br.pack import pack

cm = CovMap("maps/map.json.gz")
tests = json.load(open("sample/tests.json"))["tests"]
by = {t["number"]: t for t in tests}
num = {t["id"]: t["number"] for t in tests}
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
to = [num[t] for t in cm.tests]
vc = json.load(open("maps/vcost.json"))
SCALE = 1.35
HEAVY = {"tests/test_optimizer.py", "tests/test_executor.py"}

def est_cpu(ts):
    if not ts: return 0.0
    files = {by[n]["file"] for n in ts}
    return SCALE * (vc["base"] + sum(vc["files"].get(f, .3) for f in files) + sum(vc["tests"][by[n]["id"]] for n in ts))

def P(ts): return pack(sorted(ts), ms)[1] / 1000

rows = []
def add(name, patch, truth, src):
    ch = analyze(patch, ".work/sqlglot", cm)
    C = {to[i] for i in ch.tests}
    data_only = all(not p.endswith(".py") for p in ch.new_files)
    rows.append((name, src, C, truth & C, truth - C, data_only, ch))
for cid, v in json.load(open("sample/answers.json"))["commits"].items():
    add("S" + cid, open(f"sample/commits/{cid}.patch").read(), set(v["failing"]), "sample")
for d, src in ((".work/mutants", "rand"), (".work/tmut", "targ")):
    for tf in glob.glob(f"{d}/*.truth.json"):
        tr = json.load(open(tf))
        if tr.get("failing") is None: continue
        mid = Path(tf).name.split(".")[0]
        truth = {num[n] for n in tr["failing"] if n in num and not n.startswith("tests/test_sqlglotc")}
        for ce in tr.get("collect_errors", []):
            truth |= {n for i, n in num.items() if i.startswith(ce)}
        add(mid, open(f"{d}/{mid}.patch").read(), truth, src)

def policy(row, exp_s=2.0, drop=False, light_only=False, min_cover=0, p_pass=0.45):
    name, src, C, F, missed, data_only, ch = row
    base = P(C) + (300 if missed else 0)
    if not C or data_only:
        return base, 0, False
    E = {n for n in C if by[n]["seconds"] >= exp_s}
    K = C - E
    S = {n for n in K if not (light_only and by[n]["file"] in HEAVY)}
    if not S or len(S) < min_cover:
        return base, 0, False
    keep = K - S
    after = (keep if drop and E else keep | E)
    est = est_cpu(sorted(S))
    gain = P(C) - P(after)
    if p_pass * gain - est * (p_pass + (1 - p_pass) * 0.5) <= 0.2:
        return base, 0, False
    if F & S:  # a verified test fails: early exit (charge half), output everything not seen passing
        return P(C) + est * 0.5 + (300 if missed else 0), 0, True
    out = after
    miss = bool((F - out)) or bool(missed)
    return P(out) + est + (300 if miss else 0), int(bool(F - out)), True

for label, kw in [("static", None), ("verify keep E", dict()), ("verify drop E", dict(drop=True)),
                  ("light-only drop E", dict(drop=True, light_only=True)),
                  ("light-only drop E, >=5", dict(drop=True, light_only=True, min_cover=5))]:
    tot = {}; miss = {}; nv = {}
    for r in rows:
        src = r[1]
        if kw is None:
            c, m, v = P(r[2]) + (300 if r[4] else 0), 0, False
        else:
            c, m, v = policy(r, **kw)
        tot[src] = tot.get(src, 0) + c; miss[src] = miss.get(src, 0) + m; nv[src] = nv.get(src, 0) + v
    print(f"{label:26}", "  ".join(f"{s}: n={sum(1 for r in rows if r[1]==s)} cost={tot[s]:7.1f} newmiss={miss[s]} verified={nv[s]}" for s in sorted(tot)))
bad = [r[0] for r in rows if policy(r, drop=True)[1]]
print("drop-E misses:", bad)
