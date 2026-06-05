"""
On-demand enrichment: fetch full detail specs and/or phone numbers for
listings already captured by the index crawler.

This is the expensive tier (one request per ad), so it's run only for the
listings you actually care about -- e.g. "enrich the cheapest 20 used iPhones"
rather than the whole catalog.

  * Detail page -> per-category attribute table, description, location,
    condition, primary image (merged into the listing).
  * Phone API (https://www.njuskalo.hr/ccapi/v4/phone-numbers/ad/{id}) -> phone
    numbers. Needs a Bearer token from bearer_token_finder.py (Playwright).
    The token is valid ~6h and reused across calls; refreshed on 401.

SECURITY: phone requests carry a bearer token, so they go DIRECT only
(never through the free proxy pool).

Run examples:
  python -m electronics.enrich --category apple-iphone --limit 10
  python -m electronics.enrich --ad-ids 50700077 50727301 --no-phones
  python -m electronics.enrich --cheapest mobiteli --limit 20
"""

import os
import asyncio
import argparse
import importlib.util
from curl_cffi.requests import AsyncSession

from . import db
from .fetch import AsyncFetcher
from .proxies import ProxyPool
from .parsers import parse_detail_page

# Load the existing Playwright token finder from the repo root.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "bearer_token_finder", os.path.join(_ROOT, "bearer_token_finder.py"))
bearer_token_finder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bearer_token_finder)


def _phone_api(ad_id: str) -> str:
    return f"https://www.njuskalo.hr/ccapi/v4/phone-numbers/ad/{ad_id}"


class TokenManager:
    """Lazily acquires and caches a bearer token + cookies; refreshes on demand."""
    def __init__(self):
        self.token = None
        self.cookies = None

    async def ensure(self):
        if not self.token:
            await self.refresh()
        return self.token, self.cookies

    async def refresh(self):
        print("[token] acquiring bearer token via Playwright...")
        self.token, self.cookies = await bearer_token_finder.get_bearer_token_and_cookies(headless=True)
        if not self.token:
            raise RuntimeError("Failed to acquire bearer token")
        print("[token] ok")


async def fetch_phones(session, ad_id, tokens: TokenManager, _retried=False):
    try:
        token, cookies = await tokens.ensure()
    except Exception as e:
        print(f"[phone] token unavailable, skipping phones: {type(e).__name__}")
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"https://www.njuskalo.hr/-oglas-{ad_id}",
    }
    try:
        r = await session.get(_phone_api(ad_id), headers=headers, cookies=cookies,
                              timeout=15, impersonate="chrome110")
        if r.status_code == 401 and not _retried:
            await tokens.refresh()
            return await fetch_phones(session, ad_id, tokens, _retried=True)
        r.raise_for_status()
        data = r.json()
        return [n["formattedNumber"] for n in data["data"]["attributes"]["numbers"]
                if n.get("formattedNumber")]
    except Exception as e:
        print(f"[phone] ad {ad_id}: {type(e).__name__}")
        return None


def _select_ad_ids(conn, args) -> list[tuple]:
    if args.ad_ids:
        rows = conn.execute(
            f"SELECT ad_id, url FROM listings WHERE ad_id IN "
            f"({','.join('?'*len(args.ad_ids))})", args.ad_ids).fetchall()
    elif args.cheapest:
        rows = conn.execute(
            "SELECT ad_id, url FROM listings WHERE family=? AND is_active=1 "
            "AND price_amount IS NOT NULL ORDER BY price_amount ASC LIMIT ?",
            (args.cheapest, args.limit)).fetchall()
    elif args.category:
        rows = conn.execute(
            "SELECT ad_id, url FROM listings WHERE category_slug=? AND is_active=1 LIMIT ?",
            (args.category, args.limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT ad_id, url FROM listings WHERE is_active=1 LIMIT ?",
            (args.limit,)).fetchall()
    return [(r["ad_id"], r["url"]) for r in rows]


async def run_enrich(targets, detail=True, phones=True, use_proxies=False,
                     conn=None, verbose=True):
    """Core enrichment over a list of (ad_id, url) tuples.

    Returns a list of updated full listing records. Reusable by the CLI and the
    MCP server. Opens its own DB connection unless one is provided.
    """
    pool = ProxyPool() if not use_proxies else await ProxyPool.create()
    own_conn = conn is None
    if own_conn:
        conn = db.get_conn()
        db.init_db(conn)
    tokens = TokenManager()
    updated_ids = []
    async with AsyncFetcher(pool) as fetcher, AsyncSession() as phone_session:
        for i, (ad_id, url) in enumerate(targets, 1):
            fields = {}
            try:
                if detail and url:
                    html = await fetcher.get(url, allow_free=True)
                    if html:
                        fields.update(parse_detail_page(html))
                if phones:
                    nums = await fetch_phones(phone_session, ad_id, tokens)
                    if nums is not None:
                        fields["phones"] = nums
                if fields:
                    db.update_listing(conn, ad_id, fields)
                    conn.commit()
                    updated_ids.append(ad_id)
            except Exception as e:  # one bad ad must not kill the batch
                print(f"  [{i}/{len(targets)}] {ad_id} ERROR: {type(e).__name__}: {e}")
                continue
            if verbose:
                print(f"  [{i}/{len(targets)}] {ad_id} "
                      f"{'detail ' if 'attributes' in fields else ''}"
                      f"{'phones=%d' % len(fields.get('phones', [])) if 'phones' in fields else ''}")
    from . import service
    results = [service.get(conn, aid) for aid in updated_ids]
    if own_conn:
        conn.close()
    return results


async def enrich_ad_ids(ad_ids, detail=True, phones=True, use_proxies=False):
    """Enrich specific ad ids (looks up their URLs). Returns updated records.

    This is the function the MCP server calls to fetch fresh specs/phones for an
    ad on demand during a conversation.
    """
    conn = db.get_conn()
    db.init_db(conn)
    rows = conn.execute(
        f"SELECT ad_id, url FROM listings WHERE ad_id IN "
        f"({','.join('?' * len(ad_ids))})", list(ad_ids)).fetchall()
    targets = [(r["ad_id"], r["url"]) for r in rows]
    results = await run_enrich(targets, detail=detail, phones=phones,
                               use_proxies=use_proxies, conn=conn, verbose=False)
    conn.close()
    return results


async def enrich(args):
    conn = db.get_conn()
    db.init_db(conn)
    targets = _select_ad_ids(conn, args)
    if not targets:
        print("No matching listings to enrich.")
        conn.close()
        return
    print(f"Enriching {len(targets)} listings "
          f"(detail={'no' if args.no_detail else 'yes'}, "
          f"phones={'no' if args.no_phones else 'yes'})...")
    results = await run_enrich(targets, detail=not args.no_detail,
                               phones=not args.no_phones,
                               use_proxies=args.use_proxies, conn=conn)
    print(f"\nDone. enriched={len(results)}")
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="Enrich listings with detail specs + phones")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--ad-ids", nargs="+", help="specific ad ids")
    g.add_argument("--category", help="leaf category slug")
    g.add_argument("--cheapest", help="cheapest N active in this family slug")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--no-detail", action="store_true", help="skip detail page")
    ap.add_argument("--no-phones", action="store_true", help="skip phone API")
    ap.add_argument("--use-proxies", action="store_true")
    asyncio.run(enrich(ap.parse_args()))


if __name__ == "__main__":
    main()
