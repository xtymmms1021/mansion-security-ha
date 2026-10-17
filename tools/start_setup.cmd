@echo off
setlocal
set "MANSION_SETUP_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%MANSION_SETUP_PYTHON%" (
  "%MANSION_SETUP_PYTHON%" "%~dp0account_setup.py" --open-browser
  goto done
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0account_setup.py" --open-browser
  goto done
)
where python >nul 2>nul
if not errorlevel 1 (
  python "%~dp0account_setup.py" --open-browser
  goto done
)
echo Python 3.12 or newer is required to run this local setup tool.
:done
pause
