import os
import shutil
import subprocess
import threading
import time
import uuid

import pytest

from appserver import AppServerClient
from tmux_ui import TmuxUIManager


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("RUN_CODEX_E2E") != "1", reason="set RUN_CODEX_E2E=1")
def test_real_appserver_streams_native_answer_and_completion():
    socket_path = os.environ.get(
        "CODEX_E2E_SOCKET",
        os.path.expanduser("~/.codex/tmux-bridge.sock"),
    )
    client = AppServerClient(socket_path, client_name="tmux-bridge-e2e", rpc_timeout=60)
    events = []
    completed = threading.Event()
    target_turn = None

    def on_event(event):
        nonlocal target_turn
        params = event.get("params") or {}
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if target_turn and turn_id == target_turn:
            events.append(event)
            if event.get("method") == "turn/completed":
                completed.set()

    client.add_notification_handler(on_event)
    result = client.start_thread(os.path.dirname(os.path.dirname(__file__)))
    thread_id = result["thread"]["id"]
    thread_name = f"tmux-bridge-e2e-{uuid.uuid4().hex[:8]}"
    client.set_thread_name(thread_id, thread_name)
    tmux = TmuxUIManager(
        enabled=shutil.which("tmux") is not None,
        socket_path=socket_path,
    )
    try:
        tmux_result = tmux.ensure_session(
            thread_name,
            thread_id,
            os.path.dirname(os.path.dirname(__file__)),
        )
        if tmux_result.available:
            assert tmux_result.created
            sessions = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}\t#{@codex_thread_id}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            assert f"{TmuxUIManager.session_name(thread_name)}\t{thread_id}" in sessions

        turn = client.start_turn(thread_id, "请只回复 NATIVE_E2E_OK")
        target_turn = turn["id"]
        assert completed.wait(90), [event.get("method") for event in events]

        deltas = "".join(
            (event.get("params") or {}).get("delta", "")
            for event in events
            if event.get("method") == "item/agentMessage/delta"
        )
        final_items = [
            (event.get("params") or {}).get("item") or {}
            for event in events
            if event.get("method") == "item/completed"
        ]
        final_text = "".join(
            item.get("text", "") for item in final_items if item.get("type") == "agentMessage"
        )
        completion = next(event for event in events if event.get("method") == "turn/completed")

        assert "NATIVE_E2E_OK" in (final_text or deltas)
        assert completion["params"]["turn"]["status"] == "completed"
        assert completion["params"]["turn"]["durationMs"] >= 0
    finally:
        tmux.close_session(thread_id)
        client.archive_thread(thread_id)
        client.close()
