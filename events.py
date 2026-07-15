"""Reduce native App Server notifications into a mobile-friendly task view."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _content_text(content: list[dict[str, Any]] | None) -> str:
    return "\n".join(
        part.get("text", "")
        for part in (content or [])
        if part.get("type") in {"text", "inputText", "outputText"}
    ).strip()


def _compact(value: str, limit: int = 180) -> str:
    value = " ".join((value or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _reasoning_text(item: dict[str, Any]) -> str:
    value = item.get("summary") or item.get("content") or item.get("text") or ""
    if isinstance(value, list):
        return _content_text(value)
    return str(value).strip()


def _merge_stream_text(snapshot: str, live: str) -> str:
    """Merge a persisted prefix with deltas that may race the snapshot read."""
    if not snapshot:
        return live
    if not live or snapshot == live:
        return snapshot
    if snapshot.startswith(live):
        return snapshot
    if live.startswith(snapshot):
        return live
    overlap_limit = min(len(snapshot), len(live))
    for size in range(overlap_limit, 0, -1):
        if snapshot.endswith(live[:size]):
            return snapshot + live[size:]
    return snapshot + live


@dataclass
class TurnView:
    thread_id: str
    turn_id: str
    thread_name: str = "Codex"
    user_text: str = ""
    answer: str = ""
    reasoning: str = ""
    status: str = "running"
    error: str | None = None
    duration_ms: int | None = None
    activities: list[str] = field(default_factory=list)

    def apply(self, event: dict[str, Any]) -> bool:
        """Apply one notification. Return True when the card should refresh."""
        method = event.get("method", "")
        params = event.get("params") or {}

        if method == "turn/started":
            self.status = "running"
            return True
        if method == "turn/completed":
            turn = params.get("turn") or {}
            self.status = turn.get("status") or "completed"
            self.duration_ms = turn.get("durationMs")
            error = turn.get("error")
            if error:
                self.error = error.get("message") if isinstance(error, dict) else str(error)
            return True
        if method == "error":
            error = params.get("error") or {}
            self.error = error.get("message") if isinstance(error, dict) else str(error)
            self.status = "retrying" if params.get("willRetry") else "failed"
            return True
        if method == "turn/aborted":
            self.status = "aborted"
            return True

        if method == "item/agentMessage/delta":
            self.answer += params.get("delta", "")
            return True
        if "reasoning" in method.lower() and method.endswith("delta"):
            self.reasoning += params.get("delta", "")
            return True

        if method not in {"item/started", "item/completed"}:
            return False
        item = params.get("item") or {}
        item_type = item.get("type", "")
        completed = method == "item/completed"

        if item_type == "userMessage":
            self.user_text = _content_text(item.get("content")) or self.user_text
            return True
        if item_type == "agentMessage":
            if completed and item.get("text") is not None:
                self.answer = item.get("text", "")
            return True

        activity = self._activity(item, completed)
        if activity:
            base = activity.rsplit(" · ", 1)[0]
            self.activities = [
                current
                for current in self.activities
                if current.rsplit(" · ", 1)[0] != base
            ]
            self.activities.append(activity)
            self.activities = self.activities[-8:]
            return True
        return False

    def merge_live(self, live: "TurnView") -> None:
        """Merge notifications received while a history snapshot was loading."""
        if (self.thread_id, self.turn_id) != (live.thread_id, live.turn_id):
            raise ValueError("cannot merge different turns")
        self.user_text = self.user_text or live.user_text
        self.answer = _merge_stream_text(self.answer, live.answer)
        self.reasoning = _merge_stream_text(self.reasoning, live.reasoning)
        for activity in live.activities:
            base = activity.rsplit(" · ", 1)[0]
            self.activities = [
                current
                for current in self.activities
                if current.rsplit(" · ", 1)[0] != base
            ]
            self.activities.append(activity)
        self.activities = self.activities[-8:]
        if live.finished or live.status != "running":
            self.status = live.status
            self.error = live.error
            self.duration_ms = live.duration_ms

    @staticmethod
    def _activity(item: dict[str, Any], completed: bool) -> str | None:
        item_type = item.get("type", "")
        suffix = "完成" if completed else "进行中"
        if item_type in {"commandExecution", "command"}:
            command = item.get("command") or item.get("commandLine") or "command"
            exit_code = item.get("exitCode")
            if completed and exit_code is not None:
                suffix = f"退出码 {exit_code}"
            return f"命令 · {_compact(str(command), 120)} · {suffix}"
        if item_type in {"mcpToolCall", "toolCall", "dynamicToolCall"}:
            name = item.get("tool") or item.get("name") or item.get("server") or "tool"
            return f"工具 · {_compact(str(name), 100)} · {suffix}"
        if item_type in {"fileChange", "fileChanges"}:
            path = item.get("path") or item.get("filePath") or "文件变更"
            return f"文件 · {_compact(str(path), 120)} · {suffix}"
        if item_type in {"webSearch", "webSearchCall"}:
            query = item.get("query") or "Web 搜索"
            return f"搜索 · {_compact(str(query), 120)} · {suffix}"
        return None

    @property
    def finished(self) -> bool:
        return self.status in {"completed", "failed", "aborted", "interrupted"}

    def render(self) -> str:
        answer = self.answer.strip()
        if not answer:
            answer = {
                "running": "正在处理…",
                "retrying": "连接波动，正在重试…",
                "failed": "执行失败",
                "aborted": "已中断",
            }.get(self.status, "等待输出…")
        if self.error:
            answer += f"\n\n> **错误**：{self.error}"
        return answer

    def render_activity(self) -> str:
        return "\n".join(f"- {item}" for item in self.activities[-6:])

    def footer(self) -> str:
        labels = {
            "running": "Codex 工作中",
            "retrying": "Codex 重连中",
            "completed": "Codex 已完成",
            "failed": "Codex 执行失败",
            "aborted": "Codex 已中断",
            "interrupted": "Codex 已中断",
        }
        label = labels.get(self.status, f"Codex · {self.status}")
        if self.duration_ms is not None:
            label += f" · {self.duration_ms / 1000:.1f}s"
        return label

    def template(self) -> str:
        if self.status in {"failed", "aborted", "interrupted"}:
            return "red"
        if self.finished:
            return "blue"
        return "orange"


def turn_view_from_history(
    thread_id: str,
    turn: dict[str, Any],
    thread_name: str = "Codex",
) -> TurnView:
    """Rebuild a live card view from a native ``thread/read`` turn snapshot."""
    status = {
        "inProgress": "running",
        "active": "running",
        "canceled": "aborted",
    }.get(turn.get("status"), turn.get("status") or "running")
    view = TurnView(
        thread_id=thread_id,
        turn_id=turn["id"],
        thread_name=thread_name,
        status=status,
        duration_ms=turn.get("durationMs"),
    )
    error = turn.get("error")
    if error:
        view.error = error.get("message") if isinstance(error, dict) else str(error)

    for item in turn.get("items") or []:
        if item.get("type") == "reasoning":
            reasoning = _reasoning_text(item)
            if reasoning:
                view.reasoning = reasoning
            continue
        item_status = item.get("status")
        completed = item_status not in {"inProgress", "running", "pending"}
        view.apply({
            "method": "item/completed" if completed else "item/started",
            "params": {"item": item},
        })
    return view


def render_thread_history(thread: dict[str, Any], limit: int = 5) -> str:
    turns = (thread.get("turns") or [])[-limit:]
    if not turns:
        return "（暂无对话）"
    rendered: list[str] = []
    for turn in turns:
        user = ""
        answer = ""
        for item in turn.get("items") or []:
            if item.get("type") == "userMessage":
                user = _content_text(item.get("content"))
            elif item.get("type") == "agentMessage":
                answer = item.get("text", "")
        if user:
            rendered.append(f"**你**\n{user}")
        if answer:
            rendered.append(f"**Codex**\n{answer}")
    return "\n\n---\n\n".join(rendered) or "（暂无可显示内容）"
