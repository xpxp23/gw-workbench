#!/usr/bin/env bash
# ==============================================================================
# 公文与材料智能工作台 (GW-Workbench v3.3.0) — 银河麒麟 Kylin V10 / Linux 启动与管理工具
# 用法：
#   ./start.sh              # 前台启动服务（默认端口 8090，可通过 PORT 覆盖）
#   ./start.sh --bg         # 后台常驻运行（日志追加输出至 data/server.log，超 10MB 自动轮转）
#   ./start.sh --stop       # 优雅停止后台运行的服务
#   ./start.sh --desktop    # 在桌面自动生成一键打开工作台的快捷方式图标
#   ./start.sh --help       # 查看帮助信息
# ==============================================================================

set -e
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
PORT="${PORT:-8090}"
PID_FILE="$APP_DIR/data/server.pid"
LOG_FILE="$APP_DIR/data/server.log"
MAX_LOG_BYTES=$((10 * 1024 * 1024))

mkdir -p "$APP_DIR/data" "$APP_DIR/workspace_files"

usage() {
  cat <<'EOF'
用法：
  ./start.sh              前台启动服务（默认端口 8090，可通过环境变量 PORT 覆盖）
  ./start.sh --bg         后台常驻运行（自动探活，日志追加至 data/server.log）
  ./start.sh --stop       优雅停止后台运行的工作台服务
  ./start.sh --desktop    在桌面生成一键启动并打开工作台的 .desktop 快捷方式
  ./start.sh --help       显示本帮助信息
EOF
}

check_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ 未检测到 python3，请先安装 Python 3.8 或以上版本。" >&2
    exit 1
  fi
  if ! python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >/dev/null 2>&1; then
    echo "❌ 当前 python3 版本过低，请升级至 Python 3.8 或以上版本。" >&2
    exit 1
  fi
}

# 核验给定 PID 是否确为工作台 server.py 进程，防止误杀无关进程
is_workbench_pid() {
  local pid="$1"
  local cmdline=""

  if [ -z "$pid" ] || ! [[ "$pid" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi

  if [ -r "/proc/$pid/cmdline" ]; then
    cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  fi
  if [ -z "$cmdline" ]; then
    cmdline="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  fi

  if [[ "$cmdline" == *"server.py"* ]]; then
    return 0
  fi
  return 1
}

# 读取并验证 PID_FILE；若为空或为僵尸/无关 PID，自动清理并返回 1
get_running_pid() {
  if [ ! -f "$PID_FILE" ]; then
    return 1
  fi
  local pid=""
  pid="$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null || true)"
  if [ -z "$pid" ]; then
    rm -f "$PID_FILE"
    return 1
  fi
  if is_workbench_pid "$pid"; then
    echo "$pid"
    return 0
  fi
  # 僵尸 PID 或已被其他无关进程复用，自动清理 PID_FILE
  rm -f "$PID_FILE"
  return 1
}

# 探测本机端口是否已开始监听
is_port_open() {
  python3 - "$PORT" <<'PY' >/dev/null 2>&1
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.3)
try:
    rc = s.connect_ex(("127.0.0.1", port))
finally:
    s.close()
raise SystemExit(0 if rc == 0 else 1)
PY
}

# 日志超过 10MB 时自动轮转备份
rotate_log_if_needed() {
  if [ -f "$LOG_FILE" ]; then
    local size=0
    size="$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)"
    size="$(echo "$size" | tr -d '[:space:]')"
    if [[ "$size" =~ ^[0-9]+$ ]] && [ "$size" -ge "$MAX_LOG_BYTES" ]; then
      mv -f "$LOG_FILE" "${LOG_FILE}.1"
    fi
  fi
}

case "${1:-}" in
  --help|-h)
    usage
    exit 0
    ;;

  --stop)
    pid=""
    if pid="$(get_running_pid)"; then
      kill "$pid" 2>/dev/null || true
      # 轮询等待最多 5 秒优雅退出
      waited=0
      while [ "$waited" -lt 25 ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
          break
        fi
        sleep 0.2
        waited=$((waited + 1))
      done
      # 超时仍存活则强制 SIGKILL
      if kill -0 "$pid" 2>/dev/null && is_workbench_pid "$pid"; then
        kill -9 "$pid" 2>/dev/null || true
        sleep 0.2
      fi
      rm -f "$PID_FILE"
      echo "✅ 已停止后台运行的工作台服务 (PID: $pid)。"
    else
      echo "ℹ️ 未检测到正在后台运行的工作台服务。"
    fi
    exit 0
    ;;

  --desktop)
    DESKTOP_DIR="$HOME/桌面"
    [ ! -d "$DESKTOP_DIR" ] && DESKTOP_DIR="$HOME/Desktop"
    mkdir -p "$DESKTOP_DIR"
    SHORTCUT="$DESKTOP_DIR/公文与材料智能工作台.desktop"
    # 安全转义 APP_DIR 中的单引号与反斜杠，避免路径含空格或特殊字符破坏 Exec 命令
    SAFE_APP_DIR="${APP_DIR//\'/\'\\\'\'}"
    SAFE_PORT="${PORT//[^0-9]/}"
    [ -z "$SAFE_PORT" ] && SAFE_PORT="8090"
    cat > "$SHORTCUT" <<EOF
[Desktop Entry]
Type=Application
Name=公文与材料智能工作台
Comment=启动并打开公文与材料智能工作台 (GW-Workbench)
Exec=bash -c 'cd '\''$SAFE_APP_DIR'\'' && PORT=$SAFE_PORT ./start.sh --bg && xdg-open "http://127.0.0.1:$SAFE_PORT"'
Icon=folder-documents
Terminal=false
Categories=Office;Utility;
EOF
    chmod +x "$SHORTCUT"
    echo "✅ 已在桌面生成快捷方式：$SHORTCUT"
    exit 0
    ;;

  --bg)
    pid=""
    if pid="$(get_running_pid)"; then
      echo "ℹ️ 工作台已在后台运行中 (PID: $pid)，直接访问 http://127.0.0.1:$PORT"
      exit 0
    fi
    check_python
    rotate_log_if_needed
    nohup python3 "$APP_DIR/server.py" "$PORT" >> "$LOG_FILE" 2>&1 &
    new_pid=$!
    echo "$new_pid" > "$PID_FILE"

    # 启动后轮询探测端口（最多 5 秒），确认服务真正存活
    ready=0
    waited=0
    while [ "$waited" -lt 25 ]; do
      if ! kill -0 "$new_pid" 2>/dev/null; then
        break
      fi
      if is_port_open; then
        ready=1
        break
      fi
      sleep 0.2
      waited=$((waited + 1))
    done

    if [ "$ready" -eq 1 ]; then
      echo "✅ 工作台已在后台启动并就绪 (PID: $new_pid)"
      echo "👉 本机访问地址：http://127.0.0.1:$PORT"
      echo "📄 日志文件位置：$LOG_FILE"
      exit 0
    else
      if ! kill -0 "$new_pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        echo "❌ 工作台启动失败，进程已提前退出，请查看日志：$LOG_FILE" >&2
      else
        echo "❌ 工作台启动超时（端口 $PORT 未在 5 秒内就绪），请检查日志：$LOG_FILE" >&2
      fi
      exit 1
    fi
    ;;

  "")
    check_python
    exec python3 "$APP_DIR/server.py" "$PORT"
    ;;

  *)
    echo "❌ 未知参数：$1" >&2
    usage >&2
    exit 1
    ;;
esac
