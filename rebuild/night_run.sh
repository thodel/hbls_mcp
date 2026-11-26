#!/bin/bash
# The HBLS rebuild, by night only (Tobias, 26.11.2026: GPUStack qwen 23:00-07:00, 4 workers).
# Started by cron at 23:00 on idhefix; stops itself at 06:55. Every step is resumable, so each night
# carries on where the last one stopped:
#   1. page OCR of all 5777 pages (rebuild.ocr, columns at 300 dpi), then the failed pages once more
#   2. stoplist from the abbreviation pages, cutting into articles (rebuild.abbrev, rebuild.segment)
#   3. qwen structure of the family articles (rebuild.structure)
#   4. hbls.staging.db (rebuild.build) and the gold-standard figures (rebuild.evaluate)
# Embeddings and the switch of the live database are NOT done here.
#   crontab: 0 23 * * * /bin/bash $HOME/hbls_build/code/rebuild/night_run.sh
set -uo pipefail
B=$HOME/hbls_build
cd "$B/code" || exit 1
exec 9>"$B/night_run.lock"
flock -n 9 || { echo "$(date -Is) already running" >> "$B/night_run.log"; exit 0; }
exec >> "$B/night_run.log" 2>&1
set -a; . /home/tobias/mcp/hbls_mcp/.env; set +a
PY=$B/.venv/bin/python
WORKERS=${HBLS_NIGHT_WORKERS:-4}

left() {  # seconds until 06:55 today (or tomorrow, if started before midnight)
  local end now
  end=$(date -d "06:55" +%s); now=$(date +%s)
  [ "$end" -le "$now" ] && end=$(date -d "tomorrow 06:55" +%s)
  echo $((end - now))
}
pending() { $PY - "$1" <<'PY'
import sqlite3, sys, os
c = sqlite3.connect(os.path.expanduser("~/hbls_build/pages.db"))
q = {"ocr": "SELECT COUNT(*) FROM pages WHERE mode='columns' AND status!='ok' AND attempts<3",
     "registered": "SELECT COUNT(*) FROM pages WHERE mode='columns'"}[sys.argv[1]]
print(c.execute(q).fetchone()[0])
PY
}

h=$(date +%-H)
if [ "$h" -ge 7 ] && [ "$h" -lt 23 ] && [ -z "${HBLS_NIGHT_FORCE:-}" ]; then
  echo "$(date -Is) daytime: GPUStack qwen only 23-07, not starting"; exit 0
fi
echo "=== $(date -Is) night run, $(left) s left"
if [ "$(pending registered)" -lt 5777 ] || [ "$(pending ocr)" -gt 0 ]; then
  timeout -k 30 "$(left)" $PY -m rebuild.ocr run --all --mode columns --workers "$WORKERS"
  [ "$(left)" -gt 1800 ] && timeout -k 30 "$(left)" $PY -m rebuild.ocr run --all --mode columns --workers "$WORKERS" --retry
  $PY -m rebuild.ocr status
  if [ "$(pending ocr)" -gt 0 ]; then echo "$(date -Is) OCR not finished, next night"; exit 0; fi
fi
if [ ! -s "$B/articles.jsonl" ] || [ "$B/pages.db" -nt "$B/articles.jsonl" ]; then
  $PY -m rebuild.abbrev
  $PY -m rebuild.segment --out "$B/articles.jsonl.tmp" && mv "$B/articles.jsonl.tmp" "$B/articles.jsonl"
fi
[ "$(left)" -gt 600 ] || exit 0
timeout -k 30 "$(left)" $PY -m rebuild.structure --articles "$B/articles.jsonl" --workers "$WORKERS"
todo=$($PY - <<'PY'
import json, os, sqlite3
from rebuild.structure import article_id, is_family
done = {r[0] for r in sqlite3.connect(os.path.expanduser("~/hbls_build/structure.db")).execute(
    "SELECT aid FROM structure WHERE status='ok'")}
print(sum(1 for ln in open(os.path.expanduser("~/hbls_build/articles.jsonl"))
          if is_family(a := json.loads(ln)) and article_id(a) not in done))
PY
)
echo "$(date -Is) family articles left: $todo"
if [ "$todo" -gt 20 ]; then exit 0; fi
if [ ! -s "$B/hbls.staging.db" ] || [ "$B/structure.db" -nt "$B/hbls.staging.db" ]; then
  V=${HBLS_DATA_VERSION:-$(date +%Y.%m)-ocr1}
  $PY -m rebuild.build --articles "$B/articles.jsonl" --out "$B/hbls.staging.db.tmp" --version "$V" \
    && mv "$B/hbls.staging.db.tmp" "$B/hbls.staging.db"
  $PY -m rebuild.evaluate --system db --db "$B/hbls.staging.db" --json "$B/eval_staging.json"
  echo "$(date -Is) STAGING BUILT $V"
fi
