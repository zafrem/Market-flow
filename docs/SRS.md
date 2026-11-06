# Software Requirements Specification (SRS)
## Project: MarketFlow (Multi-Channel Market Analysis & Dynamic Price Optimization System)

**Document Version:** 1.2  
**Target Environment:** Local Dockerized Sandbox & Cloud Infrastructure  
**Author/Architect:** Development Team / System Architect  
**Status:** Draft – revised after review; pricing-strategy defaults (F-04-1) and legal review (F-01-2) pending confirmation  

**Revision note (1.2):** Fixed MySQL partition/FK incompatibility, defined the price recommendation algorithm, added product-matching, self-healing validation, execution-mode/approval, RBAC detail, observability NFRs, and reconciled test thresholds.

---

## 1. Introduction & Executive Summary

### 1.1 Purpose
MarketFlow is an automated, data-driven decision-execution platform engineered for retail operators, category managers (MDs), and marketplace sellers. Its core mission is to safeguard gross margins, prevent stock-out or write-off losses, and maximize inventory turnover. 

The system continuously acquires, normalizes, and reconciles:
1. **External Public Benchmarks:** Wholesale auction prices (KAMIS, Garak Market), consumer inflation monitors (KCA Price), and customs clearance statistics.
2. **External Retail Channels:** Live consumer listings, promotions, and stock levels across major e-commerce platforms (Coupang, Market Kurly, SSG, B-Mart, Oasis, Naver Smartstore).
3. **Internal Operational Datasets:** Landed cost of goods sold (COGS), variable packaging, cold-chain fulfillment, channel commission rates, real-time inventory levels, and shelf-life / shelf-aging metrics.

The platform executes dynamic price optimization, allocates liquidation channels, prevents negative gross margins (*reverse margins*), and detects supply vacuums.

### 1.2 Core Architectural Principles
* **Extensible Core + Domain Plugin Architecture:** The core engine is agnostic to product categories. Domain-specific business logic is implemented via decoupled plugins:
  * **Fresh Produce Plugin (Phase 1 Baseline):** Daily wholesale auction settlement indexing, $100\text{g}$ base conversion, shelf-life ($D\text{-Day}$) decay guard, and regional early-morning delivery zones.
  * **Packaged / Ambient Food & Household Plugin (Extension):** GTIN/EAN-13 barcode reconciliation, bundle/multipack price decomposition, holding-cost escalation monitoring.
  * **Imported Raw Materials Plugin (Extension):** Harmonized System (HS) code-level customs tariff and clearance tracking, real-time FX hedging calculation.
* **First-Class Testability & Verifiability:** Every component—from scraper pipelines to calculation algorithms—must expose deterministic entry points, test fixture injection interfaces, and mock harnesses to guarantee testability across unit, integration, and E2E tiers.

---

## 2. System Architecture & Infrastructure

### 2.1 Component Interaction Pipeline

```
+---------------------------------------------------------------------------------+
|                                External Ingestion                               |
|  +------------------------+  +--------------------------+  +-----------------+  |
|  | Public Benchmark APIs  |  | External Marketplaces    |  | Internal Data   |  |
|  | (KAMIS, Garak, Customs)|  | (Coupang, Kurly, SSG...) |  | (WMS, ERP, CSV) |  |
|  +-----------+------------+  +------------+-------------+  +--------+--------+  |
+--------------|----------------------------|-------------------------|-----------+
               v                            v                         v
+---------------------------------------------------------------------------------+
|                       MarketFlow Core Ingestion Worker                          |
|  +---------------------------------------------------------------------------+  |
|  | Multi-Source Pipeline (Python Asyncio / Playwright)                       |  |
|  |  - Human-Like Browser Navigation (Typing jitter, randomized mouse scroll)  |  |
|  |  - Anti-Bot Mitigation (Stealth plugins, residential IP rotation hooks)   |  |
|  +-------------------------------------+-------------------------------------+  |
|                                        v                                        |
|  +---------------------------------------------------------------------------+  |
|  | Self-Healing Module (AI Fallback)                                         |  |
|  |  - DOM change & extraction failure detection (NULL checks / Thresholds)   |  |
|  |  - Sanitized Markdown distillation -> Lightweight LLM (Gemini Flash)     |  |
|  |  - Schema-enforced JSON extraction & selector regeneration suggestion     |  |
|  +-------------------------------------+-------------------------------------+  |
|                                        v                                        |
|  +---------------------------------------------------------------------------+  |
|  | Normalization & Unit Parsing Engine                                       |  |
|  |  - Regex / Heuristic unit decomposition (e.g., 1.2kg -> 100g standard unit)  |  |
|  +-------------------------------------+-------------------------------------+  |
|                                        v                                        |
|  +---------------------------------------------------------------------------+  |
|  | Decision & Execution Engine                                               |  |
|  |  - Net Margin Calculation, Run-rate, Expiry risk vs D-Day, Floor guarding |  |
|  +---------------------------------------------------------------------------+  |
+----------------------------------------|----------------------------------------+
                                         v
+---------------------------------------------------------------------------------+
|                          Persistence Tier (MySQL 8.0)                           |
|  - Range-Partitioned Time-Series Tables (Benchmark, Scraped, Operations)        |
|  - Master Catalog & Normalized SKU Entities                                     |
+----------------------------------------|----------------------------------------+
                                         v
+---------------------------------------------------------------------------------+
|                     Presentation & API Tier (FastAPI + UI)                      |
|  - Matrix comparison view ([Benchmark] vs [Internal Cost] vs [Competitor])      |
|  - User watchlists & role-based rule alerts (Slack / Webhook / Toast)           |
|  - Deterministic Mock / Replay Test Harness Endpoint                            |
+---------------------------------------------------------------------------------+
```

