#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
unset PLAYWRIGHT_BROWSERS_PATH

STAMP=$(date +%Y%m%d_%H%M%S)
LOG="data/batch_run_${STAMP}.log"
TSV="data/scrape_results_${STAMP}_resume.tsv"

sites=(
  ggzy_安徽省 ggzy_四川省 ccgp_湖北省 ggzy_湖北省 ggzy_山东省
  ccgp_河南省 ggzy_河南省 ecp_sgcc csg_bidding sinopec_bidding
  crec_bidding chinamobile_bidding ccgp_四川省
)

echo "started_at=$(date -Iseconds)" | tee "$LOG"
printf 'site_id\tstatus\tscraped\tnew\tseconds\terror\n' > "$TSV"

for sid in "${sites[@]}"; do
  echo "========== ${sid} ==========" | tee -a "$LOG"
  started=$(date +%s)
  if bash scripts/run_with_local_chrome.sh .venv/bin/python scripts/run_once.py "$sid" --max-items 10 --intelligent 2>&1 | tee -a "$LOG"; then
    ec=0; status=ok
  else
    ec=1; status=fail
  fi
  elapsed=$(( $(date +%s) - started ))
  scraped=$(grep -oE '抓取 [0-9]+ 条' "$LOG" | tail -1 | grep -oE '[0-9]+' || echo "?")
  new=$(grep -oE '新增 [0-9]+ 条' "$LOG" | tail -1 | grep -oE '[0-9]+' || echo "?")
  echo "site=${sid} ec=${ec} scraped=${scraped} new=${new} elapsed=${elapsed}s" | tee -a "$LOG"
  printf '%s\t%s\t%s\t%s\t%s\t\n' "$sid" "$status" "$scraped" "$new" "$elapsed" >> "$TSV"
  sleep 2
done

echo "finished_at=$(date -Iseconds)" | tee -a "$LOG"
echo "RESULTS_FILE=${TSV}" | tee -a "$LOG"
