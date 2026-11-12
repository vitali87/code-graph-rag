<#
.SYNOPSIS
    code-graph-rag MCP 服务管理器（Windows）

.DESCRIPTION
    一条命令完成：启动 / 停止 / 查看 MCP 服务，自动写入项目级 .mcp.json，
    可选同时启动 cgr watch 让索引跟随代码变化。

    每个仓库一个实例、一个端口，互不干扰；在哪个仓库里开 Claude 会话，
    就自动用哪个仓库的服务。

.PARAMETER Action
    install  启动服务 + 写入 .mcp.json（首次用这个）
    start    只启动服务
    stop     停止服务
    restart  重启
    status   查看状态
    list     列出所有已登记的服务
    remove   停止并从登记表移除

.PARAMETER RepoPath
    要解析的代码仓库路径。不传则用当前目录。

.PARAMETER Port
    HTTP 端口。不传则按仓库路径自动分配（8700-8799）。

.PARAMETER Watch
    同时启动 cgr watch，代码一改自动更新图谱。

.PARAMETER ReadOnly
    只读模式：不暴露写文件、改名、建索引等工具。

.EXAMPLE
    .\setup_mcp.ps1 -Action install -RepoPath F:\qdmes\mes-service -Watch

.EXAMPLE
    .\setup_mcp.ps1 -Action status

.EXAMPLE
    .\setup_mcp.ps1 -Action stop -RepoPath F:\qdmes\mes-service
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [ValidateSet('install', 'start', 'stop', 'restart', 'status', 'list', 'remove')]
    [string]$Action = 'status',

    [string]$RepoPath = '',

    [int]$Port = 0,

    [switch]$Watch,

    [switch]$ReadOnly,

    [string]$Name = 'code-graph-rag'
)

$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot
$CgrExe = Join-Path $Root '.venv\Scripts\cgr.exe'
$PythonExe = Join-Path $Root '.venv\Scripts\python.exe'
$StateDir = Join-Path $Root 'cgr-data\mcp'
$RegistryPath = Join-Path $StateDir 'servers.json'
$LogDir = Join-Path $StateDir 'logs'

foreach ($d in @($StateDir, $LogDir)) {
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
}

if (-not (Test-Path -LiteralPath $CgrExe)) {
    throw "找不到 cgr：$CgrExe`n请先运行 .ootstrap.ps1 安装依赖。"
}

# ---------- 基础函数 ----------

function Get-Registry {
    if (-not (Test-Path -LiteralPath $RegistryPath)) { return @() }
    $raw = Get-Content -LiteralPath $RegistryPath -Raw -Encoding UTF8
    if (-not $raw.Trim()) { return @() }
    try { $data = $raw | ConvertFrom-Json } catch { return @() }
    $out = @()
    foreach ($item in @($data)) {
        if ($item -and $item.repo) { $out += $item }
    }
    return $out
}

function Save-Registry($entries) {
    $arr = @()
    foreach ($e in @($entries)) {
        if ($e -and $e.repo) { $arr += $e }
    }
    if ($arr.Count -eq 0) {
        Set-Content -LiteralPath $RegistryPath -Value '[]' -Encoding UTF8
        return
    }
    $json = ConvertTo-Json -InputObject $arr -Depth 6
    if ($arr.Count -eq 1 -and -not $json.TrimStart().StartsWith('[')) { $json = "[$json]" }
    Set-Content -LiteralPath $RegistryPath -Value $json -Encoding UTF8
}

function Resolve-Repo([string]$p) {
    if (-not $p) { $p = (Get-Location).Path }
    if (-not (Test-Path -LiteralPath $p)) { throw "仓库不存在：$p" }
    return (Resolve-Path -LiteralPath $p).Path.TrimEnd('\')
}

function Get-StablePort([string]$repo) {
    # 同一个仓库永远拿到同一个端口，重启后 .mcp.json 不用改
    $hash = 0
    foreach ($ch in $repo.ToLower().ToCharArray()) { $hash = ($hash * 31 + [int]$ch) % 100000007 }
    return 8700 + ($hash % 100)
}

function Test-PortBusy([int]$p) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $p, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($iar)
        return $true
    }
    catch { return $false }
    finally { $client.Close() }
}

