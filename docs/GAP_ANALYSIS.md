# Gap Analysis: Revised Functional Specification vs. Current Implementation

Source: `docs/경쟁사 가격·마진 분석 시스템 기능 명세서 (수정본).md` (Nov 12, 2026; "the spec"). Requirement IDs are defined in `SRS.md` §F-08. Status was checked against the code in this repository on 2026-11-12 and updated after the P0 definition work (marked **Done (P0 work)** or noted in the row); items marked "not verified" were read from documentation only.

Legend: **Done** = implemented and tested; **Partial** = some of the requirement exists; **Missing** = not implemented; **Conflict** = current behavior contradicts the spec.

## 1. Summary

MarketFlow already covers most of the P0 *mechanics* (ERP import, unit conversion, net margin, floor guard, comparison matrix, RBAC, audit). The P0 gaps are in the *definitions*: the effective purchase price (실구매가), the disposal-cost term of net margin, BEP, human-confirmed "same product" matching, and how alerts expose margin data. Several P1 items (auto-matching with review queue, approval flow, alerts, backfill) exist in an earlier form because the project was built outside the spec's phase order.

## 2. Module 1: Data collection and integration

| ID | Spec requirement | Phase | Status | Current state / gap |
|---|---|---|---|---|
| F-08-1.1 | Multi-source connectors, read-only accounts, retry + alert, last success and row count visible | P0/P1 | Partial | ERP/own-mall data comes in as CSV/XLSX (`import-erp`), not a read-only DB connector. Crawlers show last success, rows and failure reasons on their pages and raise `CRAWLER_FAILED`; ERP imports have no equivalent status entry. Source-vs-loaded count check (0.1%) not implemented. |
| F-08-1.2 | Unified schema mapper, versioned rules, unmapped codes listed and excluded from aggregates | P0 | Partial | Header aliases in `config/extraction.json`; unmatched titles and unmapped benchmark items go to `review_queue`. No mapping versioning; no per-run "excluded rows" count on screen. |
| F-08-1.3 | Batch backfill uploader with validation, idempotent re-upload | P1 | Partial | MI import is an idempotent upsert with line-numbered errors (CSV/XLSX, 5 MB). Sales/discount history backfill and a downloadable failure file do not exist. |
| F-08-1.4 | Point-in-time snapshot with dual timestamps (occurred vs. ingested) | P1 | Missing | Tables carry only the occurrence date (`recorded_date`, `scraped_at`). There is no ingestion time, so "what we knew on date X" cannot be reproduced and retroactive cost corrections overwrite history. |
| F-08-1.5 | Competitor price crawler: price, discount, **coupon, shipping fee**, stock; change detection | P0 | Partial | Collects sale/original price, badge, stock; self-healing detects structure changes and alerts. Coupon and shipping fields are not stored. Cadence is 3×/day, not hourly for core SKUs. Success-rate (95%) and sample-accuracy (99%) are not measured. |
| F-08-1.6 | Competitor flyer parser (image/PDF, field confidence, side-by-side review) | P2 | Missing | **Now:** flyer images dropped in the inbox are read with the OpenAI API into weekly PROMOTION mentions (names, prices not stored); no PDF, no per-field confidence or side-by-side review. Previously flyer OCR happened outside; its output enters through the `flyer` file inbox as promotion counts. No parser, no review screen. |

## 3. Module 2: Normalization and matching

| ID | Spec requirement | Phase | Status | Current state / gap |
|---|---|---|---|---|
| F-08-2.1 | Manual SKU matching 1:N with grade (same / similar / substitute), history, re-check on discontinued URL | P0 | Partial | **Now:** grade column, Product matches screen, `cli match`/`unmatch`, API, audit history. **Still missing:** a re-check flag for discontinued listings. |
| F-08-2.2 | Unit conversion; fresh-food attributes (grade/origin/cut); differing attributes block "same" | P0 | Partial | Title parser converts to standard unit and returns `UNPARSEABLE` to review. Attributes are not compared when deciding sameness. |
| F-08-2.3 | **Margin and alerts use only human-confirmed "same" matches** | P0 | **Done (P0 work)** | Scraped listings are stored as unconfirmed candidates in `product_channel_map`; recommendations, matrix and alerts use only listings an MD/ADMIN graded SAME (`require_confirmed_match`, default on). |
| F-08-2.4 | Auto-match engine with review queue; every candidate needs approval; precision ≥95%, recall ≥80% | P1 | Partial | Barcode → rule → LLM suggestions with a 0.8 threshold and a review queue. High-confidence matches are accepted without approval; precision/recall are not measured. |
| F-08-2.5 | Category auto-classifier | P1 | Missing | MI uses the `category_l` given in the imported file. |
| F-08-2.6 | Synonym / alias dictionary with change history and impact count | P1 | Missing | `master_product_aliases` maps product aliases only; no keyword dictionary. |

