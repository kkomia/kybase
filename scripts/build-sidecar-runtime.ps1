<#
.SYNOPSIS
    构建"客户端运行时"：一份干净的 venv + 边车依赖 + 我们自己的后端代码 + 启动入口。

.NOTES
    ⚠️ **本文件必须存成 UTF-8 with BOM**（与 `scripts/test-changed.ps1` 同一纪律）：
    PowerShell 5.1 对**无 BOM** 的脚本会按系统 ANSI（GBK）解码 —— 下面的中文注释与字符串
    直接变成乱码、甚至解析失败（2026-09-30 实测：某个编辑器保存时把 BOM 吃掉，脚本就报
    `ParserError`）。同一个坑还有第二张脸：**读**别处的无 BOM UTF-8 文件（`requirements-sidecar.txt`）
    时，默认解码会把中文注释吃掉、**连带把后面的换行也吃掉** —— 清单 103 行只读出 54 行、
    33 个 pin 只认出 13 个，于是新加的依赖整段被吞（脚本"成功"了，运行时却缺包）。
    所以本脚本里读清单那一处**显式**写了 `-Encoding UTF8`（见"离线装配"那段）。

.DESCRIPTION
    为什么要有这个脚本（P4-3）：客户端（壳/边车）不该拖着 400 MB 的服务器依赖走。
    边车那条链真正需要的第三方只有四个：fastapi / uvicorn / httpx / pydantic
    （清单见 backend\requirements-sidecar.txt）。这个脚本把它们装进**一个干净目录**，
    再把 backend\app（约 3.2 MB）整份拷进去，最后**称重**（总大小与文件数）。

    三条红线（写在代码里，不靠记）：
    1. 绝不打包 dev venv（backend\.venv，400 MB）—— 目标目录落在它里面直接拒绝；
    2. 绝不打包 LibreOffice（约 700 MB）—— 本机若装了，只**检测并打印**，按需使用；
    3. 不裁剪 backend\app 的源码（整份拷；少拷一个文件就是一个只有上线才发现的缺件）。

.PARAMETER OutDir
    产物目录（默认 build\sidecar-runtime）。**会被清空重建**。不要指向仓库里已存在的目录。
    ⚠️ 落在 `build/` 而不是 `dist/`（2026-09-29 搬家）：`build/` 与 `dist/` 都在 .gitignore 里，
    但 `dist/` 这个名字在前后端工具链里到处都是（`frontend/dist/` 是前端产物），
    仓库根再放一份"边车运行时"容易看错；`build/` 只放我们自己造的构建物，一眼能对上。

.PARAMETER PythonExe
    用来建 venv 的解释器（默认用 `python`）。必须带 pip（`python -m venv` 会用 ensurepip 装）。

.PARAMETER EmbeddedPython
    可选的嵌入式 CPython 目录（含 python.exe）。**只留接口**：给了就校验并打印布局，
    不给就用 venv 里的解释器。本机没有就别去下载几百 MB 的东西。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build-sidecar-runtime.ps1
#>
# 注意：本文件必须存成 **UTF-8 带 BOM**。Windows PowerShell 5.1 读无 BOM 的 UTF-8 会按 GBK
# 解码，中文字符串当场变成乱码并语法崩溃（scripts\dev-backend.ps1 第 3 行记过同一条教训）。

[CmdletBinding()]
param(
    [string]$OutDir = 'build\sidecar-runtime',
    [string]$PythonExe = '',
    [string]$EmbeddedPython = '',
    [string]$IndexUrl = '',
    [switch]$Offline,
    [switch]$SkipInstall,
    [switch]$KeepPip
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "== $Text" -ForegroundColor Cyan
}

function Get-DirSize {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{ Bytes = 0; Files = 0 }
    }
    $files = Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue
    $sum = ($files | Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { $sum = 0 }
    return [pscustomobject]@{ Bytes = [int64]$sum; Files = $files.Count }
}

