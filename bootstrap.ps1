#Requires -Version 5.1
# ============================================================================
#  bootstrap.ps1 -- 在全新机器上把本仓库拉起来
#
#  设计原则：
#    1. 不写死任何绝对路径。项目根 = 本脚本所在目录，其余全部相对推导。
#    2. 幂等。每一步都先检测再动作，重复执行不会破坏已有环境。
#    3. 每一步失败都给出可执行的下一步，而不是抛一个栈。
#
#  用法：
#    .\bootstrap.ps1                     # 完整安装（含建图）
#    .\bootstrap.ps1 -SkipIndex          # 只装环境，不建图
#    .\bootstrap.ps1 -RepoPath F:\x\y    # 索引另一个仓库
#    .\bootstrap.ps1 -Cpu                # 无 NVIDIA 显卡，装 CPU 版 torch
#    .\bootstrap.ps1 -Check              # 只体检，不改动任何东西
#
#  前置条件：Windows 10/11，已安装 uv（https://docs.astral.sh/uv/）
# ============================================================================
[CmdletBinding()]
param(
    # 要建图的代码仓库。默认读取 repos.json 的 default 条目。
    [string]$RepoPath = '',

    # 跳过依赖安装（已确认 .venv 健康时用）
    [switch]$SkipDeps,

    # 跳过建图（只想要一个能起 MCP 的环境时用）
    [switch]$SkipIndex,

    # 跳过 embedding 模型检查/下载
    [switch]$SkipModel,

    # 强制 CPU 版 torch
    [switch]$Cpu,

    # 只体检，不做任何修改
    [switch]$Check
)

$ErrorActionPreference = 'Stop'

# ---------- 控制台编码 ----------
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
}
catch { }

$Root = $PSScriptRoot
if (-not $Root) { $Root = (Get-Location).Path }

$VenvDir = Join-Path $Root '.venv'
$Python = Join-Path $VenvDir 'Scripts\python.exe'
$CgrExe = Join-Path $VenvDir 'Scripts\cgr.exe'
$EnvFile = Join-Path $Root '.env'
$DataDir = Join-Path $Root 'cgr-data'
$ModelDir = Join-Path $DataDir 'models\bge-m3'

$script:Failures = @()

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "    OK   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    WARN $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "    FAIL $msg" -ForegroundColor Red; $script:Failures += $msg }
function Info($msg) { Write-Host "         $msg" -ForegroundColor DarkGray }

# ============================================================================
# 0. 前置检查
# ============================================================================
Step '0/7' '检查前置条件'

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Fail '未找到 uv'
    Write-Host ''
    Write-Host '  安装 uv（任选其一）：' -ForegroundColor Yellow
    Write-Host '    powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
    Write-Host '    winget install --id=astral-sh.uv -e'
    exit 1
}
Ok "uv $((& uv --version) -replace '^uv ', '')"

$pyOk = $false
try {
    $v = (& uv python list --only-installed 2>$null | Select-String '3\.12').Count
    if ($v -gt 0) { $pyOk = $true }
}
catch { }
if ($pyOk) {
    Ok 'Python 3.12 已就绪'
}
else {
    Warn '未发现 Python 3.12，将按需下载'
    if (-not $Check) {
        & uv python install 3.12
        if ($LASTEXITCODE -ne 0) { Fail 'Python 3.12 安装失败'; exit 1 }
        Ok 'Python 3.12 已安装'
    }
}

# 磁盘空间：venv 约 5GB，模型约 2.2GB，索引约 0.2GB
if (-not $Check) {
    $free = (Get-PSDrive -Name ([System.IO.Path]::GetPathRoot($Root).TrimEnd('\:'))).Free
    if ($free -lt 10GB) {
        Warn ("剩余磁盘 {0:N1} GB，建议至少 10 GB" -f ($free / 1GB))
    }
}

# ============================================================================
# 1. 虚拟环境
# ============================================================================
Step '1/7' '准备虚拟环境'

if (Test-Path -LiteralPath $Python) {
    $ver = (& $Python --version) 2>&1
    Ok ".venv 已存在（$ver）"
}
else {
    if ($Check) {
        Fail '.venv 不存在'
    }
    else {
        Info '创建 .venv ...'
        & uv venv $VenvDir --python 3.12
        if ($LASTEXITCODE -ne 0) { Fail '.venv 创建失败'; exit 1 }
        Ok '.venv 已创建'
    }
}

# ============================================================================
# 2. Python 依赖
# ============================================================================
Step '2/7' '安装 Python 依赖'

