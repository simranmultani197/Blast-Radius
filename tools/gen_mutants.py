"""Generate synthetic mutant patches against the pinned tree (validation only).

    python3 tools/gen_mutants.py .work/sqlglot .work/mutants N SEED
Each mutant is <out>/<id>.patch plus <out>/<id>.meta.json (kind, operator).
"""
import difflib
import io
import json
import os
import random
import re
import sys
import tokenize

root, out, n, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
os.makedirs(out, exist_ok=True)

py_files = []
for dp, _, fns in os.walk(os.path.join(root, "sqlglot")):
    for fn in fns:
        if fn.endswith(".py"):
            p = os.path.relpath(os.path.join(dp, fn), root)
            py_files.append(p)
fixture_files = []
for dp, _, fns in os.walk(os.path.join(root, "tests", "fixtures")):
    for fn in fns:
        if fn.endswith(".sql"):
            fixture_files.append(os.path.relpath(os.path.join(dp, fn), root))
test_files = [p for p in py_files]  # placeholder
tests_py = []
for dp, _, fns in os.walk(os.path.join(root, "tests")):
    for fn in fns:
        if fn.startswith("test_") and fn.endswith(".py"):
            tests_py.append(os.path.relpath(os.path.join(dp, fn), root))

sizes = {p: os.path.getsize(os.path.join(root, p)) for p in py_files}
ALLOWED = None
if os.environ.get("BR_LINES"):
    ALLOWED = {f: set(v) for f, v in json.load(open(os.environ["BR_LINES"])).items()}
    sizes = {p: len(ALLOWED[p]) for p in py_files if p in ALLOWED}

SWAPS = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">", "+": "-", "-": "+",
         "and": "or", "or": "and", "True": "False", "False": "True", "is": "==", "in": "not in"}
COMMENTS = ["# Cheap check first", "# Kept for backwards compatibility", "# Keep in sync with the generator",
            "# Intentionally not cached", "# Mirrors the behavior of the reference implementation",
            "# TODO: revisit", "# noqa", "# Fast path", "# See the docs for details"]


def read(p):
    return open(os.path.join(root, p), encoding="utf-8").read()


def make_patch(path, old, new):
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    diff = difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=3)
    body = "".join(diff)
    if not body:
        return None
    return f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n" + body


def mutate_code(path):
    src = read(path)
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    lines = src.split("\n")
    cands = []
    for t in toks:
        if ALLOWED is not None and t.start[0] not in ALLOWED.get(path, ()):
            continue
        if t.type == tokenize.OP and t.string in ("==", "!=", "<", "<=", ">", ">=", "+", "-"):
            cands.append(("op", t))
        elif t.type == tokenize.NAME and t.string in ("and", "or", "True", "False", "is", "not", "in", "None"):
            cands.append(("name", t))
        elif t.type == tokenize.NUMBER:
            cands.append(("num", t))
        elif t.type == tokenize.STRING and len(t.string) < 40 and t.start[0] == t.end[0]:
            cands.append(("str", t))
    if not cands:
        return None
    kind, t = rng.choice(cands)
    (r, c0), (_, c1) = t.start, t.end
    line = lines[r - 1]
    s = t.string
    if kind == "op":
        rep = SWAPS[s]
        op = f"swap {s}->{rep}"
    elif kind == "name":
        if s == "not":
            # drop the negation
            rep = ""
            c1 = c1 + 1 if line[c1:c1 + 1] == " " else c1
            op = "drop not"
        elif s == "None":
            return None
        elif s == "is":
            nxt = line[c1:c1 + 5]
            if nxt.startswith(" not "):
                c1 += 4
                rep = "!="
                op = "is not->!="
            else:
                rep = "=="
                op = "is->=="
        elif s == "in":
            prev = line[max(0, c0 - 4):c0]
            if prev.endswith("not "):
                c0 -= 4
                rep = "in"
                op = "not in->in"
            else:
                rep = "not in"
                op = "in->not in"
        else:
            rep = SWAPS[s]
            op = f"swap {s}->{rep}"
    elif kind == "num":
        try:
            v = int(s, 0)
            rep = str(v + rng.choice([1, -1])) if v not in (0, 1) else str(1 - v)
        except ValueError:
            return None
        op = f"num {s}->{rep}"
    else:
        q = s[-1]
        inner = s[s.index(q) + 1:-1]
        if not inner or s.startswith(("f", "b", "r", "F", "B", "R")) or '"""' in s or "'''" in s:
            return None
        i = rng.randrange(len(inner) + 1)
        ch = rng.choice("xyz_ ")
        rep = s[: s.index(q) + 1] + inner[:i] + ch + inner[i:] + q
        op = "string tweak"
    new_line = line[:c0] + rep + line[c1:]
    lines[r - 1] = new_line
    return src, "\n".join(lines), op


