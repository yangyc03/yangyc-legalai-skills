#!/bin/bash
# 双击后修改已有成果的人名，仅重新生成 Word 文字稿。
set -u
cd "$(dirname "$0")" || exit 1

APP_DIR="${CHAT_RECORDING_HOME:-$HOME/Library/Application Support/聊天录屏工具}"
PYTHON_BIN="$APP_DIR/venv/bin/python3"

echo "=============================================="
echo "  修改人名并重新生成 Word 文字稿"
echo "=============================================="
if [ ! -x "$PYTHON_BIN" ]; then
  echo "本机运行环境尚未准备。请先双击“安装或修复运行环境.command”。"
  read -r -p "按回车退出…" || true
  exit 1
fi

"$PYTHON_BIN" edit_names.py
rc=$?
echo
if [ "$rc" -eq 0 ]; then
  read -r -p "完成，按回车退出…" || true
else
  echo "未完成修改（错误码 $rc）。"
  read -r -p "按回车退出…" || true
fi
exit "$rc"
