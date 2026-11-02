@echo off
rem Double-click: install agent-recall for the current user
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0brainctl.ps1" install %*
pause
