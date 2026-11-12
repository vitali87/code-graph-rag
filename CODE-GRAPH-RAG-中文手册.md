# Code-Graph-RAG 中文手册

> 本手册面向 **Windows + 本地 SQLite + 本地 GPU** 的部署方式，把项目原理、模型作用、MCP 能力、命令速查、文档地图整合在一起。
> 安装与运维见同目录 `DEPLOY.md`；本手册侧重「为什么」和「怎么用」。
>
> **日常操作只需要 `.\cgr.ps1`**（start / stop / status / sync / rebuild / doctor / logs），详见 `DEPLOY.md`。
>
> 最后更新：2026-11-10，对应 code-graph-rag 0.5.6（中文定制分支）

---

## 一、这是什么

Code-Graph-RAG（命令 `cgr`）做一件事：**把代码库变成一张可查询的图，再让 AI 基于这张图回答问题、改代码。**

```
源码  --tree-sitter-->  AST  --解析-->  知识图谱(SQLite)  <--查询--  Cypher
                                              ^
                                              |  向量索引（语义检索）
                                          embedding 模型（本地 GPU）
```

它和「把代码切片丢进向量库」的 RAG 有本质区别：

| | 普通代码 RAG | Code-Graph-RAG |
|---|---|---|
| 检索单位 | 文本块（chunk） | 函数/类/方法/模块 + 它们之间的关系 |
| 关系 | 无 | 调用、继承、实现、导入、覆写、数据流 |
| 能否回答「谁调用了这个方法」 | 靠猜 | 确定性的图查询，0 幻觉 |
| 能否安全改名 | 不能 | 能，改完所有调用点并校验语法 |

图谱里存的节点：项目、模块、包、类、接口、函数、方法、文件、测试、端点（HTTP 路由）。
边：`CALLS`、`IMPORTS`、`INHERITS`、`IMPLEMENTS`、`OVERRIDES`、`INSTANTIATES`、`REFERENCES`、`DEFINES`、`FLOWS_TO`（数据流）等。

默认存储是**嵌入式 SQLite 文件**，不需要 Docker、不需要 Memgraph/Qdrant 服务。

---

## 二、三类「模型」的分工（重点）

很多人会混淆。这个项目里同时存在三种模型，各管各的：

| 模型 | 跑在哪 | 作用 | 不配会怎样 |
|---|---|---|---|
| **Embedding 模型** | 本机 GPU（本项目：`BAAI/bge-m3`） | 把代码片段和你的问题变成向量，用于「按意思找代码」 | `semantic_search`、`cgr context "中文描述"` 不可用 |
| **Orchestrator LLM** | 远程 API 或本地 Ollama | 和用户对话、决定调哪个工具、组织答案 | `cgr start` 交互模式不可用 |
| **Cypher LLM** | 远程 API 或本地 Ollama | 把自然语言翻译成 Cypher 查询 | `query_code_graph` 不可用 |

### 2.1 Embedding 模型（你唯一需要本地显卡的那个）

- **只管一件事**：语义检索。即「用中文描述一个功能，找出相关函数」。
- **不管**：解析代码、建图、调用关系、改名、死代码分析——这些是确定性的，**完全不经过模型**。
- 当前配置：

```ini
CGR_EMBEDDING_MODEL=F:/code-graph-rag-chinese/cgr-data/models/bge-m3
SQLITE_VECTOR_DIM=1024
CGR_EMBEDDING_DEVICE=cuda
```

为什么选 `BAAI/bge-m3`（1024 维）：

- 你的代码是 Java（英文标识符），提问是中文 —— 这是**跨语言检索**，需要中英文在同一个向量空间对齐。bge-m3 正是为此训练的。
- 8192 token 上下文，能覆盖较长的方法。
- transformers 原生架构，不需要 `trust_remote_code`（jina-embeddings-v2 系列在 transformers 5 上加载不了，权重会变成随机值）。
- 实测：`提交订单的时候做了什么` 和 `where is the warehouse notified` 能命中**不同**的函数，说明对齐有效。

