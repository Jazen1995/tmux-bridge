import time

from controller import BotController
from state import StateStore
from tmux_ui import TmuxSession


class FakeAppServer:
    def __init__(self):
        self.handler = None
        self.threads = []
        self.turns = []
        self.archived = []
        self.resumed = []
        self.closed = False

    def add_notification_handler(self, handler):
        self.handler = handler

    def connect(self):
        pass

    def close(self):
        self.closed = True

    def list_threads(self, *, cwd=None, search_term=None, limit=50):
        result = self.threads
        if search_term:
            result = [item for item in result if search_term in (item.get("name") or "")]
        return result[:limit]

    def start_thread(self, cwd):
        thread = {
            "id": f"thread-{len(self.threads) + 1}",
            "name": None,
            "cwd": cwd,
            "status": {"type": "idle"},
        }
        self.threads.append(thread)
        return {"thread": thread, "model": "gpt-5.6-sol"}

    def set_thread_name(self, thread_id, name):
        for thread in self.threads:
            if thread["id"] == thread_id:
                thread["name"] = name

    def resume_thread(self, thread_id):
        self.resumed.append(thread_id)
        return {"thread": next(item for item in self.threads if item["id"] == thread_id)}

    def read_thread(self, thread_id, include_turns=False):
        thread = dict(next(item for item in self.threads if item["id"] == thread_id))
        if include_turns:
            thread["turns"] = list(thread.get("turns") or [])
        else:
            thread.pop("turns", None)
        return thread

    def start_turn(self, thread_id, text):
        turn = {"id": f"turn-{len(self.turns) + 1}", "thread_id": thread_id, "text": text}
        self.turns.append(turn)
        return turn

    def interrupt_turn(self, thread_id, turn_id):
        self.interrupted = (thread_id, turn_id)

    def archive_thread(self, thread_id):
        self.archived.append(thread_id)


class FakeMessenger:
    def __init__(self):
        self.cards = []
        self.updates = []
        self.texts = []

    def send_card(self, chat_id, **card):
        message_id = f"msg-{len(self.cards) + 1}"
        self.cards.append((chat_id, message_id, card))
        return message_id

    def update_card(self, message_id, **card):
        self.updates.append((message_id, card))
        return True

    def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))


class FakeTmux:
    def __init__(self):
        self.windows = []
        self.closed = []
        self.closed_named = []
        self.refreshed = []
        self.listed = []

    def ensure_session(self, name, thread_id, cwd):
        self.windows.append((name, thread_id, cwd))
        if not any(item.thread_id == thread_id for item in self.listed):
            self.listed.append(TmuxSession(name=name, thread_id=thread_id, cwd=cwd))
        return type(
            "Result",
            (),
            {"available": True, "created": True, "message": "tmux ready", "target": name},
        )()

    def list_sessions(self):
        return list(self.listed)

    def close_session(self, thread_id):
        self.closed.append(thread_id)
        self.listed = [item for item in self.listed if item.thread_id != thread_id]

    def close_named_session(self, name):
        self.closed_named.append(name)
        self.listed = [item for item in self.listed if item.name != name]

    def refresh_session(self, thread_id):
        self.refreshed.append(thread_id)
        return type(
            "Result",
            (),
            {"available": True, "created": True, "message": "tmux refreshed", "target": "demo"},
        )()


def make_controller(tmp_path):
    app = FakeAppServer()
    messenger = FakeMessenger()
    store = StateStore(str(tmp_path / "state.json"), default_cwd=str(tmp_path))
    tmux = FakeTmux()
    controller = BotController(appserver=app, messenger=messenger, store=store, tmux=tmux)
    return controller, app, messenger, store, tmux


