# 启动本机边车（桌面壳拉起的那个后端：对话 + 本机数据）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\dev-sidecar.ps1
# 本文件须以 UTF-8 with BOM 保存（Windows PowerShell 5.1 对无 BOM 的 .ps1 按 GBK 解码）
#
# **它是什么**：本机档那个后端，与桌面壳拉起的是**同一个入口**（`python -m app.sidecar`，
# 参数口径见 desktop/src-tauri/src/sidecar.rs::arguments）。
#
# **为什么在浏览器里迭代 web 端要用它**：对话那一轮（`POST /turn/stream`，以及审批、会话、
# 笔记、设置、记忆、工作区这些本机权威面 `/api/v1/local/*`）都挂在它身上；而 `app.main:app`
# （scripts/dev-backend.ps1 那一份）**不挂 `/turn/*`** —— 只起那一份时对话一律报
# 「这一轮没跑起来：网络没连上（这条请求没有发出去）」（2026-10-05 实测真因）。
#
# **数据落在哪**：`--data-dir` 默认就是壳的真实数据目录（Tauri `app_data_dir()` 口径：
# Windows `%APPDATA%\com.kylab.desktop`、macOS `~/Library/Application Support/com.kylab.desktop`、
# Linux `~/.local/share/com.kylab.desktop`），工作区取 `<数据目录>\workspace` —— 与壳一致，
# 于是同一个库、同一批会话、同一份设置。要一份隔离开的数据（不碰壳里的真实数据）就设
# `KYLAB_DATA_DIR`。
#
# **它现在与壳同一个口径（地址与钥匙）**：启动时读壳的 `config.json`（`server` + `api_key`）
# → 给边车传 `--server {server}/api/v1` / `--token {api_key}`，于是这一档的知识库提供者
# （`GET /api/v1/local/provider`）与备份上传都拿得到地址与凭据 —— 与壳拉起它时传的
# 是同两项（`desktop/src-tauri/src/main.rs:434` 起那个口径）。钥匙串优先、回落
# `config.json`：`api_key` 被壳收进系统钥匙串的那些机器（M5 阶段 6B 之后是常态），
# 从 `kylab:nas_token:<地址>` 读同一把（壳的 `Shell::api_key_for` 就是这条链）。
# 两个环境变量可显式覆盖：`KYLAB_SERVER` / `KYLAB_TOKEN`（排障、连别的 NAS 用，
# **改了地址就一并给钥匙** —— 钥匙串是按地址归档的）。
# **密钥纪律**：钥匙只在变量与 argv 里活着 —— 不写日志、不进任何提示
# （argv 同机器可见，见 `desktop/src-tauri/src/sidecar.rs` 的头三条纪律）。
#
# **与 dev-backend.ps1 的关系**：两个进程可并存，同一份 SQLite 库（定时任务的认领是一次
# CAS，只会跑一遍，见 backend/app/sidecar.py::_lifespan）。`/api/**` 那条"服务器面"的请求
# 在浏览器里走 Vite 反代，**默认**仍打到 :8000（那是"服务器档"的开发后端，自己带知识库）；
# 要接 NAS 就设 `KYLAB_API_TARGET=http://192.168.31.18:8081` —— 那正是桌面壳的形态（壳把
# `/api/**` 转发到 NAS、`Authorization` 用页面自己的），出处与理由见 frontend/vite.config.ts
# 的那段注释。不设它时那些请求按各页自己的降级显示。
$ErrorActionPreference = 'Stop'

$port = if ($env:KYLAB_SIDECAR_PORT) { [int]$env:KYLAB_SIDECAR_PORT } else { 8765 }

# 数据目录按平台取（与 Tauri 的 app_data_dir() 同一个口径）
if ($env:KYLAB_DATA_DIR) {
    $dataDir = $env:KYLAB_DATA_DIR
}
elseif ($env:APPDATA) {
    $dataDir = Join-Path $env:APPDATA 'com.kylab.desktop'
}
elseif ($env:HOME -and (Test-Path (Join-Path $env:HOME 'Library'))) {
    $dataDir = Join-Path $env:HOME 'Library/Application Support/com.kylab.desktop'
}
else {
    $base = if ($env:XDG_DATA_HOME) { $env:XDG_DATA_HOME } else { Join-Path $env:HOME '.local/share' }
    $dataDir = Join-Path $base 'com.kylab.desktop'
}

# 目录不存在就建（与壳一致：工作区在数据目录下面），并统一成绝对路径
$dataDir = [System.IO.Path]::GetFullPath($dataDir)
$workspace = Join-Path $dataDir 'workspace'
New-Item -ItemType Directory -Force -Path $workspace | Out-Null

