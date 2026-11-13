# MarketFlow Architecture

Requirements live in [SRS.md](SRS.md); day-to-day procedures in [PLAYBOOK.md](PLAYBOOK.md). This document describes how the system is put together and why.

## 1. Shape of the system

Three deployable pieces share one database. There is no message broker and no cache: dedupe, job state, run queues and alert history all live in the database.

```
                 ┌──────────────────────────── collector-worker (1 instance) ───────────────────────────┐
 KAMIS / Garak ─▶│ CrawlerService.loop (every 30 s)  ── dispatches per-crawler schedules + "Run now"     │
 RSS / Atom   ──▶│ Scheduler ── "decide" job (+30 min after collection) ──▶ decision engine              │
 retail sites ──▶│ Notifier ── Slack-compatible webhook (dedupe, backoff, dead letter)                   │
 data/inbox/* ──▶└──────────────┬───────────────────────────────────────────────────────────────────────┘
                                │ writes
                         ┌──────▼───────┐         ┌──────────────────────────────┐
  ERP CSV/XLSX ─ cli ───▶│  MySQL 8 /   │◀────────│ backend-api (FastAPI + static │◀── browser (MD / BUYER / ADMIN)
  MI CSV/XLSX  ─ cli ───▶│  SQLite(dev) │  reads  │ dashboard, same process)      │
                         └──────────────┘         └──────────────┬───────────────┘
                                                                 │ shares ./inbox volume with the worker
```

| Piece | Entry point | Responsibility |
|---|---|---|
| `db` | MySQL 8 (`db/init/001_schema.sql`) | System of record. SQLite is for local development only (no partitions, NUMERIC not exact). |
| `collector-worker` | `python -m marketflow.worker` | Runs crawlers on their schedules, runs the decision job, sends alerts. Run **one** instance. |
| `backend-api` | `python -m marketflow.web` | REST API and the single-page dashboard. Never fetches external sites. |
| CLI | `python -m marketflow.cli` | Operator tasks: imports, decide/approve/apply, users, audits, one-off crawler runs. |

## 2. Code layout (`src/marketflow/`)

| Package / module | Role |
|---|---|
| `settings.py`, `config/settings.toml` | Layered config: TOML file, overridden by environment variables. Secrets are redacted in `--check`. |
| `db.py`, `repository.py`, `ops_repo.py` | SQLAlchemy 2 engine/DDL (MySQL and SQLite dialects) and repositories. `SQLITE_DDL` is generated from the same model as `db/init/001_schema.sql` by `scripts/gen_schema.py`. |
| `collectors/` | `KamisCollector`, `GarakCollector`: public wholesale benchmarks into `ext_public_benchmarks`. |
| `scrape/` | Retail scraping: transport, humanized pacing, HTML sanitizing, `selfheal` extractor (LLM-repaired selectors). |
| `title_parser.py`, `units.py`, `curation.py` | Product title parsing, unit-price normalization, mapping channel items to `master_products`. |
| `erp.py` | Inventory/cost import (CSV/XLSX, English/Korean headers, errors reported by line). |
| `decision/` | `calc.py` (price math), `engine.py` (recommendation rules), `service.py` (run, approve, reject, apply, revert; audited), `config.py`. |
| `mi/` | Market Intelligence: importer, store, `analysis.py` (Category Health, Trend Radar), bilingual `messages.py`. |
| `crawlers/` | One-crawler-per-data-item framework: `base.py` (contract), `adapters.py`, `store.py`, `service.py`. |
| `scheduler.py`, `schedule.py` | KST slot arithmetic (`latest_slot`) and the `decide` job. |
| `notify.py` | Alert construction, dedupe, retry, dead letter. |
| `web/` | `app.py` (routes), `repo.py` (read models), `security.py` (hashing, JWT, throttle), `static/` (dashboard: `index.html`, `app.js`, `i18n.js`, `style.css`). |
| `matching.py`, `promotions.py`, `quality.py`, `snapshot.py`, `vision.py`, `flyers.py`, `insights.py` | Match grading (audited), promotion calendar, daily data-quality report and alerts, point-in-time snapshots (occurred vs. ingested time), flyer reading through the OpenAI API, flyer review (items wait for a person before analysis), P2 analyses (promotion results, long discounts, new products, sales-response scenarios). |
| `mi/` (more) | `privacy.py` (personal-data filter on import), `classify.py` (category classifier), `gap.py` (search–sales gap diagnosis), synonym dictionary in `store.py`. |
| `time_provider.py` | Injectable clock; all business logic takes a `TimeProvider`, so tests run on a fixed time. |

## 3. Data model

Defined in `db/init/001_schema.sql`.

