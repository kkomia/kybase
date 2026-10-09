# 启动后端开发服务（FastAPI + 热重载）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\dev-backend.ps1
# 本文件须以 UTF-8 with BOM 保存（Windows PowerShell 5.1 对无 BOM 的 .ps1 按 GBK 解码）
$ErrorActionPreference = 'Stop'

$port = 8000

# **端口先探一次**（v0.37）：Windows 上 SO_REUSEADDR 会让第二个实例**绑得上**同一个端口，
# 于是"再启动一次"不会报错，而是变成两个后端同时在跑——连接落到哪一个由系统挑，
# 表现为"我明明改了后端，界面却在报 Method Not Allowed / 找不到路由"（见《开发计划》§12.218）
$busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = ($busy | Select-Object -First 1).OwningProcess
    Write-Error "端口 $port 已经被占用（PID $owner）——先停掉那个进程再启动：Stop-Process -Id $owner -Force"
    exit 1
}

$root = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $root 'backend')
try {
    # **--all-extras 不是可选的**：裸 `uv sync` 会把 extras 卸掉，而后端启动时要 import duckdb
    uv sync --all-extras
    uv run uvicorn app.main:app --reload --host 127.0.0.1 --port $port
}
finally {
    Pop-Location
}
