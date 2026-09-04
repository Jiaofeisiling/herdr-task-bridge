<div align="center">
  <img src="assets/logo.webp" alt="herdr-task-bridge logo" width="180">
</div>

# NeSI Sentinel Bridge (herdr-task-bridge)

[![tests](https://github.com/Jiaofeisiling/herdr-task-bridge/actions/workflows/tests.yml/badge.svg)](https://github.com/Jiaofeisiling/herdr-task-bridge/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Windows ↔ NeSI Herdr Sentinel 桥接服务。让 Windows 上的 Codex/Claude 会话可以把任务委派给 NeSI 上一个持久运行的 Claude Code 会话（"Sentinel"），并发保护、异步任务队列、自动恢复都已内建。

## 架构

```
Windows PowerShell (sentinel.ps1)
        │ HTTP
        ▼
VS Code Remote-SSH 端口转发 (本地 127.0.0.1:8765 → 远程同端口)
        │
        ▼
bridge.py (跑在远程 NeSI 可达主机上，ThreadingHTTPServer)
        │ herdr CLI
        ▼
Persistent Claude Sentinel (Herdr 管理的终端会话)
```

`bridge.py` 内部有两条相互独立又共享同一把锁（`AGENT_LOCK`）的执行路径：

- **同步路径**：`/ask`、`/prompt` 直接持锁执行，HTTP 连接一直开到任务完成。
- **异步路径**：`/delegate` 立即返回 `task_id`，任务写入本地 SQLite（`tasks.db`），由一个后台 worker 线程按顺序取出、持锁执行。

两条路径永远不会同时对 Sentinel 发起委派——`AGENT_LOCK` 是唯一的收敛点。`/health` 是唯一不接触 Sentinel、不查数据库的接口，用来单独判断"bridge 进程是否存活"。

## 文件结构

```
.
├── sentinel.ps1                    Windows 端 CLI 入口
├── sentinel-bridge\
│   ├── bridge.py                    本地工作副本，与远程部署的版本保持同步
│   ├── test_bridge.py               pytest 套件（70 个用例，全部 mock 掉 herdr）
│   └── .venv\                       项目专用虚拟环境（miniforge3 的 conda 环境对非管理员只读，装不了包）
└── docs\superpowers\plans\
    └── 2026-08-30-sentinel-bridge-v2.2-v2.3.md   完整实施计划（18 个任务的详细设计与代码）
```

远程主机上的实际运行位置是 `~/sentinel-bridge/bridge.py`（当前是 `~/sentinel-bridge` 目录，数据库在 `~/sentinel-bridge/tasks.db`）。

## 快速开始

在仓库根目录下执行：

```powershell
.\sentinel.ps1 health
.\sentinel.ps1 ready

$id = (.\sentinel.ps1 delegate "检查当前 NeSI 项目的 Git 状态，不要修改任何文件" | ConvertFrom-Json).task_id
.\sentinel.ps1 wait $id
```

## 命令参考

| 命令 | HTTP | 端点 | 说明 |
|---|---|---|---|
| `health` | GET | `/health` | bridge 进程是否存活；**不接触** herdr/Sentinel/数据库，任务再忙也秒回 |
| `ready` | GET | `/ready` | Sentinel 当前能不能接新任务（`agent_status` 为 `idle`/`done` 才算 ready） |
| `status` | GET | `/status` | 原始 `herdr agent get` 结果 |
| `read` | GET | `/read` | 读最近 120 行终端输出 |
| `delegate <任务描述>` | POST | `/delegate` | **异步**提交任务，立即返回 `task_id`，不等待完成；默认超时 6 小时 |
| `task <task_id>` | GET | `/tasks/<id>` | 查询异步任务当前状态（`queued`/`running`/`done`/`error`/`orphaned`） |
| `wait <task_id>` | GET | `/tasks/<id>`（轮询） | 每 3 秒轮询一次直到任务到达终态，只输出干净的 `result_text`/`error_text`，不用手动反复调用 `task` |
| `tasks` | GET | `/tasks` | 列出最近 20 条任务 |
| `ask <任务描述>` | POST | `/ask` | **同步**提交任务，等待完成后一次性返回结果；Sentinel 忙时返回 `409` |
| `prompt <任务描述>` | POST | `/prompt` | 同步发送 prompt，但不读取/提取结果（比 `ask` 少一步） |

通用参数：`-TimeoutMs <毫秒>`（`ask`/`prompt` 默认 120000，`delegate` 不传时让服务端 6 小时默认值生效）、`-Lines <行数>`（`read` 相关，默认 80）。

## 异步任务生命周期

```
queued → running → done
              ↓        ↑ (missing marker，自动补发一次恢复提示)
           error / orphaned
```

- `orphaned`：Sentinel 可能还在继续执行，只是 bridge 等不到完成信号了（例如 `herdr` 调用超时）——**不代表任务失败**，只是状态未知。
- `error`：任务确认执行失败（herdr 命令本身出错、恢复提示也没等到结果等）。
- bridge 进程重启时，任何还停留在 `running` 的任务会被自动标记为 `orphaned`，绝不会自动重跑（避免危险操作被无意中重复执行）。

## 鉴权（可选，默认关闭）

默认没有任何鉴权——任何能连到 `127.0.0.1:8765` 的本地进程都能提交任务。要开启：

1. 远程：设置环境变量后重启 `bridge.py`
   ```bash
   export SENTINEL_BRIDGE_TOKEN="一个随机字符串（仅 ASCII，非 ASCII 会导致鉴权异常）"
   python3 ~/sentinel-bridge/bridge.py
   ```
2. Windows：在跑 `sentinel.ps1` 的会话里设置同样的值
   ```powershell
   $env:SENTINEL_BRIDGE_TOKEN = "同一个随机字符串"
   ```

未设置时 bridge 启动会打印一条明显的警告提示当前无鉴权；密钥含非 ASCII 字符时也会在启动时打印警告（`hmac.compare_digest` 不支持非 ASCII 字符串比较，那种密钥下的每个请求都会 fail-closed 返回 401）。`/health` 永远不需要鉴权（用于纯存活探测）。

## 环境变量（bridge.py，远程侧）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SENTINEL_BRIDGE_PORT` | `8765` | 监听端口 |
| `HERDR_BIN` | `herdr` | herdr 可执行文件路径 |
| `SENTINEL_AGENT` | `sentinel` | Herdr 里的 agent 名字 |
| `SENTINEL_DB` | `~/sentinel-bridge/tasks.db` | 任务队列数据库路径 |
| `SENTINEL_BRIDGE_TOKEN` | 空 | 鉴权密钥，留空即不启用鉴权 |
| `SENTINEL_MAX_QUEUE_DEPTH` | `50` | `/delegate` 队列里同时允许多少个 `queued` 任务，超过返回 `429` |

`timeout_ms`（`/ask`、`/prompt`、`/delegate` 均适用）必须落在 `[1000, 21600000]`（1 秒 ~ 6 小时）区间内，否则返回 `400`。

## 部署（更新远程 bridge.py）

本地没有到远程主机的 SSH 通道，每次改动都需要手动同步：

1. 把仓库里 `sentinel-bridge/bridge.py` 的内容复制到远程 `~/sentinel-bridge/bridge.py`（VS Code Remote-SSH 文件浏览器 / 集成终端 / `scp` 均可）。
2. 停掉旧进程，重新执行 `python3 ~/sentinel-bridge/bridge.py`。
3. 从 Windows 验证：
   ```bash
   curl -sS http://127.0.0.1:8765/health
   ```
   核对 `"version"` 字段确认跑的是新代码（每次改动 `/health` 的响应结构时记得手动同步这个数字）。

## 测试

```bash
cd sentinel-bridge
.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

79 个用例，全部通过 monkeypatch 模拟 `run_herdr`/`get_agent_status`，不需要真实 herdr 或网络。

```powershell
Invoke-Pester -Path .\sentinel.Tests.ps1 -Output Detailed
```

9 个用例，把 `sentinel.ps1` 当子进程启动、指向一个用 `System.Net.HttpListener` 搭的本地假 bridge（通过 `SENTINEL_BRIDGE_URL` 环境变量指定地址，默认仍是 `http://127.0.0.1:8765`）。之所以不直接 dot-source 脚本做进程内测试，是因为 `sentinel.ps1` 好几个分支会调用 `exit`——dot-source 进测试进程会直接把整个测试进程杀掉；子进程 + 真实 HTTP 往返和 `test_bridge.py` 的 `live_server` fixture 是同一个思路。需要本机装了 Pester（`Install-Module -Name Pester -Scope CurrentUser`）。

CI（`.github/workflows/tests.yml`）在每次 push/PR 到 `master` 时自动跑这两套测试。

## 设计文档

完整设计文档（含每一步的具体代码、测试和历次代码审查记录）见 [docs/superpowers/plans/2026-08-30-sentinel-bridge-v2.2-v2.3.md](docs/superpowers/plans/2026-08-30-sentinel-bridge-v2.2-v2.3.md)。
