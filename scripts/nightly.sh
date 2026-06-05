#!/usr/bin/env bash
#
# Nightly njuskalo monitor.
#
# Crawls the watched categories (apple-iphone, xbox-series-s/x), refreshes their
# listings + prices in the DB, then evaluates the watchlist and writes
# data/watch_report.md with anything NEW or price-dropped since last run.
#
# Targeted (not a full-catalog crawl) so it's fast and light on njuskalo.
# Schedule via cron, e.g.:  0 3 * * *  /path/to/scripts/nightly.sh
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
# Load secrets (Telegram creds) if present — gitignored.
if [ -f .env ]; then set -a; # shellcheck disable=SC1091
  source .env; set +a; fi
# shellcheck disable=SC1091
source venv/bin/activate

STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
echo "===== nightly run $STAMP =====" >> data/nightly.log

# watch.py crawls the watched categories (newest-first), evaluates, queues
# deals. Capped at 12 pages: njuskalo sorts newest-first, so newly-posted deals
# are always on the early pages -- a full scan (apple-iphone alone is ~130 pages
# / 12 min) is unnecessary nightly. The "retire only on a complete crawl" rule
# means this cap never wrongly marks deeper listings as sold.
python -m electronics.watch --max-pages 12 >> data/nightly.log 2>&1

echo "[done $STAMP] report -> data/watch_report.md" >> data/nightly.log
