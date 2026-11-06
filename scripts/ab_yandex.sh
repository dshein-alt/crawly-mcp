#!/usr/bin/env bash
# Paired A/B: baseline (HEAD, single reading) vs the settle branch, interleaved.
#
# Yandex's willingness varies over the minute, so runs compared across time are
# confounded. Each round runs the two builds back to back on identical cold
# profiles, in alternating order, and counts what each reported.
set -uo pipefail

BASE=/home/dshein/src/crawly
BRANCH=$BASE/.worktrees/yandex-captcha
N=${1:-6}
QUERIES=("python" "документация Python" "алгоритмы сортировки")

declare -A ok blocked other
for k in base branch; do ok[$k]=0; blocked[$k]=0; other[$k]=0; done

for ((round=1; round<=N; round++)); do
  # Alternate which build goes first so drift cannot favour one of them.
  if (( round % 2 )); then order="base branch"; else order="branch base"; fi
  for build in $order; do
    q=${QUERIES[$(( (round-1) % ${#QUERIES[@]} ))]}
    tree=$BASE; [[ $build == branch ]] && tree=$BRANCH
    out=$(cd "$tree" && rm -rf "/tmp/ab-$build-$round" && \
      CRAWLY_PROFILE_DIR="/tmp/ab-$build-$round" \
      CRAWLY_SEARCH_PROVIDER_ORDER=yandex \
      CRAWLY_SEARCH_MAX_PROVIDERS=1 \
      PLAYWRIGHT_BROWSER_SOURCE=bundled \
      CRAWLY_LOG_LEVEL=ERROR \
      timeout 90 uv run crawly-cli search --provider yandex --context "$q" 2>/dev/null)
    status=$(printf '%s' "$out" | python3 -c 'import json,sys;
try: print(json.load(sys.stdin).get("status","?"))
except Exception: print("parse_error")')
    pk=$(printf '%s' "$out" | python3 -c 'import json,sys;
try:
 d=json.load(sys.stdin); print(d["attempted"][0].get("page_kind","-") if d.get("attempted") else "-")
except Exception: print("-")')
    n=$(printf '%s' "$out" | python3 -c 'import json,sys;
try: print(len(json.load(sys.stdin).get("urls",[])))
except Exception: print(0)')
    case "$status" in
      ok) ok[$build]=$(( ${ok[$build]} + 1 ));;
      blocked) blocked[$build]=$(( ${blocked[$build]} + 1 ));;
      *) other[$build]=$(( ${other[$build]} + 1 ));;
    esac
    printf 'round%-2s %-7s q=%-22s status=%-9s page_kind=%-9s urls=%s\n' \
      "$round" "$build" "$q" "$status" "$pk" "$n"
  done
done

echo
printf '%-8s %-6s %-9s %s\n' build ok blocked "other"
for k in base branch; do printf '%-8s %-6s %-9s %s\n' "$k" "${ok[$k]}" "${blocked[$k]}" "${other[$k]}"; done
