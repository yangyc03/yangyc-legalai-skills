#!/bin/zsh

set -u
export PYTHONUTF8=1

SCRIPT_DIR=${0:A:h}
REPO_ROOT=${SCRIPT_DIR:h}
RUNTIME_SCRIPT="$HOME/.codex/skills/local-redaction-assistant/scripts/web_app.py"
SOURCE_SCRIPT="$REPO_ROOT/skills/local-redaction-assistant/scripts/web_app.py"

if [[ -f "$RUNTIME_SCRIPT" ]]; then
  WEB_SCRIPT="$RUNTIME_SCRIPT"
  echo "使用已同步的 local-redaction-assistant runtime。"
elif [[ -f "$SOURCE_SCRIPT" ]]; then
  WEB_SCRIPT="$SOURCE_SCRIPT"
  echo "runtime 尚未同步，使用当前仓库源版本。"
else
  echo "找不到本地脱敏网页入口："
  echo "  $RUNTIME_SCRIPT"
  echo "  $SOURCE_SCRIPT"
  read "reply?按回车关闭窗口："
  exit 1
fi

BUNDLED_PYTHON="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
PDF_PYTHON="$HOME/.venvs/local-redaction-assistant-pdf/bin/python"

python_supports_pymupdf() {
  "$1" -c 'import pymupdf' >/dev/null 2>&1
}

if [[ -x "$BUNDLED_PYTHON" ]] && python_supports_pymupdf "$BUNDLED_PYTHON"; then
  PYTHON="$BUNDLED_PYTHON"
  echo "使用 Codex bundled Python（含 PDF 预览依赖）。"
elif [[ -x "$PDF_PYTHON" ]] && python_supports_pymupdf "$PDF_PYTHON"; then
  PYTHON="$PDF_PYTHON"
  echo "Bundled Python 缺少 PyMuPDF，切换到本机 PDF 专用环境。"
elif [[ -x "$BUNDLED_PYTHON" ]]; then
  PYTHON="$BUNDLED_PYTHON"
  echo "警告：当前 Python 缺少 PyMuPDF，PDF/图片流程可能不可用。"
elif (( $+commands[python3] )); then
  PYTHON="$(command -v python3)"
  echo "使用系统 Python；如缺少 PyMuPDF，PDF/图片流程可能不可用。"
else
  echo "找不到可用的 Python。"
  read "reply?按回车关闭窗口："
  exit 1
fi

echo "正在启动本机脱敏网页；文件只在本机处理。"
"$PYTHON" "$WEB_SCRIPT" --port 0
EXIT_CODE=$?
echo "本地脱敏网页已退出，退出码：$EXIT_CODE"
read "reply?按回车关闭窗口："
exit $EXIT_CODE
