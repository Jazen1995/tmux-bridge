#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER_UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [[ ! -f "$ROOT/.env" ]]; then
  echo "缺少 $ROOT/.env，请先复制并填写 .env.example" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a

: "${FEISHU_APP_ID:?请在 .env 中填写 FEISHU_APP_ID}"
: "${FEISHU_APP_SECRET:?请在 .env 中填写 FEISHU_APP_SECRET}"

CODEX_PATH="$(command -v "${CODEX_BIN:-codex}" || true)"
if [[ -z "$CODEX_PATH" ]]; then
  echo "找不到 Codex CLI，请先安装 Codex 或在 .env 中配置 CODEX_BIN" >&2
  exit 1
fi

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
APP_SERVER_SOCKET="${APP_SERVER_SOCKET:-$CODEX_HOME/tmux-bridge.sock}"
if [[ "$APP_SERVER_SOCKET" != /* ]]; then
  echo "APP_SERVER_SOCKET 必须是绝对路径: $APP_SERVER_SOCKET" >&2
  exit 1
fi
for path in "$ROOT" "$CODEX_PATH" "$CODEX_HOME" "$APP_SERVER_SOCKET"; do
  if [[ "$path" =~ [[:space:]] ]]; then
    echo "当前安装脚本暂不支持路径中包含空白字符: $path" >&2
    exit 1
  fi
done

mkdir -p "$USER_UNITS" "$(dirname "$APP_SERVER_SOCKET")"
chmod 600 "$ROOT/.env"

# 只持久化 App Server 访问模型和飞书端点所需的网络变量。
umask 077
: > "$ROOT/.appserver.env"
for name in http_proxy https_proxy no_proxy LARK_CLI_NO_PROXY; do
  value="${!name-}"
  if [[ -n "$value" && "$value" != *$'\n'* ]]; then
    printf '%s=%s\n' "$name" "$value" >> "$ROOT/.appserver.env"
  fi
done

# 本地 Codex TUI 由最小化 PATH 的 systemd/tmux 进程启动，因此保存绝对路径。
printf 'CODEX_BIN=%s\n' "$CODEX_PATH" > "$ROOT/.runtime.env"
chmod 600 "$ROOT/.appserver.env" "$ROOT/.runtime.env"

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

ROOT_REPLACEMENT="$(escape_sed_replacement "$ROOT")"
CODEX_REPLACEMENT="$(escape_sed_replacement "$CODEX_PATH")"
CODEX_HOME_REPLACEMENT="$(escape_sed_replacement "$CODEX_HOME")"
SOCKET_REPLACEMENT="$(escape_sed_replacement "$APP_SERVER_SOCKET")"

render_unit() {
  local source="$1"
  local target="$2"
  sed \
    -e "s|@ROOT@|$ROOT_REPLACEMENT|g" \
    -e "s|@CODEX_BIN@|$CODEX_REPLACEMENT|g" \
    -e "s|@CODEX_HOME@|$CODEX_HOME_REPLACEMENT|g" \
    -e "s|@SOCKET@|$SOCKET_REPLACEMENT|g" \
    "$source" > "$target"
}

render_unit \
  "$ROOT/systemd/tmux-bridge-appserver.service.in" \
  "$USER_UNITS/tmux-bridge-appserver.service"
render_unit \
  "$ROOT/systemd/tmux-bridge.service.in" \
  "$USER_UNITS/tmux-bridge.service"

systemctl --user daemon-reload
systemctl --user enable tmux-bridge-appserver.service tmux-bridge.service
systemctl --user restart tmux-bridge-appserver.service

for _ in $(seq 1 40); do
  [[ -S "$APP_SERVER_SOCKET" ]] && break
  sleep 0.25
done
if [[ ! -S "$APP_SERVER_SOCKET" ]]; then
  echo "Codex App Server 未在 10 秒内创建 socket: $APP_SERVER_SOCKET" >&2
  systemctl --user status tmux-bridge-appserver.service --no-pager >&2 || true
  exit 1
fi

systemctl --user restart tmux-bridge.service
systemctl --user --no-pager --full status \
  tmux-bridge-appserver.service tmux-bridge.service
