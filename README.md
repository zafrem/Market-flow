
## Run locally (no Docker, SQLite)

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp config/settings.example.toml config/settings.toml && chmod 600 config/settings.toml
$EDITOR config/settings.toml                # put your KAMIS key/id and Garak URL/key here (git-ignored)

.venv/bin/python -m marketflow.worker --check   # shows what is configured (tokens redacted)
.venv/bin/python -m marketflow.worker --once    # crawl now -> data/marketflow.db (created + seeded on first run)
.venv/bin/python -m marketflow.worker           # or stay running on the KST schedule (06:00, 09:00)
```

Any setting can be overridden by an env var (`KAMIS_API_KEY`, `KAMIS_API_ID`, `GARAK_API_URL`, `GARAK_API_KEY`,
`DATABASE_URL`, `MOCK_LLM`). Point `database.url` at a `mysql+pymysql://...` URL to use MySQL instead.
SQLite is for local development: no partitioning, and NUMERIC columns are not exact decimals.

Crawl rules (field names, regexes, parser entrypoints) live in `config/extraction.json`, hot-reload on change,
and approved fixes go to `config/overrides.json`.

## Tests

```bash
.venv/bin/pytest                                   # offline: fixtures + SQLite, no tokens needed
MARKETFLOW_LIVE=1 .venv/bin/pytest -m live -s      # real KAMIS/Garak calls using your configured tokens
MARKETFLOW_TEST_DB_URL=mysql+pymysql://... .venv/bin/pytest -m mysql   # MySQL DDL/partition tests
```

## Retail scraping & self-healing (Phase 2)

Platform selectors live in `config/extraction.json` under `retail.<PLATFORM>`. A `SCRAPE` connector will not run until
`legal_review_ref` is filled in (record the ToS/legal review) and `urls` is set.

```bash
.venv/bin/python -m marketflow.worker --retail     # scrape configured platforms once
```

When selectors stop matching, the page is sanitized and sent to the LLM (`llm.mock=false` + `gemini_api_key` in
`settings.toml`; default is an offline mock). LLM records are validated and checked against the page text; verified
replacement selectors are held as a *pending candidate* until a person calls `SelfHealingExtractor.approve(platform, actor)`,
which writes them to `config/overrides.json`. Optional real-browser transport: `pip install -e '.[browser]' && playwright install chromium`.

## ERP import, decisions & approvals (Phase 3)

```bash
.venv/bin/python -m marketflow.cli import-erp inventory.xlsx      # .csv or .xlsx; bad rows are reported by line number
.venv/bin/python -m marketflow.cli decide                         # evaluate today's inventory -> PENDING / SUPPRESSED recommendations
.venv/bin/python -m marketflow.cli recs --status PENDING
.venv/bin/python -m marketflow.cli approve 3 --as kim:MD          # MD/ADMIN only; then apply / revert (all audited)
.venv/bin/python -m marketflow.cli apply 3 --as kim:MD
.venv/bin/python -m marketflow.cli audit
```

Header aliases (English/Korean) are in `config/extraction.json` under `erp.columns`; pricing parameters go in the
`[decision]` table of `config/settings.toml` (see `settings.example.toml`). The engine is advisory: `apply` updates the
internal ledger only; nothing is written back to a sales channel.

## API & dashboard (Phase 4)

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"      # put the output in settings.toml -> [auth] jwt_secret
.venv/bin/python -m marketflow.cli add-user kim@example.com "Kim" MD       # prompts for a password (>= 10 chars)
.venv/bin/python -m marketflow.cli assign kim@example.com Fresh/Fruit      # MD/BUYER see only assigned categories
.venv/bin/python -m marketflow.web                                          # http://127.0.0.1:8000  (refuses to start without a strong jwt_secret)
```

- `POST /auth/login` -> bearer token (rate-limited: 5 failures / 15 min per email). Roles are re-read on every request, so
  deactivating a user or changing a role takes effect immediately.
- `GET /matrix[?watchlist=true]` three-way comparison; `GET/PUT /watchlist`, `/preferences`; `GET /recommendations`;
  `POST /recommendations/{id}/approve|reject|apply|revert`; `/admin/users`, `/admin/audit` (ADMIN only).
- **BUYER responses omit cost and margin keys entirely** (enforced server-side, covered by tests); out-of-scope resources return 404.
- The dashboard keeps its token in memory only (a reload signs you out). Serve it behind HTTPS if exposed beyond localhost.

## Running unattended (scheduler, alerts, review queue)

```bash
.venv/bin/python -m marketflow.worker      # benchmarks 06:00/09:00 · retail 08:00/13:00/19:00 · decisions +30 min (KST)
```

- **Alerts** go to `[alerts] webhook_url` (Slack-compatible): reverse-margin, spoilage, low margin, competitor stock-out,
  failed jobs/collectors, escalated selector faults, new selector candidates. Each key is sent at most once per 6 h, retried
  with backoff (1, 2, 4, 8 s), then dead-lettered in `alert_log`; at most 20 per run (the rest become one summary).
  Scraped text is escaped so it cannot trigger `@channel`-style mentions. The alert text includes margin figures, so use a
  channel only MD/ADMIN can read. Test it with `python -m marketflow.cli alert-test`.
- **Review queue**: titles that could not be matched/parsed, quarantined LLM records and unmapped benchmark items are stored
  (de-duplicated) instead of only logged. `cli review` / `resolve ID`, or `GET /review`, `POST /review/{id}/resolve`.
- **Selector candidates**: when the LLM heals a page, the verified replacement selectors are saved as `PENDING`.
  `cli selectors`, `approve-selector ID --as kim:MD` (or `POST /selectors/{id}/approve`) writes them to `config/overrides.json`.
