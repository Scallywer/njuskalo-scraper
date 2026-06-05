"""
Database layer for the Njuskalo electronics scraper.

Single source of truth for the schema, the connection helper, and the
upsert logic (including price-history tracking and listing lifecycle).

Storage is a single SQLite file at ``data/njuskalo.db`` (relative to the
repo root). The schema is intentionally Postgres-portable so the same
shape can move to a server database when this becomes a multi-user API.

Design notes
------------
* ``listings`` keeps a stable core (id, title, price, condition, location,
  category, seller, ...) as real columns for fast filtering, plus a flexible
  JSON ``attributes`` bag for the per-category specs that vary wildly across
  electronics types (a phone has Memorija/Boja; a laptop has CPU/RAM).
* ``price_history`` records one row per *observed price change*, enabling
  "going rate" and "price dropped" style queries.
* ``is_active`` + ``last_seen_at`` track listing lifecycle: a re-crawl marks
  listings that have vanished from a category as inactive (sold/delisted).
"""

import os
import json
import sqlite3
from datetime import datetime, timezone

# --- Paths ---
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "data", "njuskalo.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    slug            TEXT PRIMARY KEY,
    name            TEXT,
    family          TEXT,          -- top-level group, e.g. 'mobiteli'
    parent_slug     TEXT,
    url             TEXT,
    is_leaf         INTEGER DEFAULT 0,
    last_crawled_at TEXT
);

CREATE TABLE IF NOT EXISTS listings (
    ad_id          TEXT PRIMARY KEY,   -- njuskalo ad id (= JSON-LD sku)
    title          TEXT,
    price_amount   REAL,
    price_currency TEXT,
    condition      TEXT,               -- 'novo' / 'rabljeno' / NULL
    location       TEXT,               -- raw "Grad Zagreb, Crnomerec, ..."
    county         TEXT,               -- parsed first segment
    city           TEXT,               -- parsed second segment
    category_slug  TEXT,
    family         TEXT,
    url            TEXT,
    seller_name    TEXT,
    seller_type    TEXT,               -- 'private' / 'business' / NULL
    description    TEXT,
    image_url      TEXT,
    attributes     TEXT,               -- JSON: per-category specs
    phones         TEXT,               -- JSON: list of phone numbers
    first_seen_at  TEXT,
    last_seen_at   TEXT,
    scraped_at     TEXT,
    is_active      INTEGER DEFAULT 1,
    FOREIGN KEY (category_slug) REFERENCES categories(slug)
);

CREATE TABLE IF NOT EXISTS price_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id          TEXT NOT NULL,
    price_amount   REAL,
    price_currency TEXT,
    observed_at    TEXT,
    FOREIGN KEY (ad_id) REFERENCES listings(ad_id)
);

