# `network-verification.json` 规范

## 版本与兼容

新建运行文件固定为 `schema_version: "1.1"`。schema 1.0 只允许读取、校验和构建既有文件；不得由 `prepare` 创建、升级或追加新查询。

## 范围确认

schema 1.1 顶层必须有以下 `query_scope`。它是查询前的只读确认对象，而不是 Agent 自动扩展范围的建议清单。

```json
{
  "status": "user_confirmed",
  "confirmed_at": "2026-07-19T10:00:00+08:00",
  "selection_mode": "default_plus_custom",
  "items": [{
    "scope_item_id": "SCOPE-001",
    "subject_id": "SUB-001",
    "matter_category_id": "company.litigation_judgments",
    "site_id": "court-docs",
    "site_name": "中国裁判文书网",
    "site_basis": "default_profile",
    "allowed_domains": ["court.gov.cn"],
    "query_term_mode": "exact_subject_name",
    "conditions": [{"condition_id": "COND-001", "field": "period", "value": "2023年1月1日至查询日"}]
  }]
}
```

- `status` 只能为 `pending_confirmation` 或 `user_confirmed`；只有后者可以包含或执行查询。
- `selection_mode` 只能为 `default_profile`、`custom` 或 `default_plus_custom`。默认模式不能混入自定义来源；自定义模式不能混入默认来源。
- 每项必须固定列明主体、事项类别、网站、允许域名、查询方式及至少一项非空 `period` 条件。未知字段一律拒绝。
- `site_basis` 只能是 `default_profile`、`user_specified` 或 `user_confirmed_suggestion`。`allowed_domains` 必须是纯域名。
- `query_term_mode` 只能是 `exact_subject_name` 或公司使用的 `company_credit_code`。不得记录完整身份证号码或其查询方式。

`scope-preview RUN.json` 只输出主体—事项—网站—域名—方式—条件矩阵，供用户审阅；它不进行联网查询，也不改变运行文件。

## 主体与正式身份列

`subjects[]` 继续使用 `subject_id`、`type`、`name`、`role`、`associated_entity`、`credit_code` 和可选 `masked_id_number`。

- 公司 `credit_code` 必须通过统一社会信用代码字符集和校验位验证；自然人不得填写该字段。
- `formal_identifier_mode` 默认为 `user_fill`。公司仅在用户明确选择 `auto_fill_company_credit_code` 后才可填入已验证代码；自然人只能为 `user_fill`。
- `masked_id_number` 仅可为既有脱敏辅助显示（前10位数字加末8位星号），不能替代正式记录的人工填写位置。
- `formal-mode=final` 仍要求查询日期、地点和查询人，但第三列可为空。Skill 完成审计、结构检查和逐页渲染后，用户可自行补填完整身份证号码；补填文件不得再次交给 Skill 处理。

## `queries[]`

schema 1.1 每项查询必须有 `scope_item_id`、`matter_category_id`、`query_term_mode`、`condition_ids` 和 `conditions`。其主体、事项、网站、名称、方式、条件及 URL 域名必须与所引范围项精确相符；不相符即拒绝。`query_time` 不得早于 `query_scope.confirmed_at`。

schema 1.1 的查询项不得保存 `query_terms` 或 `filters` 自由文本。内部底稿和正式记录中的“查询条件”“筛选条件”由脚本根据已确认的 `query_term_mode`、主体类型和 `conditions` 确定性生成。schema 1.0 既有文件继续读取其原有 `query_terms` 和 `filters`。查询结果、截图、措辞和敏感信息规则继续按本 Skill 的既有规范执行。

## 通用边界

禁止字段包括 `id_number`、`identity_number`、`birth_date`、`mobile`、`email`、`cookie`、`token`、`password` 和 `raw_response`。全部文本禁止完整居民身份证号码、凭证、授权原文和绝对否定表述。

`build` 默认输出 `01-内部底稿/` 与 `02-正式记录/`。`artifact-audit` 只接受本次专用的生成层目录或明确成果文件；保留图片仍需人工确认不显示敏感内容。