def wait_for(predicate, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_new_thread_uses_appserver_and_tmux_only_as_tui(tmp_path):
    controller, app, messenger, store, tmux = make_controller(tmp_path)

    controller.handle_message("chat", "tn demo")

    assert store.get("chat")["thread_id"] == "thread-1"
    assert app.threads[0]["name"] == "demo"
    assert tmux.windows == [("demo", "thread-1", str(tmp_path))]
    card = messenger.cards[-1][2]
    assert card["footer"] == "tmux ready"
    assert card["template"] == "green"
    assert "Codex 已就绪" in card["content"]
    assert "tmux attach -t demo" in card["content"]
    assert "starting" not in card["content"].lower()
    controller.close()


def test_feishu_turn_consumes_native_deltas_and_completion(tmp_path):
    controller, app, messenger, store, tmux = make_controller(tmp_path)
    app.threads.append({"id": "thread-1", "name": "demo", "cwd": str(tmp_path)})
    store.update("chat", thread_id="thread-1", thread_name="demo")

    controller.handle_message("chat", "今天星期几")
    turn_id = app.turns[0]["id"]
    for event in [
        {"method": "turn/started", "params": {"threadId": "thread-1", "turn": {"id": turn_id}}},
        {"method": "item/agentMessage/delta", "params": {"threadId": "thread-1", "turnId": turn_id, "delta": "今天"}},
        {"method": "item/agentMessage/delta", "params": {"threadId": "thread-1", "turnId": turn_id, "delta": "星期日"}},
        {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": turn_id, "status": "completed", "durationMs": 1000}}},
    ]:
        controller.on_appserver_event(event)

    assert wait_for(lambda: any("今天星期日" in update[1]["content"] for update in messenger.updates))
    final = [item for item in messenger.updates if "今天星期日" in item[1]["content"]][-1]
    assert final[1]["footer"] == "Codex 已完成 · 1.0s"
    assert tmux.refreshed == ["thread-1"]
    controller.close()


def test_second_feishu_turn_waits_for_first_completion(tmp_path):
    controller, app, _, store, _ = make_controller(tmp_path)
    app.threads.append({"id": "thread-1", "name": "demo", "cwd": str(tmp_path)})
    store.update("chat", thread_id="thread-1", thread_name="demo")

    controller.handle_message("chat", "first")
    controller.handle_message("chat", "second")
    assert [turn["text"] for turn in app.turns] == ["first"]

    controller.on_appserver_event({
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": "completed", "durationMs": 10},
        },
    })

    assert [turn["text"] for turn in app.turns] == ["first", "second"]
    controller.close()


def test_local_tui_turn_creates_card_for_bound_feishu_chat(tmp_path):
    controller, app, messenger, store, tmux = make_controller(tmp_path)
    app.threads.append({"id": "thread-1", "name": "demo", "cwd": str(tmp_path)})
    store.update("chat", thread_id="thread-1", thread_name="demo")

    controller.on_appserver_event({
        "method": "turn/started",
        "params": {"threadId": "thread-1", "turn": {"id": "local-turn"}},
    })
    controller.on_appserver_event({
        "method": "item/started",
        "params": {
            "threadId": "thread-1",
            "turnId": "local-turn",
            "item": {
                "type": "userMessage",
                "content": [{"type": "text", "text": "从开发机发出的消息"}],
            },
        },
    })

    assert messenger.cards[-1][0] == "chat"
    assert messenger.cards[-1][2]["prompt"] == "从开发机发出的消息"
    assert messenger.cards[-1][2]["content"] == "正在处理…"
    controller.on_appserver_event({
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {"id": "local-turn", "status": "completed"},
        },
    })
    assert tmux.refreshed == []
    controller.close()


def test_attach_by_tmux_name_opens_same_native_thread(tmp_path):
    controller, app, messenger, store, tmux = make_controller(tmp_path)
    thread_id = "019f56cf-1234-5678-9abc-def012345678"
    app.threads.append({
        "id": thread_id,
        "name": None,
        "preview": "分析一下这个项目的架构与长期演进方向",
        "cwd": str(tmp_path),
        "status": {"type": "idle"},
    })
    tmux.listed = [TmuxSession("demo", thread_id, str(tmp_path))]

    controller.handle_message("chat", "ta demo")

    assert store.get("chat")["thread_name"] == "demo"
    assert tmux.windows[-1][1] == thread_id
    assert messenger.cards[-1][2]["title"] == "demo"
    controller.close()


