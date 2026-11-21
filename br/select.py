"""Patch -> candidate tests, using the coverage map built from the pinned tree."""
from __future__ import annotations

import ast
import gzip
import re
import json
import os

from . import analysis as A
from . import patch as P


class CovMap:
    def __init__(self, path):
        pkl = path[: -len(".json.gz")] + ".pkl" if path.endswith(".json.gz") else None
        if pkl and os.path.exists(pkl):
            import pickle

            with open(pkl, "rb") as f:
                d = pickle.load(f)
        else:
            with gzip.open(path, "rb") as f:
                d = json.loads(f.read())
        self.tests = d["tests"]
        self._sets_raw = d["sets"]
        self._sets = {}
        self.files = d["files"]
        self.opened = d["opened"]
        self.receivers = d.get("receivers", {})
        self.lambdas = d.get("lambdas", {})
        self.classes = d.get("classes", {})
        self.xrefs = d.get("xrefs", {})
        self.modules = d.get("modules", {})
        self.attr_reads = d.get("attr_reads")
        self.attr_import = set(d.get("attr_import", []))
        self.str_names = set(d.get("str_names", []))
        self.du = d.get("du")
        self.du_strings = set(self.du["strings"]) if self.du else set()
        self.dkeys = d.get("dkeys")
        self.dall = d.get("dall", {})
        self.dimport = set(d.get("dimport", []))
        self.keyed_import = any("|getk|" in x for x in self.dimport)
        self.consumed_ok = d.get("set_consumption_traced", False)
        self._runtime_origins = None
        self.ev = d.get("ev")
        self.ev_unknown = frozenset(d.get("ev_unknown", []))
        self.cmp_unknown = frozenset(d.get("cmp_unknown", []))
        self.dict_unknown = frozenset(d.get("dict_unknown", []))
        self.ev_import = set(d.get("ev_import", []))
        self.cmp = d.get("cmp")
        self.cmp_import = set(d.get("cmp_import", []))
        self.all = frozenset(range(len(self.tests)))

    def runtime_origins(self):
        if self._runtime_origins is None:
            self._runtime_origins = {k.split("|", 1)[0] for k in (self.dkeys or {})} | \
                {k.split("|", 1)[0] for k in self.dall}
        return self._runtime_origins

    def recv_tests(self, key):
        sid = self.receivers.get(key)
        return self.set(sid) if sid is not None else frozenset()

    def lambda_tests(self, path, line):
        sid = self.lambdas.get(f"{path}:{line}")
        return self.set(sid) if sid is not None else frozenset()

    def set(self, sid):
        s = self._sets.get(sid)
        if s is None:
            out, prev = [], -1
            raw = self._sets_raw[sid]
            if raw:
                for tok in raw.split("."):
                    prev += int(tok, 16)
                    out.append(prev)
            s = frozenset(out)
            self._sets[sid] = s
        return s

    def line_tests(self, path, line):
        d = self.files.get(path)
        if not d:
            return frozenset()
        sid = d.get(str(line))
        return self.set(sid) if sid is not None else frozenset()

    def span_tests(self, path, start, end):
        d = self.files.get(path)
        out = set()
        if not d:
            return out
        for ln in range(start, end + 1):
            sid = d.get(str(ln))
            if sid is not None:
                out |= self.set(sid)
        return out

    def file_tests(self, path):
        """Tests that import the module at path (or run any of its lines)."""
        d = self.files.get(path)
        out = set()
        if d:
            for sid in set(d.values()):
                out |= self.set(sid)
        sid = self.modules.get(path)
        if sid is not None:
            out |= self.set(sid)
        if not out and path.endswith(".py"):
            # never imported by a traced test: a package __init__ is still
            # imported by everything below it
            pkg = os.path.dirname(path)
            if os.path.basename(path) == "__init__.py":
                for f, sid2 in self.modules.items():
                    if f.startswith(pkg + "/"):
                        out |= self.set(sid2)
        return out


