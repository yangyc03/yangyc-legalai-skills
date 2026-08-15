from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile


TOOL_ROOT = Path(__file__).resolve().parent.parent
FIXED_STAMP = "2099-01-01_0000"


def workflow_source(bundle: Path) -> tuple[Path, dict, str]:
    plist_path = bundle / "Contents" / "document.wflow"
    with plist_path.open("rb") as handle:
        document = plistlib.load(handle)
    source = document["actions"][0]["action"]["ActionParameters"]["source"]
    return plist_path, document, source


def write_workflow_source(bundle: Path, source: str) -> None:
    plist_path, document, _ = workflow_source(bundle)
    document["actions"][0]["action"]["ActionParameters"]["source"] = source
    with plist_path.open("wb") as handle:
        plistlib.dump(document, handle, fmt=plistlib.FMT_XML, sort_keys=False)


def run_script(script_path: Path, input_path: Path) -> None:
    result = subprocess.run(
        ["/usr/bin/osascript", str(script_path), str(input_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"AppleScript failed ({result.returncode}): {result.stdout}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_creation_script(
    temporary_root: Path,
    workflow_name: str,
    template_name: str,
    app_bundle: str,
) -> tuple[Path, Path]:
    temporary_root.mkdir(parents=True, exist_ok=True)
    source_bundle = TOOL_ROOT / "src" / "workflows" / workflow_name

    support_dir = temporary_root / "support"
    template_dir = support_dir / "FinderOfficeQuickActions"
    template_dir.mkdir(parents=True, exist_ok=True)
    template_path = template_dir / template_name
    shutil.copy2(TOOL_ROOT / "templates" / template_name, template_path)

    _, _, source = workflow_source(source_bundle)
    source = source.replace(
        "on run {input, parameters}",
        "on run argv\n\tset input to {POSIX file (item 1 of argv)}\n\tset parameters to {}",
        1,
    )
    source = source.replace(
        "set supportPath to POSIX path of (path to application support from user domain)",
        f'set supportPath to "{support_dir.as_posix()}/"',
    )
    source = source.replace(
        'set stamp to do shell script "/bin/date \'+%Y-%m-%d_%H%M\'"',
        f'set stamp to "{FIXED_STAMP}"',
    )
    source = source.replace(
        f'do shell script "/usr/bin/open -b {app_bundle} " & quoted form of outputPath',
        'do shell script "/usr/bin/true"',
    )
    source = source.replace(
        f'display dialog "无法创建 {"Word 文档" if "Word" in workflow_name else "Excel 工作簿"}：" & errMsg buttons {{"好"}} default button "好" with icon stop',
        'error "TEST_CREATE_FAILURE: " & errMsg number errNum',
    )
    if str(support_dir) not in source or FIXED_STAMP not in source or app_bundle in source:
        raise AssertionError(f"Failed to prepare {workflow_name}")
    script_path = temporary_root / f"{workflow_name}.applescript"
    script_path.write_text(source, encoding="utf-8")
    return script_path, template_path


def test_creation(
    temporary_root: Path,
    workflow_name: str,
    template_name: str,
    app_bundle: str,
    label: str,
    extension: str,
) -> None:
    script, template = prepare_creation_script(
        temporary_root / label,
        workflow_name,
        template_name,
        app_bundle,
    )
    target = temporary_root / f"创建测试-含 空格'{label}"
    target.mkdir(parents=True)

    run_script(script, target)
    run_script(script, target)
    first = target / f"{FIXED_STAMP}_{label}{extension}"
    second = target / f"{FIXED_STAMP}_{label}_02{extension}"
    assert first.is_file() and second.is_file()
    assert sha256(first) == sha256(template) == sha256(second)

    shutil.rmtree(target)
    target.mkdir()
    for index in range(1, 100):
        suffix = "" if index == 1 else f"_{index:02d}"
        (target / f"{FIXED_STAMP}_{label}{suffix}{extension}").write_bytes(b"occupied")
    run_script(script, target)
    hundredth = target / f"{FIXED_STAMP}_{label}_100{extension}"
    assert hundredth.is_file() and sha256(hundredth) == sha256(template)
    assert not list(target.glob(".finder-office-*"))


def prepare_move_script(
    temporary_root: Path,
    desktop: Path,
    choice: str,
    fake_trash: Path,
) -> Path:
    source_bundle = TOOL_ROOT / "src" / "workflows" / "移动到桌面.workflow"
    _, _, source = workflow_source(source_bundle)
    source = source.replace(
        "on run {input, parameters}",
        "on run argv\n\tset input to {POSIX file (item 1 of argv)}\n\tset parameters to {}",
        1,
    )
    source = source.replace(
        "set desktopPath to POSIX path of (path to desktop folder)",
        f'set desktopPath to "{desktop.as_posix()}/"',
    )
    dialog_start = 'set choice to button returned of (display dialog "桌面已有“" & sourceName & "”。" buttons {"取消", "保留两者", "替换"} default button "保留两者" with icon caution)'
    source = source.replace(dialog_start, f'set choice to "{choice}"')
    trash_handler_start = "on moveItemToTrash(targetPath)"
    trash_handler_end = "end moveItemToTrash"
    handler_start_index = source.index(trash_handler_start)
    handler_end_index = source.index(trash_handler_end, handler_start_index) + len(trash_handler_end)
    fake_trash_handler = (
        "on moveItemToTrash(targetPath)\n"
        '\tdo shell script "/bin/mv " & quoted form of targetPath & " " & quoted form of '
        f'"{fake_trash.as_posix()}/"\n'
        "end moveItemToTrash"
    )
    source = source[:handler_start_index] + fake_trash_handler + source[handler_end_index:]
    source = source.replace(
        'display dialog summaryText buttons {"好"} default button "好"',
        "set summaryText to summaryText",
    )
    if str(desktop) not in source or dialog_start in source or "trashItemAtURL" in source:
        raise AssertionError(f"Failed to prepare move workflow: {choice}")
    script_path = temporary_root / f"移动到桌面-{choice}.applescript"
    script_path.write_text(source, encoding="utf-8")
    return script_path


def test_move(temporary_root: Path) -> None:
    desktop = temporary_root / "fake-desktop"
    fake_trash = temporary_root / "fake-trash"
    sources = temporary_root / "sources"
    desktop.mkdir()
    fake_trash.mkdir()
    sources.mkdir()

    keep = prepare_move_script(temporary_root, desktop, "保留两者", fake_trash)
    replace = prepare_move_script(temporary_root, desktop, "替换", fake_trash)
    cancel = prepare_move_script(temporary_root, desktop, "取消", fake_trash)

    simple = sources / "普通 文件.txt"
    simple.write_text("simple", encoding="utf-8")
    run_script(keep, simple)
    assert not simple.exists() and (desktop / simple.name).read_text(encoding="utf-8") == "simple"

    collision = sources / ".config.json"
    collision.write_text("new", encoding="utf-8")
    (desktop / collision.name).write_text("old", encoding="utf-8")
    run_script(keep, collision)
    assert (desktop / ".config_02.json").read_text(encoding="utf-8") == "new"

    dotted_folder = sources / "Matter.v1"
    dotted_folder.mkdir()
    (desktop / dotted_folder.name).mkdir()
    run_script(keep, dotted_folder)
    assert (desktop / "Matter.v1_02").is_dir()

    cancelled = sources / "取消.txt"
    cancelled.write_text("stay", encoding="utf-8")
    (desktop / cancelled.name).write_text("desktop", encoding="utf-8")
    run_script(cancel, cancelled)
    assert cancelled.read_text(encoding="utf-8") == "stay"

    replacement = sources / "替换.txt"
    replacement.write_text("new", encoding="utf-8")
    (desktop / replacement.name).write_text("old", encoding="utf-8")
    run_script(replace, replacement)
    assert (desktop / replacement.name).read_text(encoding="utf-8") == "new"
    assert (fake_trash / replacement.name).read_text(encoding="utf-8") == "old"

    already_there = desktop / "已在桌面.txt"
    already_there.write_text("same", encoding="utf-8")
    run_script(keep, already_there)
    assert already_there.read_text(encoding="utf-8") == "same"

    link_target = sources / "链接真实目标.txt"
    link_target.write_text("target", encoding="utf-8")
    link_path = sources / "链接.txt"
    link_path.symlink_to(link_target)
    run_script(keep, link_path)
    assert link_path.is_symlink()
    assert link_target.read_text(encoding="utf-8") == "target"
    assert not (desktop / link_path.name).exists()

    desktop_link_target = desktop / "桌面链接真实目标.txt"
    desktop_link_target.write_text("desktop-target", encoding="utf-8")
    desktop_link = desktop / "桌面链接.txt"
    desktop_link.symlink_to(desktop_link_target)
    source_against_link = sources / desktop_link.name
    source_against_link.write_text("source", encoding="utf-8")
    run_script(replace, source_against_link)
    assert source_against_link.read_text(encoding="utf-8") == "source"
    assert desktop_link.is_symlink()
    assert desktop_link_target.read_text(encoding="utf-8") == "desktop-target"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="foqa-workflow-test.") as directory:
        root = Path(directory)
        test_creation(
            root,
            "新建 Word 文档.workflow",
            "Word中性空白文档.docx",
            "com.microsoft.Word",
            "新建Word文档",
            ".docx",
        )
        test_creation(
            root,
            "新建 Excel 工作簿.workflow",
            "Excel中性空白工作簿.xlsx",
            "com.microsoft.Excel",
            "新建Excel工作簿",
            ".xlsx",
        )
        test_move(root)
    print("WORKFLOW_TEST=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