**显存占用**：bge-m3 是 568M 参数，fp32 约 2.3 GB，A4000 16G 绰绰有余。

### 2.2 Orchestrator / Cypher LLM

这两个只在 **`cgr start` 交互模式**和 **`ask_agent` / `query_code_graph` 工具**里用。配置示例：

```ini
# 用本地 Ollama（需要自己装 Ollama 并 pull 模型）
ORCHESTRATOR_PROVIDER=ollama
ORCHESTRATOR_MODEL=qwen2.5-coder
ORCHESTRATOR_ENDPOINT=http://localhost:11434/v1

# 或用 OpenAI / Google / Anthropic / MiniMax
# ORCHESTRATOR_PROVIDER=openai
# ORCHESTRATOR_MODEL=gpt-5.6-terra
# ORCHESTRATOR_API_KEY=sk-...
```

> `PROVIDER` 和 `MODEL` 必须成对出现，只写一个会直接报错退出。

### 2.3 关键认知：走 MCP 给 AI 用时，你不需要配 LLM

如果你只是 `cgr mcp-server` 然后接到 Claude Code / Cursor / Cline 里：

- **对话用的 LLM 是客户端自带的**（Claude、GPT、通义……），不需要在 cgr 里再配一遍。
- cgr 只负责「提供工具」。
- 所以 `.env` 里 `ORCHESTRATOR_*` / `CYPHER_*` **可以完全不填**。
- 但 `CGR_EMBEDDING_*` 必须配好，否则 `semantic_search` 和 `context` 的模糊匹配会退化。

---

## 三、本机当前状态

| 项 | 值 |
|---|---|
| 项目路径 | `F:\code-graph-rag-chinese` |
| Python | 3.12.10（`.venv`） |
| torch | 2.14.1+cu126，CUDA 可用 |
| GPU | RTX A4000 16GB |
| Embedding | `BAAI/bge-m3`，1024 维，cuda:0 |
| 图存储 | `cgr-data\db\mes-graph.sqlite` |
| 向量存储 | `cgr-data\db\mes-vectors.sqlite` |
| 后端 | 全 SQLite，无 Docker |

---

## 四、MCP 提供的能力

`cgr mcp-server` 把这些能力暴露给 AI 客户端。分四类：

### 4.1 确定性查询（不调用 LLM，快且零幻觉）

| 工具 | 作用 |
|---|---|
| `resolve` | 名字/位置 → 限定名。支持 `OrderService.submit`、`path:line` |
| `definition` | 查定义：文件、行号、装饰器、docstring、源码 |
| `callers` | 谁调用了它（`depth>1` 递归） |
| `callees` | 它调用了谁 |
| `implementors` | 谁实现了这个接口/继承了这个类 |
| `overrides` | 方法覆写关系（双向） |
| `importers` | 谁导入了这个模块（可穿透 re-export） |
| `tests_reaching` | 改这个符号要跑哪些测试 |
| `endpoints` | 项目暴露的 HTTP 路由/RPC 端点 |
| `endpoint_callers` | 谁调用了某个端点（跨项目） |
| `remote_dependencies` | 项目对外发起的所有网络依赖 |
| `get_code_snippet` | 按限定名取源码 |
| `context` | **按 token 预算给最小上下文切片**（含调用方、被调方签名、测试、文档） |
| `glosses` | 查某个定义上的注释（Gloss） |

> 这些是这套系统最有价值的部分：AI 不用猜，直接查。

### 4.2 语义检索（用本地 GPU 模型）

| 工具 | 作用 |
|---|---|
| `semantic_search` | 用自然语言描述功能，返回相似函数 + 相似度 |
| `get_function_source` | 按语义检索结果的 node id 取源码 |

### 4.3 结构化搜索/改写（ast-grep）

