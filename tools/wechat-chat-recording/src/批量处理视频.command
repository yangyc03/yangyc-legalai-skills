#!/bin/bash
# 把包含聊天录屏的视频文件夹拖入窗口，逐段处理并汇总 PDF、Word 文稿。
set -u
cd "$(dirname "$0")" || exit 1

APP_DIR="${CHAT_RECORDING_HOME:-$HOME/Library/Application Support/聊天录屏工具}"
PYTHON_BIN="$APP_DIR/venv/bin/python3"

echo "=============================================="
echo "  批量处理聊天录屏（PDF + Word 汇总）"
echo "=============================================="
echo "把包含视频的文件夹拖到这里，然后按回车："
read -r folder || true

folder=$(python3 - "$folder" <<'PY'
import os
import sys
s = sys.argv[1].strip()
if len(s) >= 2 and s[0] in "'\"" and s[-1] == s[0]:
    s = s[1:-1]
print(os.path.expanduser(s.replace("\\ ", " ")))
PY
)

if [ ! -d "$folder" ]; then
  echo "找不到文件夹：$folder"
  read -r -p "按回车退出…" || true
  exit 1
fi
if [ ! -x "$PYTHON_BIN" ]; then
  echo "本机运行环境尚未准备。请先双击“安装或修复运行环境.command”。"
  read -r -p "按回车退出…" || true
  exit 1
fi

"$PYTHON_BIN" batch_process.py "$folder"
rc=$?
echo
if [ "$rc" -eq 0 ]; then
  echo "处理完成：请查看“聊天录屏工具批量成果/汇总”。"
else
  echo "部分视频未完成；已处理的视频和批处理记录仍保留。"
fi
read -r -p "按回车退出…" || true
exit "$rc"
