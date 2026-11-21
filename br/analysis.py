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


def same_ast(old_src, new_src) -> bool | None:
    """True when the two sources compile to the same AST (comments/format only)."""
    a, b = parse(old_src), parse(new_src)
    if a is None or b is None:
        return None
    return ast.dump(a, include_attributes=False) == ast.dump(b, include_attributes=False)


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
    def __init__(self, src):
        self.src = src
        self.tree = parse(src)
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
