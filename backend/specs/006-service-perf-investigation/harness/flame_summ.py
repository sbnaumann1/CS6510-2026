"""Summarize py-spy raw (collapsed) output: leaf self-time and inclusive time by library."""
import sys, collections, re
self_t = collections.Counter(); incl = collections.Counter(); total = 0
CATS = [("sqlalchemy", "sqlalchemy"), ("asyncpg", "asyncpg"), ("h11", "h11"), ("uvicorn", "uvicorn"),
        ("starlette", "starlette"), ("fastapi", "fastapi"), ("anyio", "anyio"), ("pydantic", "pydantic"),
        ("orjson", "orjson"), ("checkout_service", "checkout_service"), ("checkout_data", "checkout_data"),
        ("checkout_web", "checkout_web"), ("asyncio", "asyncio/"), ("uvloop", "uvloop")]
for line in open(sys.argv[1]):
    line = line.rstrip()
    if not line: continue
    stack, n = line.rsplit(" ", 1); n = int(n); total += n
    frames = stack.split(";")
    self_t[re.sub(r"\s*\(.*?:\d+\)", lambda m: m.group(0).split(":")[0] + ")", frames[-1])] += n
    seen = set()
    for f in frames:
        for cat, pat in CATS:
            if pat in f and cat not in seen:
                seen.add(cat); incl[cat] += n
print(f"samples={total}")
print("inclusive by library:")
for k, v in incl.most_common(): print(f"  {k:18} {v/total:6.1%}")
print("top self (leaf) frames:")
for k, v in self_t.most_common(25): print(f"  {v/total:6.1%}  {k[:150]}")
