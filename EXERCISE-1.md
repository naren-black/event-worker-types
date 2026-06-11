# Exercise 1 — Overselling Discovery & Diagnosis Pack

## Context

A retailer sells through its own storefront plus eBay and Amazon marketplaces.
Marketplace orders are aggregated via SFTP/CSV into a middleware layer, which
forwards orders to the WMS and synchronizes inventory availability across all
channels on a **15-minute batch cycle**. Customers are reporting purchases of
items that are actually out of stock ("overselling").

This pack diagnoses likely causes, defines success metrics, weighs remediation
options, and lays out a phased plan. Exercise 2 in this repo prototypes the
event-driven, idempotent ingestion pattern referenced as the long-term fix for
the SFTP/CSV pipeline.

## 1. Current State & Hypotheses

| # | Hypothesis | How to validate |
|---|---|---|
| 1 | **15-minute sync window leaves stale availability.** A unit sold on one channel remains "in stock" on the others for up to 15 minutes, so high-velocity or low-stock SKUs can be sold multiple times before the next sync corrects it. | Plot oversold order timestamps against sync-cycle boundaries; expect clustering near the end of each window. |
| 2 | **No reservation/decrement at order time.** Inventory only changes on the batch sync, so two channels can independently see the same "available" unit and both sell it within the same window. | For oversold SKUs, check whether the conflicting order timestamps fall in the same sync interval. |
| 3 | **SFTP/CSV order aggregation lacks idempotency/checksums.** A re-run, partial transfer, or duplicate export can cause orders to be double-counted or dropped, corrupting the inventory math the next sync relies on. | Scan middleware/WMS logs for duplicate order IDs, repeated filenames, or zero-byte/partial files. |
| 4 | **SKU/variant mapping drift between channels and WMS.** A channel SKU mapped to the wrong (or no) WMS SKU means a sale decrements the wrong stock record, or none at all. | Spot-check oversold SKUs' channel→WMS mappings against the WMS catalog. |
| 5 | **Bundle/kit SKUs don't decrement shared components atomically.** If a bundle's components aren't decremented in lock-step with the bundle (or vice versa), shared components can be oversold across multiple bundles. | Check whether oversold SKUs are bundles, or share components with other oversold SKUs. |

## 2. OKRs / SLIs / SLOs

**Objective:** eliminate overselling while keeping cross-channel inventory data fresh.

| SLI | Target SLO | Rationale |
|---|---|---|
| Oversell rate (oversold order lines / total order lines) | **< 0.1%** within one quarter, trending toward 0 | Directly measures the customer-facing problem. |
| Inventory-availability propagation latency (p95: time from a sale on any channel to updated availability on all others) | **p95 ≤ 5 min** in R1/R2 (down from 15 min); **near-real-time (< 1 min)** by R3 | This window is the root cause's blast radius - shrinking it shrinks oversell exposure directly. |
| Order ingestion → WMS acknowledgement latency (p95) | **p95 ≤ 5 min** | Ensures the WMS reflects sold units quickly, feeding back into availability calculations. |

## 3. Options, Trade-offs & Recommendation

| Option | Description | Pros | Cons |
|---|---|---|---|
| **A. Shrink/push-based inventory sync** | Reduce the batch interval (e.g. 1-2 min) or push deltas on change | Directly shrinks the staleness window; low lift if infra supports more frequent runs | Shrinks the race window but doesn't close it; marketplace APIs may rate-limit frequent calls |
| **B. Per-channel safety-stock buffers** | Hold back N units or X% per channel so "available to sell" is conservative | Fast, cheap, no architecture change, immediate risk reduction | Doesn't fix the root cause; reduces sellable inventory and needs per-SKU tuning |
| **C. Event-driven order ingestion + real-time WMS reservation** | Each order triggers an immediate reservation/decrement against one source of truth | Closest to eliminating the race entirely; same pattern Exercise 2 demonstrates (broker, idempotency, retry/DLQ) | Largest scope - needs WMS support for real-time reservations and reliable channel events |
| **D. Centralized inventory orchestration/reservation layer** | A dedicated service holds the authoritative "available to sell" figure and arbitrates reservations across channels | Decouples channel quirks from the WMS; single place for reservation logic and observability | New service to build and operate; needs careful concurrency design |