| Group | Tables | Notes |
|---|---|---|
| Catalog | `master_products`, `master_product_aliases`, `product_channel_map` | `std_item_id` is the join key across all sources. |
| Market data | `ext_public_benchmarks`, `ext_retail_prices` | Time-partitioned on MySQL (`partitions.py` keeps future partitions created). |
| Internal | `int_operations_inventory`, `inventory_revisions` | ERP import: stock, cost, price. Every load also appends a revision stamped with its ingestion time. Cost data never reaches BUYER responses. |
| Decisions | `price_recommendations` | Status: PENDING → APPROVED → APPLIED, or SUPPRESSED / REJECTED / REVERTED. Person-made proposals (drafts) carry an evidence snapshot in `flags`. |
| Operations | `review_queue`, `alert_log`, `selector_candidates`, `audit_log` | Review items and alerts are de-duplicated. |
| Access | `users`, `user_category_assignments`, `user_watchlists`, `user_preferences` | |
| Market intelligence | `mi_observations` | Long format: `signal_type`, `period_start`, `category_l`, `keyword`, `metric`, `value`, `source`; unique on that key. |
| Keywords and calendar | `mi_synonyms`, `promotions` | Synonym dictionary with history; promotion calendar (informational). |
| Flyers | `flyers`, `flyer_items` | Items read from images; only APPROVED items become PROMOTION observations. |
| Crawlers | `crawler_settings`, `crawler_runs` | Settings (enabled, KST times, JSON config, `last_slot`); run history with status and `rows_loaded`. |

## 4. Main flows

### 4.1 Price recommendation
1. Collectors and retail crawlers write benchmarks and competitor prices (matched to `std_item_id` by the title parser; unmatched titles go to `review_queue`).
2. `import-erp` loads inventory and cost.
3. Only competitor listings a person confirmed as SAME count (`product_channel_map.reviewed_at`); the simulator adds expected margin and break-even volume at the matched effective price. The `decide` job (also `cli decide`) evaluates each inventory row: floor from target margin and VAT, match or undercut the lowest competitor, cap the move per run (`max_step_pct`), apply a cooldown, hold price cuts while wholesale is rising, markdown for spoilage risk. The result is a `price_recommendations` row with a reason code and flags, or `SUPPRESSED` with the reason.
4. An MD/ADMIN approves (solo mode: the proposer may approve their own; team mode: author ≠ approver, and below the margin defense line an ADMIN); `apply` updates the internal ledger. **Nothing is written back to a sales channel**: the engine is advisory. `export-prices` produces the change list.
5. Each transition is an atomic status update (compare-and-set) and writes `audit_log`.

### 4.2 Crawlers
- `crawlers/base.py` defines `Crawler` (id, label, schema of settings `Field`s, `run()` returning `RunOutcome`). `NotConfigured` marks a crawler that cannot run yet (SKIPPED, not FAILED).
- Eight crawlers, one per data item: `report`, `buyer`, `news`, `competitor`, `flyer`, `newproduct`, `benchmark_kamis`, `benchmark_garak`.
- Adapters: `InboxCrawler` (CSV/XLSX files in `data/inbox/<id>/`), `NewsCrawler` (RSS/Atom to weekly keyword mention counts), `BenchmarkCrawler`, `CompetitorCrawler`.
- `CrawlerService.dispatch_once` runs every 30 s inside the worker: reap stale runs, start queued manual runs, start due scheduled runs. A slot is due when `latest_slot(now, times)` is newer than `last_slot` and within the 6 h catch-up window; older missed slots are skipped.
- One run at a time per crawler: a pre-check plus a compare-and-set `UPDATE`. RUNNING longer than 2 h is reaped to FAILED. Failure raises a `CRAWLER_FAILED` alert.
- The API creates `QUEUED` rows for "Run now" (`POST /crawlers/{id}/run`, 202) and the worker picks them up. The only inline execution in the API is the upload path of file-inbox crawlers.

### 4.3 Market intelligence
`import-mi` / `POST /mi/import` / inbox crawlers write `mi_observations`. `mi_report` then computes, per request and per user scope:
- **Category health**: demand direction (GMV, else units sold), demand share shift in percentage points, stock cover (TIGHT < 3 days, SURPLUS > 14 days) from inventory, and a Search→demand lead test (period-over-period changes, lags 0–2, accepted only if r exceeds both `lead_min_r` and a one-sided p < 0.01 critical value).
- **Trend radar**: lifecycle stage (SEED / EMERGING / GROWING / MAINSTREAM / FADING / STABLE) from growth, volume percentile among at least 3 peers, and corroboration by PRODUCT / PROMOTION / MEDIA signals; weighted score and opportunity score.
- Narratives (what changed / why it matters / what to do) are rendered server-side in `en` or `ko` (`?lang=`); tests assert both catalogues have identical keys and placeholders.

