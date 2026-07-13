# DOCX 法律模板脱敏策略 v1.0

## 1. 定位

本策略只用于律师明确指定的 DOCX 副本。它提供本地候选、确定性修改、Word 原生验证和人工复核，不承诺自动识别全部敏感信息。

## 2. 输入与预检

预检必须在正文读取前完成，输出只保留数量和安全状态：

- ZIP CRC、重复部件、必需部件；
- XML/rels 解析、content types、关系目标；
- `xsi:type`、`mc:Ignorable` 中 lexical QName 前缀声明；
- 修订节点、`trackRevisions`、批注数量；
- 文本框、字段、外部关系、图片、宏和嵌入对象数量。

宏、OLE、嵌入附件默认阻断 `legal-template`。字段代码、外部链接目标、图片文字不改写。文本框可显示候选但未完成兼容验收前不自动替换，并阻断成功。

## 3. 修订和批注清洁

发现修订、格式修订、移动文本、继续跟踪或批注时：

1. 本地页面显示数量，不显示正文；
2. 律师确认接受修订和移除批注；
3. 将输入复制为安全编号临时副本；
4. Word for Mac 接受全部修订、关闭继续跟踪、删除全部批注并保存；
5. 重新审计 `w:ins`、`w:del`、move、`*PrChange`、`trackRevisions` 和 comments；
6. 任一残留不为零即停止，不进入候选替换。

不得用正则或普通 XML 逻辑静默接受修订。

## 4. 正式文本范围

支持：

- `word/document.xml` 正文和表格；
- 全部 `word/header*.xml`、`word/footer*.xml`；
- `word/footnotes.xml`、`word/endnotes.xml`；
- 清洁前批注的计数和确认移除；
- 超链接显示文本。

不直接修改：

- `instrText`、field code、外部链接 target；
- VML/DrawingML 文本框；
- 宏、OLE、附件、图片和图表内部文字；
- 删除文本或未接受修订中的文本。

## 5. occurrence ID

每个候选命中生成 `O000001` 形式的短生命周期 ID，并在内存绑定：

- OOXML part；
- paragraph 顺序；
- logical text 起止位置；
- 原始精确值；
- 所跨 `w:t` run 区段；
- 候选类别和规范化显示值。

浏览器只接收 ID、规范化显示值、部件范围和是否支持自动替换。确认后按 ID 回到精确区段；原值、坐标和映射不得落盘。

跨 run 替换只修改相关 `w:t` 内容字节，不整体序列化 `word/document.xml`、页眉页脚或表格部件。

## 6. 候选类别

候选均需律师确认：

- 主体全称、简称、基金和核查对象名称；
- 姓名、联系人、律师、股东、董事、监事、授权代表、计票人和监票人；
- 会议地点、普通地点、通讯地址和地名；
- 手机、座机、邮箱；
- 合法证件格式，以及证件语境中 15—20 位长号码；
- 年份、具体日期；
- 人数（人/位/名）、股份数、票数、表决权总数、比例；
- 服务费、报价、贷款金额、价格、薪酬津贴；
- 服务期限、合同期限；
- 登记编号、备案编号和基金管理人登记编号。

保护器优先排除法规修订/施行年份、法定比例、利率、注册资本、罚款等常见非项目变量。保护器不是最终结论，律师仍可在本地补充精确词项。

## 7. 敏感表格

每张表生成 `T0001` safe ID，只显示部件范围、行数和识别到的合计行编号。逐表允许：

- `term-only`：只执行 occurrence 替换；
- `clear-detail-rows`：保留第一行表头及含“合计/总计/总数”的行，清空其他行的文本节点。

清空操作不得删除行列、合并关系、宽度、边框、底纹或段落格式。未逐表确认不得清空。

## 8. 元数据

只改 `docProps`：

- `creator`、`lastModifiedBy` 设为 `yangyc`；
- 标题、主题、关键词、描述、公司、管理者、模板和 hyperlink base 清空；
- 自定义属性名称改为安全编号，值按类型清空或归一化；
- created、modified、lastPrinted 固定为 `2000-01-01T00:00:00Z`；
- revision 归一化为 `1`。

序列化前注册源 namespace，确保 `dcterms:W3CDTF` 等 lexical QName 的前缀仍有声明。

## 9. 残留审计

输出成功前同时检查：

- 正文、表格、页眉页脚、脚注尾注和批注；
- 已确认原值；
- 电话、邮箱和证件长号码；
- 经确认的年份、金额、股份数、票数和比例；
- 修订节点、批注、继续跟踪；
- 文本框、宏、嵌入对象和外部关系；
- core/app/custom 元数据。

支持范围内残留不为零不得输出成功。

## 10. Word 原生闸门

Mac v1.0 使用 Word 自有容器下的安全编号暂存副本，避免扩大文件访问授权：

- DOCX 验证：Word 原生打开，出现修复提示、权限提示、超时或自动化失败即失败；
- 修订清洁：Word 原生命令处理暂存副本，清洁后复制回受控工作区；
- PDF：JXA 调用 Word `SaveAs`，`FileFormat=17`（PDF），输出必须以 `%PDF` 开头且非空；
- 清理：只处理当前 safe ID 文档和锁文件，不关闭其他 Word 文档。

LibreOffice 可做辅助渲染，但永远不能替代 Word 成功闸门。

## 11. 批量 manifest

`redaction_batch.local.json` 示例：

```json
{
  "files": [
    {"safe_id": "SAFE-001", "path": "/absolute/local/path/copy-1.docx"},
    {"safe_id": "SAFE-002", "path": "/absolute/local/path/copy-2.docx"}
  ]
}
```

要求：

- owner-only 权限；
- 1—30 条；
- safe ID 只含大写字母、数字、下划线或短横线；
- 路径必须为绝对、本地、非符号链接 DOCX；
- 不接受目录或递归扫描；
- manifest 不复制进 reports、不进 Git。

按 manifest 顺序逐份处理，不并发读取正文。单份失败不覆盖原件，其余文件继续并记录 safe 状态。

## 12. 集中复核册

每份成功 DOCX 由 Word 原生导出 PDF。复核册包含：

1. 动态封面：文件数、正文总页数、人工复核提示；
2. 每份 safe ID 分隔页；
3. 对应 Word PDF 正文；
4. `page_map.json`：safe ID、分隔页、正文起止页和正文页数。

复核册和 page map 不含原始文件名、路径、客户名称、候选值或表格内容。

## 13. 安全报告

`batch_summary.json` 只允许：

- safe ID；
- 成功、失败、取消数量；
- 安全状态和错误码；
- Word PDF、复核册状态；
- 残留统计和人工复核提示。

不得包含原始值、文件名、路径、路径哈希、精确大小、候选词、表格内容、run 坐标或 manifest 映射。

## 14. 发布边界

v1.0 只宣称 Mac Word 支持。Windows COM、PDF 真 redaction、XLSX、图片 OCR、宏/OLE 和图表文字在完成真实验收前不得宣称支持。
