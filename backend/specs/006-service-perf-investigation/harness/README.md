# Performance harness (spec 006)

Run everything from `backend/`, with `H=specs/006-service-perf-investigation/harness`.
Output goes to `$PERF_RUNS/<run>/` (default `backend/.perf-runs/`, git-ignored).

| File | What it is |
|---|---|
| `run_exp.sh NAME` | Starts a fresh compose stack (`-p perf`, plus `instrument.yaml` for `pg_stat_statements`) and runs the Java load client. While it runs it samples `pg_stat_activity` wait events (4 Hz), `docker stats` and host CPU. Afterwards it dumps statement stats and verifies the invariant. |
| `native_exp.sh NAME` | Runs the backend natively against host PG (`/tmp` socket, database `checkout_perf`) on `:8090`. Default: this checkout's services in one process (`composite/`). |
| `summ.py`, `nginx_summ.py`, `flame_summ.py` | Summarize a run directory, an nginx timing log, and a py-spy raw profile. |
| `layers_bench.py`, `cpubench.py`, `cpupar.sh` | The per-layer scan microbenchmark, and the native-vs-VM CPU benchmark. |
| `*.yaml` | Compose overlays, passed via `EXTRA="-f $H/x.yaml"`: `gwlog` (nginx timing log, set by `GWLOG=1`), `pgsock`, `w10`, `httptools`, and the investigation prototypes `proto`, `desc`, `atomic_desc`. |
| `proto/`, `desc/`, `atomic_desc/` | Prototype module copies mounted by those overlays. |
| `archive/` | The exact command chains of the investigation (record only). |

**Environment variables**

| Group | Variables |
|---|---|
| Paths (optional) | `BACK` (backend/), `LC` (load-client/), `PERF_RUNS` |
| Workload | `STATIONS`=100, `DURATION`=30, `STOCK`=2000000 |
| Compose | `CLIENT`=host\|net (`net` runs the client inside the compose network), `EXTRA`, `GWLOG`=0\|1 |
| Native | `APPDIR` (default this checkout), `APP` (default `composite:app`), `APPDIR_FLAG`, `WORKERS`=4, `DB_POOL_SIZE`=12 |

**Measurement rule**

- **Compare within one session.** Absolute numbers depend on host power state. Always run a
  calibration arm in the same session and compare against it.
- **Keep the Mac awake.** Wrap runs in `caffeinate -i`.
- **Discard sleep-affected runs.** Discard any run with client timeouts, or a Sleep event in
  `pmset -g log` during it.
- **Never run load tests in parallel** with each other or with the test suite.

**Examples**

```sh
caffeinate -i $H/run_exp.sh base100                                   # 100 st / 30 s / ample stock
STATIONS=100 DURATION=120 STOCK=10000 caffeinate -i $H/run_exp.sh g_stress   # graded stress config
caffeinate -i $H/native_exp.sh native100                              # native, one process
```

The load client must be built first: `../load-client/build.sh`.
