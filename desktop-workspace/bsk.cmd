@echo off
setlocal
where bsk.exe >nul 2>nul
if errorlevel 1 (
  echo BrowserSkill CLI not found. Install it from https://github.com/Tencent/BrowserSkill
  exit /b 127
)
bsk.exe %*
exit /b %errorlevel%