CREATE INDEX IF NOT EXISTS idx_listings_family    ON listings(family);
CREATE INDEX IF NOT EXISTS idx_listings_category  ON listings(category_slug);
CREATE INDEX IF NOT EXISTS idx_listings_condition ON listings(condition);
CREATE INDEX IF NOT EXISTS idx_listings_price     ON listings(price_amount);
CREATE INDEX IF NOT EXISTS idx_listings_active    ON listings(is_active);
CREATE INDEX IF NOT EXISTS idx_pricehist_ad       ON price_history(ad_id);
"""

# Columns that may be supplied in a listing dict and written verbatim.
_LISTING_COLUMNS = (
    "ad_id", "title", "price_amount", "price_currency", "condition",
    "location", "county", "city", "category_slug", "family", "url",
    "seller_name", "seller_type", "description", "image_url",
    "attributes", "phones",
)


def utcnow() -> str:
    """ISO-8601 UTC timestamp (second precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_conn(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with sane defaults (WAL, FK enforcement, row dicts)."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")  # tolerate concurrent writers
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they don't already exist."""
    conn.executescript(SCHEMA)
    conn.commit()


def _normalize(listing: dict) -> dict:
    """Coerce dict/list fields to JSON strings; keep only known columns."""
    row = {}
    for col in _LISTING_COLUMNS:
        val = listing.get(col)
        if col in ("attributes", "phones") and val is not None and not isinstance(val, str):
            val = json.dumps(val, ensure_ascii=False)
        row[col] = val
    return row


def upsert_category(conn: sqlite3.Connection, slug: str, name=None, family=None,
                    parent_slug=None, url=None, is_leaf=0, last_crawled_at=None) -> None:
    """Insert or update one category row (idempotent on slug)."""
    conn.execute(
        """
        INSERT INTO categories (slug, name, family, parent_slug, url, is_leaf, last_crawled_at)
        VALUES (:slug, :name, :family, :parent_slug, :url, :is_leaf, :last_crawled_at)
        ON CONFLICT(slug) DO UPDATE SET
            name            = COALESCE(excluded.name, categories.name),
            family          = COALESCE(excluded.family, categories.family),
            parent_slug     = COALESCE(excluded.parent_slug, categories.parent_slug),
            url             = COALESCE(excluded.url, categories.url),
            is_leaf         = excluded.is_leaf,
            last_crawled_at = COALESCE(excluded.last_crawled_at, categories.last_crawled_at)
        """,
        dict(slug=slug, name=name, family=family, parent_slug=parent_slug,
             url=url, is_leaf=is_leaf, last_crawled_at=last_crawled_at),
    )


def upsert_listing(conn: sqlite3.Connection, listing: dict) -> str:
    """
    Insert or update a listing by ad_id.

    Maintains first_seen_at / last_seen_at / is_active, and appends a
    price_history row whenever the observed price differs from the last
    known price (or on first insert). Returns 'inserted' or 'updated'.
    """
    row = _normalize(listing)
    ad_id = row["ad_id"]
    if not ad_id:
        raise ValueError("listing requires an 'ad_id'")
    now = utcnow()

    existing = conn.execute(
        "SELECT price_amount FROM listings WHERE ad_id = ?", (ad_id,)
    ).fetchone()

    if existing is None:
        cols = list(row.keys()) + ["first_seen_at", "last_seen_at", "scraped_at", "is_active"]
        vals = dict(row, first_seen_at=now, last_seen_at=now, scraped_at=now, is_active=1)
        placeholders = ", ".join(f":{c}" for c in cols)
        conn.execute(
            f"INSERT INTO listings ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        _record_price(conn, ad_id, row.get("price_amount"), row.get("price_currency"), now)
        return "inserted"

    # Update existing row.
    set_cols = list(row.keys())
    assignments = ", ".join(f"{c} = :{c}" for c in set_cols)
    conn.execute(
        f"UPDATE listings SET {assignments}, last_seen_at = :last_seen_at, "
        f"scraped_at = :scraped_at, is_active = 1 WHERE ad_id = :ad_id",
        dict(row, last_seen_at=now, scraped_at=now),
    )
    # Record price only if it actually changed.
    old_price = existing["price_amount"]
    new_price = row.get("price_amount")
    if new_price is not None and new_price != old_price:
        _record_price(conn, ad_id, new_price, row.get("price_currency"), now)
    return "updated"


def _record_price(conn, ad_id, price_amount, price_currency, observed_at) -> None:
    if price_amount is None:
        return
    conn.execute(
        "INSERT INTO price_history (ad_id, price_amount, price_currency, observed_at) "
        "VALUES (?, ?, ?, ?)",
        (ad_id, price_amount, price_currency, observed_at),
    )


def update_listing(conn: sqlite3.Connection, ad_id: str, fields: dict) -> bool:
    """
    Partially update an existing listing (used by on-demand enrichment).

    Only the provided columns are written, so absent fields are preserved.
    ``attributes`` is *merged* into the existing JSON rather than replaced.
    A price change is recorded in price_history. Returns False if the ad_id
    doesn't exist.
    """
    existing = conn.execute(
        "SELECT price_amount, attributes FROM listings WHERE ad_id = ?", (ad_id,)
    ).fetchone()
    if existing is None:
        return False

    fields = dict(fields)

    # Merge attributes instead of overwriting.
    if "attributes" in fields and fields["attributes"] is not None:
        new_attrs = fields["attributes"]
        if isinstance(new_attrs, str):
            new_attrs = json.loads(new_attrs)
        merged = {}
        if existing["attributes"]:
            try:
                merged = json.loads(existing["attributes"])
            except (ValueError, TypeError):
                merged = {}
        merged.update(new_attrs)
        fields["attributes"] = json.dumps(merged, ensure_ascii=False)

    if "phones" in fields and fields["phones"] is not None and not isinstance(fields["phones"], str):
        fields["phones"] = json.dumps(fields["phones"], ensure_ascii=False)

    now = utcnow()
    fields["scraped_at"] = now
    set_cols = [c for c in fields if c in
                set(_LISTING_COLUMNS) | {"scraped_at", "last_seen_at", "is_active"}]
    assignments = ", ".join(f"{c} = :{c}" for c in set_cols)
    params = {c: fields[c] for c in set_cols}
    params["ad_id"] = ad_id
    conn.execute(f"UPDATE listings SET {assignments} WHERE ad_id = :ad_id", params)

    new_price = fields.get("price_amount")
    if new_price is not None and new_price != existing["price_amount"]:
        _record_price(conn, ad_id, new_price, fields.get("price_currency"), now)
    return True


def mark_inactive(conn: sqlite3.Connection, category_slug: str, seen_ad_ids) -> int:
    """
    Mark listings in a category that were NOT seen in the latest crawl as
    inactive (sold/delisted). Returns the number of rows deactivated.
    """
    seen = set(seen_ad_ids)
    rows = conn.execute(
        "SELECT ad_id FROM listings WHERE category_slug = ? AND is_active = 1",
        (category_slug,),
    ).fetchall()
    stale = [r["ad_id"] for r in rows if r["ad_id"] not in seen]
    if stale:
        now = utcnow()
        conn.executemany(
            "UPDATE listings SET is_active = 0, last_seen_at = ? WHERE ad_id = ?",
            [(now, ad_id) for ad_id in stale],
        )
    return len(stale)


def stats(conn: sqlite3.Connection) -> dict:
    """Quick counts for sanity-checking / CLI output."""
    def scalar(sql, *args):
        return conn.execute(sql, args).fetchone()[0]
    return {
        "categories": scalar("SELECT COUNT(*) FROM categories"),
        "listings": scalar("SELECT COUNT(*) FROM listings"),
        "active_listings": scalar("SELECT COUNT(*) FROM listings WHERE is_active = 1"),
        "price_history_rows": scalar("SELECT COUNT(*) FROM price_history"),
        "by_family": {
            r["family"]: r["n"] for r in conn.execute(
                "SELECT family, COUNT(*) n FROM listings GROUP BY family ORDER BY n DESC"
            ).fetchall()
        },
    }


if __name__ == "__main__":
    # `python -m electronics.db` initializes the database and prints stats.
    conn = get_conn()
    init_db(conn)
    print(f"Initialized DB at {DEFAULT_DB_PATH}")
    print(json.dumps(stats(conn), ensure_ascii=False, indent=2))
    conn.close()
