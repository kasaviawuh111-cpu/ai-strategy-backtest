#!/bin/zsh
set -u

# Run this while the VPN is temporarily disabled.  It repeatedly exercises the
# already-running local service through the same public HTTP contracts used by
# the H5, and saves each round for later inspection.  It never reads or prints
# provider credentials; the local backend owns them.

project_dir='/Users/mima0000/Documents/回测'
local_api_base="${LOCAL_API_BASE:-http://127.0.0.1:8011}"
duration_minutes="${1:-60}"
interval_seconds="${2:-900}"
output_root='/private/tmp/ashare-mx-direct-soak'
run_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="$output_root/$run_stamp"
summary_file="$run_dir/summary.log"

if ! [[ "$duration_minutes" =~ '^[0-9]+$' ]] || (( duration_minutes < 1 )); then
  print -u2 '用法：run_mx_direct_soak.command [运行分钟数] [每轮间隔秒数]'
  exit 2
fi
if ! [[ "$interval_seconds" =~ '^[0-9]+$' ]] || (( interval_seconds < 10 )); then
  print -u2 '每轮间隔至少为 10 秒。'
  exit 2
fi

mkdir -p "$run_dir"
chmod 700 "$output_root" "$run_dir"
cd "$project_dir"

if [[ ! -f 'scripts/probe_mx_live_interfaces.py' ]]; then
  print -u2 '缺少 scripts/probe_mx_live_interfaces.py。'
  exit 2
fi

if ! /usr/bin/curl -fsS "$local_api_base/api/v1/openapi.json" >/dev/null 2>&1; then
  print -u2 "本地后端未运行：$local_api_base"
  print -u2 '请先启动 scripts/start_local_dialogue_review.command，再运行本脚本。'
  exit 2
fi

start_epoch="$(date +%s)"
end_epoch="$(( start_epoch + duration_minutes * 60 ))"
round=0
passed=0
failed=0

{
  print "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  print "local_api_base=$local_api_base"
  print "duration_minutes=$duration_minutes"
  print "interval_seconds=$interval_seconds"
} >"$summary_file"
chmod 600 "$summary_file"

while (( $(date +%s) < end_epoch )); do
  round=$(( round + 1 ))
  round_log="$run_dir/round-$(printf '%03d' "$round").log"
  print "第 $round 轮开始：$(date '+%Y-%m-%d %H:%M:%S')"

  /usr/bin/python3 scripts/probe_mx_live_interfaces.py \
    --skip-provider \
    --local-api-base "$local_api_base" \
    --require-local \
    --timeout 45 >"$round_log" 2>&1
  status=$?
  chmod 600 "$round_log"

  if (( status == 0 )); then
    passed=$(( passed + 1 ))
    state='PASS'
  else
    failed=$(( failed + 1 ))
    state='FAIL'
  fi
  print "round=$round state=$state exit_code=$status log=$round_log" | tee -a "$summary_file"

  remaining="$(( end_epoch - $(date +%s) ))"
  if (( remaining <= 0 )); then
    break
  fi
  wait_seconds="$interval_seconds"
  if (( remaining < wait_seconds )); then
    wait_seconds="$remaining"
  fi
  sleep "$wait_seconds"
done

{
  print "finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  print "rounds=$round"
  print "passed=$passed"
  print "failed=$failed"
} | tee -a "$summary_file"

latest_file="$output_root/LATEST"
print "$run_dir" >"$latest_file"
chmod 600 "$latest_file"

print "长时间检查结束。汇总：$summary_file"
(( failed == 0 ))
