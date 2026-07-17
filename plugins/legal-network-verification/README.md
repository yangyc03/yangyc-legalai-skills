# Legal Network Verification

`legal-network-verification` 是一份平台中立的中国法律公开网络核查 Skill。它用于经授权的公司或自然人公开信息查询、同名排除、网页区域截图、时间水印和两层工作底稿。

核心 Skill 位于 `skills/legal-network-verification/`。`.codex-plugin/plugin.json` 仅用于 Codex 发现和安装，不承载业务逻辑。

## Agent 兼容

- **Codex**：可安装外层插件，也可直接加载核心 Skill。
- **WorkBuddy**：直接导入 `skills/legal-network-verification/`；不能自动识别时，手工指定其 `SKILL.md`。
- **其他 Agent**：支持 Agent Skills 目录规范的可直接导入；否则手工加载 `SKILL.md`。

完整流程需要本地文件读写、Python 3.10+及可交互浏览器。只有对话能力的 Agent 只能制作查询计划，不得声称已完成查询。具体降级路径见核心 Skill 内的 `references/agent-compatibility.md`。

### v1.2.0-beta 验证状态（2026-07-17）

- **通用核心**：匿名命令行测试、DOCX生成、敏感信息审计及 Word 原生渲染检查已通过。
- **Codex适配层**：清单和目录结构验证已通过；已从临时隔离本地市场安装与源码完全一致的 `v1.2.0-beta` 包，并在全新任务中确认加载插件版 Skill、版本与缓存路径。匿名测试正确拒绝在未查询时声称完成，并正确执行完整身份证号码的人工接管边界。测试后已卸载插件、移除临时市场并删除临时目录；公开发布后的安装仍应再次核对版本与源码哈希。
- **WorkBuddy 5.2.6**：已实际导入同一核心目录，并在新任务中自动加载 `legal-network-verification`，匿名测试正确拒绝在未查询时声称完成，并正确执行完整身份证号码的人工接管边界。WorkBuddy内的真实网页截图和DOCX全流程尚未单独实跑。
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

## 法律边界

本 Skill 用于记录公开信息查询过程，不构成针对任何具体事项的法律意见，也不替代身份证明、主管机关文件、当事人声明和其他书面核验。

## License

Apache License 2.0。第三方依赖详见 `THIRD_PARTY_NOTICES.md`。
