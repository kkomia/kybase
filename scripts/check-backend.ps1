# 后端门禁（**只改后端时跑这一个**）：ruff + 分层纪律 + emoji + API 类型核对 + pytest
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
# **没有域间引用检查**（原先那一步跑 scripts/check_domains.py）：它守的是"KB 域 ↔ Agent 域
# 不许互引"那张归属表，而本仓只剩 KB 域——名单里的 Agent 侧模块整族已删，跑起来只是围着
# 不存在的模块报旧账，还会往 docs/计划与记录/ 写一份本仓没有的报告目录。分层引用方向本身
# 由上面那条 check_layering.py 全量守着（L1–L6 覆盖 api / services / storage / parsers）。
#
# **不生成《API 接口规范》**（原先那一步跑 scripts/gen_api_spec.py）：那个脚本的正文是
# kylab 时代的（整段讲技能分类 / 精选），落点 docs/规范/ 本仓也没有，跑一次就是
# FileNotFoundError。接口事实的权威处是运行中的 /api/v1/openapi.json。
# 前端类型与后端 schema 的契约核对：改后端的人最该跑它——schema 动了而前端没跟上时直接红
Invoke-Step '核对 API 类型（前端 ← OpenAPI）' (Join-Path $root 'backend/.venv/Scripts/python.exe') @("$root/scripts/gen_api_types.py", '--check')
Invoke-Step '后端测试' 'uv' @('run', '--directory', "$root/backend", 'pytest', 'tests', '-m', 'not bench and not cloud', '-q')

if ($script:failures -gt 0) {
    Write-Host "后端门禁未通过（$script:failures 项）" -ForegroundColor Red
    exit 1
}
Write-Host '后端门禁全绿' -ForegroundColor Green
