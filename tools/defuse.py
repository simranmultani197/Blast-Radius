"""Static def-use index for module-level names in the pinned tree.

build(root) -> {
  "refs": {"mod:name": [[file, start, end, kind, extra], ...]},
  "aliases": {"mod:name": ["mod2:name2", ...]},   # re-exports / from-imports at module level
  "strings": [identifier-like string literals],
  "dynamic": [modules using globals()/vars()/__dict__/__getattr__],
  "modfiles": {module: file},
}
kind: "f" function statement span, "l" lambda (start line), "c" class-level
statement (extra=[class_qual, targets]), "m" module-level statement (extra=targets).
"""
import ast
import os
import re

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def module_of(rel):
    m = rel[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


def build(root):
    files = {}
    for d in ("sqlglot", "tests"):
        for dp, _, fns in os.walk(os.path.join(root, d)):
            for fn in fns:
                if fn.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dp, fn), root)
                    files[module_of(rel)] = rel
    trees = {}
    for mod, rel in files.items():
        try:
            trees[mod] = ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
        except SyntaxError:
            pass
    refs, aliases, strings, dynamic = {}, {}, set(), set()

    def resolve_rel(mod, level, name, is_pkg):
        if not level:
            return name
        parts = mod.split(".")
        base = parts if is_pkg else parts[:-1]
        base = base[: len(base) - (level - 1)]
        return ".".join(base + ([name] if name else []))

    for mod, tree in trees.items():
        rel = files[mod]
        is_pkg = rel.endswith("__init__.py")
        # module-level bindings: name -> ("mod", M) | ("name", M, N)
        binds = {}

        def import_binds(node, into):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        into[a.asname] = ("mod", a.name)
                    else:
                        into[a.name.split(".")[0]] = ("mod", a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                src = resolve_rel(mod, node.level, node.module, is_pkg)
                for a in node.names:
                    if a.name == "*":
                        into.setdefault("*", []).append(src)
                        continue
                    local = a.asname or a.name
                    if f"{src}.{a.name}" in files:
                        into[local] = ("mod", f"{src}.{a.name}")
                    else:
                        into[local] = ("name", src, a.name)

        for st in tree.body:
            for node in ast.walk(st) if isinstance(st, (ast.If, ast.Try)) else [st]:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    import_binds(node, binds)
        for local, b in binds.items():
            if local == "*":
                for src in b:
                    aliases.setdefault(f"{src}:*", []).append(f"{mod}:*")
                continue
            if b[0] == "name":
                aliases.setdefault(f"{b[1]}:{b[2]}", []).append(f"{mod}:{local}")

        def target_of(name, local_scope):
            """What a free Name refers to: ("mod:name") key or None."""
            if name in local_scope:
                b = local_scope[name] if isinstance(local_scope, dict) else None
                if b is None:
                    return None
                return f"{b[1]}:{b[2]}" if b[0] == "name" else None
            b = binds.get(name)
            if b is None:
                return f"{mod}:{name}"
            if b[0] == "name":
                return f"{b[1]}:{b[2]}"
            return None  # a module object

        def attr_target(node, local_scope):
            # X.attr where X (possibly a dotted chain) names a module
            chain = []
            cur = node
            while isinstance(cur, ast.Attribute):
                chain.append(cur.attr)
                cur = cur.value
            if not isinstance(cur, ast.Name):
                return None
            if cur.id in local_scope:
                b = local_scope[cur.id] if isinstance(local_scope, dict) else None
            else:
                b = binds.get(cur.id)
            if b is None or b[0] != "mod":
                return None
            chain.reverse()
            modname = b[1]
            # walk down submodules
            i = 0
            while i < len(chain) - 1 and f"{modname}.{chain[i]}" in files:
                modname = f"{modname}.{chain[i]}"
                i += 1
            return f"{modname}:{chain[i]}"

        def add(key, item):
            refs.setdefault(key, []).append(item)

        def locals_of(fn):
            names = {}
            a = fn.args
            for x in a.posonlyargs + a.args + a.kwonlyargs + ([a.vararg] if a.vararg else []) + ([a.kwarg] if a.kwarg else []):
                names[x.arg] = None
            if isinstance(fn, ast.Lambda):
                return names
            glob = set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Global):
                    glob.update(n.names)
            for n in ast.walk(fn):
                if n is fn:
                    continue
                if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                    names[n.id] = None
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names[n.name] = None
                elif isinstance(n, (ast.Import, ast.ImportFrom)):
                    tmp = {}
                    import_binds(n, tmp)
                    tmp.pop("*", None)
                    for k, v in tmp.items():
                        names.setdefault(k, v)
                elif isinstance(n, ast.ExceptHandler) and n.name:
                    names[n.name] = None
            for g in glob:
                names.pop(g, None)
            return names

        def scan_expr(expr, scope_locals, ctx):
            """ctx: callable(node) -> item to record for a reference found in expr."""
            for n in ast.walk(expr):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    k = target_of(n.id, scope_locals)
                    if k:
                        add(k, ctx(n))
                    if n.id in ("globals", "vars", "locals", "__import__"):
                        dynamic.add(mod)
                elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
                    k = attr_target(n, scope_locals)
                    if k:
                        add(k, ctx(n))
                    if n.attr in ("__dict__",):
                        dynamic.add(mod)
                elif isinstance(n, ast.Constant) and isinstance(n.value, str) and IDENT.match(n.value):
                    strings.add(n.value)

        def visit_func(fn, outer_locals):
            loc = {**outer_locals, **locals_of(fn)}
            for st in fn.body:
                visit_stmt_in_func(st, loc)

        def visit_stmt_in_func(st, loc):
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for e in st.decorator_list + st.args.defaults + [d for d in st.args.kw_defaults if d]:
                    scan_func_expr(e, loc, st.lineno, st.lineno)
                visit_func(st, loc)
                return
            if isinstance(st, ast.ClassDef):
                for x in st.body:
                    visit_stmt_in_func(x, loc)
                for e in st.bases + st.decorator_list:
                    scan_func_expr(e, loc, st.lineno, st.lineno)
                return
            children = []
            for fld in ("body", "orelse", "finalbody", "handlers", "cases"):
                for c in getattr(st, fld, []) or []:
                    children.append(c)
            if children:
                # header part of a compound statement
                hdr_end = max(st.lineno, (children[0].lineno - 1) if hasattr(children[0], "lineno") else st.lineno)
                for fld, val in ast.iter_fields(st):
                    if fld in ("body", "orelse", "finalbody", "handlers", "cases"):
                        continue
                    for v in (val if isinstance(val, list) else [val]):
                        if isinstance(v, ast.AST):
                            scan_func_expr(v, loc, st.lineno, hdr_end)
                for c in children:
                    if isinstance(c, ast.ExceptHandler):
                        if c.type is not None:
                            scan_func_expr(c.type, loc, c.lineno, c.lineno)
                        for x in c.body:
                            visit_stmt_in_func(x, loc)
                    elif isinstance(c, getattr(ast, "match_case", ())):
                        for x in c.body:
                            visit_stmt_in_func(x, loc)
                    else:
                        visit_stmt_in_func(c, loc)
                return
            scan_func_expr(st, loc, st.lineno, st.end_lineno)

        def scan_func_expr(expr, loc, s0, s1):
            # references inside nested lambdas are recorded against the lambda
            lam_nodes = [n for n in ast.walk(expr) if isinstance(n, ast.Lambda)]
            inner = set()
            for lam in lam_nodes:
                for n in ast.walk(lam.body):
                    inner.add(id(n))
                ll = {**loc, **locals_of(lam)}
                scan_expr(lam.body, ll, lambda n, lam=lam: [rel, lam.lineno, lam.lineno, "l", None])
            for n in ast.walk(expr):
                if id(n) in inner or isinstance(n, ast.Lambda):
                    continue
                if isinstance(n, (ast.Name, ast.Attribute, ast.Constant)):
                    scan_expr_single(n, loc, [rel, s0, s1, "f", None])

        def scan_expr_single(n, loc, item):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                k = target_of(n.id, loc)
                if k:
                    add(k, item)
                if n.id in ("globals", "vars", "locals", "__import__"):
                    dynamic.add(mod)
            elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
                k = attr_target(n, loc)
                if k:
                    add(k, item)
                if n.attr == "__dict__":
                    dynamic.add(mod)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and IDENT.match(n.value):
                strings.add(n.value)

        def targets(st):
            out = set()
            tl = []
            if isinstance(st, ast.Assign):
                tl = st.targets
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                tl = [st.target]
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return {st.name}
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                for al in st.names:
                    out.add((al.asname or al.name).split(".")[0])
                return out
            for t in tl:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        out.add(n.id)
                    elif not isinstance(t, (ast.Name, ast.Tuple, ast.List)):
                        out.add("?")
            return out or {"?"}

        def visit_class(cls, qual):
            for st in cls.body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for e in st.decorator_list + st.args.defaults + [d for d in st.args.kw_defaults if d]:
                        scan_expr(e, set(), lambda n, st=st: [rel, st.lineno, st.lineno, "c", [qual, [st.name]]])
                    visit_func(st, {})
                elif isinstance(st, ast.ClassDef):
                    for e in st.bases + st.decorator_list + [k.value for k in st.keywords]:
                        scan_expr(e, set(), lambda n, st=st: [rel, st.lineno, st.lineno, "c", [qual, [st.name]]])
                    visit_class(st, f"{qual}.{st.name}")
                else:
                    tg = sorted(targets(st))
                    lam_inner = set()
                    for lam in [n for n in ast.walk(st) if isinstance(n, ast.Lambda)]:
                        for n in ast.walk(lam.body):
                            lam_inner.add(id(n))
                        scan_expr(lam.body, locals_of(lam), lambda n, lam=lam: [rel, lam.lineno, lam.lineno, "l", None])
                    for n in ast.walk(st):
                        if id(n) in lam_inner or isinstance(n, ast.Lambda):
                            continue
                        if isinstance(n, (ast.Name, ast.Attribute, ast.Constant)):
                            scan_expr_single(n, set(), [rel, st.lineno, st.end_lineno, "c", [qual, tg]])

        for st in tree.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for e in st.decorator_list + st.args.defaults + [d for d in st.args.kw_defaults if d]:
                    scan_expr(e, set(), lambda n, st=st: [rel, st.lineno, st.lineno, "m", [st.name]])
                visit_func(st, {})
            elif isinstance(st, ast.ClassDef):
                for e in st.bases + st.decorator_list + [k.value for k in st.keywords]:
                    scan_expr(e, set(), lambda n, st=st: [rel, st.lineno, st.lineno, "m", [st.name]])
                visit_class(st, st.name)
            else:
                tg = sorted(targets(st)) if not isinstance(st, (ast.If, ast.Try, ast.For, ast.While, ast.With)) else ["?"]
                lam_inner = set()
                for lam in [n for n in ast.walk(st) if isinstance(n, ast.Lambda)]:
                    for n in ast.walk(lam.body):
                        lam_inner.add(id(n))
                    scan_expr(lam.body, locals_of(lam), lambda n, lam=lam: [rel, lam.lineno, lam.lineno, "l", None])
                for n in ast.walk(st):
                    if id(n) in lam_inner or isinstance(n, ast.Lambda):
                        continue
                    if isinstance(n, (ast.Name, ast.Attribute, ast.Constant)):
                        scan_expr_single(n, set(), [rel, st.lineno, st.end_lineno, "m", tg])
                # functions defined inside module-level if/try blocks
                for n in ast.walk(st):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not st:
                        visit_func(n, {})
    # dedupe
    out_refs = {}
    for k, v in refs.items():
        seen, lst = set(), []
        for item in v:
            key = (item[0], item[1], item[2], item[3], repr(item[4]))
            if key not in seen:
                seen.add(key)
                lst.append(item)
        out_refs[k] = lst
    return {"refs": out_refs, "aliases": aliases, "strings": sorted(strings), "dynamic": sorted(dynamic),
            "modfiles": files}


if __name__ == "__main__":
    import json
    import sys

    d = build(sys.argv[1])
    print(len(d["refs"]), "keys;", sum(map(len, d["refs"].values())), "refs; dynamic", d["dynamic"][:10])
    for k in sys.argv[2:]:
        print(k, d["refs"].get(k, [])[:10], "aliases", d["aliases"].get(k))
