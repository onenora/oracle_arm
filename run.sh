#!/usr/bin/env bash
set -euo pipefail

# 检查当前环境是否安装了 python3 和 pip
if ! command -v python3 &> /dev/null; then
    echo "Python3 未安装，请先执行 sudo apt install python3 python3-pip"
    exit 1
fi

# 检查 main.tf 是否存在
TF_FILE="main.tf"

# 检查 requirements.txt 是否存在
if [ ! -f "requirements.txt" ]; then
    echo "❌ 错误: 当前目录下找不到 requirements.txt 文件！"
    exit 1
fi

if [ ! -f "$TF_FILE" ]; then
    echo "❌ 错误: 当前目录下找不到 $TF_FILE 文件，请先上传！"
    exit 1
fi

VENV_DIR="venv"
echo "正在配置 Python 虚拟环境..."

if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# 更新 pip 并安装依赖
echo "正在安装运行依赖库..."
pip install --upgrade pip
pip install -r requirements.txt

# 停止可能已经存在的旧进程
pkill -f "python3 oracle_arm.py" || true

# 使用 nohup 挂起后台运行
echo "🚀 正在启动后台挂机任务..."
nohup python3 oracle_arm.py "$TF_FILE" "$@" > run.log 2>&1 &

echo "✅ 任务已在后台运行！"
echo "👉 您可以使用命令实时查看日志: tail -f run.log"