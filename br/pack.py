"""Split a set of tests over up to 8 workers to minimise max load + 2 s/worker."""
from __future__ import annotations

WORKER_MS = 2000


def pack(tests: list[int], ms: dict[int, int], max_workers: int = 8):
    if not tests:
        return [], 0
    order = sorted(tests, key=lambda t: (-ms[t], t))
    total = sum(ms[t] for t in order)
    best = None
    for k in range(1, max_workers + 1):
        loads = [0] * k
        bins = [[] for _ in range(k)]
        for t in order:
            i = loads.index(min(loads))
            bins[i].append(t)
            loads[i] += ms[t]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[1]:
            best = ([b for b in bins if b], cost)
        if ms[order[0]] + WORKER_MS * (k + 1) >= best[1] and total / (k + 1) + WORKER_MS * (k + 1) >= best[1]:
            break
    return best