class Change:
    """What one patch does, with the candidate tests and why."""

    def __init__(self):
        self.tests: dict[int, str] = {}
        self.notes: list[str] = []
        self.broad = False       # some candidates come from import-level (coarse) reasoning
        self.syntax_error = False
        self.new_files: dict[str, str | None] = {}  # path -> new text (None = deleted)

    def add(self, tests, why):
        for t in tests:
            self.tests.setdefault(t, why)


def _func_at(fi: A.FileIndex, p: int, indent: int | None):
    """Innermost function whose body contains an insertion after old line p."""
    best = None
    for u in fi.units:
        n = u.node
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        inside = n.lineno <= p and (p + 1 <= n.end_lineno or (p == n.end_lineno and indent is not None and indent > n.col_offset))
        if inside and (best is None or n.lineno >= best.lineno):
            best = n
    return best


def _indent_of(lines):
    for ln in lines:
        if ln.strip():
            return len(ln) - len(ln.lstrip())
    return None


def analyze_py(path, old_src, new_src, fp: P.FilePatch, cmap: CovMap, ch: Change):
    if old_src is None:
        # brand-new module: only reachable through other changed files, or by
        # pytest itself when it is a conftest
        if os.path.basename(path) == "conftest.py":
            d = os.path.dirname(path)
            ch.add([i for i, t in enumerate(cmap.tests) if t.startswith(d + "/") or d == ""], "conftest")
            ch.broad = True
        ch.notes.append(f"new file {path}")
        return
    if new_src is None:
        ch.add(cmap.file_tests(path), f"deleted {path}")
        ch.broad = True
        return
    old_tree = _parse_cached(path, old_src)
    new_tree = A.parse(new_src)
    same = A.same_ast(old_src, new_src, old_tree, new_tree)
    if same:
        ch.notes.append(f"{path}: same AST")
        return
    if new_tree is None:
        ch.syntax_error = True
        ch.add(cmap.file_tests(path), f"syntax error {path}")
        ch.broad = True
        return
    if cmap.ev is not None:
        _index_cached(path, old_src, old_tree)
        tests = expression_rules(cmap, path, old_tree, new_tree, ch)
        if tests is not None:
            return
    if cmap.cmp is not None:
        sw = A.single_compare_swap(old_tree, new_tree)
        if sw is not None:
            line, col, old_op, new_op = sw
            site = f"{path}:{line}:{col}"
            if site not in cmap.cmp_import and new_op in CMP_ALTS.get(old_op, ()):
                sid = cmap.cmp.get(f"{site}|{new_op}")
                tests = set(cmap.set(sid)) if sid is not None else set()
                if cmap.cmp_unknown:
                    u = _index_cached(path, old_src, old_tree).smallest_unit(line)
                    if u is not None:
                        tests |= cmap.span_tests(path, u.start, u.end) & cmap.cmp_unknown
                ch.add(tests, f"{site} {old_op}->{new_op}")
                ch.notes.append(f"compare rule {site} {old_op}->{new_op}")
                return
    fi = _index_cached(path, old_src, old_tree)
    removed, inserts = P.changed_old_lines(fp)
    ins_text = _inserted_text(fp)
    cols = _changed_cols(fp)
    for line in sorted(removed):
        c0, c1 = cols.get(line, (0, 10 ** 6))
        lams = A.lambdas_containing(fi.tree, line, c0, c1)
        if lams:
            for lam in lams:
                ch.add(cmap.lambda_tests(path, lam.lineno), f"{path}:{line} lambda@{lam.lineno}")
            continue
        u = fi.smallest_unit(line)
        if u is None:
            fn = _func_at(fi, line - 1, None)
            if fn is not None:
                ch.add(cmap.span_tests(path, fn.body[0].lineno, fn.end_lineno), f"{path}:{line} in {fn.name}")
            else:
                _near(path, fi, line, line, cmap, ch)
            continue
        n = u.node
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            dec_end = max((d.end_lineno for d in n.decorator_list), default=0)
            if line <= dec_end or (u.scope != "function" and _name_changed(n, new_src)):
                _scope_rule(path, fi, u, cmap, ch, f"{path}:{line} decorator/name")
            ch.add(cmap.span_tests(path, n.body[0].lineno, n.end_lineno), f"{path}:{line} signature {n.name}")
            continue
        if u.scope == "function":
            ch.add(cmap.span_tests(path, u.start, u.end), f"{path}:{line} {u.kind}")
            continue
        if u.scope in ("class", "module") and cmap.dkeys is not None and line in cols:
            tests = dict_rule(cmap, path, u, line, cols[line], new_tree)
            if tests is None and SET_RULE:
                tests = set_rule(cmap, path, u, line, cols[line], new_tree)
            if tests is not None and cmap.dict_unknown:
                fb = Change()
                _scope_rule(path, fi, u, cmap, fb, "fallback")
                tests |= set(fb.tests) & cmap.dict_unknown
            if tests is not None:
                ch.add(tests, f"{path}:{line} dict entry")
                ch.notes.append(f"dict rule {path}:{line}")
                continue
        _scope_rule(path, fi, u, cmap, ch, f"{path}:{line} {u.kind}@{u.scope}")
    for p in inserts:
        indent = _indent_of(ins_text.get(p, []))
        fn = _func_at(fi, p, indent)
        if fn is not None:
            ch.add(cmap.span_tests(path, fn.body[0].lineno, fn.end_lineno), f"{path}:+{p} in {fn.name}")
            continue
        _near(path, fi, p, p + 1, cmap, ch)