function Get-FreePort([int]$start) {
    for ($p = $start; $p -lt $start + 100; $p++) {
        if (-not (Test-PortBusy $p)) { return $p }
    }
    throw "8700-8799 都被占用了，请用 -Port 手动指定。"
}

function Test-ProcessAlive($procId) {
    $id = 0
    if (-not [int]::TryParse("$procId", [ref]$id)) { return $false }
    if ($id -le 0) { return $false }
    return [bool](Get-Process -Id $id -ErrorAction SilentlyContinue)
}

function Wait-Http([int]$p, [int]$seconds = 90) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortBusy $p) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Write-McpJson([string]$repo, [int]$p) {
    $path = Join-Path $repo '.mcp.json'
    # Built line by line: a `-f` format string would treat the JSON's own braces
    # as format placeholders and throw.
    $lines = @(
        '{',
        '  "mcpServers": {',
        "    `"$Name`": {",
        '      "type": "http",',
        "      `"url`": `"http://127.0.0.1:$p/mcp/`"",
        '    }',
        '  }',
        '}'
    )
    $json = ($lines -join "`r`n") + "`r`n"
    if (Test-Path -LiteralPath $path) {
        Copy-Item -LiteralPath $path -Destination "$path.bak" -Force
    }
    Set-Content -LiteralPath $path -Value $json -Encoding UTF8
    return $path
}

# ---------- 动作 ----------

function Start-Server([string]$repo, [int]$p, [switch]$WithWatch) {
    $entries = @(Get-Registry)
    $existing = $entries | Where-Object { $_.repo -eq $repo }
    if ($existing -and (Test-ProcessAlive $existing.pid) -and (Test-PortBusy $existing.port)) {
        Write-Host "==> 已在运行：$repo (端口 $($existing.port), PID $($existing.pid))" -ForegroundColor Yellow
        $mcpPath = Write-McpJson $repo $existing.port
        Write-Host "    .mcp.json 已同步：$mcpPath" -ForegroundColor Green
        return
    }

    if (Test-PortBusy $p) {
        $p = Get-FreePort ($p + 1)
        Write-Host "    端口被占用，改用 $p" -ForegroundColor DarkYellow
    }

    $safe = ($repo -replace '[\\:\s]', '_')
    $outLog = Join-Path $LogDir "$safe.out.log"
    $errLog = Join-Path $LogDir "$safe.err.log"

    # 子进程继承这些环境变量
    $env:TARGET_REPO_PATH = $repo
    # 不硬编码 CGR_EMBEDDING_DEVICE：embedder._select_device 会自动探测
    # CUDA → MPS → CPU。硬写 'cuda' 会让 CPU 机器先打印一条 "CUDA 不可用"
    # 的警告再降级。仅在调用方显式设置时才透传。
    $env:HF_HUB_OFFLINE = '1'

    $args = @('mcp-server', '--transport', 'http', '--host', '127.0.0.1', '--port', "$p")
    if ($ReadOnly) { $args += '--read-only' }

    Write-Host "==> 启动 MCP 服务：$repo" -ForegroundColor Cyan
    Write-Host "    端口 $p，日志 $errLog" -ForegroundColor DarkGray

    $proc = Start-Process -FilePath $CgrExe -ArgumentList $args -PassThru `
        -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
        -WorkingDirectory $Root

    if (-not (Wait-Http $p 120)) {
        Write-Host "    启动失败，最后 20 行日志：" -ForegroundColor Red
        Get-Content -LiteralPath $errLog -Tail 20 -ErrorAction SilentlyContinue
        throw "MCP 服务未在 120 秒内就绪"
    }
    Write-Host "    就绪：http://127.0.0.1:$p/mcp/" -ForegroundColor Green

    $watchPid = 0
    if ($WithWatch) {
        $wOut = Join-Path $LogDir "$safe.watch.log"
        $w = Start-Process -FilePath $CgrExe `
            -ArgumentList @('watch', '--repo-path', $repo, '--debounce', '5') `
            -PassThru -WindowStyle Hidden -RedirectStandardOutput $wOut -RedirectStandardError "$wOut.err" `
            -WorkingDirectory $Root
        $watchPid = $w.Id
        Write-Host "    索引监听已启动（PID $($w.Id)），改代码自动同步" -ForegroundColor Green
    }

    $entries = @($entries | Where-Object { $_.repo -ne $repo })
    $entries += [pscustomobject]@{
        repo       = $repo
        port       = $p
        pid        = $proc.Id
        watchPid   = $watchPid
        readOnly   = [bool]$ReadOnly
        startedAt  = (Get-Date).ToString('s')
    }
    Save-Registry $entries

    $mcpPath = Write-McpJson $repo $p
    Write-Host "    已写入 $mcpPath" -ForegroundColor Green
}

