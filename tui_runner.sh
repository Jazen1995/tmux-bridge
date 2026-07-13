#!/usr/bin/env bash
# Keep one interactive Codex client attached to one App Server thread.
# This wrapper does not inspect terminal output or inject terminal input.
set -u

if [[ "${1:-}" == "--" ]]; then
  shift
fi

logger -t tmux-bridge-tui "runner started: tty=$(tty), TERM=${TERM:-unset}"
while true; do
  "$@"
  status=$?
  logger -t tmux-bridge-tui "Codex TUI exited with status ${status}; retrying"
  printf '\nCodex TUI 已退出（状态码 %s），2 秒后重新连接同一会话…\n' "$status" >&2
  sleep 2
done
