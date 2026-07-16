# 本地配置与模板

## 配置位置

查找顺序固定为：

1. 全局参数 `--config PATH`；
2. 环境变量 `LEGAL_MATTER_ARCHIVE_CONFIG`；
3. macOS/Linux 的 `~/.config/legal-matter-archive/config.json`，或 Windows 的 `%LOCALAPPDATA%\legal-matter-archive\config.json`。

运行环境与缓存使用用户本地数据目录，不写入 Skill 目录或案件目录。配置只保存能力、程序路径、归档规则和模板描述，不保存客户资料、案件路径、项目数据、密码或密钥。

`setup --apply` 返回的 `venv_python` 是本 Skill 的独立 Python。Agent 或用户应使用它运行后续 `doctor`、`configure` 和归档命令；导入 Skill 本身不会改变 WorkBuddy、Codex 或其他 Agent 的系统 Python。`enabled_layers` 和 `policies` 是实际执行边界，不是展示字段；未启用的可选程序即使已安装也不得调用。

## 必填配置

顶层至少包含：

- `config_schema_version: "1.0"`；
- `enabled_layers`；
- `profiles`，至少配置一种归档类型。

每种 profile 包含：

- `template_mode`：`pdf` 或 `docx`；
- `rule.path`：本地归档规则文件；
- `cover.path`、`cover.expected_pages`；
- `directory.path`、`directory.expected_pages`；
- `ocr_enabled`。

`configure --apply` 会把已检测的 Python、外部程序路径、模板哈希和启用能力写入用户级配置。模板或规则内容变化后，原哈希失效，必须重新检查和确认配置。

## PDF 模式

用户提供已经填写完成、无需自动改字的封皮 PDF 和目录 PDF。Tool 校验格式、页数和哈希，再把它们作为前置页。每个具体事项应使用对应的已填写副本，不要指向会被日常覆盖的模板原件。

## DOCX 模式

用户提供本地封皮和目录 DOCX。封皮必须具有可识别的字段标签和独立值区；目录必须含“序号、名称、页码、备注”表头及可复制的数据行。Tool 只在输出副本上填值，不修改模板原件。

运行 `template-check` 只检查兼容性，不修改文件。DOCX 模式需要 lxml 和 LibreOffice；转换后的页数必须等于配置中的预期页数，否则阻断。

## OCR 与视觉策略

- `0.1.0-beta` 的 OCR 层只检测 Tesseract 并记录用户管理的本地 OCR 策略；归档 Tool 不自动执行 OCR。外部本地 OCR 结果是待校对线索，不自动成为案件事实。
- 自动视觉核验需要同时启用 `visual` 层、选择 `automatic_when_available` 并检测到 Poppler。缺少或未启用时技术处理可以继续，但必须保留人工视觉复核状态。
- qpdf、Ghostscript 和 Tesseract 均为按需能力，不应成为基础 PDF 流程的强制依赖。
