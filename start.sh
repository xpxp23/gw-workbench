#!/usr/bin/env bash
# ==============================================================================
# 公文与材料智能工作台 (GW-Workbench) — 银河麒麟 Kylin V10 (海光 3350) 一键启动与管理工具
# 用法：
#   ./start.sh              # 前台启动服务（默认端口 8090）
#   ./start.sh --bg         # 后台常驻运行（日志输出至 data/server.log）
#   ./start.sh --stop       # 停止后台运行的服务
#   ./start.sh --desktop    # 在银河麒麟桌面自动生成一键打开工作台的快捷方式图标
# ==============================================================================

set -e
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
PORT="${PORT:-8090}"
PID_FILE="$APP_DIR/data/server.pid"
LOG_FILE="$APP_DIR/data/server.log"

mkdir -p "$APP_DIR/data" "$APP_DIR/workspace_files"

if [ "$1" = "--stop" ]; then
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    kill "$(cat "$PID_FILE")"
    rm -f "$PID_FILE"
    echo "✅ 已停止后台运行的工作台服务。"
  else
    echo "ℹ️ 未检测到正在后台运行的服务。"
  fi
  exit 0
fi

if [ "$1" = "--desktop" ]; then
  DESKTOP_DIR="$HOME/桌面"
  [ ! -d "$DESKTOP_DIR" ] && DESKTOP_DIR="$HOME/Desktop"
  mkdir -p "$DESKTOP_DIR"
  SHORTCUT="$DESKTOP_DIR/公文与材料智能工作台.desktop"
  cat > "$SHORTCUT" <<EOF
[Desktop Entry]
Type=Application
Name=公文与材料智能工作台
Comment=启动并打开公文与材料智能工作台 (GW-Workbench)
Exec=bash -c "cd '$APP_DIR' && ./start.sh --bg && xdg-open http://127.0.0.1:$PORT"
Icon=folder-documents
Terminal=false
Categories=Office;Utility;
EOF
  chmod +x "$SHORTCUT"
  echo "✅ 已在桌面生成快捷方式：$SHORTCUT"
  exit 0
fi

if [ "$1" = "--bg" ]; then
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "ℹ️ 工作台已在后台运行中 (PID: $(cat "$PID_FILE"))，直接访问 http://127.0.0.1:$PORT"
    exit 0
  fi
  nohup python3 "$APP_DIR/server.py" "$PORT" > "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  echo "✅ 工作台已在后台启动 (PID: $(cat "$PID_FILE"))"
  echo "👉 本机访问地址：http://127.0.0.1:$PORT"
  echo "📄 日志文件位置：$LOG_FILE"
  exit 0
fi

exec python3 "$APP_DIR/server.py" "$PORT"
