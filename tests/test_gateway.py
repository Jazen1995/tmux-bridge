import json
from types import SimpleNamespace

from gateway import MessageDeduplicator, handle_feishu_event


class Controller:
    def __init__(self):
        self.calls = []

    def handle_message(self, chat_id, text):
        self.calls.append((chat_id, text))


def make_event(*, message_id="om_1", union_id="owner", text="tls"):
    return SimpleNamespace(event=SimpleNamespace(
        message=SimpleNamespace(
            message_id=message_id,
            message_type="text",
            content=json.dumps({"text": text}),
            chat_id="oc_chat",
        ),
        sender=SimpleNamespace(sender_id=SimpleNamespace(
            open_id="ou_new_app",
            union_id=union_id,
        )),
    ))


def test_owner_event_routes_chat_and_text_once():
    controller = Controller()
    deduplicator = MessageDeduplicator()
    event = make_event()

    first = handle_feishu_event(
        event,
        controller=controller,
        deduplicator=deduplicator,
        owner_open_id="ou_old_app",
        owner_union_id="owner",
    )
    duplicate = handle_feishu_event(
        event,
        controller=controller,
        deduplicator=deduplicator,
        owner_open_id="ou_old_app",
        owner_union_id="owner",
    )

    assert first is True
    assert duplicate is False
    assert controller.calls == [("oc_chat", "tls")]


def test_non_owner_is_rejected_before_controller():
    controller = Controller()

    routed = handle_feishu_event(
        make_event(union_id="someone-else"),
        controller=controller,
        deduplicator=MessageDeduplicator(),
        owner_open_id="",
        owner_union_id="owner",
    )

    assert routed is False
    assert controller.calls == []


def test_unsupported_message_is_not_marked_as_routed():
    controller = Controller()
    event = make_event(text="")

    routed = handle_feishu_event(
        event,
        controller=controller,
        deduplicator=MessageDeduplicator(),
        owner_open_id="",
        owner_union_id="owner",
    )

    assert routed is False
    assert controller.calls == []