### 2.2 Infrastructure & Deployment
* **Containerization:** The entire topology is orchestrated via a unified `docker-compose.yml` comprising:
  * `db`: MySQL 8.0 with dedicated volume binding (`./mysql_data:/var/lib/mysql`) for state persistence.
  * `collector-worker`: Python 3.11+ asynchronous batch & scraping service with Playwright.
  * `backend-api`: FastAPI REST service exposing decision outputs and simulation endpoints.
  * `frontend`: Single-page responsive operations dashboard.
  * `mock-server` (test profile only): WireMock / static-fixture server for scraper transport tests.
* **Time Zone Policy:** All `DATETIME` columns are stored in **UTC**. Cron schedules and business-day boundaries are defined in `Asia/Seoul` (KST) and converted at the application layer. `DATE` columns (`recorded_date`) represent the KST calendar date.
* **Configuration & Security:** All secrets, platform credentials, and API keys are strictly injected through `.env` runtime files and decoupled from Git tracking.

---

## 3. Comprehensive Data Model (Database Schema)

> **Schema note (MySQL constraint):** MySQL/InnoDB does **not** support foreign keys on partitioned tables. Columns marked `FK*` below are *logical* references to `master_products`; referential integrity for the three partitioned tables is enforced in the repository layer (validated on write, plus a nightly orphan-check job). Non-partitioned tables use real FKs. All money arithmetic uses `Decimal` (never binary floats).

### 3.1 Master Catalog (`master_products`)
Represents the normalized anchor for all cross-channel matching.

| Column Name | Logical Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- | :--- |
| `std_item_id` | Standard Product ID | `INT` | `PK, AUTO_INCREMENT` | Unique system identifier |
| `category_l` | Category Large | `VARCHAR(50)` | `NOT NULL` | Fresh, Ambient, Household, etc. |
| `category_m` | Category Medium | `VARCHAR(50)` | `NOT NULL` | Fruit, Meat, Detergent, Seasoning |
| `item_name` | Canonical Item Name | `VARCHAR(100)` | `NOT NULL` | Standard name (e.g., Apple, Onion) |
| `standard_unit` | Standard Metric Unit | `VARCHAR(20)` | `NOT NULL` | Canonical base: `100g`, `1kg`, `100ml`, `1unit` (rolls, sheets, pieces map to `1unit`) |
| `barcode` | Global Barcode | `VARCHAR(50)` | `UNIQUE, NULLABLE` | GTIN/EAN-13 code (can be null for fresh produce) |
| `custom_attributes` | Domain Extensions | `JSON` | `NULLABLE` | Grade, variety, packaging specs, `hs_code` (imported goods) |
| `created_at` | Record Created At | `DATETIME` | `DEFAULT CURRENT_TIMESTAMP` | Audit timestamp |

### 3.2 External Public Benchmarks (`ext_public_benchmarks`)
Time-series table capturing wholesale market auctions and government price indices.

