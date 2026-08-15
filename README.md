# 杨颖超律师 LegalAI Skills 与工具

Reusable Agent Skills and local productivity tools, maintained by **Yingchao Yang (杨颖超律师)**.

本仓库用于发布经过筛选、脱敏、测试和许可核验的中国法律实务 Agent Skills，以及少量边界明确的本机效率工具。项目强调本地优先、来源可追踪、人工复核和明确的能力边界。

## 项目原则

- AI 适应律师工作流，而不是改变律师的基本工作习惯。
- 客户资料、案件材料、工作底稿、密钥和本机配置不得进入本仓库。
- 法律事实、法律依据和生成结论应当可追踪并由律师复核。
- 仅发布达到公开发行标准的稳定能力或工具，不把内部测试版直接公开。
- Skill 保持清晰边界，优先复用已有工具和成熟工作流。

## 仓库结构

公开能力以平台中立的 `SKILL.md` 目录为核心，按需提供 Codex 等平台的薄适配层：

```text
plugins/
  <plugin-name>/
    .codex-plugin/
      plugin.json
    skills/
      <skill-name>/
        SKILL.md
```

WorkBuddy 或其他支持 Agent Skills 目录规范的 Agent，可直接导入 `skills/<skill-name>/`；不支持自动发现时，可手工加载其 `SKILL.md`。平台适配文件不承载业务逻辑。

不属于 Agent Skill 的独立本机工具放在：

```text
tools/
  <tool-name>/
    README.md
    VERSION
    LICENSE
    src/
    scripts/
```

独立工具不伪装成 Skill；源码进入仓库，版本化安装包通过对应 GitHub Release 提供。

当前公开候选包括 `local-redaction-assistant` 和 `legal-network-verification`；各候选只有在完成对应的发布复核并合并至 `main` 后，才视为正式发行。

当前公开工具包括：

- `finder-office-quick-actions`：在 macOS Finder 选中项目的右键“快速操作”中，新建 Word/Excel 文件或将项目移动到桌面。版本和正式发行状态以对应标签与 GitHub Release 为准。

## 公开发布标准

每项 Skill 或工具发布前至少需要完成：

1. 客户资料、个人信息、密钥、本机路径和缓存检查；
2. 作者权利、第三方许可和引用检查；
3. 匿名正向测试、反例测试和边界测试；
4. 安装、触发、依赖和新任务加载验证；
5. 法律适用范围、人工复核要求和已知限制说明。

## 作者

**Yingchao Yang (杨颖超律师)**
GitHub: [yangyc03](https://github.com/yangyc03)

## 许可证

除具体插件或 Skill 另有说明外，本仓库采用 [Apache License 2.0](LICENSE)。第三方内容继续适用其原始许可和署名要求。

## 免责声明

本仓库提供的内容仅用于法律科技研究、工作流辅助和专业交流，不构成针对任何具体事项的法律意见，不建立律师与客户关系，也不替代合资格律师的独立判断。详见 [DISCLAIMER.md](DISCLAIMER.md)。
