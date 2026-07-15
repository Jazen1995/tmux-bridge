"""Application layer: commands, thread bindings, turn queue, and card sync."""

from __future__ import annotations

import logging
import os
import shlex
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Literal

from appserver import AppServerClient, AppServerError, RpcError
from events import TurnView, render_thread_history, turn_view_from_history
from larkui import CardUpdater
from state import StateStore
from tmux_ui import TmuxSession, TmuxUIManager

logger = logging.getLogger(__name__)


HELP_TEXT = """\
**指令列表：**（`/` 前缀可省略）

**会话管理：**
- `tls` — 列出所有 tmux 会话（回复数字快速连接）
- `ta <名称>` — 连接到指定会话
- `tn <名称>` — 在当前目录新建会话并启动 Codex
- `tnc <名称>` — `tn` 的兼容别名
- `tn <名称> -dir=<文件夹>` — 在指定目录新建会话
- `tk <名称>` — 关闭一个会话
- `td` — 断开当前会话

**目录导航：**
- `cd <路径>` — 切换工作目录（`..` 返回上层，空参数回到默认目录）
- `pwd` / `dir` — 查看并编号展示子文件夹（回复数字快速进入）

**查看与控制：**
- `view` — 查看最近三轮原生对话
- `ctx` — 查看完整原生上下文
- `stop` / `esc` — 中断当前任务
- `c` — 中断当前任务（兼容 Ctrl-C 指令）
- `help` — 显示帮助

直接发文字即可对话。操作方式与 tmux-cc 一致；卡片内容来自 Codex App Server 原生事件。"""

WELCOME_TEXT = """\
**欢迎使用 tmux-bridge！**

操作方式与 tmux-cc 一致：每个 tmux 会话运行一个 Codex，飞书和开发机协作同一会话。

1. `pwd` — 查看当前目录有哪些项目
2. `cd <项目名>` — 进入项目目录
3. `tn <会话名>` — 创建会话并启动 Codex
4. 直接打字发消息 — 和当前 Codex 对话
5. `tls` — 查看所有会话，回复数字快速切换

---

""" + HELP_TEXT


_BARE_COMMANDS = {
    "tls", "tn", "tnc", "tk", "ta", "td", "help", "view", "ctx",
    "esc", "stop", "cd", "pwd", "dir", "c",
}


@dataclass
class TurnRuntime:
    view: TurnView
    cards: dict[str, str] = field(default_factory=dict)


@dataclass
class Submission:
    chat_id: str
    thread_id: str
    thread_name: str
    text: str
    message_id: str | None


@dataclass(frozen=True)
class NumericSelection:
    kind: Literal["session", "directory"]
    values: tuple[TmuxSession | str, ...]