| Column Name | Logical Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- | :--- |
| `benchmark_id` | Benchmark ID | `BIGINT` | `PK, AUTO_INCREMENT` | Composite PK with `recorded_date` |
| `recorded_date` | Observation Date | `DATE` | `PK, NOT NULL` | Partition key (`YYYY-MM-DD`) |
| `source_type` | Data Source | `VARCHAR(30)` | `NOT NULL` | `KAMIS`, `GARAK`, `KCA_PRICE`, `CUSTOMS` |
| `std_item_id` | Standard Product ID | `INT` | `FK*` -> master_products | Normalized item reference |
| `raw_item_name` | Raw Scraped Name | `VARCHAR(150)` | `NOT NULL` | Raw text captured from source |
| `avg_price` | Average Price | `DECIMAL(12,2)` | `NOT NULL` | Daily average settled price ($\text{KRW}$) |
| `min_price` | Minimum Price | `DECIMAL(12,2)` | `NOT NULL` | Lowest auction/survey price ($\text{KRW}$) |
| `max_price` | Maximum Price | `DECIMAL(12,2)` | `NOT NULL` | Highest auction/survey price ($\text{KRW}$) |
| `normalized_price` | Normalized Base Price | `DECIMAL(10,2)` | `NOT NULL` | Calculated price per `standard_unit` |
| `extra_metrics` | Additional Metrics | `JSON` | `NULLABLE` | Inflow volume ($\text{kg}$), count, exchange rate |

### 3.3 External Retail Market Prices (`ext_retail_prices`)
Time-series table capturing competitor listing snapshots.

| Column Name | Logical Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- | :--- |
| `retail_id` | Retail Snapshot ID | `BIGINT` | `PK, AUTO_INCREMENT` | Composite PK with `scraped_at` |
| `scraped_at` | Scrape Timestamp | `DATETIME` | `PK, NOT NULL` | Partition key |
| `platform` | E-Commerce Platform | `VARCHAR(30)` | `NOT NULL` | `COUPANG`, `KURLY`, `SSG`, `OASIS`, `BMART`, `NAVER` |
| `std_item_id` | Standard Product ID | `INT` | `FK*` -> master_products | Normalized item reference (resolved via `product_channel_map`, §3.6) |
| `match_confidence` | Match Confidence | `DECIMAL(4,3)` | `NOT NULL` | 0–1 score from product matcher; <0.8 excluded from price recommendations |
| `channel_item_code` | Channel SKU ID | `VARCHAR(100)` | `NOT NULL` | Platform-specific identifier |
| `product_title` | Raw Listing Title | `VARCHAR(255)` | `NOT NULL` | Full listing string |
| `original_price` | List Retail Price | `INT` | `NOT NULL` | Pre-discount strike-through price |
| `sale_price` | Realized Sale Price | `INT` | `NOT NULL` | Net listing price presented to customer |
| `normalized_unit_price` | Unit Comparison Price | `DECIMAL(10,2)` | `NOT NULL` | Derived price per canonical `standard_unit` |
| `is_out_of_stock` | Stock-out Flag | `TINYINT(1)` | `DEFAULT 0` | `0`: In Stock, `1`: Out of Stock |
| `badge_info` | Promo / Logistics Badge| `VARCHAR(100)` | `NULLABLE` | `RocketFresh`, `TimeSale`, `Closeout` |
| `extraction_source` | Extraction Source | `VARCHAR(10)` | `DEFAULT 'SELECTOR'` | `SELECTOR`, `API`, `LLM` (see F-02-4) |
| `region_code` | Postal Code Zone | `VARCHAR(10)` | `NOT NULL` | Target delivery zip/zone for cold chain |

### 3.4 Internal Operations & Inventory (`int_operations_inventory`)
Internal cost accounting, warehouse balance, and velocity snapshot.

| Column Name | Logical Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- | :--- |
| `internal_id` | Internal Record ID | `BIGINT` | `PK, AUTO_INCREMENT` | Composite PK with `recorded_date` |
| `recorded_date` | Ledger Date | `DATE` | `PK, NOT NULL` | Partition key |
| `std_item_id` | Standard Product ID | `INT` | `FK*` -> master_products | Normalized item reference |
| `internal_sku` | Internal ERP SKU | `VARCHAR(50)` | `NOT NULL` | ERP / WMS catalog code |
| `channel_name` | Operational Channel | `VARCHAR(50)` | `NOT NULL` | Target channel listing |
| `purchase_cost` | Landed COGS | `DECIMAL(10,2)` | `NOT NULL` | Direct unit acquisition cost |
| `fixed_cost` | Processing & Packaging | `DECIMAL(10,2)` | `NOT NULL` | Labor, packaging, cushioning fees |
| `logistics_cost` | Fulfillment & Storage | `DECIMAL(10,2)` | `NOT NULL` | Inbound, handling, storage, delivery |
| `channel_fee_rate` | Channel Commission % | `DECIMAL(5,2)` | `NOT NULL` | Stored as **percent** (e.g., `11.50` = 11.5%). Formulas MUST divide by 100. |
| `promo_cost` | Promotion / Coupon Cost | `DECIMAL(10,2)` | `DEFAULT 0` | Per-unit coupon/promo subsidy borne by seller |
| `vat_inclusive` | VAT Basis Flag | `TINYINT(1)` | `DEFAULT 1` | `1`: selling price includes VAT (margin computed on VAT-exclusive price) |
| `current_selling_price`| Live System Price | `INT` | `NOT NULL` | Current active listing price |
| `stock_qty` | Available Stock | `INT` | `NOT NULL` | Available on hand + allocated cross-dock |
| `daily_sales_qty` | Daily Units Sold | `INT` | `NOT NULL` | Units dispatched on `recorded_date`; the 3-day average (F-03-3) is derived from the last 3 daily rows |
| `pack_std_units` | Standard Units per Sale Unit | `DECIMAL(10,4)` | `DEFAULT 1` | Converts competitor per-`standard_unit` prices to this listing's pack (e.g. 1.2 kg = 12 × `100g`) |
| `min_price_floor` | Floor Guard Price | `INT` | `NOT NULL` | Computed by the system as the lowest integer price whose net margin ≥ `target_min_margin_pct` (closed form `ceil(costs / (k − fee/100))`, `k = (1 − target)/(1 + VAT)` if VAT-inclusive else `(1 − target)`, verified exactly in integer search). Manual override allowed (ADMIN, audited). Recomputed on cost-input changes. |
| `lifecycle_metric` | Expiry / Age Matrix | `JSON` | `NOT NULL` | `remaining_days` ($D\text{-Day}$), `holding_age_days` |

