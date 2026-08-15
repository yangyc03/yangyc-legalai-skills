#!/bin/zsh
set -euo pipefail

foqa_tool_root=${0:A:h:h}
foqa_version=$(<"$foqa_tool_root/VERSION")
foqa_zip="$foqa_tool_root/dist/Finder-Office-Quick-Actions-v$foqa_version.zip"
foqa_test_root=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/foqa-test.XXXXXX")
foqa_unpack_root="$foqa_test_root/unpacked"
foqa_home="$foqa_test_root/home"
foqa_cancel_home="$foqa_test_root/cancel-home"
foqa_tampered_home="$foqa_test_root/tampered-home"
foqa_link_home="$foqa_test_root/link-home"
foqa_link_outside="$foqa_test_root/link-outside"

trap '/bin/rm -rf "$foqa_test_root"' EXIT
/bin/mkdir -p "$foqa_unpack_root" "$foqa_home" "$foqa_cancel_home" "$foqa_tampered_home" "$foqa_link_home" "$foqa_link_outside"
/usr/bin/ditto -x -k "$foqa_zip" "$foqa_unpack_root"
foqa_package_root=$(/usr/bin/find "$foqa_unpack_root" -mindepth 1 -maxdepth 1 -type d -name 'Finder-Office-Quick-Actions-*' -print -quit)
[[ -n "$foqa_package_root" ]] || { print -u2 "ZIP 解压后未找到包目录"; exit 30; }

/usr/bin/printf 'n\n' | FOQA_INSTALL_ROOT="$foqa_cancel_home" FOQA_KEEP_TERMINAL_OPEN=0 "$foqa_package_root/安装.command" >/dev/null
[[ ! -e "$foqa_cancel_home/Library" ]] || { print -u2 "取消安装后仍产生了文件"; exit 30; }

foqa_tampered_package="$foqa_test_root/tampered-package"
/usr/bin/ditto --norsrc "$foqa_package_root" "$foqa_tampered_package"
/usr/bin/printf '%s\n' "tampered" >> "$foqa_tampered_package/快速操作/新建 Word 文档.workflow/Contents/document.wflow"
set +e
FOQA_INSTALL_ROOT="$foqa_tampered_home" FOQA_ASSUME_YES=1 FOQA_KEEP_TERMINAL_OPEN=0 "$foqa_tampered_package/安装.command" >/dev/null 2>&1
foqa_tampered_status=$?
set -e
[[ "$foqa_tampered_status" -ne 0 ]] || { print -u2 "损坏资源未被拒绝"; exit 30; }
[[ ! -e "$foqa_tampered_home/Library" ]] || { print -u2 "损坏资源在校验前产生了文件"; exit 30; }

/bin/ln -s "$foqa_link_outside" "$foqa_link_home/Library"
set +e
FOQA_INSTALL_ROOT="$foqa_link_home" FOQA_ASSUME_YES=1 FOQA_KEEP_TERMINAL_OPEN=0 "$foqa_package_root/安装.command" >/dev/null 2>&1
foqa_link_status=$?
set -e
[[ "$foqa_link_status" -ne 0 ]] || { print -u2 "符号链接祖先目录未被拒绝"; exit 30; }
[[ -z "$(/usr/bin/find "$foqa_link_outside" -mindepth 1 -print -quit)" ]] || { print -u2 "安装通过符号链接写出了测试根"; exit 30; }

FOQA_INSTALL_ROOT="$foqa_home" FOQA_ASSUME_YES=1 FOQA_KEEP_TERMINAL_OPEN=0 "$foqa_package_root/安装.command"

for foqa_name in "新建 Word 文档.workflow" "新建 Excel 工作簿.workflow" "移动到桌面.workflow"; do
  [[ -d "$foqa_home/Library/Services/$foqa_name" ]] || { print -u2 "隔离安装缺少 $foqa_name"; exit 31; }
done
for foqa_name in "Word中性空白文档.docx" "Excel中性空白工作簿.xlsx"; do
  [[ -f "$foqa_home/Library/Application Support/FinderOfficeQuickActions/$foqa_name" ]] || { print -u2 "隔离安装缺少 $foqa_name"; exit 31; }
done

foqa_original_hash=$(/usr/bin/shasum -a 256 "$foqa_home/Library/Services/新建 Word 文档.workflow/Contents/document.wflow")
foqa_original_hash=${foqa_original_hash%% *}
/usr/bin/printf '%s\n' "pre-existing-marker" > "$foqa_home/Library/Services/移动到桌面.workflow/pre-existing-marker.txt"

set +e
FOQA_INSTALL_ROOT="$foqa_home" FOQA_ASSUME_YES=1 FOQA_KEEP_TERMINAL_OPEN=0 FOQA_FAIL_AFTER=2 "$foqa_package_root/安装.command" >/dev/null 2>&1
foqa_failure_status=$?
set -e
[[ "$foqa_failure_status" -ne 0 ]] || { print -u2 "失败注入未触发"; exit 32; }

foqa_after_hash=$(/usr/bin/shasum -a 256 "$foqa_home/Library/Services/新建 Word 文档.workflow/Contents/document.wflow")
foqa_after_hash=${foqa_after_hash%% *}
[[ "$foqa_after_hash" == "$foqa_original_hash" ]] || { print -u2 "失败回滚未恢复 Word 动作"; exit 33; }
[[ -f "$foqa_home/Library/Services/移动到桌面.workflow/pre-existing-marker.txt" ]] || { print -u2 "失败回滚未恢复修改过的动作"; exit 33; }

FOQA_INSTALL_ROOT="$foqa_home" FOQA_ASSUME_YES=1 FOQA_KEEP_TERMINAL_OPEN=0 "$foqa_package_root/安装.command" >/dev/null
set +e
FOQA_INSTALL_ROOT="$foqa_home" FOQA_ASSUME_YES=1 FOQA_KEEP_TERMINAL_OPEN=0 FOQA_UNINSTALL_FAIL_AFTER=2 "$foqa_package_root/卸载.command" >/dev/null 2>&1
foqa_uninstall_failure_status=$?
set -e
[[ "$foqa_uninstall_failure_status" -ne 0 ]] || { print -u2 "卸载失败注入未触发"; exit 34; }
for foqa_name in "新建 Word 文档.workflow" "新建 Excel 工作簿.workflow" "移动到桌面.workflow"; do
  [[ -d "$foqa_home/Library/Services/$foqa_name" ]] || { print -u2 "卸载失败回滚缺少 $foqa_name"; exit 35; }
done
for foqa_name in "Word中性空白文档.docx" "Excel中性空白工作簿.xlsx"; do
  [[ -f "$foqa_home/Library/Application Support/FinderOfficeQuickActions/$foqa_name" ]] || { print -u2 "卸载失败回滚缺少 $foqa_name"; exit 35; }
done
FOQA_INSTALL_ROOT="$foqa_home" FOQA_ASSUME_YES=1 FOQA_KEEP_TERMINAL_OPEN=0 "$foqa_package_root/卸载.command" >/dev/null
[[ -f "$foqa_home/Library/Services/移动到桌面.workflow/pre-existing-marker.txt" ]] || { print -u2 "卸载未恢复安装前版本"; exit 34; }

print "ISOLATED_INSTALL_TEST=PASS"
