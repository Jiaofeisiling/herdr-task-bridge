<div align="center">
  <img src="assets/logo.webp" alt="herdr-task-bridge logo" width="180">
</div>

# Herdr Task Bridge

[![tests](https://github.com/Jiaofeisiling/herdr-task-bridge/actions/workflows/tests.yml/badge.svg)](https://github.com/Jiaofeisiling/herdr-task-bridge/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) | 简体中文

`herdr-task-bridge` 让 Windows PowerShell 客户端能够把任务委派给一台可达 Linux 主机上持久运行的 Herdr agent 会话。它提供同步请求、可持久化的异步队列、按 agent 隔离的互斥执行，以及可恢复的任务状态查询。

本项目维护并验证的参考部署是 **Windows ↔ NeSI ↔ Herdr agents**。但 bridge 本身并不依赖 NeSI：只要部署者提供一台可达的 Linux 主机、Python 3、`herdr` CLI，以及合适的私有网络或端口转发路径，它也可以部署在其他 HPC 系统或租赁 Linux 服务器上。

> [!IMPORTANT]
> 本仓库是任务桥接层，不是集群运维产品。SSH、端口转发、调度器、鉴权、存储、配额与 agent 权限等站点配置，仍由各部署者自行负责。委派任何任务前，请先在自己的安全策略与数据治理边界内完成验证。

## 功能概览

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

结果目录必须同时可被 bridge 进程与所选 agent 写入。请只授予这一个目录的窄写入权限；不要为了收集结果而削弱 agent 的整体审批策略。

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
