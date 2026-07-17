# `network-verification.json` 规范

## 顶层结构

```json
{
  "schema_version": "1.0",
  "run": {
    "run_id": "NQ-20260716-001",
    "timezone": "Asia/Shanghai",
    "profile": "private-fund",
    "project": {
      "name": "示例项目",
      "short_name": "示例",
      "matter": "专项法律意见网络核查",
      "period": "2023年1月1日至查询日"
    },
    "formal_record": {
      "query_date": "2026年7月16日",
      "query_location": "",
      "query_people": []
    }
  },
  "subjects": [],
  "queries": [],
  "opinion_wording_requested": false
}
```

## `subjects[]`

| 字段 | 要求 |
| --- | --- |
| `subject_id` | 唯一编号，如 `SUB-001` |
| `type` | `natural_person`或`company` |
| `name` | 完整姓名或登记名称 |
| `role` | 本项目角色 |
| `associated_entity` | 关联机构，可为空 |
| `credit_code` | 公司正式记录使用的统一社会信用代码；自然人不得填写 |
| `masked_id_number` | 自然人正式记录使用的脱敏身份证号码；必须为前10位数字加末8位星号，如 `1101011990********` |

禁止字段包括 `id_number`、`identity_number`、`birth_date`、`mobile`、`email`、`cookie`、`token`、`password` 和 `raw_response`。

## `queries[]`

| 字段 | 要求 |
| --- | --- |
| `evidence_id` | 唯一编号，如 `NQ-01-02-01` |
| `subject_id` | 必须关联已定义主体 |
| `site_id` | 配置内或自定义网站编号 |
| `site_name` | 网站正式名称 |
| `url` | 当次实际HTTP(S)网址 |
| `query_time` | 含时区的ISO 8601时间 |
| `query_terms` | 不含敏感值的条件描述 |
| `filters` | 期间、栏目和交叉条件描述 |
| `status` | 规定状态之一 |
| `result_summary` | 客观结果摘要 |
| `identity_assessment` | 身份匹配或同名排除判断 |
| `screenshot_path` | 相对工作底稿根目录的安全路径；受限或失败可为空 |
| `capture_kind` | 有截图时为 `watermarked_page_only`；因页面仍显示完整身份证号码而不留存截图时为 `not_retained_sensitive_page` |
| `follow_up` | 待补核事项，可为空 |

允许状态：

```text
identity_match
no_match_displayed
same_name_candidates
not_applicable
access_limited
failed
pending_review
```

- `no_match_displayed`：已完成查询及必要的身份要素核对，未发现与查询对象相符的负面记录；已完成同名排除的也使用本状态。
- `same_name_candidates`：已检索出同名记录但尚未完成排除，不得生成“未发现负面记录”结论。

## 校验规则

1. 编号唯一，查询必须关联已定义主体。
2. URL必须使用HTTP(S)，时间必须含时区。
3. 截图路径必须为相对路径，不得包含`..`；有截图时文件必须存在。
4. 有截图时`capture_kind`必须为`watermarked_page_only`。因敏感信息保护不留存截图时，`screenshot_path`必须为空，`capture_kind`必须为`not_retained_sensitive_page`，且`result_summary`或`follow_up`应明确说明“因敏感信息保护未留存该页面截图”。
5. `no_match_displayed`必须使用“未显示”、“未发现可确认匹配”或“未发现与查询对象相符的负面记录”等有限表述。
6. `access_limited`、`failed`不得包含“未发现记录”或其他零结果结论。
7. 全部文本禁止出现完整中国居民身份证号码、凭证字段、授权确认原文和绝对否定表述；自然人`masked_id_number`只接受前10位数字加末8位星号。
8. `formal-mode=final`时，查询地点、查询人以及公司统一社会信用代码/自然人脱敏身份证号码必须完整；内部Markdown不读取查询地点和查询人。
9. `output_dir`必须位于`workpaper_root`内；输出到子目录时，脚本按该目录自动重算截图相对链接。
10. 正式 DOCX 不输出 `identity_assessment` 字段名或原文；该字段只供内部 Markdown 保留身份匹配、同名排除及限制说明。

## 构建布局与网址规则

- `schema_version` 在 v1.2 继续使用 `1.0`，不新增身份证号码、授权确认或网址字段。
- `build` 默认使用 `--layout two-layer`：内部 Markdown 写入 `01-内部底稿`，正式 DOCX 写入 `02-正式记录`；`--layout flat` 保留同目录输出能力。
- `formal-mode=draft` 的 DOCX 文件名增加 `_草稿`；`formal-mode=final` 不带草稿标识。
- `url` 必须保存当次完整实际网址。正式 DOCX 的超链接目标不得改写；显示文字仍为完整网址，仅允许加入不改变可见内容的断行机会。
- 内部 Markdown 的截图链接应相对于 Markdown 实际所在目录计算；两层布局下通常链接至同层 `截图/` 目录。
