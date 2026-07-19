# Legal Network Verification

`legal-network-verification` 是一份平台中立的中国法律公开网络核查 Skill。它用于经授权的公司或自然人公开信息查询、同名排除、网页区域截图、时间水印和两层工作底稿。

核心 Skill 位于 `skills/legal-network-verification/`。`.codex-plugin/plugin.json` 仅用于 Codex 发现和安装，不承载业务逻辑。

## Agent 兼容

- **Codex**：可安装外层插件，也可直接加载核心 Skill。
- **WorkBuddy**：直接导入 `skills/legal-network-verification/`；不能自动识别时，手工指定其 `SKILL.md`。
- **其他 Agent**：支持 Agent Skills 目录规范的可直接导入；否则手工加载 `SKILL.md`。

完整流程需要本地文件读写、Python 3.10+及可交互浏览器。只有对话能力的 Agent 只能制作查询计划，不得声称已完成查询。具体降级路径见核心 Skill 内的 `references/agent-compatibility.md`。

### v1.3.0-beta（2026-07-19）

- **范围先行**：schema 1.1 用 `query_scope` 固化用户已确认的主体、事项、网站、域名、期间和查询方式；`scope-preview` 只读显示范围矩阵。范围外查询和未确认范围均失败关闭。
- **默认口径**：自然人默认仅五类负面线索；公司默认十类负面线索及官方来源范围。行业、监管、税务等扩展来源须由用户逐项确认，Skill 不会从公司自动扩展关联自然人。
- **身份列边界**：公司代码仍字段感知校验；公司只有明确选择后可自动写入已验证代码。final 可保留第三列为空，完整身份证号码只能由用户在审计后自行补填，补填版永不再由 Skill 处理。
- **兼容性**：schema 1.0 仅可读取和构建，不能新建、迁移或追加 schema 1.1 查询。真实网站、外部 Agent 运行时和发布状态均未由本候选验证。

## 本地环境

使用当前 Agent 可用的 Python 解释器安装 `requirements.txt`，然后运行：

```text
python scripts/network_workpaper.py doctor --browser unknown
python scripts/network_workpaper.py scope-preview network-verification.json
```

macOS/Linux 通常使用 `python3`；Windows 可使用 `py -3`。具体命令应以当前 Agent 配置的 Python 为准，不要安装到未经授权的系统环境。

## 输出

- `01-内部底稿/`：结构化运行文件、逐主体 Markdown 底稿及脱敏水印截图。
- `02-正式记录/`：基于用户模板或随包通用匿名模板的 DOCX 查询记录。

没有可用模板时，草稿模式只生成 Markdown；正式模式会明确报错。

## 个人信息

完整身份证号只能由用户在目标网站中手工输入，不得发送到对话或交由 Agent 读取。所有留存成果统一遮挡末8位。详见 [PRIVACY.md](PRIVACY.md)。

公司统一社会信用代码仅能写入公司主体的 `credit_code` 字段，并须通过字符集和校验位验证。审计含该代码的成果时必须同时提供当前运行文件：

```text
python scripts/network_workpaper.py artifact-audit OUTPUT_DIR --run-file RUN.json
```

## 法律边界

本 Skill 用于记录公开信息查询过程，不构成针对任何具体事项的法律意见，也不替代身份证明、主管机关文件、当事人声明和其他书面核验。

## License

Apache License 2.0。第三方依赖详见 `THIRD_PARTY_NOTICES.md`。