| 工具 | 作用 |
|---|---|
| `structural_search` | 按 AST 模式搜索（不是正则） |
| `structural_replace` | 按 AST 模式批量改写（默认 dry-run） |
| `find_duplicate_code` | 找复制粘贴的函数（含改名、微调过的） |

### 4.4 索引与编辑

| 工具 | 作用 |
|---|---|
| `list_projects` / `delete_project` / `wipe_database` | 项目管理 |
| `index_repository` | 全量重建（会先清空该项目） |
| `update_repository` | 增量更新 |
| `reingest` | 改了哪几个文件就重解析哪几个（毫秒级） |
| `read_file` / `write_file` / `list_directory` | 文件读写 |
| `surgical_replace_code` | 精确替换一段代码（diff-match-patch） |
| `rename` | 全图改名（定义 + 所有引用 + 导入 + 覆写 + `__all__`） |
| `change_signature` | 改参数列表并重写所有调用点 |
| `annotate` | 给定义挂一条持久注释（不写进源码） |
| `flow_verdict` / `explain_traceback` / `rank_root_causes` | 数据流可达性 / 异常栈归因（需 `CGR_CAPTURE=io` 建图） |
| `query_code_graph` | 自然语言 → Cypher（需要配 Cypher LLM） |
| `ask_agent` | 完整 RAG 问答（需要配 Orchestrator LLM） |

### 4.5 只读模式

```powershell
cgr mcp-server --read-only      # 或 MCP_READ_ONLY=true
```

写类工具（`write_file`、`rename`、`index_repository`、`delete_project` 等）既不列出也不执行。
`read_file` 会拒绝 `.env`、私钥、`.git/config` 等敏感文件。

---

## 五、接上 AI Agent 之后怎么用才对

### 5.1 客户端配置

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
        "CGR_EMBEDDING_DEVICE": "cuda",
        "HF_HUB_OFFLINE": "1"
      }
    }
  }
}
```

> MCP 客户端的工作目录不一定是项目目录，`env` 里必须写**绝对路径**；`.env` 只有工作目录对得上才会被读到。

### 5.2 给 AI 的提示词建议

在项目的 `CLAUDE.md` / `.cursorrules` / 系统提示里加一段，效果差别很大：

```markdown
本项目接入了 code-graph-rag MCP。

- 问「谁调用了 X」「X 的定义」这类问题，优先用 callers / definition / resolve，
  不要用 grep。
- 改代码前先用 context 拿上下文，不要整文件读。
- 改完用 reingest 让图谱跟上，再看返回的 structural delta（悬空调用、参数不匹配）。
- 只有当你不知道确切名字时，才用 semantic_search 或 query_code_graph。
```

原因：确定性工具**不用 LLM、毫秒级、结果精确**；让 AI 优先用它们，比让它自由发挥准确得多。

### 5.3 在 CC Switch 里登记（可选，但推荐）

`claude mcp add` 已经把服务写进 `~/.claude.json`（user 作用域，所有项目可见），`claude mcp list` 显示 `Connected` 即可用。

如果你还用 CC Switch 管理多个 provider，建议在它的 MCP 页面里也登记一份，这样切换 provider、重装客户端时不会丢：

1. 打开 CC Switch → 左侧 **MCP**（或「MCP 服务器」）
2. **添加** → 类型选 **stdio**
3. 按下面填：

| 字段 | 值 |
|---|---|
| 名称 | `code-graph-rag` |
| 命令 | `F:\code-graph-rag-chinese\.venv\Scripts\cgr.exe` |
| 参数 | `mcp-server` |

环境变量（逐条添加）：

```
TARGET_REPO_PATH=F:\qdmes\mes-service
SQLITE_GRAPH_PATH=F:/code-graph-rag-chinese/cgr-data/db/mes-graph.sqlite
SQLITE_VECTOR_PATH=F:/code-graph-rag-chinese/cgr-data/db/mes-vectors.sqlite
CGR_HOME=F:/code-graph-rag-chinese/cgr-data
CGR_EMBEDDING_MODEL=F:/code-graph-rag-chinese/cgr-data/models/bge-m3
SQLITE_VECTOR_DIM=1024
CGR_EMBEDDING_DEVICE=cuda
HF_HUB_OFFLINE=1
```

4. 勾选 **Claude**（`enabled_claude`），保存

> ⚠️ `TARGET_REPO_PATH` 必须写。MCP 客户端启动子进程时工作目录不一定是项目目录，不写就会用 `.` 去解析，工具会拒绝或查错项目。

如果 CC Switch 的 MCP 同步会整体重写 `~/.claude.json`，以它里面的登记为准；两条都留着也不冲突，同名会以后写入的为准。

### 5.4 日常流程

```powershell
# 1. 代码变了，先增量同步（不是每次都要全量重建）
cgr start --repo-path F:\qdmes\mes-service --update-graph