if ($Check) {
    if (Test-Path -LiteralPath $Python) {
        # uv writes its summary to stderr; redirecting 2>&1 under
        # $ErrorActionPreference='Stop' turns that into a terminating
        # NativeCommandError. Run it with the preference relaxed instead.
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { $out = & uv pip check --python $Python 2>&1 } finally { $ErrorActionPreference = $prev }
        if ($LASTEXITCODE -eq 0) { Ok '依赖一致' } else { Warn (($out | Select-Object -Last 5) -join ' ') }
    }
}
elseif ($SkipDeps) {
    Warn '已跳过（-SkipDeps）'
}
else {
    # --no-sources：跳过 pyproject 里指向 GitHub 的 tree-sitter-c/cpp 源码依赖。
    # 那些 commit 在受限网络/内网下拉不到，会让整条安装链失败；PyPI 上的等价
    # 版本功能一致。这是本机实测可用的装法。
    Info '安装项目本体 + semantic + 多语言语法包 ...'
    & uv pip install --python $Python --no-sources -e ".[semantic,treesitter-full]"
    if ($LASTEXITCODE -ne 0) {
        Fail '依赖安装失败'
        Info '常见原因：网络无法访问 pypi.org。可加镜像：'
        Info '  $env:UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"'
        exit 1
    }
    Ok '依赖已安装'
}

# ============================================================================
# 3. torch / 推理设备
#
# 默认是 CPU：uv.lock 里锁定的就是 PyPI 的 CPU 轮子，直接可用，功能完整。
# GPU 是可选增强，通过 scripts\install_gpu_torch.ps1 单独安装。
# ============================================================================
Step '3/7' '检查 torch / 推理设备'

if (Test-Path -LiteralPath $Python) {
    $probe = @'
import sys
try:
    import torch
    print("VERSION", torch.__version__)
    print("CUDA_BUILD", torch.version.cuda or "none")
    print("CUDA_OK", torch.cuda.is_available())
    if torch.cuda.is_available():
        print("DEVICE", torch.cuda.get_device_name(0))
except Exception as e:
    print("MISSING", e)
'@
    $tmp = Join-Path $env:TEMP "cgr_torch_probe_$PID.py"
    Set-Content -LiteralPath $tmp -Value $probe -Encoding UTF8
    try { $torchOut = & $Python $tmp 2>&1 } finally { Remove-Item $tmp -ErrorAction SilentlyContinue }

    $torchVer = ($torchOut | Select-String '^VERSION ' | ForEach-Object { $_.Line -replace '^VERSION ', '' })
    $cudaBuild = ($torchOut | Select-String '^CUDA_BUILD ' | ForEach-Object { $_.Line -replace '^CUDA_BUILD ', '' })
    $cudaOk = ($torchOut | Select-String '^CUDA_OK ' | ForEach-Object { $_.Line -replace '^CUDA_OK ', '' })
    $device = ($torchOut | Select-String '^DEVICE ' | ForEach-Object { $_.Line -replace '^DEVICE ', '' })

    if (-not $torchVer) {
        Fail 'torch 未安装（依赖安装可能中断）'
    }
    elseif ($cudaOk -eq 'True') {
        Ok "torch $torchVer / CUDA $cudaBuild / GPU: $device"
        Info '推理将使用 GPU'
    }
    else {
        # CPU 是默认路径，不是错误
        Ok "torch $torchVer（CPU 版）"
        Info '推理使用 CPU：功能完整，3000 个 Java 文件建图约 15~30 分钟'

        $hasNvidia = $false
        if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
            $hasNvidia = $true
        }
        if ($hasNvidia -and -not $Cpu) {
            Info '检测到 NVIDIA 显卡，如需加速（约 3~6 分钟）执行：'
            Info '  .\scripts\install_gpu_torch.ps1 -DryRun   # 先看会装哪个 CUDA 版本'
            Info '  .\scripts\install_gpu_torch.ps1           # 确认后执行'
        }
    }
}

# ============================================================================
# 4. .env 配置
# ============================================================================
Step '4/7' '生成 .env'