function Format-MB([int64]$Bytes) {
    return ('{0:N1} MB' -f ($Bytes / 1MB))
}

function Copy-DistByRecord {
    <#
        按一个发行包的 RECORD **逐文件**拷进目标 site-packages。
        为什么不用"按目录名拷"：离线装配踩过五种形态 —— 目录 / 单文件 .py（typing_extensions.py）/
        site-packages 根下的 .pyd（_cffi_backend）/ 伴生目录（*.libs）/ .dist-info 元数据。
        RECORD 就是"这个包到底有哪些文件"的权威答案，按它拷一次就都对。
    #>
    param([string]$SourceSitePackages, [string]$TargetSitePackages, [string]$Pin)
    $parts = $Pin -split '==', 2
    if ($parts.Count -lt 2) { return 0 }
    $name = $parts[0].Trim()
    $version = $parts[1].Trim()
    $normalized = ($name.ToLower() -replace '[^a-z0-9]+', '_')
    $distInfo = Join-Path $SourceSitePackages "$normalized-$version.dist-info"
    if (-not (Test-Path -LiteralPath $distInfo)) {
        $candidate = Get-ChildItem -LiteralPath $SourceSitePackages -Directory -Filter "$normalized-*.dist-info" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $candidate) { throw "源 venv 里找不到 $name==$version 的 dist-info（先把开发依赖装好）" }
        $distInfo = $candidate.FullName
    }
    $record = Join-Path $distInfo 'RECORD'
    if (-not (Test-Path -LiteralPath $record)) { throw "$distInfo 里没有 RECORD" }
    $copied = 0
    foreach ($line in Get-Content -LiteralPath $record) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $relative = ($line -split ',')[0].Trim().Trim('"')
        if ([string]::IsNullOrWhiteSpace($relative)) { continue }
        if ($relative -like '*.pyc' -or $relative -like '*__pycache__*') { continue }
        if ($relative -like '*.chm') { continue }
        $source = Join-Path $SourceSitePackages ($relative -replace '/', '\')
        if (-not (Test-Path -LiteralPath $source)) { continue }
        $target = Join-Path $TargetSitePackages ($relative -replace '/', '\')
        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetDir)) { New-Item -ItemType Directory -Path $targetDir -Force | Out-Null }
        Copy-Item -LiteralPath $source -Destination $target -Force
        $copied += 1
    }
    return $copied
}

# ------------------------------------------------------------------ 定位仓库
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $scriptDir '..')).Path
$backendDir = Join-Path $repoRoot 'backend'
$requirements = Join-Path $backendDir 'requirements-sidecar.txt'

if (-not (Test-Path -LiteralPath $requirements)) {
    throw "找不到依赖清单：$requirements（本脚本只按它装，别用 dev 的全量 requirements）"
}

Write-Host "仓库根：$repoRoot"

# ------------------------------------------------------------------ 临时目录：钉到仓库内
#
# **必须在任何 pip 调用之前**：本机沙箱会**拒写 `%TEMP%`** ✗，而 pip 的构建/解包目录
# 只看 `TEMP`/`TMP` —— 写不进去时它会**退化成用当前目录**，于是每跑一次本脚本，
# 仓库根就多几个 `pip-build-env-*` / `pip-unpack-*` / `pip-install-*`
# （2026-09-29 实测：这类目录积累过 114 个 / 124.2 MB，`git status` 全是未跟踪垃圾）。
# 指到仓库内的 `.tmp/` 之后，就算环境再变，临时文件也落在被忽略的目录里。
$tmpDir = Join-Path $repoRoot '.tmp'
if (-not (Test-Path -LiteralPath $tmpDir)) {
    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
}
$env:TEMP = $tmpDir
$env:TMP = $tmpDir
$env:TMPDIR = $tmpDir
Write-Host "临时目录：$tmpDir（pip 的构建与解包目录都落这儿）"

