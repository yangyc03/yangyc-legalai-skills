#!/bin/zsh
set -euo pipefail

foqa_package_root=${1:?"Usage: validate_distribution.zsh <package-root>"}
foqa_workflows=("新建 Word 文档.workflow" "新建 Excel 工作簿.workflow" "移动到桌面.workflow")
foqa_templates=("Word中性空白文档.docx" "Excel中性空白工作簿.xlsx")

for foqa_name in $foqa_workflows; do
  foqa_bundle="$foqa_package_root/快速操作/$foqa_name"
  [[ -d "$foqa_bundle" ]] || { print -u2 "缺少 $foqa_name"; exit 10; }
  /usr/bin/plutil -lint "$foqa_bundle/Contents/Info.plist" >/dev/null
  /usr/bin/plutil -lint "$foqa_bundle/Contents/document.wflow" >/dev/null
  foqa_source=$(/usr/bin/plutil -extract actions.0.action.ActionParameters.source raw "$foqa_bundle/Contents/document.wflow")
  foqa_compiled=$(/usr/bin/mktemp "${TMPDIR:-/tmp}/foqa-script.XXXXXX")
  /usr/bin/printf '%s\n' "$foqa_source" | /usr/bin/osacompile -o "$foqa_compiled" -
  /bin/rm -f "$foqa_compiled"
done
for foqa_name in $foqa_templates; do
  [[ -f "$foqa_package_root/空白模板/$foqa_name" ]] || { print -u2 "缺少 $foqa_name"; exit 11; }
  /usr/bin/unzip -t "$foqa_package_root/空白模板/$foqa_name" >/dev/null
done

[[ -x "$foqa_package_root/安装.command" ]] || { print -u2 "安装脚本不可执行"; exit 12; }
[[ -x "$foqa_package_root/卸载.command" ]] || { print -u2 "卸载脚本不可执行"; exit 12; }
for foqa_public_doc in LICENSE NOTICE THIRD_PARTY_NOTICES.md; do
  [[ -f "$foqa_package_root/$foqa_public_doc" ]] || { print -u2 "公开发行包缺少 $foqa_public_doc"; exit 12; }
done
/usr/bin/grep -q 'Apache License' "$foqa_package_root/LICENSE" || { print -u2 "许可证不是 Apache License 2.0"; exit 12; }

if /usr/bin/grep -R -E -n '/Users/yangyc|/usr/bin/python3|OneDrive|Client ID|API Key|credential|token' "$foqa_package_root/快速操作" "$foqa_package_root/安装.command" "$foqa_package_root/卸载.command"; then
  print -u2 "发现不应分发的本机路径、依赖或敏感词。"
  exit 13
fi

if /usr/bin/grep -R -E -n --exclude='*.docx' --exclude='*.xlsx' '仅限本人|内部非商业使用|不得商业销售|不得公开发布|yangyc-legalai-workflows' "$foqa_package_root"; then
  print -u2 "公开发行包中发现私有许可或私有仓库表述。"
  exit 13
fi

foqa_word_core=$(/usr/bin/unzip -p "$foqa_package_root/空白模板/Word中性空白文档.docx" docProps/core.xml)
foqa_excel_core=$(/usr/bin/unzip -p "$foqa_package_root/空白模板/Excel中性空白工作簿.xlsx" docProps/core.xml 2>/dev/null || true)
if [[ "$foqa_word_core$foqa_excel_core" == *yangyc* ]]; then
  print -u2 "空白模板仍包含个人作者信息。"
  exit 14
fi

for foqa_template in "$foqa_package_root/空白模板/Word中性空白文档.docx" "$foqa_package_root/空白模板/Excel中性空白工作簿.xlsx"; do
  if /usr/bin/zipgrep -i -E '/Users/|OneDrive|yangyc|TargetMode="External"|mailto:' "$foqa_template" >/dev/null 2>&1; then
    print -u2 "Office 模板中发现个人路径、外部链接或个人标识：${foqa_template:t}"
    exit 15
  fi
  if /usr/bin/unzip -Z1 "$foqa_template" | /usr/bin/grep -i -E 'vbaProject\.bin|externalLinks/|connections\.xml|docProps/custom\.xml' >/dev/null; then
    print -u2 "Office 模板中发现宏、外链、连接或自定义属性：${foqa_template:t}"
    exit 15
  fi
done

if /usr/bin/find "$foqa_package_root" -name '.DS_Store' -o -name '__MACOSX' | /usr/bin/grep -q .; then
  print -u2 "发行包中发现 Finder 缓存或资源分叉目录。"
  exit 16
fi

print "VALIDATION=PASS"
