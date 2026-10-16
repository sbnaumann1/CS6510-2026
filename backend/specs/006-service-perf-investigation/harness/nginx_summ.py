import sys, collections, re
rows = collections.defaultdict(list)
for line in open(sys.argv[1]):
    p = line.split()
    if len(p) < 8 or p[5] == "-":
        continue
    uri = p[2]
    if uri.endswith("/items") and uri.startswith("/transactions/"): k = "scan"
    elif uri.endswith("/complete"): k = "complete"
    elif uri == "/transactions": k = "start"
    else: k = uri
    try:
        rows[k].append((float(p[4]), float(p[5]), float(p[6]), float(p[7])))
    except ValueError:
        pass
print(f"{'route':10} {'n':>8} {'req_ms':>8} {'upstr_ms':>8} {'gw_ms':>7} {'conn_ms':>7} {'up p50':>7} {'up p95':>7} {'up p99':>7}")
for k in ("start", "scan", "complete"):
    v = rows.get(k)
    if not v: continue
    n = len(v)
    req = sum(r[0] for r in v) / n * 1000
    up = sum(r[1] for r in v) / n * 1000
    cn = sum(r[2] for r in v) / n * 1000
    us = sorted(r[1] for r in v)
    q = lambda f: us[min(n - 1, int(f * n))] * 1000
    print(f"{k:10} {n:8d} {req:8.3f} {up:8.3f} {req-up:7.3f} {cn:7.3f} {q(.5):7.1f} {q(.95):7.1f} {q(.99):7.1f}")
