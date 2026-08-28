@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "APP_ROOT=%~dp0.."
set "APP_PY=%APP_ROOT%\runtime\python\python.exe"
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"
set "TCL_LIBRARY=%APP_ROOT%\runtime\python\tcl\tcl8.6"
set "TK_LIBRARY=%APP_ROOT%\runtime\python\tcl\tk8.6"
if not exist "%APP_PY%" (
  echo PNG Mockup private runtime is missing. Run PNG Mockup Setup.exe again.
  exit /b 10
)
"%APP_PY%" -I -B -X utf8 %*
exit /b %errorlevel%
