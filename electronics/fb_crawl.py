"""
Facebook Marketplace crawler (anonymous, local).

Why anonymous: the Bright Data v. Meta ruling found Meta's ToS only bind
*logged-in* users, so scraping logged-off public pages is the lowest-risk path
(no account, no ban risk, no ToS breach). Meta serves the first page of search
results — title, price, city, listing id — embedded as JSON, no login wall.

Location: FB resolves location by a numeric place id in the URL
(/marketplace/<id>/search). A city *slug* like "osijek" is NOT recognized and
silently falls back to a default (US) location, so we MUST use the numeric id.
Osijek = 107795952581802 (covers Osijek + Slavonia: Slavonski Brod, Vinkovci,
Vukovar, Đakovo, Valpovo, Našice...).

Limits: ~one page (~24 results) per query, ~30-60 req/hour/IP unauthenticated.
We only poll a handful of queries, so we stay far under that. No pagination, no
seller PII stored (GDPR) — just title / price / url / city.

Results are upserted into the same `listings` table with source='facebook' and
ad_id "fb<listing_id>" (prefixed so it can't collide with njuskalo ids), under
the category_slug of the watch that asked for them, so the existing watch logic
matches FB and njuskalo rows together.
"""

import re
import asyncio
import random
from curl_cffi.requests import AsyncSession

from . import db

OSIJEK_LOCATION_ID = "107795952581802"
BASE = "https://www.facebook.com/marketplace"
HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "hr-HR,hr;q=0.9,en;q=0.8",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
}
_ID_BEFORE = '"if_gk_just_listed_tag_on_search_feed"'


def _decode(s: str) -> str:
    """Turn FB's \\uXXXX JSON escapes into real characters."""
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)


def parse_fb_search(html: str) -> list[dict]:
    """Parse a FB Marketplace search page into listing dicts."""
    out, seen = [], set()
    marks = [m.start() for m in re.finditer(re.escape(_ID_BEFORE), html)]
    for k, pos in enumerate(marks):
        idm = re.search(r'"id":"(\d{8,})"\},\Z', html[max(0, pos - 80):pos])
        if not idm:
            continue
        lid = idm.group(1)
        if lid in seen:
            continue
        seg = html[pos: marks[k + 1] if k + 1 < len(marks) else pos + 4000]
        pr = re.search(r'"listing_price":\{"formatted_amount":"([^"]+)",'
                       r'"amount_with_offset_in_currency":"[^"]*","amount":"([\d.]+)"', seg)
        ti = re.search(r'"marketplace_listing_title":"([^"]+)"', seg)
        if not (pr and ti):
            continue
        fmt, amt = pr.group(1), float(pr.group(2))
        cur = ("EUR" if ("\\u20ac" in fmt or "€" in fmt)
               else "USD" if "USD" in fmt
               else "HRK" if "kn" in fmt.lower() else None)
        ci = re.search(r'"city":"([^"]+)"', seg)
        sold = re.search(r'"is_sold":(true|false)', seg)
        seen.add(lid)
        out.append({
            "ad_id": f"fb{lid}",
            "source": "facebook",
            "title": _decode(ti.group(1)),
            "price_amount": amt,
            "price_currency": cur,
            "city": _decode(ci.group(1)) if ci else None,
            "url": f"https://www.facebook.com/marketplace/item/{lid}/",
            "is_sold": bool(sold and sold.group(1) == "true"),
        })
    return out


async def _fetch(session, query: str, location_id: str) -> str | None:
    url = f"{BASE}/{location_id}/search?query={query.replace(' ', '%20')}"
    try:
        r = await asyncio.wait_for(
            session.get(url, headers=HEADERS, impersonate="chrome110"), timeout=25)
        return r.text if r.status_code == 200 else None
    except Exception as e:
        print(f"[fb] fetch failed for {query!r}: {type(e).__name__}")
        return None


async def crawl_fb(queries, location_id=OSIJEK_LOCATION_ID, conn=None,
                   jitter=(3.0, 8.0)) -> int:
    """Crawl FB for a list of (query, category_slug, family) tuples.

    Upserts each parsed listing under the given category_slug with
    source='facebook'. Skips sold items. Returns count upserted. Does NOT
    deactivate anything (it only adds FB rows to a njuskalo category).
    """
    own = conn is None
    if own:
        conn = db.get_conn()
        db.init_db(conn)
    total = 0
    async with AsyncSession() as session:
        for query, category_slug, family in queries:
            await asyncio.sleep(random.uniform(*jitter))
            html = await _fetch(session, query, location_id)
            if not html:
                continue
            for item in parse_fb_search(html):
                if item.get("is_sold"):
                    continue
                item.pop("is_sold", None)
                item["category_slug"] = category_slug
                item["family"] = family
                db.upsert_listing(conn, item)
                total += 1
            conn.commit()
            print(f"[fb] {query!r} -> upserted (running {total})")
    if own:
        conn.close()
    return total
