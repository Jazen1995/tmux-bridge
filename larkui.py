"""Feishu JSON 2.0 cards and coalesced message updates."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_MAX_CONTENT = 28000
_OMITTED = "…（较早内容已省略）\n\n"


def _sanitize(value: str) -> str:
    value = (value or "").strip()
    value = _EMAIL_RE.sub("[EMAIL]", value)
    return re.sub(r"\n{3,}", "\n\n", value)


def _truncate(value: str, limit: int = _MAX_CONTENT) -> str:
    if len(value) <= limit:
        return value
    tail = value[-(limit - len(_OMITTED)):]
    newline = tail.find("\n")
    if newline >= 0:
        tail = tail[newline + 1:]
    return _OMITTED + tail


def build_card(
    *,
    title: str,
    content: str,
    template: str = "blue",
    subtitle: str | None = None,
    prompt: str | None = None,
    reasoning: str | None = None,
    activity: str | None = None,
    footer: str | None = None,
) -> str:
    elements: list[dict[str, Any]] = []
    if prompt:
        elements.append({
            "tag": "collapsible_panel",
            "header": {
                "title": {"tag": "plain_text", "content": "本轮任务"},
            },
            "elements": [{"tag": "markdown", "content": _truncate(_sanitize(prompt))}],
            "expanded": True,
        })
        output_elements: list[dict[str, Any]] = []
        if subtitle:
            output_elements.append({
                "tag": "markdown",
                "content": f"*{_sanitize(subtitle)}*",
            })
        output_elements.append({
            "tag": "markdown",
            "content": _truncate(_sanitize(content)) or "（暂无内容）",
        })
        elements.append({
            "tag": "collapsible_panel",
            "header": {
                "title": {"tag": "plain_text", "content": "本轮输出"},
            },
            "elements": output_elements,
            "expanded": True,
        })
    else:
        if subtitle:
            elements.append({"tag": "markdown", "content": f"*{_sanitize(subtitle)}*"})
        elements.append({
            "tag": "markdown",
            "content": _truncate(_sanitize(content)) or "（暂无内容）",
        })

    execution_sections: list[str] = []
    if reasoning:
        execution_sections.append(f"**思考摘要**\n{_sanitize(reasoning)}")
    if activity:
        label = "**工具与命令**\n" if reasoning else ""
        execution_sections.append(f"{label}{_sanitize(activity)}")
    if execution_sections:
        elements.append({
            "tag": "collapsible_panel",
            "header": {
                "title": {"tag": "plain_text", "content": "执行记录"},
            },
            "elements": [{
                "tag": "markdown",
                "content": _truncate("\n\n".join(execution_sections)),
            }],
            "expanded": False,
        })
    if footer:
        elements.extend([
            {"tag": "hr"},
            {"tag": "markdown", "content": f"*{_sanitize(footer)}*"},
        ])
    return json.dumps({
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title or "Codex"},
            "template": template,
        },
        "body": {"elements": elements},
    }, ensure_ascii=False)


class LarkMessenger:
    def __init__(self, app_id: str, app_secret: str):
        self.client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    def send_card(self, chat_id: str, **card: Any) -> str | None:
        content = build_card(**card)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(content)
                .build()
            )
            .build()
        )
        response = self.client.im.v1.message.create(request)
        if not response.success():
            logger.error("send card failed: code=%s msg=%s", response.code, response.msg)
            return None
        return response.data.message_id

    def update_card(self, message_id: str, **card: Any) -> bool:
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(build_card(**card))
                .build()
            )
            .build()
        )
        response = self.client.im.v1.message.patch(request)
        if not response.success():
            logger.error("update card failed: code=%s msg=%s", response.code, response.msg)
            return False
        return True

    def send_text(self, chat_id: str, text: str) -> None:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": _truncate(text, 10000)}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self.client.im.v1.message.create(request)
        if not response.success():
            logger.error("send text failed: code=%s msg=%s", response.code, response.msg)


class CardUpdater:
    """Serialize Feishu PATCH calls and keep only the newest frame per card."""

    def __init__(
        self,
        update: Callable[..., bool],
        *,
        min_interval: float = 0.25,
        retry_delays: tuple[float, ...] = (0.5, 1.5, 3.0),
    ):
        self.update = update
        self.min_interval = min_interval
        self.retry_delays = retry_delays
        self._condition = threading.Condition()
        self._pending: dict[str, dict[str, Any]] = {}
        self._closed = False
        self._last_update = 0.0
        self._thread = threading.Thread(target=self._run, name="card-updater", daemon=True)
        self._thread.start()

    def submit(self, message_id: str, **card: Any) -> None:
        with self._condition:
            self._pending[message_id] = card
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if self._closed and not self._pending:
                    return
                message_id, card = self._pending.popitem()
            remaining = self.min_interval - (time.monotonic() - self._last_update)
            if remaining > 0:
                time.sleep(remaining)
            for attempt, delay in enumerate((0.0,) + self.retry_delays):
                if delay:
                    time.sleep(delay)
                try:
                    if self.update(message_id, **card):
                        break
                except Exception:
                    logger.exception("card update failed (attempt %s)", attempt + 1)
            self._last_update = time.monotonic()
