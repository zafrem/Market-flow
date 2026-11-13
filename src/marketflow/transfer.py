"""Whole-data export and import: one zip with every analysis table (JSON lines) and the flyer images.

What travels: the catalog, prices, inventory and cost history, matches, recommendations, promotions, demand signals,
queues, run history and flyers. What stays on each machine: user accounts and passwords, the audit trail, crawler
settings and every token (those live in config/settings.toml). Rows are plain text, so a file made on DuckDB loads into
SQLite or MySQL and the other way round.

Import is meant for an empty database (nothing to merge): ids of tables that other tables point to are re-assigned and the
links rewritten, so the target's own catalog and users stay as they are."""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .crawlers.service import resolve_inbox
from .db import insert_id

FORMAT = 1
# Parents first. master_products is merged by name; product_channel_map and master_product_aliases point at it.
TABLES = ["master_products", "master_product_aliases", "mi_synonyms", "int_operations_inventory", "inventory_revisions",
          "ext_retail_prices", "ext_public_benchmarks", "product_channel_map", "promotions", "mi_observations", "review_queue",
          "alert_log", "selector_candidates", "crawler_runs", "flyers", "flyer_items", "price_recommendations"]
ITEM_ID_TABLES = {"master_product_aliases", "int_operations_inventory", "inventory_revisions", "ext_retail_prices",
                  "ext_public_benchmarks", "product_channel_map", "price_recommendations"}     # carry a std_item_id
SKIP_ON_CONFLICT = {"master_product_aliases", "mi_synonyms", "product_channel_map"}              # natural keys: keep what is there
PRIMARY_KEYS = {"int_operations_inventory": "internal_id", "inventory_revisions": "rev_id", "ext_retail_prices": "retail_id",
                "ext_public_benchmarks": "benchmark_id", "promotions": "promo_id", "mi_observations": "obs_id", "review_queue": "review_id",
                "alert_log": "alert_id", "selector_candidates": "cand_id", "crawler_runs": "run_id", "flyer_items": "item_id",
                "price_recommendations": "rec_id"}          # re-numbered on import; nothing else points at them
MAX_ARCHIVE = 1024 * 1024 * 1024          # total size once unpacked
MAX_IMAGE = 20 * 1024 * 1024
MAX_UPLOAD = 300 * 1024 * 1024
IMAGE_DIR = "flyer/review"
_IMAGE_NAME = re.compile(r"^[\w.\-가-힣 ]{1,120}\.(png|jpe?g|webp|gif)$", re.I)


class TransferError(ValueError):
    """The file is not a usable export (the message is safe to show)."""


def _jsonable(v: Any) -> Any:
    return v if v is None or isinstance(v, (bool, int, float, str)) else str(v)       # dates, Decimals: ISO / plain text


def export_all(engine: Engine, inbox: str, out: BinaryIO) -> dict[str, Any]:
    """Write the zip to `out` and return the manifest."""
    root = resolve_inbox(inbox).resolve()
    manifest: dict[str, Any] = {"format": FORMAT, "exported_at": datetime.now().isoformat(timespec="seconds"),
                                "store": engine.dialect.name, "tables": {}, "images": []}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z, engine.connect() as c:
        for t in TABLES:
            res = c.execute(text(f"SELECT * FROM {t}"))
            cols, n = list(res.keys()), 0
            with z.open(f"data/{t}.jsonl", "w") as f:
                for row in res:
                    f.write((json.dumps({k: _jsonable(v) for k, v in zip(cols, row)}, ensure_ascii=False) + "\n").encode())
                    n += 1
            manifest["tables"][t] = n
        for (rel,) in c.execute(text("SELECT DISTINCT image_path FROM flyers")):
            path = (root / rel).resolve()
            name = Path(rel).name
            if rel.replace("\\", "/") != f"{IMAGE_DIR}/{name}" or not _IMAGE_NAME.match(name) \
                    or root not in path.parents or not path.is_file() or path.is_symlink():
                continue                                          # only files the dashboard itself would serve
            z.write(path, f"inbox/{IMAGE_DIR}/{name}")
            manifest["images"].append(name)
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return manifest


def _check_archive(z: zipfile.ZipFile) -> dict[str, Any]:
    total = 0
    allowed = {f"data/{t}.jsonl" for t in TABLES} | {"manifest.json"}
    for i in z.infolist():
        total += i.file_size
        if total > MAX_ARCHIVE:
            raise TransferError("the file is too large once unpacked")
        if i.filename in allowed:
            continue
        m = re.fullmatch(rf"inbox/{IMAGE_DIR}/([^/]+)", i.filename)
        if m and _IMAGE_NAME.match(m[1]) and i.file_size <= MAX_IMAGE:
            continue
        raise TransferError(f"unexpected entry in the file: {i.filename[:80]}")
    if "manifest.json" not in z.namelist():
        raise TransferError("this is not a MarketFlow export (manifest.json is missing)")
    manifest = json.loads(z.read("manifest.json"))
    if manifest.get("format") != FORMAT:
        raise TransferError(f"unsupported export format {manifest.get('format')!r}")
    return manifest


def _insert_returning(c, table: str, row: dict, pk: str) -> int:
    cols = list(row)
    return insert_id(c, f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + k for k in cols)})", row, pk)