def test_attach_to_running_session_hydrates_prefix_and_continues_live_card(tmp_path):
    controller, app, messenger, store, tmux = make_controller(tmp_path)
    app.threads.append({
        "id": "thread-live",
        "name": "live",
        "cwd": str(tmp_path),
        "status": {"type": "active"},
        "turns": [{
            "id": "turn-live",
            "status": "inProgress",
            "items": [
                {
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "执行长任务"}],
                },
                {"type": "agentMessage", "text": "已经完成一半"},
                {
                    "type": "commandExecution",
                    "command": "pytest -q",
                    "status": "completed",
                    "exitCode": 0,
                },
            ],
        }],
    })
    tmux.listed = [TmuxSession("live", "thread-live", str(tmp_path))]

    controller.handle_message("chat", "tls")
    controller.handle_message("chat", "1")

    card = messenger.cards[-1][2]
    assert store.get("chat")["thread_id"] == "thread-live"
    assert card["title"] == "live"
    assert card["prompt"] == "执行长任务"
    assert card["content"] == "已经完成一半"
    assert "pytest -q" in card["activity"]
    assert card["footer"] == "Codex 工作中"
    assert card["template"] == "orange"

    controller.on_appserver_event({
        "method": "item/agentMessage/delta",
        "params": {
            "threadId": "thread-live",
            "turnId": "turn-live",
            "delta": "，继续输出",
        },
    })
    assert wait_for(lambda: any(
        update[1]["content"] == "已经完成一半，继续输出"
        for update in messenger.updates
    ))

    controller.on_appserver_event({
        "method": "turn/completed",
        "params": {
            "threadId": "thread-live",
            "turn": {
                "id": "turn-live",
                "status": "completed",
                "durationMs": 5000,
            },
        },
    })
    assert wait_for(lambda: any(
        update[1]["footer"] == "Codex 已完成 · 5.0s"
        for update in messenger.updates
    ))
    assert tmux.refreshed == []
    controller.close()


def test_reselect_running_session_pushes_new_card_and_moves_live_updates(tmp_path):
    controller, app, messenger, store, tmux = make_controller(tmp_path)
    app.threads.extend([
        {
            "id": "thread-live",
            "name": "live",
            "cwd": str(tmp_path),
            "status": {"type": "active"},
            "turns": [{
                "id": "turn-live",
                "status": "inProgress",
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "执行长任务"}],
                    },
                    {"type": "agentMessage", "text": "已有输出"},
                ],
            }],
        },
        {
            "id": "thread-other",
            "name": "other",
            "cwd": str(tmp_path),
            "status": {"type": "idle"},
        },
    ])
    tmux.listed = [
        TmuxSession("live", "thread-live", str(tmp_path)),
        TmuxSession("other", "thread-other", str(tmp_path)),
    ]

    controller.handle_message("chat", "tls")
    controller.handle_message("chat", "1")
    first_live_card_id = messenger.cards[-1][1]

    controller.handle_message("chat", "tls")
    controller.handle_message("chat", "2")
    controller.handle_message("chat", "tls")
    controller.handle_message("chat", "1")

    second_live_card = messenger.cards[-1]
    assert second_live_card[1] != first_live_card_id
    assert second_live_card[2]["prompt"] == "执行长任务"
    assert second_live_card[2]["content"] == "已有输出"

    controller.on_appserver_event({
        "method": "item/agentMessage/delta",
        "params": {
            "threadId": "thread-live",
            "turnId": "turn-live",
            "delta": "，继续输出",
        },
    })
    assert wait_for(lambda: any(
        message_id == second_live_card[1]
        and card["content"] == "已有输出，继续输出"
        for message_id, card in messenger.updates
    ))
    assert not any(
        message_id == first_live_card_id
        and card["content"] == "已有输出，继续输出"
        for message_id, card in messenger.updates
    )
    controller.close()


def test_start_hydrates_running_bound_thread(tmp_path):
    controller, app, messenger, store, _ = make_controller(tmp_path)
    app.threads.append({
        "id": "thread-live",
        "name": "live",
        "cwd": str(tmp_path),
        "status": {"type": "active"},
        "turns": [{
            "id": "turn-live",
            "status": "inProgress",
            "items": [
                {
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "重启前任务"}],
                },
                {"type": "agentMessage", "text": "恢复已有输出"},
            ],
        }],
    })
    store.update("chat", thread_id="thread-live", thread_name="live")
    controller.tmux_reconcile_interval = 0

    controller.start()

    card = messenger.cards[-1][2]
    assert card["prompt"] == "重启前任务"
    assert card["content"] == "恢复已有输出"
    assert card["footer"] == "Codex 工作中"
    controller.close()


