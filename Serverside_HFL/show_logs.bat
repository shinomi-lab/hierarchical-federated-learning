@echo off
REM Show organized logs (newest-first)
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\list_organized.ps1" %*
