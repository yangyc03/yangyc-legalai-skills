#!/usr/bin/env python3
"""Fail-closed Microsoft Word validation for locally generated DOCX copies."""

from __future__ import annotations

import argparse
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


WORD_APP = Path("/Applications/Microsoft Word.app")
WORD_VALIDATION_MODES = ("required", "off")
WORD_STAGING_ROOT = Path.home() / "Library/Containers/com.microsoft.Word/Data/Documents/LocalRedactionAssistant"
WORD_CLEANUP_TIMEOUT_SECONDS = 15
WORD_CLEANUP_POLL_SECONDS = 0.25

OPEN_AND_CLOSE_SCRIPT = r'''
on run argv
    set targetPath to item 1 of argv
    tell application "Microsoft Word"
        set openedDocument to open file name targetPath
        delay 1
        set openedName to name of openedDocument
        return "WORD_OPEN_OK|" & openedName
    end tell
end run
'''

CLEAN_COPY_SCRIPT = r'''
on run argv
    set targetPath to item 1 of argv
    set acceptFlag to item 2 of argv
    set removeFlag to item 3 of argv
    tell application "Microsoft Word"
        set openedDocument to open file name targetPath
        if acceptFlag is "true" then
            accept all revisions openedDocument
            set track revisions of openedDocument to false
        end if
        if removeFlag is "true" then
            delete every Word comment of openedDocument
        end if
        save openedDocument
        set openedName to name of openedDocument
        return "WORD_CLEAN_OK|" & openedName
    end tell
end run
'''

EXPORT_PDF_JXA = r'''
function run(argv) {
    var word = Application("Microsoft Word");
    word.open(Path(argv[0]));
    var document = word.documents.byName(argv[2]);
    word.saveAs(document, {fileName: argv[1], fileFormat: 17});
    return "WORD_PDF_OK";
}
'''

ACCESS_PROMPT_COUNT_SCRIPT = r'''
tell application "System Events"
    if not (exists process "Microsoft Word") then return "0"
    tell process "Microsoft Word"
        return ((count of (windows whose name is "授予文件访问权限")) + (count of (windows whose name is "Grant File Access"))) as text
    end tell
end tell
'''

ACCESS_PROMPT_CANCEL_SCRIPT = r'''
tell application "System Events" to tell process "Microsoft Word"
    if (count of (windows whose name is "授予文件访问权限")) > 0 then
        click button "取消" of item 1 of (windows whose name is "授予文件访问权限")
    else if (count of (windows whose name is "Grant File Access")) > 0 then
        click button "Cancel" of item 1 of (windows whose name is "Grant File Access")
    end if
end tell
'''

CLOSE_NAMED_OBJECT_SCRIPT = r'''
on run argv
    set targetName to item 1 of argv
    tell application "Microsoft Word"
        set matchedDocuments to every document whose name is targetName
        if (count of matchedDocuments) is 0 then return "WORD_OBJECT_CLOSE_NOT_FOUND"
        if (count of matchedDocuments) is not 1 then return "WORD_OBJECT_CLOSE_AMBIGUOUS"
        set targetDocument to item 1 of matchedDocuments
        set saved of targetDocument to true
    end tell
    tell application "System Events"
        tell process "Microsoft Word"
            set matchedWindows to {}
            repeat with candidateWindow in windows
                try
                    set documentURL to value of attribute "AXDocument" of candidateWindow as text
                    if documentURL ends with targetName then
                        set end of matchedWindows to candidateWindow
                    end if
                end try
            end repeat
            if (count of matchedWindows) is 0 then return "WORD_OBJECT_WINDOW_NOT_FOUND"
            if (count of matchedWindows) is not 1 then return "WORD_OBJECT_WINDOW_AMBIGUOUS"
            set targetWindow to item 1 of matchedWindows
            if (count of buttons of targetWindow) is 0 then return "WORD_OBJECT_CLOSE_UNAVAILABLE"
            set targetCloseButton to button 1 of targetWindow
            if (subrole of targetCloseButton as text) is not "AXCloseButton" then return "WORD_OBJECT_CLOSE_UNAVAILABLE"
        end tell
    end tell
    tell application "Microsoft Word"
        set matchedDocuments to every document whose name is targetName
        if (count of matchedDocuments) is not 1 then return "WORD_OBJECT_CLOSE_RACE"
    end tell
    tell application "System Events" to tell process "Microsoft Word" to click targetCloseButton
    return "WORD_OBJECT_CLOSE_SENT"
end run
'''

