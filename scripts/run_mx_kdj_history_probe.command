#!/bin/zsh
set -euo pipefail

# One-command real-data probe for the KDJ acceptance strategy.  The provider
# must return K/D/J itself: this script deliberately does not request OHLCV and
# does not calculate KDJ locally.  Run it while the Eastmoney MX endpoint is
# directly reachable.  The provider key is read by the installed
# mx-finance-data Skill from ~/.mx-skills/em_api_key; this script never prints
# or copies it.

project_dir='/Users/mima0000/Documents/回测'
skill_script='/Users/mima0000/.codex/skills/mx-finance-data/scripts/get_data.py'
python_bin="$project_dir/.venv/bin/python"
bundled_site_packages='/Users/mima0000/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/lib/python3.12/site-packages'
run_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="/private/tmp/ashare-mx-kdj-history/$run_stamp"

if [[ ! -x "$python_bin" ]]; then
  print -u2 "未找到项目 Python：$python_bin"
  exit 2
fi
if [[ ! -f "$skill_script" ]]; then
  print -u2 "未找到 mx-finance-data Skill：$skill_script"
  exit 2
fi
if [[ ! -s "$HOME/.mx-skills/em_api_key" && -z "${EM_API_KEY:-}" ]]; then
  print -u2 '未找到东方财富 MX 凭证。'
  exit 2
fi

mkdir -p "$run_dir"
chmod 700 "${run_dir:h}" "$run_dir"
cd "$run_dir"

query='查询同花顺(300033.SZ)2025-09-03至2026-09-03每个交易日的KDJ指标K值、D值、J值'
indicators='2025-09-03至2026-09-03逐交易日KDJ指标K值、D值、J值'

if ! NO_PROXY="${NO_PROXY:+${NO_PROXY},}ai-saas.eastmoney.com" \
  no_proxy="${no_proxy:+${no_proxy},}ai-saas.eastmoney.com" \
  PYTHONPATH="${PYTHONPATH:+${PYTHONPATH}:}$bundled_site_packages" \
  "$python_bin" "$skill_script" \
    --query "$query" \
    --indicators "$indicators" \
    >"$run_dir/probe.log" 2>&1; then
  print -u2 "KDJ 历史指标取数失败，请查看：$run_dir/probe.log"
  exit 1
fi

if ! find "$run_dir" -type f \( -name '*.xlsx' -o -name '*.md' \) -print -quit | grep -q .; then
  print -u2 "接口未产生可校验的 KDJ 结果文件：$run_dir"
  exit 1
fi

find "$run_dir" -type f -exec chmod 600 {} +
print "$run_dir" >'/private/tmp/ashare-mx-kdj-history/LATEST'
chmod 600 '/private/tmp/ashare-mx-kdj-history/LATEST'

print "KDJ 真实历史取数完成：$run_dir"
print '请重新打开 VPN 后告诉我“跑完了”，我会检查字段和覆盖并继续接回测。'
