import time, json
def work():
    d = {}
    for i in range(200000):
        d[i % 1000] = json.dumps({"a": i, "b": [i, i + 1]})
    return len(d)
best = 1e9
for _ in range(5):
    t = time.perf_counter(); work(); best = min(best, time.perf_counter() - t)
print(f"{best*1000:.1f} ms")
