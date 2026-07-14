# tmux-bridge

让手机上的飞书和开发机里的 tmux，共同操作同一个 Codex 会话。

`tmux-bridge` 保留了 `tmux-cc` 简单直接的工作方式：一个 tmux session 对应一个
Codex，会话可以从电脑进入，也可以从飞书继续。不同之处是，飞书卡片不再抓取、解析
终端文本，而是直接消费 Codex App Server 的原生事件。

## 为什么这样设计

```text
飞书 Bot ───────────────┐
                       ├─ Codex App Server ─ thread
tmux 中的 Codex TUI ────┘
```

- Codex App Server 是会话、消息和任务状态的唯一数据源。
- tmux 只负责承载本地 Codex TUI，并保存对应的 thread id。
- 飞书和本地 TUI 连接同一个 thread，因此两端看到的是同一段原生上下文。
- 项目不使用 `capture-pane`、`send-keys`，也不解析终端画面。

这让本地协作体验保持不变，同时避免 TUI 文本变化导致卡片误判、重复或长期显示
`Working`。

## 卡片体验

每轮任务只创建一张卡片，随后用原生事件持续更新：

1. **本轮任务**：默认展开，保留本轮用户要求。
2. **本轮输出**：默认展开，优先展示 Codex 的当前回答和最终结论。
3. **执行记录**：默认收起，集中展示思考摘要、命令、工具调用和文件修改。

卡片底部展示运行状态和耗时。旧轮次完成后冻结，不会混入下一轮内容。

## 前置条件

- Linux 开发机，支持 systemd user service
- Python 3.11 或更高版本
- tmux
- 已安装并登录的 Codex CLI，且支持 `app-server` 和 `--remote`
- 一个启用了机器人的飞书自建应用

飞书应用需要使用长连接订阅消息事件，并具备接收消息、发送消息及更新卡片所需权限。
部署不需要公网 webhook。建议填写 `FEISHU_OWNER_ID`，把机器人限制为仅自己可操作。

## 安装

```bash
git clone git@code.byted.org:wangzhenjia/tmux-bridge.git
cd tmux-bridge

cp .env.example .env
chmod 600 .env
```

编辑 `.env`：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_OWNER_ID=ou_xxx
FEISHU_OWNER_UNION_ID=

WORK_DIR=/path/to/your/workspace
APP_SERVER_SOCKET=/home/you/.codex/tmux-bridge.sock
STATE_FILE=/home/you/.local/state/tmux-bridge/sessions.json
TMUX_UI_ENABLED=true
CODEX_BIN=codex
LOG_LEVEL=INFO
```

安装并启动服务：

```bash
./scripts/install-services.sh
systemctl --user status tmux-bridge-appserver tmux-bridge
```

安装脚本会根据当前 clone 目录和本机 Codex CLI 的绝对路径生成用户级 systemd 配置，
不会依赖某个固定用户名或安装目录。飞书凭据只保存在 Git 忽略的 `.env` 中。

查看日志：

```bash
journalctl --user -u tmux-bridge -f
journalctl --user -u tmux-bridge-appserver -f
```

## 使用

在飞书里发送：

- `tls`：列出开发机上所有真实 tmux session，回复数字快速连接
- `tn <名称>`：创建 tmux session、启动 Codex 并连接
- `tn <名称> -dir=<路径>`：在指定目录创建会话
- `ta <名称>`：连接已有的托管会话
- `tk <名称>`：关闭会话；托管会话会同时归档 Codex thread
- `td`：断开当前飞书绑定，不关闭 tmux 或 Codex
- `view`：查看最近三轮原生对话
- `ctx`：查看完整原生上下文
- `stop` / `esc` / `c`：中断当前任务
- `pwd` / `dir`：编号展示当前目录的子文件夹，回复数字快速进入并继续下钻
- `cd <路径>`：直接切换新会话使用的工作目录

数字只作用于最近一次 `tls` 或 `pwd` / `dir` 展示的列表；发送其他命令或任务后，旧列表
立即失效。命令可以省略 `/` 前缀。直接发送普通文本，就是向当前 Codex 会话提交任务。
项目只支持 Codex，不提供 Claude 兼容层。

在开发机进入同一会话：

```bash
tmux ls
tmux attach -t <会话名>
```

按 `Ctrl-b d` 退出 tmux attach，Codex 仍会在后台常驻。电脑和飞书任一端产生的原生
事件，都会同步到当前飞书卡片。

普通的、非 `tmux-bridge` 创建的 tmux session 也会出现在 `tls` 中；但它们没有 Codex
thread 元数据，因此不会伪装成可读取原生事件的协同会话。

## 测试

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest -q
```

真实 App Server 端到端测试：

```bash
RUN_CODEX_E2E=1 \
CODEX_E2E_SOCKET=/path/to/tmux-bridge.sock \
.venv/bin/pytest -q -m e2e
```

## 核心文件

| 文件 | 职责 |
| --- | --- |
| `bot.py` | 飞书长连接入口与依赖装配 |
| `controller.py` | 命令路由、会话绑定、任务队列与卡片同步 |
| `appserver.py` | Codex App Server 原生 WebSocket / JSON-RPC 客户端 |
| `events.py` | 原生事件归并与展示模型 |
| `larkui.py` | 飞书 JSON 2.0 卡片构建、发送与合并更新 |
| `tmux_ui.py` | tmux 生命周期和本地 Codex TUI 管理 |
| `state.py` | 飞书 chat 与 Codex thread 绑定持久化 |

## 安全边界

- `.env`、运行环境文件和状态文件均被 Git 忽略。
- 日志和测试不得输出 App Secret。
- Bot 所在开发机可以只保留内网访问；飞书通过官方长连接把事件送达进程。
- tmux 不是消息总线，也不是数据源；终端内容不会被上传或硬解析。
