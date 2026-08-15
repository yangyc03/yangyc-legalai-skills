#!/bin/zsh
set -euo pipefail

foqa_tool_root=${0:A:h:h}

/usr/bin/plutil -replace actions.0.action.ActionParameters.source -string "$(<"$foqa_tool_root/src/applescript/new_word.applescript")" "$foqa_tool_root/src/workflows/新建 Word 文档.workflow/Contents/document.wflow"
/usr/bin/plutil -replace actions.0.action.ActionParameters.source -string "$(<"$foqa_tool_root/src/applescript/new_excel.applescript")" "$foqa_tool_root/src/workflows/新建 Excel 工作簿.workflow/Contents/document.wflow"
/usr/bin/plutil -replace actions.0.action.ActionParameters.source -string "$(<"$foqa_tool_root/src/applescript/move_to_desktop.applescript")" "$foqa_tool_root/src/workflows/移动到桌面.workflow/Contents/document.wflow"

/usr/bin/plutil -convert xml1 "$foqa_tool_root/src/workflows/新建 Word 文档.workflow/Contents/document.wflow"
/usr/bin/plutil -convert xml1 "$foqa_tool_root/src/workflows/新建 Excel 工作簿.workflow/Contents/document.wflow"
/usr/bin/plutil -convert xml1 "$foqa_tool_root/src/workflows/移动到桌面.workflow/Contents/document.wflow"
