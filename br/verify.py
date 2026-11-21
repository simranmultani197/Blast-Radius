"""Run chosen tests against a patched tree, in forked children of a warm parent.

The parent imports pytest (and duckdb/pandas when needed) but never sqlglot,
so each child imports the patched sqlglot from a symlink farm in /tmp.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time

_preloaded = set()


def preload(heavy=False):
    if "pytest" not in _preloaded:
        import pytest  # noqa: F401
        import _pytest.unittest  # noqa: F401
        import unittest.mock  # noqa: F401
        import concurrent.futures.process  # noqa: F401
        _preloaded.add("pytest")
    if heavy and "heavy" not in _preloaded:
        for m in ("numpy", "pandas", "pandas.testing", "duckdb"):
            try:
                __import__(m)
            except Exception:  # noqa: BLE001
                pass
        _preloaded.add("heavy")
    assert "sqlglot" not in sys.modules


def make_farm(root: str, changes: dict, dst: str):
    """Symlink tree of root at dst with changed files written for real."""
    os.makedirs(dst)
    real_dirs = {""}
    for path in changes:
        d = os.path.dirname(path)
        while d:
            real_dirs.add(d)
            d = os.path.dirname(d)
    for d in sorted(real_dirs, key=lambda x: x.count("/") if x else -1):
        os.makedirs(os.path.join(dst, d), exist_ok=True)
        src_dir = os.path.join(root, d)
        if not os.path.isdir(src_dir):
            continue
        for entry in os.listdir(src_dir):
            rel = os.path.join(d, entry) if d else entry
            if rel in real_dirs or rel in changes:
                continue
            if entry == "__pycache__" and d:
                # own pycache dir: symlink the pycs of unchanged modules only
                own = os.path.join(dst, rel)
                os.makedirs(own, exist_ok=True)
                changed_stems = {os.path.splitext(os.path.basename(p))[0] for p in changes if os.path.dirname(p) == d}
                for pyc in os.listdir(os.path.join(src_dir, entry)):
                    if pyc.split(".")[0] in changed_stems:
                        continue
                    os.symlink(os.path.join(src_dir, entry, pyc), os.path.join(own, pyc))
                continue
            os.symlink(os.path.join(src_dir, entry), os.path.join(dst, rel))
    for path, text in changes.items():
        if text is None:
            continue
        full = os.path.join(dst, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(text)


class _InlineFuture:
    def __init__(self, fn, args, kwargs):
        import pickle

        self._exc = None
        self._res = None
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            res = fn(*args, **kwargs)
            self._res = pickle.loads(pickle.dumps(res))
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            try:
                self._exc = pickle.loads(pickle.dumps(e))
            except Exception:  # noqa: BLE001
                self._exc = e

    def result(self, timeout=None):
        if self._exc is not None:
            raise self._exc
        return self._res

    def exception(self, timeout=None):
        return self._exc

    def done(self):
        return True

    def cancelled(self):
        return False

    def running(self):
        return False

    def cancel(self):
        return False

    def add_done_callback(self, fn):
        fn(self)


class InlinePool:
    """Stands in for ProcessPoolExecutor: runs work in-process, keeping the pickle round trip."""

    def __init__(self, *a, **k):
        pass

    def submit(self, fn, /, *args, **kwargs):
        return _InlineFuture(fn, args, kwargs)

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        futs = [self.submit(fn, *a) for a in zip(*iterables)]
        return (f.result() for f in futs)

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _inline_wait(fs, timeout=None, return_when=None):
    import concurrent.futures as cf

    return cf._base.DoneAndNotDoneFutures(set(fs), set())


def _child(farm, nodeids, budgets, wfd):
    out = os.fdopen(wfd, "w", buffering=1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.chdir(farm)
    sys.path.insert(0, farm)
    os.environ["PYTHONPATH"] = farm
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    import concurrent.futures as cf
    import concurrent.futures.process as cfp

    cf.ProcessPoolExecutor = InlinePool
    cfp.ProcessPoolExecutor = InlinePool
    cf.as_completed = lambda fs, timeout=None: iter(list(fs))
    cf.wait = _inline_wait
    import pytest

    class Timeout(Exception):
        pass

    def on_alarm(signum, frame):
        signal.setitimer(signal.ITIMER_REAL, 0.5)
        raise Timeout("budget exceeded")

    signal.signal(signal.SIGALRM, on_alarm)

    class Rec:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            signal.setitimer(signal.ITIMER_REAL, budgets.get(item.nodeid, 5.0))
            out.write(json.dumps(["start", item.nodeid]) + "\n")
            yield
            signal.setitimer(signal.ITIMER_REAL, 0)
            out.write(json.dumps(["end", item.nodeid]) + "\n")

        def pytest_runtest_logreport(self, report):
            if report.failed:
                out.write(json.dumps(["fail", report.nodeid]) + "\n")

        def pytest_collectreport(self, report):
            if report.failed:
                out.write(json.dumps(["collectfail", report.nodeid]) + "\n")

    args = list(nodeids) + ["-q", "-p", "no:cacheprovider", "--assert=plain", "-p", "no:warnings",
                            "-p", "no:doctest", "-p", "no:junitxml", "-p", "no:pastebin", "-p", "no:nose",
                            "--no-header", "-o", "console_output_style=classic"]
    try:
        pytest.main(args, plugins=[Rec()])
    finally:
        out.write(json.dumps(["done", None]) + "\n")
        out.flush()
        os._exit(0)


class Job:
    def __init__(self, key, pid, rfd, farm, nodeids, deadline):
        self.key, self.pid, self.farm, self.nodeids, self.deadline = key, pid, farm, nodeids, deadline
        self.reader = os.fdopen(rfd, "r")
        self.started = time.time()


def launch(key, root, changes, nodeids, budgets, tmp="/tmp/br", wall_limit=60.0):
    farm = os.path.join(tmp, key)
    if os.path.exists(farm):
        shutil.rmtree(farm, ignore_errors=True)
    make_farm(root, changes, farm)
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            _child(farm, nodeids, budgets, w)
        except BaseException:  # noqa: BLE001
            os._exit(1)
    os.close(w)
    return Job(key, pid, r, farm, nodeids, time.time() + wall_limit)


def collect(job: Job):
    """Wait for a job; returns (failing nodeids, unknown nodeids, cpu seconds)."""
    events = []
    import select as _sel

    fd = job.reader.fileno()
    buf = ""
    killed = False
    while True:
        timeout = job.deadline - time.time()
        if timeout <= 0:
            os.kill(job.pid, signal.SIGKILL)
            killed = True
            break
        ready, _, _ = _sel.select([fd], [], [], min(timeout, 1.0))
        if ready:
            chunk = os.read(fd, 65536).decode()
            if not chunk:
                break
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                events.append(json.loads(line))
    _, status, ru = os.wait4(job.pid, 0)
    job.reader.close()
    cpu = ru.ru_utime + ru.ru_stime
    shutil.rmtree(job.farm, ignore_errors=True)
    failing, ended, collect_fail = set(), set(), []
    done = False
    for kind, nid in events:
        if kind == "fail":
            failing.add(nid)
        elif kind == "end":
            ended.add(nid)
        elif kind == "collectfail":
            collect_fail.append(nid)
        elif kind == "done":
            done = True
    unknown = set()
    for nid in job.nodeids:
        if nid in failing:
            continue
        if any(nid == c or nid.startswith(c + "::") or nid.startswith(c.rstrip("/") + "/") or c == "" for c in collect_fail):
            failing.add(nid)
        elif nid not in ended:
            unknown.add(nid)
    if killed or not done:
        unknown |= {n for n in job.nodeids if n not in ended and n not in failing}
    return failing, unknown, cpu
