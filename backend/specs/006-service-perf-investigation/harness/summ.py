import glob, json, sys, os, collections
out = sys.argv[1]
name = os.path.basename(out)
reps = glob.glob(f"{out}/report-*.json")
if not reps:
    print(name, "NO REPORT"); sys.exit()
d = json.load(open(reps[0]))
ops = {o["operation"]: o for o in d["operations"]}
s, c = ops["SCAN_ITEM"], ops["COMPLETE_TRANSACTION"]
inv = open(f"{out}/invariant.txt").read().strip().splitlines()[-2:] if os.path.exists(f"{out}/invariant.txt") else []
print(f"{name}: tx/s={d['transactionsPerSecond']:.1f} items/s={d['itemsPerSecond']:.0f} "
      f"scan mean/p50/p95/p99={s['meanMs']:.2f}/{s['p50Ms']:.2f}/{s['p95Ms']:.2f}/{s['p99Ms']:.2f} "
      f"complete mean/p50/p95/p99={c['meanMs']:.2f}/{c['p50Ms']:.2f}/{c['p95Ms']:.1f}/{c['p99Ms']:.1f} "
      f"cerr={c['errorRate']:.1%} | {' '.join(inv)}")
# pg activity: avg count per (state,wait) per sample
p = f"{out}/pg_activity.csv"
if os.path.exists(p):
    tot = collections.Counter(); ts = set()
    for line in open(p):
        parts = line.strip().split(",")
        if len(parts) != 5: continue
        ts.add(parts[0][:13]); tot[(parts[1], parts[2], parts[3])] += int(parts[4])
    n = max(len(ts), 1)
    print("  pg backends (avg per sample):", ", ".join(f"{k[0]}/{k[1]}/{k[2]}={v/n:.1f}" for k, v in tot.most_common(8)))
p = f"{out}/docker_cpu.csv"
if os.path.exists(p):
    agg = collections.defaultdict(list)
    for line in open(p):
        parts = line.strip().split(",")
        if len(parts) == 3 and parts[2].endswith("%"):
            agg[parts[1]].append(float(parts[2][:-1]))
    print("  docker cpu% (median):", ", ".join(f"{k.replace('perf-','')}={sorted(v)[len(v)//2]:.0f}" for k, v in sorted(agg.items()) if v))
p = f"{out}/host_cpu.csv"
if os.path.exists(p):
    agg = collections.defaultdict(lambda: collections.defaultdict(float))
    for line in open(p):
        parts = line.strip().split(",", 2)
        if len(parts) == 3:
            key = "java" if "java" in parts[2] else "vm"
            agg[key][parts[0]] += float(parts[1])
    print("  host cpu% (median):", ", ".join(f"{k}={sorted(v.values())[len(v)//2]:.0f}" for k, v in agg.items()))
