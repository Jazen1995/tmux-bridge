import json

from feishu_parser import extract_message_text, sender_is_allowed


def test_extract_text_and_post_messages():
    assert extract_message_text("text", json.dumps({"text": " hello "})) == "hello"
    post = {"content": [[{"tag": "text", "text": "hello"}, {"tag": "a", "text": " link"}]]}
    assert extract_message_text("post", json.dumps(post)) == "hello link"


def test_union_id_is_stable_owner_boundary_across_apps():
    assert sender_is_allowed(
        open_id="new-app-open-id",
        union_id="owner-union-id",
        owner_open_id="old-app-open-id",
        owner_union_id="owner-union-id",
    )
    assert not sender_is_allowed(
        open_id="old-app-open-id",
        union_id="attacker-union-id",
        owner_open_id="old-app-open-id",
        owner_union_id="owner-union-id",
    )


def test_no_owner_configuration_fails_closed():
    assert not sender_is_allowed(
        open_id="anyone",
        union_id="anyone",
        owner_open_id="",
        owner_union_id="",
    )
