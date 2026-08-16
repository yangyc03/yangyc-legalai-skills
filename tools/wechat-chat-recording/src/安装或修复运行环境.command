#!/bin/bash
# 在每台 Mac 上独立检查并准备本地运行环境。默认不下载语音模型。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="${CHAT_RECORDING_HOME:-$HOME/Library/Application Support/聊天录屏工具}"
VENV_DIR="$APP_DIR/venv"
MODEL_DIR="$APP_DIR/models"
MODEL_NAME="${1:-}"

if [ -n "$MODEL_NAME" ] && [ "$MODEL_NAME" != "small" ] && [ "$MODEL_NAME" != "medium" ]; then
  echo "如需显式下载 whisper.cpp 备用模型，参数只能是 small 或 medium。"
  exit 2
fi

echo "正在检查并准备聊天录屏工具的本机环境…"
mkdir -p "$APP_DIR" "$MODEL_DIR"

brew_install() {
  if ! command -v brew >/dev/null 2>&1; then
    echo "缺少 $1，且本机没有 Homebrew。请先从 https://brew.sh/ 安装 Homebrew。"
    exit 1
  fi
  brew install "$1"
}

PYTHON_CMD=""
if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  PYTHON_CMD="$(command -v python3)"
else
  echo "未找到 Python 3.10 或更高版本，正在安装本机 Python 3.12…"
  brew_install python@3.12
  PYTHON_CMD="$(brew --prefix python@3.12)/bin/python3.12"
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  brew_install ffmpeg
fi

if [ -d "$VENV_DIR" ] && { [ ! -x "$VENV_DIR/bin/python3" ] || ! "$VENV_DIR/bin/python3" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }; then
  mv "$VENV_DIR" "$APP_DIR/venv-旧环境-$(date +%Y%m%d-%H%M%S)"
fi
if [ ! -x "$VENV_DIR/bin/python3" ]; then
  "$PYTHON_CMD" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python3" -m pip install --upgrade pip
"$VENV_DIR/bin/python3" -m pip install -r "$SCRIPT_DIR/requirements.txt"

MODEL_PATH=""
if [ -n "$MODEL_NAME" ]; then
  if ! command -v whisper-cli >/dev/null 2>&1; then
    brew_install whisper-cpp
  fi
  MODEL_PATH="$MODEL_DIR/ggml-$MODEL_NAME.bin"
  if [ "$MODEL_NAME" = "small" ]; then
    MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/c521a4b02f422512d734391fdf08bb08c0862f68/ggml-small.bin"
    MODEL_SHA256="1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"
  else
    MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/c521a4b02f422512d734391fdf08bb08c0862f68/ggml-medium.bin"
    MODEL_SHA256="6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208"
    echo "medium 模型约 1.53 GB，下载和处理都会更慢。"
  fi

  model_ok="no"
  if [ -f "$MODEL_PATH" ]; then
    current_sha="$(shasum -a 256 "$MODEL_PATH" | awk '{print $1}')"
    [ "$current_sha" = "$MODEL_SHA256" ] && model_ok="yes"
  fi
  if [ "$model_ok" != "yes" ]; then
    tmp_model="$(mktemp "$MODEL_DIR/.ggml-$MODEL_NAME.download.XXXXXX")"
    trap 'rm -f "$tmp_model"' EXIT
    echo "已按显式参数开始下载本地 $MODEL_NAME 备用模型…"
    curl -fL --retry 3 "$MODEL_URL" -o "$tmp_model"
    actual_sha="$(shasum -a 256 "$tmp_model" | awk '{print $1}')"
    if [ "$actual_sha" != "$MODEL_SHA256" ]; then
      echo "模型完整性校验失败，已停止。"
      exit 1
    fi
    mv "$tmp_model" "$MODEL_PATH"
    trap - EXIT
  fi
fi

PYTHONPATH="$SCRIPT_DIR" "$VENV_DIR/bin/python3" -c 'from config import ensure_template; print("本机配置：", ensure_template())'

echo
echo "本机环境检查/准备完成。"
echo "运行环境：$VENV_DIR"
if command -v mw >/dev/null 2>&1 && [ -d "$HOME/Library/Application Support/MacWhisper/models" ]; then
  echo "语音模型：优先复用 MacWhisper 已安装的本地模型"
elif [ -n "$MODEL_PATH" ]; then
  echo "语音模型：$MODEL_PATH"
else
  echo "语音模型：未找到可直接确认的本地后端，已保持不下载。"
  echo "如确实需要 whisper.cpp 备用 small 模型，可显式运行：bash \"$SCRIPT_DIR/安装或修复运行环境.command\" small"
fi
if command -v tesseract >/dev/null 2>&1; then
  echo "OCR：使用 macOS Vision，并可调用本机 Tesseract 质量兜底"
else
  echo "OCR：使用 macOS Vision（本机未安装 Tesseract，不影响基本识别）"
fi
echo "现在可以双击“处理视频.command”。"
read -r -p "按回车退出…" || true
