"""Turn per-test traces into the compact map the selector ships with.

    python3 tools/build_map.py sample/tests.json .work/trace maps/map.json.gz
"""
import collections
import glob
import os
import gzip
import json
import sys


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    trace_dir, out = sys.argv[2], sys.argv[3]
    ids = [t["id"] for t in tests]
    index = {tid: i for i, tid in enumerate(ids)}
    per_line = collections.defaultdict(lambda: collections.defaultdict(list))
    opened = collections.defaultdict(list)
    receivers = collections.defaultdict(set)
    lambdas = collections.defaultdict(set)
    num_to_id = {str(t["number"]): t["id"] for t in tests}
    for p in glob.glob(f"{trace_dir}/*.*.json"):
        r = json.load(open(p))
        ti = index[num_to_id[r["test"]]]
        for f, ls in r["lines"].items():
            d = per_line[f]
            for ln in ls:
                d[ln].append(ti)
        for o in r["opened"]:
            if o not in ("pyproject.toml", "setup.cfg"):
                opened[o].append(ti)
        for k in r.get("receivers", []):
            receivers[k].add(ti)
        for f, ln in r.get("lambdas", []):
            lambdas[f"{f}:{ln}"].add(ti)
    sets = {}
    set_list = []

    def sid(s):
        key = tuple(sorted(s))
        if key not in sets:
            sets[key] = len(set_list)
            set_list.append(key)
        return sets[key]

    files = {}
    for f, d in per_line.items():
        files[f] = {str(ln): sid(ts) for ln, ts in sorted(d.items())}
    data = {
        "tests": ids,
        "files": files,
        "opened": {o: sid(set(ts)) for o, ts in opened.items()},
        "receivers": {k: sid(ts) for k, ts in receivers.items()},
        "lambdas": {k: sid(ts) for k, ts in lambdas.items()},
    }
    data["sets"] = [_encode(s) for s in set_list]
    if len(sys.argv) > 4:
        classes = json.load(open(sys.argv[4]))
        for v in classes.values():
            v.pop("subclasses", None)
        data["classes"] = classes
    if len(sys.argv) > 5:
        sys.path.insert(0, os.path.dirname(__file__))
        import xrefs
        data["xrefs"] = xrefs.build(sys.argv[5])
    raw = json.dumps(data, separators=(",", ":")).encode()
    with gzip.open(out, "wb", 9) as f:
        f.write(raw)
    print(f"files={len(files)} lines={sum(len(v) for v in files.values())} "
          f"sets={len(set_list)} raw={len(raw)/1e6:.1f}MB", file=sys.stderr)


def _encode(s):
    # delta-encode sorted ints as a compact string of base-36 gaps
    out, prev = [], -1
    for x in s:
        out.append(format(x - prev, "x"))
        prev = x
    return ".".join(out)


if __name__ == "__main__":
    main()
