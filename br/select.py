"""Patch -> candidate tests, using the coverage map built from the pinned tree."""
from __future__ import annotations

import ast
import gzip
import json
import os

from . import analysis as A
from . import patch as P


class CovMap:
    def __init__(self, path):
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
        self.all = frozenset(range(len(self.tests)))

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
        d = self.files.get(path)
        out = set()
        if d:
            for sid in set(d.values()):
                out |= self.set(sid)
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
    same = A.same_ast(old_src, new_src)
    if same:
        ch.notes.append(f"{path}: same AST")
        return
    if A.parse(new_src) is None:
        ch.syntax_error = True
        ch.add(cmap.file_tests(path), f"syntax error {path}")
        ch.broad = True
        return
    fi = A.FileIndex(old_src)
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


REGISTRY_ATTRS = {"_sql_names", "key", "__name__", "__qualname__", "__slots__", "__init_subclass__"}


def _scope_rule(path, fi, u, cmap, ch, why, attrs=None):
    """A statement that runs at import time (class body or module level)."""
    if u.scope == "class" and u.qual and cmap.classes:
        ckey = f"{A.module_name(path)}:{u.qual}"
        names = A.assigned_names(u.node) if attrs is None else attrs
        tests = class_tests(cmap, ckey, names, u.cls, path)
        if tests is not None:
            ch.add(tests, why + f" class {u.qual}")
            ch.notes.append(f"class rule {ckey} {sorted(names)}")
            return
    ch.add(cmap.span_tests(path, u.start, u.end), why + " importers")
    ch.broad = True


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
    base = os.path.basename(path)
    if base in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini"):
        text = "\n".join(t for h in fp.hunks for _, t in h.lines)
        if "pytest" in text or "[tool.pytest" in text or "addopts" in text:
            ch.add(cmap.all, "pytest config")
            ch.broad = True
