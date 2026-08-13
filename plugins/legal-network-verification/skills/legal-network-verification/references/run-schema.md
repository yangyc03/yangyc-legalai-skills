# `network-verification.json` Schema 1.2 规范

## 目录

1. [顶层结构](#顶层结构)
2. [`subjects[]`](#subjects)
3. [`query_scope`](#query_scope)
4. [`query_executions[]`](#query_executions)
5. [`queries[]`](#queries)
6. [跨字段门禁](#跨字段门禁)
7. [兼容、构建与审计](#兼容构建与审计)

## 顶层结构

Schema 1.2 将浏览器执行状态与法律查询结果分离。`query_executions[]` 记录每个已确认范围项的执行状态；`queries[]` 只记录已完成或终态受限/失败的法律结果。

```json
{
  "schema_version": "1.2",
  "run": {
    "run_id": "NQ-20260812-001",
    "timezone": "Asia/Shanghai",
    "profile": "general-company",
    "project": {
      "name": "示例项目",
      "short_name": "示例",
      "matter": "专项法律意见网络核查",
      "period": "2023年1月1日至查询日"
    },
    "formal_record": {
      "query_date": "2026年8月12日",
      "query_location": "",
      "query_people": []
    }
  },
  "query_scope": {
    "status": "user_confirmed",
    "confirmed_at": "2026-08-12T10:00:00+08:00",
    "selection_mode": "default_profile",
    "items": [
      {
        "scope_item_id": "SCOPE-001",
        "subject_id": "SUB-001",
        "matter_category_id": "company.litigation_judgments",
        "site_id": "court-docs",
        "site_name": "中国裁判文书网",
        "site_basis": "default_profile",
        "allowed_domains": ["court.gov.cn"],
        "query_term_mode": "exact_subject_name",
        "conditions": [
          {"condition_id": "COND-001", "field": "period", "value": "2023年1月1日至查询日"}
        ]
      }
    ]
  },
  "subjects": [
    {
      "subject_id": "SUB-001",
      "type": "company",
      "name": "示例公司",
      "role": "核查对象",
      "formal_identifier_mode": "user_fill"
    }
  ],
  "query_executions": [
    {
      "execution_id": "EXEC-001",
      "scope_item_id": "SCOPE-001",
      "browser_surface": "chrome",
      "browser_selection": "user_explicit",
      "execution_state": "completed",
      "access_state": "authenticated",
      "submit_state": "submitted",
      "block_reason": "",
      "attempts": [
        {"sequence": 1, "at": "2026-08-12T10:20:00+08:00", "event": "access_probe"},
        {"sequence": 2, "at": "2026-08-12T10:21:00+08:00", "event": "query_submitted"},
        {"sequence": 3, "at": "2026-08-12T10:21:05+08:00", "event": "result_region_loaded"},
        {"sequence": 4, "at": "2026-08-12T10:21:06+08:00", "event": "screenshot"}
      ],
      "completion_evidence": {
        "query_submitted": true,
        "result_region_loaded": true,
        "result_signal": "explicit_empty_state",
        "observed_at": "2026-08-12T10:21:06+08:00",
        "evidence_id": "NQ-01-01-01",
        "capture_review": "conditions_and_result_visible"
      },
      "safe_resume_location": "https://wenshu.court.gov.cn/"
    }
  ],
  "queries": [
    {
      "evidence_id": "NQ-01-01-01",
      "execution_id": "EXEC-001",
      "scope_item_id": "SCOPE-001",
      "subject_id": "SUB-001",
      "matter_category_id": "company.litigation_judgments",
      "site_id": "court-docs",
      "site_name": "中国裁判文书网",
      "query_term_mode": "exact_subject_name",
      "condition_ids": ["COND-001"],
      "conditions": [
        {"condition_id": "COND-001", "field": "period", "value": "2023年1月1日至查询日"}
      ],
      "url": "https://wenshu.court.gov.cn/",
      "query_time": "2026-08-12T10:21:06+08:00",
      "query_terms": "完整主体名称：示例公司",
      "filters": "period：2023年1月1日至查询日",
      "status": "no_match_displayed",
      "result_summary": "页面明确显示本次条件下暂无符合条件的结果",
      "identity_assessment": "未显示可确认匹配",
      "screenshot_path": "01-内部底稿/截图/NQ-01-01-01_示例.png",
      "capture_kind": "watermarked_page_only",
      "follow_up": ""
    }
  ],
  "opinion_wording_requested": false
}
```

## `subjects[]`

| 字段 | 要求 |
| --- | --- |
| `subject_id` | 唯一编号，如 `SUB-001` |
| `type` | `natural_person` 或 `company` |
| `name` | 完整姓名或登记名称 |
| `role` | 本项目角色 |
| `associated_entity` | 关联机构，可为空 |
| `credit_code` | 公司正式记录使用的统一社会信用代码；自然人不得填写 |
| `formal_identifier_mode` | `user_fill`（默认）或公司明确选择的 `auto_fill_company_credit_code`；自然人只能为 `user_fill` |
| `masked_id_number` | 可选既有脱敏辅助字段；仅接受前10位数字加末8位星号，不能替代正式记录中的人工填写位置 |

禁止任何位置出现 `id_number`、`identity_number`、`birth_date`、`mobile`、`email`、账号、Profile 名、Session ID、Cookie、Token、密码、登录页原文、浏览器存储或原始页面/工具响应。

## `query_scope`

- `status` 必须为 `user_confirmed` 才能查询；`prepare` 可以生成 `pending_confirmation` 空骨架。
- `selection_mode` 只允许 `default_profile`、`custom`、`default_plus_custom`。
- 每个范围项固定包含 `scope_item_id`、`subject_id`、`matter_category_id`、站点字段、`allowed_domains[]`、`query_term_mode` 和结构化 `conditions[]`。
- `site_basis` 只允许 `default_profile`、`user_specified`、`user_confirmed_suggestion`。
- 每项至少包含一个非空 `field=period` 条件。未知键和自由文本范围一律拒绝。
- `query_term_mode` 只允许 `exact_subject_name`、`company_credit_code`；`user_manual_full_id` 只表示用户本人在目标网站手工输入完整号码；Agent 和 Skill 不得读取、填充、粘贴或提交，实际号码不得落盘。

## `query_executions[]`

每个范围项最多一条执行记录；`execution_id` 和 `scope_item_id` 均须唯一。最终构建要求每个已确认范围项都有执行记录。

### 字段与枚举

| 字段 | 允许值或要求 |
| --- | --- |
| `execution_id` | 唯一编号，如 `EXEC-001` |
| `scope_item_id` | 必须引用已确认范围项 |
| `browser_surface` | `in_app_browser`、`chrome`、`computer_use` |
| `browser_selection` | `user_explicit`、`automatic_primary`、`automatic_fallback` |
| `execution_state` | `pending_user_action`、`ready`、`running`、`completed`、`blocked`、`failed` |
| `access_state` | `unknown`、`not_required`、`authenticated`、`login_required`、`session_expired`、`human_verification_required`、`limited` |
| `submit_state` | `not_submitted`、`submitted`、`unknown` |
| `block_reason` | 空值或下列标准阻断码 |
| `attempts[]` | 只含连续序号、含时区时间和事件码 |
| `completion_evidence` | 结构化完成证据；不能保存页面原文 |
| `safe_resume_location` | `scheme + host + path`；禁止账号、查询参数和片段，且域名必须在范围内 |

`block_reason` 允许：

```text
login_required
session_expired
human_verification_required
rate_limited
permission_required
paywall
site_maintenance
network_error
tool_error
submit_state_unknown
user_stopped
```

`blocked`、`failed` 必须有阻断码；`ready`、`running`、`completed` 不得保留阻断码。`rate_limited`、`permission_required`、`paywall`、`site_maintenance` 和明确的 `user_stopped` 只能映射 `blocked/access_limited`；`network_error`、`tool_error` 只能映射 `failed/failed`。登录、会话过期和人工验证保持可恢复的 `pending_user_action`，不能自行转为终态；只有最后事件为 `user_stopped` 才可终止。`pending_user_action` 必须保留安全恢复位置。

### `attempts[]`

每项只允许 `sequence`、`at`、`event`。序号从1连续递增，时间不得倒序。事件码为：

```text
access_probe
browser_fallback
handoff_started
user_resume_confirmed
resume_safety_check_passed
page_read
click
screenshot
query_submitted
result_region_loaded
login_required
session_expired
human_verification_required
rate_limited
permission_required
paywall
site_maintenance
network_error
tool_error
explicit_resubmit_confirmed
sensitive_result_review_confirmed
user_stopped
```

- `user_explicit` 禁止 `browser_fallback`；`automatic_primary` 必须没有回退，`automatic_fallback` 必须恰有一次 `browser_fallback`，因此自动选择最多回退一次。
- 登录、会话过期或人工验证事件开启人工接管；接管关闭前不得出现 `access_probe`、`browser_fallback`、`page_read`、`click`、`screenshot`、`query_submitted` 或 `result_region_loaded`，确保没有隐性轮询、浏览器切换或结果读取。
- 只有 `user_resume_confirmed` 或 `user_stopped` 能关闭接管；前者仅在用户发送固定恢复语后记录。`user_resume_confirmed` 后必须紧接 `resume_safety_check_passed`，否则不得继续页面读取、提交或截图。
- 查询提交后出现登录、验证、网络错误或工具错误时必须 `submit_state=unknown`；登录/验证恢复需先完成安全复核，任何再次提交前都必须先有 `explicit_resubmit_confirmed`。
- `browser_fallback` 只能发生在首次提交和人工接管之前。
- 当前提交周期的最后一个 `result_region_loaded` 后才能记录 `screenshot`；新的结果加载会使此前截图或敏感目视复核失效。敏感不留图分支只能记录由用户或律师完成的 `sensitive_result_review_confirmed`。

### `completion_evidence`

| 字段 | 要求 |
| --- | --- |
| `query_submitted` | 布尔值，必须与 `query_submitted` 尝试事件一致 |
| `result_region_loaded` | 布尔值，必须与 `result_region_loaded` 尝试事件一致 |
| `result_signal` | `not_observed`、`explicit_zero_count`、`explicit_empty_state`、`matching_results_displayed`、`same_name_candidates_displayed`、`not_applicable` |
| `observed_at` | 已观察结果必须填含时区时间；未观察时为空 |
| `evidence_id` | 完成结果关联的证据编号；未观察时为空 |
| `capture_review` | `not_reviewed`、`conditions_and_result_visible`、`sensitive_no_capture` |

未完成执行只能保留 `result_signal=not_observed`、空时间、空证据编号和 `capture_review=not_reviewed`。`completed` 必须有已观察结果信号；除 `not_applicable` 外，还必须同时满足查询已提交、结果区已加载和 `submit_state=submitted`。

`capture_review` 是人工复核状态，不是截图 OCR 结论。普通页面使用 `conditions_and_result_visible`，由执行 Agent 确认当次页面并由律师目视复核截图；页面仍显示完整身份证号码而禁止 Agent 读取或截图时，使用 `sensitive_no_capture`，由用户或律师直接目视复核。普通截图必须由 `watermark_capture.py` 在加水印前通过启发式源图内容门，并以 PNG 元数据绑定证据编号、观察时间、主体、网站和截图类型；这仍不是网页真实性的密码学证明，也不替代人工目视复核。

## `queries[]`

Schema 1.2 在既有字段基础上新增必填 `execution_id`。`scope_item_id`、站点、事项、查询模式和结构化条件必须与执行记录及已确认范围项精确一致。一个执行记录最多对应一个法律结果。

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

- `completed` 不能对应 `access_limited` 或 `failed`。
- `blocked` 必须对应 `access_limited`；`failed` 必须对应 `failed`。
- 非终态执行不能有法律结果；终态执行必须恰有一条法律结果。
- `pending_user_action` 只存在于执行层，不能伪造 `queries[]` 记录。
- `identity_match` 对应 `matching_results_displayed`；`same_name_candidates` 对应 `same_name_candidates_displayed`；`not_applicable` 对应 `not_applicable`。
- Schema 1.2 的 `access_limited`、`failed` 采用封闭的固定非否定性 `result_summary`、`identity_assessment` 和 `follow_up`，不得用自由文本夹带“未检出匹配”等同义结论；正式记录由状态生成固定披露文本。

截图路径必须相对 `workpaper_root`、不得包含 `..`，并指向实际文件。有截图时 `capture_kind=watermarked_page_only`。敏感页面不留存时使用空路径和 `capture_kind=not_retained_sensitive_page`，并写明“因敏感信息保护未留存该页面截图”。登录页、密码页、验证码页、二维码页和全应用截图均不得作为证据。

## 跨字段门禁

### `no_match_displayed`

必须全部满足：

1. 执行为 `completed`，访问状态为 `not_required` 或 `authenticated`；
2. `submit_state=submitted`，且尝试轨迹与完成证据均证明已经提交、结果区已经加载；
3. `result_signal` 为 `explicit_zero_count` 或 `explicit_empty_state`；
4. 完成证据与查询的 `evidence_id` 相同；
5. 默认必须走普通证据路径：`capture_review=conditions_and_result_visible`、查询使用 `watermarked_page_only`、存在当前提交周期内生成的 PNG 网页区域截图，且源图在水印前通过非空白/非近纯色启发式门、元数据与证据编号/时间/主体/网站一致；
6. 只有自然人 `user_manual_full_id` 查询的结果页仍显示完整身份证号码、无法安全留存页面时，才允许固定窄例外：`capture_review=sensitive_no_capture`、`capture_kind=not_retained_sensitive_page`、截图路径为空，结果摘要或补核事项含“因敏感信息保护未留存该页面截图”，并在最后结果加载后记录 `sensitive_result_review_confirmed`；不得把公司查询、普通姓名查询、登录页、验证码页、截图失败或一般访问受限改记为本例外；
7. 普通路径由执行 Agent 与律师目视确认查询条件和结果状态同屏可辨；敏感路径由用户或律师直接目视确认，Agent 不读取敏感页面。

空白页、空表格、加载中页面、登录页中的数字“0”、用户只表示“已登录”或仅有结构化布尔值都不能通过该门禁。

### 最终 DOCX

`formal-mode=final` 必须满足：

- 范围已确认且非空；
- 每个范围项都有执行记录；
- 全部执行为 `completed`、`blocked` 或 `failed`；
- 不存在 `submit_state=unknown`；
- 查询地点和查询人完整；
- 其他主体身份及模板门禁均通过。

`pending_user_action`、`ready`、`running`、缺少执行记录或提交状态不明均拒绝最终构建。用户明确停止某站后，可记录 `user_stopped`，转为 `blocked` 和 `access_limited`，但不能改写成零结果。

`completion-preview` 是只读预览，必须提供 `workpaper_root`，输出 `scope_status`、`total_scope_items`、各状态 `counts`、`blocking_items[]`、`execution_ready`、`readiness_issues[]` 和 `final_ready`；`final_ready` 会实际调用 final 校验，因此正式字段缺失、截图文件/元数据不合格或任何执行门未通过时均为 false。它不修改运行文件，也不检查 Word 模板本身是否可读。

## 兼容、构建与审计

- `prepare` 默认且仅创建 Schema 1.2；Schema 1.0、1.1 只兼容校验和构建，不自动迁移，也不得追加新查询。
- `scope-preview` 支持 Schema 1.1 和 1.2；Schema 1.2 新查询必须先通过确认范围门禁。
- `build` 默认 `--layout two-layer`；内部 Markdown 写入 `01-内部底稿`，正式 DOCX 写入 `02-正式记录`。草稿文件名含 `_草稿`，最终文件名不含。
- 草稿可以显示待登录或待验证；最终构建遵守上述严格门禁。正式正文不输出 `identity_assessment` 原文、技术状态码、账号或浏览器信息。
- `url` 保存经安全归一化的当次 HTTP(S) 页面位置，域名必须在确认范围内；Schema 1.2 不持久化查询参数、分号参数或片段，并在有界百分号解码后拒绝账号信息及 token/session/auth 等凭证形态路径。正式 DOCX 超链接目标不得改写。
- `artifact-audit` 只扫描本次专用的 `01-内部底稿`、`02-正式记录` 或一个明确成果文件；不扫描客户项目根目录。它不做图片 OCR，所有保留图片仍须人工目视确认。
- `formal_identifier_mode=user_fill` 的完整号码/代码位置在 Skill 审计和逐页渲染后由用户自行填写；补填后的归档版不得再交给 Skill 读取、渲染或审计。
