@echo off
:: Run the main logic through PowerShell to avoid cmd encoding issues
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