## 4. Module 3: Analysis and simulation

| ID | Spec requirement | Phase | Status | Current state / gap |
|---|---|---|---|---|
| F-08-3.1 | Net margin = (P − purchase − logistics − fee − **disposal**) ÷ P; matches finance ±0.5 pp | P0 | Partial | **Now:** disposal cost term (ERP `disposal_cost` per SKU, else category `disposal_rate_pct`), also in the price floor. **Still open:** VAT basis differs from the spec formula (§7, item 6); reconciliation against finance not done. |
| F-08-3.2 | Price-match simulator: expected margin and **BEP quantity** for matching competitor *effective* price | P0 | **Done (P0 work)** | Matrix column "If we match" and `match_simulation` on every recommendation: expected margin and weekly break-even volume, UNACHIEVABLE when the cut leaves no margin. Uses weekly volume = recent daily average x 7. |
| F-08-3.3 | Effective purchase price (list − instant discount − public coupon + shipping; 1+1 as per-unit 50%) as the comparison basis | P0 | Partial | Stored and used for comparison (`coupon_discount`, `shipping_fee`, `bonus_qty`, `effective_unit_price`). Needs coupon/shipping/bonus selectors configured per platform; card/membership prices not handled (open question). |
| F-08-3.4 | Margin defense line: per-category minimum, SKU exceptions, change history, filterable warnings | P0 | Partial | `target_min_margin_pct` per category in config produces `min_price_floor`; ADMIN floor override is audited; `REVERSE_MARGIN` and low-margin flags exist. Defense lines are edited in the settings file, not in the UI; per-SKU exception and change history are limited to floor overrides. |
| F-08-3.5 | Sales response scenarios (elasticity; pessimistic / base / optimistic) | P2 | Missing | |
| F-08-3.6 | Keyword frequency and share, WoW / YoY | P1 | Partial | MI Trend Radar computes keyword growth, share shift and lifecycle from imported signals. WoW/YoY labels and "dictionary applied" indicator are not shown. |
| F-08-3.7 | Search–sales gap diagnosis with ordered causes (no product → out of stock → price disadvantage → other) | P1 | Missing | MI tests whether search leads demand; it does not diagnose per keyword why conversion is low. |
| F-08-3.8 | New-product detector | P2 | Missing | `newproduct` crawler only imports counts from files. |
| F-08-3.9 | Discount-period anomaly detection (failed promotion, prolonged discount) | P2 | Missing | |

## 5. Module 4: Dashboard and administration

| ID | Spec requirement | Phase | Status | Current state / gap |
|---|---|---|---|---|
| F-08-4.1 | Price/margin comparison sheet: own price, effective competitor price and unit price, current margin, expected margin, BEP, match grade, data time; sort/filter/export; 3 s load | P0 | Partial | **Now:** effective competitor price, expected margin and BEP, unconfirmed-listing count, audited CSV export (MD/ADMIN). **Still missing:** in-table sort/filter, per-listing match grade column, load time not measured. |
| F-08-4.2 | Data source management screen with URL validation and change history | P0 | Partial | Crawlers pages: status, last success, failures, schedule, settings, audited changes; news feed URLs are validated. Retail target URLs live in `config/extraction.json`, not in the screen. |
| F-08-4.3 | Keyword and status overview (gap quadrant chart) | P1 | Partial | Category health and Trend radar tabs; no gap quadrant. |
| F-08-4.4 | Promotion calendar (own vs. competitor, year-over-year overlay) | P1 | Partial | **Now:** `promotions` table, `/promotions` API, Promotions tab with a 90-day-back/30-day-ahead lane; MD/ADMIN record, audited. **Missing:** competitor promotions are entered by hand (no collection), no year-over-year overlay. |
| F-08-4.5 | Alerts: per-type thresholds, 24 h dedupe per SKU/type, daily digest for normal severity, **body without cost/margin figures** | P1 | Partial | **Now:** alert text has no price/margin/cost figures and links to the dashboard; dedupe window is 24 h. **Still missing:** daily digest for normal severity. **P1:** opt-in daily digest (`[alerts] digest = true`): everything below RED goes out as one summary per run. |
| F-08-4.6 | Price change approval flow: draft → approve → export; **below-defense-line drafts need higher approval**; evidence snapshot kept | P1 | Partial | Recommendation → approve/reject → apply → revert, all audited; `export-prices` lists applied changes. The engine clamps to the floor instead of routing below-floor changes to a higher approver; the evidence snapshot is limited to the recommendation's `flags`. No automatic write-back, which matches the spec ("out of scope"). |