### 3.5 Personalization & Watchlists (`users`, `user_category_assignments`, `user_watchlists`, `user_preferences`)
* `users`: `user_id` (PK), `email`, `name`, `role` (`ADMIN`, `MD`, `BUYER`), `password_hash` (scrypt), `active`.
* `user_category_assignments`: `user_id` (FK), `category_l`, `category_m` (NULL = all) — defines the scope used by F-05-1.
* `user_watchlists`: `watch_id` (PK), `user_id` (FK), `std_item_id` (FK), `sort_order`.
* `user_preferences`: `pref_id` (PK), `user_id` (FK), `alert_margin_bep` (`DECIMAL(5,2)`), `ui_layout` (`JSON`).

### 3.6 Channel Product Mapping & Audit (`product_channel_map`, `price_recommendations`, `audit_log`)
* `product_channel_map`: `platform`, `channel_item_code` (composite PK), `std_item_id` (FK), `pack_size_factor` (`DECIMAL(10,4)`), `match_method` (`BARCODE`, `RULE`, `LLM`, `MANUAL`), `match_confidence`, `reviewed_by`, `reviewed_at`. Matching order: barcode exact → rule/token match (variety, grade, origin, pack size) → LLM-assisted suggestion. Matches below 0.8 confidence enter a manual review queue and are excluded from pricing.
* `price_recommendations`: `rec_id` (PK), `created_at`, `std_item_id`, `channel_name`, `current_price`, `suggested_price`, `reason_code`, `flags` (`JSON`), `status` (`PENDING`, `APPROVED`, `REJECTED`, `APPLIED`, `SUPPRESSED`, `REVERTED`), `decided_by` (actor name), `decided_at` (also the cool-down clock: set again on apply/revert).
* `audit_log`: append-only record of price changes, floor overrides, role changes, selector approvals, and match reviews (`actor`, `action`, `before`, `after`, `ts`).

---

## 4. Functional Requirements

### F-01: Multi-Source Data Collection Engine
* **F-01-1 (Public Benchmark Sync):** Scheduled cron workers execute daily at `06:00` and `09:00 KST` to parse KAMIS and Garak Market auction APIs. Weekly scheduled cron executes for KCA Price.
* **F-01-2 (External Retail Scraper):** Executes thrice daily at `08:00` (Morning cutoff), `13:00` (Midday sync), and `19:00` (Clearance markdowns).
  * Implements **Human-Like Navigation**: Simulates user keystroke intervals ($50\text{--}150\text{ms}$ jitter via `page.type()`), randomized smooth page scrolling ($200\text{--}500\text{px}$ increments with random pauses), and dynamic viewport/session variation.
  * Incorporates context persistence and localized headers (`ko-KR`, `Asia/Seoul`).
  * **Compliance gate (required before production use):** Each platform connector declares its access method (`OFFICIAL_API`, `PARTNER_FEED`, `SCRAPE`). Official APIs/feeds MUST be preferred. A `SCRAPE` connector may be enabled only after a documented legal review of the platform's ToS and applicable Korean law, recorded in the connector's config (`legal_review_ref`). Connectors enforce `robots.txt`/rate limits per config. Anti-bot mitigation features (stealth, IP rotation) are disabled by default and require explicit per-connector enablement after that review.
