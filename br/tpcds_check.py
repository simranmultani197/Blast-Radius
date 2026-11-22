"""Drop test_execute_tpcds when a patch cannot change what it compares.

The test runs all 99 TPC-DS queries; for `# execute` queries it plans and runs
them with sqlglot's Python executor on fixture data and compares the rows with
duckdb running the duckdb SQL sqlglot generates. It fails only when one of
those inputs changes or something raises. A patch is accepted when:

1. it edits only statements inside function bodies of sqlglot/*.py, outside
   sqlglot/executor/ (no import-time code, signatures, new files);
2. none of those statements ever runs inside PythonExecutor (code generation
   for the plan and the data processing) or at import, per
   tools/tpcds_lines.py over the real test data (maps/tpcds_lines.json);
3. br/tpcds_probe.py, run on a copy of the tree with the patch applied, gives
   the same per-query fingerprint as the pinned tree (parse and pickle without
   errors, tables, plan steps with expressions/types/meta, duckdb SQL). When
   only the plans of a few queries with a small known run time differ, it runs
   those queries on the fixture data with the patched code and requires the
   same rows as the pinned tree (maps/tpcds_rows.json, computed at build).

Then everything the executor and duckdb see is identical, and so are the rows.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import analysis as A
from . import patch as P

TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
HERE = os.path.dirname(os.path.abspath(__file__))
MAPS = os.path.join(os.path.dirname(HERE), "maps")
_DATA = {}


def _load(name):
    if name not in _DATA:
        try:
            with open(os.path.join(MAPS, name)) as f:
                _DATA[name] = json.load(f)
        except (OSError, ValueError):
            _DATA[name] = None
    return _DATA[name]


def guard(patch_text, root):
    """True when the patch only touches function-body statements the check covers."""
    keep = _load("tpcds_lines.json")
    if not keep:
        return False
    keep = keep["lines"]
    fps = P.parse(patch_text)
    if not fps:
        return False
    for fp in fps:
        p = fp.path
        if fp.binary or fp.old_path is None or fp.new_path is None or fp.old_path != fp.new_path \
                or not p.endswith(".py") or not p.startswith("sqlglot/") or p.startswith("sqlglot/executor/"):
            return False
        try:
            with open(os.path.join(root, p), encoding="utf-8", newline="") as f:
                old = f.read()
        except OSError:
            return False
        tree = A.parse(old)
        if tree is None:
            return False
        fi = A.FileIndex(old, tree)
        hot = set(keep.get(p, ()))
        removed, inserts = P.changed_old_lines(fp)
        for line in removed:
            u = fi.smallest_unit(line)
            if u is None or u.scope != "function" or isinstance(u.node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                                          ast.ClassDef, ast.Lambda)):
                return False
            if any(ln in hot for ln in range(u.start, u.end + 1)):
                return False
        for after in inserts:
            fn = _enclosing_function(tree, after)
            if fn is None or any(ln in hot for ln in range(fn.lineno, fn.end_lineno + 1)):
                return False
    return True


def _enclosing_function(tree, after):
    """Innermost function whose body strictly contains an insertion after old line `after`."""
    best = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body[0].lineno <= after < n.end_lineno:
            if best is None or n.lineno >= best.lineno:
                best = n
    return best


def _farm(root, new_files):
    farm = tempfile.mkdtemp(prefix="br_tpcds_", dir="/tmp" if os.path.isdir("/tmp") else None)
    base = os.path.join(root, "sqlglot")
    try:
        _mirror(base, root, farm)
    except OSError:
        shutil.rmtree(farm, ignore_errors=True)
        return None
    return _patch_farm(farm, new_files)


def _mirror(base, root, farm):
    """Symlinks only (and the read-only __pycache__ dirs): a few KB in /tmp."""
    for dp, dns, fns in os.walk(base):
        rel = os.path.relpath(dp, root)
        os.makedirs(os.path.join(farm, rel), exist_ok=True)
        for d in list(dns):
            if d == "__pycache__":
                os.symlink(os.path.join(dp, d), os.path.join(farm, rel, d))
                dns.remove(d)
        for fn in fns:
            os.symlink(os.path.join(dp, fn), os.path.join(farm, rel, fn))


def _patch_farm(farm, new_files):
    try:
        for path, text in new_files.items():
            dst = os.path.join(farm, path)
            if not path.startswith("sqlglot/") or text is None or not os.path.lexists(dst):
                shutil.rmtree(farm, ignore_errors=True)
                return None
            os.remove(dst)
            with open(dst, "w", encoding="utf-8", newline="") as f:
                f.write(text)
    except OSError:
        shutil.rmtree(farm, ignore_errors=True)
        return None
    return farm


def unchanged(new_files, root, timeout=60):
    """Run the probe on the patched tree and compare with the pinned fingerprint."""
    pinned = _load("tpcds_fp.json")
    if not pinned:
        return False
    farm = _farm(root, new_files)
    if farm is None:
        return False
    try:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"),
               "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        r = subprocess.run([sys.executable, "-s", os.path.join(HERE, "tpcds_probe.py"), farm, root,
                            os.path.join(MAPS, "tpcds_fp.json"), os.path.join(MAPS, "tpcds_rows.json")],
                           capture_output=True, text=True, env=env, timeout=timeout,
                           cwd=os.path.dirname(farm))
        if r.returncode:
            return False
        return json.loads(r.stdout.strip().splitlines()[-1]).get("differs", 0) is None
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return False
    finally:
        shutil.rmtree(farm, ignore_errors=True)


def droppable(patch_text, new_files, root, budget_until=None):
    if budget_until is not None and time.time() > budget_until:
        return False
    return guard(patch_text, root) and unchanged(new_files, root)
