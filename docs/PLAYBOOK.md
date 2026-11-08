# MarketFlow Operations Playbook

Procedures for running and troubleshooting MarketFlow. How the pieces fit together is in [ARCHITECTURE.md](ARCHITECTURE.md). Commands assume the repo root; locally use `.venv/bin/python`, in Docker use `docker compose exec backend-api python`.

## 1. First-time setup

### Local (SQLite)
1. `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`
2. `cp config/settings.example.toml config/settings.toml && chmod 600 config/settings.toml`
3. Generate a secret: `python -c "import secrets; print(secrets.token_urlsafe(48))"` → `[auth] jwt_secret`.
4. `python -m marketflow.worker --check` to see what is configured (tokens redacted).
5. Create the first admin: `python -m marketflow.cli add-user admin@example.com "Admin" ADMIN` (prompts for a password, ≥ 10 chars).
6. Start the API (`python -m marketflow.web`) and the worker (`python -m marketflow.worker`).

### Docker
1. `cp .env.example .env`; set `MYSQL_ROOT_PASSWORD`, `MYSQL_PASSWORD`, `JWT_SECRET` (compose refuses to start without it).
2. `docker compose up --build -d`
3. `docker compose exec backend-api python -m marketflow.cli add-user admin@example.com "Admin" ADMIN`
4. Open http://127.0.0.1:8000. For remote access put a TLS reverse proxy in front and set `hsts = true` and `trust_proxy`.
5. Run exactly one `collector-worker`.

## 2. Onboarding people

| Task | Command |
|---|---|
| Add an MD or buyer | `cli add-user kim@example.com "Kim" MD` (roles: `ADMIN`, `MD`, `BUYER`) |
| Give category scope | `cli assign kim@example.com Fresh/Fruit` (MD/BUYER see only assigned categories) |
| List / disable a user, change a role | ADMIN API `GET /admin/users` and its update routes (there is no users page in the dashboard). Takes effect on the next request. |

Rules to explain to users: buyers never see cost or margin; MDs can approve and apply prices and view crawlers; only admins change crawlers and import MI data.

## 3. Daily operations

### 3.1 Morning check (about 5 minutes, admin)
1. **Crawlers tab**: every enabled crawler should show a recent OK run and a fresh data date. FAILED or stale rows: see §6.
2. **Review queue** (`cli review` or dashboard): resolve unmatched titles and unmapped benchmark items (§4.2).
3. **Selector candidates** (`cli selectors`): approve or ignore healed selectors (§4.3).
4. **Recommendations**: MDs work through PENDING items (§3.2).
5. Alert channel: look for `CRAWLER_FAILED`, `LOW_MARGIN`, stock-out and fault alerts.

### 3.2 Price recommendations (MD)
1. Import today's inventory if it was not automated: `cli import-erp inventory.xlsx` (CSV or XLSX; bad rows are listed with line numbers; fix and re-import the file).
2. Recommendations appear after the `decide` job (30 minutes after collection) or on demand with `cli decide`. Re-running is safe; duplicates are not created.
3. Review each PENDING row on the dashboard: current vs suggested price, reason code, flags (reverse margin, spoilage, stale data).
4. `approve` then `apply`, or `reject`. CLI: `cli approve 3 --as kim:MD`, `cli apply 3 --as kim:MD`.
5. Applying updates the internal ledger only. Export the change list for the channel team: `cli export-prices --since 2026-11-01 --file changes.csv`. Push the prices to the sales channel yourself.
6. Mistake? `cli revert ID --as kim:MD` (audited).

### 3.3 Market intelligence (weekly or monthly)
1. Get the data: search report (Hive export) → `report` crawler; Power BI monthly export → `buyer`; flyer OCR output → `flyer`; new-product list → `newproduct`.
2. Drop the CSV/XLSX in `data/inbox/<crawler>/` or upload on that crawler's page. One row per period (and optionally category and keyword); other numeric columns become metrics. Accepted periods: `2026-10-26`, `20261026`, `202610`, `2026-W43`. Re-importing a period overwrites it.
3. Direct import is also available: `cli import-mi file.csv --type SEARCH|BUYER|SKU|PRODUCT|PROMOTION|MEDIA`.
4. Read the result: dashboard tabs *Category health* and *Trend radar*, or `cli mi-report --lang ko`.
5. Interpreting it: a Search signal marked "interest, not demand" has *not* passed the lead test for that category. Do not plan inventory from it. Trend stages SEED/EMERGING are early signals to watch; GROWING with corroboration is the actionable one.

## 4. Routine tasks

### 4.1 Change a crawler schedule or settings
Crawlers > pick the crawler > edit enabled / times (KST `HH:MM`) / settings > Save. No restart needed. Changes are audited (`CRAWLER_UPDATED`).

### 4.2 Review queue
`cli review` lists items; `cli resolve ID` closes one. Typical causes: a new product title the parser cannot match (add an alias to `master_product_aliases` or fix the parser rules in `config/extraction.json`, then resolve), an unmapped KAMIS/Garak item (add it to the channel map).

### 4.3 Selector candidates (self-healing)
When a retail page changes and the LLM proposes new selectors, they stay `PENDING`. Inspect with `cli selectors`; if the sample extraction looks right, `cli approve-selector ID --as kim:MD`. This writes `config/overrides.json` (hot-reloaded).

### 4.4 Add a news feed
Crawlers > `news` > settings: add the RSS/Atom URLs (http/https, public hosts only) and the lookback window. Tracked keywords come from the MI data you have imported. Run now to verify.

