# 启动前端开发服务（Vite）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\dev-frontend.ps1
# 本文件须以 UTF-8 with BOM 保存（Windows PowerShell 5.1 对无 BOM 的 .ps1 按 GBK 解码）
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $root 'frontend')
try {
    if (-not (Test-Path 'node_modules')) { pnpm install }
    pnpm dev
}
finally {
    Pop-Location
}
