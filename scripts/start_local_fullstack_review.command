#!/bin/zsh
set -euo pipefail
set +x
umask 077
unsetopt BG_NICE

project_dir='/Users/mima0000/Documents/回测'
backend_port='8011'
frontend_port='5184'
backend_log='/private/tmp/ashare-local-review-backend.log'
frontend_log='/private/tmp/ashare-local-review-frontend.log'
keychain_service='com.openai.codex.ashare-backtest.deepseek'
keychain_account="${USER:-mima0000}"
key_source='environment'

cd "$project_dir"

if [[ ! -x '.venv/bin/uvicorn' ]]; then
  print -u2 '缺少项目 Python 环境：.venv/bin/uvicorn'
  exit 1
fi
if [[ ! -x 'web/node_modules/.bin/vite' ]]; then
  print -u2 '缺少前端依赖：web/node_modules/.bin/vite'
  exit 1
fi

if [[ -z "${CANDIDATE_PROVIDER_API_KEY:-}" ]]; then
  CANDIDATE_PROVIDER_API_KEY=$(/usr/bin/security find-generic-password \
    -a "$keychain_account" -s "$keychain_service" -w 2>/dev/null || true)
  if [[ -n "$CANDIDATE_PROVIDER_API_KEY" ]]; then
    key_source='keychain'
  else
    key_response=$(/usr/bin/osascript <<'APPLESCRIPT'
try
  set dialogResult to display dialog "请输入 DeepSeek 模型 / token API Key（不是搜索 API Key）\n\n用于策略解析和生成。选择‘保存并启动’后，密钥只保存在 macOS 登录钥匙串，不写入项目、.env 或 Git。" default answer "" with hidden answer buttons {"取消", "仅本次", "保存并启动"} default button "保存并启动" cancel button "取消" with title "A 股回测 · 本地联调"
  return (button returned of dialogResult) & linefeed & (text returned of dialogResult)
on error number -128
  return ""
end try
APPLESCRIPT
    )
    key_choice="${key_response%%$'\n'*}"
    if [[ "$key_response" == *$'\n'* ]]; then
      CANDIDATE_PROVIDER_API_KEY="${key_response#*$'\n'}"
    else
      CANDIDATE_PROVIDER_API_KEY=''
    fi
    unset key_response
    if [[ "$key_choice" == '保存并启动' && -n "$CANDIDATE_PROVIDER_API_KEY" ]]; then
      # `security -w` as the final option reads and confirms the value from
      # stdin, so the secret never appears in argv or process listings.
      printf '%s\n%s\n' "$CANDIDATE_PROVIDER_API_KEY" "$CANDIDATE_PROVIDER_API_KEY" \
        | /usr/bin/security add-generic-password -a "$keychain_account" \
          -s "$keychain_service" -U -w >/dev/null
      key_source='saved_keychain'
    else
      key_source='session'
    fi
    unset key_choice
  fi
fi

if [[ -z "$CANDIDATE_PROVIDER_API_KEY" ]]; then
  print -u2 '未输入 DeepSeek API Key，未启动任何服务。'
  exit 1
fi

# Resolve the already-saved search credential BEFORE stopping a working
# backend. A locked keychain must not take the current review session offline.
if [[ "${RESEARCH_PROVIDER_MODE:-}" != 'tencent_web_search' || -z "${RESEARCH_PROVIDER_API_KEY:-}" ]]; then
  local_search_key=$(/usr/bin/security find-generic-password \
    -a "$keychain_account" -s 'com.openai.codex.ashare-backtest.tencent-wsa' -w 2>/dev/null || true)
  if [[ -z "$local_search_key" ]]; then
    print -u2 '未能读取已保存的腾讯搜索凭证。请允许钥匙串读取后重试；现有服务保持运行。'
    exit 1
  fi
  export RESEARCH_PROVIDER_MODE='tencent_web_search'
  export RESEARCH_PROVIDER_API_KEY="$local_search_key"
  unset local_search_key
fi

owned_listeners() {
  local port="$1"
  local expected_root="$2"
  local pid
  local cwd
  local command
  local owned='false'
  for pid in $(/usr/sbin/lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true); do
    # A reload parent and child can both appear in the initial lsof snapshot.
    # Killing the first may remove the second before this lookup.  Keep that
    # normal disappearance from tripping set -e/pipefail.
    cwd=$(/usr/sbin/lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1 || true)
    # macOS lsof escapes non-ASCII paths as literal \\xNN bytes. Decode only
    # that display encoding, then still require the exact project directory.
    cwd=$(printf '%b' "$cwd")
    cwd=$(printf '%b' "$cwd")
    command=$(/bin/ps -p "$pid" -o command= 2>/dev/null || true)
    if ! /bin/kill -0 "$pid" 2>/dev/null; then
      continue
    fi
    owned='false'
    if [[ "$cwd" == "$expected_root" || "$command" == *"$expected_root"* ]]; then
      owned='true'
    fi
    if [[ "$owned" != 'true' ]]; then
      # An exiting launcher can close this socket before its process exits.
      # Do not diagnose that disappearing listener as a foreign project.
      if [[ -z "$(/usr/sbin/lsof -a -p "$pid" -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)" ]]; then
        continue
      fi
      print -u2 "端口 $port 被其他项目占用，未终止该进程：$pid"
      exit 1
    fi
    print -r -- "$pid"
  done
}

