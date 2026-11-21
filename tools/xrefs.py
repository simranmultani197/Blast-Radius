"""Static index of class-level / module-level attribute reads like `MySQL.Parser.FUNCTIONS`.

Returns {attr: [[chain, file, class_qualname_or_null], ...]} for every attribute
access that runs at import time (class bodies and module level), so a change to
a class attribute can follow copies like {**Other.FUNCTIONS, ...}.
"""
import ast
import os


def chain_text(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def scan_file(path, rel, out):
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError:
        return

    def visit_body(stmts, qual):
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in st.decorator_list:
                    scan_expr(d, qual)
                for d in st.args.defaults + [x for x in st.args.kw_defaults if x is not None]:
                    scan_expr(d, qual)
                continue
            if isinstance(st, ast.ClassDef):
                for b in st.bases + [k.value for k in st.keywords] + st.decorator_list:
                    scan_expr(b, qual)
                visit_body(st.body, f"{qual}.{st.name}" if qual else st.name)
                continue
            scan_expr(st, qual)

    def scan_expr(node, qual):
        for n in ast.walk(node):
            if isinstance(n, ast.Lambda):
                continue
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
                base = chain_text(n.value)
                if base:
                    out.setdefault(n.attr, []).append([base, rel, qual])

    visit_body(tree.body, None)


def build(root):
    out = {}
    for d in ("sqlglot", "tests"):
        for dp, _, fns in os.walk(os.path.join(root, d)):
            for fn in fns:
                if fn.endswith(".py"):
                    p = os.path.join(dp, fn)
                    scan_file(p, os.path.relpath(p, root), out)
    # dedupe
    return {k: sorted({tuple(x) if x[2] else (x[0], x[1], "") for x in v}) for k, v in out.items()}