* **F-01-3 (Internal Data Synchronization):** Provides REST endpoints and an asynchronous CSV/XLSX drag-and-drop batch parser for WMS/ERP inventory dumps.

### F-02: AI Self-Healing Scraper
* **F-02-1 (Failure Detection):** If standard CSS/XPath selectors fail for 3 consecutive attempts or return critical fields (`sale_price`, `product_title`) as `NULL`, the scraper intercepts execution and raises a `ScrapeExtractionFault`.
* **F-02-2 (LLM Fallback Extraction):**
  * Strips scripts, style tags, SVG assets, and base64 payloads to construct sanitized, token-efficient HTML/Markdown.
  * Dispatches payload to Gemini Flash with structured schema constraints:
    $$\text{Payload} \xrightarrow{\text{Gemini Flash}} \{\text{product\_title}, \text{sale\_price}, \text{original\_price}, \text{is\_out\_of\_stock}, \text{badge\_info}\}$$
* **F-02-3 (Selector Re-Synthesis):** Uses LLM token match locations to derive new robust selector candidates and emits an alert to Slack. Candidates are persisted in `selector_candidates`; they are **promoted to the persistent selector set only after human approval** (audited).
* **F-02-4 (Extraction Validation):** Every LLM-extracted record MUST pass sanity checks before persistence: `sale_price > 0`, `sale_price <= original_price`, and deviation from the last accepted price for the same `channel_item_code` within a configurable bound (default ±50%). Failing records are quarantined (not written to `ext_retail_prices`) and alerted. Persisted LLM-sourced rows carry `extraction_source = 'LLM'` (§3.3) so they can be filtered.

### F-03: Data Normalization & Metric Computations
* **F-03-1 (Unit Standardization):** Uses regular expressions and token dictionaries to parse ambiguous descriptions:
  $$\text{"[Organic] Seongju Melon 1.2kg (4-6 units)"} \implies \text{Total Weight} = 1200\text{g} \implies \text{Rate} = \frac{\text{Sale Price}}{12} \text{ per } 100\text{g}$$
* **F-03-2 (Realized Net Margin Engine):**
  Let $P_{net} = \text{Selling Price}/1.10$ if `vat_inclusive = 1` (Korean VAT 10%), else $\text{Selling Price}$. Commission rate $r = \text{channel\_fee\_rate}/100$.
  $$\text{Net Margin Amount} = P_{net} - \left(\text{COGS} + \text{Packaging} + \text{Logistics} + \text{Promo} + (\text{Selling Price} \times r)\right)$$
  $$\text{Net Margin Rate (\%)} = \left(\frac{\text{Net Margin Amount}}{P_{net}}\right) \times 100$$
  Computation uses `Decimal`; results are rounded half-up to 2 decimals. (Commission base — VAT-inclusive vs exclusive — is configurable per channel; default VAT-inclusive.)
* **F-03-3 (Inventory Run-Rate & Exhaustion Velocity):**
  $$\text{Run-Rate (Days to Exhaust)} = \frac{\text{Current Available Stock}}{\text{Average Daily Sales (Last 3 Days)}}$$
  If the 3-day average is $0$, Run-Rate is `INFINITE` (no exhaustion projected); if fewer than 3 days of history exist, the average uses available days. Downstream rules MUST handle `INFINITE` explicitly.

### F-04: Action & Decision Optimization Engine
* **F-04-1 (Dynamic Price Floor Guarding):**
  * **Recommendation algorithm (default strategy `COMPETITIVE_MATCH`, configurable per category):**
    1. Gather competitor normalized unit prices for the item from in-stock listings with `match_confidence >= 0.8`, in the same `region_code`, scraped within the last 24h. If none, produce no recommendation.
    2. Reference price $R$ = the lowest competitor price, converted to this item's pack size via `pack_size_factor`.
    3. Target $T = R \times (1 - u)$, where $u$ is the undercut ratio (default `0%`, i.e. match; range 0–5%).
    4. Cap movement: $|T - \text{current}| / \text{current} \le$ `max_step` (default 10% per run) to avoid oscillation; apply a cool-down (default 6h) after any applied change. The cool-down holds only the price: spoilage and reverse-margin warnings are still raised (low-margin and wholesale-guard stay quiet, as they follow from the change just applied) (recommendation `SUPPRESSED`, reason `COOLDOWN_HOLD`, held price in `flags.COOLDOWN_HOLD`).
    5. Wholesale guard: if the public benchmark index rose more than a configurable threshold since the last run, do not recommend a decrease for that item.
    6. Clamp to floor: Suggested Price $= \max(T, \text{min\_price\_floor})$.
    Alternative strategies (`MARGIN_TARGET`, `BENCHMARK_MARKUP`) implement the same `PricingStrategy` interface and are selected via configuration.
  * If the unclamped target $T < \text{min\_price\_floor}$, suppress automatic downward adjustment, keep the current price (or floor, if the current price is already below it), and trigger a `REVERSE_MARGIN_WARNING` (Level: RED).
