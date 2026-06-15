#!/bin/bash
# Batch-runs every URL in source_data/urls.txt through the live site
# (Vercel proxy → mac mini). Sequential — mac server has a single-run lock.
# Appends one result line per video to BATCH_RESULTS so it can be tail-watched.

set -uo pipefail
REPO="/c/Users/nclaw/acq_long_to_short_content"
URLS="$REPO/source_data/urls.txt"
BATCH_RESULTS="$REPO/homebox/batch_45_results.md"
SITE="https://acq-clipper.vercel.app"
BATCH_ID="batch_$(date +%H%M%S)"

mapfile -t URL_LIST < "$URLS"
TOTAL=${#URL_LIST[@]}

{
  echo "# Batch run — $BATCH_ID ($(date +%H:%M:%S))"
  echo ""
  echo "$TOTAL videos. Live site: $SITE. Each row updates as the run completes."
  echo ""
  echo "| # | Video ID | Status | Wall (s) | Cost (\$) | Clip |"
  echo "|---|----------|--------|----------|----------|------|"
} > "$BATCH_RESULTS"

for i in "${!URL_LIST[@]}"; do
  URL="${URL_LIST[$i]}"
  VID=$(echo "$URL" | sed -E 's|.*v=||; s|&.*||')
  JOB="${BATCH_ID}_$(printf '%02d' "$i")_${VID}"
  START=$(date +%s)

  KICK=$(curl -s -X POST "$SITE/api/process" \
    -H "content-type: application/json" \
    -d "{\"url\":\"$URL\",\"job_id\":\"$JOB\"}" \
    -m 25)
  if ! echo "$KICK" | grep -q "queued"; then
    echo "| $((i+1)) | $VID | KICK_FAIL | - | - | \`$(echo "$KICK" | head -c 120)\` |" >> "$BATCH_RESULTS"
    sleep 2
    continue
  fi

  STATUS=""; BLOB=""; COST=""; WALL=""
  for attempt in $(seq 1 30); do
    sleep 15
    S=$(curl -s "$SITE/api/status?job_id=$JOB" -m 15)
    if echo "$S" | grep -q '"status": *"done"'; then
      STATUS="done"
      BLOB=$(echo "$S" | sed -E 's/.*"blob_url": *"([^"]+)".*/\1/')
      COST=$(echo "$S" | sed -E 's/.*"total_cost_usd": *([0-9.]+).*/\1/')
      break
    fi
    if echo "$S" | grep -q '"status": *"failed"'; then
      STATUS="failed"
      BLOB=$(echo "$S" | sed -E 's/.*"error": *"([^"]+)".*/\1/' | head -c 80)
      break
    fi
  done
  WALL=$(( $(date +%s) - START ))
  if [ -z "$STATUS" ]; then STATUS="timeout"; fi

  if [ "$STATUS" = "done" ]; then
    echo "| $((i+1)) | $VID | ✓ done | $WALL | $COST | [mp4]($BLOB) |" >> "$BATCH_RESULTS"
  else
    echo "| $((i+1)) | $VID | $STATUS | $WALL | $COST | $BLOB |" >> "$BATCH_RESULTS"
  fi
done

echo "" >> "$BATCH_RESULTS"
echo "_Done — $(date +%H:%M:%S)_" >> "$BATCH_RESULTS"
