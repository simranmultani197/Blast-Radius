"""Which lines of the pinned tree a change can influence, at statement granularity."""
from __future__ import annotations

import ast
import io
import tokenize

COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
            ast.Try, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
if hasattr(ast, "TryStar"):
    COMPOUND = COMPOUND + (ast.TryStar,)
if hasattr(ast, "Match"):
    COMPOUND = COMPOUND + (ast.Match,)


def parse(src):
    try:
        return ast.parse(src)
    except (SyntaxError, ValueError):
        return None


_BODY_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _is_doc(node):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def ast_equal(a, b, skip_docstrings=False):
    """Structural equality ignoring positions; optionally ignoring docstrings."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        for f in a._fields:
            x, y = getattr(a, f, None), getattr(b, f, None)
            if skip_docstrings and f == "body" and isinstance(a, _BODY_OWNERS):
                x = x[1:] if x and _is_doc(x[0]) else x
                y = y[1:] if y and _is_doc(y[0]) else y
            if not ast_equal(x, y, skip_docstrings):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(ast_equal(x, y, skip_docstrings) for x, y in zip(a, b))
    return a == b


def same_ast(old_src, new_src, old_tree=None, new_tree=None) -> bool | None:
    """True when the two sources compile to the same AST, ignoring comments,
    formatting and docstrings (docstrings only reach tests through doctests,
    which pytest does not collect here; sqlglot reads __doc__ only to default it)."""
    a = old_tree if old_tree is not None else parse(old_src)
    b = new_tree if new_tree is not None else parse(new_src)
    if a is None or b is None:
        return None
    return ast_equal(a, b, skip_docstrings=True)


class Unit:
    __slots__ = ("start", "end", "kind", "scope", "body_span", "node", "qual", "cls")

    def __init__(self, start, end, kind, scope, body_span=None, node=None, qual=None, cls=None):
        self.start, self.end, self.kind, self.scope = start, end, kind, scope
        self.body_span = body_span
        self.node = node
        self.qual = qual  # qualname of the enclosing class when scope == "class"
        self.cls = cls    # that ClassDef node

    def __repr__(self):
        return f"Unit({self.kind} {self.start}-{self.end} in {self.scope})"


def _header_end(node):
    """Last line of a compound statement's header (before its body)."""
    body = getattr(node, "body", None)
    if isinstance(node, ast.Match):
        return node.subject.end_lineno
    if isinstance(node, ast.Try) or (hasattr(ast, "TryStar") and isinstance(node, ast.TryStar)):
        return node.lineno
    if body:
        first = body[0]
        start = first.lineno
        if isinstance(first, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and first.decorator_list:
            start = min(d.lineno for d in first.decorator_list)
        return max(node.lineno, start - 1) if start > node.lineno else node.lineno
    return node.end_lineno


def build_units(tree):
    """List of Units: every simple statement and every compound header.

    scope is 'module', 'class' or 'function' (where the statement runs).
    """
    units = []

    def visit(stmts, scope, func=None, qual=None, cls=None):
        for node in stmts:
            start = node.lineno
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
                start = min(d.lineno for d in node.decorator_list)
            if isinstance(node, COMPOUND):
                hend = _header_end(node)
                body_span = None
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    body_span = (node.body[0].lineno, node.end_lineno)
                kind = type(node).__name__
                units.append(Unit(start, hend, kind, scope, body_span, node, qual, cls))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(node.body, "function", node)
                elif isinstance(node, ast.ClassDef):
                    if scope == "function":
                        visit(node.body, "function", func)
                    else:
                        visit(node.body, "class", func, f"{qual}.{node.name}" if qual else node.name, node)
                else:
                    for fld in ("body", "orelse", "finalbody"):
                        visit(getattr(node, fld, []) or [], scope, func, qual, cls)
                    for h in getattr(node, "handlers", []) or []:
                        units.append(Unit(h.lineno, h.body[0].lineno - 1 if h.body[0].lineno > h.lineno else h.lineno,
                                          "Handler", scope, None, h, qual, cls))
                        visit(h.body, scope, func, qual, cls)
                    for c in getattr(node, "cases", []) or []:
                        units.append(Unit(c.pattern.lineno, c.body[0].lineno - 1 if c.body[0].lineno > c.pattern.lineno else c.pattern.lineno,
                                          "Case", scope, None, c, qual, cls))
                        visit(c.body, scope, func, qual, cls)
            else:
                units.append(Unit(start, node.end_lineno, type(node).__name__, scope, None, node, qual, cls))

    visit(tree.body, "module")
    return units


def code_lines(src):
    """Line numbers holding something other than blanks/comments."""
    lines = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                            tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            for ln in range(tok.start[0], tok.end[0] + 1):
                lines.add(ln)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return lines


class FileIndex:
    def __init__(self, src, tree=None):
        self.src = src
        self.tree = tree if tree is not None else parse(src)
        self.units = build_units(self.tree) if self.tree else []
        self.code = code_lines(src)
        self.nlines = src.count("\n") + 1

    def smallest_unit(self, line):
        best = None
        for u in self.units:
            if u.start <= line <= u.end:
                if best is None or (u.end - u.start) <= (best.end - best.start):
                    best = u
        return best

    def enclosing_units(self, line):
        """Compound statements whose full extent contains line (outermost first)."""
        out = []
        for u in self.units:
            n = u.node
            if n is not None and isinstance(n, COMPOUND):
                start = u.start
                if start <= line <= n.end_lineno:
                    out.append(u)
        return out

    def prev_code_line(self, line):
        while line >= 1 and line not in self.code:
            line -= 1
        return line if line >= 1 else None

    def next_code_line(self, line):
        while line <= self.nlines and line not in self.code:
            line += 1
        return line if line <= self.nlines else None


def lambdas_containing(tree, line, c0, c1):
    """Lambdas whose *body* contains the changed span (line, byte cols c0..c1)."""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Lambda):
            b = n.body
            if not (b.lineno <= line <= b.end_lineno):
                continue
            if line == b.lineno and c0 < b.col_offset:
                continue
            if line == b.end_lineno and c1 > b.end_col_offset:
                continue
            out.append(n)
    return out


