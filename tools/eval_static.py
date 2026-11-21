"""Static-only evaluation on a labelled set: candidates vs truth, cost if output as-is."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from br.select import CovMap, analyze  # noqa: E402
from br.pack import pack  # noqa: E402

root, sample = sys.argv[1], Path(sys.argv[2])
cmap = CovMap(sys.argv[3] if len(sys.argv) > 3 else "maps/map.json.gz")
tests = json.load(open(sample / "tests.json"))["tests"]
answers = json.load(open(sample / "answers.json"))["commits"]
num_by_id = {t["id"]: t["number"] for t in tests}
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
to_num = [num_by_id[t] for t in cmap.tests]
total = misses = 0
oracle = 0
for pf in sorted((sample / "commits").glob("*.patch")):
    cid = pf.stem
    ch = analyze(pf.read_text(), root, cmap)
    cand = {to_num[i] for i in ch.tests}
    truth = set(answers[cid]["failing"])
    miss = truth - cand
    _, cost = pack(sorted(cand), ms)
    _, ocost = pack(sorted(truth), ms)
    oracle += ocost
    total += cost + (300000 if miss else 0)
    misses += bool(miss)
    big = sorted((ms[t] for t in cand), reverse=True)[:3]
    print(f"{cid} {answers[cid]['kind']:8} fail={len(truth):3} cand={len(cand):5} "
          f"cost={cost/1000:6.1f} {'MISS ' + str(sorted(miss)) if miss else 'ok'} broad={ch.broad} top={big} {ch.notes[:2]}")
print(f"total={total/1000:.1f}s misses={misses} oracle={oracle/1000:.1f}s")
