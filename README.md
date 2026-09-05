<div align="center">
  <img src="assets/logo.webp" alt="herdr-task-bridge logo" width="180">
</div>

# NeSI Sentinel Bridge (herdr-task-bridge)

[![tests](https://github.com/Jiaofeisiling/herdr-task-bridge/actions/workflows/tests.yml/badge.svg)](https://github.com/Jiaofeisiling/herdr-task-bridge/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Windows ↔ NeSI Herdr Sentinel 桥接服务。让 Windows 上的 Codex/Claude 会话可以把任务委派给 NeSI 上任意一个持久运行的 Herdr agent 会话（不止一个"Sentinel"——同一台主机上可以有多个，用 `agent` 参数挑），并发保护、异步任务队列、自动恢复都已内建。

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

`bridge.py` 内部有两条执行路径：

- **同步路径**：`/ask`、`/prompt` 直接持锁执行，HTTP 连接一直开到任务完成。
- **异步路径**：`/delegate` 立即返回 `task_id`，任务写入本地 SQLite（`tasks.db`），由一个后台 worker 线程按顺序取出、持锁执行。

**每个 agent 一把独立的锁**（`get_agent_lock(agent_name)`），不是一把全局锁——同步请求可以同时使用不同 agent，同一个 agent 的两条路径则永远不会同时向它发起委派。worker 按创建时间取异步任务时，如果最老任务的 agent 已经忙碌，会跳过去寻找另一个空闲 agent 的任务。异步队列目前仍由**一个 worker 串行执行**：一旦它开始等待某个 agent 的任务完成，在该任务结束前不会启动另一个异步任务；v4 提供的是多 agent 路由和互斥隔离，不承诺异步任务跨 agent 并行。`/health` 是唯一不接触 Sentinel/herdr、不查数据库的接口，用来单独判断"bridge 进程是否存活"。

## 文件结构

```
.
├── sentinel.ps1                    Windows 端 CLI 入口
├── sentinel.profile.ps1            让 sentinel.ps1 可以在任意目录下当 `sentinel` 用（见"部署"）
├── sentinel.Tests.ps1              Pester 套件（15 个用例）
├── remote\
│   ├── bridge-aliases.sh           远程重启/部署流程的别名（见"部署"）
│   └── bridge-supervisor.sh        崩溃自动重启循环，screen 里跑这个而不是裸 bridge.py
├── sentinel-bridge\
│   ├── bridge.py                    本地工作副本，与远程部署的版本保持同步
│   ├── test_bridge.py               pytest 套件（99 个用例，全部 mock 掉 herdr）
│   └── .venv\                       项目专用虚拟环境（miniforge3 的 conda 环境对非管理员只读，装不了包）
└── docs\superpowers\plans\
    └── 2026-08-30-sentinel-bridge-v2.2-v2.3.md   完整实施计划（18 个任务的详细设计与代码）
```

远程主机上代码实际跑在 `~/herdr-task-bridge/sentinel-bridge/bridge.py`（本仓库的 git clone，见下方"部署"）；数据库仍在 `~/sentinel-bridge/tasks.db`——这是 `SENTINEL_DB` 固定基于 `$HOME` 的默认值，跟代码部署在哪个目录无关，历史上先有这个数据库目录，代码搬到 git 管理的位置后就没再动它。

## 快速开始

在仓库根目录下执行：

```powershell
.\sentinel.ps1 health
.\sentinel.ps1 ready

$id = (.\sentinel.ps1 delegate "检查当前 NeSI 项目的 Git 状态，不要修改任何文件" | ConvertFrom-Json).task_id
.\sentinel.ps1 wait $id
```

主机上不止一个 agent 时，先看看谁在、谁空闲，再指定目标：

```powershell
.\sentinel.ps1 agents
.\sentinel.ps1 ask -Agent "sentinel-opencode" "check disk usage"
```

## 命令参考

| 命令 | HTTP | 端点 | 说明 |
|---|---|---|---|
| `health` | GET | `/health` | bridge 进程是否存活；**不接触** herdr/Sentinel/数据库，任务再忙也秒回 |
| `agents` | GET | `/agents` | 列出这台主机上 herdr 管理的所有 agent 及各自的 `agent_status`（`herdr agent list` 的结构化封装） |
| `ready` | GET | `/ready` | 指定 agent 当前能不能接新任务（`agent_status` 为 `idle`/`done` 才算 ready） |
| `status` | GET | `/status` | 原始 `herdr agent get` 结果 |
| `read` | GET | `/read` | 读最近 120 行终端输出 |
| `delegate <任务描述>` | POST | `/delegate` | **异步**提交任务，立即返回 `task_id`，不等待完成；默认超时 6 小时 |
| `task <task_id>` | GET | `/tasks/<id>` | 查询异步任务当前状态（`queued`/`running`/`done`/`error`/`orphaned`） |
| `wait <task_id>` | GET | `/tasks/<id>`（轮询） | 每 3 秒轮询一次直到任务到达终态，只输出干净的 `result_text`/`error_text`，不用手动反复调用 `task` |
| `tasks` | GET | `/tasks` | 列出最近 20 条任务 |
| `ask <任务描述>` | POST | `/ask` | **同步**提交任务，等待完成后一次性返回结果；对应 agent 忙时返回 `409` |
| `prompt <任务描述>` | POST | `/prompt` | 同步发送 prompt，但不读取/提取结果（比 `ask` 少一步） |

通用参数：`-Agent <名字>`（挑选目标 agent，不传就用 bridge 的 `SENTINEL_AGENT` 默认值；`ask`/`prompt`/`delegate` 写进请求体，`ready`/`status`/`read` 拼成 query parameter；先用 `agents` 看看这台主机上实际有哪些、谁空闲）、`-TimeoutMs <毫秒>`（`ask`/`prompt` 默认 120000，`delegate` 不传时让服务端 6 小时默认值生效）、`-Lines <行数>`（`read` 相关，默认 80，允许 1–5000）。agent 名称必须是非空字符串，首尾空白会被去除，最长 200 个字符。

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
   cd ~/herdr-task-bridge/sentinel-bridge && python3 bridge.py
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
| `SENTINEL_AGENT` | `sentinel` | 请求没指定 `agent` 时用哪一个——不是"唯一能用的 agent"，一台主机上可以有多个（`herdr agent list`/`GET /agents` 能看到全部） |
| `SENTINEL_DB` | `~/sentinel-bridge/tasks.db` | 任务队列数据库路径 |
| `SENTINEL_BRIDGE_TOKEN` | 空 | 鉴权密钥，留空即不启用鉴权 |
| `SENTINEL_MAX_QUEUE_DEPTH` | `50` | `/delegate` 队列里同时允许多少个 `queued` 任务，超过返回 `429` |

`timeout_ms`（`/ask`、`/prompt`、`/delegate` 均适用）必须落在 `[1000, 21600000]`（1 秒 ~ 6 小时）区间内，否则返回 `400`。

## 部署（更新远程 bridge.py）

远程主机上是本仓库的一个真正的 git clone（`~/herdr-task-bridge`），更新代码就是 `git pull`；重启是单独一步。

**bridge.py 由 [remote/bridge-supervisor.sh](remote/bridge-supervisor.sh) 这个重启循环拉起，跑在具名的 `screen` 会话里，不要直接把 `python3 bridge.py` 扔进 screen。** 这是两次真实事故换来的：

1. 一开始是裸 `&` 后台，进程从来没挂在任何 VS Code 终端标签上，等标签一关就找不到它了——`/health` 还能连上（进程没死，NeSI 登录节点 `KillUserProcesses=false`，单次 SSH 会话断开不会连带杀掉它），但没地方看输出、按 Ctrl+C。
2. 改成 `screen -dmS bridge bash -c '... python3 bridge.py'` 之后，某次 bridge.py 自己崩了——screen 默认行为是**直接子进程一退出，窗口就跟着关掉**，`screen -r bridge` 直接报 "screen is terminating"，会话彻底消失，而且当时没有日志文件，连它为什么崩的都查不出来。

`bridge-supervisor.sh` 用一个 `while true` 循环把 `python3 bridge.py` 包起来，每次它退出（不管崩溃还是被 kill）都记一行时间戳到 `sentinel-bridge/bridge.log`，等 5 秒后自动重新拉起。screen 的直接子进程变成这个循环本身，不再是 bridge.py，所以 bridge.py 崩多少次窗口都还在、都有记录。

### 命令别名（推荐）

命令太长记不住，[remote/bridge-aliases.sh](remote/bridge-aliases.sh) 把下面这套流程包成了几个函数。装一次：

```bash
echo 'source ~/herdr-task-bridge/remote/bridge-aliases.sh' >> ~/.bashrc
source ~/.bashrc
```

之后：

| 命令 | 作用 |
|---|---|
| `bridge-deploy` | `git pull` + 重启（日常改完代码后最常用的一条） |
| `bridge-pull` | 只同步代码，不重启 |
| `bridge-restart` | 只重启（kill 旧进程 + 起新的 screen 会话），不拉代码 |
| `bridge-status` | 看 screen 会话、进程、`/health`、最近 20 行日志是否正常 |
| `bridge-attach` | `screen -r bridge`，接上去看重启循环的实时输出/Ctrl+C |
| `bridge-logs` | `tail -f` 日志（`sentinel-bridge/bridge.log`），单独看不用接进 screen |

`remote/bridge-aliases.sh` 是这套重启流程唯一的权威定义，改动流程时改这个文件，不要只改下面的文字说明。

Windows 端同理，[sentinel.profile.ps1](sentinel.profile.ps1) 让 `sentinel.ps1` 可以在任何目录下直接当 `sentinel` 用：

```powershell
# 加进你的 $PROFILE（notepad $PROFILE 打开）：
. "E:\herdr-task-bridge\sentinel.profile.ps1"

# 之后：
sentinel health
sentinel ask "check disk usage"
```

### 手动步骤（`bridge-deploy` 内部做的事）

1. **同步代码**（不涉及重启，随便在哪个终端做都安全）：
   ```bash
   cd ~/herdr-task-bridge && git pull
   ```

2. **重启 bridge.py**（`bridge-restart` 做的就是这些）：
   ```bash
   screen -S bridge -X quit             # 干掉整个旧会话（重启循环 + 它当前拉起的 bridge.py）
   pkill -f '^python3 bridge\.py$'      # 防御性清理，怕有漏网的旧 bridge.py 不在 screen 里
   screen -dmS bridge bash ~/herdr-task-bridge/remote/bridge-supervisor.sh
   screen -ls                           # 确认出现 "xxxx.bridge (Detached)"
   ```
   `pkill -f` 的 pattern **必须**像这样加 `^...$` 锚点，不能只写 `pkill -f 'python3 bridge.py'`——这几条命令如果是被打包成 `bash -c "一长串命令"` 远程执行的（比如通过 Sentinel 代劳时就是这样），那个包装用的 `bash -c` 进程自己的命令行里就含有 `python3 bridge.py` 这几个字（因为 pkill 那条命令本身的文本就在里面），不加锚点会把执行这条指令的 shell 自己也匹配杀掉，后面的命令全部执行不到。这是实测踩过的坑，不是理论风险。加锚点后目标进程的命令行确实是精确的 `python3 bridge.py`，而包装用的 shell 命令行是 `bash -c ...`，锚点能把两者分开。

   可以在 NeSI 的终端里手动敲，也可以通过 `sentinel.ps1 ask`/`delegate` 让 Sentinel 代劳——但如果是让 Sentinel 执行"杀掉当前 bridge"这步，**触发这条指令的那次 HTTP 请求本身会因为连接被腰斩而报错**（bridge.py 杀掉自己进程的那一刻，正在处理这条指令的连接跟着断；后续命令不受影响，照样会执行完），这是实测会发生的正常现象，不代表部署失败，用下一步单独验证就行，不要看到这个报错就以为要回滚。
   之后需要看实时输出/交互，从任意终端 `screen -r bridge` 接上去（看到的是重启循环，不是 bridge.py 本身）；`Ctrl+A D` 分离，不会杀掉进程。只想看日志不想接进 screen 就用 `bridge-logs` / `tail -f sentinel-bridge/bridge.log`。

3. **验证**（从 Windows）：
   ```bash
   curl -sS http://127.0.0.1:8765/health
   curl -sS http://127.0.0.1:8765/ready
   ```
   `/health` 核对 `"version"` 字段确认跑的是新代码（每次改动 `/health` 的响应结构时记得手动同步这个数字）；`/ready` 确认 Sentinel 没被卡在 `working`。**本机连不连得上跟 bridge.py 或 screen 都无关**——完全取决于 VS Code Remote-SSH 有没有开着、有没有把 `127.0.0.1:8765` 转发过来；这边再稳，VS Code 一断，本机照样连不上。

数据库路径不受代码搬家影响——`SENTINEL_DB` 默认值是固定基于 `$HOME` 的 `~/sentinel-bridge/tasks.db`，跟 `bridge.py` 自己部署在哪个目录无关，所以切到 git 管理的新目录时不需要迁移任何数据。旧的裸文件部署目录（`~/sentinel-bridge/bridge.py`）已删除，那个目录现在只保留 `tasks.db`。

## 测试

```bash
cd sentinel-bridge
.venv/Scripts/python.exe -m pytest test_bridge.py -v
```

99 个用例，全部通过 monkeypatch 模拟 `run_herdr`/`get_agent_status`，不需要真实 herdr 或网络。

```powershell
Invoke-Pester -Path .\sentinel.Tests.ps1 -Output Detailed
```

15 个用例，把 `sentinel.ps1` 当子进程启动、指向一个用 `System.Net.HttpListener` 搭的本地假 bridge（通过 `SENTINEL_BRIDGE_URL` 环境变量指定地址，默认仍是 `http://127.0.0.1:8765`）。之所以不直接 dot-source 脚本做进程内测试，是因为 `sentinel.ps1` 好几个分支会调用 `exit`——dot-source 进测试进程会直接把整个测试进程杀掉；子进程 + 真实 HTTP 往返和 `test_bridge.py` 的 `live_server` fixture 是同一个思路。需要本机装了 Pester 5.6.1（`Install-Module -Name Pester -RequiredVersion 5.6.1 -Scope CurrentUser`）。

CI（`.github/workflows/tests.yml`）在每次 push/PR 到 `master` 时自动跑 pytest，并分别用 Windows PowerShell 5.1（`powershell`）和 PowerShell Core（`pwsh`）跑 Pester。

## 设计文档

完整设计文档（含每一步的具体代码、测试和历次代码审查记录）见 [docs/superpowers/plans/2026-08-30-sentinel-bridge-v2.2-v2.3.md](docs/superpowers/plans/2026-08-30-sentinel-bridge-v2.2-v2.3.md)。