def _near(path, fi, before, after, cmap, ch):
    """A change between statements outside any function: use the neighbours' scope."""
    done = False
    for x in (fi.prev_code_line(before), fi.next_code_line(after)):
        if x:
            uu = fi.smallest_unit(x)
            if uu is not None:
                # inserting next to a class member counts as a class-level change
                if uu.scope == "function":
                    ch.add(cmap.span_tests(path, uu.start, uu.end), f"{path}:{before} near")
                else:
                    _scope_rule(path, fi, uu, cmap, ch, f"{path}:{before} near {uu.kind}", attrs=set())
                done = True
    if not done:
        ch.add(cmap.file_tests(path), f"{path}:{before} unplaced")
        ch.broad = True


CMP_ALTS = {
    "Eq": ["NotEq", "Is", "IsNot"], "NotEq": ["Eq", "Is", "IsNot"],
    "Is": ["IsNot", "Eq", "NotEq"], "IsNot": ["Is", "Eq", "NotEq"],
    "Lt": ["LtE", "Gt", "GtE", "Eq", "NotEq"], "LtE": ["Lt", "Gt", "GtE", "Eq", "NotEq"],
    "Gt": ["GtE", "Lt", "LtE", "Eq", "NotEq"], "GtE": ["Gt", "Lt", "LtE", "Eq", "NotEq"],
}

REGISTRY_ATTRS = {"_sql_names", "key", "__name__", "__qualname__", "__slots__", "__init_subclass__"}


UPPER = re.compile(r"^_*[A-Z][A-Z0-9_]*$")


def attr_rule(cmap, ckey, names, cls_node, stmt):
    """Tests that read class attribute ckey.NAME at runtime, when that is all that can matter."""
    if cmap.attr_reads is None or not names or cls_node is None:
        return None
    if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
        return None
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    if not all(isinstance(t, ast.Name) for t in targets):
        return None
    out = set()
    for n in names:
        if not UPPER.match(n) or n in cmap.str_names or f"{ckey}:{n}" in cmap.attr_import:
            return None
        if _class_body_reads(cls_node, n, stmt):
            return None
        sid = cmap.attr_reads.get(f"{ckey}:{n}")
        if sid is not None:
            out |= cmap.set(sid)
    return out


