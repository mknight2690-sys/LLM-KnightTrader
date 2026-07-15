@echo off
title LLM KnightTrader Agent CLI
cd /d "%~dp0.."
set PYTHONPATH=%~dp0..
python agent_cli.py %*
if errorlevel 1 (
    echo.
    echo Agent CLI exited with error.
    pause
)
