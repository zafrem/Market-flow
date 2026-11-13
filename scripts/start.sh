#!/usr/bin/env bash
# MarketFlow start script: prepares everything on first run, then starts the dashboard.
#   scripts/start.sh             dashboard only         (http://127.0.0.1:8000)
#   scripts/start.sh --worker    dashboard + collector worker in the same terminal (Ctrl+C stops both)
#   PORT=8765 scripts/start.sh   another port
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
WITH_WORKER=0
for arg in "$@"; do
  case "$arg" in
    --worker) WITH_WORKER=1 ;;
    -h|--help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# 1) virtualenv and dependencies
if [ ! -x "$PY" ]; then
  echo "[start] creating .venv..."
  python3 -m venv .venv
fi
if ! "$PY" -c "import marketflow, duckdb_engine, fastapi" 2>/dev/null; then
  echo "[start] installing dependencies into .venv (first run, or after an update)..."
  "$PY" -m pip install -q -e '.[dev]'
fi

# 2) settings file (git-ignored, holds your keys)
if [ ! -f config/settings.toml ]; then
  cp config/settings.example.toml config/settings.toml
  chmod 600 config/settings.toml
  echo "[start] created config/settings.toml - add your KAMIS / Garak / OpenAI keys there when you have them."
fi

# 3) login secret: use JWT_SECRET or settings.toml if set, otherwise generate one once and keep it in data/.jwt_secret
if [ -z "${JWT_SECRET:-}" ] && ! grep -Eq '^[[:space:]]*jwt_secret[[:space:]]*=[[:space:]]*"[^"]{40,}"' config/settings.toml; then
  mkdir -p data
  if [ ! -s data/.jwt_secret ]; then
    ( umask 077; $PY -c 'import secrets; print(secrets.token_urlsafe(48))' > data/.jwt_secret )
    echo "[start] generated a login secret in data/.jwt_secret (keep it: changing it logs everyone out)."
  fi
  JWT_SECRET="$(cat data/.jwt_secret)"
  export JWT_SECRET
fi

# 4) first user: ask for an admin account when there is none yet
USERS=$($PY -W ignore - <<'PYEOF' 2>/dev/null || echo 0
from sqlalchemy import text
from marketflow.db import init_sqlite, make_engine
from marketflow.settings import load_settings
e = make_engine(load_settings().database_url)
if e.dialect.name in ("sqlite", "duckdb"):
    init_sqlite(e)
with e.connect() as c:
    print(c.execute(text("SELECT COUNT(*) FROM users")).scalar())
PYEOF
)
if [ "${USERS:-0}" = "0" ]; then
  echo "[start] no users yet - create the first ADMIN account."
  read -r -p "  email: " EMAIL
  read -r -p "  name:  " NAME
  $PY -m marketflow.cli add-user "$EMAIL" "$NAME" ADMIN     # asks for the password twice
fi

# 5) run
PORT="${PORT:-8000}"
if [ "$WITH_WORKER" = "1" ]; then
  $PY -m marketflow.worker &
  WORKER=$!
  trap 'kill $WORKER 2>/dev/null || true' EXIT INT TERM
fi
echo "[start] dashboard: http://127.0.0.1:$PORT   (Ctrl+C to stop)"
$PY -m marketflow.web --port "$PORT"
