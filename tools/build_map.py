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
    modules = collections.defaultdict(set)
    attr_reads = collections.defaultdict(set)
    attr_import = set()
    have_attrs = False
    cmp_diff = collections.defaultdict(set)
    cmp_import = set()
    have_cmp = False
    num_to_id = {str(t["number"]): t["id"] for t in tests}
    paths = [p for d in trace_dir.split(",") for p in glob.glob(f"{d}/*.*.json")]
    for p in paths:
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
        for m in r.get("modules", []):
            modules[m].add(ti)
        if "attr_reads" in r:
            have_attrs = True
            for a in r["attr_reads"]:
                attr_reads[a].add(ti)
            attr_import.update(r["attr_import"])
        if "cmp_diff" in r:
            have_cmp = True
            for k in r["cmp_diff"]:
                cmp_diff[k].add(ti)
            cmp_import.update(r["cmp_import"])
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
        "modules": {k: sid(ts) for k, ts in modules.items()},
    }
    if have_cmp:
        data["cmp"] = {k: sid(ts) for k, ts in cmp_diff.items()}
        data["cmp_import"] = sorted(cmp_import)
    if have_attrs:
        data["attr_reads"] = {k: sid(ts) for k, ts in attr_reads.items()}
        data["attr_import"] = sorted(attr_import)
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
        data["str_names"] = xrefs.upper_strings(sys.argv[5])
        import defuse
        du = defuse.build(sys.argv[5])
        data["du"] = {"refs": du["refs"], "aliases": du["aliases"], "strings": du["strings"]}
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
