
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