* **F-04-0 (Execution Mode & Approval):** Default mode is **advisory**: recommendations are written to `price_recommendations` with status `PENDING` and require MD/ADMIN approval. Auto-apply is opt-in per item/category, requires an ADMIN-defined guard set (floor, `max_step`, cool-down), and writes back to channels only through the channel connector's official API. Every decision and applied change is recorded in `audit_log` and can be reverted to the prior price in one action.
* **F-04-2 (Expiration & Aging Defensive Actions):**
  * If $\text{Remaining Shelf Life }(D\text{-Day}) \le \text{Run-Rate (Days)} + \text{safety\_buffer}$ (buffer default `0` days; configurable per category, e.g. last-mile handling), immediately assert `SPOILAGE_RISK_FLAG`. An `INFINITE` Run-Rate with stock > 0 always asserts the flag.
  * Suggest automated actions: Trigger flash discount coupons or reroute stock to secondary/offline markdown channels.
* **F-04-3 (Supply Vacuum Exploitation):**
  * If competitor listings transition to $\text{is\_out\_of\_stock} = 1$ while internal inventory is healthy ($\text{Run-Rate} > 7\text{ days}$) **and `SPOILAGE_RISK_FLAG` is not asserted** (spoilage takes precedence), recommend an opportunistic upward price adjustment of $+5\%$ to $+15\%$.

### F-05: User Interface & Personalization
* **F-05-1 (Role-Based Watchlist):** Category managers access filtered matrices displaying only assigned items (`category_l`, `category_m`).
* **F-05-2 (Three-Way Comparison Matrix):** Side-by-side visual mapping across:
  $$\text{[Public Wholesale Index]} \longleftrightarrow \text{[Internal Landed Cost \& Active Price]} \longleftrightarrow \text{[Competitor Scraped Prices]}$$
* **F-05-3 (Configurable Notification Rules):** Webhook/Slack alert dispatches when margin drops below threshold ($\le 10\%$ default; per category the highest `alert_margin_bep` among active ADMINs and the MDs assigned to it, BUYERs ignored) or stock-out events occur. Alerts are de-duplicated per (rule, item, channel) with a configurable suppression window (default 6h, persisted in `alert_log`), rate-limited, and retried with exponential backoff (max 5 attempts) before landing in a dead-letter log.

### F-06: Market Intelligence (Category Health + Trend Radar)
Implemented per `docs/MI_FRAMEWORK.md`. Demand and interest signals (SEARCH, BUYER, SKU, PRODUCT, PROMOTION, MEDIA) are imported from CSV/XLSX (`import-mi`, or admin-only `POST /mi/import`) into `mi_observations` (long format, upsert on signal/period/category/keyword/metric).
* **F-06-1 (Category Health):** per category, demand direction (GMV, else units sold; recent mean vs prior mean), demand-share shift in percentage points, stock cover from `int_operations_inventory` (TIGHT < 3 days, SURPLUS > 14), and a Search→demand lead test: period-over-period changes correlated at lags 0..2, accepted only above both `lead_min_r` and a one-sided p<0.01 critical value, otherwise search is reported as interest, not demand.
* **F-06-2 (Trend Radar):** per keyword, a lifecycle stage (SEED / EMERGING / GROWING / MAINSTREAM / FADING) from search growth, volume percentile among at least three peers in its category, and corroboration by PRODUCT / PROMOTION / MEDIA mentions; a weighted score (weights configurable in `[mi.weights]`) and an opportunity score (score × (1 − volume percentile)).
* **F-06-3 (Presentation and access):** every row carries "what changed / why it matters / what to do". Users see only their assigned `category_l` values; rows without a category are admin-only; no cost data is involved.

---

## 5. Non-Functional & Operational Requirements

### 5.1 Performance & Scalability (N-01)
* **Storage Partitioning:** `ext_public_benchmarks`, `ext_retail_prices`, and `int_operations_inventory` are partitioned using MySQL `RANGE PARTITIONING` based on date:
  ```sql
  -- recorded_date tables (DATE)
  PARTITION BY RANGE COLUMNS (recorded_date) (
    PARTITION p202601 VALUES LESS THAN ('2026-02-01'), ...
  )
  -- ext_retail_prices (DATETIME, partition key scraped_at)
  PARTITION BY RANGE (TO_DAYS(scraped_at)) ( ... )
  ```
  Both forms support partition pruning on range predicates (a composite `YEAR()*100+MONTH()` expression does not). A scheduled job pre-creates the next 3 monthly partitions. Composite indexes: `(std_item_id, recorded_date)` / `(std_item_id, scraped_at)`.
