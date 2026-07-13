import json
import stat

from state import StateStore


def test_binding_is_persisted_atomically_and_restored(tmp_path):
    path = tmp_path / "state" / "sessions.json"
    store = StateStore(str(path), default_cwd="/workspace")

    store.update("chat-1", thread_id="thread-1", thread_name="demo")
    restored = StateStore(str(path), default_cwd="/other")

    assert restored.get("chat-1") == {
        "cwd": "/workspace",
        "thread_id": "thread-1",
        "thread_name": "demo",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_suffix(".json.tmp").exists()
    assert json.loads(path.read_text())["chat-1"]["thread_id"] == "thread-1"


def test_chats_for_thread_and_unbind(tmp_path):
    store = StateStore(str(tmp_path / "state.json"), default_cwd="/workspace")
    store.update("chat-a", thread_id="thread-1")
    store.update("chat-b", thread_id="thread-1")
    store.update("chat-c", thread_id="thread-2")

    assert store.chats_for_thread("thread-1") == ["chat-a", "chat-b"]
    store.unbind("chat-a")
    assert store.chats_for_thread("thread-1") == ["chat-b"]


def test_corrupt_state_fails_closed_to_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not-json")

    store = StateStore(str(path), default_cwd="/workspace")

    assert store.get("chat") == {
        "cwd": "/workspace",
        "thread_id": None,
        "thread_name": None,
    }
