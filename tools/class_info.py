"""Import every sqlglot module and describe its classes for the selector.

    python tools/class_info.py /out/classes.json     (run with /src on sys.path)
"""
import enum
import importlib
import inspect
import json
import pkgutil
import sys

import sqlglot
from sqlglot.dialects.dialect import Dialect
from sqlglot.expressions.core import Expression


def key(c):
    return f"{c.__module__}:{c.__qualname__}"


def _sub(a, b):
    try:
        return issubclass(a, b)
    except TypeError:
        return False


def main():
    mods = []
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            mods.append(importlib.import_module(info.name))
        except Exception as e:  # noqa: BLE001
            print("skip", info.name, e, file=sys.stderr)
    classes = {}

    def walk(c):
        k = key(c)
        if k in classes or not c.__module__.startswith("sqlglot"):
            return
        classes[k] = c
        for v in vars(c).values():
            if isinstance(v, type):
                walk(v)

    for m in mods:
        for v in vars(m).values():
            if isinstance(v, type):
                walk(v)
    # every subclass reachable at runtime too
    stack = list(classes.values())
    while stack:
        c = stack.pop()
        try:
            subs = c.__subclasses__()
        except TypeError:
            subs = []
        for s in subs:
            if s.__module__.startswith("sqlglot") and key(s) not in classes:
                classes[key(s)] = s
                stack.append(s)

    groups = {}
    for k, c in classes.items():
        if _sub(c, Dialect):
            g = set()
            for base in c.__mro__:
                if base.__module__.startswith("sqlglot"):
                    g.add(key(base))
            for name in dir(c):
                if not name.endswith("_class") and name not in ("Tokenizer", "Parser", "Generator", "JSONPathTokenizer"):
                    continue
                v = getattr(c, name, None)
                if isinstance(v, type):
                    for base in v.__mro__:
                        if base.__module__.startswith("sqlglot"):
                            g.add(key(base))
            groups[k] = sorted(g)
    aliases = {k: {c.__name__, c.__qualname__} for k, c in classes.items()}
    for k, c in classes.items():
        if _sub(c, Dialect):
            for name in ("Tokenizer", "Parser", "Generator", "JSONPathTokenizer", "tokenizer_class",
                         "parser_class", "generator_class", "jsonpath_tokenizer_class"):
                v = getattr(c, name, None)
                if isinstance(v, type) and key(v) in aliases:
                    aliases[key(v)].add(f"{c.__name__}.{name}")
    member_of = {}
    for d, g in groups.items():
        for k in g:
            member_of.setdefault(k, []).append(d)
    out = {}
    for k, c in classes.items():
        has_py = any(
            inspect.isfunction(v) or isinstance(v, (classmethod, staticmethod, property))
            for b in c.__mro__ if b.__module__.startswith("sqlglot")
            for v in vars(b).values()
        )
        out[k] = {
            "enum": _sub(c, enum.Enum),
            "expr": _sub(c, Expression),
            "py": has_py,
            "dialects": sorted(member_of.get(k, [])),
            "aliases": sorted(aliases[k]),
            "subclasses": sorted(key(s) for s in classes.values() if s is not c and _sub(s, c)),
        }
    json.dump(out, open(sys.argv[1], "w"))
    print(f"{len(out)} classes, {len(groups)} dialects", file=sys.stderr)


if __name__ == "__main__":
    main()