def _class_body_reads(cls_node, name, skip):
    """Does code running in the class body itself (not in methods) read `name`?"""
    def walk(nodes):
        for st in nodes:
            if st is skip:
                continue
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                exprs = st.decorator_list + st.args.defaults + [d for d in st.args.kw_defaults if d is not None]
            elif isinstance(st, ast.ClassDef):
                exprs = st.bases + [k.value for k in st.keywords] + st.decorator_list
            else:
                exprs = [st]
            for e in exprs:
                for n in ast.walk(e):
                    if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load):
                        return True
        return False

    return walk(cls_node.body)


def _scope_rule(path, fi, u, cmap, ch, why, attrs=None):
    """A statement that runs at import time (class body or module level)."""
    if u.scope == "class" and u.qual and cmap.classes:
        ckey = f"{A.module_name(path)}:{u.qual}"
        names = A.assigned_names(u.node) if attrs is None else attrs
        if attrs is None:
            tests = attr_rule(cmap, ckey, names, u.cls, u.node)
            if tests is not None:
                ch.add(tests, why + f" attr {u.qual}.{sorted(names)}")
                ch.notes.append(f"attr rule {ckey} {sorted(names)}")
                return
        tests = class_tests(cmap, ckey, names, u.cls, path)
        if tests is not None:
            ch.add(tests, why + f" class {u.qual}")
            ch.notes.append(f"class rule {ckey} {sorted(names)}")
            return
    if u.scope == "module" and cmap.du is not None and attrs is None:
        names = A.assigned_names(u.node)
        simple = isinstance(u.node, (ast.Assign, ast.AnnAssign)) and all(
            isinstance(t, ast.Name) for t in (u.node.targets if isinstance(u.node, ast.Assign) else [u.node.target]))
        if simple and names and not _uses_globals(fi):
            tests = module_rule(cmap, A.module_name(path), names, set())
            if tests is not None:
                ch.add(tests, why + f" module names {sorted(names)}")
                ch.notes.append(f"module rule {A.module_name(path)} {sorted(names)}")
                return
    ch.add(cmap.span_tests(path, u.start, u.end), why + " importers")
    ch.broad = True


def _ev_tests(cmap, node, tags=("T", "F", "?")):
    site = f"{node.lineno}:{node.col_offset}"
    return site, tags


def expression_rules(cmap, path, old_tree, new_tree, ch):
    """Single expression-level edits, using the evaluation/outcome profiles.
    Returns the selected set (already added to ch) or None to fall through."""
    d = A.single_diff(old_tree, new_tree)
    if d is None:
        return None
    x, y, anc = d
    line = getattr(x, "lineno", None)
    if line is None:
        return None
    u = _INDEX[path].smallest_unit(line) if path in _INDEX else None
    if u is None:
        return None
    if u.scope != "function":
        return None  # import-time code: leave it to the class/module rules
    executors = cmap.span_tests(path, u.start, u.end)

    def ev(node, tags=("T", "F", "?")):
        site = f"{path}:{node.lineno}:{node.col_offset}"
        if any(f"{site}|{t}" in cmap.ev_import for t in ("T", "F", "?")):
            return None
        out = set()
        for t in tags:
            sid = cmap.ev.get(f"{site}|{t}")
            if sid is not None:
                out |= cmap.set(sid)
        return out
    # 1. comparison operator swap: outcome profile
    if isinstance(x, (ast.Compare, ast.cmpop)) and cmap.cmp is not None:
        sw = A.single_compare_swap(old_tree, new_tree)
        if sw is not None:
            line, col, old_op, new_op = sw
            site = f"{path}:{line}:{col}"
            if site not in cmap.cmp_import and new_op in CMP_ALTS.get(old_op, ()):
                sid = cmap.cmp.get(f"{site}|{new_op}")
                tests = set(cmap.set(sid)) if sid is not None else set()
                tests |= executors & cmap.cmp_unknown
                ch.add(tests, f"{site} {old_op}->{new_op}")
                ch.notes.append(f"compare rule {site} {old_op}->{new_op}")
                return tests
    # 2. and <-> or on two operands, where only truthiness matters
    if isinstance(x, ast.BoolOp) and isinstance(y, ast.BoolOp) and type(x.op) is not type(y.op) \
            and len(x.values) == 2 and anc and A.in_bool_context(x, anc[-1]) \
            and all(A.ast_equal(p, q) for p, q in zip(x.values, y.values)) \
            and not any(isinstance(n, (ast.Call, ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom, ast.Lambda))
                        for v in x.values for n in ast.walk(v)):
        # swapping the operator also changes whether the second operand runs at all,
        # so only operands without calls (no side effects) qualify
        flip = "F" if isinstance(x.op, ast.And) else "T"
        a = ev(x.values[0], (flip, "?"))
        b = ev(x.values[1], (flip, "?"))
        if a is not None and b is not None:
            tests = a | b | (executors & cmap.ev_unknown)
            ch.add(tests, f"{path}:{x.lineno} boolop {type(x.op).__name__}->{type(y.op).__name__}")
            ch.notes.append(f"boolop rule {path}:{x.lineno}")
            return tests
    # 3. an edit inside a short-circuited operand or a ternary branch
    chain = anc + [x]
    for i in range(len(chain) - 1, 0, -1):
        parent, child = chain[i - 1], chain[i]
        operand = None
        if isinstance(parent, ast.BoolOp) and child in parent.values[1:]:
            operand = child
        elif isinstance(parent, ast.IfExp) and (child is parent.body or child is parent.orelse):
            operand = child
        if operand is not None:
            if isinstance(x, (ast.Lambda, ast.NamedExpr)) or any(isinstance(n, (ast.NamedExpr, ast.Lambda, ast.Yield,
                                                                                ast.YieldFrom, ast.Await))
                                                              for n in ast.walk(operand)):
                return None
            tests = ev(operand)
            if tests is None:
                return None
            tests |= executors & cmap.ev_unknown
            ch.add(tests, f"{path}:{operand.lineno} evaluated operand")
            ch.notes.append(f"operand rule {path}:{operand.lineno}:{operand.col_offset}")
            return tests
        if isinstance(parent, (ast.stmt, ast.Lambda, ast.comprehension)):
            break
    return None


