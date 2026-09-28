@echo off
setlocal EnableExtensions
pushd "%~dp0"
if errorlevel 1 (
  echo Cannot access the DevCockpit project directory.
  goto :failed_without_directory
)

if not exist "config.yml" (
  echo Missing config.yml. Define repositories and GitHub Projects first.
  goto :failed
)

where gh >nul 2>&1
if errorlevel 1 (
  echo GitHub CLI ^(gh^) is required on Windows. Install it and run gh auth login.
  goto :failed
)
gh auth status >nul 2>&1
if errorlevel 1 (
  echo GitHub CLI is not authenticated on Windows. Run gh auth login.
  goto :failed
)

where py >nul 2>&1
if not errorlevel 1 (
  set "PYTHON=py -3"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python 3.10 or newer is required on Windows.
    goto :failed
  )
  set "PYTHON=python"
)
%PYTHON% -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 (
  echo Python 3.10 or newer is required on Windows.
  goto :failed
)

if not exist ".venv-win\Scripts\python.exe" (
  echo Creating Windows virtual environment...
  %PYTHON% -m venv ".venv-win"
  if errorlevel 1 goto :failed
)

if not exist ".venv-win\requirements.txt" goto :install
fc /b "requirements.txt" ".venv-win\requirements.txt" >nul 2>&1
if errorlevel 1 goto :install
goto :start

:install
echo Installing Python dependencies...
".venv-win\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto :failed
copy /y "requirements.txt" ".venv-win\requirements.txt" >nul
if errorlevel 1 goto :failed

:start
if not defined PORT set "PORT=7777"
echo Starting DevCockpit at http://127.0.0.1:%PORT%/
echo Keep this window open while using DevCockpit. Press Ctrl+C to stop it.
".venv-win\Scripts\python.exe" -m app
if errorlevel 1 goto :failed
popd
exit /b 0

:failed
popd
:failed_without_directory
echo DevCockpit did not start. Review the message above.
pause
exit /b 1
