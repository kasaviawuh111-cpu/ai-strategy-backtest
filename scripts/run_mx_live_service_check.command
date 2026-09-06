#!/bin/zsh
set -euo pipefail
unsetopt bg_nice

# One command for a real provider -> local FastAPI integration check.  It uses
# the local skill credential file and never prints or copies the credential.
project_dir='/Users/mima0000/Documents/回测'
listen_port='8016'
output_root='/private/tmp/ashare-mx-live-probe'
run_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
service_log="$output_root/service-$run_stamp.log"

mkdir -p "$output_root"
chmod 700 "$output_root"
cd "$project_dir"

if [[ ! -x '.venv/bin/uvicorn' ]]; then
  print -u2 '未找到项目 .venv/bin/uvicorn，本地后端无法启动。'
  exit 2
fi

set -a
[[ -f '.env.local' ]] && source '.env.local'
set +a
export APP_ENV=local
export APP_HOST=127.0.0.1
export APP_PORT="$listen_port"
export MARKET_DATA_PROFILE=on_demand_snapshot
export ON_DEMAND_DAILY_SOURCE=choice_then_eastmoney
export ON_DEMAND_REFRESH_EACH_SUBMISSION=true
export EVENT_DATA_REQUIRED=false
export SESSION_REFERENCE_MODE=research_300059
export QUEUE_BACKEND=thread
export INITIALIZE_SCHEMA=true

# MX is a mainland service. Keep this exact host outside environment HTTP
# proxies while leaving DeepSeek and all unrelated traffic unchanged. A VPN
# TUN/DNS rule can still override this; in that case run this probe once with
# the VPN disabled and inspect the saved result after reconnecting.
mx_direct_hosts='ai-saas.eastmoney.com,127.0.0.1,localhost'
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}${mx_direct_hosts}"
export no_proxy="${no_proxy:+${no_proxy},}${mx_direct_hosts}"

cleanup() {
  if [[ -n "${service_pid:-}" ]] && kill -0 "$service_pid" 2>/dev/null; then
    kill "$service_pid" 2>/dev/null || true
    wait "$service_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

.venv/bin/uvicorn ashare_lab.main:create_app \
  --factory --host 127.0.0.1 --port "$listen_port" >"$service_log" 2>&1 &
service_pid=$!

ready=false
for _ in {1..60}; do
  if /usr/bin/curl -fsS "http://127.0.0.1:$listen_port/api/v1/openapi.json" >/dev/null 2>&1; then
    ready=true
    break
  fi
  if ! kill -0 "$service_pid" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

if [[ "$ready" != true ]]; then
  print -u2 "本地后端未启动成功。日志已保存：$service_log"
  tail -n 30 "$service_log" >&2 || true
  exit 2
fi

/usr/bin/python3 scripts/probe_mx_live_interfaces.py \
  --local-api-base "http://127.0.0.1:$listen_port" \
  --require-local \
  --timeout 45

print "全链路检查完成。后端日志：$service_log"
