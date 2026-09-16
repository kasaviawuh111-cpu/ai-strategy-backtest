#!/bin/zsh
set -euo pipefail

project_dir='/Users/mima0000/Documents/回测'
key_pipe='/private/tmp/ashare-dialogue-deepseek.pipe'
backend_port="${BACKEND_PORT:-8011}"
keychain_service='com.openai.codex.ashare-backtest.deepseek'
search_keychain_service='com.openai.codex.ashare-backtest.tencent-wsa'
keychain_account="${USER:-mima0000}"

cd "$project_dir"
if [[ -n "${CANDIDATE_PROVIDER_API_KEY:-}" ]]; then
  # A local launcher may inject the secret directly into this process.  Keep
  # it in memory only and do not prompt a second time.
  :
elif [[ -p "$key_pipe" ]]; then
  IFS= read -r CANDIDATE_PROVIDER_API_KEY < "$key_pipe"
  rm -f "$key_pipe"
else
  CANDIDATE_PROVIDER_API_KEY=$(/usr/bin/security find-generic-password \
    -a "$keychain_account" -s "$keychain_service" -w 2>/dev/null || true)
  if [[ -z "$CANDIDATE_PROVIDER_API_KEY" ]]; then
    read -s 'CANDIDATE_PROVIDER_API_KEY?请输入 DeepSeek 模型 API Key（按 token 计费，不是搜索 API Key；输入不会显示，并安全保存到 macOS 钥匙串）：'
    print
    if [[ -n "$CANDIDATE_PROVIDER_API_KEY" ]]; then
      printf '%s\n%s\n' "$CANDIDATE_PROVIDER_API_KEY" "$CANDIDATE_PROVIDER_API_KEY" \
        | /usr/bin/security add-generic-password -a "$keychain_account" \
          -s "$keychain_service" -U -w >/dev/null
    fi
  fi
fi

if [[ -z "$CANDIDATE_PROVIDER_API_KEY" ]]; then
  print -u2 '未提供 DeepSeek 模型 API Key，未启动后端。'
  exit 1
fi

local_candidate_key="$CANDIDATE_PROVIDER_API_KEY"
# The deep-planning profile may use a separately injected credential.  For
# local review we default to the same DeepSeek account, but still keep both
# values process-local and restore them after loading the non-secret dotenv.
local_plan_key="${PLAN_DEEP_PROVIDER_API_KEY:-$CANDIDATE_PROVIDER_API_KEY}"
local_search_key="${RESEARCH_PROVIDER_API_KEY:-}"
local_search_mode="${RESEARCH_PROVIDER_MODE:-}"

# Both local launchers share these settings, including backend-only restarts.
local_minute_enabled="${MINUTE_GRID_ENABLED:-}"
local_minute_root="${EXTERNAL_MINUTE_ROOT:-}"
set -a
source .env.local
set +a
export MINUTE_GRID_ENABLED="${local_minute_enabled:-${MINUTE_GRID_ENABLED:-true}}"
export EXTERNAL_MINUTE_ROOT="${local_minute_root:-${EXTERNAL_MINUTE_ROOT:-/Volumes/外地磁盘/stock_1min}}"
unset local_minute_enabled local_minute_root
# An injected provider wins; otherwise retain the configured dotenv provider.
# Loading dotenv must not silently turn a configured paid search into fallback.
local_search_key="${local_search_key:-${RESEARCH_PROVIDER_API_KEY:-}}"
local_search_mode="${local_search_mode:-${RESEARCH_PROVIDER_MODE:-disabled}}"
# Tencent WSA is the preferred dedicated search channel.  Its service key is
# read from the named keychain entry or entered for this process only.
# A generic research key belongs to its declared provider, never relabel it.
if [[ "$local_search_mode" != tencent_web_search ]]; then
  local_search_key=''
fi
if [[ -z "$local_search_key" ]]; then
  local_search_key=$(/usr/bin/security find-generic-password \
    -a "$keychain_account" -s "$search_keychain_service" -w 2>/dev/null || true)
fi
if [[ -z "$local_search_key" ]]; then
  search_key_response=$(/usr/bin/osascript <<'APPLESCRIPT'
try
  set dialogResult to display dialog "请输入腾讯云 Web Search API 的 Service API Key。\n\n它只保留在本次后端进程内存中，不会写入项目、日志或命令行。" default answer "" with hidden answer buttons {"暂不配置", "本次启动"} default button "本次启动" cancel button "暂不配置" with title "A 股回测 · 腾讯云搜索"
  return (button returned of dialogResult) & linefeed & (text returned of dialogResult)
on error number -128
  return ""
end try
APPLESCRIPT
  )
  search_key_choice="${search_key_response%%$'\n'*}"
  if [[ "$search_key_response" == *$'\n'* ]]; then
    local_search_key="${search_key_response#*$'\n'}"
  fi
  unset search_key_response
  unset search_key_choice
fi
if [[ -n "$local_search_key" ]]; then
  local_search_mode='tencent_web_search'
