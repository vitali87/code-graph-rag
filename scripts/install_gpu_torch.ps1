<#
# ============================================================================
#  install_gpu_torch.ps1 -- 可选：安装 GPU 版 torch
#  【默认不需要运行本脚本】
#  项目默认使用 CPU 版 torch（uv.lock 里锁定的就是 PyPI 的 CPU 轮子），
#  功能完全一致，只是建图慢一些（3000 个 Java 文件约 15~30 分钟）。
#
#  只有当你希望把建图时间压到 3~6 分钟时才需要装 GPU 版。
#
#  为什么不能直接 pip install torch：PyPI 上的 Windows torch 只有 CPU 版，
#  GPU 版只发布在 PyTorch 官方索引上，且必须按显卡架构选择 CUDA 版本。
#
#  用法：
#    .\scripts\install_gpu_torch.ps1                 # 自动检测显卡并选择 CUDA 版本
#    .\scripts\install_gpu_torch.ps1 -List           # 只列出可选组合
#    .\scripts\install_gpu_torch.ps1 -Cu cu128       # 手动指定 CUDA 版本
#    .\scripts\install_gpu_torch.ps1 -DryRun         # 只显示将执行的命令
#    .\scripts\install_gpu_torch.ps1 -Revert         # 回退到 CPU 版
#
#  离线 / 已有 wheel（推荐，避免 2.4 GB 下载）：
#    .\scripts\install_gpu_torch.ps1 -WheelPath E:\resource\torch-2.14.1+cu126-cp312-cp312-win_amd64.whl
#    .\scripts\install_gpu_torch.ps1 -WheelDir E:\resource      # 自动在目录里找匹配的 wheel
# ============================================================================
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    # CUDA 版本标签。留空则按显卡型号自动选择。
    [ValidateSet('', 'cu118', 'cu121', 'cu124', 'cu126', 'cu128', 'cu129')]
    [string]$Cu = '',

    # 显式指定 torch 版本。留空则用该 CUDA 索引下的最新版。
    [string]$TorchVersion = '',

    # 本地 wheel 文件的完整路径。指定后跳过下载，直接安装。
    [string]$WheelPath = '',

    # 本地 wheel 所在目录。按当前显卡/CUDA 版本自动匹配文件名。
    [string]$WheelDir = '',

    # 只列出可选组合，不安装
    [switch]$List,

    # 只显示将执行的命令
    [switch]$DryRun,

    # 回退到 CPU 版
    [switch]$Revert,

    # 下载源：pypi（官方，需能访问 download.pytorch.org）/ aliyun（国内镜像）
    [ValidateSet('pypi', 'aliyun')]
    [string]$Index = 'pypi'
)

$ErrorActionPreference = 'Stop'

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONUTF8 = '1'
}
catch { }

$Root = $PSScriptRoot
if (-not $Root) { $Root = (Get-Location).Path }
if (-not (Test-Path (Join-Path $Root '.venv\Scripts\python.exe'))) {
    $Root = Split-Path -Parent $PSScriptRoot
}
$Python = Join-Path $Root '.venv\Scripts\python.exe'

function Write-Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok($m) { Write-Host "    OK   $m" -ForegroundColor Green }
function Write-Warn2($m) { Write-Host "    WARN $m" -ForegroundColor Yellow }
function Write-Err2($m) { Write-Host "    FAIL $m" -ForegroundColor Red }
function Write-Info($m) { Write-Host "         $m" -ForegroundColor DarkGray }

# ============================================================================
# 显卡 → CUDA 版本映射
#
# 依据：
#   - 计算能力 < 5.0（GTX 700 及更早）已不被 torch 2.x 支持，直接排除
#   - 计算能力 5.x / 6.x / 7.0：只能用 cu118（更新的 CUDA 已移除对应内核）
#   - 计算能力 7.5+（RTX 20 系及以后）：cu124 / cu126 / cu128 都可用
#   - Blackwell（RTX 50 系 / B100 等，计算能力 10.x+）：需要 cu128 及以上
#   - 驱动版本决定上限：CUDA 12.x 驱动向下兼容所有 12.x 运行时
# ============================================================================
$GpuMap = @(
    # 关键字           推荐 CUDA   说明
    @{ Match = 'RTX 50'; Cu = 'cu128'; Note = 'Blackwell 架构，需 CUDA 12.8+' }
    @{ Match = 'B100|B200|H100|H200|A100|A30|A10|L40|L4'; Cu = 'cu128'; Note = '数据中心卡，CUDA 12.8+' }
    @{ Match = 'RTX 40'; Cu = 'cu126'; Note = 'Ada 架构，CUDA 12.6 稳定' }
    @{ Match = 'RTX 30'; Cu = 'cu126'; Note = 'Ampere 架构，CUDA 12.6 稳定' }
    @{ Match = 'RTX 20|GTX 16'; Cu = 'cu126'; Note = 'Turing 架构，CUDA 12.6 稳定' }
    @{ Match = 'A4000|A5000|A6000|A2000'; Cu = 'cu126'; Note = 'Ampere 工作站卡' }
    @{ Match = 'GTX 10'; Cu = 'cu118'; Note = 'Pascal 架构，只能用 CUDA 11.8' }
    @{ Match = 'Quadro'; Cu = 'cu118'; Note = '旧款专业卡，保守选 CUDA 11.8' }
)

