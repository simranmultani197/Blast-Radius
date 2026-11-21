"""Validation mutants for pair-format SQL fixtures (testing only).

    python3 tools/gen_fixture.py .work/sqlglot .work/fmut SEED

Edits the TPC-H / TPC-DS optimizer fixtures: tokens inside input and expected
statements (including the line carrying the ';'), comment and blank lines,
trailing whitespace, '#' meta lines, ';' count changes and multi-pair edits.
cands.json holds every reader of the file, so labels cover the old selection.
"""
import difflib
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from br.select import CovMap  # noqa: E402

root, out, seed = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(seed)
os.makedirs(out, exist_ok=True)
cm = CovMap("maps/map.json.gz")

FILES = {
    "ds": "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql",
    "h": "tests/fixtures/optimizer/tpc-h/tpc-h.sql",
}


def classify(lines):
    """Per line: ('comment'|'blank'|'meta'|'in'|'exp', pair index)."""
    kinds, stmt, started = [], 0, False
    for ln in lines:
        if not ln or ln.startswith("--"):
            kinds.append(("blank" if not ln else "comment", stmt // 2))
            continue
        part = "in" if stmt % 2 == 0 else "exp"
        if part == "in" and ln.startswith("#") and not started:
            kinds.append(("meta", stmt // 2))
        else:
            kinds.append((part, stmt // 2))
            started = True
        if ";" in ln:
            stmt += ln.count(";")
            started = False
    return kinds


SWAPS = [
    (r"\b\d+\b", lambda m: str(int(m.group()) + 1)),
    (r">=", lambda m: ">"), (r"<=", lambda m: "<"), (r"<>", lambda m: "="),
    (r"(?<![<>!])=(?!=)", lambda m: "<>"),
    (r"\bSUM\b", lambda m: "MAX"), (r"\bAVG\b", lambda m: "MIN"), (r"\bMIN\b", lambda m: "MAX"),
    (r"\bAND\b", lambda m: "OR"), (r"\bOR\b", lambda m: "AND"),
    (r"\bASC\b", lambda m: "DESC"), (r"\bDESC\b", lambda m: "ASC"),
    (r'"(\w+)"', lambda m: f'"{m.group(1)}x"'),
    (r"\b[a-z_]+\.[a-z_]+\b", lambda m: m.group().split(".")[0] + "." + m.group().split(".")[1][:-1]),
    (r"'[^']*'", lambda m: m.group()[:-1] + "Z'"),
]


def tok_edit(line):
    opts = [(p, f) for p, f in SWAPS if re.search(p, line)]
    rng.shuffle(opts)
    for p, f in opts:
        ms = list(re.finditer(p, line))
        m = rng.choice(ms)
        new = line[: m.start()] + f(m) + line[m.end():]
        if new != line and new.count(";") == line.count(";"):
            return new
    return None


def pick(kinds, lines, want, semi=None):
    idx = [i for i, (k, _) in enumerate(kinds) if k == want and lines[i].strip()
           and (semi is None or (";" in lines[i]) == semi)]
    return rng.choice(idx)


made = 0


def emit(key, new_lines, old_lines, ops):
    global made
    rel = FILES[key]
    old, new = "\n".join(old_lines), "\n".join(new_lines)
    if old == new:
        return
    d = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    mid = f"f{seed:02d}{made:04d}"
    with open(f"{out}/{mid}.patch", "w") as f:
        f.write(f"diff --git a/{rel} b/{rel}\nindex 1111111..2222222 100644\n" + d)
    json.dump({"kind": "fixture", "ops": [f"{os.path.basename(rel)}: {o}" for o in ops]},
              open(f"{out}/{mid}.meta.json", "w"))
    json.dump(sorted(cm.tests[i] for i in cm.set(cm.opened[rel])), open(f"{out}/{mid}.cands.json", "w"))
    made += 1


def tok_mutant(key, part, n, semi=None):
    lines = open(os.path.join(root, FILES[key]), encoding="utf-8").read().split("\n")
    kinds = classify(lines)
    for _ in range(n):
        for _try in range(50):
            i = pick(kinds, lines, part, semi)
            new = tok_edit(lines[i])
            if new is not None:
                nl = list(lines)
                nl[i] = new
                emit(key, nl, lines, [f"{part} pair {kinds[i][1]} line {i + 1}: {lines[i].strip()[:60]!r} -> {new.strip()[:60]!r}"])
                break


def misc_mutants(key):
    lines = open(os.path.join(root, FILES[key]), encoding="utf-8").read().split("\n")
    kinds = classify(lines)

    def one(i, new, what, insert=False, delete=False):
        nl = list(lines)
        if insert:
            nl.insert(i, new)
        elif delete:
            del nl[i]
        else:
            nl[i] = new
        emit(key, nl, lines, [f"{what} at line {i + 1} ({kinds[min(i, len(kinds) - 1)][0]})"])

    c = pick(kinds, lines, "comment")
    one(c, lines[c] + " edited", "comment text")
    one(pick(kinds, lines, "exp"), "-- inserted comment; with a semicolon", "comment line inserted", insert=True)
    c = pick(kinds, lines, "comment")
    one(c, None, "comment line deleted", delete=True)
    one(pick(kinds, lines, "exp", semi=False), "", "blank line inside expected", insert=True)
    i = pick(kinds, lines, "exp", semi=False)
    one(i, lines[i] + "  ", "trailing spaces in expected")
    i = pick(kinds, lines, "in", semi=False)
    one(i, None, "input line deleted", delete=True)
    i = pick(kinds, lines, "exp", semi=False)
    one(i, None, "expected line deleted", delete=True)
    # ';' count changes
    i = pick(kinds, lines, "exp", semi=True)
    one(i, lines[i].replace(";", ""), "';' removed from expected")
    i = pick(kinds, lines, "in", semi=False)
    one(i, lines[i] + ";", "';' added in input")
    # meta lines
    metas = [i for i, (k, _) in enumerate(kinds) if k == "meta"]
    if metas:
        i = rng.choice(metas)
        one(i, lines[i].replace("true", "false"), "meta execute true->false")
        i = rng.choice(metas)
        one(i, None, "meta line deleted", delete=True)
        i = rng.choice(metas)
        one(i, "# execute true", "meta line without colon")
    firsts = [i for i, (k, p) in enumerate(kinds) if k == "in" and (i == 0 or kinds[i - 1][0] != "in")
              and (i == 0 or kinds[i - 1][0] != "meta")]
    i = rng.choice(firsts)
    one(i, "# execute: true", "meta execute added", insert=True)
    i = rng.choice(firsts)
    one(i, "# title: renamed", "meta title added", insert=True)
    # several pairs at once
    for _ in range(2):
        nl, ops = list(lines), []
        for part in ("exp", "exp", rng.choice(["exp", "comment"])):
            for _try in range(50):
                i = pick(kinds, lines, part)
                new = tok_edit(nl[i]) if part != "comment" else nl[i] + " x"
                if new is not None and new != nl[i]:
                    ops.append(f"{part} pair {kinds[i][1]} line {i + 1}")
                    nl[i] = new
                    break
        emit(key, nl, lines, ["multi " + "; ".join(ops)])


for key in (("ds", "h") if not (len(sys.argv) > 4 and sys.argv[4] == "official") else ()):
    tok_mutant(key, "exp", 10 if key == "ds" else 8, semi=False)
    tok_mutant(key, "exp", 3 if key == "ds" else 2, semi=True)
    tok_mutant(key, "in", 5 if key == "ds" else 4)
    misc_mutants(key)
print("made", made)


def official_style(key, n):
    """Like the sample's fixture commits: one token inserted or deleted on a random line."""
    lines = open(os.path.join(root, FILES[key]), encoding="utf-8").read().split("\n")
    kinds = classify(lines)
    for _ in range(n):
        for _try in range(50):
            i = rng.randrange(len(lines))
            ln = lines[i]
            if not ln.strip():
                continue
            if rng.random() < 0.6:
                spots = [m.start() for m in re.finditer(r"(?<=\S)(?=\s)|(?<=\s)(?=\S)|(?<=\w)(?=\W)", ln)] or [len(ln)]
                at = rng.choice(spots)
                tok = rng.choice(["42", "NULL", "0", "1", "x", "a"])
                new = ln[:at] + (" " if at and ln[at - 1] != " " else "") + tok + (" " if at < len(ln) and ln[at] != " " else "") + ln[at:]
            else:
                toks = list(re.finditer(r"\w+|[^\w\s]", ln))
                m = rng.choice(toks)
                new = ln[: m.start()] + ln[m.end():]
            if new != ln:
                nl = list(lines)
                nl[i] = new
                emit(key, nl, lines, [f"official-style {kinds[i][0]} pair {kinds[i][1]} line {i + 1}: {ln.strip()[:50]!r} -> {new.strip()[:50]!r}"])
                break


if len(sys.argv) > 4 and sys.argv[4] == "official":
    made = 0
    official_style("ds", 22)
    official_style("h", 10)
    print("official-style made", made)