# ------------------------------------------------------------------ 红线检查
$resolvedOut = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutDir))
$devVenv = Join-Path $backendDir '.venv'
$appSource = Join-Path $backendDir 'app'
$repoBuild = Join-Path $repoRoot 'build'

foreach ($forbidden in @($devVenv, $appSource, $backendDir)) {
    if ($resolvedOut.StartsWith($forbidden, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "产物目录不能在 $forbidden 里面（红线 1：绝不打包 dev venv / 源码目录）"
    }
}
if (-not $resolvedOut.StartsWith($repoBuild, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Host "注意：产物目录不在 build\ 下（$resolvedOut）—— 只要不是 dev venv 就不拦。" -ForegroundColor Yellow
}

# ------------------------------------------------------------------ 解释器
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $candidates = @()
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    # 本机优先用带 pip 的那个（dev venv 是 uv 建的，**没有 pip** ✗）
    $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        $hasPip = & $candidate -c "import importlib.util as u; print(1 if u.find_spec('pip') or u.find_spec('ensurepip') else 0)" 2>$null
        if ("$hasPip".Trim() -eq '1') { $PythonExe = $candidate; break }
    }
}
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    throw '找不到带 pip 的解释器：请用 -PythonExe 指定一个（dev venv 是 uv 建的，没有 pip）'
}
Write-Host "用这个解释器建 venv：$PythonExe"
Write-Host ("版本：" + (& $PythonExe -c "import sys; print(sys.version.split()[0])"))

# ------------------------------------------------------------------ 干净目录 + venv
Write-Step "重建产物目录（干净起点）"
# 先确认没有进程正开着这个目录：实测踩过两次 —— 残留的 python/pip 会让孩子进程
# 一边写一边被删（venv 半创建、pip 报 -1、目录删不掉、体量读数里混进 pip 临时目录）。
$busy = @()
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match '^python' -and $_.CommandLine -and $_.CommandLine -like "*$resolvedOut*"
} | ForEach-Object { $busy += "$($_.Name) PID $($_.ProcessId)" }
if ($busy.Count -gt 0) {
    throw ("有进程正开着产物目录（{0}）：先停掉再重跑（否则目录删不干净、体量读数会被带偏）" -f ($busy -join '；'))
}
if (Test-Path -LiteralPath $resolvedOut) {
    Remove-Item -LiteralPath $resolvedOut -Recurse -Force
}
New-Item -ItemType Directory -Path $resolvedOut -Force | Out-Null

Write-Step "建 venv（python -m venv）"
& $PythonExe -m venv $resolvedOut
if ($LASTEXITCODE -ne 0) { throw "建 venv 失败（退出码 $LASTEXITCODE）" }
$venvPython = Join-Path $resolvedOut 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) { throw "venv 里没有 python.exe：$venvPython" }

# venv 有可能"半创建"（实测过一次：目录里只有 Scripts、没有 Lib，pip 也没装上）——
# 那次是目录被别的进程占着导致的。这里显式自检并兜底补一次 pip，免得后面报一句看不懂的错。
$pipOk = & $venvPython -c "import importlib.util as u; print(1 if u.find_spec('pip') else 0)" 2>$null
if ("$pipOk".Trim() -ne '1') {
    Write-Host 'venv 里没有 pip，用 ensurepip 补一次…' -ForegroundColor Yellow
    & $venvPython -m ensurepip --upgrade | Out-Host
    $pipOk = & $venvPython -c "import importlib.util as u; print(1 if u.find_spec('pip') else 0)" 2>$null
    if ("$pipOk".Trim() -ne '1') {
        throw "venv 建得不完整（ensurepip 之后仍然没有 pip）：删掉 $resolvedOut 再重跑"
    }
}

