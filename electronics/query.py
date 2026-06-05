"""
Query CLI for the electronics database -- the "ask me about used things" surface.

This is what a future API would wrap. It already answers the common questions:
search by family / category / condition / price / spec, market price stats, and
per-listing price history.

Examples:
  python -m electronics.query search --family mobiteli --condition rabljeno --max-price 400
  python -m electronics.query search --category samsung-mobiteli --spec Memorija="256 GB"
  python -m electronics.query stats --family mobiteli
  python -m electronics.query history 47523086
  python -m electronics.query show 47523086
"""

import json
import argparse

from . import db, service


def _print_rows(rows):
    if not rows:
        print("(no results)")
        return
    for r in rows:
        d = dict(r)
        price = f"{d['price_amount']:.0f} {d['price_currency']}" if d.get("price_amount") else "n/a"
        cond = f" [{d['condition']}]" if d.get("condition") else ""
        print(f"  {d['ad_id']:>9}  {price:>11}{cond}  {(d.get('title') or '')[:50]}")
        if d.get("url"):
            print(f"             {d['url']}")


def cmd_search(conn, args):
    specs = dict(s.split("=", 1) for s in (args.spec or []) if "=" in s)
    rows = service.search(conn, family=args.family, category=args.category,
                          condition=args.condition, min_price=args.min_price,
                          max_price=args.max_price, text=args.text, specs=specs,
                          sort=args.sort, include_inactive=args.include_inactive,
                          limit=args.limit)
    _print_rows(rows)


def cmd_stats(conn, args):
    s = service.stats(conn, family=args.family, category=args.category)
    print(f"  count={s['count']}  avg={s['avg']}  min={s['min']}  max={s['max']}"
          f"{'  median=%.0f' % s['median'] if s.get('median') else ''}")


def cmd_history(conn, args):
    rows = service.history(conn, args.ad_id)
    if not rows:
        print("(no price history)")
        return
    for r in rows:
        print(f"  {r['observed_at']}  {r['price_amount']:.0f} {r['price_currency']}")


def cmd_show(conn, args):
    d = service.get(conn, args.ad_id)
    if not d:
        print("(not found)")
        return
    for k, v in d.items():
        if k in ("attributes", "phones") and v:
            v = json.dumps(v, ensure_ascii=False)
        if v not in (None, ""):
            print(f"  {k:14} {v}")


def main():
    ap = argparse.ArgumentParser(description="Query the electronics database")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search listings")
    s.add_argument("--family"); s.add_argument("--category")
    s.add_argument("--condition", help="e.g. 'rabljeno' or 'novo'")
    s.add_argument("--min-price", type=float); s.add_argument("--max-price", type=float)
    s.add_argument("--text", help="substring in title")
    s.add_argument("--spec", action="append", help="attribute filter key=value (repeatable)")
    s.add_argument("--sort", choices=["price", "recent"], default="price")
    s.add_argument("--include-inactive", action="store_true")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_search)

    st = sub.add_parser("stats", help="price stats for a family/category")
    st.add_argument("--family"); st.add_argument("--category")
    st.set_defaults(func=cmd_stats)

    h = sub.add_parser("history", help="price history for an ad")
    h.add_argument("ad_id"); h.set_defaults(func=cmd_history)

    sh = sub.add_parser("show", help="full record for an ad")
    sh.add_argument("ad_id"); sh.set_defaults(func=cmd_show)

    args = ap.parse_args()
    conn = db.get_conn()
    db.init_db(conn)
    args.func(conn, args)
    conn.close()


if __name__ == "__main__":
    main()