if (Test-Path -LiteralPath $EnvFile) {
    Ok '.env 已存在，跳过（不覆盖本地配置）'
    # 检查关键项
    $envText = Get-Content -LiteralPath $EnvFile -Raw -Encoding UTF8
    if ($envText -match 'F:[/\\]code-graph-rag-chinese' -and $Root -notmatch 'code-graph-rag-chinese') {
        Warn '.env 里仍有旧机器的绝对路径，建议手工核对：'
        Info "  当前项目根：$Root"
    }
}
elseif ($Check) {
    Fail '.env 不存在'
}
else {
    # 全部用相对路径 + 运行时推导，换机器无需改动。
    # cgr 的 config 只做 expanduser()，不解析相对路径，所以这里写死绝对路径
    # 但由脚本在生成时按当前目录计算 —— 拉取后跑一次 bootstrap 即可。
    $posix = ($Root -replace '\\', '/')
    # 设备不写死：留空时 embedder._select_device 自动按 CUDA → MPS → CPU 探测。
    # 这样同一份 .env 在有无显卡的机器上都能直接用。
    $deviceLine = if ($Cpu) { 'CGR_EMBEDDING_DEVICE=cpu' } else { '# CGR_EMBEDDING_DEVICE=    # 留空=自动探测 (cuda/mps/cpu)' }
    $lines = @(
        '# ===== 由 bootstrap.ps1 生成 =====',
        "# 项目根：$Root",
        '',
        '# ===== 数据文件 =====',
        "CGR_HOME=$posix/cgr-data",
        "SQLITE_GRAPH_PATH=$posix/cgr-data/db/mes-graph.sqlite",
        "SQLITE_VECTOR_PATH=$posix/cgr-data/db/mes-vectors.sqlite",
        '',
        '# ===== 向量化 =====',
        '# 默认模型 bge-m3：XLM-RoBERTa 编码器，1024 维，原生支持中文 + 英文 + 代码检索',
        'CGR_EMBEDDING_PROVIDER=unixcoder',
        "CGR_EMBEDDING_MODEL=$posix/cgr-data/models/bge-m3",
        'SQLITE_VECTOR_DIM=1024',
        $deviceLine,
        '# 批大小按设备给：CPU 用 64，GPU 16GB 可到 256（显存不足会自动减半重试）',
        'CGR_EMBEDDING_BATCH_SIZE=64',
        'CGR_EMBEDDING_MAX_LENGTH=1024',
        '',
        '# ===== HuggingFace =====',
        'HF_ENDPOINT=https://hf-mirror.com',
        "HF_HUB_CACHE=$posix/cgr-data/hf-cache",
        'HF_HUB_OFFLINE=1',
        '',
        'CGR_SPONSOR_PLEDGE=false'
    )
    # 无 BOM：带 BOM 时首个键会被前导字符污染（python-dotenv 会剥掉，但其他
    # 读取方不一定）。用 WriteAllLines 显式 UTF8 无 BOM。
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($EnvFile, $lines, $utf8NoBom)
    Ok "已生成 $EnvFile"
}

# 目录骨架
if (-not $Check) {
    foreach ($d in @("$DataDir\db", "$DataDir\models", "$DataDir\state", "$DataDir\mcp\logs")) {
        if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
    }
}

# ============================================================================
# 5. Embedding 模型
# ============================================================================
Step '5/7' '检查 embedding 模型'

