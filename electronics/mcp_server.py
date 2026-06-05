"""
MCP server exposing the njuskalo electronics database to Claude.

Run as a local stdio MCP server so Claude (Claude Code / Claude Desktop) can
query and refresh used-electronics data natively during a conversation.

Tools:
  read  -> search_listings, price_stats, price_history, get_listing,
           list_categories, list_families
  fetch -> enrich_listing (fresh specs + phone for an ad), crawl_category
           (pull the latest listings for a category into the DB)

Register in Claude Code (.mcp.json):
  {
    "mcpServers": {
      "njuskalo-electronics": {
        "command": "/abs/path/to/venv/bin/python",
        "args": ["-m", "electronics.mcp_server"],
        "cwd": "/abs/path/to/njuskalo-scraper"
      }
    }
  }
"""

from contextlib import closing

from mcp.server.fastmcp import FastMCP

from . import db, service
from .enrich import enrich_ad_ids
from .crawl_index import crawl_index

mcp = FastMCP("njuskalo-electronics")


# ---------------------------------------------------------------- read tools

@mcp.tool()
def search_listings(family: str = None, category: str = None,
                    condition: str = None, min_price: float = None,
                    max_price: float = None, text: str = None,
                    memory: str = None, sort: str = "price",
                    include_inactive: bool = False, limit: int = 20) -> list[dict]:
    """Search used-electronics listings scraped from njuskalo.hr.

    Args:
        family: top-level group slug, e.g. 'mobiteli', 'informatika', 'foto',
            'audio-oprema', 'bijela-tehnika', 'mali-kucanski-aparati',
            'slusalice', 'dronovi'. Use list_families to see what's populated.
        category: leaf category slug, e.g. 'apple-iphone', 'samsung-mobiteli'.
            Use list_categories to discover valid slugs.
        condition: Croatian condition value, e.g. 'rabljeno' (used), 'novo'
            (new), 'novo s etiketom'. Only set on detail-enriched listings.
        min_price / max_price: price bounds in EUR.
        text: case-insensitive substring to match in the title.
        memory: convenience filter for phone/storage capacity, e.g. '256 GB'
            (matched against the 'Memorija' attribute; enriched listings only).
        sort: 'price' (cheapest first) or 'recent' (newest scrape first).
        include_inactive: include sold/delisted listings (default False).
        limit: max results (default 20).

    Returns a list of listings with ad_id, title, price, condition, location,
    category, family, and url.
    """
    specs = {"Memorija": memory} if memory else None
    with closing(db.get_conn()) as conn:
        return service.search(conn, family=family, category=category,
                              condition=condition, min_price=min_price,
                              max_price=max_price, text=text, specs=specs,
                              sort=sort, include_inactive=include_inactive,
                              limit=limit)


@mcp.tool()
def price_stats(family: str = None, category: str = None) -> dict:
    """Price statistics (count, average, median, min, max in EUR) for a family
    or leaf category of active listings -- the 'going rate' for used items."""
    with closing(db.get_conn()) as conn:
        return service.stats(conn, family=family, category=category)


@mcp.tool()
def price_history(ad_id: str) -> list[dict]:
    """Observed price changes for one listing over time (oldest first).
    Useful to see whether a seller has dropped the price."""
    with closing(db.get_conn()) as conn:
        return service.history(conn, ad_id)


@mcp.tool()
def get_listing(ad_id: str) -> dict | None:
    """Full record for one listing, including the per-category spec attributes
    and phone numbers (if it has been enriched). Returns None if not found."""
    with closing(db.get_conn()) as conn:
        return service.get(conn, ad_id)


@mcp.tool()
def list_categories(family: str = None) -> list[dict]:
    """List leaf category slugs (optionally within a family) so you know valid
    values for the `category` filter."""
    with closing(db.get_conn()) as conn:
        return service.list_categories(conn, family=family, leaf_only=True)


@mcp.tool()
def list_families() -> list[dict]:
    """List the electronics families present in the DB with active-listing counts."""
    with closing(db.get_conn()) as conn:
        return service.families(conn)


# --------------------------------------------------------------- fetch tools

@mcp.tool()
async def enrich_listing(ad_id: str, fetch_phone: bool = True) -> dict | None:
    """Fetch fresh data for one listing from njuskalo right now: the full spec
    table, description, location, condition, and (if fetch_phone) the seller's
    phone number. Updates the stored record and returns it.

    Use this when the user wants details/contact for a specific listing that
    hasn't been enriched yet. Hits njuskalo live, so use sparingly.
    """
    results = await enrich_ad_ids([ad_id], detail=True, phones=fetch_phone)
    return results[0] if results else None


@mcp.tool()
async def crawl_category(category: str, max_pages: int = 3) -> dict:
    """Pull the latest listings for a leaf category from njuskalo into the DB
    (index data only: id, title, price, url, image). Use when the user asks
    about a category that may be stale or empty. `max_pages` bounds the work
    (each page ~40 ads). Returns updated DB stats.

    Find valid category slugs with list_categories.
    """
    await crawl_index(category=category, max_pages=max_pages, direct_only=True)
    with closing(db.get_conn()) as conn:
        return db.stats(conn)


def main():
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