def comment_insert(path):
    src = read(path)
    lines = src.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.strip().startswith(("#", '"', "'", ")", "]", "}"))]
    if not idx:
        return None
    i = rng.choice(idx)
    ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    lines.insert(i, ind + rng.choice(COMMENTS))
    return src, "\n".join(lines), "comment"


def delete_stmt(path):
    src = read(path)
    lines = src.split("\n")
    idx = [i for i, l in enumerate(lines)
           if re.match(r"^\s+(\w+(\.\w+)*\s*=[^=].*|return .*|\w+\(.*\))$", l) and not l.rstrip().endswith(("(", "[", "{", ",", "\\"))]
    if not idx:
        return None
    i = rng.choice(idx)
    ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    lines[i] = ind + "pass"
    return src, "\n".join(lines), "stmt->pass"


def fixture_mutation(path):
    src = read(path)
    lines = src.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith(("--", "#"))]
    if not idx:
        return None
    i = rng.choice(idx)
    l = lines[i]
    choice = rng.random()
    if choice < 0.5:
        j = rng.randrange(len(l) + 1)
        lines[i] = l[:j] + rng.choice(["x ", "42 ", "1", " ", ",", "("]) + l[j:]
        op = "fixture insert"
    elif choice < 0.8:
        j = rng.randrange(len(l))
        lines[i] = l[:j] + l[j + 1:]
        op = "fixture delete char"
    else:
        lines[i] = l + " -- note"
        op = "fixture comment"
    return src, "\n".join(lines), op


def pick_py():
    files = list(sizes)
    weights = [sizes[f] for f in files]
    return rng.choices(files, weights)[0]


made = 0
attempts = 0
while made < n and attempts < n * 50:
    attempts += 1
    r = rng.random() * (0.62 if ALLOWED is not None else 1.0)
    patches, ops = [], []
    if r < 0.55:
        kind = "code"
        p = pick_py()
        m = mutate_code(p)
        parts = [(p, m)]
    elif r < 0.62:
        kind = "code"
        p = pick_py()
        parts = [(p, delete_stmt(p))]
    elif r < 0.72:
        kind = "comment"
        p = pick_py()
        parts = [(p, comment_insert(p))]
    elif r < 0.9:
        kind = "fixture"
        p = rng.choice(fixture_files)
        parts = [(p, fixture_mutation(p))]
    else:
        kind = "multi"
        p1, p2, p3 = pick_py(), pick_py(), rng.choice(fixture_files)
        if len({p1, p2}) < 2:
            continue
        parts = [(p1, comment_insert(p1)), (p2, mutate_code(p2)), (p3, fixture_mutation(p3))]
    if any(m is None for _, m in parts):
        continue
    text = ""
    for p, (old, new, op) in parts:
        pt = make_patch(p, old, new)
        if pt is None:
            break
        text += pt
        ops.append(f"{p}: {op}")
    else:
        mid = f"m{seed:02d}{made:04d}"
        open(f"{out}/{mid}.patch", "w").write(text)
        json.dump({"kind": kind, "ops": ops}, open(f"{out}/{mid}.meta.json", "w"))
        made += 1
print(f"made {made}", file=sys.stderr)
