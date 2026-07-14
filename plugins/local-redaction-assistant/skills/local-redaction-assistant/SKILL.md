---
name: local-redaction-assistant
description: Run a local-only, browser-guided redaction workflow for one explicitly selected lawyer copy or an owner-only whitelist of up to 30 DOCX copies. Use for AI-sharing copies, legal-template extraction, Word revision cleanup, occurrence-level candidate confirmation, sensitive-table clearing, Microsoft Word native validation/PDF review books, local PDF/image manual areas, masked reports, and lawyer review without exposing client text to Codex. 用于普通 AI 共享脱敏、法律模板提炼、Word 修订清洁、逐处候选确认、敏感表格清空、受控批量及集中复核。
---

# 本地文件脱敏辅助 / Local Redaction Assistant

## 维护信息

- 作者：Yingchao Yang（杨颖超律师）
- 版本号：v1.2.0
- 状态：公开发行候选 v1.2.0；Mac Word 正式验证边界保持不变；本地安全壳 + 确定性 Tool + 律师确认
- 创建日期：2026-07-08
- 最近更新日期：2026-07-14
- 许可：本 Skill 自有代码按 Apache License 2.0 发布；第三方依赖按其原始许可，见插件根目录 `../../THIRD_PARTY_NOTICES.md`。
- 引用：复制、迁移或衍生使用时保留作者、版本、日期和许可声明。

## 何时使用

当用户明确指定一份本地副本，或提供 owner-only 的 DOCX 白名单 manifest，并要求：

- 在交给 AI、外部顾问或模板提炼前脱敏；
- 本地确认姓名、主体、简称、地点、电话、证件号、日期、人数、股份数、金额等变量；
- 接受 Word 修订、移除批注并生成清洁副本；
- 对敏感表格逐表选择词项替换或清空明细行；
- 生成安全编号 DOCX、Word 原生 PDF 或集中复核册；
- 对 PDF/图片使用本地人工区域生成图片型视觉副本。

不要用于无人值守目录递归、PDF 真 redaction、XLSX 脱敏、图片 OCR 自动脱敏、宏/OLE/附件改写或声称 100% 自动识别全部姓名地名。

## 强制安全边界

1. 原件不动：不覆盖、不移动、不删除、不重命名原件；输出只写新目录。
2. 本地运行：不联网、不调用云端 OCR 或外部 API；真实正文不得复制到对话。
3. 人工确认：候选默认不自动生效；手机号、邮箱、通过校验的居民身份证号和统一社会信用代码只有在本地页面明确开启并确认后才可自动处理，姓名、主体、地址、项目和数字变量仍须逐项确认。
4. 报告不泄密：reports 只含 safe ID、状态、数量、安全错误码和残留统计。
5. 映射短生命周期：occurrence ID 与原始区段只保存在进程内存；不得写入 reports。
6. Word 失败关闭：`legal-template` DOCX 必须通过 OOXML 静态校验和 Microsoft Word 原生打开；关闭后须确认目标 safe ID 已离开 Word 文档列表、锁文件和暂存件均已消失，LibreOffice 不能替代。
7. 宏、嵌入对象和未处理文本框默认阻断 `legal-template` 成功。
8. 所有输出必须由律师最终逐页复核，不得宣称全自动完成。

## Codex 读取边界

Codex 可以读取：

- 本 Skill、references、示例配置和合成测试；
- 不含原始值的 `reports/*.json`；
- 用户明确允许读取的安全编号脱敏副本。

Codex 不读取：

- 未经授权的客户文件正文；
- `redaction_batch.local.json`、候选词、occurrence 映射、regions、坐标或本地索引；
- 真实项目的原始文件名、路径、客户名称、人员姓名或表格内容。

## 默认入口

本地网页入口（单文件）可通过仓库双击启动文件打开：

- Mac：双击 `tools/启动本地脱敏网页.command`；
- Windows：双击 `tools/启动本地脱敏网页.bat`。

启动文件优先使用已同步的 `~/.codex/skills/local-redaction-assistant` runtime；runtime 尚未同步时回退到当前仓库源版本，并优先使用 Codex bundled Python。网页只绑定 `127.0.0.1`，一次处理一份 DOCX、PDF 或图片；上传文件进入本次临时工作区，完成后由网页下载安全编号副本，服务退出时清理临时工作区。网页本身不向 OneDrive 写出文件，也不调用网络服务。