def test_start_restores_bound_thread_and_local_window(tmp_path):
    controller, app, _, store, tmux = make_controller(tmp_path)
    app.threads.append({
        "id": "thread-1",
        "name": None,
        "preview": "可读的历史摘要",
        "cwd": str(tmp_path),
        "status": {"type": "idle"},
    })
    store.update("chat", thread_id="thread-1", thread_name="my-session")
    controller.tmux_reconcile_interval = 0

    controller.start()

    assert app.resumed == ["thread-1"]
    assert store.get("chat")["thread_name"] == "my-session"
    assert tmux.windows == [("my-session", "thread-1", str(tmp_path))]
    controller.close()


def test_each_feishu_submission_reconciles_local_tui(tmp_path):
    controller, app, _, store, tmux = make_controller(tmp_path)
    app.threads.append({"id": "thread-1", "name": "demo", "cwd": str(tmp_path)})
    store.update("chat", thread_id="thread-1", thread_name="demo")

    controller.handle_message("chat", "继续协作")

    assert tmux.windows == [("demo", "thread-1", str(tmp_path))]
    controller.close()


def test_tls_lists_real_tmux_sessions_and_number_binds_managed_codex(tmp_path):
    controller, app, messenger, store, tmux = make_controller(tmp_path)
    app.threads.append({
        "id": "thread-1",
        "name": "managed",
        "cwd": str(tmp_path),
        "status": {"type": "idle"},
    })
    tmux.listed = [
        TmuxSession("ordinary", "", "/tmp"),
        TmuxSession("managed", "thread-1", str(tmp_path), attached=1),
    ]

    controller.handle_message("chat", "tls")

    card = messenger.cards[-1][2]
    assert card["title"] == "会话列表"
    assert "1.** ordinary" in card["content"]
    assert "2.** managed" in card["content"]
    assert "回复数字快速连接" in card["content"]

    controller.handle_message("chat", "2")

    assert store.get("chat")["thread_id"] == "thread-1"
    assert app.resumed == ["thread-1"]
    controller.close()


def test_tls_rejects_unmanaged_tmux_without_faking_native_collaboration(tmp_path):
    controller, _, messenger, store, tmux = make_controller(tmp_path)
    tmux.listed = [TmuxSession("ordinary", "", "/tmp")]

    controller.handle_message("chat", "tls")
    controller.handle_message("chat", "1")

    assert store.get("chat")["thread_id"] is None
    assert "没有原生事件" in messenger.texts[-1][1]
    controller.close()


