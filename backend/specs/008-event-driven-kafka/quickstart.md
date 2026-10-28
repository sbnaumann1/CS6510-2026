# Quickstart: Validating the Event-Driven Backend

Run from `backend/`. Prerequisites: Docker Desktop, Python 3.11 with `uv`, Java 17+ for the load
client. Contracts: [events](./contracts/events.md), [completion flow](./contracts/completion-flow.md),
[topology](./contracts/compose-topology.md).

## 1. Start the stack

```bash
docker compose up -d --build --wait      # Kafka, topic init, seeds, four services, gateway
docker compose ps                         # every unit healthy; kafka-init exited 0
```

**Expect**: `mesh` does not exist (`docker network ls`); topics exist:

```bash
docker compose exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --list
# catalog.items.v1  checkout.scans.v1  checkout.completions.v1  inventory.stock.v1  dead-letters.v1
```

## 2. The contract still holds (US1, SC-001)

```bash
uv run pytest tests/contract tests/integration -q        # in-process, MemoryBus
# against the running gateway (use the test stack: docker compose -f compose.yaml -f compose.test.yaml up -d --build --wait)
GATEWAY_URL=http://localhost:8080 uv run pytest tests/contract -q
```

**Expect**: all pass, with no assertion changes from 007.

## 3. No service-to-service calls (US2, SC-002)

```bash
uv run python scripts/check_boundaries.py                # E1–E10 plus 007's rules
./scripts/check_isolation.sh                             # no container resolves a peer service; all reach Kafka
./scripts/check_no_peer_calls.sh                         # a full workload through the gateway; no peer connection
```

**Expect**: zero violations; every public endpoint still works.

## 4. Events are visible (US2 scenario 3, SC-011)

```bash
./scripts/trace_request.sh                               # one scan, one completion
```

**Expect**: the same request id in the logs of Gateway, Checkout and Inventory, and listed on
`checkout.completions.v1` and `inventory.stock.v1` with their offsets.

## 5. Correctness under failure (US3, US4, SC-006 to SC-009)

```bash
uv run pytest tests/failure -q                           # duplicates, drops, delays, reorders, crash before commit
./scripts/check_independence.sh                          # stop each service (and Kafka) in turn; needs a fresh stack
./scripts/check_crash_recovery.sh                        # kill Checkout and Inventory under load
./scripts/check_broker_outage.sh                         # stop Kafka under load, restart
# the real-broker subset: keep only the broker (the in-process services join the same consumer groups)
docker compose stop gateway checkout inventory analytics catalog
BROKER_TESTS=1 uv run pytest tests/broker -q             # topic config, keys, DLQ, replay on real Kafka
```

**Expect**: after every drill `uv run python scripts/verify_invariant.py` (or the `tools` container the scripts run) reports: stock sold equals
units scanned in completed transactions, no negative stock, no transaction stuck, no outbox row
unpublished, Analytics has consumed every scan.

## 6. The graded deliverables (US5, SC-003, SC-004, SC-010)

```bash
REPORT_DIR=008-event-driven-kafka ./scripts/run_load_tests.sh all
#   default run:  java -cp out Main                                    (defaults)
#   stress run:   java -cp out Main --stations=100 --duration=120
ls ../load-client/reports/008-event-driven-kafka/        # exactly two report-*.json files
./scripts/render_diagram.sh                              # docs/architecture.svg from the Mermaid source
```

**Expect**: both runs complete with zero transport failures and zero server errors, the invariant
passes after each, and the README table compares throughput with 007's (default 200.5 tx/s, stress
196.2 tx/s) and explains any shortfall.

Submit the repository URL. It must show, without running anything:

| Deliverable | Where |
| --- | --- |
| Default-parameters report (JSON) | `load-client/reports/008-event-driven-kafka/report-*.json` |
| Stress-mode report (JSON) | same folder, second file |
| Architecture diagram | `backend/docs/architecture.md` (renders in place) and `architecture.svg` |

## 7. Tear down

```bash
docker compose down -v
```
