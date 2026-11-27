@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PORT=%GW_PORT%"
if "%PORT%"=="" set "PORT=8090"

title 公文与材料智能工作台 (GW-Workbench v3.3.0)

echo =======================================================
echo   公文与材料智能工作台 (GW-Workbench v3.3.0)
echo =======================================================
echo.

cd /d "%~dp0"

:: 实测校验真实 Python 3.8+ 环境，抵御 Windows Store 假 python.exe 占位符
set "PY_CMD="
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,8) else 1)" >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=python"
) else (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,8) else 1)" >nul 2>&1
    if %errorlevel% equ 0 (
        set "PY_CMD=py -3"
    )
)

if "%PY_CMD%"=="" (
    echo [错误] 未检测到可用的 Python 3.8 或以上版本！
    echo 如系统自带 Microsoft Store 假 python.exe 占位符，请从 python.org 安装正式版 Python 3.8+ 并勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)

:: 确保必要数据目录存在
if not exist "data" mkdir "data"
if not exist "workspace_files" mkdir "workspace_files"

:: 启动前检测端口是否已被占用
%PY_CMD% -c "import socket, sys; s = socket.socket(); s.settimeout(0.3); rc = s.connect_ex(('127.0.0.1', int(sys.argv[1]))); s.close(); raise SystemExit(0 if rc == 0 else 1)" "%PORT%" >nul 2>&1
if %errorlevel% equ 0 (
    echo [提示] 检测到端口 %PORT% 已被占用（可能工作台已在运行）。
    echo [*] 正在直接打开 http://127.0.0.1:%PORT% ...
    start "" "http://127.0.0.1:%PORT%"
    echo [*] 如需更换端口，可先执行: set GW_PORT=8091
    pause
    exit /b 0
)

:: 启动提示
echo [*] 正在启动工作台核心服务 (端口: %PORT%，解释器: %PY_CMD%)...
echo [*] 本机访问地址: http://127.0.0.1:%PORT%
echo [*] 局域网协同访问: http://^<你的局域网IP^>:%PORT%
echo [*] 提示: 请勿关闭此控制台窗口（最小化即可常驻运行）。
echo =======================================================
echo.

:: 后台轮询探测端口就绪后自动在默认浏览器中打开页面（最多等待 15 秒）
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "$port=%PORT%; for($i=0; $i -lt 50; $i++){ try { $c = New-Object System.Net.Sockets.TcpClient('127.0.0.1', $port); $c.Close(); Start-Process ('http://127.0.0.1:' + $port); break } catch { Start-Sleep -Milliseconds 300 } }"

:: 运行主服务
%PY_CMD% server.py %PORT%
if %errorlevel% neq 0 (
    echo.
    echo [提示] 服务已停止。
    pause
)
