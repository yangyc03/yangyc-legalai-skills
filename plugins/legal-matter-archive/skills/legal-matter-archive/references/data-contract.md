# 公共数据契约

## 本地配置

配置示例见 `../configs/local-config.example.json`，结构版本固定为 `1.0`。配置由 `configure --plan` 规范化并计算模板哈希，只有令牌确认后才写入用户级目录。

## 归档请求

请求结构见 `../scripts/schemas/archive_request.schema.json`。公共顶层字段为：

- `request_schema_version`：当前为 `1.0`；
- `profile`：三种归档类型之一；
- `matter`：本事项标识、内部结案日期及可选案号、阶段；
- `source_root`（或 `source_roots`）、`output_root`：项目内相对路径，且相互分离；
- `items`：材料来源、纳入决定、顺序、类别和展示名称；
- `exclusions`：来源、理由和律师确认；
- `engagement`：主委托依据、补充协议、共享来源和范围说明；
- `summary`：可选的律师确认小结；
- `confirmations`：正式归档、范围、材料、目录、封皮、页码等确认；
- `required_categories`、`directory_groups`、`cover_fields` 等归档控制字段。

请求不得包含密码、密钥、token、凭证或外部 Agent 的专用执行指令。上游系统信息如有帮助，只能先映射为上述字段，并在本流程中重新校验。

## 路径与不变性

案件请求中的来源与输出使用 `--project-root` 下的相对路径。Tool 在 plan 中记录大小、修改时间与 SHA-256；执行前重新核对，任何变化都会使令牌失效。

本地规则和模板允许位于项目外，但配置只记录本地路径、哈希、类型与预期页数。配置不记录客户正文。

## 输出契约

计划、prepared manifest、最终 manifest、verification、delivery plan 和 invalidation plan 均使用 `scripts/schemas/` 中的 JSON Schema。不得把这些文件当作其他系统的直接写入指令。
