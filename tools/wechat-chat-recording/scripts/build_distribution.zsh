#!/bin/zsh
# 生成可公开分发的 macOS 安装包；只打包公开白名单文件，不含模型、日志或配置。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOOL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$TOOL_DIR/VERSION")"
PACKAGE_NAME="wechat-chat-recording-v${VERSION}-macOS"
OUTPUT_DIR="${1:-$TOOL_DIR/dist}"
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/wechat-chat-recording-package.XXXXXX")"
trap 'rm -rf "$STAGE_DIR"' EXIT

mkdir -p "$OUTPUT_DIR" "$STAGE_DIR/$PACKAGE_NAME"
cp "$TOOL_DIR/src"/*.py "$TOOL_DIR/src"/*.swift "$TOOL_DIR/src"/*.command \
  "$TOOL_DIR/src/requirements.txt" "$TOOL_DIR/src/工具配置.example.json" "$STAGE_DIR/$PACKAGE_NAME/"
cp "$TOOL_DIR/README.md" "$TOOL_DIR/使用说明.md" "$TOOL_DIR/LICENSE" "$TOOL_DIR/THIRD_PARTY_NOTICES.md" "$STAGE_DIR/$PACKAGE_NAME/"
chmod +x "$STAGE_DIR/$PACKAGE_NAME"/*.command

ARCHIVE="$OUTPUT_DIR/${PACKAGE_NAME}.zip"
rm -f "$ARCHIVE"
(
  cd "$STAGE_DIR"
  zip -rqX "$ARCHIVE" "$PACKAGE_NAME"
)
shasum -a 256 "$ARCHIVE"
