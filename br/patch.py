"""Unified-diff parsing and applying (no external tools needed)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list = field(default_factory=list)  # (tag, text) tag in ' ', '-', '+'


@dataclass
class FilePatch:
    old_path: str | None  # None when the file is created
    new_path: str | None  # None when the file is deleted
    hunks: list = field(default_factory=list)
    binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path


def _strip(p: str) -> str | None:
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def parse(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: Hunk | None = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = FilePatch(m.group(1) if m else None, m.group(2) if m else None)
            files.append(cur)
            hunk = None
        elif line.startswith("--- ") and (i + 1 < len(lines) and lines[i + 1].startswith("+++ ")):
            old = _strip(line[4:])
            new = _strip(lines[i + 1][4:])
            if cur is None or cur.hunks:
                cur = FilePatch(old, new)
                files.append(cur)
            else:
                cur.old_path, cur.new_path = old, new
            hunk = None
            i += 1
        elif line.startswith("new file mode") and cur is not None:
            cur.old_path = None
        elif line.startswith("deleted file mode") and cur is not None:
            cur.new_path = None
        elif line.startswith("rename from ") and cur is not None:
            cur.old_path = line[len("rename from "):]
        elif line.startswith("rename to ") and cur is not None:
            cur.new_path = line[len("rename to "):]
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            if cur is not None:
                cur.binary = True
        elif line.startswith("@@") and cur is not None:
            m = HUNK_RE.match(line)
            if m:
                hunk = Hunk(
                    int(m.group(1)),
                    int(m.group(2)) if m.group(2) is not None else 1,
                    int(m.group(3)),
                    int(m.group(4)) if m.group(4) is not None else 1,
                )
                cur.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk.lines.append((line[0], line[1:]))
        elif hunk is not None and line.startswith("\\"):
            # "\ No newline at end of file": mark the previous line
            if hunk.lines:
                tag, txt = hunk.lines[-1]
                hunk.lines[-1] = (tag, txt + "\x00NONL")
        elif hunk is not None and line == "" and i < len(lines) - 1:
            # some tools drop the leading space of empty context lines
            hunk.lines.append((" ", ""))
        i += 1
    return files


def changed_old_lines(fp: FilePatch):
    """Old-file line numbers that were removed/modified, and insertion points.

    Returns (removed, inserts) where inserts is a list of old-line positions
    p meaning "new lines were inserted between old line p and p+1". Added
    lines that replace removed ones one-for-one are modifications, not
    insertions; extra added lines beyond that count as an insertion too.
    """
    removed: set[int] = set()
    inserts: list[int] = []
    for h in fp.hunks:
        old = h.old_start if h.old_len else h.old_start + 1
        lines = h.lines
        i = 0
        while i < len(lines):
            tag = lines[i][0]
            if tag == " ":
                old += 1
                i += 1
                continue
            ndel = nadd = 0
            while i < len(lines) and lines[i][0] == "-":
                removed.add(old)
                old += 1
                ndel += 1
                i += 1
            while i < len(lines) and lines[i][0] == "+":
                nadd += 1
                i += 1
            if nadd > ndel:
                inserts.append(old - 1)
    return removed, inserts


def apply(old_text: str | None, fp: FilePatch) -> str | None:
    """Apply one file's hunks; returns new text (None when deleted)."""
    if fp.new_path is None:
        return None
    old_lines = [] if old_text is None else old_text.split("\n")
    # keep track of trailing newline: split gives '' as last element
    trailing_nl = old_text is None or old_text.endswith("\n")
    if old_text is not None and old_text.endswith("\n"):
        old_lines = old_lines[:-1]
    out: list[str] = []
    pos = 0  # index into old_lines
    new_no_nl = False
    for h in fp.hunks:
        start = (h.old_start - 1) if h.old_len else h.old_start
        expected = [t.replace("\x00NONL", "") for tag, t in h.lines if tag != "+"]
        start = _locate(old_lines, expected, start, pos)
        out.extend(old_lines[pos:start])
        pos = start
        for tag, txt in h.lines:
            nonl = txt.endswith("\x00NONL")
            txt = txt.replace("\x00NONL", "")
            if tag == " ":
                out.append(old_lines[pos])
                pos += 1
                if nonl:
                    new_no_nl = True
            elif tag == "-":
                pos += 1
            else:
                out.append(txt)
                new_no_nl = nonl
    out.extend(old_lines[pos:])
    text = "\n".join(out)
    if out and not new_no_nl and (trailing_nl or fp.hunks):
        text += "\n"
    return text


def _locate(old_lines, expected, start, minpos):
    def ok(s):
        if s < minpos or s + len(expected) > len(old_lines):
            return False
        return old_lines[s:s + len(expected)] == expected

    if ok(start):
        return start
    for d in range(1, 400):
        for s in (start - d, start + d):
            if ok(s):
                return s
    return start