def _key_reprs(node):
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool) or v is None:
            return {f"c:{v!r}"}
        if isinstance(v, str):
            return {f"s:{v}"}
        if isinstance(v, int):
            return {f"i:{v}"}
        return None
    if isinstance(node, ast.Attribute):
        return {f"t:{node.attr}", f"e:{node.attr}"}
    if isinstance(node, ast.Tuple):
        parts = [_key_reprs(e) for e in node.elts]
        if any(p is None for p in parts) or len(node.elts) > 4:
            return None
        out = {"u:"}
        for i, p in enumerate(parts):
            out = {f"{o}{'|' if i else ''}{x}" for o in out for x in p}
        return out
    return None


def _span_contains(node, line, c0, c1):
    s_ok = (line > node.lineno) or (line == node.lineno and c0 >= node.col_offset)
    e_ok = (line < node.end_lineno) or (line == node.end_lineno and c1 <= node.end_col_offset)
    return s_ok and e_ok


def dict_rule(cmap, path, u, line, cols, new_tree):
    """A change inside one entry of a traced class/module-level dict literal."""
    st = u.node
    if not isinstance(st, (ast.Assign, ast.AnnAssign)) or not isinstance(st.value, ast.Dict):
        return None
    tg = st.targets if isinstance(st, ast.Assign) else [st.target]
    if len(tg) != 1 or not isinstance(tg[0], ast.Name):
        return None
    mod = A.module_name(path)
    origin = f"{mod}:{u.qual}:{tg[0].id}" if u.scope == "class" else f"{mod}::{tg[0].id}"
    if f"{origin}|created" not in cmap.dimport:
        return None
    c0, c1 = cols
    d = st.value
    hit = None
    for i, (k, v) in enumerate(zip(d.keys, d.values)):
        if k is not None and _span_contains(k, line, c0, c1):
            hit = (i, "key")
        elif _span_contains(v, line, c0, c1):
            hit = (i, "value" if k is not None else "spread")
    if hit is None or hit[1] == "spread":
        return None
    i, where = hit
    imports = {m for m in ("get", "keys", "vals") if f"{origin}|{m}" in cmap.dimport}
    keyed = cmap.keyed_import
    old_reprs = _key_reprs(d.keys[i])
    if old_reprs is None:
        return None
    reprs = set(old_reprs)
    if where == "value":
        if "vals" in imports:
            return None
        if "get" in imports and (not keyed or any(f"{origin}|getk|{r}" in cmap.dimport for r in reprs)):
            return None
        alls = ("vals",)
    else:
        new_reprs = _new_key_reprs(new_tree, st, i)
        if new_reprs is None:
            return None
        reprs |= new_reprs
        if imports & {"keys", "vals"}:
            return None
        if "get" in imports and (not keyed or any(f"{origin}|getk|{r}" in cmap.dimport for r in reprs)):
            return None
        alls = ("vals", "keys")
    out = set()
    for r in reprs:
        sid = cmap.dkeys.get(f"{origin}|{r}")
        if sid is not None:
            out |= cmap.set(sid)
    for m in alls:
        sid = cmap.dall.get(f"{origin}|{m}")
        if sid is not None:
            out |= cmap.set(sid)
    return out