stop_review_listeners() {
  local listeners="$1"
  local pid
  for pid in ${(f)listeners}; do
    /bin/kill -0 "$pid" 2>/dev/null || continue
    if ! /bin/kill -TERM "$pid" 2>/dev/null; then
      # The listener may disappear after the ownership check.  A surviving
      # process is a real stop failure and must still fail closed.
      if /bin/kill -0 "$pid" 2>/dev/null; then
        print -u2 "无法终止本项目的本地联调进程：$pid"
        exit 1
      fi
      continue
    fi
    for _ in {1..40}; do
      /bin/kill -0 "$pid" 2>/dev/null || break
      /bin/sleep 0.1
    done
  done
}

# Validate BOTH ports before stopping either. The old launcher may clean up
# both children as soon as one exits; its peer must not be inspected mid-exit.
# A foreign frontend must also leave a healthy backend completely untouched.
backend_listeners=$(owned_listeners "$backend_port" "$project_dir")
frontend_listeners=$(owned_listeners "$frontend_port" "$project_dir/web")
stop_review_listeners "$backend_listeners"
stop_review_listeners "$frontend_listeners"
unset backend_listeners frontend_listeners

: > "$backend_log"
: > "$frontend_log"

(
  export CANDIDATE_PROVIDER_API_KEY
  # The backend launcher owns shared defaults, also for backend-only restarts.
  # The launcher exits after readiness checks.  Ignore its terminal hangup so
  # the reviewed backend (and its in-memory-only key) stays alive afterwards.
  exec /usr/bin/nohup "$project_dir/scripts/start_local_dialogue_review.command"
) >"$backend_log" 2>&1 &
backend_pid=$!

(
  cd "$project_dir/web"
  export VITE_USE_MOCK='false'
  export VITE_API_PROXY_TARGET='http://127.0.0.1:8011'
  export VITE_API_BASE_URL=''
  # Keep Vite alive after this short-lived launcher closes its terminal.
  exec /usr/bin/nohup ./node_modules/.bin/vite --config vite.live.config.ts
) >"$frontend_log" 2>&1 &
frontend_pid=$!

# Remove the parent shell's copy as soon as both child processes have started.
unset CANDIDATE_PROVIDER_API_KEY RESEARCH_PROVIDER_API_KEY

backend_ready='false'
frontend_ready='false'
for _ in {1..300}; do
  if /usr/bin/curl -fsS --max-time 1 "http://127.0.0.1:${backend_port}/api/v1/health" >/dev/null 2>&1; then
    backend_ready='true'
  fi
  if /usr/bin/curl -fsS --max-time 1 "http://127.0.0.1:${frontend_port}/" >/dev/null 2>&1; then
    frontend_ready='true'
  fi
  if [[ "$backend_ready" == 'true' && "$frontend_ready" == 'true' ]]; then
    break
  fi
  /bin/sleep 0.2
done

if [[ "$backend_ready" != 'true' || "$frontend_ready" != 'true' ]]; then
  print -u2 "本地联调启动失败（后端=$backend_ready，前端=$frontend_ready）。"
  print -u2 "后端日志：$backend_log"
  print -u2 "前端日志：$frontend_log"
  /bin/kill -TERM "$backend_pid" "$frontend_pid" 2>/dev/null || true
  exit 1
fi

print "本地联调已启动："
print "  前端：http://127.0.0.1:${frontend_port}/"
print "  后端：http://127.0.0.1:${backend_port}/api/v1/health"
print "  后端日志：$backend_log"
print "  前端日志：$frontend_log"
print "  后端 PID：$backend_pid；前端 PID：$frontend_pid"
case "$key_source" in
  keychain) print 'DeepSeek Key 已从 macOS 钥匙串读取；项目文件中不保存密钥。' ;;
  saved_keychain) print 'DeepSeek Key 已保存到 macOS 钥匙串；以后本地启动无需重复输入。' ;;
  environment) print 'DeepSeek Key 由当前环境注入，未改写钥匙串或项目文件。' ;;
  *) print 'DeepSeek Key 仅用于本次后端进程；未保存到项目文件。' ;;
esac
print '请保持这个启动器运行；关闭它会一并停止本地前后端。'

/usr/bin/open "http://127.0.0.1:${frontend_port}/"

cleanup() {
  /bin/kill -TERM "$backend_pid" "$frontend_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Keep the parent launcher alive.  Some managed terminals reap background
# children when their short-lived parent exits even when nohup is used.
wait "$backend_pid" "$frontend_pid"
