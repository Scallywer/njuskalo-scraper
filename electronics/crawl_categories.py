"""
Category crawler: discover leaf subcategories for each electronics family.

A family page (e.g. /mobiteli) lists its subcategories in
``div.entity-list-categories``. Some subcategories are themselves leaves (they
hold listings directly); others nest further. We walk one level deep per family
(njuskalo electronics is shallow -- brand/type, then listings) and record every
subcategory as a leaf to crawl. Families with no subcategory list (e.g.
/slusalice, /dronovi) are themselves the leaf.

Results are stored in the `categories` table.

Run:  python -m electronics.crawl_categories
"""

import asyncio
from bs4 import BeautifulSoup

from . import db
from .categories import FAMILIES, family_url
from .parsers import absolute
from .fetch import AsyncFetcher
from .proxies import ProxyPool


def extract_subcategories(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    div = soup.find("div", class_="entity-list-categories")
    if not div:
        return []
    out = []
    for a in div.select("a.CategoryListing-topCategoryLink"):
        href = a.get("href")
        if href:
            out.append({"name": a.get_text(strip=True),
                        "slug": href.strip("/").split("?")[0],
                        "url": absolute(href)})
    return out


async def crawl_categories(direct_only: bool = True):
    pool = ProxyPool() if direct_only else await ProxyPool.create()
    conn = db.get_conn()
    db.init_db(conn)
    now = db.utcnow()
    total_leaves = 0

    async with AsyncFetcher(pool) as fetcher:
        for fam_slug, fam_label in FAMILIES.items():
            url = family_url(fam_slug)
            html = await fetcher.get(url, allow_free=True)
            if not html:
                print(f"[{fam_slug}] FAILED to fetch family page")
                continue
            subs = extract_subcategories(html)
            # Record the family itself.
            db.upsert_category(conn, slug=fam_slug, name=fam_label, family=fam_slug,
                               parent_slug=None, url=url,
                               is_leaf=0 if subs else 1, last_crawled_at=now)
            if not subs:
                # Family page is itself a leaf (holds listings directly).
                total_leaves += 1
                print(f"[{fam_slug}] leaf family (no subcategories)")
            else:
                for sub in subs:
                    db.upsert_category(conn, slug=sub["slug"], name=sub["name"],
                                       family=fam_slug, parent_slug=fam_slug,
                                       url=sub["url"], is_leaf=1, last_crawled_at=now)
                total_leaves += len(subs)
                print(f"[{fam_slug}] {len(subs)} subcategories")
            conn.commit()

    leaves = conn.execute("SELECT COUNT(*) FROM categories WHERE is_leaf=1").fetchone()[0]
    print(f"\nDone. {leaves} leaf categories recorded across {len(FAMILIES)} families.")
    conn.close()


if __name__ == "__main__":
    asyncio.run(crawl_categories())