**Recommendation - phased:**
- **R1 (now):** Option B as an immediate stop-gap, paired with hardening the SFTP/CSV pipeline (idempotency + checksums + DLQ - mirrors Exercise 2's design) so the data feeding every later option is trustworthy.
- **R2 (next):** Option A plus fixing SKU-mapping drift, further shrinking the staleness window using the now-reliable pipeline.
- **R3 (the real fix):** Options C + D together - event-driven order ingestion feeding a centralized reservation layer, built on the same broker/idempotency/retry pattern prototyped in Exercise 2.

## 4. Top Risks, Dependencies, Mitigations & Owners

| Risk / Dependency | Mitigation | Owner |
|---|---|---|
| Marketplace API rate limits / webhook reliability for higher-frequency or event-driven sync | Start with higher-frequency polling (R2) before committing to webhooks; add backoff and circuit-breakers | Integrations team |
| WMS API may not support real-time reservation calls | Spike against WMS API early; if unsupported, Option D's orchestration layer holds reservations independently | Platform/WMS team |
| SKU/variant mapping data quality | Build a reconciliation job comparing channel catalogs to the WMS catalog; alert on unmapped/ambiguous SKUs | Data engineering |
| SFTP/CSV pipeline fragility (duplicates, partial files, no idempotency) | Apply the idempotency-key + checksum + retry/DLQ pattern from Exercise 2 to order ingestion | Platform team |
| Cutover risk moving batch → event-driven | Shadow-mode rollout: run the new pipeline alongside the old and compare outputs before cutover | Eng lead |

## 5. Governance-by-Design

- **Identity & access:** no long-lived static credentials for marketplace APIs or cloud storage; short-lived, role-based credentials (OIDC/Workload Identity Federation) scoped per integration - the same posture documented in [`docs/event-contract.md`](docs/event-contract.md#3-identity--auth-posture) for Exercise 2.
- **Secrets management:** API keys and credentials live in a secrets manager with rotation, never committed to source control.
- **Observability:** correlation IDs threaded from channel → middleware → WMS so any order can be traced end-to-end; structured JSON logs; dashboards and alerts on the SLIs in section 2.
- **CI/CD security:** SAST, dependency scanning, secrets scanning and IaC scanning on every change; mandatory review for changes touching integration credentials or inventory-decrement logic.
- **Change management:** any new event/message formats follow the schema-versioning and deprecation rules in `docs/event-contract.md`.

## 6. Staged Backlog & DoR/DoD

**R1 (≈2 weeks)**
- Per-channel, per-SKU safety-stock buffers
- SFTP/CSV pipeline: idempotency keys + checksums + DLQ for failed/duplicate order files
- Alerting on sync failures and stale-sync age

**R2 (≈4-6 weeks)**
- Shrink the inventory sync interval / move to push-based deltas where marketplace APIs allow
- SKU/variant mapping audit, reconciliation job, and drift alerting

**R3 (next quarter)**
- Event-driven order ingestion per channel, feeding a centralized inventory reservation layer
- Real-time WMS reservation/decrement integration
- Shadow-mode validation and cutover

### Definition of Ready (DoR)
- Hypothesis backed by data (oversell incidents, sync logs)
- A target SLI from section 2 identified
- Dependencies (WMS API, marketplace API) confirmed available or spiked
- Rollback/feature-flag plan defined

### Definition of Done (DoD)
- Code reviewed, tested, and security-scanned
- Dashboards/alerts updated for the relevant SLI
- Runbook updated with new failure modes
- Where applicable, validated in shadow mode against production data before cutover