### 4.5 Tune pricing or MI parameters
Edit `[decision]` (per category: `[decision.Fresh]`) or `[mi]` / `[mi.weights]` in `config/settings.toml`, then restart the API and worker. Start with `undercut_pct`, `max_step_pct`, `cooldown_hours`; change one at a time and compare the next day's recommendations.

### 4.6 Configure alerts
Set `[alerts] webhook_url` to a Slack-compatible webhook for a restricted channel (alerts include margins). Test: `cli alert-test`. Each alert key is sent at most once per 6 h, retried with backoff, then dead-lettered in `alert_log`; at most 20 per run.

## 5. Deploy and upgrade

1. Take a backup first (§7).
2. `git pull`; run `.venv/bin/pytest` (and `cd tests/js && npm test` if the dashboard changed).
3. Docker: `docker compose up --build -d`. New tables are created from `db/init/*.sql` only on an **empty** data directory; for an existing MySQL, apply the new `CREATE TABLE IF NOT EXISTS` statements from `db/init/001_schema.sql` manually. SQLite creates missing tables on start.
4. Check `docker compose logs collector-worker backend-api` for errors, then the Crawlers tab for the next slot.
5. Rollback: previous image/commit, restore the backup if the schema changed.

If you change the schema, edit `scripts/gen_schema.py` and regenerate `db/init/001_schema.sql` and `SQLITE_DDL`; do not hand-edit them separately.

## 6. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| API will not start | `jwt_secret` missing or < 32 chars | Set `[auth] jwt_secret` / `JWT_SECRET`. |
| Cannot log in, "too many attempts" | 5 failures in 15 min for that email | Wait 15 min or restart the API (throttle is in memory). Check the password with an admin. |
| Crawler never runs | Disabled; no times set; worker not running; `last_slot` already past | Check enabled and times on its page; `docker compose ps`; `cli crawler-run ID` to test in-process. |
| "Run now" stays QUEUED | Worker down or not on the same DB | Worker log; it picks up queued runs within about 30 s. |
| Run FAILED, message "stale" | Worker restarted mid-run (RUNNING > 2 h is reaped) | Run again; check the worker log for the crash. |
| Run SKIPPED / "not configured" | Missing KAMIS key/id, Garak URL, `legal_review_ref`, or news feeds | Fill the setting; see `worker --check`. |
| File-inbox crawler loads nothing | File not CSV/XLSX; symlink; > 20 MB; missing period column | Look in `data/inbox/<id>/failed/` and the run message (bad cells are listed with line numbers). In Docker, confirm `./inbox` is mounted in both services. |
| Dashboard upload rejected | File > 5 MB, or the crawler is not a file-inbox one, or user is not admin | Split the file or use the inbox folder directly. |
| News crawler finds 0 mentions | No tracked keywords yet; feed unreachable; feed rejected as private host | Import SEARCH data first; test the URL from the worker host; use a public URL. |
| Competitor prices missing/odd | Selectors broke; extraction faults escalated | Check fault alerts and `cli selectors`; approve a healed candidate or fix `config/extraction.json`. |
| No recommendations | No inventory for today; items unmatched; all suppressed | `cli import-erp`; check `cli recs --status SUPPRESSED` for reason codes. |
| Category health shows "interest only" | Lead test not passed or fewer than 4 periods | Import more history; this is intentional, not a bug. |
| Buyer sees no categories | No assignment | `cli assign`. |
| Dashboard text in the wrong language | Stored choice in the browser | Use the language button in the header. Crawler run messages are English only. |
| MySQL "reserved word" error after a schema edit | Column named with a reserved word | Rename the column (this happened with `stored` → `rows_loaded`). |

## 7. Backup, restore, retention

- **Back up**: the MySQL database (`mysqldump --single-transaction marketflow`), `config/settings.toml`, `config/overrides.json`, and `inbox/` (processed files are the only copy of raw MI uploads).
- **Restore**: stop worker and API, restore the dump, start the API, then the worker. Check the Crawlers tab; if the dump is old, missed slots older than 6 h are skipped by design, so run the crawlers manually.
- Time-partitioned tables get future partitions on worker start (`ensure_partitions`); dropping old partitions is a manual decision.
- `audit_log` is append-only; do not prune it without a retention decision.

## 8. Security routine

- Rotate `jwt_secret` after any suspected leak (all users must log in again); rotate KAMIS/Garak/Gemini keys and the webhook URL in `settings.toml`, never in the repo (`config/settings.toml` is git-ignored).
- Quarterly: review active users and category assignments; review `cli audit` (or `GET /admin/audit`) for unexpected approvals or crawler changes.
- Keep the alert channel restricted to MD/ADMIN.
- Do not enable a retail `SCRAPE` connector before the ToS/legal review is recorded in `legal_review_ref`.
- Do not put feeds or credentials into crawler settings that you would not want in the audit trail.

## 9. Incident quick reference

| Situation | First 10 minutes |
|---|---|
| A wrong price was applied | `cli revert ID --as you:MD`; export the corrected change list; tell the channel team. |
| Bad MI data imported | Re-import the corrected file for the same periods (overwrites). For a wrong `signal_type`, delete those `mi_observations` rows for that source and re-import. |
| Credentials leaked | Rotate the secret (§8), then review `audit_log` from the exposure time. |
| Worker crash loop | `docker compose logs collector-worker`; disable the offending crawler on its page (or `UPDATE crawler_settings SET enabled=0`) to stop the loop, then fix. |
| Alert flood | Alerts are capped at 20 per run with a summary; fix the root cause (usually one failing collector) rather than muting the channel. |
| Database full / slow | Check the partitioned tables first (`ext_retail_prices`); archive old partitions after a backup. |
