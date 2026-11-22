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
   When the duckdb SQL of some queries differs, it runs the new SQL in duckdb
   (the version the tests use, installed in the image) on the fixture data and
   requires the same columns, types and rows as the pinned SQL
   (maps/tpcds_duck.json, computed at build), with 1 and with 4 threads.

Then everything the executor and duckdb see is identical, and so are the rows.
"""
from __future__ import annotations

import ast
import difflib
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
TPCDS_MODULE = "sqlglot/dialects/duckdb.py"
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


def _duckdb_module_guard(patch_text, new_files, root, process_profile):
    """Admit DuckDB-dialect module edits only with complete process evidence.

    The probe below checks the exact TPC-DS parse/plan/SQL/results. The offline
    profile additionally proves that the test's spawned executor workers do
    not import this dialect module; all missing or incomplete evidence fails
    closed.
    """
    if not process_profile or not process_profile.get("complete") or not process_profile.get("modules_complete"):
        return False
    if TPCDS_MODULE not in process_profile.get("main_modules", ()):
        return False
    if TPCDS_MODULE in process_profile.get("worker_modules", ()):
        return False
    fps = P.parse(patch_text)
    if not fps or set(new_files) != {TPCDS_MODULE}:
        return False
    for fp in fps:
        if fp.binary or fp.old_path != TPCDS_MODULE or fp.new_path != TPCDS_MODULE:
            return False
        if new_files.get(TPCDS_MODULE) is None:
            return False
        try:
            with open(os.path.join(root, TPCDS_MODULE), encoding="utf-8", newline="") as f:
                old = f.read()
        except OSError:
            return False
        new = new_files[TPCDS_MODULE]
        old_tree, new_tree = A.parse(old), A.parse(new)
        if old_tree is None or new_tree is None:
            return False
        old_index, new_index = A.FileIndex(old, old_tree), A.FileIndex(new, new_tree)
        old_imports, new_imports = _imported_names(old_tree), _imported_names(new_tree)
        if old_imports != new_imports:
            return False
        old_lines, new_lines = old.splitlines(), new.splitlines()
        changed_code = 0
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            for index, tree, first, last in ((old_index, old_tree, i1, i2),
                                              (new_index, new_tree, j1, j2)):
                for offset in range(first, last):
                    line = offset + 1
                    if line not in index.code:
                        continue
                    changed_code += 1
                    unit = index.smallest_unit(line)
                    if unit is None or unit.scope not in ("class", "module") \
                            or not _declarative_assignment(unit.node, old_imports, tree):
                        return False
        if not changed_code:
            return False
    return True


def _imported_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")
    return names


def _declarative_assignment(node, imported_names, tree):
    """Only plain assignments with simple container/value syntax.

    No set displays (their iteration order is not pinned). A bare name or
    attribute value binds an object that is also reachable elsewhere, so it is
    rejected when anything in the file (`tree`, the side of the patch the unit
    is on) reads into or mutates the target through attribute access,
    subscripting or augmented assignment: `X = Base.X` then `X.pop(...)`
    would mutate the shared object.
    """
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return False
    target = node.targets[0]
    if not isinstance(target, ast.Name) or target.id in imported_names:
        return False
    if isinstance(node.value, (ast.Name, ast.Attribute)) and _used_through(target.id, tree):
        return False
    safe = (ast.Constant, ast.Name, ast.Attribute, ast.Dict, ast.List, ast.Tuple, ast.Load)
    for n in ast.walk(node.value):
        if not isinstance(n, safe):
            return False
        if isinstance(n, ast.Attribute):
            root = n
            while isinstance(root, ast.Attribute):
                root = root.value
            if not isinstance(root, ast.Name) or root.id not in imported_names:
                return False
    return True


def _used_through(name, tree):
    """True when `name` is the root Name of an Attribute/Subscript, or an AugAssign target, in tree."""
    for n in ast.walk(tree):
        if isinstance(n, (ast.Attribute, ast.Subscript)):
            root = n.value
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if isinstance(root, ast.Name) and root.id == name:
                return True
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
            return True
    return False


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
        if TPCDS_MODULE in new_files:
            # an edit of the dialect module: import it before the first query,
            # so its import-time effects reach every query the probe checks
            env["BR_PROBE_PREIMPORT"] = "1"
        r = subprocess.run([sys.executable, "-s", os.path.join(HERE, "tpcds_probe.py"), farm, root,
                            os.path.join(MAPS, "tpcds_fp.json"), os.path.join(MAPS, "tpcds_rows.json"),
                            os.path.join(MAPS, "tpcds_duck.json")],
                           capture_output=True, text=True, env=env, timeout=timeout,
                           cwd=os.path.dirname(farm))
        if r.returncode:
            return False
        return json.loads(r.stdout.strip().splitlines()[-1]).get("differs", 0) is None
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return False
    finally:
        shutil.rmtree(farm, ignore_errors=True)


def droppable(patch_text, new_files, root, budget_until=None, process_profile=None):
    if budget_until is not None and time.time() > budget_until:
        return False
    structurally_covered = guard(patch_text, root) or _duckdb_module_guard(
        patch_text, new_files, root, process_profile)
    return structurally_covered and unchanged(new_files, root)
