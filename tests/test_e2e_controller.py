import os
import shutil
import subprocess
import threading
import time
import uuid

import pytest

from appserver import AppServerClient
from controller import BotController
from state import StateStore
from tmux_ui import TmuxUIManager


class RecordingMessenger:
    def __init__(self):
        self.cards = []
        self.updates = []
        self.texts = []
        self.completed = threading.Event()
        self.local_completed = threading.Event()
        self.tmux_completed = threading.Event()

    def send_card(self, chat_id, **card):
        message_id = f"message-{len(self.cards) + 1}"
        self.cards.append((chat_id, message_id, card))
        return message_id

    def update_card(self, message_id, **card):
        self.updates.append((message_id, card))
        if "CONTROLLER_NATIVE_OK" in card.get("content", "") and "已完成" in card.get("footer", ""):
            self.completed.set()
        if "LOCAL_NATIVE_OK" in card.get("content", "") and "已完成" in card.get("footer", ""):
            self.local_completed.set()
        if "TMUX_NATIVE_OK" in card.get("content", "") and "已完成" in card.get("footer", ""):
            self.tmux_completed.set()
        return True

    def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("RUN_CODEX_E2E") != "1", reason="set RUN_CODEX_E2E=1")
def test_real_controller_maps_feishu_card_to_native_turn(tmp_path):
    socket_path = os.environ.get(
        "CODEX_E2E_SOCKET",
        os.path.expanduser("~/.codex/tmux-bridge.sock"),
    )
    appserver = AppServerClient(socket_path, client_name="tmux-bridge-controller-e2e", rpc_timeout=60)
    messenger = RecordingMessenger()
    store = StateStore(str(tmp_path / "state.json"), default_cwd=os.path.dirname(os.path.dirname(__file__)))
    tmux = TmuxUIManager(
        enabled=False,
        socket_path=socket_path,
    )
    controller = BotController(appserver=appserver, messenger=messenger, store=store, tmux=tmux)
    name = f"controller-e2e-{uuid.uuid4().hex[:8]}"
    controller.start()
    try:
        controller.handle_message("chat-e2e", f"tn {name}")
        thread_id = store.get("chat-e2e")["thread_id"]
        assert thread_id

        controller.handle_message("chat-e2e", "请只回复 CONTROLLER_NATIVE_OK")

        assert messenger.completed.wait(90), messenger.updates[-5:]
        assert any("CONTROLLER_NATIVE_OK" in card[1]["content"] for card in messenger.updates)
        assert not messenger.texts

        appserver.start_turn(thread_id, "请只回复 LOCAL_NATIVE_OK")

        assert messenger.local_completed.wait(90), messenger.updates[-5:]
        local_cards = [card for card in messenger.cards if card[2].get("prompt") == "请只回复 LOCAL_NATIVE_OK"]
        assert len(local_cards) == 1
        assert any("LOCAL_NATIVE_OK" in card[1]["content"] for card in messenger.updates)
        appserver.archive_thread(thread_id)
    finally:
        controller.close()


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("RUN_CODEX_E2E") != "1", reason="set RUN_CODEX_E2E=1")
def test_real_tn_creates_ready_tmux_and_completes_on_same_native_thread(tmp_path):
    socket_path = os.environ.get(
        "CODEX_E2E_SOCKET",
        os.path.expanduser("~/.codex/tmux-bridge.sock"),
    )
    codex_bin = os.environ.get("CODEX_BIN") or shutil.which("codex")
    assert codex_bin, "Codex CLI is required for the real tmux e2e"
    appserver = AppServerClient(socket_path, client_name="tmux-bridge-tmux-e2e", rpc_timeout=60)
    messenger = RecordingMessenger()
    store = StateStore(
        str(tmp_path / "state.json"),
        default_cwd=os.path.dirname(os.path.dirname(__file__)),
    )
    tmux = TmuxUIManager(enabled=True, socket_path=socket_path, codex_bin=codex_bin)
    controller = BotController(
        appserver=appserver,
        messenger=messenger,
        store=store,
        tmux=tmux,
        tmux_reconcile_interval=0,
    )
    name = f"codex-e2e-{uuid.uuid4().hex[:8]}"
    thread_id = None
    controller.start()
    try:
        controller.handle_message("chat-e2e", f"tn {name}")
        thread_id = store.get("chat-e2e")["thread_id"]
        assert thread_id

        created = messenger.cards[-1][2]
        assert created["template"] == "green"
        assert "Codex 已就绪" in created["content"]
        assert "starting" not in created["content"].lower()
        session = tmux.session_for_thread(thread_id)
        assert session is not None
        assert session.name == name
        assert session.alive
        pane_pid_before = subprocess.check_output(
            ["tmux", "list-panes", "-t", name, "-F", "#{pane_pid}"],
            text=True,
        ).strip()

        controller.handle_message("chat-e2e", "请只回复 TMUX_NATIVE_OK")
        assert messenger.tmux_completed.wait(90), messenger.updates[-5:]
        deadline = time.time() + 10
        pane_pid_after = pane_pid_before
        while pane_pid_after == pane_pid_before and time.time() < deadline:
            time.sleep(0.1)
            pane_pid_after = subprocess.check_output(
                ["tmux", "list-panes", "-t", name, "-F", "#{pane_pid}"],
                text=True,
            ).strip()
        assert pane_pid_after != pane_pid_before

        history = appserver.read_thread(thread_id, include_turns=True)
        assert any(
            item.get("type") == "agentMessage" and "TMUX_NATIVE_OK" in item.get("text", "")
            for turn in history.get("turns") or []
            for item in turn.get("items") or []
        )
    finally:
        try:
            if thread_id:
                try:
                    appserver.archive_thread(thread_id)
                finally:
                    tmux.close_session(thread_id)
        finally:
            controller.close()
