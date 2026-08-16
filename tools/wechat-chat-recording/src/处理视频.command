#!/bin/bash
# 聊天录屏 → 逐屏截图 + 带页码 PDF（把视频拖进窗口即可）
set -u
cd "$(dirname "$0")" || { echo "无法进入脚本所在目录"; exit 1; }
APP_DIR="${CHAT_RECORDING_HOME:-$HOME/Library/Application Support/聊天录屏工具}"
PYTHON_BIN="$APP_DIR/venv/bin/python3"

echo "=============================================="
echo "  聊天录屏 → 逐屏截图 + 带页码 PDF"
echo "=============================================="
echo "把视频文件拖到本窗口，然后按回车："
video=""
read -r video || true

# 清理拖拽/粘贴产生的引号与转义：空格、撇号、双引号、~ 家目录
video=$(python3 - "$video" <<'PY'
import os
import sys

s = sys.argv[1]
if len(s) >= 2 and s[0] in "'\"" and s[-1] == s[0]:
    s = s[1:-1]
s = s.replace("\\ ", " ").replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")
while "''" in s:  # 折叠 zsh 拖拽撇号转义残留
    s = s.replace("''", "'")
s = os.path.expanduser(s).strip()
print(s)
PY
)

if [ -z "$video" ]; then
    echo "没有输入视频，已退出。"
    read -r -p "按回车退出…" || true
    exit 1
fi
if [ ! -f "$video" ]; then
    echo "找不到这个文件（请确认拖入的是单个视频文件、路径正确）："
    echo "  $video"
    read -r -p "按回车退出…" || true
    exit 1
fi
if [ ! -x "$PYTHON_BIN" ]; then
    echo "本机运行环境尚未准备或已失效。"
    echo "请先双击本目录下的“安装或修复运行环境.command”完成检查和准备。"
    read -r -p "按回车退出…" || true
    exit 1
fi

printf "是否先做预检（推荐：先生成样张和成员建议，再全量处理）？[y/N]："
read -r PRE || true
if [ "$PRE" = "y" ] || [ "$PRE" = "Y" ]; then
    "$PYTHON_BIN" video2screens.py "$video" --preview
    echo
    echo "预检完成：请查看输出目录里的“00_预检”样张和报告，"
    echo "填写“视频信息.json”（右侧/左侧是谁、群聊名单）后，重新运行本工具即可全量处理。"
    read -r -p "按回车退出…" || true
    exit 0
fi

echo "（可选）填写对话人姓名，直接回车可跳过："
printf "右侧对话人（绿色气泡一侧，默认“我”）："
read -r ME || true
printf "左侧对话人（白色气泡一侧，默认“对方”）："
read -r OTHER || true

args=(video2screens.py "$video")
[ -n "$ME" ] && args+=(--me "$ME")
[ -n "$OTHER" ] && args+=(--other "$OTHER")
"$PYTHON_BIN" "${args[@]}"
rc=$?
echo
if [ "$rc" -eq 0 ]; then
    read -r -p "处理完成，按回车退出…" || true
    exit 0
else
    echo "处理未成功（错误码 $rc），请查看上方的错误提示。"
    read -r -p "按回车退出…" || true
    exit "$rc"
fi
