"""Line/open tracer that follows a test into any Python process it spawns.

Active only when BR_TRACE_DIR and BR_TRACE_TEST are set; each process writes
<dir>/<test>.<pid>.json at exit.
"""
import atexit
import json
import os
import signal
import sys

SRC = "/src/"
_state = {}


def start(test=None):
    out_dir = os.environ.get("BR_TRACE_DIR")
    test = test if test is not None else os.environ.get("BR_TRACE_TEST")
    if not out_dir or test is None or _state:
        return
    lines, opened, seen = {}, set(), set()
    recv_codes, seen_types, receivers, lambdas = set(), set(), set(), set()
    getframe = sys._getframe
    mon = sys.monitoring
    TOOL = 4
    mon.use_tool_id(TOOL, "br-trace")

    depth = [0]
    module_codes = set()

    def on_start(code, offset):
        if code in module_codes:
            depth[0] += 1
            return None
        if code in recv_codes:
            if depth[0]:
                return None
            v = getframe(1).f_locals.get(code.co_varnames[0])
            t = v if isinstance(v, type) else type(v)
            if t not in seen_types:
                seen_types.add(t)
                for k in getattr(t, "__mro__", ()):
                    m = getattr(k, "__module__", "") or ""
                    if m.startswith(("sqlglot", "tests")):
                        receivers.add(f"{m}:{k.__qualname__}")
            return None
        if code.co_filename.startswith(SRC) and code not in seen:
            seen.add(code)
            mon.set_local_events(TOOL, code, mon.events.LINE)
            if code.co_name == "<module>":
                module_codes.add(code)
                mon.set_local_events(TOOL, code, mon.events.LINE)
                depth[0] += 1
                return None
            if code.co_name == "<lambda>":
                lambdas.add((code.co_filename, code.co_firstlineno))
            if code.co_argcount and code.co_varnames[0] in ("self", "cls", "klass", "mcs"):
                recv_codes.add(code)
                return on_start(code, offset)
        return mon.DISABLE

    def on_return(code, offset, retval):
        if code in module_codes:
            depth[0] -= 1
            return None
        return mon.DISABLE

    def on_unwind(code, offset, exc):
        if code in module_codes:
            depth[0] -= 1

    def on_line(code, line):
        lines.setdefault(code.co_filename, set()).add(line)
        return mon.DISABLE

    def audit(event, args):
        if event == "open" and args and isinstance(args[0], str):
            p = args[0]
            if p.startswith(SRC) and not p.endswith((".pyc", ".py")):
                opened.add(p)
        elif event in ("os.listdir", "os.scandir", "glob.glob") and args:
            p = args[0]
            if isinstance(p, str) and p.startswith(SRC):
                opened.add(p.rstrip("/") + "/")

    def dump(*_):
        if _state.get("done"):
            return
        _state["done"] = True
        mon.set_events(TOOL, 0)
        rec = {
            "test": str(test),
            "pid": os.getpid(),
            "lines": {k[len(SRC):]: sorted(v) for k, v in lines.items()},
            "opened": sorted(p[len(SRC):] for p in opened),
            "receivers": sorted(receivers),
            "lambdas": sorted([f[len(SRC):], ln] for f, ln in lambdas),
        }
        with open(f"{out_dir}/{test}.{os.getpid()}.json", "w") as f:
            json.dump(rec, f)

    _state["dump"] = dump
    _state["depth"] = depth
    _state["receivers"] = receivers
    sys.addaudithook(audit)
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.PY_RETURN, on_return)
    mon.register_callback(TOOL, mon.events.PY_UNWIND, on_unwind)
    mon.set_events(TOOL, mon.events.PY_START | mon.events.PY_RETURN | mon.events.PY_UNWIND)
    atexit.register(dump)

    def on_term(signum, frame):
        dump()
        os._exit(1)

    try:
        signal.signal(signal.SIGTERM, on_term)
    except ValueError:
        pass


def finish():
    if "dump" in _state:
        _state["dump"]()


start()
