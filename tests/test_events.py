from events import TurnView, render_thread_history, turn_view_from_history


def event(method, **params):
    return {"method": method, "params": params}


def test_native_deltas_build_final_answer_and_completion_state():
    view = TurnView("thread-1", "turn-1", "demo")

    view.apply(event("turn/started", threadId="thread-1", turn={"id": "turn-1"}))
    view.apply(event(
        "item/started",
        threadId="thread-1",
        turnId="turn-1",
        item={"type": "userMessage", "content": [{"type": "text", "text": "今天星期几"}]},
    ))
    view.apply(event("item/agentMessage/delta", delta="今天"))
    view.apply(event("item/agentMessage/delta", delta="星期日"))
    view.apply(event(
        "item/completed",
        item={"type": "agentMessage", "text": "今天星期日", "phase": "final_answer"},
    ))
    view.apply(event(
        "turn/completed",
        turn={"status": "completed", "durationMs": 3986, "error": None},
    ))

    assert view.user_text == "今天星期几"
    assert view.answer == "今天星期日"
    assert view.finished
    assert view.template() == "blue"
    assert view.footer() == "Codex 已完成 · 4.0s"
    assert view.render() == "今天星期日"
    assert view.user_text == "今天星期几"


def test_error_event_is_explicit_and_retryable():
    view = TurnView("thread-1", "turn-1")

    view.apply(event(
        "error",
        error={"message": "Reconnecting... 2/5"},
        willRetry=True,
    ))

    assert view.status == "retrying"
    assert view.error == "Reconnecting... 2/5"
    assert not view.finished
    assert view.template() == "orange"


def test_tool_activity_uses_structured_item_fields():
    view = TurnView("thread-1", "turn-1")

    view.apply(event(
        "item/started",
        item={"type": "commandExecution", "command": "pytest -q"},
    ))
    view.apply(event(
        "item/completed",
        item={"type": "commandExecution", "command": "pytest -q", "exitCode": 0},
    ))

    assert view.activities[-1] == "命令 · pytest -q · 退出码 0"
    assert view.render_activity() == "- 命令 · pytest -q · 退出码 0"


def test_render_thread_history_reads_native_items_without_terminal_parser():
    thread = {
        "turns": [{
            "items": [
                {"type": "userMessage", "content": [{"type": "text", "text": "问题"}]},
                {"type": "agentMessage", "text": "答案"},
            ]
        }]
    }

    rendered = render_thread_history(thread)

    assert "**你**\n问题" in rendered
    assert "**Codex**\n答案" in rendered


def test_active_turn_snapshot_rebuilds_task_output_reasoning_and_activity():
    view = turn_view_from_history("thread-1", {
        "id": "turn-live",
        "status": "inProgress",
        "durationMs": None,
        "items": [
            {
                "type": "userMessage",
                "content": [{"type": "text", "text": "执行长任务"}],
            },
            {"type": "reasoning", "summary": [{"type": "text", "text": "先检查状态"}]},
            {
                "type": "commandExecution",
                "command": "pytest -q",
                "status": "completed",
                "exitCode": 0,
            },
            {"type": "agentMessage", "text": "已经完成一半"},
        ],
    }, "demo")

    assert view.turn_id == "turn-live"
    assert view.user_text == "执行长任务"
    assert view.answer == "已经完成一半"
    assert view.reasoning == "先检查状态"
    assert view.activities == ["命令 · pytest -q · 退出码 0"]
    assert view.status == "running"
    assert view.footer() == "Codex 工作中"
    assert not view.finished


def test_history_snapshot_keeps_newer_live_delta_and_terminal_state():
    snapshot = turn_view_from_history("thread-1", {
        "id": "turn-live",
        "status": "inProgress",
        "items": [
            {
                "type": "userMessage",
                "content": [{"type": "text", "text": "长任务"}],
            },
            {"type": "agentMessage", "text": "已经完成一半"},
            {
                "type": "commandExecution",
                "command": "pytest -q",
                "status": "inProgress",
            },
        ],
    })
    live = TurnView("thread-1", "turn-live")
    live.answer = "一半，继续输出"
    live.activities = ["命令 · pytest -q · 退出码 0"]
    live.status = "completed"
    live.duration_ms = 5000

    snapshot.merge_live(live)

    assert snapshot.answer == "已经完成一半，继续输出"
    assert snapshot.activities == ["命令 · pytest -q · 退出码 0"]
    assert snapshot.status == "completed"
    assert snapshot.duration_ms == 5000