def _columns(c, table: str) -> list[str]:
    return list(c.execute(text(f"SELECT * FROM {table} WHERE 1=0")).keys())


def _insert(c, table: str, cols: list[str], rows: list[dict], skip: bool) -> int:
    if not rows:
        return 0
    verb = "INSERT OR IGNORE" if skip and c.dialect.name in ("sqlite", "duckdb") else ("INSERT IGNORE" if skip else "INSERT")
    if c.dialect.name == "duckdb":
        # DuckDB binds parameters row by row, which is very slow; its own JSON reader loads the same rows in a blink
        spec = ", ".join(f"'{k}': 'VARCHAR'" for k in cols)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rows.jsonl"
            with path.open("w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps({k: (None if r.get(k) is None else str(r[k])) for k in cols}, ensure_ascii=False) + "\n")
            c.execute(text(f"{verb} INTO {table} ({', '.join(cols)}) SELECT {', '.join(cols)} "
                           f"FROM read_json(:path, format='newline_delimited', columns={{{spec}}})"), {"path": str(path)})
        return len(rows)
    sql = text(f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + k for k in cols)})")
    for i in range(0, len(rows), 2000):
        c.execute(sql, [{k: r.get(k) for k in cols} for r in rows[i:i + 2000]])
    return len(rows)


def import_all(engine: Engine, inbox: str, src: BinaryIO | Path) -> dict[str, int]:
    """Load an export into this database. Everything is one transaction: a bad file changes nothing. Returns rows loaded."""
    try:
        zf = zipfile.ZipFile(src)
    except zipfile.BadZipFile as e:
        raise TransferError("this is not a zip file") from e
    root = resolve_inbox(inbox)
    loaded: dict[str, int] = {}
    images: dict[str, bytes] = {}
    with zf as z:
        _check_archive(z)
        names = set(z.namelist())
        item_map: dict[int, int] = {}
        flyer_map: dict[int, int] = {}
        review_dir = root / IMAGE_DIR
        renamed: dict[str, str] = {}
        for n in names:
            if n.startswith(f"inbox/{IMAGE_DIR}/"):
                name = n.rsplit("/", 1)[1]
                data = z.read(n)
                target = review_dir / name
                if target.exists() and target.read_bytes() != data:       # never overwrite another file: store under a new name
                    new = f"imp-{hashlib.sha1(data).hexdigest()[:8]}-{name}"
                    renamed[name] = new
                    name = new
                images[name] = data
        with engine.begin() as c:
            for t in TABLES:
                member = f"data/{t}.jsonl"
                if member not in names:
                    continue
                cols = _columns(c, t)
                rows: list[dict] = []
                for line in z.read(member).decode("utf-8").splitlines():
                    if line.strip():
                        row = json.loads(line)
                        unknown = set(row) - set(cols)
                        if unknown:
                            raise TransferError(f"{t}: unknown column(s) {', '.join(sorted(unknown))}; the file is from a different version")
                        rows.append(row)
                if t == "master_products":
                    for r in rows:
                        old = int(r.pop("std_item_id"))
                        key = {"l": r["category_l"], "m": r["category_m"], "n": r["item_name"]}
                        found = c.execute(text("SELECT std_item_id FROM master_products WHERE category_l=:l AND category_m=:m AND item_name=:n"), key).scalar()
                        if found is None:
                            if r.get("barcode") and c.execute(text("SELECT 1 FROM master_products WHERE barcode=:b"), {"b": r["barcode"]}).first():
                                r["barcode"] = None
                            found = _insert_returning(c, t, r, "std_item_id")
                        item_map[old] = int(found)
                    loaded[t] = len(rows)
                    continue
                if t in ITEM_ID_TABLES:
                    for r in rows:
                        if int(r["std_item_id"]) not in item_map:
                            raise TransferError(f"{t}: a row points at item {r['std_item_id']}, which is not in the file's catalog")
                        r["std_item_id"] = item_map[int(r["std_item_id"])]
                if t == "flyers":
                    for r in rows:
                        name = Path(r["image_path"]).name
                        r["image_path"] = f"{IMAGE_DIR}/{renamed.get(name, name)}"
                        old = int(r.pop("flyer_id"))
                        flyer_map[old] = _insert_returning(c, t, r, "flyer_id")
                    loaded[t] = len(rows)
                    continue
                if t == "flyer_items":
                    for r in rows:
                        if int(r["flyer_id"]) not in flyer_map:
                            raise TransferError("flyer_items: a row points at a flyer that is not in the file")
                        r["flyer_id"] = flyer_map[int(r["flyer_id"])]
                pk = PRIMARY_KEYS.get(t)
                if pk:                                               # fresh ids from the target's own counter; nothing else points at them
                    rows = [{k: v for k, v in r.items() if k != pk} for r in rows]
                    cols = [k for k in cols if k != pk]
                loaded[t] = _insert(c, t, cols, rows, t in SKIP_ON_CONFLICT)
    review_dir = root / IMAGE_DIR                                      # the database part succeeded: now the files
    if images:
        review_dir.mkdir(parents=True, exist_ok=True)
        for name, data in images.items():
            (review_dir / name).write_bytes(data)
    loaded["images"] = len(images)
    return loaded


def spooled() -> BinaryIO:
    """A seekable temp file on disk (removed when closed): exports and uploads can be large."""
    return tempfile.TemporaryFile()
