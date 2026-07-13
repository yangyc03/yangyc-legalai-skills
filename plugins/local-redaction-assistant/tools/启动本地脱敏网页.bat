@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"
set "RUNTIME_SCRIPT=%USERPROFILE%\.codex\skills\local-redaction-assistant\scripts\web_app.py"
set "SOURCE_SCRIPT=%REPO_ROOT%\skills\local-redaction-assistant\scripts\web_app.py"

if exist "%RUNTIME_SCRIPT%" (
  set "WEB_SCRIPT=%RUNTIME_SCRIPT%"
  echo 使用已同步的 local-redaction-assistant runtime。
) else if exist "%SOURCE_SCRIPT%" (
  set "WEB_SCRIPT=%SOURCE_SCRIPT%"
  echo runtime 尚未同步，使用当前仓库源版本。
) else (
  echo 找不到本地脱敏网页入口。
  echo %RUNTIME_SCRIPT%
  echo %SOURCE_SCRIPT%
  pause
  exit /b 1
)

set "BUNDLED_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
set "PDF_PYTHON=%USERPROFILE%\.venvs\local-redaction-assistant-pdf\Scripts\python.exe"

if exist "%BUNDLED_PYTHON%" (
  "%BUNDLED_PYTHON%" -c "import pymupdf" >nul 2>nul
  if not errorlevel 1 goto :run_bundled
)
if exist "%PDF_PYTHON%" (
  "%PDF_PYTHON%" -c "import pymupdf" >nul 2>nul
  if not errorlevel 1 goto :run_pdf_python
)
if exist "%BUNDLED_PYTHON%" goto :run_bundled_without_pdf
where py >nul 2>nul
if not errorlevel 1 goto :run_py_launcher
where python >nul 2>nul
if not errorlevel 1 goto :run_system_python
echo 找不到 Codex bundled Python、PDF 专用 Python、py -3 或 python。
pause
exit /b 1

:run_bundled
echo 使用 Codex bundled Python（含 PDF 预览依赖）。
"%BUNDLED_PYTHON%" "%WEB_SCRIPT%" --port 0
set "EXIT_CODE=%ERRORLEVEL%"
goto :finish

:run_pdf_python
echo Bundled Python 缺少 PyMuPDF，切换到本机 PDF 专用环境。
"%PDF_PYTHON%" "%WEB_SCRIPT%" --port 0
set "EXIT_CODE=%ERRORLEVEL%"
goto :finish

:run_bundled_without_pdf
echo 警告：当前 Python 缺少 PyMuPDF，PDF/图片流程可能不可用。
"%BUNDLED_PYTHON%" "%WEB_SCRIPT%" --port 0
set "EXIT_CODE=%ERRORLEVEL%"
goto :finish

:run_py_launcher
echo 使用 Windows Python Launcher 启动本机脱敏网页。
py -3 "%WEB_SCRIPT%" --port 0
set "EXIT_CODE=%ERRORLEVEL%"
goto :finish

:run_system_python
echo 使用系统 Python 启动本机脱敏网页。
python "%WEB_SCRIPT%" --port 0
set "EXIT_CODE=%ERRORLEVEL%"

:finish

echo 本地脱敏网页已退出，退出码：%EXIT_CODE%
pause
exit /b %EXIT_CODE%
