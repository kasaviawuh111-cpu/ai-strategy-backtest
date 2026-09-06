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
  CANDIDATE_PROVIDER_API_KEY=$(/usr/bin/osascript <<'APPLESCRIPT'
try
  display dialog "请输入 DeepSeek API Key\n\n仅用于本次本地联调，输入内容不会显示，也不会写入文件。" default answer "" with hidden answer buttons {"取消", "启动本地联调"} default button "启动本地联调" cancel button "取消" with title "A 股回测 · 本地联调"
  return text returned of result
on error number -128
  return ""
end try
APPLESCRIPT
  )
fi

if [[ -z "$CANDIDATE_PROVIDER_API_KEY" ]]; then
  print -u2 '未输入 DeepSeek API Key，未启动任何服务。'
  exit 1
fi

stop_owned_listener() {
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
      print -u2 "端口 $port 被其他项目占用，未终止该进程：$pid"
      exit 1
    fi
    if ! /bin/kill -TERM "$pid" 2>/dev/null; then
      # The listener may disappear after the ownership check.  A surviving
      # process is a real stop failure and must still fail closed.
      if /bin/kill -0 "$pid" 2>/dev/null; then
        print -u2 "无法终止本项目在端口 $port 的进程：$pid"
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

stop_owned_listener "$backend_port" "$project_dir"
stop_owned_listener "$frontend_port" "$project_dir/web"

: > "$backend_log"
: > "$frontend_log"

(
  export CANDIDATE_PROVIDER_API_KEY
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
print 'DeepSeek Key 仅存在于本次后端进程内存中；独立网页搜索不需要其他 Key。'
print '请保持这个启动器运行；关闭它会一并停止本地前后端。'

/usr/bin/open "http://127.0.0.1:${frontend_port}/"

cleanup() {
  /bin/kill -TERM "$backend_pid" "$frontend_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Keep the parent launcher alive.  Some managed terminals reap background
# children when their short-lived parent exits even when nohup is used.
wait "$backend_pid" "$frontend_pid"
