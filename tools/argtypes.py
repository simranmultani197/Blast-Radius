"""Static index of how arg_types dicts feed each other (build step).

Expr.__init_subclass__ turns cls.arg_types into required_args; a class whose
arg_types literal spreads `**Other.arg_types` copies Other's values. Any other
read of arg_types values outside functions (import time), or any
reassignment of arg_types / required_args, marks the index unusable.
"""
import ast
import os

KEY_ONLY_CALLS = {"set", "tuple", "list", "len", "zip", "sorted", "frozenset", "iter", "enumerate"}


def build(root):
    spreads = {}      # source class name -> [dependent class keys]
    unsafe = []       # reasons the value-flip rule must not be used
    value_readers = []  # runtime reads that may return values (then key lookups count)
    for dp, _, fns in os.walk(os.path.join(root, "tests")):
        for fn in sorted(fns):
            if fn.endswith(".py"):
                path = os.path.join(dp, fn)
                _runtime_reads(ast.parse(open(path, encoding="utf-8").read()), os.path.relpath(path, root), value_readers)
    for dp, _, fns in os.walk(os.path.join(root, "sqlglot")):
        for fn in sorted(fns):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dp, fn)
            rel = os.path.relpath(path, root)
            mod = rel[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            tree = ast.parse(open(path, encoding="utf-8").read())
            _scan(tree, mod, rel, spreads, unsafe)
            _runtime_reads(tree, rel, value_readers)
    return {"spreads": {k: sorted(set(v)) for k, v in spreads.items()}, "unsafe": unsafe[:50],
            "value_readers": value_readers[:50]}


def _keys_only(n, par, fn, depth=0):
    """Is this load of an arg_types dict used only for its keys?"""
    p = par.get(n)
    if isinstance(p, (ast.For, ast.comprehension)) and p.iter is n:
        return True
    if isinstance(p, ast.Compare) and n in p.comparators and all(isinstance(o, (ast.In, ast.NotIn)) for o in p.ops):
        return True
    if isinstance(p, ast.Call) and n in p.args and getattr(p.func, "id", None) in KEY_ONLY_CALLS:
        return True
    if isinstance(p, ast.IfExp) and n is not p.test:
        return _keys_only(p, par, fn, depth)
    if isinstance(p, ast.Assign) and len(p.targets) == 1 and isinstance(p.targets[0], ast.Name) and depth < 2 and fn:
        name = p.targets[0].id
        uses = [x for x in ast.walk(fn) if isinstance(x, ast.Name) and x.id == name]
        return all(x is p.targets[0] or (isinstance(x.ctx, ast.Load) and _keys_only(x, par, fn, depth + 1)) for x in uses)
    return False


def _runtime_reads(tree, rel, out):
    par = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            par[c] = n
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and n.value == "arg_types":
            out.append(f"{rel}:{n.lineno} 'arg_types' string")
        if not (isinstance(n, ast.Attribute) and n.attr == "arg_types" and isinstance(n.ctx, ast.Load)):
            continue
        fn = par.get(n)
        while fn is not None and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            fn = par.get(fn)
        if fn is None:
            continue  # import time: _scan
        if isinstance(fn, ast.FunctionDef) and fn.name == "__init_subclass__":
            continue  # required_args
        if not _keys_only(n, par, fn):
            out.append(f"{rel}:{n.lineno} value read")


def _scan(tree, mod, rel, spreads, unsafe):
    par = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            par[c] = n

    def qual_of(n):
        names, x = [], par.get(n)
        while x is not None:
            if isinstance(x, ast.ClassDef):
                names.append(x.name)
            elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return None
            x = par.get(x)
        return ".".join(reversed(names))

    def in_func(n):
        x = par.get(n)
        while x is not None:
            if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return x
            x = par.get(x)
        return None

    for n in ast.walk(tree):
        # class-body `arg_types = {...}`
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(par.get(n), ast.ClassDef):
            tg = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(isinstance(t, ast.Name) and t.id == "arg_types" for t in tg):
                qual = qual_of(n)
                v = n.value
                if not isinstance(v, ast.Dict):
                    continue  # built from module-level names: any `.arg_types` in it is flagged below
                for k, val in zip(v.keys, v.values):
                    if k is None:
                        if isinstance(val, ast.Attribute) and val.attr == "arg_types":
                            src = val.value.attr if isinstance(val.value, ast.Attribute) else getattr(val.value, "id", None)
                            if src is None:
                                unsafe.append(f"{rel}:{n.lineno} spread of unknown arg_types")
                            else:
                                spreads.setdefault(src, []).append(f"{mod}:{qual}")

            if any(isinstance(t, ast.Name) and t.id == "required_args" for t in tg) and qual_of(n) != "Expr":
                unsafe.append(f"{rel}:{n.lineno} required_args assigned")
            continue
        if isinstance(n, ast.Attribute) and n.attr in ("arg_types", "required_args"):
            p = par.get(n)
            fn = in_func(n)
            if not isinstance(n.ctx, ast.Load):
                if not (n.attr == "required_args" and fn is not None and fn.name == "__init_subclass__"):
                    unsafe.append(f"{rel}:{n.lineno} {n.attr} stored")
                continue
            if n.attr == "required_args":
                if fn is None or fn.name not in ("error_messages", "__init_subclass__"):
                    unsafe.append(f"{rel}:{n.lineno} required_args read in {getattr(fn, 'name', 'module')}")
                continue
            if fn is not None:
                continue  # runtime reads: covered by the dict tracer (dkeys / dall)
            # import-time read outside a function: keys only, or a recognised spread
            if isinstance(p, ast.Dict) or (isinstance(p, ast.Call) and getattr(p.func, "id", None) in KEY_ONLY_CALLS) \
                    or (isinstance(p, ast.Compare) and n in p.comparators) or isinstance(p, (ast.For, ast.comprehension)):
                if isinstance(p, ast.Dict) and n not in p.values:
                    unsafe.append(f"{rel}:{n.lineno} arg_types as dict key")
                continue
            unsafe.append(f"{rel}:{n.lineno} arg_types value read at import")