# Off: a plain set on the left of `-`/`|` (or set.update(...)) copies a traced
# set in C without calling its methods, so lookups can go unrecorded.
SET_RULE = True


def set_rule(cmap, path, u, line, cols, new_tree):
    """A change to one member of a set display inside a traced class/module-level assignment."""
    st = u.node
    if not isinstance(st, (ast.Assign, ast.AnnAssign)) or st.value is None or not cmap.keyed_import:
        return None
    tg = st.targets if isinstance(st, ast.Assign) else [st.target]
    if len(tg) != 1 or not isinstance(tg[0], ast.Name):
        return None
    mod = A.module_name(path)
    origin = f"{mod}:{u.qual}:{tg[0].id}" if u.scope == "class" else f"{mod}::{tg[0].id}"
    if f"{origin}|created" not in cmap.dimport or f"{origin}|keys" in cmap.dimport:
        return None
    if not cmap.consumed_ok or origin not in cmap.runtime_origins():
        # no recorded runtime use of the traced set itself: it may only be
        # reached through a copy we could not see
        return None
    c0, c1 = cols
    hit = None
    for n in ast.walk(st.value):
        if isinstance(n, ast.Lambda):
            return None
        if isinstance(n, ast.Set):
            for j, e in enumerate(n.elts):
                if _span_contains(e, line, c0, c1):
                    hit = (n, j, e)
    if hit is None:
        return None
    n, j, e = hit
    if isinstance(e, ast.Starred):
        return None
    old = _key_reprs(e)
    if old is None:
        return None
    new = None
    if new_tree is not None:
        for m in ast.walk(new_tree):
            if isinstance(m, ast.Set) and m.lineno == n.lineno and m.col_offset == n.col_offset and len(m.elts) == len(n.elts):
                new = _key_reprs(m.elts[j]) if not isinstance(m.elts[j], ast.Starred) else None
    if new is None:
        return None
    reprs = old | new
    if any(f"{origin}|getk|{r}" in cmap.dimport for r in reprs):
        return None
    out = set()
    for r in reprs:
        sid = cmap.dkeys.get(f"{origin}|{r}")
        if sid is not None:
            out |= cmap.set(sid)
    sid = cmap.dall.get(f"{origin}|keys")
    if sid is not None:
        out |= cmap.set(sid)
    return out


def _new_key_reprs(new_tree, old_stmt, i):
    if new_tree is None:
        return None
    for n in ast.walk(new_tree):
        if isinstance(n, type(old_stmt)) and n.lineno == old_stmt.lineno and isinstance(n.value, ast.Dict):
            if len(n.value.keys) != len(old_stmt.value.keys) or n.value.keys[i] is None:
                return None
            return _key_reprs(n.value.keys[i])
    return None


