"""Build-time tracer: runs every test in its own forked process and records
which source lines (sqlglot/ and tests/) it executes and which data files it
opens. Run inside the dev image with the sqlglot checkout at /src.

    python tools/trace_tests.py /data/tests.json /out/trace  [jobs]
"""
import json
import os
import sys
import time



def run_one(num, nodeid, out_dir):
    import pytest
    import sitecustomize as tracer

    os.environ["BR_TRACE_DIR"] = out_dir
    os.environ["BR_TRACE_TEST"] = str(num)
    outcome = {"failed": False}

    class Rec:
        def pytest_runtest_logreport(self, report):
            if report.failed:
                outcome["failed"] = True

        def pytest_collectreport(self, report):
            if report.failed:
                outcome["failed"] = True

    tracer.start(num)
    t0 = time.perf_counter()
    rc = pytest.main([nodeid, "-q", "-p", "no:cacheprovider", "--no-header", "-s"], plugins=[Rec()])
    dt = time.perf_counter() - t0
    tracer.finish()
    with open(f"{out_dir}/{num}.result", "w") as f:
        json.dump({"id": nodeid, "rc": int(rc), "failed": outcome["failed"], "wall": round(dt, 3)}, f)


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    out = sys.argv[2]
    jobs = int(sys.argv[3]) if len(sys.argv) > 3 else os.cpu_count()
    os.makedirs(out, exist_ok=True)
    os.chdir("/src")
    sys.path.insert(0, "/src")
    # heavy third-party imports shared by every child (none of them import sqlglot)
    import pytest  # noqa: F401
    for mod in ("numpy", "pandas", "duckdb"):
        try:
            __import__(mod)
        except Exception:
            pass
    assert "sqlglot" not in sys.modules

    todo = [t for t in tests if not os.path.exists(f"{out}/{t['number']}.result")]
    running = {}
    t0 = time.time()
    done = 0
    while todo or running:
        while todo and len(running) < jobs:
            t = todo.pop(0)
            pid = os.fork()
            if pid == 0:
                try:
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull, 1)
                    os.dup2(devnull, 2)
                    run_one(t["number"], t["id"], out)
                finally:
                    os._exit(0)
            running[pid] = t
        pid, _ = os.wait()
        running.pop(pid, None)
        done += 1
        if done % 100 == 0:
            print(f"{done} done, {time.time() - t0:.0f}s", flush=True)
    print(f"all done in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
