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
    for name in ("sqlglot", "tests"):
        shutil.copytree(f"{src}/{name}", f"{w}/{name}", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "setup.cfg"):
        shutil.copy(f"{src}/{name}", w)
    for fp in P.parse(open(pf).read()):
        old = open(f"{w}/{fp.old_path}", encoding="utf-8", newline="").read() if fp.old_path else None
        new = P.apply(old, fp)
        if new is None:
            os.remove(f"{w}/{fp.path}")
        else:
            open(f"{w}/{fp.path}", "w", encoding="utf-8", newline="").write(new)
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=f"{w}:/repo/tools", BR_LABEL_OUT=pf[:-6] + ".truth.json")
    return subprocess.Popen(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-p", "label_plugin",
         "--timeout=120", "-x" if False else "-q"],
        cwd=w, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


while todo or running:
    while todo and slot_free:
        slot = slot_free.pop()
        pf = todo.pop(0)
        running[start(pf, slot).pid] = (pf, slot)
    pid, _ = os.wait()
    if pid in running:
        pf, slot = running.pop(pid)
        slot_free.append(slot)
        done += 1
        if done % 10 == 0:
            print(f"{done} labelled, {time.time() - t0:.0f}s", flush=True)
print("all labelled", flush=True)
