"""
Query service: pure functions over the electronics DB returning plain Python
data (dicts/lists). Shared by the CLI (query.py) and the MCP server
(mcp_server.py) so there is a single source of truth for the query logic.
"""

from . import db


def _row(r):
    return dict(r) if r is not None else None


def search(conn, family=None, category=None, condition=None, min_price=None,
           max_price=None, text=None, specs=None, sort="price",
           include_inactive=False, limit=20):
    """Return a list of listing dicts matching the filters.

    specs: dict of attribute name -> exact value (e.g. {"Memorija": "256 GB"}).
    sort: "price" (cheapest first) or "recent".
    """
    q = ["SELECT ad_id, title, price_amount, price_currency, condition, "
         "location, category_slug, family, url FROM listings WHERE 1=1"]
    p = []
    if not include_inactive:
        q.append("AND is_active = 1")
    if family:
        q.append("AND family = ?"); p.append(family)
    if category:
        q.append("AND category_slug = ?"); p.append(category)
    if condition:
        q.append("AND condition = ?"); p.append(condition)
    if min_price is not None:
        q.append("AND price_amount >= ?"); p.append(min_price)
    if max_price is not None:
        q.append("AND price_amount <= ?"); p.append(max_price)
    if text:
        q.append("AND lower(title) LIKE ?"); p.append(f"%{text.lower()}%")
    for k, v in (specs or {}).items():
        q.append("AND json_extract(attributes, ?) = ?"); p.extend([f"$.{k}", v])
    order = "price_amount ASC" if sort == "price" else "scraped_at DESC"
    q.append(f"AND price_amount IS NOT NULL ORDER BY {order} LIMIT ?")
    p.append(limit)
    return [dict(r) for r in conn.execute(" ".join(q), p).fetchall()]


def stats(conn, family=None, category=None):
    """Return price stats (count/avg/median/min/max) for a family or category."""
    where, p = ["price_amount IS NOT NULL", "is_active = 1"], []
    if family:
        where.append("family = ?"); p.append(family)
    if category:
        where.append("category_slug = ?"); p.append(category)
    w = " AND ".join(where)
    row = conn.execute(
        f"SELECT COUNT(*) n, ROUND(AVG(price_amount)) avg, "
        f"MIN(price_amount) mn, MAX(price_amount) mx FROM listings WHERE {w}", p
    ).fetchone()
    out = {"count": row["n"], "avg": row["avg"], "min": row["mn"], "max": row["mx"]}
    if row["n"]:
        med = conn.execute(
            f"SELECT price_amount FROM listings WHERE {w} ORDER BY price_amount "
            f"LIMIT 1 OFFSET (SELECT COUNT(*)/2 FROM listings WHERE {w})", p + p
        ).fetchone()
        out["median"] = med["price_amount"] if med else None
    return out


def history(conn, ad_id):
    """Return the price-history points for an ad (oldest first)."""
    rows = conn.execute(
        "SELECT price_amount, price_currency, observed_at FROM price_history "
        "WHERE ad_id = ? ORDER BY observed_at", (ad_id,)).fetchall()
    return [dict(r) for r in rows]


def get(conn, ad_id):
    """Return the full listing record (attributes/phones parsed) or None."""
    import json
    r = conn.execute("SELECT * FROM listings WHERE ad_id = ?", (ad_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("attributes", "phones"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (ValueError, TypeError):
                pass
    return d


def list_categories(conn, family=None, leaf_only=True):
    """Return categories (slug/name/family) so callers know valid filter values."""
    q = ["SELECT slug, name, family, is_leaf FROM categories WHERE 1=1"]
    p = []
    if leaf_only:
        q.append("AND is_leaf = 1")
    if family:
        q.append("AND family = ?"); p.append(family)
    q.append("ORDER BY family, slug")
    return [dict(r) for r in conn.execute(" ".join(q), p).fetchall()]


def families(conn):
    """Return the distinct families with listing counts."""
    rows = conn.execute(
        "SELECT family, COUNT(*) n FROM listings WHERE is_active=1 "
        "GROUP BY family ORDER BY n DESC").fetchall()
    return [dict(r) for r in rows]