CHECK_NAMED_DOCUMENT_SCRIPT = r'''
on run argv
    set targetName to item 1 of argv
    tell application "Microsoft Word"
        set matchedDocuments to every document whose name is targetName
        if (count of matchedDocuments) > 0 then
            return "WORD_DOCUMENT_OPEN"
        end if
    end tell
    return "WORD_DOCUMENT_CLOSED"
end run
'''


def _access_prompt_count() -> int | None:
    try:
        completed = subprocess.run(
            ["osascript", "-e", ACCESS_PROMPT_COUNT_SCRIPT],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return int(completed.stdout.strip()) if completed.returncode == 0 else None
    except ValueError:
        return None


def _run_close_script(script: str, name: str, timeout_seconds: int = 8) -> str | None:
    try:
        completed = subprocess.run(
            ["osascript", "-e", script, "--", name],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _stop_process(process: subprocess.Popen[str], timeout_seconds: int = 3) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            pass


def _word_document_is_open(name: str) -> bool | None:
    result = _run_close_script(CHECK_NAMED_DOCUMENT_SCRIPT, name, timeout_seconds=5)
    if result == "WORD_DOCUMENT_OPEN":
        return True
    if result == "WORD_DOCUMENT_CLOSED":
        return False
    return None


def _word_lock_path(staged_path: Path) -> Path:
    name = staged_path.name
    lock_name = f"~${name[2:]}" if len(name) > 2 else f"~${name}"
    return staged_path.with_name(lock_name)


def _cleanup_state(name: str, staged_path: Path) -> tuple[bool | None, bool]:
    return _word_document_is_open(name), _word_lock_path(staged_path).exists()


def _wait_for_word_cleanup(name: str, staged_path: Path, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        is_open, lock_exists = _cleanup_state(name, staged_path)
        if is_open is False and not lock_exists:
            return True
        if is_open is False and lock_exists:
            try:
                _word_lock_path(staged_path).unlink(missing_ok=True)
            except OSError:
                pass
        if is_open is None or time.monotonic() >= deadline:
            return False
        time.sleep(WORD_CLEANUP_POLL_SECONDS)


def _close_safe_document(
    name: str,
    staged_path: Path,
    timeout_seconds: int = WORD_CLEANUP_TIMEOUT_SECONDS,
) -> bool:
    """Close exactly the safe document and prove Word plus lock-file postconditions."""
    object_result = _run_close_script(CLOSE_NAMED_OBJECT_SCRIPT, name)
    if object_result == "WORD_OBJECT_CLOSE_AMBIGUOUS":
        return False
    if object_result not in {"WORD_OBJECT_CLOSE_SENT", "WORD_OBJECT_CLOSE_NOT_FOUND"}:
        return False
    return _wait_for_word_cleanup(name, staged_path, timeout_seconds)


def _remove_staged_paths(paths: list[Path], timeout_seconds: int = 5) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if all(not path.exists() for path in paths):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(WORD_CLEANUP_POLL_SECONDS)


def _cleanup_word_staging(name: str, staged_paths: list[Path], timeout_seconds: int) -> bool:
    docx_paths = [path for path in staged_paths if path.suffix.lower() == ".docx"]
    if not docx_paths:
        return False
    staged_docx = docx_paths[0]
    if not _close_safe_document(name, staged_docx, timeout_seconds):
        return False
    return _remove_staged_paths(staged_paths)


def _prepare_word_staging(name: str, staged_paths: list[Path]) -> bool:
    docx_paths = [path for path in staged_paths if path.suffix.lower() == ".docx"]
    if not docx_paths:
        return False
    staged_docx = docx_paths[0]
    if not _close_safe_document(name, staged_docx, WORD_CLEANUP_TIMEOUT_SECONDS):
        return False
    return _remove_staged_paths(staged_paths)


def validate_docx_with_word(path: Path, mode: str = "required", timeout_seconds: int = 45) -> tuple[bool, list[str]]:
    """Open and close only the requested DOCX in Microsoft Word."""
    if mode not in WORD_VALIDATION_MODES:
        return False, ["word_native_validation_mode_invalid"]
    if mode == "off":
        return True, []
    if platform.system() != "Darwin" or not WORD_APP.is_dir():
        return False, ["word_native_validation_unavailable"]
    if not path.is_file() or path.suffix.lower() != ".docx":
        return False, ["word_native_validation_input_invalid"]
    try:
        WORD_STAGING_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        safe_stem = re.sub(r"[^A-Za-z0-9_-]", "-", path.stem)[:42]
        staged_path = WORD_STAGING_ROOT / f"VALIDATE-{safe_stem}.docx"
        if not _prepare_word_staging(staged_path.name, [staged_path]):
            return False, ["word_native_cleanup_failed"]
        shutil.copy2(path, staged_path)
    except OSError:
        return False, ["word_native_validation_staging_failed"]
    prompt_count_before = _access_prompt_count()
    try:
        process = subprocess.Popen(
            ["osascript", "-e", OPEN_AND_CLOSE_SCRIPT, "--", str(staged_path.resolve())],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        staged_path.unlink(missing_ok=True)
        return False, ["word_native_validation_unavailable"]
    deadline = time.monotonic() + timeout_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.5)
        prompt_count = _access_prompt_count()
        if prompt_count_before is not None and prompt_count is not None and prompt_count > prompt_count_before:
            _stop_process(process)
            if prompt_count_before == 0:
                subprocess.run(
                    ["osascript", "-e", ACCESS_PROMPT_CANCEL_SCRIPT],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            if not _cleanup_word_staging(staged_path.name, [staged_path], WORD_CLEANUP_TIMEOUT_SECONDS):
                return False, ["word_native_cleanup_failed"]
            return False, ["word_native_file_access_not_granted"]
    if process.poll() is None:
        _stop_process(process)
        if not _cleanup_word_staging(staged_path.name, [staged_path], WORD_CLEANUP_TIMEOUT_SECONDS):
            return False, ["word_native_cleanup_failed"]
        return False, ["word_native_validation_timeout"]
    stdout, _stderr = process.communicate()
    if process.returncode != 0:
        if not _cleanup_word_staging(staged_path.name, [staged_path], WORD_CLEANUP_TIMEOUT_SECONDS):
            return False, ["word_native_cleanup_failed"]
        return False, ["word_native_validation_failed"]
    if not stdout.strip().startswith("WORD_OPEN_OK|"):
        if not _cleanup_word_staging(staged_path.name, [staged_path], WORD_CLEANUP_TIMEOUT_SECONDS):
            return False, ["word_native_cleanup_failed"]
        return False, ["word_native_validation_unconfirmed"]
    if not _cleanup_word_staging(staged_path.name, [staged_path], WORD_CLEANUP_TIMEOUT_SECONDS):
        return False, ["word_native_cleanup_failed"]
    return True, []


def _run_word_action(
    script: str,
    arguments: list[str],
    expected: str,
    timeout_seconds: int,
    opened_document_path: Path,
) -> tuple[bool, list[str]]:
    prompt_count_before = _access_prompt_count()
    try:
        process = subprocess.Popen(
            ["osascript", "-e", script, "--", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return False, ["word_native_validation_unavailable"]
    deadline = time.monotonic() + timeout_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.5)
        prompt_count = _access_prompt_count()
        if prompt_count_before is not None and prompt_count is not None and prompt_count > prompt_count_before:
            _stop_process(process)
            if prompt_count_before == 0:
                subprocess.run(["osascript", "-e", ACCESS_PROMPT_CANCEL_SCRIPT], check=False, timeout=5)
            if not _cleanup_word_staging(
                opened_document_path.name,
                [opened_document_path],
                WORD_CLEANUP_TIMEOUT_SECONDS,
            ):
                return False, ["word_native_cleanup_failed"]
            return False, ["word_native_file_access_not_granted"]
    if process.poll() is None:
        _stop_process(process)
        if not _cleanup_word_staging(
            opened_document_path.name,
            [opened_document_path],
            WORD_CLEANUP_TIMEOUT_SECONDS,
        ):
            return False, ["word_native_cleanup_failed"]
        return False, ["word_native_action_timeout"]
    stdout, _stderr = process.communicate()
    if process.returncode != 0 or not stdout.strip().startswith(expected):
        if not _cleanup_word_staging(
            opened_document_path.name,
            [opened_document_path],
            WORD_CLEANUP_TIMEOUT_SECONDS,
        ):
            return False, ["word_native_cleanup_failed"]
        return False, ["word_native_action_failed"]
    if not _close_safe_document(
        opened_document_path.name,
        opened_document_path,
        WORD_CLEANUP_TIMEOUT_SECONDS,
    ):
        return False, ["word_native_cleanup_failed"]
    return True, []


def create_clean_copy_with_word(
    source_path: Path,
    output_path: Path,
    *,
    accept_revisions: bool,
    remove_comments: bool,
    timeout_seconds: int = 90,
) -> tuple[bool, list[str]]:
    output_path.unlink(missing_ok=True)
    try:
        WORD_STAGING_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        safe_stem = re.sub(r"[^A-Za-z0-9_-]", "-", output_path.stem)[:42]
        staged_path = WORD_STAGING_ROOT / f"CLEAN-{safe_stem}.docx"
        if not _prepare_word_staging(staged_path.name, [staged_path]):
            return False, ["word_native_cleanup_failed"]
        shutil.copy2(source_path, staged_path)
    except OSError:
        return False, ["word_native_clean_copy_prepare_failed"]
    ok, errors = _run_word_action(
        CLEAN_COPY_SCRIPT,
        [str(staged_path.resolve()), str(accept_revisions).lower(), str(remove_comments).lower()],
        "WORD_CLEAN_OK",
        timeout_seconds,
        staged_path,
    )
    if not ok or not staged_path.is_file():
        output_path.unlink(missing_ok=True)
        return False, errors or ["word_native_clean_copy_missing"]
    try:
        shutil.copy2(staged_path, output_path)
    except OSError:
        output_path.unlink(missing_ok=True)
        if not _remove_staged_paths([staged_path]):
            return False, ["word_native_cleanup_failed"]
        return False, ["word_native_clean_copy_copyback_failed"]
    if not _remove_staged_paths([staged_path]):
        output_path.unlink(missing_ok=True)
        return False, ["word_native_cleanup_failed"]
    return True, []


def export_pdf_with_word(source_path: Path, output_path: Path, timeout_seconds: int = 90) -> tuple[bool, list[str]]:
    output_path.unlink(missing_ok=True)
    safe_stem = re.sub(r"[^A-Za-z0-9_-]", "-", source_path.stem)[:48]
    try:
        WORD_STAGING_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        staged_docx = WORD_STAGING_ROOT / f"{safe_stem}.docx"
        staged_pdf = WORD_STAGING_ROOT / f"{safe_stem}.pdf"
        if not _prepare_word_staging(staged_docx.name, [staged_docx, staged_pdf]):
            return False, ["word_native_cleanup_failed"]
        shutil.copy2(source_path, staged_docx)
    except OSError:
        return False, ["word_native_pdf_staging_failed"]
    completed: subprocess.CompletedProcess[str] | None = None
    action_error: str | None = None
    try:
        completed = subprocess.run(
            [
                "osascript", "-l", "JavaScript", "-e", EXPORT_PDF_JXA, "--",
                str(staged_docx.resolve()), str(staged_pdf.resolve()), staged_docx.name,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        action_error = "word_native_pdf_timeout"
    except OSError:
        action_error = "word_native_validation_unavailable"

    close_ok = _close_safe_document(
        staged_docx.name,
        staged_docx,
        WORD_CLEANUP_TIMEOUT_SECONDS,
    )
    if not close_ok:
        return False, ["word_native_cleanup_failed"]

    if action_error is None:
        if completed is None or completed.returncode != 0 or not completed.stdout.strip().startswith("WORD_PDF_OK"):
            action_error = "word_native_pdf_export_failed"
        elif not staged_pdf.is_file() or staged_pdf.stat().st_size < 100 or not staged_pdf.read_bytes().startswith(b"%PDF"):
            action_error = "word_native_pdf_missing"

    if action_error is None:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged_pdf, output_path)
        except OSError:
            output_path.unlink(missing_ok=True)
            action_error = "word_native_pdf_copyback_failed"

    if not _remove_staged_paths([staged_docx, staged_pdf]):
        output_path.unlink(missing_ok=True)
        return False, ["word_native_cleanup_failed"]
    if action_error is not None:
        output_path.unlink(missing_ok=True)
        return False, [action_error]
    return True, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate one local DOCX with Microsoft Word for Mac.")
    parser.add_argument("path")
    parser.add_argument("--timeout-seconds", type=int, default=45)
    args = parser.parse_args()
    ok, errors = validate_docx_with_word(Path(args.path), "required", args.timeout_seconds)
    if ok:
        print("word_native_validation_succeeded")
        return 0
    print(errors[0] if errors else "word_native_validation_failed", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