def assigned_names(node):
    names = set()
    targets = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                names.add(n.id)
    return names


def module_name(path):
    mod = path[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


def single_compare_swap(a, b):
    """If the only difference between two module ASTs is one comparison's operator
    (same operands, single operator), return (lineno, col, old_op, new_op) of the
    comparison in `a`; otherwise None."""
    if a is None or b is None:
        return None
    diffs = []

    def rec(x, y):
        if len(diffs) > 1:
            return
        if type(x) is not type(y):
            diffs.append(None)
            return
        if isinstance(x, ast.Compare):
            if len(x.ops) == len(y.ops) == 1 and type(x.ops[0]) is not type(y.ops[0]):
                if ast_equal(x.left, y.left) and ast_equal(x.comparators[0], y.comparators[0]):
                    diffs.append((x.lineno, x.col_offset, type(x.ops[0]).__name__, type(y.ops[0]).__name__))
                else:
                    diffs.append(None)
                return
        if isinstance(x, ast.AST):
            for f in x._fields:
                rec(getattr(x, f, None), getattr(y, f, None))
        elif isinstance(x, list):
            if len(x) != len(y):
                diffs.append(None)
                return
            for i, j in zip(x, y):
                rec(i, j)
        elif x != y:
            diffs.append(None)

    rec(a, b)
    return diffs[0] if len(diffs) == 1 else None


def single_diff(a, b):
    """If old/new module ASTs differ in exactly one place, return
    (old_node, new_node, ancestors) for the smallest differing node pair
    (ancestors = old-tree path from the module down to, excluding, old_node)."""
    if a is None or b is None:
        return None
    found = []

    def rec(x, y, path):
        if len(found) > 1:
            return
        if type(x) is not type(y):
            found.append((x, y, path))
            return
        if isinstance(x, ast.AST):
            # a node whose own primitive fields differ is the diff point
            for f in x._fields:
                vx, vy = getattr(x, f, None), getattr(y, f, None)
                if not isinstance(vx, (ast.AST, list)) and vx != vy:
                    found.append((x, y, path))
                    return
                if isinstance(vx, ast.AST) and isinstance(vy, ast.AST) and type(vx) is not type(vy) \
                        and isinstance(vx, (ast.boolop, ast.operator, ast.unaryop, ast.cmpop)):
                    found.append((x, y, path))
                    return
            for f in x._fields:
                rec(getattr(x, f, None), getattr(y, f, None), path + [x])
        elif isinstance(x, list):
            if len(x) != len(y):
                found.append((path[-1] if path else None, None, path[:-1]))
                return
            for i, j in zip(x, y):
                rec(i, j, path)
                if len(found) > 1:
                    return
        elif x != y:
            found.append((path[-1] if path else None, None, path[:-1]))

    rec(a, b, [])
    if len(found) != 1 or found[0][0] is None:
        return None
    return found[0]


BOOL_CONTEXT = (ast.If, ast.While, ast.Assert)


def in_bool_context(node, parent):
    if isinstance(parent, BOOL_CONTEXT) and parent.test is node:
        return True
    if isinstance(parent, ast.IfExp) and parent.test is node:
        return True
    if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
        return True
    if isinstance(parent, ast.comprehension) and node in parent.ifs:
        return True
    return False
