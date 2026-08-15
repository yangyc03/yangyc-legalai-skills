#!/bin/zsh
set -euo pipefail

foqa_version="1.0.0"
foqa_script_dir=${0:A:h}
foqa_payload_dir="$foqa_script_dir/快速操作"
foqa_templates_dir="$foqa_script_dir/空白模板"
foqa_manifest="$foqa_script_dir/校验清单-SHA256.txt"
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
  print -u2 "安装根目录必须是已存在的绝对目录，且不能是根目录 /。"
  exit 2
fi
foqa_install_root=$(/bin/realpath "$foqa_install_root")

foqa_services_dir="$foqa_install_root/Library/Services"
foqa_support_dir="$foqa_install_root/Library/Application Support/FinderOfficeQuickActions"
foqa_state_dir="$foqa_install_root/Library/Application Support/FinderOfficeQuickActions Installer"
foqa_timestamp=$(/bin/date '+%Y%m%d-%H%M%S')
foqa_run_id="$foqa_timestamp-$(/usr/bin/uuidgen)"
foqa_transaction_dir="$foqa_state_dir/transactions/$foqa_run_id"
foqa_backup_dir="$foqa_transaction_dir/backup"
foqa_recovery_dir="$foqa_transaction_dir/recovery"
foqa_active_receipt="$foqa_state_dir/active-receipt"

foqa_codes=(word excel desktop word_template excel_template)
foqa_sources=(
  "$foqa_payload_dir/新建 Word 文档.workflow"
  "$foqa_payload_dir/新建 Excel 工作簿.workflow"
  "$foqa_payload_dir/移动到桌面.workflow"
  "$foqa_templates_dir/Word中性空白文档.docx"
  "$foqa_templates_dir/Excel中性空白工作簿.xlsx"
)
foqa_targets=(
  "$foqa_services_dir/新建 Word 文档.workflow"
  "$foqa_services_dir/新建 Excel 工作簿.workflow"
  "$foqa_services_dir/移动到桌面.workflow"
  "$foqa_support_dir/Word中性空白文档.docx"
  "$foqa_support_dir/Excel中性空白工作簿.xlsx"
)

