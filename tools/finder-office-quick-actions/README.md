# Finder 办公快速操作

这是三个 macOS Finder Automator Quick Actions 的可维护源文件和分发包构建目录：

- 新建 Word 文档
- 新建 Excel 工作簿
- 移动到桌面

当前版本：`v1.0.0`。

## 边界

- 仅在 Finder 选中项目的右键菜单中出现，不支持 Finder 空白处右键。
- 不需要 Xcode、Python、管理员权限或完全磁盘访问。
- 新建动作使用去个人信息的中性 `.docx`、`.xlsx` 空白模板，并明确调用 Microsoft Word、Microsoft Excel 打开。
- 不包含宏、网络访问、客户资料、凭证或 OneDrive 绝对路径。
- `移动到桌面` 的替换操作通过 macOS 原生文件机制将旧项目移入废纸篓，不永久删除。
- 为避免误操作链接指向的真实文件，本版本拒绝移动符号链接，也不自动处理桌面同名符号链接。
- 已在维护者本机完成自动化和隔离安装测试，并由用户在另一台 Mac 完成安装及使用验收。
- 这是公开发行源码；发布用 ZIP 还包含 Apache-2.0 许可证、署名和第三方商标说明。
- 未经过 Apple Developer ID 签名或公证；只应从本仓库正式 Release 下载并核对 SHA-256，不要关闭 Gatekeeper。

## 源码结构

- `src/applescript/`：三个动作的可审查 AppleScript 源码。
- `src/workflows/`：可由 Automator 识别的工作流包。
- `src/package/`：接收方使用的安装、卸载文件和说明。
- `templates/`：去个人信息的中性 Office 空白模板。
- `scripts/`：同步脚本、验证和 ZIP 构建工具。
- `dist/`：最终分发 ZIP 及其 SHA-256。
- 公开 ZIP 使用 ASCII 文件名 `Finder-Office-Quick-Actions-v<version>.zip`，避免发布平台清理中文附件名；包内操作名称和中文说明不变。

## 构建与验证

在 macOS 上运行：

```text
scripts/build_distribution.zsh
```

构建过程会把独立 AppleScript 源码同步进 `.workflow`，检查工作流 plist、模板 ZIP 完整性、本机路径、Python 依赖和个人作者元数据，然后生成版本化 ZIP。

## 许可

- Copyright 2026 Yingchao Yang（杨颖超）
- 本工具采用 [Apache License 2.0](LICENSE)。
- Microsoft、Microsoft Word、Microsoft Excel、Apple、macOS、Finder 和 Automator 是其各自权利人的商标；本项目不隶属于或受其背书。详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
