@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 "%SCRIPT_DIR%backend\apocalypse_ui.py" %*
) else (
  python "%SCRIPT_DIR%backend\apocalypse_ui.py" %*
)
endlocal