def _uses_globals(fi):
    for n in ast.walk(fi.tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("globals", "vars", "locals") \
                and not n.args:
            return True
    return False


def _module_of(file):
    return A.module_name(file)


def module_rule(cmap, mod, names, visited, depth=0):
    """Tests that can observe a change to module-level names, following uses
    through imports and re-exports. None when something escapes the analysis."""
    du = cmap.du
    if depth > 8:
        return None
    out = set()
    for n in names:
        if n == "?" or n in cmap.du_strings or n.startswith("__"):
            return None
        keys, stack = set(), [f"{mod}:{n}"]
        while stack:
            k = stack.pop()
            if k in keys:
                continue
            keys.add(k)
            stack.extend(du["aliases"].get(k, []))
            m, name = k.split(":", 1)
            if not name.startswith("_"):
                for star in du["aliases"].get(f"{m}:*", []):
                    stack.append(f"{star.split(':', 1)[0]}:{name}")
        for k in keys:
            if k in visited:
                continue
            visited.add(k)
            for file, s0, s1, kind, extra in du["refs"].get(k, []):
                if kind == "f":
                    out |= cmap.span_tests(file, s0, s1)
                elif kind == "l":
                    out |= cmap.lambda_tests(file, s0)
                elif kind == "c":
                    qual, tg = extra
                    if "?" in tg:
                        return None
                    t = class_tests(cmap, f"{_module_of(file)}:{qual}", set(tg), None, file)
                    if t is None:
                        return None
                    out |= t
                elif kind == "m":
                    if "?" in extra:
                        return None
                    sub = module_rule(cmap, _module_of(file), set(extra), visited, depth + 1)
                    if sub is None:
                        return None
                    out |= sub
    return out


def class_tests(cmap, ckey, names, cls_node, path, depth=0):
    info = cmap.classes.get(ckey)
    if info is None or info["enum"]:
        return None
    if not (info["py"] or info["dialects"] or info["expr"]):
        return None
    if names & REGISTRY_ATTRS:
        return None
    out = set(cmap.recv_tests(ckey))
    for d in info["dialects"]:
        out |= cmap.recv_tests(d)
    if cls_node is not None:
        for n in ast.walk(cls_node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out |= cmap.span_tests(path, n.body[0].lineno, n.end_lineno)
    if depth < 3 and names:
        aliases = info.get("aliases", [])
        for name in names:
            for base, file, qual in cmap.xrefs.get(name, []):
                if not any(base == a or base.endswith("." + a) for a in aliases):
                    continue
                if not qual:
                    out |= cmap.file_tests(file)
                    continue
                k2 = f"{A.module_name(file)}:{qual}"
                if k2 == ckey:
                    continue
                sub = class_tests(cmap, k2, {name}, None, file, depth + 1)
                out |= sub if sub is not None else cmap.file_tests(file)
    return out


def _changed_cols(fp: P.FilePatch):
    """For 1:1 replaced lines, the changed byte-column range in the old line."""
    out = {}
    for h in fp.hunks:
        old = h.old_start if h.old_len else h.old_start + 1
        i = 0
        lines = h.lines
        while i < len(lines):
            if lines[i][0] == " ":
                old += 1
                i += 1
                continue
            dels, adds = [], []
            while i < len(lines) and lines[i][0] == "-":
                dels.append(lines[i][1].replace("\x00NONL", ""))
                i += 1
            while i < len(lines) and lines[i][0] == "+":
                adds.append(lines[i][1].replace("\x00NONL", ""))
                i += 1
            if len(dels) == len(adds):
                for k, (a, b) in enumerate(zip(dels, adds)):
                    ab, bb = a.encode(), b.encode()
                    pre = 0
                    while pre < min(len(ab), len(bb)) and ab[pre] == bb[pre]:
                        pre += 1
                    suf = 0
                    while suf < min(len(ab), len(bb)) - pre and ab[-1 - suf] == bb[-1 - suf]:
                        suf += 1
                    out[old + k] = (pre, max(pre, len(ab) - suf))
            old += len(dels)
    return out


_TREES = {}
_INDEX = {}


def _parse_cached(path, src):
    """Old (pinned) sources never change, so parse each file once per run.
    The cached tree is only read, never modified."""
    t = _TREES.get(path)
    if t is None:
        t = _TREES[path] = A.parse(src)
    return t


def _index_cached(path, src, tree):
    fi = _INDEX.get(path)
    if fi is None:
        fi = _INDEX[path] = A.FileIndex(src, tree)
    return fi


def _name_changed(n, new_src):
    try:
        tree = ast.parse(new_src)
    except SyntaxError:
        return True
    names = {x.name for x in ast.walk(tree) if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return n.name not in names


def _inserted_text(fp: P.FilePatch):
    out = {}
    for h in fp.hunks:
        old = h.old_start if h.old_len else h.old_start + 1
        buf = []
        for tag, txt in h.lines:
            if tag == "+":
                buf.append(txt.replace("\x00NONL", ""))
            else:
                if buf:
                    out.setdefault(old - 1, []).extend(buf)
                    buf = []
                old += 1
        if buf:
            out.setdefault(old - 1, []).extend(buf)
    return out


def analyze(patch_text: str, root: str, cmap: CovMap) -> Change:
    ch = Change()
    for fp in P.parse(patch_text):
        path = fp.path
        old_src = None
        if fp.old_path:
            try:
                with open(os.path.join(root, fp.old_path), encoding="utf-8", newline="") as f:
                    old_src = f.read()
            except (OSError, UnicodeDecodeError):
                old_src = None
        try:
            new_src = P.apply(old_src, fp) if not fp.binary else None
        except Exception:
            new_src = None
            ch.notes.append(f"apply failed {path}")
        ch.new_files[path] = new_src
        if fp.old_path and fp.new_path and fp.old_path != fp.new_path:
            ch.new_files[fp.old_path] = None
        if path.endswith(".py") and not fp.binary:
            if fp.old_path and fp.new_path and fp.old_path != fp.new_path:
                ch.add(cmap.file_tests(fp.old_path), "renamed")
                ch.broad = True
                continue
            analyze_py(path, old_src, new_src, fp, cmap, ch)
        else:
            analyze_data(fp, old_src, new_src, cmap, ch)
    return ch


def analyze_data(fp, old_src, new_src, cmap: CovMap, ch: Change):
    path = fp.path
    for p in {fp.old_path, fp.new_path} - {None}:
        sid = cmap.opened.get(p)
        if sid is not None:
            ch.add(cmap.set(sid), f"reads {p}")
        if fp.old_path is None or fp.new_path is None or fp.old_path != fp.new_path:
            # a file appearing or disappearing matters to tests that list its directory
            d = os.path.dirname(p)
            while True:
                sid = cmap.opened.get(d + "/")
                if sid is not None:
                    ch.add(cmap.set(sid), f"lists {d}/")
                if not d:
                    break
                d = os.path.dirname(d)
    base = os.path.basename(path)
    if base in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py") and os.path.dirname(path) == "":
        if _config_breaks_pytest(base, old_src, new_src):
            ch.add(cmap.all, "pytest config")
            ch.broad = True


def _config_breaks_pytest(base, old_src, new_src):
    """pytest reads these at startup; a broken file or new pytest settings affect every test."""
    if new_src is None:
        return base in ("pytest.ini", "tox.ini")
    if base == "pyproject.toml":
        try:
            import tomllib

            old = tomllib.loads(old_src or "")
            new = tomllib.loads(new_src)
        except Exception:  # noqa: BLE001
            return True
        return old.get("tool", {}).get("pytest") != new.get("tool", {}).get("pytest")
    if base in ("setup.cfg", "tox.ini", "pytest.ini"):
        import configparser

        def sections(text):
            cp = configparser.RawConfigParser(strict=False)
            cp.read_string(text or "")
            return {k: dict(cp[k]) for k in cp.sections() if "pytest" in k}

        try:
            return sections(old_src) != sections(new_src)
        except Exception:  # noqa: BLE001
            return True
    return True
