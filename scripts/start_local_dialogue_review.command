#!/bin/zsh
set -euo pipefail

project_dir='/Users/mima0000/Documents/回测'
key_pipe='/private/tmp/ashare-dialogue-deepseek.pipe'

cd "$project_dir"
if [[ -n "${CANDIDATE_PROVIDER_API_KEY:-}" ]]; then
  # A local launcher may inject the secret directly into this process.  Keep
  # it in memory only and do not prompt a second time.
  :
elif [[ -p "$key_pipe" ]]; then
  IFS= read -r CANDIDATE_PROVIDER_API_KEY < "$key_pipe"
  rm -f "$key_pipe"
else
  read -s 'CANDIDATE_PROVIDER_API_KEY?请输入 DeepSeek API Key（输入不会显示）：'
  print
fi

local_candidate_key="$CANDIDATE_PROVIDER_API_KEY"
# The deep-planning profile may use a separately injected credential.  For
# local review we default to the same DeepSeek account, but still keep both
# values process-local and restore them after loading the non-secret dotenv.
local_plan_key="${PLAN_DEEP_PROVIDER_API_KEY:-$CANDIDATE_PROVIDER_API_KEY}"

set -a
source .env.local
set +a
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
# The local skill app uses independent public search, then DeepSeek analysis.
# Do not retain an unused Ark/search credential or build a second model provider.
export RESEARCH_PROVIDER_MODE=disabled
unset RESEARCH_PROVIDER_API_KEY RESEARCH_PROVIDER_ENDPOINT RESEARCH_PROVIDER_MODEL

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
exec .venv/bin/uvicorn ashare_lab.main:create_app --factory --host 127.0.0.1 --port 8011
