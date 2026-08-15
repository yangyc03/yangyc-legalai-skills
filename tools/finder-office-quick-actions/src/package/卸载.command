#!/bin/zsh
set -euo pipefail

foqa_install_root="${FOQA_INSTALL_ROOT:-$HOME}"
foqa_finish_for_test_user() {
  if [[ "${FOQA_KEEP_TERMINAL_OPEN:-1}" == "1" && "${FOQA_ASSUME_YES:-0}" != "1" ]]; then
    print ""
    read "foqa_done?按回车键关闭此窗口。"
  fi
}
trap 'foqa_finish_for_test_user' EXIT
if [[ "$EUID" -eq 0 ]]; then
  print -u2 "请使用普通用户运行，不要使用 sudo。"
  exit 2
fi
if [[ "$foqa_install_root" != /* || "$foqa_install_root" == "/" || ! -d "$foqa_install_root" ]]; then
  print -u2 "卸载根目录必须是已存在的绝对目录，且不能是根目录 /。"
  exit 2
fi
foqa_install_root=$(/bin/realpath "$foqa_install_root")

foqa_services_dir="$foqa_install_root/Library/Services"
foqa_support_dir="$foqa_install_root/Library/Application Support/FinderOfficeQuickActions"
foqa_state_dir="$foqa_install_root/Library/Application Support/FinderOfficeQuickActions Installer"
foqa_active_receipt="$foqa_state_dir/active-receipt"
foqa_timestamp=$(/bin/date '+%Y%m%d-%H%M%S')
foqa_run_id="$foqa_timestamp-$(/usr/bin/uuidgen)"
foqa_recovery_dir="$foqa_state_dir/recoveries/$foqa_run_id"

foqa_codes=(word excel desktop word_template excel_template)
foqa_targets=(
  "$foqa_services_dir/新建 Word 文档.workflow"
  "$foqa_services_dir/新建 Excel 工作簿.workflow"
  "$foqa_services_dir/移动到桌面.workflow"
  "$foqa_support_dir/Word中性空白文档.docx"
  "$foqa_support_dir/Excel中性空白工作簿.xlsx"
)

foqa_path_components=(
  "$foqa_install_root/Library"
  "$foqa_install_root/Library/Services"
  "$foqa_install_root/Library/Application Support"
  "$foqa_support_dir"
  "$foqa_state_dir"
)
for foqa_parent in $foqa_path_components; do
  if [[ -L "$foqa_parent" ]]; then
    print -u2 "卸载目标目录是符号链接，已停止：$foqa_parent"
    exit 4
  fi
  if [[ -e "$foqa_parent" && ! -d "$foqa_parent" ]]; then
    print -u2 "卸载目标路径不是文件夹，已停止：$foqa_parent"
    exit 4
  fi
done

if [[ ! -f "$foqa_active_receipt" ]]; then
  print "未发现本安装器的活动安装记录，没有改动。"
  exit 0
fi
foqa_transaction_dir=$(<"$foqa_active_receipt")
case "$foqa_transaction_dir" in
  "$foqa_state_dir/transactions/"*) ;;
  *) print -u2 "安装记录路径异常，已停止。"; exit 3 ;;
esac
[[ -d "$foqa_transaction_dir" ]] || { print -u2 "安装记录不存在，已停止。"; exit 3; }
foqa_backup_dir="$foqa_transaction_dir/backup"

print "将卸载当前便携版，并恢复本次安装前的同名项目（如有）："
for foqa_i in {1..5}; do
  foqa_target=${foqa_targets[$foqa_i]}
  foqa_code=${foqa_codes[$foqa_i]}
  if [[ -e "$foqa_target" || -L "$foqa_target" ]]; then
    print "  [移到可恢复目录] $foqa_target"
  fi
  if [[ -e "$foqa_backup_dir/$foqa_code" || -L "$foqa_backup_dir/$foqa_code" ]]; then
    print "  [恢复安装前版本] $foqa_target"
  fi
done
print "当前版本的卸载快照：$foqa_recovery_dir"
print "不会删除使用本工具创建的 Word、Excel 文件，也不会清空历史备份。"

if [[ "${FOQA_ASSUME_YES:-0}" != "1" ]]; then
  read "foqa_reply?继续卸载？输入 y 确认，其他键取消："
  [[ "$foqa_reply" == [yY] ]] || { print "已取消，未作改动。"; exit 0; }
fi

foqa_rollback_uninstall() {
  trap - ZERR INT TERM
  set +e
  local foqa_i foqa_code foqa_target
  /bin/mkdir -p "$foqa_recovery_dir/failed-restores"
  for foqa_i in {1..5}; do
    foqa_code=${foqa_codes[$foqa_i]}
    foqa_target=${foqa_targets[$foqa_i]}
    if [[ -f "$foqa_recovery_dir/processed-$foqa_code" ]]; then
      if [[ -e "$foqa_target" || -L "$foqa_target" ]]; then
        /bin/mv "$foqa_target" "$foqa_recovery_dir/failed-restores/$foqa_code"
      fi
      if [[ -e "$foqa_recovery_dir/current/$foqa_code" || -L "$foqa_recovery_dir/current/$foqa_code" ]]; then
        /bin/mkdir -p "${foqa_target:h}"
        /bin/mv "$foqa_recovery_dir/current/$foqa_code" "$foqa_target"
      fi
    fi
  done
  if [[ -f "$foqa_recovery_dir/original-active-receipt" ]]; then
    /bin/cp "$foqa_recovery_dir/original-active-receipt" "$foqa_active_receipt.tmp"
    /bin/mv "$foqa_active_receipt.tmp" "$foqa_active_receipt"
  fi
  print -u2 "卸载未完成；卸载前的当前版本已经恢复。"
  exit 20
}

/bin/mkdir -p "$foqa_recovery_dir/current"
/bin/chmod 700 "$foqa_recovery_dir"
/bin/cp "$foqa_active_receipt" "$foqa_recovery_dir/original-active-receipt"
trap 'foqa_rollback_uninstall' ZERR INT TERM

foqa_uninstall_count=0
for foqa_i in {1..5}; do
  foqa_code=${foqa_codes[$foqa_i]}
  foqa_target=${foqa_targets[$foqa_i]}
  if [[ -e "$foqa_target" || -L "$foqa_target" ]]; then
    /bin/mv "$foqa_target" "$foqa_recovery_dir/current/$foqa_code"
  fi
  : > "$foqa_recovery_dir/processed-$foqa_code"
  if [[ -e "$foqa_backup_dir/$foqa_code" || -L "$foqa_backup_dir/$foqa_code" ]]; then
    /bin/mkdir -p "${foqa_target:h}"
    /usr/bin/ditto --norsrc "$foqa_backup_dir/$foqa_code" "$foqa_target"
  fi
  foqa_uninstall_count=$((foqa_uninstall_count + 1))
  if [[ -n "${FOQA_UNINSTALL_FAIL_AFTER:-}" && "$foqa_uninstall_count" -eq "$FOQA_UNINSTALL_FAIL_AFTER" ]]; then
    print -u2 "测试注入：卸载中断。"
    false
  fi
done

if [[ -f "$foqa_transaction_dir/previous-active-receipt" ]]; then
  /bin/cp "$foqa_transaction_dir/previous-active-receipt" "$foqa_active_receipt.tmp"
  /bin/mv "$foqa_active_receipt.tmp" "$foqa_active_receipt"
else
  /bin/mv "$foqa_active_receipt" "$foqa_recovery_dir/active-receipt"
fi
/usr/bin/printf '%s\n' "uninstalled" > "$foqa_recovery_dir/status"
trap - ZERR INT TERM

print "卸载完成。当前版本已保存在："
print "  $foqa_recovery_dir"
print "如 Finder 菜单未立即刷新，可重新打开 Finder 窗口或退出后重新登录。"