# **端口先探一次**（照 dev-backend.ps1）：Windows 上 SO_REUSEADDR 会让第二个实例**绑得上**
# 同一个端口，于是"再启动一次"不会报错，而是变成两个边车同时在跑——连接落到哪一个由系统挑，
# 表现为"我明明改了后端，界面却还是旧行为"。所以这里先看端口，占了就明确拒绝启动。
$busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = ($busy | Select-Object -First 1).OwningProcess
    Write-Error "端口 $port 已经被占用（PID $owner）——先停掉那个进程再启动：Stop-Process -Id $owner -Force"
    exit 1
}

$root = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $root 'backend')
try {
    # ---------------------------------------------------------------- 地址与钥匙
    # **与壳同一个口径**：壳起边车时传的就是 `--server {server}/api/v1` + `--token {壳里那把钥匙}`
    # （desktop/src-tauri/src/main.rs:434 起）。三个来源按下面的顺序取，**明文不进任何输出**：
    #   地址：KYLAB_SERVER → 壳的 config.json 的 server
    #   钥匙：KYLAB_TOKEN → 系统钥匙串 kylab:nas_token:<地址> → config.json 的 api_key
    # （钥匙串排在明文前面是照壳的 `Shell::api_key_for`：钥匙串优先、回落 config.json ——
    #  M5 阶段 6B 把明文收进钥匙串之后，config.json 里那一栏只是"老配置还读得动"。）
    $config = Join-Path $dataDir 'config.json'

    function Get-ConfigValue([string]$name) {
        # config.json 里的一个键；文件没有 / JSON 读不动 → 空串
        if (-not (Test-Path $config)) { return '' }
        try { $parsed = Get-Content -Raw -Encoding UTF8 -Path $config | ConvertFrom-Json } catch { return '' }
        $value = $parsed.$name
        if ($value -is [string]) { return $value.Trim() }
        return ''
    }

    function Get-KeychainValue([string]$origin) {
        # 失败了那个 CLI 会往 stdout 打一句人话，所以按退出码决定要不要它
        $value = $null
        try {
            $value = & uv run --all-extras python -m app.services.credentials nas-token `
                --data-dir $dataDir --origin $origin --show 2>$null
        }
        catch { return '' }
        if ($LASTEXITCODE -ne 0) { return '' }
        return ([string]($value | Out-String)).Trim()
    }

    $server = if ($env:KYLAB_SERVER) { $env:KYLAB_SERVER } else { '' }
    $serverSource = 'KYLAB_SERVER'
    if (-not $server) {
        $server = Get-ConfigValue 'server'
        $serverSource = '壳的 config.json'
    }

    $token = if ($env:KYLAB_TOKEN) { $env:KYLAB_TOKEN } else { '' }
    $tokenSource = 'KYLAB_TOKEN'
    if (-not $token -and $server) {
        $token = Get-KeychainValue $server
        $tokenSource = '系统钥匙串'
    }
    # **没有地址就一把钥匙都不给**：不给 `--server` 时边车会用自己那档默认地址
    # （`KYLAB_SERVER_URL` / :8000），把 NAS 的钥匙送到那儿去是"钥匙送错门"
    if (-not $token -and $server) {
        $token = Get-ConfigValue 'api_key'
        $tokenSource = '壳的 config.json'
    }

    # `--server` 要的是**含 /api/v1** 的基址（知识库提供者拿它拼 `/provider/handshake`）
    $server = $server.TrimEnd('/')
    if ($server -and -not $server.EndsWith('/api/v1')) { $server = "$server/api/v1" }

    if ($server) {
        if ($token) {
            Write-Host "边车：接上 $server（地址来自 $serverSource，钥匙来自 $tokenSource，不回显）"
        }
        else {
            Write-Host "边车：接上 $server（地址来自 $serverSource），但没有钥匙——知识库会如实回「凭据缺失」；"
            Write-Host "      在壳里对那台 NAS 登一次，或给 KYLAB_TOKEN。"
        }
    }
    else {
        Write-Host "边车：没找到壳的 config.json（或里面没有 server）—— 这一档不带 NAS 地址与钥匙（知识库与备份上传按各页自己的降级显示）"
    }

    $extra = @()
    if ($server) { $extra += @('--server', $server) }
    if ($token) { $extra += @('--token', $token) }

    # **--all-extras 不是可选的**：裸 `uv sync` 会把 extras（duckdb / 解析器 / office 这些）
    # 卸掉，而边车启动时就 import duckdb（backend/Dockerfile 与 CI 用的都是 --all-extras，
    # 同一条理由）。`uv run` 自己会同步环境，这里把 extras 一并带上。
    uv run --all-extras python -m app.sidecar --port $port --workspace $workspace --data-dir $dataDir @extra
}
finally {
    Pop-Location
}
