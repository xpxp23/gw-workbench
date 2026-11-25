@echo off
chcp 65001 >nul
title 公文与材料智能工作台 (GW-Workbench v3.1.0)

echo =======================================================
echo   公文与材料智能工作台 (GW-Workbench v3.1.0)
echo =======================================================
echo.

cd /d "%~dp0"

:: 检查 Python 环境
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python 环境！
    echo 请先安装 Python 3.7 或以上版本，并勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)

:: 确保必要数据目录存在
if not exist "data" mkdir "data"
if not exist "workspace_files" mkdir "workspace_files"

:: 启动提示
echo [*] 正在启动工作台核心服务 (端口: 8090)...
echo [*] 本机访问地址: http://127.0.0.1:8090
echo [*] 局域网协同访问: http://^<你的局域网IP^>:8090
echo [*] 提示: 请勿关闭此控制台窗口（最小化即可常驻运行）。
echo =======================================================
echo.

:: 延时 2 秒自动在默认浏览器中打开页面
start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8090"

:: 运行主服务
python server.py 8090
if %errorlevel% neq 0 (
    echo.
    echo [提示] 服务已停止。
    pause
)
