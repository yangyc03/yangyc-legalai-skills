# 杨颖超律师 LegalAI Skills

Reusable Codex skills for Chinese legal workflows, maintained by **Yingchao Yang (杨颖超律师)**.

本仓库用于发布经过筛选、脱敏、测试和许可核验的中国法律实务 Codex Skills。项目强调本地优先、来源可追踪、律师复核和明确的能力边界。

## 项目原则

- AI 适应律师工作流，而不是改变律师的基本工作习惯。
- 客户资料、案件材料、工作底稿、密钥和本机配置不得进入本仓库。
- 法律事实、法律依据和生成结论应当可追踪并由律师复核。
- 仅发布达到公开发行标准的稳定能力，不把内部测试版直接公开。
- Skill 保持清晰边界，优先复用已有工具和成熟工作流。

## 仓库结构

公开能力将按业务线封装为可独立安装的 Codex 插件：

```text
plugins/
  <plugin-name>/
    .codex-plugin/
      plugin.json
    skills/
      <skill-name>/
        SKILL.md
```

当前仓库处于初始化阶段，尚未发布可安装 Skill。首批 Skill 将在完成公开发布审查后加入。

## 公开发布标准

每项 Skill 发布前至少需要完成：

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
