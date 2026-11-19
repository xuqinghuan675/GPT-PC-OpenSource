@echo off
setlocal
if defined GPT_PC_PYTHON (set "PYTHON=%GPT_PC_PYTHON%") else (set "PYTHON=python")
"%PYTHON%" "%~dp0agentmouse.py" %*
exit /b %errorlevel%