* **Query Latency (single reference definition):**
  | Dataset | Query | Target |
  | :--- | :--- | :--- |
  | 10M+ rows | Multi-year analytical query on `(std_item_id, recorded_date)` | $\le 2.0\text{ s}$ |
  | 5M rows / 24 partitions | Single-item 90-day time-series (stress test) | $< 500\text{ ms}$, `EXPLAIN` shows pruning |
  | Small mock set | Repository-layer integration query | $\le 200\text{ ms}$ |

### 5.2 Containerization & Deployment (N-02)
* The entire system must launch deterministically from a clean state using a single command:
  ```bash
  docker-compose up --build -d
  ```

### 5.3 Security, Integrity & Access Control (N-03)
* **Authentication:** JWT-based sessions issued after login (OIDC/SSO-compatible); tokens short-lived with refresh; all API routes require authentication except health checks.
* **RBAC matrix:**
  | Capability | ADMIN | MD | BUYER |
  | :--- | :---: | :---: | :---: |
  | View public benchmarks / competitor prices | ✔ | ✔ | ✔ |
  | View COGS, cost sheets, margins | ✔ | ✔ (assigned categories) | ✘ (masked) |
  | Approve/reject price recommendations | ✔ | ✔ (assigned categories) | ✘ |
  | Edit floors, auto-apply guards, strategies | ✔ | ✘ | ✘ |
  | Approve selectors / product matches | ✔ | ✔ | ✘ |
  | Manage users & assignments | ✔ | ✘ | ✘ |
  Masking is enforced server-side in the API layer (fields omitted, not merely hidden in the UI).
* **Credentials:** No secrets in source control. Verified via pre-commit git-secrets hooks.

### 5.4 Reliability, Observability & Data Lifecycle (N-04)
* **Observability:** Structured JSON logs with correlation IDs per job run; metrics for scrape success rate, LLM-fallback rate, quarantined-record count, job duration, and alert delivery; health/readiness endpoints for every service.
* **Scraper failure behavior:** Per-connector retry with backoff; a connector failing for 3 consecutive scheduled runs raises an alert and stale data is marked (`data_age` shown in the UI); decisions MUST NOT use competitor data older than 24h.
* **Backup & retention:** Daily MySQL logical backup with restore tested monthly; retention default 3 years for time-series tables (older partitions archived/dropped); `audit_log` retained indefinitely.
* **Availability target:** Dashboard/API 99.5% monthly; collection jobs are idempotent and safe to re-run.

---

## 6. Testability Specifications & Verification Harness

To ensure the system can be validated automatically by an individual developer, the architecture mandates dedicated testing interfaces.

### 6.1 Testing Tier Requirements

| Test Tier | Scope | Target Tech Stack | Pass Criteria |
| :--- | :--- | :--- | :--- |
| **Unit Test** | Unit conversion parser, margin formulas, Run-rate logic, pricing strategy | `pytest`, `pytest-mock` | $100\%$ formula edge coverage; all money math in `Decimal`, exact equality except unit-price rounding (±0.01 KRW) |
| **Integration Test** | Repository layer, range partitions, DB indexing | `pytest`, Testcontainers (MySQL) | Partition pruning verified; query latency per §5.1 table; logical-FK orphan checks pass |
| **Contract / AI Test**| Self-healing JSON fallback, LLM schema conformance | `pytest`, VCR.py / recorded fixtures | $100\%$ schema validity on mocked malformed HTML inputs |
| **E2E Pipeline Test** | Search -> Scrape -> Self-Heal -> DB -> UI Matrix API | Playwright mock server + FastAPI TestClient | Full run completes without unhandled exceptions |

### 6.2 Testability Hooks & Mock Injection Points

1. **Scraper Pluggable Network Transports:**
   * The scraper engine must support a decoupled network transport adapter. During automated tests, network calls are redirected to a local `WireMock` or static fixture directory serving pre-recorded e-commerce HTML snapshots.
2. **Deterministic Time Provider:**
   * Time-sensitive functions (e.g., $D\text{-Day}$ calculation, Run-rate, partition targeting) must consume a mockable `TimeProvider` interface rather than system clock (`datetime.now()`).
3. **LLM Cost & Response Stubbing:**
   * The Self-Healing subsystem must implement an offline mock mode (`MOCK_LLM=true`) returning deterministic JSON payloads to permit continuous CI testing without invoking paid APIs.

