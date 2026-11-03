# Software Requirements Specification (SRS)
## Project: MarketFlow (Multi-Channel Market Analysis & Dynamic Price Optimization System)

**Document Version:** 1.1  
**Target Environment:** Local Dockerized Sandbox & Cloud Infrastructure  
**Author/Architect:** Development Team / System Architect  
**Status:** Approved for Implementation & Automated Verification  

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
* **Configuration & Security:** All secrets, platform credentials, and API keys are strictly injected through `.env` runtime files and decoupled from Git tracking.

---

## 3. Comprehensive Data Model (Database Schema)

### 3.1 Master Catalog (`master_products`)
Represents the normalized anchor for all cross-channel matching.

| Column Name | Logical Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- | :--- |
| `std_item_id` | Standard Product ID | `INT` | `PK, AUTO_INCREMENT` | Unique system identifier |
| `category_l` | Category Large | `VARCHAR(50)` | `NOT NULL` | Fresh, Ambient, Household, etc. |
| `category_m` | Category Medium | `VARCHAR(50)` | `NOT NULL` | Fruit, Meat, Detergent, Seasoning |
| `item_name` | Canonical Item Name | `VARCHAR(100)` | `NOT NULL` | Standard name (e.g., Apple, Onion) |
| `standard_unit` | Standard Metric Unit | `VARCHAR(20)` | `NOT NULL` | Canonical base: `100g`, `1kg`, `100ml`, `1unit` |
| `barcode` | Global Barcode | `VARCHAR(50)` | `NULLABLE` | GTIN/EAN-13 code (can be null for fresh produce) |
| `custom_attributes` | Domain Extensions | `JSON` | `NULLABLE` | Grade, variety, packaging specs |
| `created_at` | Record Created At | `DATETIME` | `DEFAULT CURRENT_TIMESTAMP` | Audit timestamp |

### 3.2 External Public Benchmarks (`ext_public_benchmarks`)
Time-series table capturing wholesale market auctions and government price indices.

| Column Name | Logical Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- | :--- |
| `benchmark_id` | Benchmark ID | `BIGINT` | `PK, AUTO_INCREMENT` | Composite PK with `recorded_date` |
| `recorded_date` | Observation Date | `DATE` | `PK, NOT NULL` | Partition key (`YYYY-MM-DD`) |
| `source_type` | Data Source | `VARCHAR(30)` | `NOT NULL` | `KAMIS`, `GARAK`, `KCA_PRICE`, `CUSTOMS` |
| `std_item_id` | Standard Product ID | `INT` | `FK -> master_products` | Normalized item reference |
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
| `std_item_id` | Standard Product ID | `INT` | `FK -> master_products` | Normalized item reference |
| `channel_item_code` | Channel SKU ID | `VARCHAR(100)` | `NOT NULL` | Platform-specific identifier |
| `product_title` | Raw Listing Title | `VARCHAR(255)` | `NOT NULL` | Full listing string |
| `original_price` | List Retail Price | `INT` | `NOT NULL` | Pre-discount strike-through price |
| `sale_price` | Realized Sale Price | `INT` | `NOT NULL` | Net listing price presented to customer |
| `normalized_unit_price` | Unit Comparison Price | `DECIMAL(10,2)` | `NOT NULL` | Derived price per canonical `standard_unit` |
| `is_out_of_stock` | Stock-out Flag | `TINYINT(1)` | `DEFAULT 0` | `0`: In Stock, `1`: Out of Stock |
| `badge_info` | Promo / Logistics Badge| `VARCHAR(100)` | `NULLABLE` | `RocketFresh`, `TimeSale`, `Closeout` |
| `region_code` | Postal Code Zone | `VARCHAR(10)` | `NOT NULL` | Target delivery zip/zone for cold chain |

### 3.4 Internal Operations & Inventory (`int_operations_inventory`)
Internal cost accounting, warehouse balance, and velocity snapshot.

| Column Name | Logical Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- | :--- |
| `internal_id` | Internal Record ID | `BIGINT` | `PK, AUTO_INCREMENT` | Composite PK with `recorded_date` |
| `recorded_date` | Ledger Date | `DATE` | `PK, NOT NULL` | Partition key |
| `std_item_id` | Standard Product ID | `INT` | `FK -> master_products` | Normalized item reference |
| `internal_sku` | Internal ERP SKU | `VARCHAR(50)` | `NOT NULL` | ERP / WMS catalog code |
| `channel_name` | Operational Channel | `VARCHAR(50)` | `NOT NULL` | Target channel listing |
| `purchase_cost` | Landed COGS | `DECIMAL(10,2)` | `NOT NULL` | Direct unit acquisition cost |
| `fixed_cost` | Processing & Packaging | `DECIMAL(10,2)` | `NOT NULL` | Labor, packaging, cushioning fees |
| `logistics_cost` | Fulfillment & Storage | `DECIMAL(10,2)` | `NOT NULL` | Inbound, handling, storage, delivery |
| `channel_fee_rate` | Channel Commission % | `DECIMAL(5,2)` | `NOT NULL` | Channel take-rate (e.g., $11.50\%$) |
| `current_selling_price`| Live System Price | `INT` | `NOT NULL` | Current active listing price |
| `stock_qty` | Available Stock | `INT` | `NOT NULL` | Available on hand + allocated cross-dock |
| `daily_sales_qty` | Trailing Daily Velocity| `INT` | `NOT NULL` | Units dispatched in prior 24 hours |
| `min_price_floor` | Floor Guard Price | `INT` | `NOT NULL` | Absolute minimum price to prevent loss |
| `lifecycle_metric` | Expiry / Age Matrix | `JSON` | `NOT NULL` | `remaining_days` ($D\text{-Day}$), `holding_age_days` |

