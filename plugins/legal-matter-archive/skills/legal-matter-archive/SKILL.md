---
name: legal-matter-archive
description: "Prepare safe, lawyer-confirmed formal archives for Chinese litigation and arbitration matters, special non-litigation projects, and regular-counsel service-period closure. Use for 卷宗归档、结案归档、案卷目录、卷宗封皮、连续页码、办案小结、项目工作小结或常法期满归档 after a lawyer confirms formal closing. Preserve source files, require dry-run and confirmation tokens, and distinguish technical completion from visual QA and OA submission readiness. Do not manage active matters, make legal judgments, operate OA, or modify original project files."
---

# Legal Matter Archive

## 维护信息

- 中文名称：法律业务结案归档
- 英文名称：Legal Matter Archive
- 维护者：Yingchao Yang（`yangyc`）
- 软件版本：`0.1.0-beta`
- 配置结构版本：`1.0`
- 最近更新：2026-07-16
- 许可：Apache-2.0，详见 `LICENSE`
- 来源说明：作者本人曾独立制作 WorkBuddy `case-archive-organizer`。本公开版为独立重构，不包含或复制 WorkBuddy 源代码。

`0.1.0-beta` 表示公开功能仍在测试。`config_schema_version: "1.0"` 只标识本地配置文件结构；软件和配置结构可以分别升级。

## 接管条件与边界

仅在律师确认事项已进入正式结案归档阶段后使用。本 Skill 始终遵守：

1. 原始材料只读；不得移动、删除、重命名、覆盖，输出目录必须与来源目录分离。
2. 先盘点和预演，再由律师确认；只有匹配的确认令牌才能执行准备、最终化、交付或失效操作。
3. 一份主委托合同及属于该合同的补充协议原则上构成一个归档单元；例外由律师明确指定。
4. 材料范围、顺序、排除项、必备材料、小结、封皮字段、内部结案日期和现有页码均由律师确认，不从文件名、时间戳或旧经验推断。
5. 本 Skill 不进行法律分析，不判断胜败或项目成功，不管理在办事项，不写案件台账，不操作 OA。
6. 客户材料、配置、运行结果和模板只保存在用户指定的本地位置，不上传网络。

其他 Skill 或 Agent 可以通过 `profile`、`matter`、`source_root`、`output_root`、`items`、`exclusions`、`summary`、`engagement`、`confirmations` 等公共字段进行可选兼容交接。本 Skill 不要求对方声明来源 Skill，也不依赖任何特定 Skill、Agent 或专用 handoff schema。

## 首次使用

脚本入口为 `scripts/legal_matter_archive.py`。`--help`、`doctor`、`setup` 和 `configure` 在没有第三方 Python 库时也必须可运行。

1. 检测环境，不读取案件目录：

   ```text
   python scripts/legal_matter_archive.py doctor
   ```

2. 生成安装计划。`core` 是必选层，其他层按需加入：

   ```text
   python scripts/legal_matter_archive.py setup --plan --layers core,visual
   ```

3. 向用户完整展示安装内容、位置、命令、影响和 `confirmation_token`。只有用户明确确认后才能执行：

   ```text
   python scripts/legal_matter_archive.py setup --apply --layers core,visual --confirm TOKEN
   ```

   `setup --apply` 创建独立的用户级虚拟环境，不修改 Agent 自带 Python。把返回结果中的 `venv_python` 记为后续命令使用的 `PYTHON`；例如执行 `PYTHON scripts/legal_matter_archive.py doctor`。如果 Agent 有“本地 Python/命令解释器”设置，应配置为该路径。不要把依赖静默安装到系统 Python。

4. 复制并填写 `configs/local-config.example.json`。首次配置必须由用户提供：

   - 启用的归档类型：诉讼仲裁、专项非诉、常年顾问期满中的一种或多种；
   - 每种类型使用已填写 PDF，还是本地 DOCX 封皮和目录模板；
   - 本地归档规则、封皮和目录文件，以及各模板预期页数；
   - 是否启用图像输入、自动视觉核验、qpdf 兼容增强、可打印权限加密 PDF、OCR；
   - 模板路径必须是用户本地文件，不得把模板、客户资料或密钥写进 Skill。

5. 先检查模板，再预览配置写入：

   ```text
   python scripts/legal_matter_archive.py template-check --mode pdf --cover COVER.pdf --directory DIRECTORY.pdf
   python scripts/legal_matter_archive.py configure --plan --input local-config.json
   ```

6. 向用户展示写入位置、模板检查结果和令牌；确认后执行：

   ```text
   python scripts/legal_matter_archive.py configure --apply --input local-config.json --confirm TOKEN
   ```

