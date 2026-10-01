@echo off
rem Double-click to start Minecraft Skill Builder. Close this window (or press Ctrl+C) to stop it.
title Minecraft Skill Builder
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-windows.ps1" %*
pause
