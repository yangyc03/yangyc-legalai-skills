#!/bin/zsh
# 验证公开安装包的结构；不下载依赖、不读取任何用户视频。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOOL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/wechat-chat-recording-check.XXXXXX")"
trap 'rm -rf "$CHECK_DIR"' EXIT

"$SCRIPT_DIR/build_distribution.zsh" "$CHECK_DIR/dist" >/dev/null
ARCHIVE="$(find "$CHECK_DIR/dist" -name '*.zip' -print -quit)"
test -n "$ARCHIVE"
unzip -tq "$ARCHIVE" >/dev/null
unzip -q "$ARCHIVE" -d "$CHECK_DIR/unpacked"
PACKAGE_DIR="$(find "$CHECK_DIR/unpacked" -mindepth 1 -maxdepth 1 -type d -print -quit)"

for item in video2screens.py batch_process.py '安装或修复运行环境.command' '处理视频.command' '批量处理视频.command' README.md 使用说明.md LICENSE THIRD_PARTY_NOTICES.md; do
  test -e "$PACKAGE_DIR/$item"
done
test -x "$PACKAGE_DIR/安装或修复运行环境.command"
python3 -m compileall -q "$PACKAGE_DIR"
echo "公开安装包结构验证通过：$ARCHIVE"
