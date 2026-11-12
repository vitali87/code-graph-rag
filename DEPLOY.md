# 部署与迁移

本文档说明如何在**一台全新的 Windows 机器**上把本项目拉起来，以及日常如何维护。

> 已有环境（本机）请直接跳到 [日常维护](#日常维护)。

---

## 一、前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10 / 11 | PowerShell 5.1+ |
| Python | 3.12.x | 由 `uv` 自动下载，无需预装 |
| uv | 任意较新版本 | **必须**，见下方安装 |
| 磁盘 | ≥ 15 GB 可用 | venv 5 GB + 模型 2.2 GB + 索引 0.3 GB + 余量 |
| 显卡 | NVIDIA（可选） | 无显卡用 `-Cpu`，功能完整但建图慢 5~10 倍 |
| 网络 | 能访问 pypi.org | 模型下载另需 ModelScope 或 hf-mirror |

安装 uv（任选其一）：

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
# 或
winget install --id=astral-sh.uv -e
```

---

## 二、首次部署

```powershell
git clone <本仓库地址> F:\code-graph-rag-chinese
cd F:\code-graph-rag-chinese

# 1. 先体检，确认环境判断正确（不修改任何东西）
.\bootstrap.ps1 -Check

# 2. 正式安装
.\bootstrap.ps1 -RepoPath F:\your\code\repo
```

`bootstrap.ps1` 会依次完成 7 步，每步幂等，可重复执行：

| 步骤 | 动作 | 失败时会怎样 |
|---|---|---|
| 0/7 | 检查 uv / Python / 磁盘 | 给出安装命令后退出 |
| 1/7 | 创建 `.venv` | — |
| 2/7 | `uv pip install --no-sources -e ".[semantic,treesitter-full]"` | 提示配镜像 |
| 3/7 | 检查 torch / CUDA | 提示装 GPU 版 torch |
| 4/7 | 生成 `.env`（绝对路径按当前目录计算） | — |
| 5/7 | 下载 bge-m3 模型（2.2 GB） | 提示手工下载地址 |
| 6/7 | 增量建图 | 提示查日志 |
| 7/7 | 可用性验证 | 汇总所有失败项 |

### 常用参数

```powershell
.\bootstrap.ps1 -Check              # 只体检
.\bootstrap.ps1 -SkipDeps           # 跳过依赖安装（venv 已健康）
.\bootstrap.ps1 -SkipIndex          # 只装环境，不建图
.\bootstrap.ps1 -SkipModel          # 跳过模型下载
.\bootstrap.ps1 -Cpu                # 强制 .env 里写死 CPU 设备
.\bootstrap.ps1 -RepoPath D:\a\b    # 索引指定仓库
```

---

## 三、CPU 与 GPU

### 默认就是 CPU，开箱即用

`uv.lock` 锁定的就是 PyPI 上的 **CPU 版 torch**（`torch 2.15.0+cpu`），
`uv sync --frozen` 直接可用，**不需要任何额外步骤**。

CPU 与 GPU 版本在功能上完全一致，只是建图耗时不同：

| | 建图耗时（3000 个 Java 文件） | 查询响应 |
|---|---|---|
| CPU（默认） | 约 15~30 分钟 | 秒级 |
| GPU | 约 3~6 分钟 | 秒级 |

> 日常 `sync` 是增量的，通常几秒到几十秒，CPU/GPU 差别不大。
> 只有首次建图和 `rebuild` 才明显。

### 需要 GPU 时

GPU 版 torch 不在 PyPI 上（PyPI 的 Windows torch 只有 CPU 版），
必须从 PyTorch 官方索引安装，且 **CUDA 版本要匹配显卡架构**。

脚本会自动识别显卡并选择正确的 CUDA 版本：

```powershell
# 1. 先看会装什么（不实际安装）
.\scripts\install_gpu_torch.ps1 -DryRun

# 2. 确认后执行
.\scripts\install_gpu_torch.ps1

# 3. 查看所有可选组合
.\scripts\install_gpu_torch.ps1 -List

# 4. 手动指定 CUDA 版本
.\scripts\install_gpu_torch.ps1 -Cu cu128

# 5. 回退到 CPU 版
.\scripts\install_gpu_torch.ps1 -Revert
```

### 已有 wheel 文件（推荐，省去 2.4 GB 下载）

torch 的 CUDA 轮子约 2.4 GB，从 `download.pytorch.org` 拉可能极慢。
提前下好放在本地，直接安装：

```powershell
# 指定文件
.\scripts\install_gpu_torch.ps1 -WheelPath E:\resource\torch-2.14.1+cu126-cp312-cp312-win_amd64.whl

# 或指定目录，按当前显卡自动匹配文件名
.\scripts\install_gpu_torch.ps1 -WheelDir E:\resource
```

> **安装前会自动检测占用**。torch 的 `.pyd` 文件被运行中的 MCP 服务锁住时，
> 安装会报 `os error 5 拒绝访问` 且可能留下半装状态。脚本会列出占用进程并询问是否停止。
> 也可以先手动 `.\cgr.ps1 stop`。

**wheel 命名规则**（下载时对照）：

```
torch-<版本>+<CUDA标签>-cp312-cp312-win_amd64.whl
例：torch-2.14.1+cu126-cp312-cp312-win_amd64.whl
     ↑版本    ↑CUDA   ↑Python 3.12  ↑Windows 64 位
```

官方下载地址（把 `{cu}` 换成 cu118/cu126/cu128）：
`https://download.pytorch.org/whl/{cu}/torch-2.14.1%2B{cu}-cp312-cp312-win_amd64.whl`

**显卡型号 → CUDA 版本对照**

| 显卡 | 推荐 CUDA | 说明 |
|---|---|---|
| RTX 50 系（5060/5070/5080/5090） | `cu128` | Blackwell，必须 12.8+ |
| H100 / H200 / B100 / B200 | `cu128` | 数据中心卡 |
| RTX 40 系（4060~4090） | `cu126` | Ada |
| RTX 30 系（3060~3090） | `cu126` | Ampere |
| RTX 20 系 / GTX 16 系 | `cu126` | Turing |
| A4000 / A5000 / A6000 | `cu126` | Ampere 工作站卡 |
| GTX 10 系（1050~1080） | `cu118` | Pascal，只能用 11.8 |
| 旧款 Quadro | `cu118` | 保守选择 |

**驱动要求**

- CUDA 12.x 需要 NVIDIA 驱动 **≥ 525**（Windows）
- 查询当前驱动：`nvidia-smi`（右上角 `Driver Version`）
- 驱动过旧时脚本会提示，但不会阻止安装

**国内网络**

默认走 `download.pytorch.org`（需能访问）。若被墙：

```powershell
.\scripts\install_gpu_torch.ps1 -Index aliyun
```

### 设备选择是自动的

`.env` 里 `CGR_EMBEDDING_DEVICE` **留空即可**，代码会按 `CUDA → MPS → CPU`
自动探测（见 `codebase_rag/embedder.py` 的 `_select_device`）。
同一份配置在有无显卡的机器上都能直接用。

需要强制指定时才写：

```ini
CGR_EMBEDDING_DEVICE=cpu    # 有显卡但想省显存
CGR_EMBEDDING_DEVICE=cuda   # 要求必须用 GPU，没有则告警并降级
```

### 关于 uv.lock

`uv.lock` 锁定 CPU 版 torch。**不要**在装了 GPU 版之后跑 `uv sync --frozen`，
它会把 GPU 版换回 CPU 版。依赖变更请用：

```powershell
uv pip install --no-sources -e ".[semantic,treesitter-full]"
```

---

## 四、默认模型：bge-m3（中英文都支持）

项目默认 embedding 模型是 **`BAAI/bge-m3`**，选它的原因：

- **XLM-RoBERTa 编码器**，在中文、英文、代码三种语料上联合训练
- 1024 维，8192 token 上下文（`CGR_EMBEDDING_MAX_LENGTH` 默认 1024，覆盖绝大多数方法）
- 原生支持 transformers，不需要 `trust_remote_code`
- 代码注释里明确标注：*trained for Chinese + English + code retrieval*

因此**中英文混合的代码库、中文注释、中文业务概念查询都能正常检索**，
不需要额外配置。实测 `semantic_search("报废品分层管理")` 能命中对应代码。

> 代码里 `CGR_EMBEDDING_PROVIDER` 的取值仍是 `unixcoder`，这是历史命名
> （早期确实用过 Microsoft UniXcoder），实际加载的是 bge-m3。
> 见 `codebase_rag/constants/providers.py:92`。

### 更换模型

需要其他模型时，在 `.env` 里改两处（维度必须与模型一致）：

```ini
CGR_EMBEDDING_MODEL=F:/path/to/your/model
SQLITE_VECTOR_DIM=1024    # 必须等于模型的向量维度
```

**换模型后必须重建索引**，否则向量维度不匹配会报错：

```powershell
.\cgr.ps1 rebuild
```

## 五、目录结构与版本控制

```
code-graph-rag-chinese/
├─ bootstrap.ps1              # 一键部署（本文档的主角）
├─ cgr.ps1                    # 日常运维：start/stop/sync/doctor/logs
├─ setup_mcp.ps1              # MCP 服务进程管理（被 cgr.ps1 调用）
├─ start_cgr_env.ps1          # 加载 .env + 激活 venv 后执行 cgr
├─ repos.example.json         # 仓库别名表模板（repos.json 不入库）
├─ .env.example               # 配置模板（.env 不入库）
├─ pyproject.toml / uv.lock   # 依赖声明与锁定
├─ codebase_rag/              # 主代码
├─ scripts/                   # 辅助脚本（模型下载、torch 安装等）
└─ cgr-data/                  # 【不入库】运行时数据
   ├─ db/                     #   图谱 + 向量库
   ├─ models/bge-m3/          #   embedding 模型
   └─ mcp/                    #   MCP 服务登记表与日志
```

**不入版本控制的内容**（见 `.gitignore`）：

| 路径 | 大小 | 原因 |
|---|---|---|
| `.venv/` | 4.8 GB | 平台相关，`bootstrap.ps1` 重建 |
| `cgr-data/` | 2.5 GB | 数据与模型，可重建 |
| `rust/cgr_graph_rs/target/` | 208 MB | Rust 构建产物 |
| `.env` | — | 含本机绝对路径 |
| `repos.json` | — | 含本机绝对路径 |
| `mcp.json` | — | 由 `setup_mcp.ps1` 生成 |

> 仓库体积因此从 ~7.5 GB 降到 ~66 MB。

---

## 六、启动 MCP 服务

```powershell
.\cgr.ps1 start              # 启动默认仓库的 MCP 服务
.\cgr.ps1 status             # 查看状态 + 图谱统计
.\cgr.ps1 sync               # 增量同步代码变更
.\cgr.ps1 rebuild            # 全量重建（换模型/图谱异常时）
.\cgr.ps1 doctor             # 环境自检
.\cgr.ps1 logs mes -Tail     # 跟踪日志
```

启动后会向目标仓库写入 `.mcp.json`（HTTP 传输，端口 8700-8799 按路径哈希分配）。
在目标仓库里打开 Claude Code，首次会提示是否信任，选允许。

### 其他客户端（Claude Desktop / Cursor / Cline）

这些客户端用 stdio 传输，配置写到各自的 `mcpServers` 里。**路径必须是绝对路径**
（客户端的工作目录不一定是项目目录，`.env` 不会被自动读取）：

> 下面示例中的 `F:/code-graph-rag-chinese` 是**本机安装路径**，请替换成你的实际路径。
> 运行 `.\cgr.ps1 start` 会自动生成并维护这份配置，通常不需要手写。

```json
{
  "mcpServers": {
    "code-graph-rag": {
      "command": "F:\\code-graph-rag-chinese\\.venv\\Scripts\\cgr.exe",
      "args": ["mcp-server"],
      "env": {
        "SQLITE_GRAPH_PATH": "F:/code-graph-rag-chinese/cgr-data/db/mes-graph.sqlite",
        "SQLITE_VECTOR_PATH": "F:/code-graph-rag-chinese/cgr-data/db/mes-vectors.sqlite",
        "CGR_EMBEDDING_MODEL": "F:/code-graph-rag-chinese/cgr-data/models/bge-m3",
        "SQLITE_VECTOR_DIM": "1024",
        "HF_HUB_OFFLINE": "1"
      }
    }
  }
}
```

> `CGR_EMBEDDING_DEVICE` 不用写，留空自动探测。

### 多仓库

用工作区，避免反复切 `--repo-path`：

```powershell
cgr workspace create myworkspace --repos D:\code\repo-a D:\code\repo-b
cgr mcp-server --workspace myworkspace
```

---

## 七、日常维护

### 代码变了，更新索引

```powershell
.\cgr.ps1 sync               # 推荐：增量更新，几秒
```

或在 Claude Code 里让 AI 调用 MCP 的 `update_repository` 工具。

只改了几个文件时，`reingest` 更快（毫秒级，只重解析指定文件）。

### 环境坏了

```powershell
.\bootstrap.ps1              # 幂等重跑，只补缺失的部分
```

### 换机器 / 换目录

1. 复制仓库（`git clone` 或直接拷贝，**不含** `cgr-data`、`.venv`）
2. 跑 `.\bootstrap.ps1 -RepoPath <新路径>`
3. 如果沿用旧的 `cgr-data`，把 `.env` 里的绝对路径改掉，或删掉 `.env` 让脚本重新生成

### 换显卡

```powershell
.\scripts\install_gpu_torch.ps1 -DryRun     # 看会自动选哪个 CUDA 版本
.\scripts\install_gpu_torch.ps1             # 自动识别并安装
.\scripts\install_gpu_torch.ps1 -Cu cu128   # 或手动指定
```

---

## 八、故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `uv pip install` 卡在 github.com | `tree-sitter-c/cpp` 指向 GitHub commit | 已在命令中带 `--no-sources`，用 PyPI 版本 |
| 模型下载超时 | 无法访问 HuggingFace | 脚本自动回退 ModelScope / hf-mirror；都不行就手工下载 |
| 建图很慢（15 分钟以上） | 正在用 CPU 推理 | 正常。需要加速见 [三、CPU 与 GPU](#三cpu-与-gpu) |
| `torch.cuda.is_available()` 为 False | 装的是 PyPI 的 CPU 版（这是默认） | 需要 GPU 时跑 `.\scripts\install_gpu_torch.ps1` |
| GPU 装了但没生效 | 驱动过旧 / CUDA 版本不匹配 | `nvidia-smi` 看驱动；`.\cgr.ps1 doctor` 看检测结果 |
| `cgr context` 返回 `resolved: null` | 没带 `--repo-path`，或项目没建图 | 检查 `.\cgr.ps1 status` |
| MCP 连接失败 | 服务没起 / 端口被占 | `.\cgr.ps1 status`，必要时 `.\cgr.ps1 restart` |
| PowerShell 中文乱码 | 脚本编码 | 本仓库脚本均为 UTF-8 **带 BOM**，勿用编辑器另存为无 BOM |
| `cgr.ps1 sync` 中途退出 | 已修复（stderr 触发终止错误） | 确认用的是本仓库最新版 `cgr.ps1` |
| 重装 torch 报 `os error 5 拒绝访问` | MCP 服务正占用 `.pyd` 文件 | 先 `.\cgr.ps1 stop`；`install_gpu_torch.ps1` 会检测并提示 |

### 详细排查

**`ModuleNotFoundError: No module named 'httpx'`**
依赖解析器装了 `httpx2`（另一个发行包），而代码 import 的是 `httpx`。
`pyproject.toml` 已显式声明 `httpx>=0.27`，重跑依赖安装即可。

**`Failed to generate semantic embeddings: ... c10.dll`（WinError 1114）**
McAfee 等安全软件自带的旧版 `MSVCP140.dll` 先被加载，torch 的新版运行库无法初始化。
已在 `codebase_rag/_win_runtime.py` 修复（CLI 启动时先绑定 System32 的 MSVC 运行库）。
换别的安全软件后又出现，把它的目录从 PATH 移除。

**向量检索结果很差 / 全是随机**
检查模型是否真的加载了权重。启动日志出现下面这行说明权重没加载，向量等于噪声：

```
... declares auto_map (custom modelling code) but was loaded without trust_remote_code
```

本项目用 `BAAI/bge-m3`，transformers 原生架构，不会有这个问题。

**显存不足（CUDA out of memory）**
代码已做自动减半重试（128 → 64 → … → 1），不会中断。想主动降低占用：

```ini
CGR_EMBEDDING_BATCH_SIZE=64
CGR_EMBEDDING_MAX_LENGTH=256
```

**`cgr status` 显示 `stack: stopped`**
那是可选的 Memgraph + Qdrant 容器栈。本方案用嵌入式 SQLite，**不需要启动**，忽略即可。

### 性能：慢在哪

实测（RTX A4000 16G，3375 个 Java 文件 / 12264 条向量）：

| 阶段 | 耗时 |
|---|---|
| `import torch` + transformers | 14.1 s |
| 加载 bge-m3 权重到显存（2.2 GB） | 5.4 s |
| 单次 embed（热） | 0.05 s |
| sqlite-vec 检索 12264 条 | 0.076 s |
| **一次 `cgr context` 冷启动合计** | **27.5 s** |
| **MCP 常驻后单次工具调用** | **0.01 ~ 0.2 s** |

结论：90% 的时间花在**进程启动 + 模型加载**，不是检索。
**常驻 MCP 服务是最大的优化**，比换任何模型都管用（27.5s → 0.2s）。
换小模型只能把 5.4s 降到 1s 左右，冷启动仍是 20s 量级。

### 已知的可选项

`cgr doctor` 会报两项 FAIL：

```
FAIL  Orchestrator model not reachable (ollama:llama3.2)
FAIL  Cypher model not reachable (ollama:llama3.2)
```

这是**可选的 LLM 后端**，只有 `ask_agent` 这类自然语言问答工具需要。
检索类工具（`context` / `semantic_search` / `callers` 等）完全不受影响。
不需要可以忽略；需要的话安装 [Ollama](https://ollama.com/) 并 `ollama pull llama3.2`，
或在 `.env` 里配置 `ORCHESTRATOR_PROVIDER` / `CYPHER_PROVIDER`。

---

## 九、依赖版本说明

| 场景 | 命令 | 结果 |
|---|---|---|
| 全新安装（默认 CPU） | `uv sync --frozen --extra semantic --extra treesitter-full` | torch 2.15.0+cpu |
| 已装 GPU 后补依赖 | `uv pip install --no-sources -e ".[semantic,treesitter-full]"` | 保留 GPU 版 torch |
| 已装 GPU 后误跑 `uv sync --frozen` | — | ⚠️ 会被换回 CPU 版，需重跑 `install_gpu_torch.ps1` |

`uv.lock` 锁定的是 PyPI 的 CPU 轮子，这是**有意为之**：
保证任意机器 clone 后一条命令即可运行，不依赖显卡和驱动。
GPU 是可选增强，通过 `scripts/install_gpu_torch.ps1` 覆盖。

