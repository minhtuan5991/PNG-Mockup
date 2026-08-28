@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_setup.ps1" %*
exit /b %errorlevel%