# ============================================================================
# 列出可选组合
# ============================================================================
if ($List) {
    Write-Host ''
    Write-Host '可选的 CUDA 版本组合（Windows x64 / Python 3.12）：' -ForegroundColor Cyan
    Write-Host ''
    $rows = @(
        [pscustomobject]@{ CUDA = 'cu118'; 适用显卡 = 'GTX 10 系 / 旧款 Quadro'; 说明 = 'Pascal 及更早' }
        [pscustomobject]@{ CUDA = 'cu124'; 适用显卡 = 'RTX 20/30/40 系'; 说明 = '较保守的选择' }
        [pscustomobject]@{ CUDA = 'cu126'; 适用显卡 = 'RTX 20/30/40 系 / A 系列'; 说明 = '推荐（默认）' }
        [pscustomobject]@{ CUDA = 'cu128'; 适用显卡 = 'RTX 50 系 / H100 / B200'; 说明 = 'Blackwell 必需' }
        [pscustomobject]@{ CUDA = 'cu129'; 适用显卡 = '最新卡'; 说明 = '最新，可能不稳定' }
    )
    $rows | Format-Table -AutoSize
    Write-Host '  驱动要求：CUDA 12.x 需要 NVIDIA 驱动 >= 525（Windows）' -ForegroundColor DarkGray
    Write-Host '  查询驱动：nvidia-smi' -ForegroundColor DarkGray
    Write-Host ''
    exit 0
}

# ============================================================================
# 回退到 CPU
# ============================================================================
if ($Revert) {
    Write-Step '回退到 CPU 版 torch'
    if (-not (Test-Path $Python)) { throw "找不到虚拟环境：$Python" }
    & uv pip install --python $Python --force-reinstall --no-cache "torch" 2>&1 | Write-Host
    if ($LASTEXITCODE -ne 0) { Write-Err2 '回退失败'; exit 1 }
    Write-Ok '已回退到 CPU 版'
    & $Python -c "import torch; print('torch', torch.__version__, '| cuda', torch.version.cuda or 'none')"
    exit 0
}

# ============================================================================
# 前置检查
# ============================================================================
Write-Step '检查环境'

if (-not (Test-Path $Python)) {
    Write-Err2 "找不到虚拟环境：$Python"
    Write-Info '请先运行 .\bootstrap.ps1'
    exit 1
}
Write-Ok "Python: $(& $Python --version 2>&1)"

# 关键：torch 的 .pyd 被正在运行的 MCP 服务占用时，重装会报
# "failed to remove file ... os error 5 (拒绝访问)"，且回滚到一半，
# 环境可能处于不可用状态。安装前必须先停掉所有占用 venv 的进程。
Write-Host "`n[检查占用]" -ForegroundColor Cyan
$venvPath = (Resolve-Path -LiteralPath (Join-Path $Root '.venv')).Path
$busy = @()
try {
    $busy = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='cgr.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($venvPath) })
}
catch { }

