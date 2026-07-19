---
name: legal-network-verification
description: Conduct authorized Chinese legal public-source verification for companies and individuals through an available interactive browser or user-provided privacy-safe evidence; resolve same-name candidates, preserve page-only timestamp-watermarked screenshots, and generate internal Markdown workpapers plus concise formal DOCX query records. Use for 网络核查、公开网站查询、同名排除、截图底稿及证券、基金和其他非诉项目工作底稿。
---

# 法律网络核查 / Legal Network Verification

## 维护信息

- 版本：v1.3.0-beta
- 维护者：Yingchao Yang
- 许可：Apache License 2.0
- 最近更新：2026-07-19
- 适用语境：中国法律公开信息核查；法律依据和网站功能均需按项目时点复核。

## 任务边界

记录经授权的公开网站查询过程和有限结果。不得自动认定主体不存在诉讼、执行、处罚、失信或其他负面事项；不替代身份证明、主管机关文件、当事人声明、访谈和其他书面查验。

核心 Skill 对 Agent 平台中立。开始前读取 `references/agent-compatibility.md`，判断当前 Agent 是否具备浏览器、本地文件和 Python 能力。能力不足时按其中的降级路径处理，不得把计划、用户自查或未完成查询表述为 Agent 已查询。

## 输入

至少确认：

```text
项目名称：
核查事项：
核查对象：完整的公司名称或自然人姓名
核查期间：
输出目录：
```

可选输入包括项目类型、关联机构、主体角色、统一社会信用代码、既有脱敏身份辅助字段、指定网站及已脱敏截图。用户须预先确定主体、事项、网站、域名、期间和查询方式；自然人只能由用户明确列入，不从公司派生关联自然人。

- 默认时区为 `Asia/Shanghai`，可在运行文件中显式改为其他 IANA 时区。
- 根据主体和项目选择 `general-person`、`general-company`、`private-fund` 或 `custom`；读取 `references/site-profiles.md`。
- 统一社会信用代码只允许写入公司主体的 `credit_code`，并按 GB 32100 字符集和校验位验证；自然人不得使用该字段。
- schema 1.1 的新查询必须先建立 `query_scope`，运行只读 `scope-preview`，并将范围状态确认至 `user_confirmed`；每条查询必须引用已确认的范围项，且查询时间不得早于确认时间。范围外的网站、域名、事项、主体、期间或方式一律拒绝。
- schema 1.1 不保存 Agent 自行填写的 `query_terms` 或 `filters`；底稿中的查询条件描述由脚本根据已确认的查询方式、主体类型和结构化 `conditions` 生成。
- 公司 `formal_identifier_mode` 默认为 `user_fill`；仅在用户明确选择 `auto_fill_company_credit_code` 后，才可将已校验的统一社会信用代码填入正式记录。自然人只能 `user_fill`。
- `formal-mode=final` 允许身份号码/代码人工填写位置为空，并保留正式记录第三列；draft 显示 `【待用户填写】`。完整身份证号码只允许用户在本 Skill 完成审计后自行补填，补填的归档版不得再交给本 Skill 读取、渲染、截图、审计或复核。
- schema 1.0 仅兼容读取和构建；不得用 `prepare` 创建或向其追加 schema 1.1 查询。

## 个人信息强制规则

涉及自然人、登录、身份证号码或验证码时，必须先读取 `references/privacy-and-sensitive-query.md`。

1. 不得请求用户在对话中提供完整身份证号码，不得从文件或页面读取、复制、转录或保存完整号码。
2. 目标网站确需完整号码时，按“当前任务＋特定对象＋特定网站”取得明确确认；没有确认不得继续。
3. 暂停 Agent 操作，由用户手工输入和提交身份证号码，并完成短信、滑块、人脸等验证。
4. 只有用户确认结果页面不显示完整号码后才能恢复 Agent 操作。页面仍显示完整号码时禁止截图。
5. 留存的脱敏身份证号码必须为前10位数字加末8位星号，例如 `1101011990********`。
6. 任务结束前提示用户清空输入、关闭敏感页面并删除未脱敏临时文件，然后运行 `artifact-audit`。

## 核心流程

1. 锚定项目、事项、主体、期间和输出目录。多候选主体先让用户确认，不自行补全名称或扩展自然人。
2. 运行 `doctor` 判断当前能力，然后读取 `references/site-profiles.md`。将主体、事项、网站、域名、期间和查询方式写入 schema 1.1 `query_scope`，运行 `scope-preview` 供用户确认；未达 `user_confirmed` 不得查询。
3. 涉及同名、身份要素或结论措辞时读取 `references/identity-and-wording-rules.md`。
4. 在授权范围内使用当前 Agent 可用的交互浏览器或用户指定会话。不读取 Cookie、Token、密码或浏览器存储，不绕过验证码和访问限制。
5. 截取网页 viewport 或必要的页面矩形，不截取浏览器收藏夹栏、侧边栏、系统桌面或其他无关区域。在临时目录取得原始截图，调用 `scripts/watermark_capture.py` 生成项目唯一留存版。
6. 使用 `prepare` 创建 schema 1.1 `network-verification.json`，按 `references/run-schema.md` 填写 `queries[]`。新查询逐项引用已确认范围及条件；完整身份证号码、出生日期、手机、邮箱、授权原文和浏览器凭证不得落盘。
7. 使用 `validate` 检查主体、状态、网址、查询时间、截图路径和结论措辞。失败、受限或未完成查询不得解释为无记录。
8. 使用 `build` 生成内部 Markdown 和可选 DOCX。默认 `two-layer`：内部底稿输出至 `01-内部底稿`，正式记录输出至 `02-正式记录`。
9. 交付前运行 `artifact-audit`，检查文本、常见图片元数据及 DOCX 内嵌图片元数据，并对 DOCX 进行结构检查和逐页渲染检查。成果含公司统一社会信用代码时必须通过 `--run-file` 提供当前已验证运行文件；只精确放行该文件中有效的公司代码，无上下文时失败关闭。不修改、移动或覆盖客户原始文件。

