# Legal Network Verification

`legal-network-verification` 是一份平台中立的中国法律公开网络核查 Skill。它用于经授权的公司或自然人公开信息查询、同名排除、网页区域截图、时间水印和两层工作底稿。

核心 Skill 位于 `skills/legal-network-verification/`。`.codex-plugin/plugin.json` 仅用于 Codex 发现和安装，不承载业务逻辑。

## Agent 兼容

- **Codex**：可安装外层插件，也可直接加载核心 Skill。
- **WorkBuddy**：直接导入 `skills/legal-network-verification/`；不能自动识别时，手工指定其 `SKILL.md`。
- **其他 Agent**：支持 Agent Skills 目录规范的可直接导入；否则手工加载 `SKILL.md`。

完整流程需要本地文件读写、Python 3.10+及可交互浏览器。只有对话能力的 Agent 只能制作查询计划，不得声称已完成查询。具体降级路径见核心 Skill 内的 `references/agent-compatibility.md`。

### v1.2.1-beta 候选修复（2026-07-18）

- **字段感知校验**：公司主体 `credit_code` 通过统一社会信用代码字符集和校验位验证后，不再被18位身份证号码模式误拦截；自然人字段及其他文本位置仍保持失败关闭。
- **成果审计**：成果中需要保留公司统一社会信用代码时，必须使用 `artifact-audit --run-file RUN.json` 显式提供当前已验证运行文件；只精确放行其中的公司代码。无运行文件上下文、其他18位数字或文件名中的号码仍被拦截。
- **版本边界**：本修复不修改 schema 1.0、模板、查询范围或完整身份证号码人工接管规则。`v1.2.0-beta` 的匿名命令行、DOCX、隐私审计、Codex 新任务加载及 WorkBuddy 导入验收记录继续作为前一版本历史；本候选仍须完成本版本测试和发布门禁。
- **v1.2.0 WorkBuddy 5.2.6 历史验收**：前一版本已实际导入核心目录，并在新任务中自动加载 `legal-network-verification`，匿名测试正确拒绝在未查询时声称完成，并正确执行完整身份证号码的人工接管边界。v1.2.1-beta 及 WorkBuddy 内的真实网页截图和 DOCX 全流程尚未单独实跑。
- **其他 Agent**：按能力前提设计，尚未逐一完成运行时验证。

## 本地环境

使用当前 Agent 可用的 Python 解释器安装 `requirements.txt`，然后运行：

```text
python scripts/network_workpaper.py doctor --browser unknown
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