if ($SkipModel) {
    Warn '已跳过（-SkipModel）'
}
elseif (Test-Path -LiteralPath (Join-Path $ModelDir 'config.json')) {
    $size = (Get-ChildItem -LiteralPath $ModelDir -Recurse -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum / 1GB
    Ok ("bge-m3 已存在（{0:N2} GB）" -f $size)
    if ($size -lt 2) {
        Warn '模型目录偏小（<2 GB），可能不完整'
    }
}
else {
    Warn "模型不存在：$ModelDir"
    if (-not $Check) {
        Info '拉取模型（约 2.2 GB，优先 ModelScope，失败回退 HuggingFace）...'
        & (Join-Path $Root 'scripts\fetch_embedding_model.ps1')
        if ($LASTEXITCODE -ne 0) {
            Fail '模型下载失败'
            Info '可手工执行： .\scripts\fetch_embedding_model.ps1'
            Info '或设置 CGR_EMBEDDING_PROVIDER=openai 改用远程 embedding'
        }
        else {
            Ok '模型已就绪'
        }
    }
}

# ============================================================================
# 6. 建图
# ============================================================================
Step '6/7' '代码索引'

# 解析目标仓库
$targetRepo = $RepoPath

# repos.json 不在版本控制里（绝对路径与本机相关）。首次运行时从模板生成。
$reposFile = Join-Path $Root 'repos.json'
$reposTemplate = Join-Path $Root 'repos.example.json'
if (-not (Test-Path -LiteralPath $reposFile) -and (Test-Path -LiteralPath $reposTemplate) -and -not $Check) {
    Copy-Item -LiteralPath $reposTemplate -Destination $reposFile
    Info '已从 repos.example.json 生成 repos.json（请按需修改路径）'
}

if (-not $targetRepo -and (Test-Path -LiteralPath $reposFile)) {
    try {
        $repos = Get-Content -LiteralPath $reposFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $defaultAlias = $repos.default
        $targetRepo = $repos.repos.$defaultAlias.path
    }
    catch { }
}

if ($SkipIndex) {
    Warn '已跳过（-SkipIndex）'
}
elseif (-not $targetRepo) {
    Warn '未指定 -RepoPath，且 repos.json 无 default 条目'
    Info '用法： .\bootstrap.ps1 -RepoPath F:\your\repo'
}
elseif (-not (Test-Path -LiteralPath $targetRepo)) {
    Fail "目标仓库不存在：$targetRepo"
    Info '修改 repos.json 或传 -RepoPath'
}
else {
    Ok "目标仓库：$targetRepo"
    if ($Check) {
        $graphDb = Join-Path $DataDir 'db\mes-graph.sqlite'
        if (Test-Path -LiteralPath $graphDb) {
            Ok ("图谱已存在（{0:N0} MB）" -f ((Get-Item $graphDb).Length / 1MB))
        }
        else {
            Warn '图谱尚未建立'
        }
    }
    else {
        Info '增量建图（首次为全量，约 10 分钟 / 3000 文件 / GPU）...'
        & $CgrExe start --repo-path $targetRepo --update-graph
        if ($LASTEXITCODE -ne 0) {
            Fail '建图失败'
            Info '查看日志： .\cgr.ps1 logs'
        }
        else {
            Ok '建图完成'
        }
    }
}

# ============================================================================
# 7. 验证
# ============================================================================
Step '7/7' '可用性验证'

if ($Check -or -not (Test-Path -LiteralPath $CgrExe)) {
    if (-not (Test-Path -LiteralPath $CgrExe)) { Fail 'cgr 命令不存在' }
}
else {
    # 7.1 CLI 能跑
    # 注意：不能写 `& $exe --version | Select-Object -First 1` 再读
    # $LASTEXITCODE —— 管道提前关闭会让退出码变成 -1。先取完整输出，
    # 立刻保存退出码，再做字符串处理。
    $verOut = & $CgrExe --version 2>&1
    $verCode = $LASTEXITCODE
    $ver = ($verOut | Where-Object { "$_".Trim() } | Select-Object -First 1)
    if ($verCode -eq 0) { Ok "CLI: $ver" } else { Fail "CLI 无法执行（退出码 $verCode）" }

    # 7.2 配置能被加载，且指向本机路径
    $smoke = @'
import sys
sys.path.insert(0, r"%ROOT%")
try:
    from codebase_rag.config import settings
    print("CONFIG_OK", settings.CGR_HOME)
except Exception as e:
    print("CONFIG_FAIL", type(e).__name__, e)
    sys.exit(1)
'@ -replace '%ROOT%', ($Root -replace '\\', '\\')
    $tmp = Join-Path $env:TEMP "cgr_smoke_$PID.py"
    Set-Content -LiteralPath $tmp -Value $smoke -Encoding UTF8
    try { $r = & $Python $tmp 2>&1 } finally { Remove-Item $tmp -ErrorAction SilentlyContinue }
    if ($r -match 'CONFIG_OK') {
        Ok "配置加载: $(($r | Select-String 'CONFIG_OK').Line -replace '^CONFIG_OK ', 'CGR_HOME=')"
    }
    else {
        Fail "配置加载失败: $r"
    }

    # 7.3 图谱后端可连（doctor 会同时报 LLM provider，那是可选的，单独过滤）
    $doc = & $CgrExe doctor 2>&1
    if ($doc -match 'Embedded graph \(SQLite\) connection successful') {
        Ok '图谱后端可连接'
    }
    else {
        Fail '图谱后端不可用'
        Info ($doc | Select-Object -Last 10)
    }
    if ($doc -match 'Orchestrator model not reachable') {
        Info '提示：未配置 LLM provider（ollama）。MCP 的检索类工具不需要它，'
        Info '      只有 ask_agent / 自然语言问答应答才需要。'
    }
}

# ============================================================================
# 汇总
# ============================================================================
Write-Host ''
Write-Host ('=' * 60) -ForegroundColor DarkGray
if ($script:Failures.Count -eq 0) {
    Write-Host ' 全部检查通过' -ForegroundColor Green
    Write-Host ''
    Write-Host ' 下一步：' -ForegroundColor Cyan
    Write-Host '   1. 启动 MCP 服务：  .\cgr.ps1 start'
    Write-Host '   2. 或在 Claude Code 里直接使用（项目级 .mcp.json 已就绪）'
    Write-Host '   3. 体检：            .\cgr.ps1 doctor'
}
else {
    Write-Host (" 有 {0} 项未通过：" -f $script:Failures.Count) -ForegroundColor Red
    $script:Failures | ForEach-Object { Write-Host "   - $_" -ForegroundColor Red }
    exit 1
}
Write-Host ('=' * 60) -ForegroundColor DarkGray
