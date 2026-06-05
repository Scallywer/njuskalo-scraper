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

# watch.py crawls its own watched categories, then evaluates + writes the report.
python -m electronics.watch --max-pages 10 >> data/nightly.log 2>&1

echo "[done $STAMP] report -> data/watch_report.md" >> data/nightly.log