# ------------------------------------------------------------------ 装依赖
if ($Offline) {
    Write-Step "离线装配（从开发 venv 按每个包的 RECORD 逐文件拷）"
    $sourceSite = Join-Path $devVenv 'Lib\site-packages'
    if (-not (Test-Path -LiteralPath $sourceSite)) { throw "源 venv 不在：$sourceSite" }
    $targetSite = Join-Path $resolvedOut 'Lib\site-packages'
    New-Item -ItemType Directory -Path $targetSite -Force | Out-Null
    # ⚠️ **必须显式 `-Encoding UTF8`**（2026-09-30 实测的真 bug）：清单是 UTF-8 **无 BOM**
    # 且带中文注释，而 PowerShell 5.1 的 `Get-Content` 默认按**系统 ANSI（GBK）**解码 ——
    # 错解的中文会把**后面的换行也吃掉**：103 行的清单只读出 54 行、33 个 pin 只认出 13 个，
    # 于是新加的 `openpyxl / lxml / python-docx / python-pptx / et-xmlfile` 整段被吞 ✗
    # （表现：脚本"成功"了，但运行时里没有这几个包 → 桌面导出"一调就失败"）✓。
    $pins = Get-Content -LiteralPath $requirements -Encoding UTF8 |
        Where-Object { $_ -match '^\s*[A-Za-z0-9][A-Za-z0-9_.\-]*\s*==' }
    Write-Host ("要装的包 {0} 个（清单：{1}）" -f $pins.Count, (Split-Path -Leaf $requirements))
    $totalCopied = 0
    foreach ($pin in $pins) {
        $clean = ($pin -split '#')[0].Trim()
        $copied = Copy-DistByRecord -SourceSitePackages $sourceSite -TargetSitePackages $targetSite -Pin $clean
        if ($copied -eq 0) { Write-Host ("  !! {0}：一个文件都没拷到" -f $clean) -ForegroundColor Yellow }
        $totalCopied += $copied
        Write-Host ("  {0}（{1} 个文件）" -f $clean, $copied)
    }
    Write-Host ("离线装配合计拷贝 {0} 个文件" -f $totalCopied)
} elseif ($SkipInstall) {
    Write-Step "跳过安装（-SkipInstall）"
} else {
    Write-Step "装边车依赖（清单：backend\requirements-sidecar.txt）"
    # 走哪个源：默认用 pip 自己的配置（本机指向清华镜像）；实测这份清单在镜像上会**卡住**
    # （十几分钟不动），换官方源正常 —— 所以失败/超时后自动用官方源重试一次，并把这件事打出来。
    $indexArgs = @()
    if (-not [string]::IsNullOrWhiteSpace($IndexUrl)) { $indexArgs = @('--index-url', $IndexUrl) }
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
        '--progress-bar', 'off') + $indexArgs + @('-r', $requirements)
    & $venvPython @pipArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host '第一次装失败（本机 pip 配置的源可能是镜像）：改用官方 PyPI 重试一次' -ForegroundColor Yellow
        & $venvPython -m pip install --disable-pip-version-check --no-input --progress-bar off `
            --index-url https://pypi.org/simple -r $requirements
        if ($LASTEXITCODE -ne 0) { throw "装依赖失败（官方源也不成，退出码 $LASTEXITCODE）" }
    }
    Write-Host '已装（pip freeze 的实测版本）：'
    & $venvPython -m pip freeze
}

# ------------------------------------------------------------------ 清掉"装完就不该留"的东西
# 这三类都是**实测**踩出来的（不清理会把体量读数带偏，甚至涨 55 MB）：
# 1. pip 的临时目录：安装被中断/失败时会留在 venv 根下（pip-install-* / pip-unpack-*）
#    —— 实测一次留下 36.6 + 18.3 = 54.9 MB；
# 2. pip / setuptools / wheel 本体（site-packages 里约 15 MB）：**运行边车用不到它们**，
#    更新这份运行时的方式是重跑本脚本（不是在里面 pip install）；
# 3. __pycache__ / *.pyc：可重生成（保留会让启动略快，但体量不透明；先清掉再按需生成）。
Write-Step "清理可删项（pip 临时目录 / pip 本体 / 缓存）"
$removable = @()
foreach ($pattern in @('pip-install-*', 'pip-unpack-*', 'pip-build-env-*', 'pip-build-tracker-*',
        'pip-ephem-wheel-cache-*', '*.tmp')) {
    Get-ChildItem -LiteralPath $resolvedOut -Directory -Filter $pattern -ErrorAction SilentlyContinue |
        ForEach-Object { $removable += $_.FullName }
}
foreach ($name in @('pip', 'pip-*', 'setuptools', 'setuptools-*', 'wheel', 'wheel-*', 'pkg_resources', '_distutils_hack')) {
    Get-ChildItem -LiteralPath (Join-Path $resolvedOut 'Lib\site-packages') -Filter $name -ErrorAction SilentlyContinue |
        ForEach-Object { $removable += $_.FullName }
}
Get-ChildItem -LiteralPath (Join-Path $resolvedOut 'Scripts') -Filter 'pip*' -ErrorAction SilentlyContinue |
    ForEach-Object { $removable += $_.FullName }
Get-ChildItem -LiteralPath $resolvedOut -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    ForEach-Object { $removable += $_.FullName }

if ($KeepPip) {
    Write-Host '-KeepPip：保留 pip（调试用），只清临时目录与缓存'
    $removable = $removable | Where-Object { $_ -notmatch 'site-packages\\(pip|setuptools|wheel|pkg_resources|_distutils_hack)' }
}
$freed = 0
foreach ($item in ($removable | Sort-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $item)) { continue }
    $size = (Get-ChildItem -LiteralPath $item -Recurse -File -Force -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
    if ($null -eq $size) { $size = 0 }
    $freed += $size
    Remove-Item -LiteralPath $item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host ("  删掉 {0}（{1}）" -f (Split-Path -Leaf $item), (Format-MB ([int64]$size)))
}
Write-Host ("清理合计释放：{0}" -f (Format-MB ([int64]$freed)))

# ------------------------------------------------------------------ 拷代码
Write-Step "拷我们自己的代码（backend\app 整份 ✗ 不裁剪）"
$targetApp = Join-Path $resolvedOut 'app'
Copy-Item -LiteralPath $appSource -Destination $targetApp -Recurse -Force
# 测试与 __pycache__ 不拷（前者是开发物、后者是编译缓存，都能重生成）
Get-ChildItem -LiteralPath $targetApp -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# ------------------------------------------------------------------ 启动入口
Write-Step "写启动入口"
$cmdLauncher = @'
@echo off
rem 客户端运行时入口：循环 + 工具 + 沙箱都在本机，KB 与模型打远端 ✓
rem 用法：run-sidecar.cmd --port 8767 --server http://127.0.0.1:8000/api/v1 --token <用户令牌>
setlocal
set HERE=%~dp0
"%HERE%Scripts\python.exe" -m app.sidecar %*
endlocal
'@
Set-Content -LiteralPath (Join-Path $resolvedOut 'run-sidecar.cmd') -Value $cmdLauncher -Encoding ASCII

$psLauncher = @'
# 客户端运行时入口（PowerShell 版，参数同 .cmd）
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $here 'Scripts\python.exe') -m app.sidecar @args
'@
Set-Content -LiteralPath (Join-Path $resolvedOut 'run-sidecar.ps1') -Value $psLauncher -Encoding UTF8

# 清单也放一份进去：拿着这个目录的人能看清"这份运行时是按什么装的"（可复现 ✓）
Copy-Item -LiteralPath $requirements -Destination (Join-Path $resolvedOut 'requirements-sidecar.txt') -Force

# ------------------------------------------------------------------ 嵌入式 CPython（只留接口）
Write-Step "嵌入式 CPython（接口）"
if ([string]::IsNullOrWhiteSpace($EmbeddedPython)) {
    Write-Host '未指定 -EmbeddedPython：本次用 venv 自带的解释器（约 15-20 MB，已算在总大小里）。'
    Write-Host '要换成嵌入式发行版：把解压好的目录（含 python.exe）用 -EmbeddedPython 传进来；'
    Write-Host '本机没有就别去下载几百 MB 的东西 —— 目标体量 ≈40-50 MB。'
} elseif (-not (Test-Path -LiteralPath (Join-Path $EmbeddedPython 'python.exe'))) {
    Write-Host "给的目录里没有 python.exe，跳过：$EmbeddedPython" -ForegroundColor Yellow
} else {
    $size = Get-DirSize $EmbeddedPython
    Write-Host ("嵌入式解释器：{0}（{1}，{2} 个文件）" -f $EmbeddedPython, (Format-MB $size.Bytes), $size.Files)
    Write-Host '布局约定：解释器放在 runtime\python\，启动入口改指向它即可（本次不自动搬 ✗ 避免半成品）。'
}

# ------------------------------------------------------------------ LibreOffice（红线 2）
Write-Step "LibreOffice 检测（红线 2：绝不打包）"
$soffice = Get-Command soffice -ErrorAction SilentlyContinue
if ($soffice) {
    Write-Host "本机装了 LibreOffice（$($soffice.Source)）：**不打包** ✓，运行时按需调用它。"
} else {
    Write-Host '本机没有 LibreOffice：Office 文档转换改用我们自己的 python-docx/pptx/openpyxl（服务端）✓。'
}

# ------------------------------------------------------------------ 称重
Write-Step "称重（**文件字节和**，不是"占用空间"）"
$total = Get-DirSize $resolvedOut
$venvSize = Get-DirSize (Join-Path $resolvedOut 'Lib')
$appSize = Get-DirSize $targetApp
$scriptsSize = Get-DirSize (Join-Path $resolvedOut 'Scripts')
$interpretersSize = Get-DirSize (Join-Path $resolvedOut 'Scripts')

Write-Host ("总大小：{0}" -f (Format-MB $total.Bytes))
Write-Host ("文件数：{0}" -f $total.Files)
Write-Host ("其中：Lib\site-packages {0}；app\ {1}（{2} 个文件）；Scripts\（解释器本体）{3}" -f `
        (Format-MB $venvSize.Bytes), (Format-MB $appSize.Bytes), $appSize.Files, (Format-MB $scriptsSize.Bytes))

