"""
HTML parsers for njuskalo electronics pages.

Two surfaces:
  * parse_index_page()  -> core fields for ~40 ads from a paginated category page
  * parse_detail_page() -> full per-category attribute table + description for one ad

njuskalo is a Vue/Nuxt SPA but server-renders the listing data into the static
HTML, so plain HTML parsing (no browser) gets everything except JS-rendered
pagination metadata (handled in the crawler by paginating until no new ads).
"""

import re
from bs4 import BeautifulSoup

BASE_URL = "https://www.njuskalo.hr"
_AD_ID_RE = re.compile(r"-oglas-(\d+)")
# Croatian price format: "1.200 €" or "1.200,50 €". Dots = thousands, comma = decimal.
_PRICE_RE = re.compile(r"([\d.\s]+(?:,\d+)?)\s*(€|EUR|kn|HRK)", re.I)


def absolute(href: str) -> str:
    if not href:
        return href
    return href if href.startswith("http") else f"{BASE_URL}{href}"


def ad_id_from_url(url: str):
    m = _AD_ID_RE.search(url or "")
    return m.group(1) if m else None


def parse_price(text: str):
    """Return (amount: float|None, currency: str|None) from a price string."""
    if not text:
        return None, None
    m = _PRICE_RE.search(text)
    if not m:
        return None, None
    num, cur = m.group(1), m.group(2).upper()
    num = num.replace(".", "").replace(" ", "").replace("\xa0", "").replace(",", ".")
    cur = {"€": "EUR", "KN": "HRK"}.get(cur, cur)
    try:
        return float(num), cur
    except ValueError:
        return None, cur


def _img_src(item):
    img = item.select_one("img")
    if not img:
        return None
    return img.get("data-src") or img.get("src")


def parse_index_page(html: str, category_slug: str = None, family: str = None) -> list[dict]:
    """Parse a paginated category page into a list of listing dicts (core fields).

    njuskalo category pages contain several EntityList sections: the real
    organic results (``EntityList--Regular``) plus sponsored-but-relevant ones
    (``EntityList--VauVau`` / ``--SuperVau``), AND a ``EntityList--Latest``
    widget of newest-site-wide ads that repeats on every page and is unrelated
    to the category. We exclude that widget so the data stays category-clean.
    """
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    # Items belonging to the cross-category "Latest" widget -> skip them.
    excluded = {id(li) for li in soup.select(
        ".EntityList--Latest li.EntityList-item, "
        ".EntityList--ListItemLatestAd li.EntityList-item")}
    for item in soup.select("li.EntityList-item"):
        if id(item) in excluded:
            continue
        a = item.select_one("h3.entity-title a") or item.select_one(".entity-title a")
        if not a:
            continue
        href = a.get("href")
        ad_id = ad_id_from_url(href)
        if not ad_id or ad_id in seen:
            continue
        seen.add(ad_id)
        price_el = (item.select_one(".price--hrk")
                    or item.select_one(".entity-prices .price")
                    or item.select_one(".price"))
        amount, currency = parse_price(price_el.get_text(" ", strip=True)) if price_el else (None, None)
        out.append({
            "ad_id": ad_id,
            "title": a.get_text(strip=True),
            "url": absolute(href),
            "price_amount": amount,
            "price_currency": currency,
            "image_url": _img_src(item),
            "category_slug": category_slug,
            "family": family,
        })
    return out


def parse_detail_page(html: str) -> dict:
    """Parse one ad's detail page into core fields + an attributes dict."""
    soup = BeautifulSoup(html, "html.parser")
    out = {"attributes": {}}

    h1 = soup.find("h1")
    if h1:
        out["title"] = h1.get_text(strip=True)

    # Price (detail pages render "Cijena 530 €")
    price_el = soup.find(class_=re.compile("ClassifiedDetailSummary-priceDomestic|price--", re.I))
    if price_el:
        amount, currency = parse_price(price_el.get_text(" ", strip=True))
        out["price_amount"], out["price_currency"] = amount, currency

    # Description
    desc = soup.find(class_=re.compile("ClassifiedDetailDescription", re.I))
    if desc:
        out["description"] = desc.get_text(" ", strip=True)

    # Primary image
    img = soup.find("img", src=re.compile(r"/image-"))
    if img:
        out["image_url"] = img.get("src")

    # Attribute table: dt/dd pairs across the basic-details / property groups lists
    for dl in soup.select("dl.ClassifiedDetailBasicDetails-list, "
                          "dl.ClassifiedDetailPropertyGroups-group, dl"):
        kids = dl.find_all(["dt", "dd"])
        for i in range(0, len(kids) - 1, 2):
            if kids[i].name == "dt" and kids[i + 1].name == "dd":
                k = kids[i].get_text(" ", strip=True).rstrip(":")
                v = kids[i + 1].get_text(" ", strip=True)
                if k and v:
                    out["attributes"][k] = v

    # Pull location/condition out of attributes into top-level columns if present.
    attrs = out["attributes"]
    loc = attrs.get("Lokacija")
    if loc:
        out["location"] = loc
        parts = [p.strip() for p in loc.split(",")]
        if parts:
            out["county"] = parts[0]
        if len(parts) > 1:
            out["city"] = parts[1]
    cond = attrs.get("Stanje")
    if cond:
        out["condition"] = cond

    return out