网页 DOCX `ai-share` 流程使用 v1.2.0 统一正文索引，逐段落、逐表格行和逐单元格扫描；可在本地会话导入 JSON 词典，并在明确开启后处理高置信格式项。`legal-template` 仍要求 Microsoft Word for Mac 原生清洁与验证。启动文件会优先选择含 `PyMuPDF` 的 Codex bundled Python；如果 bundled Python 缺少该依赖，会自动切换到本机 PDF 专用环境。Windows 可以启动网页和处理普通 `ai-share` DOCX、PDF/图片视觉副本，但本版本不把 Windows Word 自动化验证宣称为已实现。

v1.2.0 的 DOCX 表格扫描只覆盖正文表格的安全 `w:t` 文本；支持合并单元格、多段落、跨 run 文本和同一行标签—值关系。文本框、图片、OLE、嵌入对象和复杂域代码仍只进入人工复核。网页显示扫描覆盖统计；任一支持范围扫描失败时不生成成功结果。示例词典为 `configs/redaction_dictionary.example.json`，用户本地词典应命名为 `redaction_dictionary.local.json`，只在当前会话导入，不写入报告、runtime 或发布包。

单文件日常入口：

```bash
python scripts/guided_redactor.py \
  --input /absolute/path/copy.docx \
  --redaction-profile legal-template \
  --word-validation required \
  --revision-policy confirm-accept
```

受控批量入口：

```bash
python scripts/guided_redactor.py \
  --input-manifest /absolute/path/redaction_batch.local.json \
  --output /absolute/path/_redaction_output \
  --redaction-profile legal-template \
  --word-validation required
```

`--input` 与 `--input-manifest` 互斥。批量 manifest 只允许绝对 DOCX 路径和 safe ID，权限必须 owner-only，最多 30 份；禁止目录递归和无人值守。

批量复核册需要 `pypdf` 与 `reportlab`。优先使用 Codex bundled Python；依赖缺失时在读取 manifest 正文前失败，不自动安装。

## Legal-template 流程

按以下顺序执行：

1. 预检 ZIP、OOXML、修订、批注、文本框、字段、外部关系、宏和嵌入对象。
2. 若有修订或批注，先在本地页面确认；Word 只处理安全编号临时副本。
3. 清洁后重新审计，修订节点、继续跟踪或批注不为零即停止。
4. 扫描正文、表格、全部页眉页脚、脚注、尾注、批注和超链接显示文本。
5. 用 occurrence ID 展示规范化候选，确认后回到原始精确 run 区段替换。
6. 敏感表格逐表选择：仅替换词项，或保留表头/合计行并清空其他明细行。
7. 清理 Word 元数据；校验 ZIP、XML、关系、content types 和 lexical QName。
8. 执行残留审计，再由 Microsoft Word for Mac 原生打开验证。
9. 批量模式逐份由 Word 导出 PDF，生成动态封面、分隔页和 safe ID 页码映射。

详细规则见 [DOCX redaction policy](references/docx-redaction-policy.md)。

## 输出结构

批量输出固定为：

```text
_redaction_output/
├── redacted_files/        # safe ID DOCX
├── review_pdfs/           # Word 原生 PDF
├── review_book/           # 集中复核册和 safe ID 页码映射
├── reports/               # 安全汇总
└── logs/                  # 受控短期工作区；结束时清理安全编号临时件
```

manifest、候选、原始区段和映射不进入 reports。若输出目录可能同步，必须明确提示本地敏感中间件风险。

## PDF / 图片边界

既有 PDF/图片流程只接受用户人工区域：

- PDF 输出是页面图片化后烧录黑框的视觉副本，不是真 PDF redaction；
- 图片输出为去元数据 PNG；
- regions、预览图和 OCR 候选属于本地敏感文件；
- 不自动从 OCR 候选生成最终副本，仍需人工确认。

## 成功条件

只有全部满足才可报告成功：

- 原件 hash 与 mtime 不变；
- 支持范围内确认项和高风险残留为零；
- 元数据、外部关系和阻断对象审计通过；
- `legal-template` 输出由 Word 原生打开成功；
- Word 自动化未遗留当前 safe ID 文档或锁文件；
- reports 不含原始值、名称、路径、候选、表格内容或坐标；
- 明确提示律师最终人工复核。

## 运行时与发布

runtime 只打包 `SKILL.md`、`agents/`、`scripts/`、`references/`、`assets/` 和 `configs/`。不打包 tests、review、design、缓存、客户资料、manifest 或本地输出。

repo 合并、正式发布、runtime 同步和新任务加载验证是四个独立确认点；不得互相推定。
