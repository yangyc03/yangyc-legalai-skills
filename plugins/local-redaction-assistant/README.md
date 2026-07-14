# Local Redaction Assistant

公开发行候选版本：`1.2.0`。

这是一个本机优先的浏览器界面，用于处理律师明确选取的脱敏副本：

- DOCX：普通 AI 共享副本；正文表格逐单元格扫描；在 Mac 上可选法律模板提炼流程；
- PDF 和图片：本地人工区域脱敏，生成图片型视觉副本；
- 网页提供上传、确认、生成和下载按钮；原件不覆盖；
- 服务只绑定 `127.0.0.1`，不调用云端 OCR、外部 API 或网络服务。

v1.2.0 新增：

- DOCX 正文表格支持合并单元格、多段落和跨 run 文本扫描；
- 支持当前会话导入 `redaction_dictionary.local.json`，不写入报告或发行包；
- 手机号、邮箱、有效身份证号和有效统一社会信用代码只有在用户明确开启后才自动处理；
- 姓名、主体、地址、项目、金额、日期和比例等仍需人工确认。

## 适用边界

本工具不是 PDF 真正的法律意义上的 redaction 工具，也不是 OCR 自动识别器。它不支持 XLSX 脱敏、无人值守目录递归、宏/OLE/附件改写，也不保证识别全部敏感信息。生成文件必须由律师逐页人工复核。

网页首版一次处理一份文件，默认文件大小上限为 200 MB。请先选择副本，不要选择原件。

## 启动

无需 Codex 也可以启动网页界面，但需要本机 Python：

- macOS：双击 `tools/启动本地脱敏网页.command`；
- Windows：双击 `tools/启动本地脱敏网页.bat`；
- 命令行：使用 Python 运行 `skills/local-redaction-assistant/scripts/web_app.py`。

启动文件会优先使用用户已安装的同名 runtime，找不到时回退到当前插件内的源版本。浏览器关闭不会自动停止服务，可在网页中点击“关闭服务”，或在启动窗口中结束进程。

## 环境要求

- Python 3.10 或更高版本；
- PDF/图片预览和渲染需要安装 PyMuPDF：

  ```text
  python -m pip install -r requirements-pdf.txt
  ```

- Mac 的 `legal-template` 流程需要 Microsoft Word for Mac；
- Windows 可启动网页并处理普通 DOCX、PDF/图片视觉副本，但本候选版本不宣称支持 Windows Word 原生自动化验证。
- DOCX 词典格式模板见 `skills/local-redaction-assistant/configs/redaction_dictionary.example.json`。

PyMuPDF 不包含在本插件中。安装前请阅读其许可和适用条件，见 `THIRD_PARTY_NOTICES.md`。

## 安全与隐私

文件在本机临时工作区处理，服务退出时清理本次工作区。安全报告只记录安全编号、状态、数量和安全错误码，不记录原始姓名、候选词或坐标映射。请勿把真实客户文件上传到公共仓库、Issue 或 Pull Request。

## 许可证

本插件自有代码按仓库的 Apache License 2.0 发布。第三方依赖继续适用其原始许可；具体说明见 `THIRD_PARTY_NOTICES.md`。
