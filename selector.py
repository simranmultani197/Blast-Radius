"""Blast Radius selector: which sqlglot tests can a patch break, and on which workers.

For each patch we find the statements it changes in the pinned tree and select
the tests that execute them, using maps built from the pinned commit:
per-test line coverage (each test traced in a fresh process, so module- and
class-level lines count for every test that imports them), the classes each
test calls methods on, the lambdas it calls and the data files it opens.
"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from br.pack import pack  # noqa: E402
from br.select import CovMap, analyze  # noqa: E402
from br import tpcds_check  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
ROOT = os.environ.get("BR_SQLGLOT", "/opt/sqlglot")
MAP = os.environ.get("BR_MAP", str(HERE / "maps" / "map.json.gz"))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def main():
    t0 = time.time()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    num_by_id = {t["id"]: t["number"] for t in tests}
    all_nums = sorted(ms)
    cmap = CovMap(MAP)
    to_num = [num_by_id.get(tid) for tid in cmap.tests]
    unmapped = [n for tid, n in num_by_id.items() if tid not in set(cmap.tests)]
    if unmapped:
        log(f"warning: {len(unmapped)} tests are not in the map")
    lines = []
    tpcds = num_by_id.get(tpcds_check.TEST)
    # the TPC-DS check runs patched sqlglot in a child process: cap its total time
    budget_until = t0 + float(os.environ.get("BR_TPCDS_BUDGET", "100"))
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        try:
            text = patch.read_text(encoding="utf-8", errors="replace")
            ch = analyze(text, ROOT, cmap)
            sel = {to_num[i] for i in ch.tests if to_num[i] is not None}
            if ch.broad and unmapped:
                sel |= set(unmapped)
            why = f"{len(sel)} tests" + (" (broad)" if ch.broad else "")
            if tpcds in sel:
                c0 = time.time()
                try:
                    drop = tpcds_check.droppable(text, ch.new_files, ROOT, budget_until,
                                                 cmap.tpcds_process_profile)
                except Exception:  # noqa: BLE001 - the check failing keeps the test
                    log(f"{cid}: tpcds check failed, keeping the test\n{traceback.format_exc()}")
                    drop = False
                if drop:
                    sel.discard(tpcds)
                    why += " -tpcds"
                why += f" (tpcds check {time.time() - c0:.1f}s)"
        except Exception:  # noqa: BLE001 - never crash: fall back to running everything
            log(f"{cid}: analysis failed, running everything\n{traceback.format_exc()}")
            sel = set(all_nums)
            why = "fallback"
        workers, cost = pack(sorted(sel), ms)
        lines.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
        log(f"{cid}: {why}, {len(workers)} workers, est {cost / 1000:.1f}s")
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    sys.stdout.flush()
    cpu = time.process_time()
    import resource

    ch_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
    log(f"done {len(lines)} commits in {time.time() - t0:.1f}s wall, {cpu:.2f}s cpu "
        f"+ {ch_cpu.ru_utime + ch_cpu.ru_stime:.2f}s in checks")


if __name__ == "__main__":
    main()
