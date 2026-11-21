"""Run chosen tests against a patched tree, in forked children of a warm parent.

The parent never imports sqlglot, so each child imports the patched sqlglot
from a symlink farm in /tmp. Children run plain unittest (every sqlglot test is
a unittest.TestCase) in the suite's own order and stop at the first failure:
a failure means the commit fails, and then we fall back to the full candidate
set, so all we need to learn is "does anything fail".
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
    if "base" not in _preloaded:
        import unittest  # noqa: F401
        import unittest.mock  # noqa: F401
        import concurrent.futures.process  # noqa: F401
        import pickle  # noqa: F401
        import decimal  # noqa: F401
        import datetime  # noqa: F401
        _preloaded.add("base")
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
                own = os.path.join(dst, rel)
                os.makedirs(own, exist_ok=True)
                changed = {os.path.splitext(os.path.basename(p))[0] for p in changes if os.path.dirname(p) == d}
                for pyc in os.listdir(os.path.join(src_dir, entry)):
                    if pyc.split(".")[0] in changed:
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
            self._res = pickle.loads(pickle.dumps(fn(*args, **kwargs)))
        except Exception as e:  # noqa: BLE001
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

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **k):
        if initializer is not None:
            import pickle

            fn, args = pickle.loads(pickle.dumps((initializer, tuple(initargs))))
            fn(*args)

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


def _child(farm, nodeids, budgets, wfd):
    out = os.fdopen(wfd, "w", buffering=1)

    def emit(*ev):
        out.write(json.dumps(ev) + "\n")
        out.flush()

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    if not os.environ.get("BR_DEBUG"):
        os.dup2(devnull, 2)
    os.chdir(farm)
    sys.path.insert(0, farm)
    os.environ["PYTHONPATH"] = farm
    os.environ["SKIP_INTEGRATION"] = "0"
    import concurrent.futures as cf
    import concurrent.futures.process as cfp
    import importlib
    import unittest

    cf.ProcessPoolExecutor = InlinePool
    cfp.ProcessPoolExecutor = InlinePool
    cf.as_completed = lambda fs, timeout=None: iter(list(fs))
    cf.wait = lambda fs, timeout=None, return_when=None: cf._base.DoneAndNotDoneFutures(set(fs), set())

    def fail_and_exit(nid, why):
        emit("fail", nid, why)
        emit("done", None)
        os._exit(0)

    current = [None]
    t_start = [0.0]

    def on_alarm(signum, frame):
        fail_and_exit(current[0], "timeout")

    signal.signal(signal.SIGALRM, on_alarm)

    class Result(unittest.TestResult):
        def startTest(self, test):
            nid = by_test.get(id(test))
            current[0] = nid
            t_start[0] = time.process_time()
            signal.setitimer(signal.ITIMER_REAL, budgets.get(nid, 5.0))
            super().startTest(test)

        def stopTest(self, test):
            signal.setitimer(signal.ITIMER_REAL, 0)
            super().stopTest(test)
            emit("pass", by_test.get(id(test)), round(time.process_time() - t_start[0], 4))

        def addError(self, test, err):
            fail_and_exit(by_test.get(id(test), current[0]), "error")

        def addFailure(self, test, err):
            fail_and_exit(by_test.get(id(test), current[0]), "failure")

        def addSubTest(self, test, subtest, err):
            if err is not None:
                fail_and_exit(by_test.get(id(test), current[0]), "subtest")

        def addUnexpectedSuccess(self, test):
            fail_and_exit(by_test.get(id(test), current[0]), "unexpected success")

    by_test = {}
    suite = unittest.TestSuite()
    modules = {}
    for nid in nodeids:
        path, cls_name, meth = nid.split("::")
        mod_name = path[:-3].replace("/", ".")
        mod = modules.get(mod_name)
        if mod is None:
            current[0] = nid
            signal.setitimer(signal.ITIMER_REAL, 30.0)
            try:
                mod = importlib.import_module(mod_name)
            except BaseException:  # noqa: BLE001
                fail_and_exit(nid, "import")
            signal.setitimer(signal.ITIMER_REAL, 0)
            modules[mod_name] = mod
        try:
            test = getattr(mod, cls_name)(meth)
        except BaseException:  # noqa: BLE001
            fail_and_exit(nid, "collect")
        by_test[id(test)] = nid
        suite.addTest(test)
    emit("setup", None, round(time.process_time(), 4))
    result = Result()
    try:
        suite.run(result)
    except BaseException:  # noqa: BLE001
        fail_and_exit(current[0], "crash")
    emit("done", None)
    os._exit(0)


class Job:
    def __init__(self, key, pid, rfd, farm, nodeids, deadline):
        self.key, self.pid, self.farm, self.nodeids, self.deadline = key, pid, farm, nodeids, deadline
        self.fd = rfd
        self.buf = ""
        self.events = []
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
            pass
        os._exit(1)
    os.close(w)
    return Job(key, pid, r, farm, nodeids, time.time() + wall_limit)


def poll(job: Job, timeout=0.0):
    """Read available events; returns True once the child has exited."""
    import select

    ready, _, _ = select.select([job.fd], [], [], timeout)
    if ready:
        chunk = os.read(job.fd, 65536).decode()
        if not chunk:
            return True
        job.buf += chunk
        while "\n" in job.buf:
            line, job.buf = job.buf.split("\n", 1)
            job.events.append(json.loads(line))
    if time.time() > job.deadline:
        try:
            os.kill(job.pid, signal.SIGKILL)
        except OSError:
            pass
        return True
    return False


def finish(job: Job):
    """Reap a job. Returns (verdict, passed, cpu): verdict is 'fail', 'pass' or 'unknown'."""
    try:
        os.kill(job.pid, signal.SIGKILL)
    except OSError:
        pass
    _, status, ru = os.wait4(job.pid, 0)
    os.close(job.fd)
    shutil.rmtree(job.farm, ignore_errors=True)
    cpu = ru.ru_utime + ru.ru_stime
    passed = {nid for kind, nid, *_ in job.events if kind == "pass"}
    failed = [e for e in job.events if e[0] == "fail"]
    done = any(e[0] == "done" for e in job.events)
    if failed:
        return "fail", passed, cpu
    if done and passed >= set(job.nodeids):
        return "pass", passed, cpu
    return "unknown", passed, cpu


def run(key, root, changes, nodeids, budgets, wall_limit=60.0):
    job = launch(key, root, changes, nodeids, budgets, wall_limit=wall_limit)
    while not poll(job, 0.5):
        if any(e[0] == "fail" for e in job.events):
            break
    return finish(job)
