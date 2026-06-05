# Njuskalo Electronics Pipeline

A self-contained pipeline that scrapes **electronics** listings from
[njuskalo.hr](https://www.njuskalo.hr) into a SQLite database you can query —
the foundation for a "ask me about used electronics" API.

It lives alongside (and reuses bits of) the original real-estate scraper, but is
otherwise independent: its own modules, its own database.

## Architecture

```
families (mobiteli, informatika, foto, …)
   └─ crawl_categories.py ─→ categories table   (86 leaf subcategories)
        └─ crawl_index.py  ─→ listings table    (core data, ~40 ads/request)
             └─ enrich.py   ─→ detail specs + phone numbers  (on demand only)
                  └─ query.py / a future API
```

Two data tiers, on purpose:

| Tier | Module | Cost | When |
|------|--------|------|------|
| **Index** — id, title, price, url, image | `crawl_index.py` | ~1 req / 40 ads | bulk, fast (full catalog ≈ 1–3 h direct) |
| **Detail + phone** — full specs, location, condition, phone | `enrich.py` | 1 req / ad (phone needs a token) | only for listings you care about |

This split is the speed lever: the index pages carry the queryable core data, so
the whole catalog is a few thousand requests, not hundreds of thousands.

## Database (`data/njuskalo.db`, gitignored)

- **`categories`** — family / leaf subcategory tree.
- **`listings`** — stable core columns + a JSON **`attributes`** bag for the
  per-category specs that vary by product type (a phone has `Memorija`/`Boja`;
  a TV has `Dijagonala ekrana`/`Rezolucija`). Plus `is_active` + `last_seen_at`
  lifecycle.
- **`price_history`** — one row per observed price change → "going rate" and
  "price dropped" queries.

Schema is Postgres-portable for when this becomes a multi-user API.

## Usage

```bash
# one-time
python -m electronics.db                 # create the database
python -m electronics.crawl_categories   # discover leaf categories

# bulk index crawl (the fast tier)
python -m electronics.crawl_index --family mobiteli            # one family
python -m electronics.crawl_index --category samsung-mobiteli  # one category
python -m electronics.crawl_index                              # full catalog

# enrich on demand (specs + phones) — phone needs Playwright token
python -m electronics.enrich --cheapest mobiteli --limit 20
python -m electronics.enrich --category apple-iphone --no-phones

# query
python -m electronics.query search --family mobiteli --condition rabljeno --max-price 400
python -m electronics.query search --category samsung-mobiteli --spec Memorija="256 GB"
python -m electronics.query stats --family mobiteli
python -m electronics.query history <ad_id>
python -m electronics.query show <ad_id>
```

## Use it from Claude (MCP)

`mcp_server.py` exposes the database to Claude as native tools via the Model
Context Protocol, so Claude can query and refresh used-electronics data mid-chat.

Tools: `search_listings`, `price_stats`, `price_history`, `get_listing`,
`list_categories`, `list_families` (read) and `enrich_listing`, `crawl_category`
(fetch fresh data from njuskalo on demand).

**Claude Code** — already wired via the project `.mcp.json` (run `claude` and
approve `njuskalo-electronics` once; check with `claude mcp list`). Then just
ask, e.g. *"what's the going rate for a used iPhone 15 256GB?"* and Claude calls
`price_stats` / `search_listings`.

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "njuskalo-electronics": {
      "command": "/abs/path/njuskalo-scraper/venv/bin/python",
      "args": ["-m", "electronics.mcp_server"],
      "cwd": "/abs/path/njuskalo-scraper"
    }
  }
}
```

(For claude.ai **web** chat the server would need to be hosted remotely over
HTTP with auth — a later step.)

## Anti-ban behaviour

Per a r/croatia thread documenting njuskalo's defenses, the fetch layer
(`fetch.py`) does:

- **Real browser TLS/header fingerprint** (`curl_cffi impersonate`).
- **Random per-request jitter** — njuskalo flags *fixed* intervals instantly.
- **Ban detection** (ShieldSquare captcha / 403 / 429) → exponential backoff +
  proxy rotation.
- **JS-free fetches** (no automation fingerprint) for everything except the one
  Playwright call that grabs the phone-API bearer token.
- **Randomized category order** each run.

### Proxies

`proxies.py` is **proxy-pluggable**:

- **Direct** (default) — works fine for the index tier from a normal IP.
- **Paid endpoint** — set `NJ_PROXY_ENDPOINT=http://user:pass@gate:port`
  (one rotating endpoint) and everything routes through it. *Recommended once
  you go to volume.*
- **Free pool** — `--use-proxies` fetches & validates public proxies
  (best-effort; ~5% live, unstable). Used **only** for public GETs; token-bearing
  phone requests always go direct (`allow_free=False`) so secrets never traverse
  an untrusted proxy.

The bundled `proxies.txt` (from upstream) is **dead** — all credentials expired.
