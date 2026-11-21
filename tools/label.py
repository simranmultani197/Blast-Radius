"""Label mutants by running the full suite on each (validation only).

    python /tools/label.py /mut /src JOBS      (inside the dev image)
writes /mut/<id>.truth.json
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, "/repo")
from br import patch as P  # noqa: E402

mut, src, jobs = sys.argv[1], sys.argv[2], int(sys.argv[3])
todo = sorted(p for p in glob.glob(f"{mut}/*.patch") if not os.path.exists(p[:-6] + ".truth.json"))
running = {}
slot_free = list(range(jobs))
t0 = time.time()
done = 0


def start(pf, slot):
    w = f"/tmp/w{slot}"
    shutil.rmtree(w, ignore_errors=True)
    os.makedirs(w)
    for name in os.listdir(src):
        if name in (".git",):
            continue
        s, d = f"{src}/{name}", f"{w}/{name}"
        if os.path.isdir(s):
            shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__", ".git"), symlinks=True)
        else:
            shutil.copy(s, d)
    for fp in P.parse(open(pf).read()):
        old = open(f"{w}/{fp.old_path}", encoding="utf-8", newline="").read() if fp.old_path else None
        new = P.apply(old, fp)
        if new is None:
            os.remove(f"{w}/{fp.path}")
        else:
            open(f"{w}/{fp.path}", "w", encoding="utf-8", newline="").write(new)
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=f"{w}:/repo/tools", BR_LABEL_OUT=pf[:-6] + ".truth.json")
    nodes = os.environ.get("BR_LABEL_NODES")
    targets = ["tests"]
    if nodes:
        sel = json.load(open(pf[:-6] + ".cands.json"))
        targets = sel or []
        if not targets:
            json.dump({"failing": [], "collect_errors": [], "exit": 5}, open(pf[:-6] + ".truth.json", "w"))
            return subprocess.Popen(["true"])
    return subprocess.Popen(
        [sys.executable, "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider", "-p", "label_plugin",
         "--timeout=120"],
        cwd=w, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


LIMIT = float(os.environ.get("BR_LABEL_LIMIT", "900"))
started = {}
procs = {}
while todo or running:
    while todo and slot_free:
        slot = slot_free.pop()
        pf = todo.pop(0)
        pr = start(pf, slot)
        running[pr.pid] = (pf, slot)
        started[pr.pid] = time.time()
        procs[pr.pid] = pr
    time.sleep(0.5)
    for pid in list(running):
        pr = procs[pid]
        if pr.poll() is None and time.time() - started[pid] > LIMIT:
            import signal
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            pr.wait()
            pf = running[pid][0]
            json.dump({"failing": None, "timeout": True}, open(pf[:-6] + ".truth.json", "w"))
        if pr.poll() is None:
            continue
        pf, slot = running.pop(pid)
        slot_free.append(slot)
        done += 1
        if done % 10 == 0:
            print(f"{done} labelled, {time.time() - t0:.0f}s", flush=True)
print("all labelled", flush=True)
