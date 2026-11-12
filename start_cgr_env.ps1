<#
.SYNOPSIS
    code-graph-rag (cgr) 环境启动脚本 —— Windows / PowerShell

.DESCRIPTION
    1. 读取项目根目录的 .env（UTF-8，支持注释、引号、export 前缀）
    2. 激活 .venv
    3. 检查 torch / CUDA 是否可用（不做检查可加 -NoGpuCheck）
    4. 运行 cgr（不带参数时只打印环境和用法）

.PARAMETER Install
    重新安装/修复依赖：项目本体 + semantic + 多语言语法包，最后覆盖成 CUDA 版 torch。

.PARAMETER NoGpuCheck
    跳过 GPU 自检。

.PARAMETER CgrArgs
    透传给 cgr 的参数，例如： .\start_cgr_env.ps1 start --repo-path F:\repo --update-graph

.EXAMPLE
    .\start_cgr_env.ps1
    .\start_cgr_env.ps1 status
    .\start_cgr_env.ps1 -Install
    .\start_cgr_env.ps1 start --repo-path F:\qdmes\mes-service --update-graph

.NOTES
    若提示“禁止运行脚本”，用：
        powershell -ExecutionPolicy Bypass -File .\start_cgr_env.ps1
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$Install,
    [switch]$NoGpuCheck,
    # 换显卡/换机器时只需改这两个参数。cu126 的 wheel 在 CUDA 12.4+ 的驱动上都能跑
    # （NVIDIA 小版本兼容），A4000 当前驱动 572.16 支持到 12.8。
    [string]$TorchIndex = 'https://download.pytorch.org/whl/cu126',
    [string]$TorchVersion = '2.14.1',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CgrArgs = @()
)

$ErrorActionPreference = 'Stop'

# ---------- 控制台编码：避免 cgr 输出中文乱码 ----------
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
} catch { }

$root = $PSScriptRoot
if (-not $root) { $root = (Get-Location).Path }

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

# ---------- 1. 加载 .env ----------
$envPath = Join-Path $root '.env'
if (Test-Path -LiteralPath $envPath) {
    $loaded = 0
    $skipped = 0
    foreach ($line in (Get-Content -LiteralPath $envPath -Encoding UTF8)) {
        $text = $line.Trim()
        if (-not $text) { continue }
        if ($text.StartsWith('#')) { continue }
        if ($text.StartsWith('export ')) { $text = $text.Substring(7).Trim() }

        $eq = $text.IndexOf('=')
        if ($eq -lt 1) { $skipped++; continue }

        $key = $text.Substring(0, $eq).Trim()
        $value = $text.Substring($eq + 1).Trim()

        # 去掉成对引号（.env 里 "值" 和 '值' 都是合法的）
        if ($value.Length -ge 2) {
            $first = $value.Substring(0, 1)
            $last = $value.Substring($value.Length - 1, 1)
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }

        if ($key -match '^[A-Za-z_][A-Za-z0-9_]*$') {
            [Environment]::SetEnvironmentVariable($key, $value, 'Process')
            $loaded++
        }
        else {
            $skipped++
        }
    }
    Write-Ok ".env 已加载：$loaded 项（跳过 $skipped 行）"
}
else {
    Write-Warn2 "未找到 $envPath，使用系统环境变量"
}

# ---------- 2. 定位虚拟环境 ----------
$venvDir = Join-Path $root '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$cgrExe = Join-Path $venvDir 'Scripts\cgr.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "未找到虚拟环境：$venvPython`n请先在项目根目录执行： uv venv .venv"
}

$activate = Join-Path $venvDir 'Scripts\Activate.ps1'
if (Test-Path -LiteralPath $activate) {
    . $activate
    Write-Ok "已激活 .venv"
}

# ---------- 3. 安装 / 修复依赖 ----------
if ($Install) {
    Write-Step '安装项目依赖（semantic + 多语言语法包）'
    # --no-sources：跳过 pyproject 里指向 GitHub 的 tree-sitter-c/cpp 源码依赖，
    # 那些 commit 在部分网络/仓库状态下拉不到，会直接让安装失败。
    & uv pip install --python $venvPython --no-sources -e ".[semantic,treesitter-full]"
    if ($LASTEXITCODE -ne 0) { throw '项目依赖安装失败' }

    Write-Step "安装 CUDA 版 torch（$TorchIndex, torch==$TorchVersion）"
    # PyPI 上的 Windows torch 是 CPU 版；transformers 5.x 又要求 torch>=2.6，
    # 所以必须从 PyTorch 官方索引装。--index-url 只用于本次安装，不会写进配置。
    & uv pip install --python $venvPython `
        --index-url $TorchIndex `
        --upgrade "torch==$TorchVersion"
    if ($LASTEXITCODE -ne 0) { throw 'torch CUDA 版安装失败' }

    Write-Ok '依赖安装完成'
}

# ---------- 4. GPU 自检 ----------
if (-not $NoGpuCheck) {
    Write-Step '检查运行环境'
    $probe = @'
import sys
try:
    import torch
except Exception as e:
    print("TORCH_MISSING", e)
    sys.exit(0)
print("torch", torch.__version__)
print("cuda_available", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device", torch.cuda.get_device_name(0))
try:
    import transformers
    print("transformers", transformers.__version__)
except Exception as e:
    print("transformers MISSING", e)
'@
    $probeFile = Join-Path $env:TEMP "cgr_probe_$PID.py"
    Set-Content -LiteralPath $probeFile -Value $probe -Encoding UTF8
    try {
        & $venvPython $probeFile
    }
    finally {
        Remove-Item -LiteralPath $probeFile -ErrorAction SilentlyContinue
    }

    if ($env:CGR_EMBEDDING_DEVICE) {
        Write-Ok "CGR_EMBEDDING_DEVICE=$($env:CGR_EMBEDDING_DEVICE)"
    }
}

# ---------- 5. 运行 cgr ----------
if ($CgrArgs.Count -gt 0) {
    if (-not (Test-Path -LiteralPath $cgrExe)) {
        throw "未找到 cgr 命令：$cgrExe`n请执行： .\start_cgr_env.ps1 -Install"
    }
    Write-Step "cgr $($CgrArgs -join ' ')"
    & $cgrExe @CgrArgs
    exit $LASTEXITCODE
}

Write-Host ''
Write-Host '环境就绪。常用命令：' -ForegroundColor Cyan
Write-Host '  .\start_cgr_env.ps1 status'
Write-Host '  .\start_cgr_env.ps1 start --repo-path F:\your\repo --update-graph --clean'
Write-Host '  .\start_cgr_env.ps1 context "你的问题"'
Write-Host '  .\start_cgr_env.ps1 mcp-server'
Write-Host '  .\start_cgr_env.ps1 -Install     # 依赖坏了/换机器后重装'

