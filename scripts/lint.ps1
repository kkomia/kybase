# 代码规范检查（提交前跑一遍）：ruff + emoji 扫描 + 分层纪律 + eslint/prettier
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts\lint.ps1
#      （装了 PowerShell 7 也可用 pwsh -File scripts\lint.ps1）
#
# 两条平台约束，改动本文件前务必先读：
#
# 1. 本文件必须保存为 UTF-8 with BOM —— Windows PowerShell 5.1 会把无 BOM 的 .ps1 按 GBK 解码，
#    中文输出直接变乱码。
# 2. 调用外部命令时**不要重定向输出流**（不要写 2>&1，也不要在外层用 *> 收集本脚本输出）。
#    PS 5.1 会把原生命令的 stderr 包装成 ErrorRecord，而 pnpm 的 pnpm.ps1 包装器一旦处于重定向下
#    就会以 NativeCommandError 中断并以 1 退出——于是 eslint 明明通过、门禁却报红。
#    这里只读取 $LASTEXITCODE，输出直接透传到控制台。
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
# 同步《API 接口规范》的端点清单（T4.9）：同上，让文档不可能旧
# 同 lint.sh：这一步要 import app（含 duckdb），必须用 venv 解释器
$venvPy = "$root/backend/.venv/Scripts/python.exe"
if (Test-Path $venvPy) {
    Invoke-Step '同步 API 接口规范' $venvPy @("$root/scripts/gen_api_spec.py")
# 前端类型与后端 schema 的契约核对（T4.9 的姊妹项）：不一致就红，
# 不像上面那条把文档改掉——类型参与编译，静默重写等于把"契约变了"藏起来
Invoke-Step '核对 API 类型（前端 ← OpenAPI）' $venvPy @("$root/scripts/gen_api_types.py", '--check')
} else {
    Write-Host '==> 同步 API 接口规范（跳过：找不到 venv 解释器）' -ForegroundColor DarkGray
}

if (Test-Path "$root/frontend/package.json") {
    if (-not (Test-Path "$root/frontend/node_modules")) {
        Invoke-Step 'pnpm install' 'pnpm' @('--dir', "$root/frontend", 'install')
    }
    Invoke-Step 'eslint + prettier' 'pnpm' @('--dir', "$root/frontend", 'lint')
    Invoke-Step '类型检查（tsc）' 'pnpm' @('--dir', "$root/frontend", 'typecheck')
    Invoke-Step 'emoji 扫描（前端）' 'python' @("$root/scripts/scan_emoji.py", "$root/frontend/src")
}

if ($script:failures -gt 0) {
    Write-Host "共 $script:failures 项检查失败" -ForegroundColor Red
    exit 1
}
Write-Host '全部检查通过' -ForegroundColor Green