def test_tmux_cc_compatible_tnc_and_codex_arguments(tmp_path):
    controller, app, _, store, _ = make_controller(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    controller.handle_message("chat", "tnc demo codex -dir=project")

    assert app.threads[0]["name"] == "demo"
    assert app.threads[0]["cwd"] == str(project)
    assert store.get("chat")["thread_name"] == "demo"
    controller.close()


def test_tmux_codex_rejects_claude_without_treating_it_as_a_name(tmp_path):
    controller, app, messenger, _, _ = make_controller(tmp_path)

    controller.handle_message("chat", "tn demo claude")

    assert app.threads == []
    assert messenger.texts[-1][1] == "tmux-bridge 仅支持 Codex，不支持 Claude"
    controller.close()


def test_dir_and_detach_match_tmux_cc_interaction(tmp_path):
    controller, app, messenger, store, _ = make_controller(tmp_path)
    (tmp_path / "alpha").mkdir()
    app.threads.append({"id": "thread-1", "name": "demo", "cwd": str(tmp_path)})
    store.update("chat", thread_id="thread-1", thread_name="demo", cwd=str(tmp_path))

    controller.handle_message("chat", "dir")
    assert messenger.cards[-1][2]["title"] == "demo"
    assert f"**pwd:** `{tmp_path}`" in messenger.cards[-1][2]["content"]
    assert "**1.** alpha/" in messenger.cards[-1][2]["content"]
    assert "回复数字快速进入文件夹" in messenger.cards[-1][2]["content"]

    controller.handle_message("chat", "1")
    assert store.get("chat")["cwd"] == str(tmp_path / "alpha")
    assert f"**pwd:** `{tmp_path / 'alpha'}`" in messenger.cards[-1][2]["content"]

    controller.handle_message("chat", "td")
    assert messenger.texts[-1][1] == "已断开: demo"
    assert store.get("chat")["thread_id"] is None
    controller.close()


def test_pwd_number_selection_uses_sorted_folders_and_can_drill_down(tmp_path):
    controller, _, messenger, store, _ = make_controller(tmp_path)
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    nested = alpha / "nested"
    beta.mkdir()
    nested.mkdir(parents=True)

    controller.handle_message("chat", "pwd")
    content = messenger.cards[-1][2]["content"]
    assert content.index("**1.** alpha/") < content.index("**2.** beta/")

    controller.handle_message("chat", "1")
    assert store.get("chat")["cwd"] == str(alpha)
    assert "**1.** nested/" in messenger.cards[-1][2]["content"]

    controller.handle_message("chat", "1")
    assert store.get("chat")["cwd"] == str(nested)
    controller.close()


def test_latest_numbered_menu_wins_and_unrelated_message_expires_it(tmp_path):
    controller, app, messenger, store, tmux = make_controller(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    app.threads.append({
        "id": "thread-1",
        "name": "managed",
        "cwd": str(tmp_path),
        "status": {"type": "idle"},
    })
    store.update("chat", thread_id="thread-1", thread_name="managed", cwd=str(tmp_path))
    tmux.listed = [TmuxSession("managed", "thread-1", str(tmp_path))]

    controller.handle_message("chat", "tls")
    controller.handle_message("chat", "pwd")
    controller.handle_message("chat", "1")
    assert store.get("chat")["cwd"] == str(project)

    controller.handle_message("chat", "cd ..")
    controller.handle_message("chat", "pwd")
    controller.handle_message("chat", "普通任务")
    controller.handle_message("chat", "1")
    controller.on_appserver_event({
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": "completed", "durationMs": 10},
        },
    })

    assert [turn["text"] for turn in app.turns[-2:]] == ["普通任务", "1"]
    assert messenger.texts == []
    controller.close()


def test_view_is_recent_and_ctx_is_full_native_history(tmp_path):
    controller, app, messenger, store, _ = make_controller(tmp_path)
    turns = [
        {
            "items": [
                {"type": "userMessage", "content": [{"type": "text", "text": f"q{index}"}]},
                {"type": "agentMessage", "text": f"a{index}"},
            ]
        }
        for index in range(1, 6)
    ]
    app.threads.append({
        "id": "thread-1",
        "name": "demo",
        "cwd": str(tmp_path),
        "turns": turns,
    })
    store.update("chat", thread_id="thread-1", thread_name="demo")

    controller.handle_message("chat", "view")
    recent = messenger.cards[-1][2]
    assert "q1" not in recent["content"]
    assert "q3" in recent["content"]
    assert recent["footer"].startswith("最近对话 · 原生事件")

    controller.handle_message("chat", "ctx")
    full = messenger.cards[-1][2]
    assert "q1" in full["content"]
    assert full["footer"].startswith("完整上下文 · 原生事件")
    controller.close()


def test_non_tmux_slash_text_is_forwarded_but_unknown_t_command_is_rejected(tmp_path):
    controller, app, messenger, store, _ = make_controller(tmp_path)
    app.threads.append({"id": "thread-1", "name": "demo", "cwd": str(tmp_path)})
    store.update("chat", thread_id="thread-1", thread_name="demo")

    controller.handle_message("chat", "/compact")
    assert app.turns[-1]["text"] == "/compact"

    controller.handle_message("chat", "/tunknown")
    assert len(app.turns) == 1
    assert "未知命令" in messenger.texts[-1][1]
    controller.close()


def test_tk_closes_unmanaged_tmux_and_refreshes_list(tmp_path):
    controller, _, messenger, _, tmux = make_controller(tmp_path)
    tmux.listed = [
        TmuxSession("ordinary", "", "/tmp"),
        TmuxSession("managed", "thread-1", str(tmp_path)),
    ]

    controller.handle_message("chat", "tk ordinary")

    assert tmux.closed_named == ["ordinary"]
    assert messenger.cards[-1][2]["title"] == "Tmux Sessions"
    assert "已关闭: **ordinary**" in messenger.cards[-1][2]["content"]
    assert "managed" in messenger.cards[-1][2]["content"]
    controller.close()
