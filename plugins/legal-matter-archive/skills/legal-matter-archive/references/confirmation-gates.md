# 确认门槛

## 环境与配置

- `doctor` 只检测，不安装、不写配置、不读取案件目录。
- `setup --plan` 和 `configure --plan` 只生成预览及令牌。
- `setup --apply` 和 `configure --apply` 必须使用同一预览的匹配令牌；计划变化后令牌失效。
- `template-check` 只读检查用户模板。

## 正式归档

1. `inspect` 只读盘点。
2. `plan` 只生成预览。未确认正式归档、委托范围、材料决定、目录、封皮、页码、小结或缺失处理时必须阻断。
3. `prepare` 必须使用 plan 的 `confirmation_token`，仅在独立 run 中生成派生副本和真实模板预览，不生成最终卷宗。
4. 律师必须检查真实页数、材料顺序、封皮、目录、转换内容和所有警示。
5. `finalize` 必须使用 prepared manifest 的 `final_confirmation_token`。目录跨页等特殊警示还需要绑定候选哈希的单独接受令牌和复核人。
6. `verify` 先完成哈希、页数、连续页码和渲染检查。没有自动渲染或没有视觉复核时，状态只能是 `technical_complete_manual_visual_review_required`。
7. 只有自动视觉 QA 通过且明确记录复核人，才能提升为 `ready_for_oa_submission`。
8. `preview-deliver` 必须绑定当前 manifest、视觉复核记录、目标目录、文件名和最终 PDF 哈希；`deliver` 再次使用匹配令牌，仅复制最终 PDF。
9. `preview-invalidate` / `invalidate` 只标记 run 失效，不删除源材料、成果或历史技术记录。

`ready_for_oa_submission` 不是 OA 已审核、已接收或行政归档完成。本 Skill 不操作 OA。

## 加密、异常与回滚

- 需要打开密码或禁止打印的 PDF 必须阻断；不得绕过权限或保存密码。
- 无需密码且允许打印的权限型加密 PDF，只有启用 Ghostscript 后才能生成独立派生副本，并须逐页复核。
- pypdf 合并失败且未启用 qpdf 时阻断，不静默跳过异常页面。
- `prepare` 和 `finalize` 使用临时事务目录；异常时回滚本轮提升的文件和目录。发现遗留事务标记时停止并要求人工核对。