# 2. 启动 MCP 服务（由客户端自动拉起，一般不用手动起）
cgr mcp-server
```

或者用 `cgr watch` 常驻，边改边同步。

---

## 六、模型怎么换

### 6.1 换 embedding 模型

三步，**一步都不能少**：

1. 改 `.env`：

```ini
CGR_EMBEDDING_MODEL=BAAI/bge-m3        # 模型 ID，或本地目录
SQLITE_VECTOR_DIM=1024                 # 必须等于模型输出维度
```

2. 重建索引（**必须**，否则新旧向量维度/空间不一致）：

```powershell
cgr start --repo-path F:\qdmes\mes-service --update-graph --clean -y
```

3. 验证：

```powershell
.\.venv\Scripts\python.exe -c "from codebase_rag.embedder import get_model; m=get_model(); print(m.device, m.get_embedding_dimension())"
```

常见模型的维度：

| 模型 | 维度 | 中英 | 说明 |
|---|---|---|---|
| `BAAI/bge-m3` | 1024 | ★★★★★ | 当前选择，中英+代码，8192 上下文 |
| `BAAI/bge-base-zh-v1.5` | 768 | ★★★★ | 中文强，512 上下文，快 |
| `Qwen/Qwen3-Embedding-0.6B` | 1024 | ★★★★★ | 更新，32k 上下文，检索效果略强 |
| `intfloat/multilingual-e5-base` | 768 | ★★★★ | 需要 `query:`/`passage:` 前缀，本项目未加前缀，效果打折 |

> ⚠️ 不要选需要 `trust_remote_code` 的模型（如 `jinaai/jina-embeddings-v2-*`）。
> transformers 5 加载不了它们的自定义建模代码，权重匹配失败后**不会报错**，只是悄悄用随机权重——检索结果看起来正常，实际全是噪声。本项目启动时若检测到这种情况会打 ERROR 日志。

### 6.2 换 LLM（只在用 `cgr start` 交互或 `ask_agent` 时需要）

改 `ORCHESTRATOR_*` / `CYPHER_*` 即可，不用重建索引。

---

## 七、安装与启动（简版）

```powershell
cd F:\code-graph-rag-chinese

# 1. 依赖
uv venv .venv
uv pip install --python .venv\Scripts\python.exe --no-sources -e ".[semantic,treesitter-full]"

# 2. GPU 版 torch（可选；默认 CPU 版已够用）
.\scripts\install_gpu_torch.ps1
# 或指定：.\scripts\install_gpu_torch.ps1 -Cu cu126 -Index aliyun

# 3. 模型
.\scripts\fetch_embedding_model.ps1

# 4. 配置 .env（bootstrap.ps1 会自动生成）

