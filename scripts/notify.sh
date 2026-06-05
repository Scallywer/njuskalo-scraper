#!/usr/bin/env bash
#
# Deal-notification flush (the 8am job).
#
# Sends any deals the nightly crawl queued (still active) to Telegram, then
# marks them notified. Kept separate from the 3am crawl so alerts never fire
# in the middle of the night.
#
# Schedule via systemd timer at 08:00 (see njuskalo-notify.timer).
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
if [ -f .env ]; then set -a; # shellcheck disable=SC1091
  source .env; set +a; fi
# shellcheck disable=SC1091
source venv/bin/activate

STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
echo "===== flush run $STAMP =====" >> data/nightly.log
python -m electronics.watch --flush >> data/nightly.log 2>&1
