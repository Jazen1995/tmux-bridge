"""Feishu gateway for Codex App Server threads."""

from __future__ import annotations

import logging
import os

import lark_oapi as lark

from appserver import AppServerClient
from controller import BotController
from gateway import MessageDeduplicator, handle_feishu_event
from larkui import LarkMessenger
from state import StateStore
from tmux_ui import TmuxUIManager


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


APP_ID = os.environ["FEISHU_APP_ID"]
APP_SECRET = os.environ["FEISHU_APP_SECRET"]
OWNER_ID = os.environ.get("FEISHU_OWNER_ID", "").strip()
OWNER_UNION_ID = os.environ.get("FEISHU_OWNER_UNION_ID", "").strip()
WORK_DIR = os.path.abspath(os.path.expanduser(os.environ.get("WORK_DIR", "~")))
APP_SERVER_SOCKET = os.path.abspath(os.path.expanduser(
    os.environ.get("APP_SERVER_SOCKET", "~/.codex/tmux-bridge.sock")
))
STATE_FILE = os.path.abspath(os.path.expanduser(
    os.environ.get("STATE_FILE", "~/.local/state/tmux-bridge/sessions.json")
))

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tmux-bridge")


messenger = LarkMessenger(APP_ID, APP_SECRET)
appserver = AppServerClient(APP_SERVER_SOCKET)
store = StateStore(STATE_FILE, default_cwd=WORK_DIR)
tmux = TmuxUIManager(
    enabled=_bool_env("TMUX_UI_ENABLED", True),
    socket_path=APP_SERVER_SOCKET,
    codex_bin=os.environ.get("CODEX_BIN", "codex"),
)
controller = BotController(
    appserver=appserver,
    messenger=messenger,
    store=store,
    tmux=tmux,
)
deduplicator = MessageDeduplicator()


def on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    try:
        handle_feishu_event(
            data,
            controller=controller,
            deduplicator=deduplicator,
            owner_open_id=OWNER_ID,
            owner_union_id=OWNER_UNION_ID,
        )
    except Exception:
        logger.exception("Feishu message handler failed")


def main() -> None:
    logger.info("Starting tmux-bridge with native App Server events")
    controller.start()
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.WARNING,
    )
    try:
        client.start()
    finally:
        controller.close()


if __name__ == "__main__":
    main()
