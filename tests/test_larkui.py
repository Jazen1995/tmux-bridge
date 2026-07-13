import json
import threading
import time

from larkui import CardUpdater, build_card


def test_card_has_task_output_and_collapsed_execution_sections():
    card = json.loads(build_card(
        title="demo",
        content="答案",
        prompt="问题",
        reasoning="内部推理",
        activity="- 命令 · pytest -q · 完成",
        footer="Codex 已完成",
    ))

    assert card["schema"] == "2.0"
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "collapsible_panel"
    assert elements[0]["header"]["title"]["content"] == "本轮任务"
    assert elements[0]["expanded"] is True
    assert elements[0]["elements"][0]["content"] == "问题"
    assert elements[1]["header"]["title"]["content"] == "本轮输出"
    assert elements[1]["expanded"] is True
    assert elements[1]["elements"][-1] == {"tag": "markdown", "content": "答案"}
    panels = [element for element in elements if element["tag"] == "collapsible_panel"]
    assert [panel["header"]["title"]["content"] for panel in panels] == [
        "本轮任务",
        "本轮输出",
        "执行记录",
    ]
    assert all(set(panel["header"]) == {"title"} for panel in panels)
    assert panels[0]["expanded"] is True
    assert panels[1]["expanded"] is True
    assert panels[2]["expanded"] is False
    assert all("initial_state" not in panel for panel in panels)
    execution = panels[2]["elements"][0]["content"]
    assert "**思考摘要**\n内部推理" in execution
    assert "**工具与命令**\n- 命令" in execution
    assert card["body"]["elements"][-1]["content"] == "*Codex 已完成*"


def test_subtitle_and_answer_live_inside_expanded_output():
    card = json.loads(build_card(
        title="demo",
        subtitle="gpt-5.6-sol · /repo",
        content="答案",
        prompt="问题",
    ))

    elements = card["body"]["elements"]
    assert elements[0]["header"]["title"]["content"] == "本轮任务"
    assert elements[1]["header"]["title"]["content"] == "本轮输出"
    assert elements[1]["elements"][0]["content"] == "*gpt-5.6-sol · /repo*"
    assert elements[1]["elements"][1] == {"tag": "markdown", "content": "答案"}


def test_long_card_keeps_latest_native_result():
    content = "old\n" * 8000 + "FINAL_NATIVE_RESULT"
    card = json.loads(build_card(title="demo", content=content))

    rendered = next(
        element["content"]
        for element in card["body"]["elements"]
        if element["tag"] == "markdown"
    )
    assert len(rendered) <= 28000
    assert rendered.startswith("…（较早内容已省略）")
    assert rendered.endswith("FINAL_NATIVE_RESULT")


def test_updater_coalesces_stale_frames():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def update(message_id, **card):
        calls.append((message_id, card["content"]))
        if len(calls) == 1:
            entered.set()
            release.wait(1)
        return True

    updater = CardUpdater(update, min_interval=0)
    updater.submit("msg", content="one")
    assert entered.wait(1)
    updater.submit("msg", content="two")
    updater.submit("msg", content="three")
    release.set()
    deadline = time.time() + 2
    while len(calls) < 2 and time.time() < deadline:
        time.sleep(0.01)
    updater.close()

    assert calls == [("msg", "one"), ("msg", "three")]
