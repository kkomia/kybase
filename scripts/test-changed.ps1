# 只跑"这次改动影响到的"用例——工程规范 §5.2.1 那条内环纪律的可执行版本。
#
# （与 test-changed.sh 同一份逻辑：判定在 scripts/affected_tests.py 里，两个壳只负责
#   找解释器、借测试库、把参数原样传下去。）
#
# 分工（与规范 §5.2.1 那张表一致，**不改变标准**）：
#   内环（写代码 → 看行为）  →  powershell -File scripts\test-changed.ps1
#   一个大版块收尾 / 交付前  →  scripts\check-backend.ps1 && scripts\check-frontend.ps1
#   CI                       →  全量（裁判地位不受影响）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\test-changed.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\test-changed.ps1 --base origin/main
#   powershell -ExecutionPolicy Bypass -File scripts\test-changed.ps1 --list
#
# 平台约束（与 lint.ps1 同）：**UTF-8 with BOM 保存**——Windows PowerShell 5.1
# 会把无 BOM 的 .ps1 当 GBK 解码，下面的中文注释直接变乱码。
#
# 没设 KYLAB_TEST_DATABASE_URL 时从 backend\.env 借（与 test-backend.sh 同一口径）。
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root 'backend\.env'

$venvPy = Join-Path $root 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    $venvPy = Join-Path $root 'backend\.venv\bin\python'
}
if (-not (Test-Path $venvPy)) {
    Write-Host "找不到 venv 解释器：$venvPy" -ForegroundColor Red
    Write-Host '先建虚拟环境并装依赖（见 backend/README.md）'
    exit 2
}

if (-not $env:KYLAB_TEST_DATABASE_URL -and (Test-Path $envFile)) {
    $found = Select-String -Path $envFile -Pattern '^KYLAB_DATABASE_URL=(.*)$' |
        Select-Object -First 1
    if ($found) {
        $env:KYLAB_TEST_DATABASE_URL = $found.Matches[0].Groups[1].Value.Trim()
        # 只露主机名与库名：口令留在变量里（与 test-backend.sh 同）
        $safe = $env:KYLAB_TEST_DATABASE_URL -replace '^[^:]*://[^@]*@', ''
        Write-Host "==> 借 backend\.env 里的库作维护连接：$safe" -ForegroundColor Cyan
    }
}

# 只想看判定结果（--list / --json / --dry-run）时不要顺手跑起来。
$mode = @('--run')
foreach ($arg in $args) {
    if ($arg -in @('--list', '--json', '--dry-run')) {
        $mode = @()
    }
}

& $venvPy (Join-Path $root 'scripts\affected_tests.py') @mode @args
exit $LASTEXITCODE