## 6. Non-functional requirements

| ID | Spec requirement | Status | Current state / gap |
|---|---|---|---|
| F-08-N1 | Separate permissions for viewing cost/margin, export, defense lines, approval; company SSO | Partial | Three roles (ADMIN, MD, BUYER) with category scope; BUYER cost masking enforced server-side. Permissions are not separable beyond the roles; no SSO (local password + JWT). |
| F-08-N2 | Audit log for views, downloads, setting/matching changes, approvals | Partial | Writes (approve/apply/revert, user, crawler, upload, floor, selector) are audited. Reads and downloads are not. |
| F-08-N3 | De-identification of search/order logs; PII filter on queries | Missing | MI imports are aggregated keyword-level files; no PII filter on import. |
| F-08-N4 | Secrets in a secret store; read-only accounts for sources | Partial | Secrets via `settings.toml` (git-ignored) or environment variables. No secret manager; no direct DB sources yet. |
| F-08-N5 | Daily data-quality metrics (collection success, load delay, unmapped codes, unit-parse failures, matches to re-check) with alerts | Partial | Crawler success/failure and review queue counts exist; no daily aggregate or threshold alert for unmapped/unparsed counts. **P1:** `/quality` report, DATA_QUALITY alerts, and a status strip above the matrix. |
| F-08-N6 | Freshness: values older than 2× collection cycle shown as "stale" and excluded from alerts | Partial | Decisions exclude competitor data older than 24 h (configurable) and the matrix shows data age. The rule is a fixed window, not 2× each source's cycle. |
| F-08-N7 | Dashboard availability 99% business hours; collection failure does not block reads | Done | Worker and API are separate processes sharing only the database. |
| F-08-N8 | Crawling compliance: legal-reviewed targets only, no login areas or personal data, rate cap | Done | `SCRAPE` connectors require `legal_review_ref`; per-connector pacing. Ongoing legal review itself is an open item (spec §9). |

## 7. Conflicts that need a decision

1. **Alert content (resolved in the P0 work).** Spec: alerts carry no cost or margin figures, only a link. Current alerts print margin % and price. Change the alert text (and the README note about restricting the channel), or keep it and record an exception. Recommended: follow the spec.
2. **Which matches count (resolved in the P0 work).** Spec: only human-confirmed "same" matches feed margin and alerts. Current: matches with confidence ≥ 0.8, automatic ones included. Recommended: require a reviewed "same" match for recommendations and alerts, and keep automatic matches as suggestions.
3. **Comparison basis.** Spec compares effective purchase price; current compares shown sale price. Requires new scraped fields (coupon, shipping, gift) before the definition can be applied.
4. **Auto-apply.** SRS F-04-0 allows an opt-in auto-apply; the spec puts system write-back out of scope. The code is advisory-only; SRS 1.3 marks auto-apply as out of scope.
5. **Collection cadence.** Spec: core SKUs hourly. Current retail schedule: 08:00, 13:00, 19:00 KST. Hourly collection raises load and the legal review burden for each platform.
6. **Margin basis (VAT).** Spec formula divides by selling price P and leaves VAT treatment as an open question. Current engine divides by the VAT-exclusive price. Margin rates will differ by about 10% relative until finance fixes one basis; this must be settled before the ±0.5 pp reconciliation.

## 8. Suggested order of work

1. **Decide the open items** in spec §9 that block P0: scale and first category, VAT basis, disposal-cost source, minimum margins, whether card/membership prices count.
2. **P0 definition fixes** (small, high value): disposal-cost term in the margin formula; BEP and expected margin at the matched price; alert text without margin figures; matching grade plus "confirmed same only" rule.
3. **P0 data fixes:** store coupon, shipping fee and gift terms; compute effective price; comparison sheet columns (expected margin, BEP, grade) and audited export.
4. **P0 admin:** UI for defense lines and manual matches with history; ERP import status in the data source screen.
5. **Point-in-time (dual timestamp)** before any backfill so history is not rewritten.
6. **P1/P2** in spec order, reusing the existing crawler, MI and review-queue frameworks.
