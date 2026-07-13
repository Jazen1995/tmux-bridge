# AGENTS.md — tmux-bridge

始终使用简体中文沟通。

## 实现原则

- 长期正确优先：避免依赖终端画面、隐式状态或无法迁移的机器配置。
- 实现应简单、实用、可验证，不为尚未出现的需求引入抽象层。
- 先从原始需求和系统边界推导方案；发现前提错误时先纠正前提。
- `tmux-bridge` 是独立仓库，不修改或复用 `tmux-cc` 的运行状态与服务。

## 架构边界

- Codex App Server 是会话、消息和状态的唯一数据源。
- 飞书只能通过 App Server 原生协议提交消息和订阅事件。
- tmux 仅创建、刷新或关闭本地 Codex TUI；禁止使用 `capture-pane`、`send-keys` 或终端文本解析。
- 一个托管 tmux session 必须对应一个 Codex thread。
- thread 与飞书 chat 的绑定必须持久化，服务重启后自动恢复订阅。
- 项目只支持 Codex，不添加 Claude 兼容分支。
- 凭据只能存放在未跟踪的 `.env` 中，日志、测试和提交不得输出密钥。

## 模块

| 文件 | 职责 |
| --- | --- |
| `bot.py` | 飞书入口、配置和依赖装配 |
| `controller.py` | 命令路由、会话状态、任务队列和卡片生命周期 |
| `appserver.py` | Codex App Server 原生协议客户端 |
| `events.py` | 原生事件聚合与轮次展示模型 |
| `larkui.py` | 飞书 JSON 2.0 卡片构建与收发 |
| `tmux_ui.py` | tmux 和本地 Codex TUI 生命周期 |
| `state.py` | chat/thread 绑定持久化 |

## 卡片约定

- 顶层顺序固定为「本轮任务」「本轮输出」「执行记录」。
- 「本轮任务」和「本轮输出」默认展开。
- 「执行记录」默认收起，思考摘要、工具、命令和文件活动统一放在其中。
- 一轮只创建一张卡片，后续使用原生事件增量更新；完成后冻结。

## 质量要求

- 每次行为改动都必须补充或更新测试。
- 提交前运行 `.venv/bin/pytest -q` 和 `git diff --check`。
- App Server 协议或 tmux 协同改动还要运行标记为 `e2e` 的真实服务测试。
- 修改部署脚本后检查生成的 systemd unit，确保不含固定用户名或仓库路径。
- 修改已部署的服务代码后运行 `systemctl --user restart tmux-bridge`。
