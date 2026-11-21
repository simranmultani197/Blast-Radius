"""Build step: turn the shipped JSON map into a pickle for fast loading."""
import gzip
import json
import pickle
import sys

src, dst = sys.argv[1], sys.argv[2]
with gzip.open(src, "rb") as f:
    d = json.loads(f.read())
with open(dst, "wb") as f:
    pickle.dump(d, f, protocol=pickle.HIGHEST_PROTOCOL)
