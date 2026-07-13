"""Persistent tmux sessions for local Codex TUI clients.

Each managed tmux session represents exactly one Codex App Server thread.  The
App Server remains the source of truth: this module only creates local TUI
clients and stores the thread id in tmux metadata.  It never reads terminal
contents or injects keystrokes.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TmuxResult:
    available: bool
    created: bool = False
    message: str = ""
    target: str | None = None


@dataclass(frozen=True)
class TmuxSession:
    name: str
    thread_id: str = ""
    cwd: str = ""
    attached: int = 0
    windows: int = 1
    alive: bool = True

    @property
    def managed(self) -> bool:
        return bool(self.thread_id)


class TmuxUIManager:
    """Manage one tmux session per App Server thread."""

    _THREAD_OPTION = "@codex_thread_id"
    _NAME_OPTION = "@codex_thread_name"
    _MANAGED_OPTION = "@tmux_codex_managed"

    def __init__(
        self,
        *,
        enabled: bool,
        socket_path: str,
        codex_bin: str = "codex",
        runner=subprocess.run,
    ):
        self.enabled = enabled
        self.socket_path = socket_path
        self.codex_bin = codex_bin
        self._run = runner
        self._lock = threading.RLock()

    @staticmethod
    def session_name(thread_name: str) -> str:
        """Return a readable, target-safe tmux session name."""
        value = re.sub(r"[^\w-]+", "-", thread_name.strip(), flags=re.UNICODE).strip("-")
        return (value or "codex")[:32]

    def list_sessions(self) -> list[TmuxSession]:
        """Return the real ``tmux list-sessions`` view, including unmanaged sessions."""
        if not self.enabled or shutil.which("tmux") is None:
            return []
        result = self._run(
            [
                "tmux",
                "list-sessions",
                "-F",
                "#{session_name}\t#{@codex_thread_id}\t#{pane_current_path}"
                "\t#{session_attached}\t#{session_windows}\t#{pane_dead}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        sessions: list[TmuxSession] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 6:
                continue
            name, thread_id, cwd, attached, windows, pane_dead = parts
            try:
                attached_count = int(attached or 0)
                window_count = int(windows or 1)
            except ValueError:
                attached_count, window_count = 0, 1
            sessions.append(TmuxSession(
                name=name,
                thread_id=thread_id,
                cwd=cwd,
                attached=attached_count,
                windows=window_count,
                alive=pane_dead.strip() != "1",
            ))
        return sessions

    def ensure_session(self, thread_name: str, thread_id: str, cwd: str) -> TmuxResult:
        if not self.enabled:
            return TmuxResult(available=False, message="tmux UI 已禁用")
        if shutil.which("tmux") is None:
            return TmuxResult(available=False, message="未安装 tmux，Codex 仍可从飞书使用")
        codex_path = self._codex_path()
        if codex_path is None:
            return TmuxResult(
                available=False,
                message=f"找不到 Codex 可执行文件: {self.codex_bin}（请配置绝对路径）",
            )
        if not os.path.isdir(cwd):
            return TmuxResult(available=True, message=f"工作目录不存在: {cwd}")

        with self._lock:
            existing = self.session_for_thread(thread_id)
            command = self._command(thread_id, cwd, codex_path)
            if existing:
                if existing.alive:
                    return self._result(existing.name, created=False)
                result = self._run(
                    ["tmux", "respawn-pane", "-k", "-t", existing.name, "-c", cwd, command],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    return TmuxResult(
                        available=True,
                        message=result.stderr.strip() or "恢复 Codex tmux 会话失败",
                        target=existing.name,
                    )
                return self._result(existing.name, created=True, recovered=True)

            target = self._unique_session_name(self.session_name(thread_name), thread_id)
            result = self._run(
                ["tmux", "new-session", "-d", "-s", target, "-c", cwd],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return TmuxResult(
                    available=True,
                    message=result.stderr.strip() or "创建 Codex tmux 会话失败",
                    target=target,
                )

            self._run(
                ["tmux", "set-option", "-w", "-t", target, "remain-on-exit", "on"],
                capture_output=True,
                text=True,
            )
            for option, value in (
                (self._THREAD_OPTION, thread_id),
                (self._NAME_OPTION, thread_name),
                (self._MANAGED_OPTION, "1"),
            ):
                self._run(
                    ["tmux", "set-option", "-t", target, option, value],
                    capture_output=True,
                    text=True,
                )
            result = self._run(
                ["tmux", "respawn-pane", "-k", "-t", target, "-c", cwd, command],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self._run(
                    ["tmux", "kill-session", "-t", target],
                    capture_output=True,
                    text=True,
                )
                return TmuxResult(
                    available=True,
                    message=result.stderr.strip() or "启动 Codex TUI 失败",
                    target=target,
                )
            return self._result(target, created=True)

    def session_for_thread(self, thread_id: str | None) -> TmuxSession | None:
        if not thread_id:
            return None
        return next(
            (session for session in self.list_sessions() if session.thread_id == thread_id),
            None,
        )

    def close_session(self, thread_id: str) -> None:
        if not self.enabled or shutil.which("tmux") is None:
            return
        with self._lock:
            existing = self.session_for_thread(thread_id)
            if not existing:
                return
            self._run(
                ["tmux", "kill-session", "-t", existing.name],
                capture_output=True,
                text=True,
            )

    def refresh_session(self, thread_id: str) -> TmuxResult:
        """Reconnect the remote TUI to clear stale client-only working state."""
        if not self.enabled:
            return TmuxResult(available=False, message="tmux UI 已禁用")
        if shutil.which("tmux") is None:
            return TmuxResult(available=False, message="未安装 tmux")
        codex_path = self._codex_path()
        if codex_path is None:
            return TmuxResult(
                available=False,
                message=f"找不到 Codex 可执行文件: {self.codex_bin}",
            )
        with self._lock:
            existing = self.session_for_thread(thread_id)
            if existing is None:
                return TmuxResult(available=True, message="未找到对应的 tmux 会话")
            cwd = existing.cwd or os.path.expanduser("~")
            command = self._command(thread_id, cwd, codex_path)
            result = self._run(
                ["tmux", "respawn-pane", "-k", "-t", existing.name, "-c", cwd, command],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return TmuxResult(
                    available=True,
                    message=result.stderr.strip() or "刷新 Codex TUI 失败",
                    target=existing.name,
                )
            return TmuxResult(
                available=True,
                created=True,
                message=f"本机 Codex TUI 已同步: {existing.name}",
                target=existing.name,
            )

    def close_named_session(self, name: str) -> None:
        """Close an unmanaged tmux session by its visible name."""
        if not self.enabled or shutil.which("tmux") is None:
            return
        with self._lock:
            self._run(
                ["tmux", "kill-session", "-t", name],
                capture_output=True,
                text=True,
            )

    def _command(self, thread_id: str, cwd: str, codex_path: str) -> str:
        runner = str(Path(__file__).with_name("tui_runner.sh"))
        return shlex.join([
            "bash",
            runner,
            "--",
            codex_path,
            "-C",
            cwd,
            "resume",
            thread_id,
            "--remote",
            f"unix://{self.socket_path}",
        ])

    def _codex_path(self) -> str | None:
        value = os.path.abspath(os.path.expanduser(self.codex_bin)) if "/" in self.codex_bin else None
        if value is not None:
            return value if os.path.isfile(value) and os.access(value, os.X_OK) else None
        return shutil.which(self.codex_bin)

    def _unique_session_name(self, desired: str, thread_id: str) -> str:
        names = {session.name for session in self.list_sessions()}
        if desired not in names:
            return desired
        suffix = thread_id.replace("-", "")[:8]
        base = desired[: max(1, 32 - len(suffix) - 1)]
        candidate = f"{base}-{suffix}"
        if candidate not in names:
            return candidate
        index = 2
        while f"{candidate}-{index}" in names:
            index += 1
        return f"{candidate}-{index}"

    @staticmethod
    def _result(target: str, *, created: bool, recovered: bool = False) -> TmuxResult:
        action = "已恢复" if recovered else ("已创建" if created else "已就绪")
        return TmuxResult(
            available=True,
            created=created,
            message=f"本机 tmux {action}: {target}",
            target=target,
        )