# 5. 验证
.\start_cgr_env.ps1
```

> 详细步骤、下载地址、依赖清单见 **DEPLOY.md**。

---

## 八、命令速查

所有命令都可以用 `.\start_cgr_env.ps1 <子命令>` 透传（会自动加载 `.env` + 激活 venv）。

### 建图

```powershell
cgr start --repo-path <路径> --update-graph           # 增量建图
cgr start --repo-path <路径> --update-graph --clean -y # 全量重建（会删所有项目！）
cgr watch --repo-path <路径>                          # 常驻监听
cgr index --repo-path <路径>                          # 导出离线 protobuf 索引
```

### 查询

```powershell
cgr start --repo-path <路径>              # 交互式问答（需要 LLM）
cgr context "OrderService.submit"         # 取上下文切片（不需要 LLM）
cgr context "工单管理"                    # 语义定位（需要 embedding 模型）
cgr graph callers OrderService.submit     # 图查询，JSON 输出
cgr graph callees / definition / implementors / importers / tests-reaching
```

### 巡检

```powershell
cgr stats                    # 节点/关系计数
cgr dead-code --repo-path .  # 死代码
cgr duplicates --repo-path . # 重复代码
cgr check --base HEAD~1      # 改动影响面
cgr doctor                   # 体检（配置、依赖、后端）
```

### 重构

```powershell
cgr rename OldName NewName --repo-path .
cgr change-signature "Service.run" "a: int, b: str" --repo-path .
cgr edits list               # 看 cgr 做过的编辑
cgr edits undo <id>          # 回滚
```

### 多仓库

```powershell
cgr workspace create mes --repos F:\qdmes\mes-service F:\qdmes\pms\pms_backend
cgr workspace list
cgr mcp-server --workspace mes
```

### MCP

```powershell
cgr mcp-server                                  # stdio
cgr mcp-server --transport http --port 8080     # HTTP
cgr mcp-server --read-only                      # 只读
```

---

## 九、配置速查

### 图与向量存储

| 变量 | 默认 | 说明 |
|---|---|---|
| `GRAPH_BACKEND` | `sqlite` | `sqlite` / `memgraph` / `neo4j` |
| `SQLITE_GRAPH_PATH` | `$CGR_HOME/graph.sqlite` | 图谱文件 |
| `CGR_VECTOR_STORE_BACKEND` | `sqlite` | `sqlite` / `qdrant` / `milvus` |
| `SQLITE_VECTOR_PATH` | `$CGR_HOME/vectors.sqlite` | 向量文件 |
| `SQLITE_VECTOR_DIM` | 1024 | **必须等于模型维度** |

### 向量化

| 变量 | 说明 |
|---|---|
| `CGR_EMBEDDING_PROVIDER` | `unixcoder`（本地）/ `openai` |
| `CGR_EMBEDDING_MODEL` | 模型 ID 或本地目录 |
| `CGR_EMBEDDING_DEVICE` | `cuda` / `cpu` / `mps`，不写自动探测 |
| `CGR_EMBEDDING_BATCH_SIZE` | 默认 64，显存够可调到 128~256 |
| `CGR_EMBEDDING_MAX_LENGTH` | 单条截断长度，默认 512 |
| `CGR_SKIP_EMBEDDINGS` | `true` 时建图但不生成向量 |

### LLM

| 变量 | 说明 |
|---|---|
| `ORCHESTRATOR_PROVIDER` / `_MODEL` / `_API_KEY` / `_ENDPOINT` | 对话模型 |
| `CYPHER_PROVIDER` / `_MODEL` / `_API_KEY` / `_ENDPOINT` | Cypher 生成模型 |

### 采集与安全

| 变量 | 说明 |
|---|---|
| `CGR_CAPTURE` | `io` 时额外采集数据流（`FLOWS_TO`），供 `flow_verdict` 等使用 |
| `MCP_READ_ONLY` | `true` 只读模式 |
| `CGR_ALLOW_SENSITIVE_READS` | 允许读 `.env` 等敏感文件 |
| `CGR_ALLOW_SENSITIVE_WRITES` | 允许改 `.env` 等敏感文件 |
| `HF_HUB_OFFLINE` | `1` 完全离线 |

---

## 十、项目文档地图

### 根目录

| 文件 | 内容 |
|---|---|
| `README.md` | 项目门面：定位、原理图、语言支持、安装方式 |
| `PYPI_README.md` | 发布到 PyPI 的说明页，功能点比 README 更细 |
| `DEPLOY.md` | **部署与运维手册**（安装、CPU/GPU、模型、排错） |
| `cgr_windows.md` | 早期的离线部署草稿（内网/离线场景） |
| `CODE-GRAPH-RAG-中文手册.md` | 本文件 |
| `CONTRIBUTING.md` | 贡献流程、代码规范、测试要求 |
| `GOVERNANCE.md` | 项目治理与决策方式 |
| `NEWS.md` | 版本变更历史 |
| `ACCESSIBILITY.md` | 无障碍相关说明 |
| `AGENTS.md` / `CLAUDE.md` | 给 AI 编码代理的项目约定 |

### `docs/getting-started/`

| 文件 | 内容 |
|---|---|
| `installation.md` | 安装方式（pip / uv / 源码）、可选 extras |
| `quickstart.md` | 5 分钟上手 |
| `configuration.md` | 全部环境变量、provider 组合、错误行为 |

### `docs/guide/`

| 文件 | 内容 |
|---|---|
| `cli-reference.md` | **所有命令的完整参数说明**（最常查） |
| `mcp-server.md` | **MCP 集成**：工具清单、只读模式、Claude Code 配置 |
| `interactive-querying.md` | 交互式问答怎么用 |
| `multi-project.md` | 多仓库/工作区管理 |
| `realtime-updates.md` | `cgr watch` 实时同步机制 |
| `dead-code.md` | 死代码分析原理 |
| `duplicates.md` | 重复代码检测（AST 指纹） |
| `dynamic-tracing.md` | 运行时调用追踪（`cgr trace`） |
| `graph-export.md` | 图谱导出格式 |
| `porting.md` | 跨语言移植辅助 |
| `code-optimization.md` | 代码优化会话 |
| `cpp-semantic-mode.md` | C++ 语义模式（libclang） |

### `docs/architecture/`

| 文件 | 内容 |
|---|---|
| `overview.md` | 架构总览 |
| `graph-schema.md` | **图谱 schema 全量定义**（51KB，节点/边/属性） |
| `data-flow-edges.md` | `FLOWS_TO` 数据流边怎么建 |
| `language-support.md` | **各语言能力矩阵**（Java 支持到什么程度看这里） |
| `language-frontends.md` | 编译器级前端（libclang / go-types / Roslyn / javac / Jedi） |
| `embedded-graph.md` | 嵌入式图存储（SQLite vs Memgraph vs Neo4j） |
| `security.md` | 安全模型：敏感文件、写保护、MCP 权限 |
| `rename.md` | 改名的解析规则与拒绝条件 |
| `change-signature.md` | 改签名的调用点重写规则 |
| `context-slice.md` | `context` 的排序与裁剪算法 |
| `edit-transactions.md` | 编辑事务与回滚 |
| `patchers.md` | 各类补丁器实现 |
| `structural-delta.md` | 编辑后的结构差异报告 |
| `postcondition-contract.md` | 后置条件契约 |

### `docs/advanced/`

| 文件 | 内容 |
|---|---|
| `adding-languages.md` | 新增语言支持 |
| `building-binaries.md` | 打包二进制 |
| `git-submodules.md` | 子模块处理 |
| `ignore-patterns.md` | 忽略规则语法（13KB） |
| `troubleshooting.md` | 常见故障 |

### `docs/sdk/`

| 文件 | 内容 |
|---|---|
| `overview.md` | 作为库调用 |
| `semantic-search.md` | 语义检索 API |
| `cypher-generator.md` | Cypher 生成器 |
| `graph-loader.md` | 图谱加载器 |

### 其他

| 文件 | 内容 |
|---|---|
| `docs/index.md` | 文档首页 |
| `docs/roadmap.md` | 路线图 |
| `docs/claude-code-setup.md` | Claude Code 专项配置 |
| `docs/contributing.md` | 贡献细节 |
| `docs/incident-response.md` | 事故响应流程 |
| `docs/reports/*` | 内部技术报告（基准测试、语言推荐、索引器 Rust 化方案、重写建议等），了解设计取舍时可读 |

---

## 十一、FAQ

### Q1：`cgr start --update-graph` 很慢，正常吗？

首次全量解析 + 向量化，3300 个 Java 文件大约 10 分钟（RTX A4000）。
之后用增量更新只处理改动的文件，通常几秒。想要更快的单文件更新用 MCP 的 `reingest`。

### Q2：`cgr context` 返回 `"resolved": null`，什么都没匹配到

三种可能：

1. **没带 `--repo-path`**（最常见）。`cgr context` 默认 `--repo-path .`，会用当前目录推导项目名。在 cgr 自己的源码目录里执行，就是在查 `code-graph-rag-chinese__xxx` 这个不存在的项目，自然什么都找不到。
   → 写成 `cgr context "工单管理" --repo-path F:\qdmes\mes-service`。
2. **向量库维度不一致**。日志里会有：
   `SQLite vector store ... has vector dimension 768, expected 1024`
   → 用 `--clean` 重建索引。
3. **这个项目还没建图**。先跑 `cgr start --repo-path <路径> --update-graph`。

### Q3：`--clean` 会不会把别的项目也删了？

会。`--clean` 清空的是**整个共享数据库**，不只是当前项目。多项目场景建议：

```powershell
# 换个新项目名重建，不删任何东西
cgr start --repo-path <路径> --project-name mes-service-v2 --update-graph
```

### Q4：能不能不建图直接用 MCP？

可以用 `semantic_search` 之外的确定性工具会返回空/拒绝。至少要先 `index_repository` 或 `cgr start --update-graph` 一次。

### Q5：AI 老是 grep 而不用图查询，怎么办？

在项目提示词里明确要求（见 5.2 节）。工具描述本身已经写了「知道确切名字时优先用 resolve/callers」，但客户端不一定遵守，写进项目约定最有效。

### Q6：改了代码，图谱会自己更新吗？

不会。要么 `cgr watch` 常驻，要么每次改完调 `reingest` / `update_repository`。
`write_file`、`surgical_replace_code`、`structural_replace`、`rename` 这些 MCP 写工具会在返回里附带 structural delta，但不会自动重建图谱（除非项目已索引，它们会做增量重解析）。

### Q7：Embedding 模型能跑在 CPU 上吗？

能，`.env` 里 `CGR_EMBEDDING_DEVICE=cpu`。但 bge-m3 在 CPU 上大约慢 20~30 倍，3300 个文件要按小时算。

### Q8：换机器怎么迁移？

需要迁移的三样：

1. `cgr-data\db\*.sqlite`（图谱 + 向量）
2. `cgr-data\models\bge-m3\`（模型）
3. `.env`

或者干脆在新机器上重建索引——3300 个文件 10 分钟，通常比搬 4GB 文件更省事。

### Q9：为什么不能用 jina-embeddings-v2-base-code？

它在 transformers 5 上加载不了自定义建模代码（`find_pruneable_heads_and_indices` 已被删除、`JinaBertConfig.is_decoder` 不再存在），权重匹配失败后**静默**变成随机初始化——检索看起来正常，实际全是噪声。同理避开所有需要 `trust_remote_code` 的老模型。

### Q10：McAfee 环境下 `c10.dll` 报 WinError 1114

已修复（`codebase_rag/_win_runtime.py`）。原因是 McAfee 自带的旧版 `MSVCP140.dll` 在 PATH 里被先加载。换其他安全软件时如果复现，把该软件目录从 PATH 移除即可。
