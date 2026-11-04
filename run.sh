#!/usr/bin/env bash
# 用法: ./run.sh [main.tf 以外的 oracle_arm.py 参数...]
# 例:   TG_BOT_TOKEN=xxx TG_CHAT_ID=yyy ./run.sh --disable-pwd
set -euo pipefail
cd "$(dirname "$0")"

TF_FILE="main.tf"
VENV_DIR="venv"
PID_FILE="oracle_arm.pid"
LOG_FILE="oracle_arm.log"

command -v python3 >/dev/null || { echo "❌ 未安装 python3: sudo apt install python3 python3-venv"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "❌ 需要 Python 3.10+（当前 $(python3 -V)）"; exit 1; }
[ -f "$TF_FILE" ] || { echo "❌ 当前目录下找不到 $TF_FILE，请先上传"; exit 1; }

if [ ! -x "$VENV_DIR/bin/python" ]; then
    python3 -m venv "$VENV_DIR" || { echo "❌ 创建 venv 失败: sudo apt install python3-venv"; exit 1; }
fi
"$VENV_DIR/bin/python" -m pip install -q --upgrade pip
"$VENV_DIR/bin/python" -m pip install -q -r requirements.txt || {
    echo "❌ 依赖安装失败。若需编译 C 扩展（如 Python 3.15），先执行: sudo apt install build-essential python3-dev"; exit 1; }

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "停止旧进程 $(cat "$PID_FILE")"
    kill "$(cat "$PID_FILE")"
fi

"$VENV_DIR/bin/python" oracle_arm.py "$TF_FILE" --dry-run "$@"

nohup "$VENV_DIR/bin/python" oracle_arm.py "$TF_FILE" "$@" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "✅ 已在后台运行 (PID $(cat "$PID_FILE"))"
echo "   查看日志: tail -f $LOG_FILE"
echo "   停止:     kill \$(cat $PID_FILE)"
