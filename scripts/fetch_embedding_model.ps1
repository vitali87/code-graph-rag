<#
# ============================================================================
#  fetch_embedding_model.ps1 -- 下载 bge-m3 embedding 模型到本地
#
#  为什么需要这一步：cgr 的向量检索依赖一个本地 embedding 模型。默认从
#  HuggingFace 拉，国内经常超时。这里优先走 ModelScope（国内可达、支持
#  断点续传），失败再回退 HuggingFace 镜像。
#
#  下载约 2.2 GB。完成后 .env 的 CGR_EMBEDDING_MODEL 指向该目录即可完全
#  离线运行。
#
#  用法：
#    .\scripts\fetch_embedding_model.ps1
#    .\scripts\fetch_embedding_model.ps1 -Source huggingface
#    .\scripts\fetch_embedding_model.ps1 -Target D:\models\bge-m3
# ============================================================================
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    # ModelScope / HuggingFace 上的模型 ID
    [string]$Model = 'BAAI/bge-m3',

    # 本地保存目录。默认 <项目根>/cgr-data/models/<模型名>
    [string]$Target = '',

    # auto: 先 ModelScope，失败回退 HuggingFace；也可强制指定
    [ValidateSet('auto', 'modelscope', 'huggingface')]
    [string]$Source = 'auto'
)

$ErrorActionPreference = 'Stop'

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONUTF8 = '1'
}
catch { }

$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root '.venv\Scripts\python.exe'))) {
    $Root = (Get-Location).Path
}
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    throw "找不到虚拟环境：$Python`n请先运行 .\bootstrap.ps1"
}

if (-not $Target) {
    $Target = Join-Path $Root ('cgr-data\models\' + ($Model -split '/')[-1])
}
New-Item -ItemType Directory -Force -Path $Target | Out-Null

function Test-Net([string]$url) {
    try {
        $r = Invoke-WebRequest -Uri $url -Method Head -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop
        return $r.StatusCode -lt 500
    }
    catch { return $false }
}

# ---------------------------------------------------------------------------
# 下载实现
# ---------------------------------------------------------------------------
$pyModelScope = @'
import sys
from modelscope import snapshot_download
p = snapshot_download(sys.argv[1], local_dir=sys.argv[2])
print("DONE", p)
'@

$pyHuggingFace = @'
import sys
from huggingface_hub import snapshot_download
p = snapshot_download(repo_id=sys.argv[1], local_dir=sys.argv[2])
print("DONE", p)
'@

function Invoke-ModelScope {
    Write-Host '==> 尝试 ModelScope' -ForegroundColor Cyan
    & uv pip install --python $Python --quiet modelscope 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host '    modelscope 安装失败' -ForegroundColor Yellow
        return $false
    }
    $tmp = Join-Path $env:TEMP "cgr_dl_ms_$PID.py"
    Set-Content -LiteralPath $tmp -Value $pyModelScope -Encoding UTF8
    try {
        & $Python $tmp $Model $Target
        return ($LASTEXITCODE -eq 0)
    }
    finally { Remove-Item $tmp -ErrorAction SilentlyContinue }
}

function Invoke-HuggingFace {
    Write-Host '==> 尝试 HuggingFace' -ForegroundColor Cyan
    # huggingface-hub 已是主依赖；HF_ENDPOINT 决定镜像
    if (-not $env:HF_ENDPOINT) { $env:HF_ENDPOINT = 'https://hf-mirror.com' }
    Write-Host "    HF_ENDPOINT=$($env:HF_ENDPOINT)" -ForegroundColor DarkGray
    # 下载时必须允许联网
    $prevOffline = $env:HF_HUB_OFFLINE
    $env:HF_HUB_OFFLINE = '0'
    $tmp = Join-Path $env:TEMP "cgr_dl_hf_$PID.py"
    Set-Content -LiteralPath $tmp -Value $pyHuggingFace -Encoding UTF8
    try {
        & $Python $tmp $Model $Target
        return ($LASTEXITCODE -eq 0)
    }
    finally {
        Remove-Item $tmp -ErrorAction SilentlyContinue
        $env:HF_HUB_OFFLINE = $prevOffline
    }
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
$ok = $false
switch ($Source) {
    'modelscope' { $ok = Invoke-ModelScope }
    'huggingface' { $ok = Invoke-HuggingFace }
    'auto' {
        if (Test-Net 'https://www.modelscope.cn') {
            $ok = Invoke-ModelScope
        }
        else {
            Write-Host '    ModelScope 不可达，跳过' -ForegroundColor DarkGray
        }
        if (-not $ok) {
            Write-Host '    ModelScope 失败，回退 HuggingFace' -ForegroundColor Yellow
            $ok = Invoke-HuggingFace
        }
    }
}

if (-not $ok) {
    Write-Host ''
    Write-Host '模型下载失败。可选方案：' -ForegroundColor Red
    Write-Host '  1. 手动下载 bge-m3 到 ' -NoNewline
    Write-Host $Target
    Write-Host '     https://modelscope.cn/models/BAAI/bge-m3'
    Write-Host '     https://huggingface.co/BAAI/bge-m3'
    Write-Host '  2. 换用远程 embedding（不需要本地模型）：'
    Write-Host '       在 .env 里设置 CGR_EMBEDDING_PROVIDER=openai 及对应 API Key'
    exit 1
}

# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
Write-Host ''
$files = Get-ChildItem -LiteralPath $Target -File -ErrorAction SilentlyContinue
$sizeGB = ($files | Measure-Object -Property Length -Sum).Sum / 1GB
if (-not (Test-Path -LiteralPath (Join-Path $Target 'config.json'))) {
    Write-Host "警告：$Target 下没有 config.json，模型可能不完整" -ForegroundColor Yellow
}
else {
    Write-Host ("完成：{0}（{1:N2} GB）" -f $Target, $sizeGB) -ForegroundColor Green
}

Write-Host ''
Write-Host '.env 中应有：' -ForegroundColor Cyan
Write-Host "  CGR_EMBEDDING_MODEL=$(($Target -replace '\\','/'))"
Write-Host '  SQLITE_VECTOR_DIM=1024'
Write-Host '  HF_HUB_OFFLINE=1'
