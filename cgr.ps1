#Requires -Version 5.1

<#
.SYNOPSIS
    code-graph-rag 日常运维脚本（Windows / PowerShell）

.DESCRIPTION
    在 setup_mcp.ps1 之上封装最常用的几条命令，不需要记 cgr 的完整参数。

    设计取舍：MCP 常驻 + 手动同步，不启用 cgr watch。
    watch 会持续占用同步锁，导致 MCP 查询期间被阻塞；
    改用「改完代码手动 sync」后，查询永远是即时的。

.PARAMETER Action
    start    启动 MCP 服务（默认）
    stop     停止 MCP 服务
    restart  重启 MCP 服务
    status   查看服务状态 + 图谱统计
    sync     增量同步代码变更（等价于 update_repository）
    rebuild  全量重建索引（换模型 / 图谱异常时用）
    doctor   环境自检（GPU / 模型 / 数据库）
    logs     查看 MCP 服务日志
    repos    列出所有已登记的仓库别名

.PARAMETER RepoPath
    仓库别名或绝对路径。别名定义在 repos.json。
    省略时使用 repos.json 里的 default。

.PARAMETER Tail
    配合 logs 使用：实时跟踪日志输出

.EXAMPLE
    .\cgr.ps1
    启动默认仓库的 MCP 服务

.EXAMPLE
    .\cgr.ps1 start mes
    启动别名为 mes 的仓库

.EXAMPLE
    .\cgr.ps1 sync pms
    同步别名为 pms 的仓库

.EXAMPLE
    .\cgr.ps1 repos
    列出所有仓库别名

.EXAMPLE
    .\cgr.ps1 logs mes -Tail
    实时跟踪 mes 仓库的日志

.NOTES
    新增仓库：编辑 repos.json，在 repos 下加一条
        "pms": { "path": "F:\\qdmes\\pms\\pms_backend", "desc": "PMS 服务" }

    若提示「禁止运行脚本」，用：
        powershell -ExecutionPolicy Bypass -File .\cgr.ps1
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'sync', 'rebuild', 'doctor', 'logs', 'repos')]
    [string]$Action = 'start',

    [Parameter(Position = 1)]
    [string]$RepoPath = '',

    [switch]$Tail
)

$ErrorActionPreference = 'Stop'

# ---------- 路径 ----------
$Root       = $PSScriptRoot
$CgrExe     = Join-Path $Root '.venv\Scripts\cgr.exe'
$Setup      = Join-Path $Root 'setup_mcp.ps1'
$LogDir     = Join-Path $Root 'cgr-data\mcp\logs'
$ReposFile  = Join-Path $Root 'repos.json'

# ---------- 输出 ----------
function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "    $msg" -ForegroundColor Yellow }
function Write-Err2($msg) { Write-Host "    $msg" -ForegroundColor Red }

# 控制台 UTF-8，避免 cgr 中文输出乱码
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
} catch { }

if (-not (Test-Path -LiteralPath $CgrExe)) {
    Write-Err2 "找不到 cgr：$CgrExe"
    Write-Err2 "请先运行 .ootstrap.ps1 安装依赖。"
    exit 1
}

# ---------- 仓库别名解析 ----------

function Get-RepoConfig {
    if (-not (Test-Path -LiteralPath $ReposFile)) {
        throw "找不到别名配置文件：$ReposFile"
    }
    $raw = Get-Content -LiteralPath $ReposFile -Raw -Encoding UTF8
    try { return $raw | ConvertFrom-Json }
    catch { throw "repos.json 格式错误：$_" }
}

<#
    把 -RepoPath 的入参解析成真实路径。
    支持三种写法：
        mes                        -> 别名
        F:\qdmes\mes-service       -> 绝对路径（校验存在）
        （空）                     -> repos.json 里的 default