if ($busy.Count -gt 0) {
    Write-Warn2 "检测到 $($busy.Count) 个进程正在使用该虚拟环境："
    foreach ($p in $busy) {
        $cmd = if ($p.CommandLine.Length -gt 90) { $p.CommandLine.Substring(0, 90) + '...' } else { $p.CommandLine }
        Write-Info "PID $($p.ProcessId)  $cmd"
    }
    Write-Host ''
    Write-Host '  这些进程会锁定 torch 的 .pyd 文件，导致安装失败。' -ForegroundColor Yellow
    Write-Host '  建议先停止 MCP 服务：' -ForegroundColor Yellow
    Write-Host '      .\cgr.ps1 stop' -ForegroundColor White
    Write-Host ''
    if ($DryRun) {
        Write-Info '（-DryRun 模式，不会真正安装，可以忽略）'
    }
    else {
        $answer = Read-Host '  是否现在自动停止这些进程？(y/N)'
        if ($answer -match '^[Yy]$') {
            foreach ($p in $busy) {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            }
            Start-Sleep -Seconds 2
            Write-Ok '已停止'
        }
        else {
            Write-Err2 '已取消。请先执行 .\cgr.ps1 stop 后重试。'
            exit 1
        }
    }
}
else {
    Write-Ok '没有进程占用'
}

$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if (-not $smi) {
    Write-Err2 '未检测到 nvidia-smi'
    Write-Info '这通常意味着：没有 NVIDIA 显卡，或没装驱动。'
    Write-Info 'CPU 版 torch 已可用，无需本脚本。'
    exit 1
}

$gpuInfo = & nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Err2 'nvidia-smi 执行失败'
    exit 1
}
Write-Ok "GPU: $gpuInfo"

$gpuName = ($gpuInfo -split ',')[0].Trim()
$driverVer = ($gpuInfo -split ',')[-1].Trim()

# 驱动版本 → 支持的 CUDA 上限
$driverMajor = 0
if ($driverVer -match '^(\d+)') { $driverMajor = [int]$Matches[1] }
Write-Info "驱动版本 $driverVer"

# ============================================================================
# 选择 CUDA 版本
# ============================================================================
Write-Step '选择 CUDA 版本'

if ($Cu) {
    Write-Ok "手动指定：$Cu"
}
else {
    $picked = $null
    foreach ($entry in $GpuMap) {
        if ($gpuName -match $entry.Match) {
            $picked = $entry
            break
        }
    }
    if ($picked) {
        $Cu = $picked.Cu
        Write-Ok "按显卡自动选择：$Cu（$($picked.Note)）"
    }
    else {
        $Cu = 'cu126'
        Write-Warn2 "未识别的显卡型号，保守选择 cu126"
    }
}

# 驱动兼容性检查：CUDA 12.x 运行时需要 R525+（Windows）
if ($Cu -match 'cu12' -and $driverMajor -gt 0 -and $driverMajor -lt 525) {
    Write-Warn2 "驱动版本 $driverVer 可能过旧（CUDA 12.x 建议 R525+）"
    Write-Info '建议先更新 NVIDIA 驱动，或改用 -Cu cu118'
}

# ============================================================================
# 解析本地 wheel（-WheelPath / -WheelDir）
#
# 用途：torch 的 CUDA 轮子约 2.4 GB，从 download.pytorch.org 拉可能极慢。
# 提前下好放在本地，直接安装即可，不经过网络。
# ============================================================================
$localWheel = ''

if ($WheelPath) {
    if (-not (Test-Path -LiteralPath $WheelPath)) {
        Write-Err2 "找不到 wheel 文件：$WheelPath"
        exit 1
    }
    $localWheel = (Resolve-Path -LiteralPath $WheelPath).Path
    Write-Step '使用本地 wheel'
    Write-Ok $localWheel
}
elseif ($WheelDir) {
    if (-not (Test-Path -LiteralPath $WheelDir)) {
        Write-Err2 "目录不存在：$WheelDir"
        exit 1
    }
    Write-Step '在目录中查找匹配的 wheel'
    Write-Info "目录: $WheelDir"
    Write-Info "目标: torch-*-$Cu-cp312-cp312-win_amd64.whl"

    # 优先精确匹配选定的 CUDA 版本，其次任意 torch CUDA wheel
    $pattern = "torch-*-$Cu-cp312-cp312-win_amd64.whl"
    $candidates = @(Get-ChildItem -LiteralPath $WheelDir -Filter $pattern -File -ErrorAction SilentlyContinue)
    if ($candidates.Count -eq 0) {
        $candidates = @(Get-ChildItem -LiteralPath $WheelDir -Filter 'torch-*cu*-cp312-cp312-win_amd64.whl' -File -ErrorAction SilentlyContinue)
    }
    if ($candidates.Count -eq 0) {
        Write-Err2 "目录中没有找到 torch CUDA wheel"
        Write-Info '文件名应形如 torch-2.14.1+cu126-cp312-cp312-win_amd64.whl'
        exit 1
    }
    if ($candidates.Count -gt 1) {
        Write-Warn2 "找到多个候选，使用第一个："
        $candidates | ForEach-Object { Write-Info "  $($_.Name)" }
    }
    $localWheel = $candidates[0].FullName
    Write-Ok "选中: $($candidates[0].Name)"
}

