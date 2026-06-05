"""
Index crawler: the bulk data collector.

For each leaf category, paginate the listing pages and upsert the ~40 ads per
page (ad_id, title, price, url, image) into the DB. This is the cheap, fast
tier: ~1 request per 40 listings instead of one per listing.

Pagination has no JS-free metadata, and out-of-range pages don't go empty (they
clamp/rotate). So we stop a category when a page contributes no NEW ad ids
(with a small consecutive-empty tolerance and a hard page cap as a backstop).

After a category is fully paged, listings previously active in that category but
not seen this run are marked inactive (sold/delisted). Price changes are
recorded in price_history by the db layer.

Run examples:
  python -m electronics.crawl_index --family mobiteli --max-pages 3
  python -m electronics.crawl_index --category apple-iphone
  python -m electronics.crawl_index            # full electronics catalog
"""

import asyncio
import random
import argparse

from . import db
from .fetch import AsyncFetcher
from .proxies import ProxyPool
from .parsers import parse_index_page

PAGE_CAP = 200          # backstop against runaway pagination
EMPTY_TOLERANCE = 1     # stop after this many consecutive no-new-ads pages


def _page_url(base_url: str, page: int) -> str:
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}page={page}"


async def crawl_category(fetcher, conn, cat) -> dict:
    """Crawl one leaf category. Returns a small stats dict."""
    slug, base_url, family = cat["slug"], cat["url"], cat["family"]
    seen, inserted, updated = set(), 0, 0
    empty_streak = 0
    completed = False  # True only if we reached the natural end (ran out of new ads)

    for page in range(1, PAGE_CAP + 1):
        html = await fetcher.get(_page_url(base_url, page), allow_free=True)
        if not html:
            break  # fetch failure -> incomplete, don't retire listings
        ads = parse_index_page(html, category_slug=slug, family=family)
        new_ids = [a["ad_id"] for a in ads if a["ad_id"] not in seen]
        if not new_ids:
            empty_streak += 1
            if empty_streak > EMPTY_TOLERANCE:
                completed = True  # reached the end cleanly
                break
            continue
        empty_streak = 0
        for ad in ads:
            if ad["ad_id"] in seen:
                continue
            seen.add(ad["ad_id"])
            result = db.upsert_listing(conn, ad)
            inserted += result == "inserted"
            updated += result == "updated"
        conn.commit()

    # Only retire unseen listings when the crawl actually finished the category.
    # A truncated crawl (hit PAGE_CAP or a fetch error) must NOT mark the pages
    # it never reached as sold.
    deactivated = db.mark_inactive(conn, slug, seen) if completed else 0
    conn.commit()
    return {"slug": slug, "found": len(seen), "inserted": inserted,
            "updated": updated, "deactivated": deactivated,
            "completed": completed}


async def crawl_index(family=None, category=None, max_pages=None,
                      limit_categories=None, direct_only=True):
    global PAGE_CAP
    if max_pages:
        PAGE_CAP = max_pages

    pool = ProxyPool() if direct_only else await ProxyPool.create()
    conn = db.get_conn()
    db.init_db(conn)

    q = "SELECT slug, url, family FROM categories WHERE is_leaf=1"
    params = []
    if category:
        q += " AND slug = ?"; params.append(category)
    elif family:
        q += " AND family = ?"; params.append(family)
    cats = [dict(r) for r in conn.execute(q, params).fetchall()]

    # Fallback: an explicit --category not yet in the table -> synthesize it from
    # the slug so on-demand crawls work without a prior crawl_categories run.
    if category and not cats:
        from .parsers import BASE_URL
        url = f"{BASE_URL}/{category}"
        db.upsert_category(conn, slug=category, url=url, is_leaf=1,
                           last_crawled_at=db.utcnow())
        conn.commit()
        cats = [{"slug": category, "url": url, "family": None}]

    random.shuffle(cats)  # randomize order (anti-pattern-detection)
    if limit_categories:
        cats = cats[:limit_categories]

    if not cats:
        print("No matching leaf categories. Run crawl_categories first.")
        conn.close()
        return

    print(f"Crawling {len(cats)} categories"
          f"{' (max %d pages each)' % max_pages if max_pages else ''}...")
    totals = {"found": 0, "inserted": 0, "updated": 0, "deactivated": 0}
    async with AsyncFetcher(pool) as fetcher:
        for i, cat in enumerate(cats, 1):
            st = await crawl_category(fetcher, conn, cat)
            for k in totals:
                totals[k] += st[k]
            print(f"  [{i}/{len(cats)}] {st['slug']:28} "
                  f"found={st['found']:4} +{st['inserted']} ~{st['updated']} "
                  f"-{st['deactivated']}{'' if st['completed'] else ' (capped — not retired)'}")

    print(f"\nDone. found={totals['found']} inserted={totals['inserted']} "
          f"updated={totals['updated']} deactivated={totals['deactivated']}")
    print(db.stats(conn))
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="Crawl njuskalo electronics index pages")
    ap.add_argument("--family", help="only this family slug")
    ap.add_argument("--category", help="only this leaf category slug")
    ap.add_argument("--max-pages", type=int, help="cap pages per category")
    ap.add_argument("--limit-categories", type=int, help="cap number of categories")
    ap.add_argument("--use-proxies", action="store_true",
                    help="use the free/paid proxy pool (default: direct)")
    args = ap.parse_args()
    asyncio.run(crawl_index(family=args.family, category=args.category,
                            max_pages=args.max_pages,
                            limit_categories=args.limit_categories,
                            direct_only=not args.use_proxies))


if __name__ == "__main__":
    main()