#>
function Resolve-RepoPath([string]$selector) {
    $cfg = Get-RepoConfig

    # 1. 空值 -> 默认仓库
    if (-not $selector) {
        $selector = $cfg.default
        if (-not $selector) {
            throw "未指定仓库，且 repos.json 没有配置 default。"
        }
    }

    # 2. 别名匹配
    $repos = $cfg.repos
    if ($repos.PSObject.Properties.Name -contains $selector) {
        $path = $repos.$selector.path
        if (-not (Test-Path -LiteralPath $path)) {
            throw "别名 '$selector' 指向的路径不存在：$path`n请检查 repos.json。"
        }
        return @{ Alias = $selector; Path = (Resolve-Path -LiteralPath $path).Path.TrimEnd('\'); Desc = $repos.$selector.desc }
    }

    # 3. 当作绝对路径
    if (Test-Path -LiteralPath $selector) {
        $full = (Resolve-Path -LiteralPath $selector).Path.TrimEnd('\')
        # 反查是否登记过别名
        $alias = $null
        foreach ($p in $repos.PSObject.Properties) {
            $cfgPath = $p.Value.path
            if (-not $cfgPath) { continue }
            if (-not (Test-Path -LiteralPath $cfgPath)) { continue }
            $resolved = (Resolve-Path -LiteralPath $cfgPath).Path.TrimEnd('\')
            if ($resolved -eq $full) { $alias = $p.Name; break }
        }
        return @{ Alias = $alias; Path = $full; Desc = $null }
    }

    # 4. 都没匹配上
    $known = ($repos.PSObject.Properties.Name) -join ', '
    throw "无法识别的仓库：'$selector'`n已登记别名：$known`n或在 repos.json 中添加。"
}

# 解析仓库（repos 动作不需要）
$Repo = $null
if ($Action -ne 'repos') {
    try {
        $Repo = Resolve-RepoPath $RepoPath
    }
    catch {
        Write-Err2 $_.Exception.Message
        exit 1
    }
    $RepoPath = $Repo.Path
    $SafeName = ($RepoPath -replace '[\\:\s]', '_')
    $McpLog   = Join-Path $LogDir "$SafeName.err.log"
}

# ---------- 动作 ----------

function Format-RepoLabel {
    if ($Repo -and $Repo.Alias) { return "$($Repo.Alias)  ($RepoPath)" }
    return $RepoPath
}

function Invoke-Start {
    Write-Step "启动 MCP 服务：$(Format-RepoLabel)"
    & $Setup -Action start -RepoPath $RepoPath
    Write-Host ''
    Write-Ok "提示：本脚本不启用 watch。改完代码后执行  .\cgr.ps1 sync $($Repo.Alias)  增量同步。"
}

function Invoke-Stop {
    Write-Step "停止 MCP 服务：$(Format-RepoLabel)"
    & $Setup -Action stop -RepoPath $RepoPath
}

function Invoke-Restart {
    Write-Step "重启 MCP 服务：$(Format-RepoLabel)"
    & $Setup -Action restart -RepoPath $RepoPath
}

function Invoke-Repos {
    $cfg = Get-RepoConfig
    Write-Step "已登记的代码仓库"
    $rows = foreach ($p in $cfg.repos.PSObject.Properties) {
        $exists = Test-Path -LiteralPath $p.Value.path
        [pscustomobject]@{
            别名   = $p.Name
            默认   = if ($p.Name -eq $cfg.default) { '★' } else { '' }
            路径   = $p.Value.path
            状态   = if ($exists) { '存在' } else { '路径不存在' }
            说明   = $p.Value.desc
        }
    }
    $rows | Format-Table -AutoSize
    Write-Host "    配置文件：$ReposFile" -ForegroundColor DarkGray
    Write-Host "    用法：.\cgr.ps1 start <别名>" -ForegroundColor DarkGray
}

function Invoke-Status {
    Write-Step "服务状态"
    & $Setup -Action status
    Write-Host ''

    Write-Step "图谱数据（全局库）"
    # 所有仓库共用同一对 sqlite 文件，图谱内按 project 隔离
    foreach ($db in @(
            @{ Path = 'cgr-data\db\mes-graph.sqlite';   Name = '图谱' },
            @{ Path = 'cgr-data\db\mes-vectors.sqlite'; Name = '向量' }
        )) {
        $p = Join-Path $Root $db.Path
        if (Test-Path -LiteralPath $p) {
            $item = Get-Item -LiteralPath $p
            Write-Ok ("{0}  {1,8:N1} MB   最后更新 {2:yyyy-MM-dd HH:mm}" -f `
                    $db.Name, ($item.Length / 1MB), $item.LastWriteTime)
        }
        else {
            Write-Err2 "$($db.Name) 数据库不存在：$($db.Path)"
        }
    }

    Write-Host ''
    Write-Host "    注：本项目用 SQLite 后端，不依赖 Memgraph / Qdrant / Docker。" -ForegroundColor DarkGray
}

function Invoke-Sync {
    Write-Step "增量同步：$(Format-RepoLabel)"
    Write-Host "    只解析变更文件，通常几秒到几十秒。" -ForegroundColor DarkGray

    Push-Location $Root
    try {
        # 注意：不能用 `2>&1 | ForEach-Object`。
        # $ErrorActionPreference='Stop' 时，管道里的 stderr 会被包装成
        # NativeCommandError 并直接抛出，cgr 打印一行进度信息就会中断同步。
        # 这里显式放开偏好，再按退出码判断成败。
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $CgrExe start --repo-path $RepoPath --update-graph 2>&1 |
                ForEach-Object { Write-Host "    $_" }
            $code = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = $prev }

        if ($code -ne 0) { throw "cgr 退出码 $code" }
        Write-Ok "同步完成"
    }
    catch {
        Write-Err2 "同步失败：$_"
        exit 1
    }
    finally { Pop-Location }
}

function Invoke-Rebuild {
    Write-Step "全量重建索引：$(Format-RepoLabel)"
    Write-Warn2 "会清空该仓库的现有图谱后重建，约 3~6 分钟（GPU）/ 15~30 分钟（CPU）。"
    $confirm = Read-Host "    确认继续？(y/N)"
    if ($confirm -notmatch '^[Yy]$') {
        Write-Host "    已取消。" -ForegroundColor Yellow
        return
    }

    # 先停服务，避免重建时锁冲突
    & $Setup -Action stop -RepoPath $RepoPath | Out-Null

    Push-Location $Root
    try {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $CgrExe start --repo-path $RepoPath --update-graph --clean -y 2>&1 |
                ForEach-Object { Write-Host "    $_" }
            $code = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = $prev }
        if ($code -ne 0) { throw "cgr 退出码 $code" }
        Write-Ok "重建完成"
    }
    catch {
        Write-Err2 "重建失败：$_"
    }
    finally { Pop-Location }

    Write-Step "重新启动服务"
    & $Setup -Action start -RepoPath $RepoPath
}

function Invoke-Doctor {
    Write-Step "环境自检"

    Write-Host "`n[Python]" -ForegroundColor Cyan
    $py = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $py) {
        & $py --version 2>&1 | ForEach-Object { Write-Host "    $_" }
    }
    else {
        Write-Err2 ".venv 不存在"
    }

    # ---- 硬件与推理设备 ----
    # 项目支持 CPU / CUDA / MPS 三种设备，自动选择见 embedder._select_device。
    # 这里只做展示，不下结论：真正生效的设备以 embedder 启动日志为准。
    Write-Host "`n[推理设备]" -ForegroundColor Cyan
    $hasNvidia = $false
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        $hasNvidia = $true
        $gpu = nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>&1
        $gpu | ForEach-Object { Write-Host "    NVIDIA: $_" }
    }
    else {
        Write-Host "    未检测到 NVIDIA 显卡（nvidia-smi 不存在）" -ForegroundColor DarkGray
    }

    if (Test-Path -LiteralPath $py) {
        $probe = @'
import sys
try:
    import torch
    print("TORCH", torch.__version__)
    print("CUDA_BUILD", torch.version.cuda or "cpu")
    print("CUDA_OK", torch.cuda.is_available())
    if torch.cuda.is_available():
        print("DEVICE", torch.cuda.get_device_name(0))
except Exception as e:
    print("TORCH_MISSING", e)
'@
        $tmp = Join-Path $env:TEMP "cgr_dev_$PID.py"
        Set-Content -LiteralPath $tmp -Value $probe -Encoding UTF8
        try { $out = & $py $tmp 2>&1 } finally { Remove-Item $tmp -ErrorAction SilentlyContinue }

        $torchVer = ($out | Select-String '^TORCH ' | ForEach-Object { $_.Line -replace '^TORCH ', '' })
        $cudaBuild = ($out | Select-String '^CUDA_BUILD ' | ForEach-Object { $_.Line -replace '^CUDA_BUILD ', '' })
        $cudaOk = ($out | Select-String '^CUDA_OK ' | ForEach-Object { $_.Line -replace '^CUDA_OK ', '' })
        $devName = ($out | Select-String '^DEVICE ' | ForEach-Object { $_.Line -replace '^DEVICE ', '' })

        if (-not $torchVer) {
            Write-Err2 "torch 未安装"
        }
        else {
            Write-Host "    torch $torchVer (build: $cudaBuild)"
            if ($cudaOk -eq 'True') {
                Write-Ok "CUDA 可用 → 推理将使用 GPU：$devName"
            }
            elseif ($hasNvidia) {
                Write-Warn2 "有 NVIDIA 显卡，但当前 torch 是 CPU 版"
                Write-Warn2 "需要 GPU 加速请运行：.\scripts\install_gpu_torch.ps1"
            }
            else {
                Write-Ok "使用 CPU 推理（功能完整，建图较慢）"
            }
        }
    }

    Write-Host "`n[模型]" -ForegroundColor Cyan
    # 模型目录从 .env 的 CGR_EMBEDDING_MODEL 读取，默认 BAAI/bge-m3
    $modelDir = $null
    $envFile = Join-Path $Root '.env'
    if (Test-Path -LiteralPath $envFile) {
        $line = Get-Content -LiteralPath $envFile |
            Where-Object { $_ -match '^\s*CGR_EMBEDDING_MODEL\s*=' } |
            Select-Object -First 1
        if ($line) { $modelDir = ($line -split '=', 2)[1].Trim().Trim('"', "'") }
    }
    if (-not $modelDir) {
        $modelDir = Join-Path $Root 'cgr-data\models\bge-m3'
    }

    if (Test-Path -LiteralPath $modelDir) {
        $size = (Get-ChildItem -LiteralPath $modelDir -Recurse -File |
            Measure-Object -Property Length -Sum).Sum / 1GB
        Write-Ok ("$(Split-Path -Leaf $modelDir) 存在（{0:N2} GB）" -f $size)
        if (-not (Test-Path -LiteralPath (Join-Path $modelDir 'config.json'))) {
            Write-Warn2 "缺少 config.json，模型可能不完整"
        }
    }
    else {
        Write-Err2 "模型目录不存在：$modelDir"
        Write-Warn2 "运行 .\scripts\fetch_embedding_model.ps1 下载"
    }

    Write-Host "`n[数据库]" -ForegroundColor Cyan
    foreach ($db in @('cgr-data\db\mes-graph.sqlite', 'cgr-data\db\mes-vectors.sqlite')) {
        $p = Join-Path $Root $db
        if (Test-Path -LiteralPath $p) {
            $mb = (Get-Item -LiteralPath $p).Length / 1MB
            Write-Ok ("{0}  {1:N1} MB" -f $db, $mb)
        }
        else {
            Write-Err2 "$db 不存在"
        }
    }

    Write-Host "`n[配置]" -ForegroundColor Cyan
    if (Test-Path -LiteralPath $envFile) {
        Get-Content -LiteralPath $envFile |
            Where-Object { $_ -match '^\s*CGR_EMBEDDING|^\s*SQLITE_VECTOR_DIM' } |
            ForEach-Object { Write-Host "    $_" }
    }
}

function Invoke-Logs {
    if (-not (Test-Path -LiteralPath $McpLog)) {
        Write-Err2 "日志不存在：$McpLog"
        Write-Warn2 "先启动服务：.\cgr.ps1 start $($Repo.Alias)"
        return
    }
    Write-Step "MCP 日志：$(Format-RepoLabel)"
    if ($Tail) {
        Get-Content -LiteralPath $McpLog -Tail 50 -Wait
    }
    else {
        Get-Content -LiteralPath $McpLog -Tail 50
        Write-Host "`n    加 -Tail 参数可实时跟踪。" -ForegroundColor DarkGray
    }
}

# ---------- 分发 ----------
switch ($Action) {
    'start'   { Invoke-Start }
    'stop'    { Invoke-Stop }
    'restart' { Invoke-Restart }
    'status'  { Invoke-Status }
    'sync'    { Invoke-Sync }
    'rebuild' { Invoke-Rebuild }
    'doctor'  { Invoke-Doctor }
    'logs'    { Invoke-Logs }
    'repos'   { Invoke-Repos }
}
