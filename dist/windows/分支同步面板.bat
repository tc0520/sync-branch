@echo off
chcp 65001 >nul
title 分支同步面板

where git >nul 2>nul
if errorlevel 1 (
    echo 未找到 git，请先安装 Git for Windows: https://git-scm.com/download/win
    pause
    exit /b 1
)

cd /d "%~dp0"
set "SYNC_DEFAULT_BASE=%USERPROFILE%"

rem 优先用 pythonw（无黑窗口）启动 Web 面板，其次 python
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw sync-branches-ui.py
    exit /b 0
)
where python >nul 2>nul
if not errorlevel 1 (
    python sync-branches-ui.py
    exit /b 0
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 sync-branches-ui.py
    exit /b 0
)
echo 未找到 Python，请先安装 Python 3: https://www.python.org/downloads/
echo 安装时记得勾选 "Add Python to PATH"
pause
exit /b 1
