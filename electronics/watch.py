"""
Watchlist evaluator: hunt the DB for deals matching saved "wants" and report
only what's NEW or has DROPPED in price since last time (no repeat spam).

Designed to run nightly after a full crawl (see scripts/nightly.sh):
    python -m electronics.crawl_index      # refresh DB (prices, sold flags)
    python -m electronics.watch            # evaluate watches -> report

State is kept in a `watch_hits` table so a given listing is only reported once
per watch -- unless its price drops further, which re-reports it as a new deal.

Add/adjust what you're hunting in WATCHES below.
"""

import os
import argparse
from datetime import datetime, timezone

from . import db, service

REPORT_PATH = os.path.join(db.REPO_ROOT, "data", "watch_report.md")

# ----------------------------------------------------------------- watch config
# Each watch = a saved "want". A listing is a hit when its title contains one of
# a target's `any` keywords, contains none of `exclude`, and price <= max_price.
WATCHES = [
    {
        "name": "wife-iphone",
        "note": "Budget iPhone upgrade for wife (coming from a Samsung A54)",
        "crawl": ["apple-iphone"],
        "exclude": ["face id", "faceid", "za dijelove", "dijelove", "ne radi",
                    "neispravan", "oštećen", "ostecen", "slomljen", "puknut",
                    "pro max", "mini"],
        "targets": [
            {"label": "iPhone 14 deal", "any": ["iphone 14"], "max_price": 230},
            {"label": "iPhone 13 deal", "any": ["iphone 13"], "max_price": 190},
        ],
    },
    {
        "name": "gta-xbox",
        "note": "Cheapest Xbox for GTA VI at launch (friends are on Xbox)",
        "crawl": ["xbox-series-s", "xbox-series-x"],
        "exclude": ["xbox one", "za dijelove", "dijelove", "ne radi", "neispravan",
                    "kontroler", "controller", "ssd", "wd black", "samo kutija",
                    "kabel", "kabal", "stalak", "stand", "kamera", "igra ", "igre",
                    " fc ", "fifa"],
        "targets": [
            {"label": "Series S bargain", "any": ["series s"], "max_price": 170},
            {"label": "Series X deal", "any": ["series x"], "max_price": 350},
        ],
    },
]


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watch_hits (
            watch_name      TEXT,
            ad_id           TEXT,
            target_label    TEXT,
            last_price      REAL,
            first_reported  TEXT,
            last_reported   TEXT,
            notified_at     TEXT,
            PRIMARY KEY (watch_name, ad_id)
        )
    """)
    # Migrate older tables that predate notified_at; backfill existing rows as
    # already-notified so we don't re-spam past deals on the first flush.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(watch_hits)").fetchall()]
    if "notified_at" not in cols:
        conn.execute("ALTER TABLE watch_hits ADD COLUMN notified_at TEXT")
        conn.execute("UPDATE watch_hits SET notified_at=? WHERE notified_at IS NULL",
                     (_now(),))
    conn.commit()


def _matches(conn, watch, target):
    """Active listings matching one target's keyword/price/exclude rules.

    Scoped to the watch's own categories so the price-sorted limit isn't eaten
    by thousands of unrelated cheap listings (accessories/games) DB-wide.
    """
    out, seen = [], set()
    for slug in watch["crawl"]:
        rows = service.search(conn, category=slug, max_price=target["max_price"],
                              limit=500, sort="price")
        for r in rows:
            t = (r["title"] or "").lower()
            if r["ad_id"] in seen:
                continue
            if not any(k in t for k in target["any"]):
                continue
            if any(x in t for x in watch["exclude"]):
                continue
            seen.add(r["ad_id"])
            out.append(r)
    return out


def evaluate(conn, watch):
    """Return the list of NEW or price-dropped hits for one watch."""
    _ensure_table(conn)
    fresh = []
    for target in watch["targets"]:
        for r in _matches(conn, watch, target):
            ad_id, price = r["ad_id"], r["price_amount"]
            prev = conn.execute(
                "SELECT last_price FROM watch_hits WHERE watch_name=? AND ad_id=?",
                (watch["name"], ad_id)).fetchone()
            now = _now()
            if prev is None:
                # notified_at left NULL -> queued for the next flush (8am job)
                conn.execute(
                    "INSERT INTO watch_hits (watch_name, ad_id, target_label, "
                    "last_price, first_reported, last_reported, notified_at) "
                    "VALUES (?,?,?,?,?,?,NULL)",
                    (watch["name"], ad_id, target["label"], price, now, now))
                fresh.append({**r, "_reason": "NEW", "_target": target["label"]})
            elif price is not None and price < (prev["last_price"] or 1e9):
                drop = prev["last_price"] - price
                # re-queue (notified_at=NULL) so the price drop gets pushed
                conn.execute(
                    "UPDATE watch_hits SET last_price=?, last_reported=?, target_label=?, "
                    "notified_at=NULL WHERE watch_name=? AND ad_id=?",
                    (price, now, target["label"], watch["name"], ad_id))
                fresh.append({**r, "_reason": f"PRICE DROP -{drop:.0f}E",
                              "_target": target["label"]})
    conn.commit()
    return fresh


async def crawl_watch_categories(watch, max_pages=8):
    from .crawl_index import crawl_index
    for slug in watch["crawl"]:
        await crawl_index(category=slug, max_pages=max_pages, direct_only=True)


def render_report(results: dict) -> str:
    lines = [f"# Njuskalo watch report — {_now()}", ""]
    total = sum(len(v) for v in results.values())
    if not total:
        lines.append("_No new deals or price drops since last run._")
        return "\n".join(lines)
    for name, hits in results.items():
        watch = next(w for w in WATCHES if w["name"] == name)
        lines.append(f"## {name} — {watch['note']}")
        if not hits:
            lines.append("- (nothing new)")
        for h in sorted(hits, key=lambda x: x["price_amount"] or 9e9):
            price = f"{h['price_amount']:.0f} {h['price_currency']}"
            lines.append(f"- **{price}** · {h['_reason']} · {h['_target']}  \n"
                         f"  {h['title'][:70]}  \n  {h['url']}")
        lines.append("")
    return "\n".join(lines)


def flush_pending(conn) -> int:
    """Send any deals queued (notified_at IS NULL) but still active to Telegram,
    then mark them notified. This is the 8am job — kept separate from the crawl
    so notifications never fire in the middle of the night.
    """
    from . import notify as notifier
    _ensure_table(conn)
    rows = conn.execute("""
        SELECT wh.watch_name, wh.ad_id, wh.target_label,
               l.title, l.url, l.price_amount, l.price_currency
        FROM watch_hits wh JOIN listings l ON l.ad_id = wh.ad_id
        WHERE wh.notified_at IS NULL AND l.is_active = 1
        ORDER BY wh.watch_name, l.price_amount
    """).fetchall()
    if not rows:
        print("[flush] nothing pending")
        return 0
    lines, cur = ["🔔 Njuskalo deals"], None
    for r in rows:
        if r["watch_name"] != cur:
            cur = r["watch_name"]
            note = next((w["note"] for w in WATCHES if w["name"] == cur), cur)
            lines.append(f"\n— {note} —")
        price = f"{r['price_amount']:.0f}{r['price_currency']}" if r["price_amount"] else "n/a"
        lines.append(f"{price} · {r['target_label']} · {r['title'][:50]}\n{r['url']}")
    if not notifier.send("\n".join(lines)):
        print("[flush] send failed — leaving deals queued for next flush")
        return 0
    now = _now()
    conn.executemany(
        "UPDATE watch_hits SET notified_at=? WHERE watch_name=? AND ad_id=?",
        [(now, r["watch_name"], r["ad_id"]) for r in rows])
    conn.commit()
    print(f"[flush] pushed {len(rows)} deals to Telegram")
    return len(rows)


def flush():
    conn = db.get_conn()
    db.init_db(conn)
    n = flush_pending(conn)
    conn.close()
    return n


async def run(do_crawl=True, max_pages=8):
    """Crawl watched categories + evaluate. Queues fresh deals (does NOT send;
    the separate flush job delivers them at a civilised hour)."""
    conn = db.get_conn()
    db.init_db(conn)
    if do_crawl:
        for w in WATCHES:
            await crawl_watch_categories(w, max_pages=max_pages)
    results = {w["name"]: evaluate(conn, w) for w in WATCHES}
    report = render_report(results)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    conn.close()
    print(report)
    fresh = sum(len(v) for v in results.values())
    print(f"\n[report written to {REPORT_PATH}] — {fresh} deal(s) queued for next flush")
    return results


def main():
    import asyncio
    ap = argparse.ArgumentParser(description="Evaluate njuskalo watchlist for deals")
    ap.add_argument("--no-crawl", action="store_true",
                    help="don't crawl first (use when a full crawl just ran)")
    ap.add_argument("--flush", action="store_true",
                    help="send queued deals to Telegram and exit (the 8am job)")
    ap.add_argument("--max-pages", type=int, default=8)
    args = ap.parse_args()
    if args.flush:
        flush()
    else:
        asyncio.run(run(do_crawl=not args.no_crawl, max_pages=args.max_pages))


if __name__ == "__main__":
    main()