### 3.5 Personalization & Watchlists (`users`, `user_watchlists`, `user_preferences`)
* `users`: `user_id` (PK), `email`, `name`, `role` (`ADMIN`, `MD`, `BUYER`).
* `user_watchlists`: `watch_id` (PK), `user_id` (FK), `std_item_id` (FK), `sort_order`.
* `user_preferences`: `pref_id` (PK), `user_id` (FK), `alert_margin_bep` (`DECIMAL(5,2)`), `ui_layout` (`JSON`).

---

## 4. Functional Requirements

### F-01: Multi-Source Data Collection Engine
* **F-01-1 (Public Benchmark Sync):** Scheduled cron workers execute daily at `06:00` and `09:00 KST` to parse KAMIS and Garak Market auction APIs. Weekly scheduled cron executes for KCA Price.
* **F-01-2 (External Retail Scraper):** Executes thrice daily at `08:00` (Morning cutoff), `13:00` (Midday sync), and `19:00` (Clearance markdowns).
  * Implements **Human-Like Navigation**: Simulates user keystroke intervals ($50\text{--}150\text{ms}$ jitter via `page.type()`), randomized smooth page scrolling ($200\text{--}500\text{px}$ increments with random pauses), and dynamic viewport/session variation.
  * Incorporates context persistence and localized headers (`ko-KR`, `Asia/Seoul`).
* **F-01-3 (Internal Data Synchronization):** Provides REST endpoints and an asynchronous CSV/XLSX drag-and-drop batch parser for WMS/ERP inventory dumps.

### F-02: AI Self-Healing Scraper
* **F-02-1 (Failure Detection):** If standard CSS/XPath selectors fail for 3 consecutive attempts or return critical fields (`sale_price`, `product_title`) as `NULL`, the scraper intercepts execution and raises a `ScrapeExtractionFault`.
* **F-02-2 (LLM Fallback Extraction):**
  * Strips scripts, style tags, SVG assets, and base64 payloads to construct sanitized, token-efficient HTML/Markdown.
  * Dispatches payload to Gemini Flash with structured schema constraints:
    $$\text{Payload} \xrightarrow{\text{Gemini Flash}} \{\text{product\_title}, \text{sale\_price}, \text{original\_price}, \text{is\_out\_of\_stock}, \text{badge\_info}\}$$
* **F-02-3 (Selector Re-Synthesis):** Uses LLM token match locations to derive new robust selector candidates, emits an alert to Slack, and caches the selector in Redis/memory to prevent repeated LLM calls during the current run.

### F-03: Data Normalization & Metric Computations
* **F-03-1 (Unit Standardization):** Uses regular expressions and token dictionaries to parse ambiguous descriptions:
  $$\text{"[Organic] Seongju Melon 1.2kg (4-6 units)"} \implies \text{Total Weight} = 1200\text{g} \implies \text{Rate} = \frac{\text{Sale Price}}{12} \text{ per } 100\text{g}$$
* **F-03-2 (Realized Net Margin Engine):**
  $$\text{Net Margin Amount} = \text{Selling Price} - \left(\text{COGS} + \text{Packaging} + \text{Logistics} + (\text{Selling Price} \times \text{Commission Rate})\right)$$
  $$\text{Net Margin Rate (\%)} = \left(\frac{\text{Net Margin Amount}}{\text{Selling Price}}\right) \times 100$$
* **F-03-3 (Inventory Run-Rate & Exhaustion Velocity):**
  $$\text{Run-Rate (Days to Exhaust)} = \frac{\text{Current Available Stock}}{\text{Average Daily Sales (Last 3 Days)}}$$

### F-04: Action & Decision Optimization Engine
* **F-04-1 (Dynamic Price Floor Guarding):**
  * Evaluates competitor price updates and wholesale fluctuations to compute the target recommendation price.
  * If $\text{Suggested Price} < \text{min\_price\_floor}$, suppress automatic downward adjustment and trigger a `REVERSE_MARGIN_WARNING` (Level: RED).
* **F-04-2 (Expiration & Aging Defensive Actions):**
  * If $\text{Remaining Shelf Life }(D\text{-Day}) \le \text{Run-Rate (Days)}$, immediately assert `SPOILAGE_RISK_FLAG`.
  * Suggest automated actions: Trigger flash discount coupons or reroute stock to secondary/offline markdown channels.
