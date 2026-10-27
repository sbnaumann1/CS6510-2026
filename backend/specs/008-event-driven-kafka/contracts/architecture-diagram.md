# Contract: Architecture Diagram (FR-023, SC-005)

Committed at `backend/docs/architecture.md` (Mermaid, renders on GitHub from the repo URL) with a
rendered `backend/docs/architecture.svg` beside it, linked from the README and from the load-report
folder's README.

## Must show

| Element | Detail |
| --- | --- |
| Shopper / load client | one arrow to the gateway, labelled "public HTTP contract" |
| API Gateway | routes `/items`, `/transactions*`, `/inventory/*`, `/analytics/*`; blocks `/internal/` |
| Four microservices | Catalog, Checkout, Inventory, Analytics |
| Private stores | `catalog.csv` (Catalog), `checkout-db` (Checkout), `inventory-db` (Inventory), "in-memory window" (Analytics) |
| Kafka broker | one box in the middle, outside every service |
| Topics | the five topics, drawn on or inside the broker |
| Direction of every producer and consumer | arrow from producer into the topic, arrow from the topic to each consumer |
| No service-to-service arrows | the only arrows between services pass through the broker |

## Starting sketch

```mermaid
flowchart LR
    LC([Load client / shopper]) -->|public HTTP| GW[API Gateway]
    GW --> CAT[Catalog]
    GW --> CHK[Checkout]
    GW --> INV[Inventory]
    GW --> ANA[Analytics]

    CAT --- CATF[(catalog.csv)]
    CHK --- CHKDB[(checkout-db)]
    INV --- INVDB[(inventory-db)]
    ANA --- WIN[(in-memory window)]

    subgraph K[Kafka event broker]
        T1[[catalog.items.v1]]
        T2[[checkout.scans.v1]]
        T3[[checkout.completions.v1]]
        T4[[inventory.stock.v1]]
        T5[[dead-letters.v1]]
    end

    CAT -->|items + marker, at startup| T1
    T1 --> CHK
    T1 --> INV
    T1 --> ANA

    CHK -->|scan-recorded via relay| T2
    T2 --> ANA

    CHK -->|reservation / release requested| T3
    T3 --> INV

    INV -->|reserved / rejected / released / refused| T4
    T4 -->|reply to waiting request| CHK
```

The final diagram may add the outbox and relay inside Checkout and Inventory, and a second small
diagram for the completion sequence ([completion-flow.md](./completion-flow.md)).