function Stop-Server([string]$repo) {
    $entries = @(Get-Registry)
    $targets = if ($repo) { @($entries | Where-Object { $_.repo -eq $repo }) } else { @($entries) }
    if (-not $targets -or $targets.Count -eq 0) {
        Write-Host "没有登记在案的 MCP 服务。" -ForegroundColor Yellow
        return
    }
    foreach ($t in $targets) {
        foreach ($procId in @($t.pid, $t.watchPid)) {
            if ($procId -and (Test-ProcessAlive $procId)) {
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                Write-Host "    已停止 PID $procId ($($t.repo))" -ForegroundColor Green
            }
        }
    }
    if ($repo) {
        $entries = @($entries | Where-Object { $_.repo -ne $repo })
    }
    else {
        $entries = @()
    }
    Save-Registry $entries
}

function Show-Status {
    $entries = @(Get-Registry)
    if ($entries.Count -eq 0) {
        Write-Host "未登记任何 MCP 服务。用 -Action install -RepoPath <仓库> 启动一个。" -ForegroundColor Yellow
        return
    }
    $rows = foreach ($e in $entries) {
        $alive = Test-ProcessAlive $e.pid
        $port = Test-PortBusy $e.port
        [pscustomobject]@{
            仓库    = $e.repo
            端口    = $e.port
            PID     = $e.pid
            状态    = if ($alive -and $port) { '运行中' } elseif ($alive) { '进程在但端口不通' } else { '已停止' }
            监听    = if ($e.watchPid -and (Test-ProcessAlive $e.watchPid)) { '开' } else { '-' }
            只读    = if ($e.readOnly) { '是' } else { '否' }
            启动于  = $e.startedAt
        }
    }
    $rows | Format-Table -AutoSize
}

# ---------- 分发 ----------

switch ($Action) {
    'install' {
        $repo = Resolve-Repo $RepoPath
        $p = if ($Port -gt 0) { $Port } else { Get-StablePort $repo }
        Start-Server -repo $repo -p $p -WithWatch:$Watch
        Write-Host ''
        Write-Host "下一步：在 $repo 目录下启动 Claude Code，首次会提示是否信任 .mcp.json，选允许。" -ForegroundColor Cyan
    }
    'start' {
        $repo = Resolve-Repo $RepoPath
        $p = if ($Port -gt 0) { $Port } else { Get-StablePort $repo }
        Start-Server -repo $repo -p $p -WithWatch:$Watch
    }
    'stop' {
        Stop-Server $RepoPath
    }
    'restart' {
        Stop-Server $RepoPath
        Start-Sleep -Seconds 2
        $repo = Resolve-Repo $RepoPath
        $p = if ($Port -gt 0) { $Port } else { Get-StablePort $repo }
        Start-Server -repo $repo -p $p -WithWatch:$Watch
    }
    'remove' {
        Stop-Server $RepoPath
        if ($RepoPath) {
            $mcp = Join-Path (Resolve-Repo $RepoPath) '.mcp.json'
            if (Test-Path -LiteralPath $mcp) {
                Remove-Item -LiteralPath $mcp -Force
                Write-Host "    已删除 $mcp" -ForegroundColor Green
            }
        }
    }
    'status' { Show-Status }
    'list' { Show-Status }
}


