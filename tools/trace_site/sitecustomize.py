"""Line/open tracer that follows a test into any Python process it spawns.

Active only when BR_TRACE_DIR and BR_TRACE_TEST are set; each process writes
<dir>/<test>.<pid>.json at exit.
"""
import atexit
import json
import os
import re
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
    attr_reads, attr_import = set(), set()
    cmp_diff, cmp_import = set(), set()
    recv_codes, seen_types, receivers, lambdas = set(), set(), set(), set()
    modules = set()
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
                modules.add(code.co_filename)
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
            "modules": sorted(f[len(SRC):] for f in modules),
            "attr_reads": sorted(attr_reads),
            "attr_import": sorted(attr_import),
            "cmp_diff": sorted(cmp_diff),
            "cmp_import": sorted(cmp_import),
        }
        with open(f"{out_dir}/{test}.{os.getpid()}.json", "w") as f:
            json.dump(rec, f)

    if os.environ.get("BR_TRACE_ATTRS"):
        _install_attr_tracer(depth, attr_reads, attr_import, cmp_diff, cmp_import)

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


UPPER = re.compile(r"^_*[A-Z][A-Z0-9_]*$")


CMP_OPS = ["Eq", "NotEq", "Is", "IsNot", "Lt", "LtE", "Gt", "GtE"]
CMP_ALTS = {
    "Eq": ["NotEq", "Is", "IsNot"], "NotEq": ["Eq", "Is", "IsNot"],
    "Is": ["IsNot", "Eq", "NotEq"], "IsNot": ["Is", "Eq", "NotEq"],
    "Lt": ["LtE", "Gt", "GtE", "Eq", "NotEq"], "LtE": ["Lt", "Gt", "GtE", "Eq", "NotEq"],
    "Gt": ["GtE", "Lt", "LtE", "Eq", "NotEq"], "GtE": ["Gt", "Lt", "LtE", "Eq", "NotEq"],
}


def _install_attr_tracer(depth, reads, import_reads, cmp_diff=None, cmp_import=None):
    import ast
    import builtins
    import enum
    import importlib.abc
    import importlib.machinery

    owner_cache = {}
    skip_types = set()

    def owner_of(cls, name):
        k = (cls, name)
        o = owner_cache.get(k, 0)
        if o == 0:
            o = None
            for base in getattr(cls, "__mro__", ()):
                if name in base.__dict__:
                    m = getattr(base, "__module__", "") or ""
                    if m.startswith(("sqlglot", "tests")):
                        o = f"{m}:{base.__qualname__}:{name}"
                    break
            owner_cache[k] = o
        return o

    def record(obj, name):
        try:
            if isinstance(obj, type):
                if obj in skip_types:
                    return
                if issubclass(obj, enum.Enum):
                    skip_types.add(obj)
                    return
                o = owner_of(obj, name)
            else:
                d = getattr(obj, "__dict__", None)
                if d is not None and name in d:
                    return
                o = owner_of(type(obj), name)
            if o is not None:
                (import_reads if depth[0] else reads).add(o)
        except Exception:  # noqa: BLE001
            pass

    def br_ga(obj, name):
        r = getattr(obj, name)
        record(obj, name)
        return r

    _missing = object()

    def br_getattr(obj, name, default=_missing):
        if default is _missing:
            r = getattr(obj, name)
        else:
            r = getattr(obj, name, default)
        record(obj, name)
        return r

    def br_hasattr(obj, name):
        r = hasattr(obj, name)
        record(obj, name)
        return r

    import operator

    OPF = {"Eq": operator.eq, "NotEq": operator.ne, "Is": operator.is_, "IsNot": operator.is_not,
           "Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge}
    done_alts = {}

    def br_cmp(site, a, b, op):
        r = OPF[op](a, b)
        if cmp_diff is None:
            return r
        if depth[0]:
            cmp_import.add(site)
            return r
        seen = done_alts.get(site)
        if seen is None:
            seen = done_alts[site] = set()
        alts = CMP_ALTS[op]
        if len(seen) == len(alts):
            return r
        for alt in alts:
            if alt in seen:
                continue
            try:
                r2 = OPF[alt](a, b)
                same = type(r) is bool and type(r2) is bool and r == r2
            except Exception:  # noqa: BLE001
                same = False
            if not same:
                seen.add(alt)
                cmp_diff.add(f"{site}|{alt}")
        return r

    builtins.__br_cmp__ = br_cmp
    builtins.__br_ga__ = br_ga
    builtins.__br_getattr__ = br_getattr
    builtins.__br_hasattr__ = br_hasattr

    class Xform(ast.NodeTransformer):
        def __init__(self, rel=None):
            self.rel = rel

        def visit_Compare(self, node):
            self.generic_visit(node)
            if self.rel and cmp_diff is not None and len(node.ops) == 1 and type(node.ops[0]).__name__ in OPF:
                site = f"{self.rel}:{node.lineno}:{node.col_offset}"
                new = ast.Call(func=ast.Name("__br_cmp__", ast.Load()),
                               args=[ast.Constant(site), node.left, node.comparators[0],
                                     ast.Constant(type(node.ops[0]).__name__)], keywords=[])
                return ast.copy_location(new, node)
            return node

        def visit_arg(self, node):
            return node  # leave annotations alone

        def visit_FunctionDef(self, node):
            node.args = self.visit(node.args)
            node.body = [self.visit(x) for x in node.body]
            node.decorator_list = [self.visit(x) for x in node.decorator_list]
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_AnnAssign(self, node):
            if node.value is not None:
                node.value = self.visit(node.value)
            node.target = self.visit(node.target)
            return node

        def visit_Attribute(self, node):
            self.generic_visit(node)
            if isinstance(node.ctx, ast.Load) and UPPER.match(node.attr):
                new = ast.Call(func=ast.Name("__br_ga__", ast.Load()), args=[node.value, ast.Constant(node.attr)],
                               keywords=[])
                return ast.copy_location(new, node)
            return node

        def visit_Call(self, node):
            self.generic_visit(node)
            f = node.func
            if (isinstance(f, ast.Name) and f.id in ("getattr", "hasattr") and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                    and UPPER.match(node.args[1].value)):
                node.func = ast.copy_location(ast.Name(f"__br_{f.id}__", ast.Load()), f)
            return node

    import hashlib
    import marshal

    cache_dir = os.environ.get("BR_XCACHE")

    class XLoader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            st = os.stat(self.path)
            cpath = None
            if cache_dir:
                h = hashlib.sha1(f"{self.path}|{st.st_mtime_ns}|{st.st_size}|v2".encode()).hexdigest()
                cpath = os.path.join(cache_dir, h)
                try:
                    with open(cpath, "rb") as f:
                        return marshal.loads(f.read())
                except OSError:
                    pass
            src = self.get_data(self.path)
            tree = ast.parse(src, self.path)
            tree = Xform(self.path[len(SRC):] if self.path.startswith(SRC) else None).visit(tree)
            ast.fix_missing_locations(tree)
            code = compile(tree, self.path, "exec", dont_inherit=True)
            if cpath:
                tmp = f"{cpath}.{os.getpid()}"
                with open(tmp, "wb") as f:
                    f.write(marshal.dumps(code))
                os.replace(tmp, cpath)
            return code

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is not None and spec.origin and spec.origin.startswith(SRC) and spec.origin.endswith(".py") \
                    and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = XLoader(spec.loader.name, spec.loader.path)
            return spec

    sys.meta_path.insert(0, Finder())
    sys.dont_write_bytecode = True


def finish():
    if "dump" in _state:
        _state["dump"]()


start()
