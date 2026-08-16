# Third-Party Notices

本工具自身代码采用 Apache License 2.0（见 `LICENSE`）。下面组件不随本仓库或 Release 安装包分发；用户可按各自许可证在本机独立安装或使用。

| 组件 | 用途 | 许可证或条款来源 |
| --- | --- | --- |
| NumPy | 图像及数值处理 | BSD-3-Clause，见 <https://numpy.org> |
| OpenCV / opencv-python-headless | 画面分析 | Apache-2.0，见 <https://opencv.org> |
| Pillow | 图像与 PDF 生成 | HPND / PIL Software License，见 <https://python-pillow.org> |
| python-docx | Word 文稿生成 | MIT，见 <https://python-docx.readthedocs.io> |
| FFmpeg | 视频与音轨读取 | 具体构建可能适用 LGPL 或 GPL，见 <https://ffmpeg.org/legal.html> |
| Tesseract OCR | 可选 OCR 兜底 | Apache-2.0，见 <https://github.com/tesseract-ocr/tesseract> |
| whisper.cpp | 可选本地语音转写备用 | MIT，见 <https://github.com/ggml-org/whisper.cpp> |
| MacWhisper、macOS Vision | 可选本机语音与 OCR 能力 | 受各自软件及 Apple 条款约束，不随本工具分发 |

用户应在使用前自行确认第三方软件、模型、语言包和 Homebrew 安装包的适用许可与地区可用性。本工具不随附语音模型。