### 6.3 Test Verification Matrix

| Requirement ID | Verification Method | Acceptance Test Description |
| :--- | :--- | :--- |
| **F-02-1 / F-02-2** | Automated Integration | Inject HTML with mutated class names. Assert that CSS selectors fail, `ScrapeExtractionFault` is caught, LLM fallback is triggered, and valid JSON is parsed into the database. |
| **F-03-1** | Automated Unit | Pass an array of 50 edge-case title strings (e.g., `"1.2kg(600g x 2)"`, `"500ml x 6"`, `"휴지 30롤"` → per `1unit`). Titles whose unit cannot be resolved (e.g., `"30m 30rolls"`) MUST return an explicit `UNPARSEABLE` result and be routed to review, not guessed. Assert calculated `normalized_unit_price` matches expected value within $\pm 0.01\text{ KRW}$. |
| **F-03-2 / F-04-1** | Automated Unit | Simulate scenario where competitor price drops by $40\%$. Verify that suggested price does not breach `min_price_floor` and `REVERSE_MARGIN_WARNING` is raised. |
| **F-04-2** | Automated Unit | Set available inventory $= 500$, 3-day avg sales $= 50$ (Run-rate $= 10\text{ days}$). Set $D\text{-Day} = 5\text{ days}$. Verify `SPOILAGE_RISK_FLAG` asserts `TRUE`. |
| **N-01** | Automated Stress | Seed `ext_retail_prices` with 5,000,000 synthetic rows across 24 monthly partitions (and a 10M-row run for the 2.0 s analytical target). Execute single-item 90-day time-series query. Verify `EXPLAIN` shows partition pruning and execution time $< 500\text{ms}$. |
| **F-02-4** | Automated Contract | Feed LLM mock output with `sale_price = 0`, `sale_price > original_price`, and a +300% jump. Assert each is quarantined and not written to `ext_retail_prices`. |
| **F-03-3** | Automated Unit | Daily sales $= 0$ → Run-rate `INFINITE`; only 2 days of history → average over 2 days; no division error. |
| **F-04-0 / F-04-1** | Automated Unit/Integration | Default mode creates `PENDING` recommendations only; step cap, cool-down and wholesale guard honored; approve → `APPLIED` with `audit_log` entry; revert restores prior price. |
| **F-04-3** | Automated Unit | Competitor out of stock, Run-rate $= 10$, $D$-Day $= 5$ → no upward recommendation (spoilage precedence). |
| **F-05-1 / N-03** | Automated API | BUYER token receives no cost/margin fields; MD sees only assigned categories; ADMIN-only endpoints return 403 for others. |
| **F-05-3** | Automated Integration | Duplicate trigger within suppression window sends one alert; webhook failure retries then dead-letters. |
| **F-01-3** | Automated Integration | Upload CSV/XLSX with valid, malformed, and duplicate rows; valid rows load, invalid rows reported with line numbers. |

---

## 7. Implementation Roadmap & Milestones

* **Phase 1: Foundation, DB Engine & Public Ingestion (Weeks 1–2)**
  * Cross-cutting test seams first: `TimeProvider`, pluggable network transport, `MOCK_LLM` stub (§6.2).
  * Docker Compose scaffolding (MySQL 8.0 with Volume Mounts and Range Partitions).
  * Data Definition Language (DDL) execution and Seed Master Catalog (`master_products`).
  * KAMIS and Garak Market API collectors with test suites.
* **Phase 2: Scraper Harness, Human Simulation & AI Self-Healing (Weeks 3–4)**
  * Playwright scraper framework with typing jitter, scrolling, and random delay models.
  * Markdown distillation pipeline and Gemini Flash JSON schema integration.
  * Regression test suite using static HTML failure fixtures.
* **Phase 3: ERP Integration, Product Matching, Decision Engine & Action Logic (Weeks 5–6)**
  * Internal CSV/Excel parsing endpoint and schema validation.
  * `product_channel_map` matcher and manual review queue.
  * Dynamic pricing strategy, margin calculations, Run-rate, and Spoilage alert engine; recommendation/approval workflow and audit log.
  * Verification of all business logic equations via automated unit tests.
* **Phase 4: Auth/RBAC, Matrix Dashboard, Personalization & System Verification (Weeks 7–9)**
  * JWT auth, RBAC enforcement and field masking (§5.3); observability and backup jobs (§5.4).
  * FastAPI query service and personalized Watchlist API endpoints.
  * Responsive Three-Way Comparison Matrix Dashboard UI.
  * End-to-end sandbox verification and containerized deployment dry run.