foqa_validate_package() {
  [[ -f "$foqa_manifest" ]] || { print -u2 "缺少校验清单。"; return 3; }
  if /usr/bin/find "$foqa_payload_dir" "$foqa_templates_dir" -type l -print -quit | /usr/bin/grep -q .; then
    print -u2 "安装资源中发现符号链接，已停止。"
    return 3
  fi
  local foqa_file_count
  foqa_file_count=$(/usr/bin/find "$foqa_payload_dir" "$foqa_templates_dir" -type f | /usr/bin/wc -l | /usr/bin/tr -d ' ')
  [[ "$foqa_file_count" == "8" ]] || { print -u2 "安装资源文件数量异常，已停止。"; return 3; }
  local foqa_manifest_count
  foqa_manifest_count=$(/usr/bin/grep -c '^[0-9a-fA-F]\{64\}  ' "$foqa_manifest" || true)
  [[ "$foqa_manifest_count" == "14" ]] || { print -u2 "校验清单项目数量异常，已停止。"; return 3; }

  local foqa_line foqa_expected foqa_relative foqa_actual foqa_candidate
  while IFS= read -r foqa_line; do
    [[ -n "$foqa_line" ]] || continue
    foqa_expected=${foqa_line%%  *}
    foqa_relative=${foqa_line#*  }
    [[ "$foqa_expected" != "$foqa_line" && "$foqa_relative" != /* && "$foqa_relative" != *".."* ]] || {
      print -u2 "校验清单格式异常。"
      return 3
    }
    foqa_candidate="$foqa_script_dir/$foqa_relative"
    [[ -f "$foqa_candidate" && ! -L "$foqa_candidate" ]] || { print -u2 "缺少或拒绝资源：$foqa_relative"; return 3; }
    foqa_actual=$(/usr/bin/shasum -a 256 "$foqa_candidate")
    foqa_actual=${foqa_actual%% *}
    [[ "$foqa_actual" == "$foqa_expected" ]] || { print -u2 "校验失败：$foqa_relative"; return 3; }
  done < "$foqa_manifest"
}

foqa_rollback() {
  trap - ZERR INT TERM
  set +e
  /bin/mkdir -p "$foqa_recovery_dir/installed"
  local foqa_i foqa_code foqa_target foqa_backup foqa_was_processed
  for foqa_i in {1..5}; do
    foqa_code=${foqa_codes[$foqa_i]}
    foqa_target=${foqa_targets[$foqa_i]}
    foqa_backup="$foqa_backup_dir/$foqa_code"
    foqa_was_processed=0
    [[ -f "$foqa_transaction_dir/processed-$foqa_code" ]] && foqa_was_processed=1
    if [[ "$foqa_was_processed" -eq 1 && ( -e "$foqa_target" || -L "$foqa_target" ) ]]; then
      /bin/mv "$foqa_target" "$foqa_recovery_dir/installed/$foqa_code"
    fi
    if [[ -e "$foqa_backup" || -L "$foqa_backup" ]]; then
      /bin/mkdir -p "${foqa_target:h}"
      /bin/mv "$foqa_backup" "$foqa_target"
    fi
  done
  if [[ -f "$foqa_transaction_dir/previous-active-receipt" ]]; then
    /bin/mkdir -p "$foqa_state_dir"
    /bin/cp "$foqa_transaction_dir/previous-active-receipt" "$foqa_active_receipt"
  elif [[ -f "$foqa_active_receipt" ]] && [[ "$(<"$foqa_active_receipt")" == "$foqa_transaction_dir" ]]; then
    /bin/mv "$foqa_active_receipt" "$foqa_recovery_dir/active-receipt"
  fi
  /usr/bin/printf '%s\n' "rolled-back" > "$foqa_transaction_dir/status"
  print -u2 "安装未完成；安装前文件已经恢复。失败期间生成的文件保存在："
  print -u2 "  $foqa_recovery_dir"
  exit 20
}

foqa_validate_package

foqa_path_components=(
  "$foqa_install_root/Library"
  "$foqa_install_root/Library/Services"
  "$foqa_install_root/Library/Application Support"
  "$foqa_support_dir"
  "$foqa_state_dir"
)
for foqa_parent in $foqa_path_components; do
  if [[ -L "$foqa_parent" ]]; then
    print -u2 "安装目标目录是符号链接，已停止：$foqa_parent"
    exit 4
  fi
  if [[ -e "$foqa_parent" && ! -d "$foqa_parent" ]]; then
    print -u2 "安装目标路径不是文件夹，已停止：$foqa_parent"
    exit 4
  fi
done

print "Finder 办公快速操作 v$foqa_version"
print "即将安装五个项目到当前用户："
for foqa_i in {1..5}; do
  foqa_target=${foqa_targets[$foqa_i]}
  if [[ -e "$foqa_target" || -L "$foqa_target" ]]; then
    print "  [备份并替换] $foqa_target"
  else
    print "  [新增]       $foqa_target"
  fi
done
print "备份位置：$foqa_backup_dir"
print "不会申请管理员权限、完全磁盘访问，也不会修改 Gatekeeper。"

if [[ "${FOQA_ASSUME_YES:-0}" != "1" ]]; then
  read "foqa_reply?继续安装？输入 y 确认，其他键取消："
  [[ "$foqa_reply" == [yY] ]] || { print "已取消，未作改动。"; exit 0; }
fi

/bin/mkdir -p "$foqa_services_dir" "$foqa_support_dir" "$foqa_transaction_dir"
/bin/chmod 700 "$foqa_state_dir" "$foqa_transaction_dir"
/usr/bin/printf '%s\n' "installing" > "$foqa_transaction_dir/status"
/usr/bin/printf '%s\n' "$foqa_version" > "$foqa_transaction_dir/version"
if [[ -f "$foqa_active_receipt" ]]; then
  /bin/cp "$foqa_active_receipt" "$foqa_transaction_dir/previous-active-receipt"
fi

trap 'foqa_rollback' ZERR INT TERM
foqa_copy_count=0
for foqa_i in {1..5}; do
  foqa_code=${foqa_codes[$foqa_i]}
  foqa_source=${foqa_sources[$foqa_i]}
  foqa_target=${foqa_targets[$foqa_i]}
  if [[ -e "$foqa_target" || -L "$foqa_target" ]]; then
    /bin/mkdir -p "$foqa_backup_dir"
    /bin/mv "$foqa_target" "$foqa_backup_dir/$foqa_code"
  fi
  : > "$foqa_transaction_dir/processed-$foqa_code"
  /usr/bin/ditto --norsrc "$foqa_source" "$foqa_target"
  foqa_copy_count=$((foqa_copy_count + 1))
  if [[ -n "${FOQA_FAIL_AFTER:-}" && "$foqa_copy_count" -eq "$FOQA_FAIL_AFTER" ]]; then
    print -u2 "测试注入：安装中断。"
    false
  fi
done

/usr/bin/printf '%s\n' "$foqa_transaction_dir" > "$foqa_active_receipt.tmp"
/bin/mv "$foqa_active_receipt.tmp" "$foqa_active_receipt"
/usr/bin/printf '%s\n' "installed" > "$foqa_transaction_dir/status"
trap - ZERR INT TERM

print ""
print "安装完成。"
print "下一步：在 Finder 中右键任一文件夹，进入“快速操作 → 自定”，启用三个动作。"
print "首次运行时，只按系统实际提示授予必要权限；不需要完全磁盘访问。"