class BotController:
    def __init__(
        self,
        *,
        appserver: AppServerClient,
        messenger: Any,
        store: StateStore,
        tmux: TmuxUIManager,
        tmux_reconcile_interval: float = 10.0,
    ):
        self.appserver = appserver
        self.messenger = messenger
        self.store = store
        self.tmux = tmux
        self.tmux_reconcile_interval = tmux_reconcile_interval
        self.card_updater = CardUpdater(messenger.update_card)
        self._lock = threading.RLock()
        self._runtimes: dict[tuple[str, str], TurnRuntime] = {}
        self._active_turn: dict[str, str] = {}
        self._feishu_turns: set[tuple[str, str]] = set()
        self._drafts: dict[str, Submission] = {}
        self._queues: dict[str, deque[Submission]] = defaultdict(deque)
        self._numeric_selections: dict[str, NumericSelection] = {}
        self._closing = threading.Event()
        self._tmux_reconciler: threading.Thread | None = None
        appserver.add_notification_handler(self.on_appserver_event)

    def start(self) -> None:
        self.appserver.connect()
        restored: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for chat_id, value in self.store.all().items():
            if value.get("thread_id"):
                restored[value["thread_id"]].append((chat_id, value))
        for thread_id, bindings in restored.items():
            try:
                result = self.appserver.resume_thread(thread_id)
                thread = result.get("thread") or self.appserver.read_thread(thread_id)
                name = bindings[0][1].get("thread_name") or self._display_name(thread)
                cwd = thread.get("cwd") or bindings[0][1].get("cwd") or self.store.default_cwd
                for chat_id, _ in bindings:
                    self.store.update(chat_id, thread_name=name, cwd=cwd)
                self._ensure_local_session(name, thread_id, cwd)
                status = (thread.get("status") or {})
                if status.get("type") == "active":
                    history = self.appserver.read_thread(thread_id, include_turns=True)
                    self._hydrate_active_turn(
                        history,
                        name,
                        [chat_id for chat_id, _ in bindings],
                    )
            except AppServerError:
                logger.exception("Unable to restore thread %s", thread_id)
        if self.tmux_reconcile_interval > 0:
            self._tmux_reconciler = threading.Thread(
                target=self._tmux_reconcile_loop,
                name="tmux-reconciler",
                daemon=True,
            )
            self._tmux_reconciler.start()

    def close(self) -> None:
        self._closing.set()
        if self._tmux_reconciler:
            self._tmux_reconciler.join(timeout=2)
        self.card_updater.close()
        self.appserver.close()

    # Command handling ------------------------------------------------------

    def handle_message(self, chat_id: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        first = text.split()[0]
        if first in _BARE_COMMANDS:
            text = "/" + text

        try:
            if text.isdigit() and chat_id in self._numeric_selections:
                self._select_index(chat_id, int(text) - 1)
                return

            # A numbered menu only applies to the immediately following choice.
            # Any other message replaces it with normal command/task semantics.
            self._numeric_selections.pop(chat_id, None)

            if text == "/help":
                self._send_info(chat_id, "tmux-bridge 帮助", HELP_TEXT)
            elif text == "/tls":
                self._list_sessions(chat_id)
            elif text.startswith("/ta "):
                self._attach(chat_id, text[4:].strip())
            elif text == "/ta":
                self.messenger.send_text(chat_id, "用法: /ta <会话名>")
            elif text.startswith("/tn ") or text.startswith("/tnc "):
                offset = 5 if text.startswith("/tnc ") else 4
                self._new_thread(chat_id, text[offset:].strip())
            elif text in {"/tn", "/tnc"}:
                self.messenger.send_text(
                    chat_id,
                    "用法: /tn <会话名> [-dir=<文件夹>]，或 /tnc <会话名>",
                )
            elif text.startswith("/tk "):
                self._close_session(chat_id, text[4:].strip())
            elif text == "/tk":
                self.messenger.send_text(chat_id, "用法: /tk <会话名>")
            elif text == "/td":
                self._detach(chat_id)
            elif text == "/view":
                self._show_context(chat_id, limit=3, label="最近对话")
            elif text == "/ctx":
                self._show_context(chat_id, limit=100, label="完整上下文")
            elif text in {"/stop", "/esc"}:
                self._stop(chat_id, acknowledgement="已中断")
            elif text == "/c":
                self._stop(chat_id, acknowledgement="已发送中断请求")
            elif text in {"/pwd", "/dir"}:
                self._show_pwd(chat_id)
            elif text == "/cd" or text.startswith("/cd "):
                self._change_dir(chat_id, text[3:].strip())
            elif text.startswith("/t"):
                self.messenger.send_text(chat_id, f"未知命令: {text.split()[0]}\n发送 /help 查看帮助")
            else:
                self._submit(chat_id, text)
        except RpcError as exc:
            logger.warning("App Server RPC failed: %s", exc)
            self.messenger.send_text(chat_id, f"Codex 请求失败：{exc}")
        except AppServerError as exc:
            logger.warning("App Server unavailable: %s", exc)
            self.messenger.send_text(chat_id, f"Codex App Server 不可用：{exc}")
        except Exception:
            logger.exception("Message handling failed")
            self.messenger.send_text(chat_id, "处理消息时发生内部错误，请稍后重试")

    def _send_info(self, chat_id: str, title: str, content: str, template: str = "blue") -> None:
        self.messenger.send_card(
            chat_id,
            title=title,
            content=content,
            template=template,
        )

    def _list_sessions(self, chat_id: str) -> None:
        sessions = self.tmux.list_sessions()
        self._set_numeric_selection(chat_id, "session", sessions)
        content = self._format_session_list(chat_id, sessions)
        self._send_info(chat_id, "会话列表", content)

    def _set_numeric_selection(
        self,
        chat_id: str,
        kind: Literal["session", "directory"],
        values: list[TmuxSession] | list[str],
    ) -> None:
        if values:
            self._numeric_selections[chat_id] = NumericSelection(kind, tuple(values))
        else:
            self._numeric_selections.pop(chat_id, None)

    def _format_session_list(self, chat_id: str, sessions: list[TmuxSession]) -> str:
        if not sessions:
            return "没有活跃的 tmux 会话"
        current = self.store.get(chat_id).get("thread_id")
        lines = []
        for index, session in enumerate(sessions, 1):
            selected = "  *(current)*" if session.thread_id and session.thread_id == current else ""
            lines.append(f"**{index}.** {session.name}{selected}")
        lines.append("\n回复数字快速连接。")
        return "\n".join(lines)

    def _select_index(self, chat_id: str, index: int) -> None:
        selection = self._numeric_selections.get(chat_id)
        if selection is None:
            return
        if not (0 <= index < len(selection.values)):
            self.messenger.send_text(
                chat_id,
                f"无效数字，请输入 1-{len(selection.values)}",
            )
            return

        self._numeric_selections.pop(chat_id, None)
        selected = selection.values[index]
        if selection.kind == "directory":
            self.store.update(chat_id, cwd=str(selected))
            self._show_pwd(chat_id)
            return

        if not isinstance(selected, TmuxSession):
            raise TypeError("session selection must contain TmuxSession values")
        session = selected
        if not session.managed:
            self.messenger.send_text(
                chat_id,
                f"`{session.name}` 不是 tmux-bridge 创建的会话，没有原生事件可供飞书协同。",
            )
            return
        self._bind_thread(
            chat_id,
            self.appserver.read_thread(session.thread_id),
            session_name=session.name,
        )

    def _attach(self, chat_id: str, reference: str) -> None:
        if not reference:
            self.messenger.send_text(chat_id, "用法: /ta <会话名>")
            return
        session = next(
            (item for item in self.tmux.list_sessions() if item.name == reference),
            None,
        )
        if session is None:
            self.messenger.send_text(chat_id, f"会话 '{reference}' 不存在，发 /tls 查看列表")
            return
        if not session.managed:
            self.messenger.send_text(
                chat_id,
                f"`{session.name}` 不是 tmux-bridge 创建的会话，没有原生事件可供飞书协同。",
            )
            return
        self._bind_thread(
            chat_id,
            self.appserver.read_thread(session.thread_id),
            session_name=session.name,
        )

    def _bind_thread(
        self,
        chat_id: str,
        thread: dict[str, Any],
        *,
        session_name: str | None = None,
    ) -> None:
        result = self.appserver.resume_thread(thread["id"])
        resumed = result.get("thread") or thread
        combined = dict(thread)
        combined.update({key: value for key, value in resumed.items() if value is not None})
        name = session_name or self._display_name(combined)
        cwd = resumed.get("cwd") or thread.get("cwd") or self.store.get(chat_id)["cwd"]
        self.store.update(
            chat_id,
            thread_id=thread["id"],
            thread_name=name,
            cwd=cwd,
        )
        tmux_result = self._ensure_local_session(name, thread["id"], cwd)
        history = self.appserver.read_thread(thread["id"], include_turns=True)
        if self._hydrate_active_turn(
            history,
            name,
            [chat_id],
            create_new_cards=True,
        ):
            return
        footer = tmux_result.message or "Codex 原生会话"
        self.messenger.send_card(
            chat_id,
            title=name,
            content=render_thread_history(history, limit=3),
            subtitle=cwd,
            footer=footer,
            template="blue",
        )

    def _hydrate_active_turn(
        self,
        thread: dict[str, Any],
        thread_name: str,
        chat_ids: list[str],
        *,
        create_new_cards: bool = False,
    ) -> bool:
        """Attach subscribers to the current native turn without replaying it.

        Explicit session selection creates a new card at the current position in
        the chat. Startup recovery may keep updating an already known card.
        """
        active_turn = next(
            (
                turn
                for turn in reversed(thread.get("turns") or [])
                if turn.get("status") == "inProgress"
            ),
            None,
        )
        if active_turn is None:
            return False

        thread_id = thread["id"]
        view = turn_view_from_history(thread_id, active_turn, thread_name)
        key = (thread_id, view.turn_id)
        with self._lock:
            runtime = self._runtimes.get(key)
            if runtime is None:
                runtime = TurnRuntime(view)
                self._runtimes[key] = runtime
            else:
                view.merge_live(runtime.view)
                runtime.view = view
            self._active_turn[thread_id] = view.turn_id

            for chat_id in chat_ids:
                message_id = None if create_new_cards else runtime.cards.get(chat_id)
                if message_id:
                    self.card_updater.submit(message_id, **self._view_card(view))
                    continue
                message_id = self.messenger.send_card(chat_id, **self._view_card(view))
                if message_id:
                    runtime.cards[chat_id] = message_id
        return True

    def _new_thread(self, chat_id: str, arguments: str) -> None:
        parts = shlex.split(arguments)
        workdir = None
        names: list[str] = []
        for part in parts:
            if part.startswith("-dir="):
                workdir = part[5:]
            elif part.lower() in {"codex", "-codex", "--codex", "-agent=codex"}:
                continue
            elif part.lower() in {"claude", "-claude", "--claude", "-agent=claude"}:
                self.messenger.send_text(chat_id, "tmux-bridge 仅支持 Codex，不支持 Claude")
                return
            elif part.lower().startswith("-agent="):
                self.messenger.send_text(chat_id, "tmux-bridge 仅支持 `-agent=codex`")
                return
            else:
                names.append(part)
        name = " ".join(names).strip()
        if not name:
            self.messenger.send_text(
                chat_id,
                "用法: /tn <会话名> [-dir=<文件夹>]，或 /tnc <会话名>",
            )
            return
        if any(item.name == name for item in self.tmux.list_sessions()):
            self.messenger.send_text(chat_id, f"session '{name}' already exists")
            return

        state = self.store.get(chat_id)
        cwd = workdir or state["cwd"]
        if not os.path.isabs(cwd):
            cwd = os.path.abspath(os.path.join(state["cwd"], cwd))
        if not os.path.isdir(cwd):
            self.messenger.send_text(chat_id, f"目录不存在：{cwd}")
            return

        result = self.appserver.start_thread(cwd)
        thread = result["thread"]
        self.appserver.set_thread_name(thread["id"], name)
        thread["name"] = name
        self.store.update(chat_id, thread_id=thread["id"], thread_name=name, cwd=cwd)
        tmux_result = self._ensure_local_session(name, thread["id"], cwd)
        footer = tmux_result.message or "Codex 会话已创建"
        self.messenger.send_card(
            chat_id,
            title=name,
            content=(
                f"已创建 tmux 会话 **{name}**，Codex 已就绪。\n\n"
                f"开发机进入：`tmux attach -t {name}`\n\n"
                "直接发送消息即可开始任务。"
            ),
            subtitle=f"{result.get('model', 'Codex')} · {cwd}",
            footer=footer,
            template="green",
        )

    def _close_session(self, chat_id: str, name: str) -> None:
        session = next(
            (item for item in self.tmux.list_sessions() if item.name == name),
            None,
        )
        if session is None:
            self.messenger.send_text(chat_id, f"会话 '{name}' 不存在")
            return
        if session.managed:
            self.appserver.archive_thread(session.thread_id)
            self.tmux.close_session(session.thread_id)
            if self.store.get(chat_id).get("thread_id") == session.thread_id:
                self.store.unbind(chat_id)
        else:
            self.tmux.close_named_session(name)
        remaining = self.tmux.list_sessions()
        self._set_numeric_selection(chat_id, "session", remaining)
        self.messenger.send_card(
            chat_id,
            title="Tmux Sessions",
            content=f"已关闭: **{name}**\n\n{self._format_session_list(chat_id, remaining)}",
            template="blue",
        )

    def _detach(self, chat_id: str) -> None:
        state = self.store.get(chat_id)
        if state.get("thread_id"):
            name = state.get("thread_name") or state["thread_id"][:8]
            self.store.unbind(chat_id)
            self.messenger.send_text(chat_id, f"已断开: {name}")
        else:
            self._send_info(chat_id, "tmux-bridge", WELCOME_TEXT, "indigo")

    def _show_context(self, chat_id: str, *, limit: int, label: str) -> None:
        state = self.store.get(chat_id)
        if not state.get("thread_id"):
            self._send_info(chat_id, "tmux-bridge", WELCOME_TEXT, "indigo")
            return
        thread = self.appserver.read_thread(state["thread_id"], include_turns=True)
        self.messenger.send_card(
            chat_id,
            title=state.get("thread_name") or "Codex",
            content=render_thread_history(thread, limit=limit),
            subtitle=thread.get("cwd"),
            footer=f"{label} · 原生事件 · {len(thread.get('turns') or [])} 轮",
            template="blue",
        )

    def _show_pwd(self, chat_id: str) -> None:
        cwd = self.store.get(chat_id)["cwd"]
        try:
            dirs = sorted(
                item for item in os.listdir(cwd)
                if not item.startswith(".") and os.path.isdir(os.path.join(cwd, item))
            )
        except OSError:
            dirs = None
        if dirs is None:
            directory_list = "  (cannot read directory)"
        elif dirs:
            dirs = dirs[:80]
            directory_list = "\n".join(
                f"**{index}.** {item}/" for index, item in enumerate(dirs, 1)
            )
            self._set_numeric_selection(
                chat_id,
                "directory",
                [os.path.abspath(os.path.join(cwd, item)) for item in dirs],
            )
            directory_list += "\n\n回复数字快速进入文件夹。"
        else:
            directory_list = "  (no subdirectories)"
        if not dirs:
            self._numeric_selections.pop(chat_id, None)
        state = self.store.get(chat_id)
        title = state.get("thread_name") or "Tmux Bridge"
        self._send_info(
            chat_id,
            title,
            f"**pwd:** `{cwd}`\n\n**folders:**\n{directory_list}",
        )

    def _change_dir(self, chat_id: str, value: str) -> None:
        current = self.store.get(chat_id)["cwd"]
        if not value:
            target = self.store.default_cwd
        elif os.path.isabs(value):
            target = value
        else:
            target = os.path.abspath(os.path.join(current, value))
        if not os.path.isdir(target):
            if value and not os.path.isabs(value):
                self.messenger.send_text(chat_id, f"未找到: {value}")
            else:
                self.messenger.send_text(chat_id, f"目录不存在: {target}")
            return
        self.store.update(chat_id, cwd=target)
        self._show_pwd(chat_id)

    def _stop(self, chat_id: str, *, acknowledgement: str) -> None:
        state = self.store.get(chat_id)
        thread_id = state.get("thread_id")
        if not thread_id:
            self._send_info(chat_id, "tmux-bridge", WELCOME_TEXT, "indigo")
            return
        turn_id = self._active_turn.get(thread_id) if thread_id else None
        if not turn_id or turn_id == "external":
            self.messenger.send_text(chat_id, "当前没有可中断的 turn")
            return
        self.appserver.interrupt_turn(thread_id, turn_id)
        self.messenger.send_text(chat_id, acknowledgement)

    # Turn submission and event sync ---------------------------------------

    def _submit(self, chat_id: str, text: str) -> None:
        state = self.store.get(chat_id)
        thread_id = state.get("thread_id")
        if not thread_id:
            self._send_info(chat_id, "tmux-bridge", WELCOME_TEXT, "indigo")
            return
        thread_name = state.get("thread_name") or thread_id[:8]
        self._ensure_local_session(
            thread_name,
            thread_id,
            state.get("cwd") or self.store.default_cwd,
        )
        message_id = self.messenger.send_card(
            chat_id,
            title=thread_name,
            content="等待执行…",
            prompt=text,
            subtitle=state.get("cwd"),
            footer="Codex 已收到",
            template="orange",
        )
        submission = Submission(chat_id, thread_id, thread_name, text, message_id)
        with self._lock:
            if thread_id in self._active_turn:
                self._queues[thread_id].append(submission)
                if message_id:
                    self.card_updater.submit(
                        message_id,
                        title=thread_name,
                        content="等待前一个任务完成…",
                        prompt=text,
                        footer=f"已排队 · 前方 {len(self._queues[thread_id]) - 1} 个任务",
                        template="grey",
                    )
                return
            self._active_turn[thread_id] = "starting"
            self._drafts[thread_id] = submission
        self._start_submission(submission)

    def _start_submission(self, submission: Submission) -> None:
        try:
            turn = self.appserver.start_turn(submission.thread_id, submission.text)
        except Exception:
            with self._lock:
                self._active_turn.pop(submission.thread_id, None)
                self._drafts.pop(submission.thread_id, None)
            if submission.message_id:
                self.card_updater.submit(
                    submission.message_id,
                    title=submission.thread_name,
                    content="提交失败",
                    prompt=submission.text,
                    footer="Codex 请求失败",
                    template="red",
                )
            raise

        turn_id = turn["id"]
        key = (submission.thread_id, turn_id)
        with self._lock:
            runtime = self._runtimes.setdefault(
                key,
                TurnRuntime(TurnView(submission.thread_id, turn_id, submission.thread_name)),
            )
            if not runtime.view.finished:
                self._active_turn[submission.thread_id] = turn_id
                self._feishu_turns.add(key)
            runtime.view.user_text = submission.text
            if submission.message_id:
                runtime.cards[submission.chat_id] = submission.message_id
            self._drafts.pop(submission.thread_id, None)

    def on_appserver_event(self, event: dict[str, Any]) -> None:
        method = event.get("method", "")
        if method == "connection/restored":
            logger.info("App Server connection restored; reconciling local tmux sessions")
            self._reconcile_tmux()
            return
        if method == "connection/error":
            logger.warning("App Server event: %s", method)
            return
        params = event.get("params") or {}
        thread_id = params.get("threadId")
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if not thread_id or not turn_id:
            return

        key = (thread_id, turn_id)
        with self._lock:
            draft = self._drafts.get(thread_id)
            thread_name = draft.thread_name if draft else self._thread_name(thread_id)
            runtime = self._runtimes.setdefault(
                key,
                TurnRuntime(TurnView(thread_id, turn_id, thread_name)),
            )
            if draft:
                self._feishu_turns.add(key)
                runtime.view.user_text = draft.text
                if draft.message_id:
                    runtime.cards[draft.chat_id] = draft.message_id
            if method == "turn/started":
                self._active_turn[thread_id] = turn_id
            changed = runtime.view.apply(event)

            if runtime.view.user_text and not runtime.cards:
                self._create_cards_for_external_turn(runtime)
            cards = dict(runtime.cards)
            finished = runtime.view.finished

        if changed:
            payload = self._view_card(runtime.view)
            for message_id in cards.values():
                self.card_updater.submit(message_id, **payload)

        if finished:
            self._finish_turn(thread_id, turn_id)

    def _create_cards_for_external_turn(self, runtime: TurnRuntime) -> None:
        for chat_id in self.store.chats_for_thread(runtime.view.thread_id):
            message_id = self.messenger.send_card(chat_id, **self._view_card(runtime.view))
            if message_id:
                runtime.cards[chat_id] = message_id

    def _finish_turn(self, thread_id: str, turn_id: str) -> None:
        next_submission = None
        refresh_local_tui = False
        with self._lock:
            key = (thread_id, turn_id)
            refresh_local_tui = key in self._feishu_turns
            self._feishu_turns.discard(key)
            if self._active_turn.get(thread_id) == turn_id:
                self._active_turn.pop(thread_id, None)
            if self._queues[thread_id]:
                next_submission = self._queues[thread_id].popleft()
                self._active_turn[thread_id] = "starting"
                self._drafts[thread_id] = next_submission
        if refresh_local_tui:
            result = self.tmux.refresh_session(thread_id)
            if result.available:
                logger.info("Refreshed local TUI after Feishu turn %s: %s", turn_id[:8], result.message)
            else:
                logger.warning("Unable to refresh local TUI after Feishu turn: %s", result.message)
        if next_submission:
            try:
                self._start_submission(next_submission)
            except Exception:
                logger.exception("Queued turn failed to start")

    def _thread_name(self, thread_id: str) -> str:
        for value in self.store.all().values():
            if value.get("thread_id") == thread_id:
                return value.get("thread_name") or thread_id[:8]
        return thread_id[:8]

    @staticmethod
    def _display_name(thread: dict[str, Any]) -> str:
        value = thread.get("name") or thread.get("preview") or thread["id"][:8]
        value = " ".join(str(value).split())
        return value if len(value) <= 48 else value[:47] + "…"

    def _reconcile_tmux(self) -> None:
        seen: set[str] = set()
        for value in self.store.all().values():
            thread_id = value.get("thread_id")
            if not thread_id or thread_id in seen:
                continue
            seen.add(thread_id)
            self._ensure_local_session(
                value.get("thread_name") or thread_id[:8],
                thread_id,
                value.get("cwd") or self.store.default_cwd,
            )

    def _ensure_local_session(self, name: str, thread_id: str, cwd: str):
        result = self.tmux.ensure_session(name, thread_id, cwd)
        if result.available and result.created:
            logger.info("Local Codex TUI %s: %s", thread_id[:8], result.message)
        elif result.available:
            logger.debug("Local Codex TUI %s: %s", thread_id[:8], result.message)
        else:
            logger.warning("Local Codex TUI unavailable: %s", result.message)
        return result

    def _tmux_reconcile_loop(self) -> None:
        while not self._closing.wait(self.tmux_reconcile_interval):
            try:
                self._reconcile_tmux()
            except Exception:
                logger.exception("Unable to reconcile local tmux sessions")

    @staticmethod
    def _view_card(view: TurnView) -> dict[str, Any]:
        return {
            "title": view.thread_name,
            "content": view.render(),
            "prompt": view.user_text or None,
            "reasoning": view.reasoning or None,
            "activity": view.render_activity() or None,
            "footer": view.footer(),
            "template": view.template(),
        }