* **F-04-3 (Supply Vacuum Exploitation):**
  * If competitor listings transition to $\text{is\_out\_of\_stock} = 1$ while internal inventory is healthy ($\text{Run-Rate} > 7\text{ days}$), recommend an opportunistic upward price adjustment of $+5\%$ to $+15\%$.

### F-05: User Interface & Personalization
* **F-05-1 (Role-Based Watchlist):** Category managers access filtered matrices displaying only assigned items (`category_l`, `category_m`).
* **F-05-2 (Three-Way Comparison Matrix):** Side-by-side visual mapping across:
  $$\text{[Public Wholesale Index]} \longleftrightarrow \text{[Internal Landed Cost \& Active Price]} \longleftrightarrow \text{[Competitor Scraped Prices]}$$
* **F-05-3 (Configurable Notification Rules):** Webhook/Slack alert dispatches when margin drops below threshold ($\le 10\%$) or stock-out events occur.

---

## 5. Non-Functional & Operational Requirements

### 5.1 Performance & Scalability (N-01)
* **Storage Partitioning:** `ext_public_benchmarks`, `ext_retail_prices`, and `int_operations_inventory` are partitioned using MySQL `RANGE PARTITIONING` based on date:
  ```sql
  PARTITION BY RANGE (YEAR(recorded_date) * 100 + MONTH(recorded_date))
  ```
* **Query Latency:** Multi-year analytical queries filtering on `(std_item_id, recorded_date)` over 10M+ rows must complete within $\le 2.0\text{ seconds}$.

### 5.2 Containerization & Deployment (N-02)
* The entire system must launch deterministically from a clean state using a single command:
  ```bash
  docker-compose up --build -d
  ```

### 5.3 Security, Integrity & Access Control (N-03)
* **RBAC:** Strict segregation between Buyer/MD and Administrator roles. Cost sheets and margins are masked from unauthorized viewers.
* **Credentials:** No secrets in source control. Verified via pre-commit git-secrets hooks.

---

## 6. Testability Specifications & Verification Harness

To ensure the system can be validated automatically by an individual developer, the architecture mandates dedicated testing interfaces.

### 6.1 Testing Tier Requirements

| Test Tier | Scope | Target Tech Stack | Pass Criteria |
| :--- | :--- | :--- | :--- |
| **Unit Test** | Unit conversion parser, margin formulas, Run-rate logic | `pytest`, `pytest-mock` | $100\%$ formula edge coverage; zero floating-point discrepancies |
| **Integration Test** | Repository layer, range partitions, DB indexing | `pytest`, Testcontainers (MySQL) | Partition pruning verified; query latency $\le 200\text{ms}$ on mock set |
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
| **F-03-1** | Automated Unit | Pass an array of 50 edge-case title strings (e.g., `"1.2kg(600g x 2)"`, `"30m 30rolls"`). Assert calculated `normalized_unit_price` matches expected value within $\pm 0.01\text{ KRW}$. |
| **F-03-2 / F-04-1** | Automated Unit | Simulate scenario where competitor price drops by $40\%$. Verify that suggested price does not breach `min_price_floor` and `REVERSE_MARGIN_WARNING` is raised. |
| **F-04-2** | Automated Unit | Set available inventory $= 500$, 3-day avg sales $= 50$ (Run-rate $= 10\text{ days}$). Set $D\text{-Day} = 5\text{ days}$. Verify `SPOILAGE_RISK_FLAG` asserts `TRUE`. |
| **N-01** | Automated Stress | Seed `ext_retail_prices` with 5,000,000 synthetic rows across 24 monthly partitions. Execute single-item 90-day time-series query. Verify `EXPLAIN` shows partition pruning and execution time $< 500\text{ms}$. |

---

## 7. Implementation Roadmap & Milestones

* **Phase 1: Foundation, DB Engine & Public Ingestion (Weeks 1–2)**
  * Docker Compose scaffolding (MySQL 8.0 with Volume Mounts and Range Partitions).
  * Data Definition Language (DDL) execution and Seed Master Catalog (`master_products`).
  * KAMIS and Garak Market API collectors with test suites.
* **Phase 2: Scraper Harness, Human Simulation & AI Self-Healing (Weeks 3–4)**
  * Playwright scraper framework with typing jitter, scrolling, and random delay models.
  * Markdown distillation pipeline and Gemini Flash JSON schema integration.
  * Regression test suite using static HTML failure fixtures.
* **Phase 3: ERP Integration, Decision Engine & Action Logic (Week 5)**
  * Internal CSV/Excel parsing endpoint and schema validation.
  * Dynamic pricing, margin calculations, Run-rate, and Spoilage alert engine.
  * Verification of all business logic equations via automated unit tests.
* **Phase 4: Matrix Dashboard, Personalization & System Verification (Weeks 6–7)**
  * FastAPI query service and personalized Watchlist API endpoints.
  * Responsive Three-Way Comparison Matrix Dashboard UI.
  * End-to-end sandbox verification and containerized deployment dry run.