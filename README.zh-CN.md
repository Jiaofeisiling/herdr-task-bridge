<div align="center">
  <img src="assets/logo.webp" alt="herdr-task-bridge logo" width="180">
</div>

# Herdr Task Bridge

[![tests](https://github.com/Jiaofeisiling/herdr-task-bridge/actions/workflows/tests.yml/badge.svg)](https://github.com/Jiaofeisiling/herdr-task-bridge/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) | 简体中文

`herdr-task-bridge` 是面向远程科研计算的执行网关。它让用户只需操作 Windows 上的 ChatGPT、Claude、Cursor 或其他智能开发工具，就能把需要 Linux/NeSI 环境的命令、验证和 Slurm 工作交给持久运行的 Herdr agent，不必亲自登录服务器处理日常操作。

当前 v4 已提供 Windows CLI、SSH 隧道后的 HTTP bridge、SQLite 异步任务队列、多 Herdr agent 路由、互斥保护与保守恢复。下面的“目标架构”还包括尚待实现的 Windows Gateway、事件流、主动通知和专用监控 worker；文档会明确区分现状与规划。

## 项目定位

本项目不是“远程 shell 的薄包装”，也不是让两个 AI 随意对话。它连接的是两个职责不同、能力互补的角色：

- **Windows 主模型（Primary Coding Model）**：用户选定的主要智能模型；当前典型实例是 Windows 上的 ChatGPT。它理解科研目标，负责架构、算法、绝大部分代码、跨文件重构、代码审查与 PR。
- **Linux Herdr Agent（Remote Execution Engineer）**：远程执行工程师。它在真实 Linux/NeSI 环境中运行命令、诊断环境问题、做测试与最小修复、按授权提交或监控 Slurm，并返回结构化证据；它不是默认的主要代码作者。
- **用户（Owner / Approver）**：定义目标、预算和权限边界，选择主模型，并对昂贵、破坏性、不可逆或科学含义不明确的动作作最终决定。
- **Bridge / Gateway（Control Plane）**：可靠传递执行合同、保存任务与事件、恢复连接、去重、路由和通知；它不替代主模型作科研决策，也不替代 Herdr agent 执行 Linux 工作。

同一个 workflow 在同一时刻只设一个 Primary Coding Model。ChatGPT、Claude、Cursor 都可以作为入口或适配器，但不能在没有交接和分支隔离的情况下同时修改同一工作区。

### 代码所有权边界

Linux Herdr Agent 可以自主编写更适合在 Linux 环境中完成的内容，例如 Bash/Slurm 脚本、module/conda/CUDA 环境胶水、诊断脚本和为通过真实环境验证所需的最小局部修复。以下内容默认交还 Windows 主模型：核心算法、模型结构、数据划分、评估协议、公共 API、跨模块重构和大部分业务代码。

每个执行合同应明确选择一种 coding policy：

| 模式 | Linux Herdr Agent 的代码权限 |
|---|---|
| `no_code_changes` | 只读检查与执行，不修改代码 |
| `environment_and_minimal_fix` | 默认模式；允许 Linux 专属胶水和为验证所需的最小修复 |
| `scoped_development` | 仅在明确文件/分支/验收标准内承担一段开发工作 |

无论使用哪种模式，都遵守单写者原则：Windows 主模型与 Linux Herdr Agent 不同时修改同一文件；确需并行时使用独立 Git branch/worktree，并通过 commit/PR 交接。

## 目标架构

[![herdr-task-bridge 目标架构图](docs/diagrams/research-execution-architecture.png)](docs/diagrams/research-execution-architecture.html)

> 点击图片打开可缩放、可切换主题和导出的交互式架构图。

目标架构把控制面和执行面分开：主模型生成执行合同，Gateway/Bridge 负责可靠传输与状态，Herdr agent 负责真实环境执行，Monitor Worker 独立追踪长任务。监控不应长期占用执行 agent。

## 端到端科研工作流

[![远程科研执行工作流图](docs/diagrams/research-execution-workflow.png)](docs/diagrams/research-execution-workflow.html)

> 点击图片打开可缩放、可切换主题和导出的交互式工作流图。

建议的执行合同至少包含：

```yaml
objective: 要完成的科研或工程目标
project: 项目标识
workdir: Linux 上的明确工作目录
expected_git_commit: 预期基线 commit
allowed_actions: 允许读取、修改、安装、提交或取消的动作
coding_policy: no_code_changes | environment_and_minimal_fix | scoped_development
slurm_policy: 是否只 dry-run、允许 TEST_ONLY、是否允许唯一正式提交
acceptance: 可机器检查的验收条件
reporting: 需要返回的日志、产物、指标、限制和证据
```

## 状态与证据边界

系统必须分别报告以下层级，不能用一个 `done` 混为一谈：

1. Bridge 服务是否可达。
2. Herdr agent 是 `idle`、`working`、`done` 还是异常。
3. Bridge `task_id` 是 `queued`、`running`、`done`、`error` 还是 `orphaned`。
4. Slurm `job_id` 是排队、运行、完成、失败还是取消。
5. 日志、checkpoint、表格等 artifacts 是否存在且完整。
6. 指标、样本数、配置和评估协议是否足以支持科研结论。

因此：**bridge task `done` 不等于 Slurm 作业完成，Slurm `COMPLETED` 也不等于科研结果有效。** `orphaned` 只表示 bridge 已失去可靠跟踪，远端动作可能仍在继续；必须先检查实际影响，绝不能盲目重试。

主动报告应采用持久事件流，而不是让 Windows 端无限轮询一整段终端文本。目标设计是远端以事务方式写入 `task_events`，Windows Gateway 使用游标长轮询或订阅；状态不变时保持安静，在 `needs_input`、`error`、`orphaned`、任务完成或出现新 artifact 时通知用户。建议的关联标识为：

```text
workflow_id → task_id → command_run_id → slurm_job_id → artifact_id
                                      ↘ event_seq
```

## 何时可以称“主要开发已完成”

目前还不能这样宣称。下面是从 v4 到主要开发完成的验收清单；只有所有“主开发阻塞项”完成，并在真实 NeSI 环境通过端到端验证后，才进入以维护和扩展为主的阶段。

### 已有基础

- [x] Windows PowerShell CLI 与 HTTP bridge。
- [x] SQLite 持久任务、异步委派、重启后保守标记 `orphaned`。
- [x] 多 Herdr agent 发现、指定路由和逐 agent 互斥。
- [x] 同步/异步执行、队列深度限制、超时和基础 token 鉴权。
- [x] bridge supervisor、部署别名、pytest/Pester/CI 基线。
- [x] 角色、代码所有权、目标架构、工作流和证据边界文档。
- [x] **可靠的结果提取**：agent 通过逐任务的结果文件返回结果，不再从终端文本里刮，backend 的终端渲染方式（Claude Code vs OpenCode）不再决定结果能否被读出来。

### 主开发阻塞项

- [ ] **Workflow 与执行合同**：增加 `workflow_id`、执行合同 schema、来源 client、权限/coding policy、幂等键与关联 ID。
- [ ] **持久事件流**：实现事务性 `task_events`、单调 `event_seq`、cursor/long-poll API，以及重连后不漏报、不重报。
- [ ] **Windows Gateway**：把共享客户端、SSH 隧道生命周期、重连、订阅和 ChatGPT/Claude/Cursor 适配从单次 CLI 中抽出。
- [ ] **主动通知**：仅在完成、失败、需授权、`orphaned` 或有关键新证据时通知；支持去重、静默未变化状态和终态自动停止。
- [ ] **独立监控 worker**：分别跟踪 bridge task、Herdr agent、Slurm job 和 artifacts，且不长期占用执行 agent。
- [ ] **Slurm 安全门控**：内建静态检查 → dry-run → `TEST_ONLY` → 明确授权后唯一正式提交；记录 job ID，禁止不明状态下自动重投。
- [ ] **结构化远端报告**：支持 progress、`needs_input`、artifact、metric、warning 和 final report，而不只依赖终端文本抽取。
- [ ] **受控并发与写入隔离**：按 agent 并发执行异步任务，并对同项目写操作实施 single-writer 或 branch/worktree 隔离与显式交接。
- [ ] **安全默认值**：生产部署 token 默认开启，增加 secret 管理、命令/目录 allowlist、任务级权限和审计记录。
- [ ] **可复现证据包**：最终报告固定包含 commit、workdir、命令、环境、job/task ID、日志/产物路径、指标配置、样本数及“可说/不可说”。
- [ ] **真实端到端验收**：覆盖正常执行、bridge 重启、隧道中断、重复请求、超时/`orphaned`、授权暂停、Slurm 成败和通知恢复。
- [ ] **发布收口**：安装/升级/卸载文档、兼容性说明、迁移脚本和一个经过 NeSI 实测的稳定 release/tag。

### 不阻塞主要开发完成的后续扩展

- Web Dashboard、移动端通知和更多 UI。
- Slurm 之外的调度器或云计算后端。
- 大文件传输、artifact 在线预览和长期实验追踪平台集成。
- 更多主模型适配器和跨主机联邦调度。


## 当前 v4 实现

```
Windows PowerShell 客户端（sentinel.ps1）
        │ 通过私有连接或 SSH 端口转发发起 HTTP 请求
        ▼
Linux 主机上的 bridge.py（ThreadingHTTPServer + SQLite）
        │ herdr CLI
        ▼
持久运行、由 Herdr 管理的 agent 会话
```

服务提供两种执行方式：

- **同步**：`/ask` 和 `/prompt` 会持有目标 agent 的锁，并保持 HTTP 请求直到 agent 完成。
- **异步**：`/delegate` 将任务写入 SQLite 后立即返回 `task_id`。后台 worker 执行排队任务，状态可通过 `/tasks/<id>` 查询。

锁是**按 agent 分开的**，因此投递给不同 agent 的同步工作彼此隔离。异步 worker 本身有意保持单线程；当前版本不承诺异步队列能跨 agent 并行执行。`/health` 不依赖 Herdr 或 SQLite，所以即使 agent 或任务繁忙，也能只回答 bridge 进程是否存活。

## 快速开始

以下示例假定 Windows 已经可以通过私有连接访问远程 bridge（例如 VS Code Remote-SSH 已将远程端口转发到本机 `127.0.0.1:8765`）。在仓库根目录执行：

```powershell
.\sentinel.ps1 health
.\sentinel.ps1 ready

$id = (.\sentinel.ps1 delegate "总结当前目录；不要修改文件" | ConvertFrom-Json).task_id
.\sentinel.ps1 wait $id
```

若主机上有多个 Herdr agent，先查看它们，再明确选择目标：

```powershell
.\sentinel.ps1 agents
.\sentinel.ps1 ask -Agent "your-agent-name" "check disk usage"
```

## 命令参考

| 命令 | HTTP | 端点 | 用途 |
|---|---|---|---|
| `health` | GET | `/health` | 仅检查进程存活；不访问 Herdr 或 SQLite。 |
| `agents` | GET | `/agents` | 列出 Herdr 管理的 agent 及其状态。 |
| `ready` | GET | `/ready` | 检查所选 agent 是否可接收任务（`idle` 或 `done`）。 |
| `status` | GET | `/status` | 返回原始 `herdr agent get` 响应。 |
| `read` | GET | `/read` | 读取最近的 agent 终端输出，供诊断使用。 |
| `quota` | GET | `/quota` | 列出因模型提供商额度或余额错误而被临时阻断的 agent。 |
| `quota-reset` | POST | `/quota/reset` | 使用 `-Agent` 清除一个 agent 的额度熔断；刻意不传时清除全部熔断。 |
| `delegate <task>` | POST | `/delegate` | 将任务加入队列并返回 `task_id`；服务端默认超时为 6 小时。 |
| `task <task_id>` | GET | `/tasks/<id>` | 查询任务状态：`queued`、`running`、`done`、`error`、`orphaned` 或 `quota_exhausted`。 |
| `wait <task_id>` | GET | `/tasks/<id>` | 每 3 秒轮询至终态，再输出结果或错误。 |
| `tasks` | GET | `/tasks` | 列出最近 20 个任务。 |
| `ask <task>` | POST | `/ask` | 同步执行并返回 agent 结果；目标 agent 忙时返回 `409`。 |
| `prompt <task>` | POST | `/prompt` | 同步发送任务，但不提取结果。 |

通用客户端参数为 `-Agent <name>`、`-TimeoutMs <milliseconds>`（用于 `ask`、`prompt` 和 `delegate`）以及 `-Lines <count>`（用于 `read`）。agent 名称会去除首尾空白，不能为空，最长 200 个字符。请求超时必须在 1 秒至 6 小时之间。

## 任务生命周期与结果返回

```
queued → running → done
              ↓        ↑（一次结果文件提醒）
    error / orphaned / quota_exhausted
```

- `orphaned` 表示 bridge 没有等到任务完成信号，**不等于**远程任务一定失败。不要直接重试；应先检查该任务是否已经产生实际影响。
- `error` 表示 bridge 已确认执行或结果收集失败。
- `quota_exhausted` 表示所有合资格备用 agent 也被额度熔断或报告了额度/余额失败；任务到此为止，不会继续重试。
- bridge 重启时，所有仍为 `running` 的任务会被标记为 `orphaned`，绝不会被自动重跑。

agent 会把结果写入 `SENTINEL_RESULT_DIR` 下的一任务一文件；bridge 读出后会删除该文件。这样可避免从终端抓取文本所带来的截断、界面噪声以及对特定 agent 终端格式的依赖。agent 完成却没有写出结果文件时，bridge 只会补发一次“写入结果文件”的窄范围提醒；仍失败才报为 `error`，并保留终端输出用于诊断。

结果目录必须同时可被 bridge 进程与所选 agent 写入。请把它放在 **agent 自己的工作目录之下**（`herdr agent list` 或 `GET /agents` 会给出每个 agent 的 `cwd`）。默认值落在系统临时目录，位于该 `cwd` 之外，agent 的权限系统（OpenCode 的 `external_directory` 规则、Claude Code 的 auto-mode 分类器）会把它判为外部写入，可能在工作已经做完之后才卡住任务。放在 `cwd` 之内，它就只是一次普通的项目内写入。不要为了收集结果而削弱 agent 的整体审批策略——该移动的是目录。

### 用 AI 会话驱动 bridge

[`docs/CLIENT_PROMPT.zh-CN.md`](docs/CLIENT_PROMPT.zh-CN.md) 是给调用方 AI 会话用的现成 system prompt：接口、任务状态、何时用 `/ask` 何时用 `/delegate`，以及那些光看 API 无法发现的运维约定。它刻意不写死任何集群配置——分区名、QoS 限额和配额应由执行端 agent 在运行当下查明，而不该由一个看不见那台机器的调用方去断言。

### 主动额度故障转移

bridge 会识别常见的提供商信号，包括 Claude 的 5 小时/周限额、HTTP `429`、OpenCode API `402`、API credit 或余额不足。识别后，它会为失败 agent 持久化一条额度熔断记录，跳过原本会发送的“写入结果文件”提醒，并主动尝试合资格的备用 agent。

自动发现备用 agent 时，候选必须属于**不同的运行时家族**（例如 Claude ↔ OpenCode），避免只是切换到可能共享同一已耗尽账户的会话。如果部署中有已知的独立备用账户，请设置有序白名单 `SENTINEL_QUOTA_FAILOVER_AGENTS`。备用 agent 暂时不可用时，异步任务会保留在队列中稍后再尝试；只有所有合资格备用项都额度耗尽时，任务才会变为 `quota_exhausted`。

在提供商限额重置或账户充值后，应先在 bridge 外部确认账户可用，再主动清除熔断：

```powershell
sentinel quota
sentinel quota-reset -Agent "your-agent-name"
```

不要仅仅因为 agent 显示 `idle` 就清除熔断：模型提供商额度未恢复时，agent 仍然可能显示为空闲。

## 配置与安全

远程服务读取以下环境变量：

| 变量 | 默认值 | 含义 |
|---|---|---|
| `SENTINEL_BRIDGE_PORT` | `8765` | 监听端口。 |
| `HERDR_BIN` | `herdr` | Herdr 可执行文件路径。 |
| `SENTINEL_AGENT` | `sentinel` | 未指定时的默认目标 agent。 |
| `SENTINEL_DB` | `~/sentinel-bridge/tasks.db` | SQLite 任务队列路径。 |
| `SENTINEL_RESULT_DIR` | 系统临时目录中的 `sentinel-bridge-results` | agent 结果文件共用目录。 |
| `SENTINEL_RESULT_RETENTION_DAYS` | `7` | 启动清扫删除未被取走结果文件的年龄阈值；`0` 表示关闭。 |
| `SENTINEL_BRIDGE_TOKEN` | 未设置 | 可选的共享密钥鉴权令牌。 |
| `SENTINEL_MAX_QUEUE_DEPTH` | `50` | 异步队列允许的最大排队任务数。 |
| `SENTINEL_QUOTA_FAILOVER_AGENTS` | 未设置 | 额度失败后的逗号分隔、有序备用 agent 白名单。 |

将 [`remote/bridge.env.example`](remote/bridge.env.example) 复制为未追踪的 `remote/bridge.env`，再填写部署专用配置。绝不要提交真实主机名、项目标识、路径、用户名、任务提示词或令牌；项目的敏感信息规则见 [CONTRIBUTING.md](CONTRIBUTING.md)。

只有在 Linux 服务端和 Windows 客户端都设置了 `SENTINEL_BRIDGE_TOKEN` 后，鉴权才会启用。请使用高熵 ASCII 令牌，同时保护网络路径。为了简单存活探测，`/health` 会刻意保持无鉴权。

## 参考部署：NeSI

维护中的参考部署使用一台 NeSI 可达 Linux 主机上的 Git clone、`screen` 会话，以及 [`remote/bridge-supervisor.sh`](remote/bridge-supervisor.sh) 的重启循环。[`remote/bridge-aliases.sh`](remote/bridge-aliases.sh) 中的辅助函数是参考更新流程的权威实现：

```bash
echo 'source ~/herdr-task-bridge/remote/bridge-aliases.sh' >> ~/.bashrc
source ~/.bashrc

bridge-deploy    # 拉取并重启
bridge-status    # 检查 screen、进程、health 与日志
```

supervisor 会保留 `screen` 会话，并将重启日志写到 `sentinel-bridge/bridge.log`；不要在 `screen` 中直接运行 `bridge.py`。迁移到其他 HPC 或 Linux 服务器时，只需要按本站情况调整部署细节：clone 位置、服务守护方式、私有连接、环境文件和 agent 权限。bridge 的 API 与安全约定不变。

Windows 端可选地在 PowerShell profile 中加载 [`sentinel.profile.ps1`](sentinel.profile.ps1)，以便在任何目录直接使用 `sentinel`：

```powershell
. "E:\herdr-task-bridge\sentinel.profile.ps1"
sentinel health
sentinel ask "check disk usage"
```

## 开发与验证

远程运行时只使用 Python 标准库；测试依赖仅用于本地开发：

```bash
cd sentinel-bridge
python -m venv .venv
.venv/Scripts/python.exe -m pip install pytest  # Windows
.venv/bin/python -m pip install pytest          # Linux/macOS
.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

Python 测试套件含 111 个 mock 测试，不需要真实的 Herdr 安装或网络。Windows 客户端还有一个 16 用例的 Pester 套件，通过本地 HTTP stub 执行：

```powershell
Install-Module -Name Pester -RequiredVersion 5.6.1 -Scope CurrentUser  # 首次需要
Invoke-Pester -Path .\sentinel.Tests.ps1 -Output Detailed
```

CI 会运行 Python 测试，并在 Windows PowerShell 5.1 和 PowerShell Core 下运行 Pester 套件。单元测试不能取代在你自己的受控环境中做一次范围清晰的真实部署验证。

## 贡献与设计历史

提交 Pull Request 前，请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，尤其是防止机构或研究数据进入 Git 历史的规则。完整的设计与实现历史见 [docs/superpowers/plans/2026-08-30-sentinel-bridge-v2.2-v2.3.md](docs/superpowers/plans/2026-08-30-sentinel-bridge-v2.2-v2.3.md)。

本项目采用 [MIT License](LICENSE)。