fi
# A value from the local review prompt always wins over a stale dotenv value.
# It is deliberately restored only in memory and is never written to disk.
export CANDIDATE_PROVIDER_API_KEY="$local_candidate_key"
unset local_candidate_key
export MARKET_DATA_PROFILE=eastmoney_skill
export EVENT_DATA_REQUIRED=false
# Only the five named acceptance stocks persist provider histories/indicators.
# All other instruments are fetched through the Skill for each new run.
export PROVIDER_INDICATOR_CACHE_TTL_SECONDS=604800
export CANDIDATE_PROVIDER_MODE=openai_compatible
export CANDIDATE_PROVIDER_NAME=deepseek
export CANDIDATE_PROVIDER_ENDPOINT=https://api.deepseek.com/chat/completions
export CANDIDATE_PROVIDER_MODEL=deepseek-v4-flash
export CANDIDATE_PROVIDER_RESPONSE_MODE=json_object
export CANDIDATE_PROVIDER_THINKING=disabled
unset CANDIDATE_PROVIDER_REASONING_EFFORT
export CANDIDATE_PROVIDER_TIMEOUT_SECONDS=30

# Vague-strategy generation, strategy advice, and post-backtest review all use
# the slower reasoning profile. A failed model call does not become a rule answer.
export PLAN_DEEP_PROVIDER_MODE=openai_compatible
export PLAN_DEEP_PROVIDER_NAME=deepseek
export PLAN_DEEP_PROVIDER_ENDPOINT=https://api.deepseek.com/chat/completions
export PLAN_DEEP_PROVIDER_MODEL=deepseek-v4-pro
export PLAN_DEEP_PROVIDER_API_KEY="$local_plan_key"
export PLAN_DEEP_PROVIDER_RESPONSE_MODE=json_object
export PLAN_DEEP_PROVIDER_THINKING=enabled
export PLAN_DEEP_PROVIDER_REASONING_EFFORT=high
export PLAN_DEEP_PROVIDER_TIMEOUT_SECONDS=180
unset local_plan_key
# Preserve the Tencent key across the non-secret dotenv. Search uses a
# dedicated provider; it does not replace the dialogue model.
unset RESEARCH_PROVIDER_API_KEY RESEARCH_PROVIDER_ENDPOINT RESEARCH_PROVIDER_MODEL
if [[ "$local_search_mode" == tencent_web_search && -n "$local_search_key" ]]; then
  export RESEARCH_PROVIDER_MODE=tencent_web_search
  export RESEARCH_PROVIDER_API_KEY="$local_search_key"
  print '联网搜索主通道：腾讯云（密钥仅保留在进程内存）'
else
  export RESEARCH_PROVIDER_MODE=disabled
  print '联网搜索主通道：腾讯云未配置；本次使用 DuckDuckGo，失败后切换 Bing RSS。'
fi
unset local_search_key local_search_mode

# Configuration presence is not source validation or backtest acceptance.
# Keep dialogue usable while making missing phase-one inputs explicit.
# Retain the locally verified calendar across restarts. Explicit overrides win;
# the expanded source includes the preceding session needed by January replay.
if [[ -z "${MINUTE_MARKET_CALENDAR_PATH:-}" && -f 'var/market-calendar-2024-2026.json' ]]; then
  export MINUTE_MARKET_CALENDAR_PATH='var/market-calendar-2024-2026.json'
  print '本地日历：使用已准备的2024–2026日历，覆盖期初成交容量所需的前一交易日。'
fi
phase_one_calendar="${MINUTE_MARKET_CALENDAR_PATH:-var/market-calendar.json}"
phase_one_actions="${PRICE_PLAN_CORPORATE_ACTION_ROOT:-var/cache/price-plan-corporate-actions}"
if [[ -f "$phase_one_calendar" ]]; then
  print '一期市场日历：文件已配置，内容和覆盖区间将在回测时校验。'
else
  print '一期市场日历：缺少文件；真实股份账本/分钟回测尚不具备完整数据条件。'
fi
if [[ -n "$phase_one_actions" && -d "$phase_one_actions" ]]; then
  print '公司行动：优先核对本地缓存，缺失或覆盖不足时尝试东方财富采集；是否可用以逐股票核验结果为准。'
else
  print '公司行动：缓存尚未建立，回测时尝试采集并核对来源、因子与覆盖区间；不代表已取数成功。'
fi
case "${MINUTE_GRID_ENABLED:-false}" in
  true|True|TRUE|1) print '分钟回测：已启用；每次仍须验证真实分钟快照及日历覆盖。' ;;
  *) print '分钟回测：未启用（MINUTE_GRID_ENABLED=false），服务就绪不表示分钟策略可执行。' ;;
esac
unset phase_one_calendar phase_one_actions

# Reuse the separately permissioned Eastmoney key file when the parent did
# not inject one.  Read it into this process only; never echo or copy it.
if [[ -z "${MX_SAAS_API_KEY:-}" && -r "$HOME/.mx-skills/em_api_key" ]]; then
  IFS= read -r MX_SAAS_API_KEY < "$HOME/.mx-skills/em_api_key"
  export MX_SAAS_API_KEY
fi

# 仅对妙想服务及本地联调地址设置连接策略，不改变系统网络设置。
mx_direct_hosts='ai-saas.eastmoney.com,127.0.0.1,localhost'
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}${mx_direct_hosts}"
export no_proxy="${no_proxy:+${no_proxy},}${mx_direct_hosts}"

# The reviewed Live H5 on 5184 is deliberately pinned to this isolated
# backend port by web/vite.live.guard.ts.
# Acceptance sessions retain in-memory drafts. Auto-reload would discard them.
exec .venv/bin/uvicorn ashare_lab.main:create_app --factory --host 127.0.0.1 --port "$backend_port"
