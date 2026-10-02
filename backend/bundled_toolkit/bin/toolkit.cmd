@echo off
rem toolkit wrapper shim (toolkit install <name> ...). Delegates to toolkit.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0toolkit.ps1" %*
