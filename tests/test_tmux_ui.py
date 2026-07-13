from pathlib import Path
from types import SimpleNamespace

import tmux_ui
from tmux_ui import TmuxUIManager


class Runner:
    def __init__(self, sessions=None):
        self.sessions = sessions or []
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        command = args[1]
        if command == "list-sessions":
            if not self.sessions:
                return SimpleNamespace(returncode=1, stdout="", stderr="no server")
            output = "\n".join(
                "\t".join([
                    session["name"],
                    session.get("thread_id", ""),
                    session.get("cwd", "/tmp"),
                    str(session.get("attached", 0)),
                    str(session.get("windows", 1)),
                    "0" if session.get("alive", True) else "1",
                ])
                for session in self.sessions
            )
            return SimpleNamespace(returncode=0, stdout=output + "\n", stderr="")
        if command == "new-session":
            self.sessions.append({
                "name": args[args.index("-s") + 1],
                "thread_id": "",
                "cwd": args[args.index("-c") + 1],
                "alive": True,
            })
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == "set-option" and "-w" not in args:
            target = args[args.index("-t") + 1]
            option, value = args[-2], args[-1]
            for session in self.sessions:
                if session["name"] == target and option == "@codex_thread_id":
                    session["thread_id"] = value
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == "respawn-pane":
            target = args[args.index("-t") + 1]
            for session in self.sessions:
                if session["name"] == target:
                    session["alive"] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == "kill-session":
            target = args[args.index("-t") + 1]
            self.sessions = [session for session in self.sessions if session["name"] != target]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def manager(runner):
    return TmuxUIManager(
        enabled=True,
        socket_path="/tmp/codex.sock",
        codex_bin="codex",
        runner=runner,
    )


def tool_path(name):
    return f"/usr/bin/{name}"


def test_first_thread_creates_its_own_tmux_session_and_remote_tui(monkeypatch, tmp_path):
    monkeypatch.setattr(tmux_ui.shutil, "which", tool_path)
    runner = Runner()

    result = manager(runner).ensure_session("my task", "thread-123", str(tmp_path))

    assert result.created
    assert result.target == "my-task"
    assert any(
        call[1] == "new-session" and call[call.index("-s") + 1] == "my-task"
        for call in runner.calls
    )
    respawn = next(call for call in runner.calls if call[1] == "respawn-pane")
    command = respawn[-1]
    assert "resume thread-123" in command
    assert "--remote unix:///tmp/codex.sock" in command
    assert "tui_runner.sh" in command
    assert any(call[1] == "set-option" and "@codex_thread_id" in call for call in runner.calls)
    assert result.message == "本机 tmux 已创建: my-task"


def test_existing_managed_session_is_not_duplicated(monkeypatch, tmp_path):
    monkeypatch.setattr(tmux_ui.shutil, "which", tool_path)
    runner = Runner([{
        "name": "my-task",
        "thread_id": "thread-123",
        "cwd": str(tmp_path),
        "alive": True,
    }])

    result = manager(runner).ensure_session("my task", "thread-123", str(tmp_path))

    assert not result.created
    assert result.target == "my-task"
    assert all(call[1] != "new-session" for call in runner.calls)


def test_dead_managed_session_is_respawned_in_place(monkeypatch, tmp_path):
    monkeypatch.setattr(tmux_ui.shutil, "which", tool_path)
    runner = Runner([{
        "name": "任务",
        "thread_id": "thread-123",
        "cwd": str(tmp_path),
        "alive": False,
    }])

    result = manager(runner).ensure_session("任务", "thread-123", str(tmp_path))

    assert result.created
    assert any(call[1] == "respawn-pane" and "任务" in call for call in runner.calls)
    assert all(call[1] != "new-session" for call in runner.calls)
    assert result.message == "本机 tmux 已恢复: 任务"


def test_same_name_as_another_tmux_session_gets_unique_suffix(monkeypatch, tmp_path):
    monkeypatch.setattr(tmux_ui.shutil, "which", tool_path)
    runner = Runner([{"name": "demo", "thread_id": "", "cwd": "/tmp"}])

    result = manager(runner).ensure_session("demo", "12345678-abcd", str(tmp_path))

    assert result.created
    assert result.target == "demo-12345678"


def test_unicode_session_names_remain_readable():
    assert TmuxUIManager.session_name("飞书 协同 / 第一轮") == "飞书-协同-第一轮"


def test_list_sessions_exposes_managed_and_unmanaged_tmux(monkeypatch):
    monkeypatch.setattr(tmux_ui.shutil, "which", tool_path)
    runner = Runner([
        {
            "name": "managed",
            "thread_id": "thread-1",
            "cwd": "/repo",
            "attached": 1,
            "alive": True,
        },
        {"name": "ordinary", "thread_id": "", "cwd": "/tmp", "alive": True},
    ])

    sessions = manager(runner).list_sessions()

    assert [(item.name, item.managed) for item in sessions] == [
        ("managed", True),
        ("ordinary", False),
    ]
    assert sessions[0].attached == 1
    assert sessions[0].cwd == "/repo"


def test_close_session_targets_thread_metadata_not_a_name_collision(monkeypatch):
    monkeypatch.setattr(tmux_ui.shutil, "which", tool_path)
    runner = Runner([
        {"name": "same", "thread_id": "", "cwd": "/tmp"},
        {"name": "same-abcd", "thread_id": "thread-1", "cwd": "/repo"},
    ])

    manager(runner).close_session("thread-1")

    assert [item["name"] for item in runner.sessions] == ["same"]


def test_close_named_session_supports_tmux_cc_style_tk(monkeypatch):
    monkeypatch.setattr(tmux_ui.shutil, "which", tool_path)
    runner = Runner([
        {"name": "ordinary", "thread_id": "", "cwd": "/tmp"},
        {"name": "managed", "thread_id": "thread-1", "cwd": "/repo"},
    ])

    manager(runner).close_named_session("ordinary")

    assert [item["name"] for item in runner.sessions] == ["managed"]


def test_refresh_session_respawns_same_remote_tui(monkeypatch):
    monkeypatch.setattr(tmux_ui.shutil, "which", tool_path)
    runner = Runner([{
        "name": "demo",
        "thread_id": "thread-1",
        "cwd": "/repo",
        "alive": True,
    }])

    result = manager(runner).refresh_session("thread-1")

    assert result.created
    assert result.target == "demo"
    respawn = next(call for call in runner.calls if call[1] == "respawn-pane")
    assert respawn[respawn.index("-t") + 1] == "demo"
    assert "resume thread-1" in respawn[-1]
    assert "--remote unix:///tmp/codex.sock" in respawn[-1]


def test_production_code_has_no_terminal_scraping_or_key_injection():
    root = Path(__file__).parents[1]
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for pattern in ("*.py", "*.sh")
        for path in root.glob(pattern)
    )

    assert "capture-pane" not in production
    assert "send-keys" not in production
