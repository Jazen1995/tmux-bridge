"""Pure Feishu event gateway, isolated from WebSocket process startup."""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

from feishu_parser import extract_message_text, sender_is_allowed

logger = logging.getLogger(__name__)


class MessageDeduplicator:
    def __init__(self, max_entries: int = 2048):
        self.max_entries = max_entries
        self._order: deque[str] = deque()
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def first_seen(self, message_id: str) -> bool:
        if not message_id:
            return True
        with self._lock:
            if message_id in self._seen:
                return False
            self._seen.add(message_id)
            self._order.append(message_id)
            while len(self._order) > self.max_entries:
                self._seen.discard(self._order.popleft())
            return True


def handle_feishu_event(
    data: Any,
    *,
    controller: Any,
    deduplicator: MessageDeduplicator,
    owner_open_id: str,
    owner_union_id: str,
) -> bool:
    """Validate and route one Feishu receive event. Return whether routed."""
    message = data.event.message
    sender_id = data.event.sender.sender_id
    if not sender_is_allowed(
        open_id=sender_id.open_id,
        union_id=sender_id.union_id,
        owner_open_id=owner_open_id,
        owner_union_id=owner_union_id,
    ):
        logger.warning("Ignored message from non-owner")
        return False
    if not deduplicator.first_seen(message.message_id):
        logger.info("Ignored duplicate Feishu message")
        return False
    text = extract_message_text(message.message_type, message.content)
    if not text:
        return False
    logger.info("Received Feishu message type=%s length=%s", message.message_type, len(text))
    controller.handle_message(message.chat_id, text)
    return True