## 截图和证据

- 项目目录只保存水印版；原始截图仅在临时目录短暂存在。
- 文件名使用 `NQ-主体序号-网站序号-图片序号_网站简称_主体名称_YYYYMMDD-HHMMSS.png`。
- 水印包含带时区的查询时间、查询对象、网站名称和截图编号；不包含身份证号码、出生日期或其他敏感值。
- 页面仍显示完整号码且无法安全脱敏时，不保留截图；在运行文件中使用 `capture_kind: not_retained_sensitive_page` 并客观说明原因。
- 截图失败、页面无法保存或网站受限时如实记录，不伪造页面副本。

## 两层底稿

### 内部底稿

按 `references/internal-workpaper-template.md` 生成 Markdown，保留核查口径、总表、逐站记录、同名排除、限制、截图链接和补核建议。不重复设置查询地点、查询人或复核人字段。

### 正式查询记录

模板优先级为：用户通过 `--template-docx` 指定的模板；随包 `assets/generic-network-query-record.docx`；无可用模板时草稿模式只生成 Markdown。正式模式无模板时报错。

正文仅保留事项、期间、查询日期、地点、查询人、查询对象及脱敏身份信息、按网站编号的简洁结果、附件说明和查询人员签名栏。不列截图编号、复核人、技术状态标签或内部身份分析。

完整实际网址作为真实超链接；链接目标不改写，显示文字仅加入不可见断行机会，结果段落使用两端对齐和 `w:wordWrap=1`。

`no_match_displayed` 必须写明具体网站、页面客观结果和本次查询限定。`same_name_candidates`、`access_limited`、`failed` 和 `pending_review` 不得生成否定性结论。

## 参考文件

- `references/agent-compatibility.md`：开始任务、安装或诊断能力时读取。
- `references/privacy-and-sensitive-query.md`：自然人、登录、验证码、身份证号码或任务清理时读取。
- `references/legal-basis.md`：设计执业口径或需要说明法律依据时读取；引用前复核官方来源和现行性。
- `references/site-profiles.md`：每次选择查询来源时读取。
- `references/identity-and-wording-rules.md`：自然人、同名、零结果、受限结果或法律意见候选表述时读取。
- `references/workpaper-routing.md`：确定两层目录、用户指定分类和原始资料保护时读取。
- `references/internal-workpaper-template.md`：生成或复核内部 Markdown 时读取。
- `references/run-schema.md`：创建、补录、校验或排查运行文件时读取。

## 命令

使用当前 Agent 已配置的 Python 3.10+ 解释器：

```text
python scripts/network_workpaper.py doctor --browser unknown
python scripts/network_workpaper.py prepare input.json --output network-verification.json
python scripts/network_workpaper.py scope-preview network-verification.json
python scripts/network_workpaper.py validate network-verification.json --workpaper-root WORKPAPER_ROOT
python scripts/network_workpaper.py template-check TEMPLATE.docx
python scripts/network_workpaper.py build network-verification.json --workpaper-root WORKPAPER_ROOT --output-dir OUTPUT_DIR --formal-mode draft --layout two-layer
python scripts/network_workpaper.py artifact-audit OUTPUT_DIR --run-file network-verification.json
python scripts/watermark_capture.py input.png output.png --evidence-id NQ-01-01-01 --subject 甲某 --source 某公开网站 --queried-at 2026-07-17T14:32:18+08:00
```

macOS/Linux 可能使用 `python3`，Windows 可能使用 `py -3`。除非显式传入 `--overwrite`，不得覆盖已有运行文件或成果；输出目标为符号链接时一律拒绝写入。

## 禁止事项

1. 将搜索摘要或同名结果直接归属于核查对象。
2. 使用“确定不存在”“无任何记录”等绝对否定表述。
3. 把登录失败、验证码失败、访问受限或查询失败写成“未发现记录”。
4. 由 Agent 输入、读取、保存或上传完整身份证号码。
5. 在项目文件中保存密码、Cookie、Token、原始工具响应或浏览器会话信息。
6. 修改、覆盖或重命名用户原始截图、报告、法律意见书或模板。
7. 将客户事实、绝对路径、截图、运行文件或真实底稿放入公开代码库或 Skill 目录。
8. 默认生成法律意见书、风险报告、PDF 或 Excel。
