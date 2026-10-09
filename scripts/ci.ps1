# CI 门禁（本地复现 CI 用）：规范检查 + 后端测试 + 前端测试
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts\ci.ps1
#      （装了 PowerShell 7 也可用 pwsh -File scripts\ci.ps1）
#
# 平台约束（与 lint.ps1 同）：UTF-8 with BOM 保存；调用外部命令不重定向输出流，
# 只读 $LASTEXITCODE 判断成败，输出直接透传控制台。
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

# 用当前 PowerShell 宿主执行 lint.ps1，拿到它真实的退出码（用 & 调用会在其 exit 时打断本脚本）
if ($PSVersionTable.PSEdition -eq 'Core') {
    $hostExe = Join-Path $PSHOME 'pwsh.exe'
}
else {
    $hostExe = Join-Path $PSHOME 'powershell.exe'
}
Invoke-Step '规范检查（scripts/lint.ps1）' $hostExe @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'lint.ps1')
)

# **没有测试库就不许装绿**：v0.12 起存储只有 PostgreSQL（SQLite 已退役），
# 没有 KYLAB_TEST_DATABASE_URL 时，依赖仓储的用例与所有走 create_app 的集成用例
# 会**整体跳过**——实测 `13 passed, 3207 skipped in 19.5s`，而本脚本照样打印
# "CI 门禁全绿"。这正是 check-backend.sh 里那条纪律要挡的东西（"绿得没有意义"），
# 两个入口的门槛必须一致，否则本地这份门禁就是一份假的。
if (-not $env:KYLAB_TEST_DATABASE_URL) {
    Write-Host '!! 未设置 KYLAB_TEST_DATABASE_URL' -ForegroundColor Red
    Write-Host '   v0.12 起存储只有 PostgreSQL，没有它仓储测试与所有走 create_app 的'
    Write-Host '   集成测试都会整体跳过，门禁会变成"绿得没有意义"。请指向一个带 pgvector 的库：'
    Write-Host '   $env:KYLAB_TEST_DATABASE_URL="postgresql://用户:口令@主机:5432/postgres"'
    Write-Host '   powershell -ExecutionPolicy Bypass -File scripts\ci.ps1'
    Write-Host '   只想跑测试（允许从 backend\.env 借库作维护连接）：'
    Write-Host '   powershell -ExecutionPolicy Bypass -File scripts\test-changed.ps1    # 或 sh scripts\test-backend.sh'
    $script:failures++
}
else {
    Invoke-Step '后端测试' 'uv' @(
        'run', '--directory', "$root/backend", 'pytest', 'tests',
        '-m', 'not bench and not cloud', '--cov=app', '--cov-report=term-missing'
    )
}

if (Test-Path "$root/frontend/package.json") {
    Invoke-Step '前端测试' 'pnpm' @('--dir', "$root/frontend", 'test')
    Invoke-Step '前端生产构建' 'pnpm' @('--dir', "$root/frontend", 'build')
}

if ($script:failures -gt 0) {
    Write-Host "CI 门禁未通过（$script:failures 项）" -ForegroundColor Red
    exit 1
}
Write-Host 'CI 门禁全绿' -ForegroundColor Green
