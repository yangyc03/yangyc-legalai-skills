#!/bin/zsh
set -euo pipefail

foqa_tool_root=${0:A:h:h}
foqa_version=$(<"$foqa_tool_root/VERSION")
foqa_package_name="Finder-Office-Quick-Actions-v$foqa_version"
foqa_stage_root=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/foqa-build.XXXXXX")
foqa_package_root="$foqa_stage_root/$foqa_package_name"
foqa_temp_zip="$foqa_stage_root/$foqa_package_name.zip"
foqa_output_zip="$foqa_tool_root/dist/$foqa_package_name.zip"
foqa_output_hash="$foqa_output_zip.sha256"

trap '/bin/rm -rf "$foqa_stage_root"' EXIT

"$foqa_tool_root/scripts/sync_workflow_sources.zsh"
/bin/mkdir -p "$foqa_package_root/快速操作" "$foqa_package_root/空白模板"

for foqa_name in "新建 Word 文档.workflow" "新建 Excel 工作簿.workflow" "移动到桌面.workflow"; do
  /usr/bin/ditto --norsrc --noextattr --noqtn --noacl "$foqa_tool_root/src/workflows/$foqa_name" "$foqa_package_root/快速操作/$foqa_name"
done
for foqa_name in "Word中性空白文档.docx" "Excel中性空白工作簿.xlsx"; do
  /usr/bin/ditto --norsrc --noextattr --noqtn --noacl "$foqa_tool_root/templates/$foqa_name" "$foqa_package_root/空白模板/$foqa_name"
done

/usr/bin/ditto --norsrc --noextattr --noqtn --noacl "$foqa_tool_root/src/package/安装.command" "$foqa_package_root/安装.command"
/usr/bin/ditto --norsrc --noextattr --noqtn --noacl "$foqa_tool_root/src/package/卸载.command" "$foqa_package_root/卸载.command"
/usr/bin/ditto --norsrc --noextattr --noqtn --noacl "$foqa_tool_root/src/package/安装说明.md" "$foqa_package_root/安装说明.md"
/usr/bin/ditto --norsrc --noextattr --noqtn --noacl "$foqa_tool_root/LICENSE" "$foqa_package_root/LICENSE"
/usr/bin/ditto --norsrc --noextattr --noqtn --noacl "$foqa_tool_root/NOTICE" "$foqa_package_root/NOTICE"
/usr/bin/ditto --norsrc --noextattr --noqtn --noacl "$foqa_tool_root/THIRD_PARTY_NOTICES.md" "$foqa_package_root/THIRD_PARTY_NOTICES.md"
/bin/chmod 755 "$foqa_package_root/安装.command" "$foqa_package_root/卸载.command"

foqa_hash_manifest="$foqa_package_root/校验清单-SHA256.txt"
foqa_files=(
  "安装.command"
  "卸载.command"
  "安装说明.md"
  "LICENSE"
  "NOTICE"
  "THIRD_PARTY_NOTICES.md"
  "快速操作/新建 Word 文档.workflow/Contents/Info.plist"
  "快速操作/新建 Word 文档.workflow/Contents/document.wflow"
  "快速操作/新建 Excel 工作簿.workflow/Contents/Info.plist"
  "快速操作/新建 Excel 工作簿.workflow/Contents/document.wflow"
  "快速操作/移动到桌面.workflow/Contents/Info.plist"
  "快速操作/移动到桌面.workflow/Contents/document.wflow"
  "空白模板/Word中性空白文档.docx"
  "空白模板/Excel中性空白工作簿.xlsx"
)

: > "$foqa_hash_manifest"
for foqa_relative_path in $foqa_files; do
  foqa_hash_line=$(/usr/bin/shasum -a 256 "$foqa_package_root/$foqa_relative_path")
  foqa_hash=${foqa_hash_line%% *}
  /usr/bin/printf '%s  %s\n' "$foqa_hash" "$foqa_relative_path" >> "$foqa_hash_manifest"
done

"$foqa_tool_root/scripts/validate_distribution.zsh" "$foqa_package_root"
/bin/mkdir -p "$foqa_tool_root/dist"
/usr/bin/ditto -c -k --keepParent --norsrc --noextattr --noqtn --noacl "$foqa_package_root" "$foqa_temp_zip"
/bin/mv -f "$foqa_temp_zip" "$foqa_output_zip"

foqa_zip_hash_line=$(/usr/bin/shasum -a 256 "$foqa_output_zip")
foqa_zip_hash=${foqa_zip_hash_line%% *}
/usr/bin/printf '%s  %s\n' "$foqa_zip_hash" "${foqa_output_zip:t}" > "$foqa_output_hash"

print "$foqa_output_zip"
print "$foqa_output_hash"