if ($localWheel) {
    # 从文件名反推 CUDA 版本，供后续提示使用
    if ($localWheel -match '\+?(cu\d+)') { $Cu = $Matches[1] }
    $sizeGB = (Get-Item -LiteralPath $localWheel).Length / 1GB
    Write-Info ("文件大小 {0:N2} GB" -f $sizeGB)
}

# ============================================================================
# 构造安装命令
# ============================================================================
if ($localWheel) {
    # 本地文件安装：不联网、不解析索引。--no-index 保证不会去 PyPI 找依赖，
    # torch 的依赖（filelock/sympy 等）已在依赖安装阶段装好。
    $args = @(
        'pip', 'install', '--python', $Python,
        '--no-index',
        '--force-reinstall',
        '--no-deps',
        $localWheel
    )
    Write-Step '安装（本地文件）'
    Write-Info "wheel: $localWheel"
}
else {
    $indexUrl = if ($Index -eq 'aliyun') {
        "https://mirrors.aliyun.com/pytorch-wheels/$Cu/"
    }
    else {
        "https://download.pytorch.org/whl/$Cu"
    }

    $pkgSpec = if ($TorchVersion) { "torch==$TorchVersion+$Cu" } else { "torch" }

    Write-Step '安装（在线）'
    Write-Info "索引: $indexUrl"
    Write-Info "包:   $pkgSpec"

    $args = @(
        'pip', 'install', '--python', $Python,
        '--index-url', $indexUrl,
        '--upgrade', $pkgSpec
    )
}

if ($DryRun) {
    Write-Host ''
    Write-Host '将执行：' -ForegroundColor Yellow
    Write-Host "  uv $($args -join ' ')"
    Write-Host ''
    Write-Host '去掉 -DryRun 即真正执行。' -ForegroundColor DarkGray
    exit 0
}

& uv @args
if ($LASTEXITCODE -ne 0) {
    Write-Err2 '安装失败'
    Write-Host ''
    if ($localWheel) {
        Write-Host '可能原因：' -ForegroundColor Yellow
        Write-Host "  1. wheel 与 Python 版本不匹配（需要 cp312）"
        Write-Host "  2. wheel 与当前 CUDA 驱动不兼容"
        Write-Host "  3. 文件损坏 —— 重新下载"
    }
    else {
        Write-Host '可能原因：' -ForegroundColor Yellow
        Write-Host "  1. 无法访问 $indexUrl —— 试试 -Index aliyun，或用 -WheelPath 指定本地文件"
        Write-Host "  2. 该 CUDA 版本没有对应 Python 3.12 的轮子 —— 试试 -List 看可选组合"
        Write-Host "  3. 驱动版本过旧 —— 更新 NVIDIA 驱动后重试"
    }
    exit 1
}

# ============================================================================
# 校验
# ============================================================================
Write-Step '校验'

$verify = @'
import torch
print("torch", torch.__version__)
print("cuda_build", torch.version.cuda or "none")
print("cuda_available", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device", torch.cuda.get_device_name(0))
    print("capability", ".".join(map(str, torch.cuda.get_device_capability(0))))
else:
    print("hint", "torch 是 CUDA 版但检测不到设备，检查驱动版本")
'@
$tmp = Join-Path $env:TEMP "cgr_gpu_verify_$PID.py"
Set-Content -LiteralPath $tmp -Value $verify -Encoding UTF8
try { $out = & $Python $tmp 2>&1 } finally { Remove-Item $tmp -ErrorAction SilentlyContinue }
$out | ForEach-Object { Write-Host "    $_" }

$cudaOk = $out | Select-String '^cuda_available True'
Write-Host ''
if ($cudaOk) {
    Write-Ok 'GPU 版 torch 已就绪，建图会自动使用 GPU'
    Write-Info '如需强制 CPU，在 .env 设置 CGR_EMBEDDING_DEVICE=cpu'
    Write-Info '如需调大显存占用换速度，调整 CGR_EMBEDDING_BATCH_SIZE（默认 64，A4000 可设 256）'
}
else {
    Write-Warn2 'CUDA 不可用，将回退到 CPU 推理'
    Write-Info '检查：nvidia-smi 是否正常、驱动版本是否 >= 525'
}