配置查找顺序是：全局参数 `--config`、环境变量 `LEGAL_MATTER_ARCHIVE_CONFIG`、平台默认位置。macOS/Linux 默认为 `~/.config/legal-matter-archive/config.json`，Windows 默认为 `%LOCALAPPDATA%\legal-matter-archive\config.json`。配置不得保存客户资料、案件路径、密码、密钥或项目数据。

## 能力层与降级

- `core`：Python 3.10+、pypdf、ReportLab。处理 PDF 盘点、哈希、排序、合并、页码、清单和交付。
- `docx`：lxml、LibreOffice。只在自动填写本地 DOCX 封皮或目录时需要。
- `image`：Pillow。只在材料包含图片时需要。
- `visual`：Poppler `pdftoppm`。用于自动渲染核验，不依赖 Pillow。
- `pdf-compat`：qpdf。默认不需要；pypdf 合并失败或页面资源异常时启用。
- `encrypted-pdf`：Ghostscript。只处理无需打开密码且允许打印的权限型加密 PDF。
- `ocr`：Tesseract。供用户配置的本地 OCR 策略使用，不把 OCR 结果自动当作律师确认事实。

缺少可选能力时应明确报告降级。没有 Poppler 或未完成视觉复核时，只能标记为 `technical_complete_manual_visual_review_required`；自动视觉 QA 和律师复核均满足后，才允许标记为 `ready_for_oa_submission`。

macOS 只有在 Homebrew 已存在且用户在安装计划中明确选择时才调用；不得自动安装 Homebrew。Windows `0.1.0-beta` 为实验性支持，只检测系统程序并提供安装指引，不自动安装。

## 每次归档

先读取：

- `references/scope-and-routing.md`
- `references/profile-rules.md`
- `references/local-configuration.md`
- `references/data-contract.md`
- `references/confirmation-gates.md`

每次归档由用户提供来源目录、输出目录、案件基本信息、材料清单与顺序、排除项、必备材料、归档小结以及律师确认结果。使用 `scripts/schemas/archive_request.schema.json` 校验请求。

按以下顺序执行：

1. `inspect`：只读盘点来源文件、哈希、类型、损坏、加密和重复候选。
2. `plan`：生成材料范围、顺序、缺失项、风险、模板和拟输出预览；不得技术处理材料。
3. `prepare`：仅使用匹配令牌，在独立事务目录生成转换副本和封皮、目录预览。
4. 律师检查真实页数、目录、封皮、材料顺序和所有警示。
5. `finalize`：使用第二道令牌生成最终 PDF、manifest 和技术记录。
6. `verify`：检查哈希、页数、连续页码和渲染结果。视觉确认必须记录复核人。
7. `preview-deliver` / `deliver`：再次预览并确认后，只把最终 PDF 复制到项目内空的交付目录。
8. 如需撤回，使用 `preview-invalidate` / `invalidate` 标记 run 失效；不删除源材料或历史技术记录。

归档命令必须提供 `--project-root`。示例：

```text
python scripts/legal_matter_archive.py --project-root PROJECT plan --input archive-request.json
python scripts/legal_matter_archive.py --project-root PROJECT prepare --plan PLAN.json --confirm TOKEN
python scripts/legal_matter_archive.py --project-root PROJECT finalize --prepared-manifest PREPARED.json --confirm TOKEN
python scripts/legal_matter_archive.py --project-root PROJECT verify --manifest MANIFEST.json
```

## Agent 兼容

- 通用前提：Agent 能读取 `SKILL.md`、执行本地命令、访问用户明确指定的本地文件，并能使用 Python 3.10+ 或 `setup --apply` 返回的虚拟环境。只有对话能力、不能执行本地命令的 Agent 无法运行归档 Tool。
- Codex：可直接安装解压后的 Skill 根目录；仓库形态还可由 `.codex-plugin/plugin.json` 发现。
- WorkBuddy：导入同一 Skill 目录后，新任务依次要求 Agent 运行 `doctor`、`setup --plan`、等待人工确认、`setup --apply`，再把返回的 `venv_python` 配置为本地 Python，然后运行 `configure --plan`、等待人工确认、`configure --apply`。WorkBuddy 不会因为导入 Skill 自动安装 Python、OCR 或系统程序；若不能自动发现 Skill，手动指定根目录中的 `SKILL.md`。
- 其他 Agent：若兼容 Agent Skills 目录规范，可导入同一目录；否则手动加载 `SKILL.md`。本项目不承诺所有客户端的自动发现机制。

任何 Agent 都不得把“已生成 plan”“已完成技术处理”或“已生成 PDF”表述为“归档完成”。`ready_for_oa_submission` 仅表示具备提交 OA 的准备条件，不表示 OA 已审核、已接收或已完成行政归档。
