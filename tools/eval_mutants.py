"""Evaluate the static selector on labelled synthetic mutants.

    python3 tools/eval_mutants.py .work/sqlglot .work/mutants [-v]
"""
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from br.pack import pack  # noqa: E402
from br.select import CovMap, analyze  # noqa: E402

root, mdir = sys.argv[1], sys.argv[2]
verbose = "-v" in sys.argv
cmap = CovMap("maps/map.json.gz")
tests = json.load(open("sample/tests.json"))["tests"]
num_by_id = {t["id"]: t["number"] for t in tests}
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
file_of = {t["number"]: t["file"] for t in tests}
to_num = [num_by_id[t] for t in cmap.tests]
tot = orc = miss = n = 0
by_kind = {}
for tf in sorted(glob.glob(f"{mdir}/*.truth.json")):
    mid = Path(tf).name.split(".")[0]
    truth_raw = json.load(open(tf))
    if truth_raw.get("failing") is None:
        continue
    meta = json.load(open(f"{mdir}/{mid}.meta.json"))
    truth = set()
    for nid in truth_raw["failing"]:
        if nid in num_by_id:
            truth.add(num_by_id[nid])
        else:
            base = nid.split("[")[0]
            if base in num_by_id:
                truth.add(num_by_id[base])
            else:
                # collection error for a whole file / class
                truth |= {num for tid, num in num_by_id.items() if tid.startswith(nid)}
    for ce in truth_raw.get("collect_errors", []):
        truth |= {num for tid, num in num_by_id.items() if tid.startswith(ce)}
    patch = open(f"{mdir}/{mid}.patch").read()
    ch = analyze(patch, root, cmap)
    cand = {to_num[i] for i in ch.tests}
    missed = truth - cand
    _, cost = pack(sorted(cand), ms)
    _, ocost = pack(sorted(truth), ms)
    n += 1
    tot += cost + (300000 if missed else 0)
    orc += ocost
    k = meta["kind"]
    s = by_kind.setdefault(k, [0, 0, 0, 0])
    s[0] += 1
    s[1] += cost
    s[2] += ocost
    if missed:
        miss += 1
        s[3] += 1
        print(f"MISS {mid} {meta['ops']} truth={len(truth)} cand={len(cand)} missed={len(missed)} "
              f"e.g. {[tests[m]['id'] for m in sorted(missed)[:3]]}")
        reasons = sorted({ch.tests.get(i, '') for i in list(ch.tests)[:5]})
        print("     reasons:", reasons[:4], ch.notes[:3])
    elif verbose:
        print(f"ok   {mid} {k:8} truth={len(truth):4} cand={len(cand):4} cost={cost/1000:6.1f} oracle={ocost/1000:6.1f} {meta['ops']}")
print(f"\nmutants={n} misses={miss} cost={tot/1000:.1f}s oracle={orc/1000:.1f}s")
for k, (c, cost, oc, m) in sorted(by_kind.items()):
    print(f"  {k:8} n={c:3} cost={cost/1000:7.1f} oracle={oc/1000:7.1f} misses={m}")