Write-Step "逐项明细（site-packages 每个包一块）"
$sitePackages = Join-Path $resolvedOut 'Lib\site-packages'
$rows = @()
Get-ChildItem -LiteralPath $sitePackages -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $size = Get-DirSize $_.FullName
    $rows += [pscustomobject]@{ 项 = $_.Name; MB = [math]::Round($size.Bytes / 1MB, 1); 文件 = $size.Files }
}
# venv 根下的解释器本体 / 其它散件也算进来，别让"总大小"里有一块没主
foreach ($extra in @('Scripts', 'Lib')) {
    $size = Get-DirSize (Join-Path $resolvedOut $extra)
    $rows += [pscustomobject]@{ 项 = "$extra\（解释器与标准库外壳）"; MB = [math]::Round($size.Bytes / 1MB, 1); 文件 = $size.Files }
}
$rows += [pscustomobject]@{ 项 = 'app\（我们自己的代码）'; MB = [math]::Round($appSize.Bytes / 1MB, 1); 文件 = $appSize.Files }
$rows += [pscustomobject]@{ 项 = '根下的入口与清单'; MB = [math]::Round(((Get-DirSize $resolvedOut).Bytes - $venvSize.Bytes - $appSize.Bytes) / 1MB, 1); 文件 = (Get-ChildItem -LiteralPath $resolvedOut -File | Measure-Object).Count }
$rows | Sort-Object MB -Descending | Format-Table -AutoSize | Out-String -Width 120 | Write-Host

Write-Host ("产物：{0}" -f $resolvedOut)
if ($total.Bytes -gt 100MB) {
    Write-Host '超过 100 MB：每一项为什么在，要写进 docs\规范\客户端运行时-打包规格-v0.1.md' -ForegroundColor Yellow
}
