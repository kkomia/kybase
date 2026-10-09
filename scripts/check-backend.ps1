# 后端门禁（**只改后端时跑这一个**）：ruff + 分层纪律 + emoji + API 文档同步 + pytest
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts\check-backend.ps1
# 范围纪律见 check-frontend.ps1 的说明与开发计划 §12.24。
$root = Split-Path -Parent $PSScriptRoot
$script:failures = 0

function Invoke-Step {
    param([string]$Label, [string]$Command, [string[]]$Arguments)
    Write-Host "==> $Label" -ForegroundColor Cyan
    & $Command @Arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Host "!! $Label 失败（exit $code）" -ForegroundColor Red
        $script:failures++
    }
    else {
        Write-Host '    OK' -ForegroundColor DarkGray
    }
}

Invoke-Step 'ruff' 'uv' @('run', '--directory', "$root/backend", 'ruff', 'check', 'app/', 'tests/')
Invoke-Step 'emoji 扫描（后端）' 'python' @("$root/scripts/scan_emoji.py", "$root/backend/app")
Invoke-Step '结构性规范（分层 / 测试位置 / 界面文案 / 版本号）' 'python' @("$root/scripts/check_layering.py", $root)
Invoke-Step '同步 API 接口规范' (Join-Path $root 'backend/.venv/Scripts/python.exe') @("$root/scripts/gen_api_spec.py")
# 前端类型与后端 schema 的契约核对：改后端的人最该跑它——schema 动了而前端没跟上时直接红
Invoke-Step '核对 API 类型（前端 ← OpenAPI）' (Join-Path $root 'backend/.venv/Scripts/python.exe') @("$root/scripts/gen_api_types.py", '--check')
Invoke-Step '后端测试' 'uv' @('run', '--directory', "$root/backend", 'pytest', 'tests', '-m', 'not bench and not cloud', '-q')

if ($script:failures -gt 0) {
    Write-Host "后端门禁未通过（$script:failures 项）" -ForegroundColor Red
    exit 1
}
Write-Host '后端门禁全绿' -ForegroundColor Green
