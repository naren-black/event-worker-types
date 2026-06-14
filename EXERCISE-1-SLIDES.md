# Exercise 1
## Overselling Discovery & Diagnosis


**Exercise 1: Overselling Discovery & Diagnosis**

- Multi-channel retailer: storefront + eBay + Amazon
- Orders aggregated via **SFTP/CSV** → middleware → WMS
- Inventory availability synced every **15 minutes**
- **Problem:** customers buy items that are actually out of stock

---

## Current state

**How it works today**

```
Channels → SFTP/CSV batch → Middleware → WMS
                ↑
     Inventory sync every 15 min (all channels)
```

- Availability is **batch-refreshed**, not reserved at order time
- **Symptom:** same unit sold on multiple channels within one sync window
- **Worst case:** high-velocity / low-stock SKUs near end of sync cycle

---

## Hypotheses (validate with data)

| # | Hypothesis | Validation |
|---|------------|------------|
| 1 | 15-min sync leaves stale availability | Plot oversells vs sync-cycle boundaries |
| 2 | No reservation at order time | Conflicting orders in same 15-min window |
| 3 | SFTP/CSV lacks idempotency/checksums | Duplicate order IDs, partial files in logs |
| 4 | SKU mapping drift channel ↔ WMS | Mapping audit on oversold SKUs |
| 5 | Bundle components not decremented atomically | Shared components across oversold SKUs |

---

## Success metrics (OKRs)

**Objective:** eliminate overselling while keeping inventory fresh

| SLI | Target SLO |
|-----|------------|
| Oversell rate | < 0.1% in one quarter → 0 |
| Availability propagation latency (p95) | ≤ 5 min (R1/R2); < 1 min (R3) |
| Order ingestion → WMS ack (p95) | ≤ 5 min |

---

## Options & trade-offs

| Option | Idea | Pros | Cons |
|--------|------|------|------|
| **A** | Faster batch / push deltas | Quick win | Race remains; rate limits |
| **B** | Safety-stock buffers | Immediate risk cut | Less sellable inventory |
| **C** | Event-driven ingestion + WMS reservation | Closes the race | Big build; WMS API dependency |
| **D** | Central reservation layer | Single source of truth | New service to operate |

---

## Slide 6 — Phased recommendation

**R1 (now, ~2 weeks)**
- Per-channel safety-stock buffers
- Harden SFTP/CSV: idempotency keys, checksums, DLQ

**R2 (4–6 weeks)**
- Shrink sync interval / push deltas where APIs allow
- SKU mapping reconciliation + drift alerts

**R3 (next quarter)**
- Event-driven ingestion + centralized reservation (C + D)
- Shadow-mode validation before cutover

> Exercise 2 prototypes the ingestion pattern: SFTP → RabbitMQ (retry/DLQ) → dual-cloud upload with idempotency.

---

## Risks & governance

**Risks**
- Marketplace API limits; WMS may lack real-time reservation
- SKU mapping quality; fragile CSV pipeline; cutover risk

**Mitigations**
- Spike WMS API early; reconciliation jobs; shadow rollout

**Governance**
- Short-lived credentials, secrets manager, correlation IDs, CI security scans, schema versioning

---

## Slide 8 — Close (what to say)

1. **Diagnosis:** overselling is a timing + data-quality problem, not a single bug.
2. **Now:** measure SLIs, safety stock, reliable ingestion.
3. **Later:** real-time reservation against one authoritative availability figure.
4. **Questions for stakeholders:** WMS reservation API? webhooks vs polling? which SKUs oversell most?