## 5. Security model

| Concern | Mechanism |
|---|---|
| Authentication | Password hash + short-lived JWT (`token_ttl_minutes`); the API refuses to start without a ≥ 32-char `jwt_secret`. Roles are re-read from the DB on every request. |
| Brute force | `LoginThrottle`: 5 failures / 15 min per email, in process memory (per process, not shared between replicas). |
| Authorization | Roles ADMIN / MD / BUYER. MD and BUYER see only assigned `category_l`; out-of-scope resources return 404. BUYER responses omit cost and margin keys server-side. Crawlers: ADMIN edits, MD views, BUYER has no access. MI rows without a category are admin-only. |
| Transport | TLS directly (`tls_cert`/`tls_key`) or at a reverse proxy with `hsts = true`; `trust_proxy` makes throttling use the real client IP. |
| Browser | CSP `default-src 'self'`, no inline styles or scripts; charts are inline SVG. Token held in memory only. |
| Outbound fetches | News feed URLs must be http(s) and not localhost, `.local`, `.internal`, or private/loopback/link-local IPs. XML parsed with `defusedxml`. Retail scraping requires a recorded `legal_review_ref`. |
| File input | Inbox ignores symlinks, 20 MB cap; API upload 5 MB (MI import 5 MB); client-supplied filenames are never used on disk. |
| LLM | Page text is sanitized before it is sent; returned selectors are validated against the page and held as `PENDING` until a person approves them. |
| Alerts | Scraped text is escaped (no `@channel`). Alert text carries no price, margin or cost figures, only SKU, situation and a dashboard link. |
| Vision (OpenAI) | Images leave the machine only while the flyer crawler is enabled and a key is set; type checked from bytes, 5 MB cap, output validated, personal data dropped, key and response bodies never logged. |
| Approval | `[approval] solo_mode` (default true) lets a single operator approve their own proposals; `false` enforces author ≠ approver and admin approval below the defense line. Both are audited. |
| Audit | Approve/reject/apply/revert, user and crawler changes, uploads and imports write `audit_log`. |

## 6. Time and scheduling

- All schedules are KST (`TZ=Asia/Seoul` in containers); the database stores UTC (`--default-time-zone=+00:00`).
- Defaults: benchmarks 06:00 / 09:00, retail 08:00 / 13:00 / 19:00, decisions 30 minutes after collection. Crawler times are editable on each crawler page and take effect without restarting the worker.
- Business code never calls `datetime.now()` directly; it uses `TimeProvider` (`FixedTimeProvider` in tests).

## 7. Internationalization

The dashboard is bilingual (English / Korean): static `data-i18n` attributes plus `t(key, params)`; status, flag and reason codes go through `codeText()` with a readable fallback for unknown codes. The choice is kept in `localStorage` (`mf-lang`) and defaults to the browser language. Server-generated narratives follow `?lang=`. Known gap: crawler run messages are stored in English.

## 8. Testing

| Layer | Command | Notes |
|---|---|---|
| Offline suite | `.venv/bin/pytest` | SQLite and fixtures, no network or tokens. Includes the end-to-end autonomy test (`tests/test_autonomy.py`) that simulates days of unattended operation with a fake clock. |
| Dashboard | `cd tests/js && npm test` (also run from pytest) | The real `app.js` + `index.html` in jsdom against a fake API. |
| MySQL | `MARKETFLOW_TEST_DB_URL=... pytest -m mysql` | DDL, partitions, upserts, JSON/ENUM columns, history queries. |
| Live | `MARKETFLOW_LIVE=1 pytest -m live` | Real KAMIS/Garak calls with your tokens. |

## 9. Known limits and extension points

- The revised price/margin functional specification (SRS F-08): P0 and P1 are implemented; what is partial or missing is listed in [SRS.md](SRS.md) §4.1.
- The worker is single-instance by design; the login throttle is per process.
- Hive and Power BI have no direct connector (file inbox); the Kurly new-product collection happens outside MarketFlow and feed the inbox.
- Not verified in CI: the Docker images, MySQL SQL for newer tables, live KAMIS/Garak/Gemini/OpenAI/Slack, real RSS feeds, and the dashboard in a real browser.
- To add a data source: implement a `Crawler` in `crawlers/adapters.py`, register it in `CrawlerService.registry` (with its `ORDER` position), add translations for its labels in `web/static/i18n.js`, and add a test in `tests/test_crawlers.py`.
- To add a pricing rule: extend `decision/engine.py`, give it a reason code, and add the code to `i18n.js`.
- To add a channel write-back: implement it behind `apply` in `decision/service.py` with its own audit action; it does not exist today.
