# Legal Network Verification

`legal-network-verification` 是一份平台中立的中国法律公开网络核查 Skill。它用于经授权的公司或自然人公开信息查询、同名排除、网页区域截图、时间水印和两层工作底稿。

核心 Skill 位于 `skills/legal-network-verification/`。`.codex-plugin/plugin.json` 仅用于 Codex 发现和安装，不承载业务逻辑。

## Agent 兼容

- **Codex**：可安装外层插件，也可直接加载核心 Skill。
- **WorkBuddy**：直接导入 `skills/legal-network-verification/`；不能自动识别时，手工指定其 `SKILL.md`。
- **其他 Agent**：支持 Agent Skills 目录规范的可直接导入；否则手工加载 `SKILL.md`。

完整流程需要本地文件读写、Python 3.10+及可交互浏览器。只有对话能力的 Agent 只能制作查询计划，不得声称已完成查询。具体降级路径见核心 Skill 内的 `references/agent-compatibility.md`。

### v2.0.0-beta（2026-08-13）

- **登录可恢复、密码不托管**：可以复用既有登录态；登录、会话过期、密码管理器、Touch ID、MFA 和验证码全部由用户操作。人工接管期间页面零读取、零截图、零操作，固定恢复语后仍须做同一浏览器、域名和范围项的安全复核。
- **执行与结果分离**：schema 1.2 用 `query_executions[]` 记录逐范围项的登录、验证、提交和完成状态；`queries[]` 只承载已完成或终态受限/失败的法律结果。schema 1.0、1.1 仅兼容读取和构建。
- **零结果证据门**：只有已提交、结果区已加载、页面明确显示零结果且当前提交周期内存在同编号 PNG 网页区域水印截图时，才能记录 `no_match_displayed`。空白页、登录页中的“0”、空表格和用户口头表示“已登录”均不成立。
- **严格最终门禁**：`completion-preview` 会结合正式字段和证据文件返回 `final_ready`；待登录、待验证、执行中或提交状态不明时拒绝最终 DOCX。
- **默认口径**：自然人默认仅五类负面线索；公司默认十类负面线索及官方来源范围。行业、监管、税务等扩展来源须由用户逐项确认，Skill 不会从公司自动扩展关联自然人。
- **身份列边界**：公司代码仍字段感知校验；公司只有明确选择后可自动写入已验证代码。final 可保留第三列为空，完整身份证号码只能由用户在审计后自行补填，补填版永不再由 Skill 处理。
- **候选边界**：本地匿名测试不等于真实网站、外部 Agent 运行时或公开发布验证；这些状态必须分别留证。

## 本地环境

使用当前 Agent 可用的 Python 解释器安装 `requirements.txt`，然后运行：

```text
python scripts/network_workpaper.py doctor --browser unknown
python scripts/network_workpaper.py scope-preview network-verification.json
python scripts/network_workpaper.py completion-preview network-verification.json --workpaper-root WORKPAPER_ROOT
```

macOS/Linux 通常使用 `python3`；Windows 可使用 `py -3`。具体命令应以当前 Agent 配置的 Python 为准，不要安装到未经授权的系统环境。

## 输出

- `01-内部底稿/`：结构化运行文件、逐主体 Markdown 底稿及脱敏水印截图。
- `02-正式记录/`：基于用户模板或随包通用匿名模板的 DOCX 查询记录。

没有可用模板时，草稿模式只生成 Markdown；正式模式会明确报错。

## 个人信息

完整身份证号只能由用户在目标网站中手工输入和提交，不得发送到对话或交由 Agent 读取、粘贴或自动提交。所有由 Agent 或 Skill 留存、读取或继续处理的成果统一遮挡末8位；用户在全部技术检查后自行补填的归档版不得再交给 Agent 或 Skill。详见 [PRIVACY.md](PRIVACY.md)。

公司统一社会信用代码仅能写入公司主体的 `credit_code` 字段，并须通过字符集和校验位验证。审计含该代码的成果时必须同时提供当前运行文件：

```text
python scripts/network_workpaper.py artifact-audit OUTPUT_DIR --run-file RUN.json
```

## 法律边界

本 Skill 用于记录公开信息查询过程，不构成针对任何具体事项的法律意见，也不替代身份证明、主管机关文件、当事人声明和其他书面核验。

## License

Apache License 2.0。第三方依赖详见 `THIRD_PARTY_NOTICES.md`。
