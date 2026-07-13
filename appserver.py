"""Codex App Server JSON-RPC client over WebSocket-on-Unix-socket.

The Unix transport is a real WebSocket connection (HTTP Upgrade + frames), not
newline-delimited JSON.  Only the stdio transport uses JSONL.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class AppServerError(RuntimeError):
    """Base error raised by the App Server client."""


class RpcError(AppServerError):
    def __init__(self, error: dict[str, Any]):
        self.error = error
        super().__init__(error.get("message") or json.dumps(error, ensure_ascii=False))


class ConnectionClosed(AppServerError):
    pass


class UnixWebSocket:
    """Small RFC 6455 client for App Server's Unix socket transport."""

    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, socket_path: str, timeout: float = 30.0):
        self.socket_path = socket_path
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._send_lock = threading.Lock()

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))

        headers = self._read_headers(sock)
        status = headers.split(b"\r\n", 1)[0]
        if status != b"HTTP/1.1 101 Switching Protocols":
            sock.close()
            raise ConnectionClosed(f"WebSocket upgrade failed: {status.decode(errors='replace')}")

        expected = base64.b64encode(
            hashlib.sha1((key + self._GUID).encode("ascii")).digest()
        ).decode("ascii")
        parsed = {}
        for line in headers.split(b"\r\n")[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                parsed[name.strip().lower()] = value.strip().decode("ascii", errors="replace")
        if parsed.get(b"sec-websocket-accept") != expected:
            sock.close()
            raise ConnectionClosed("invalid WebSocket Sec-WebSocket-Accept")

        sock.settimeout(None)
        self._socket = sock

    @staticmethod
    def _read_headers(sock: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionClosed("connection closed during WebSocket upgrade")
            data.extend(chunk)
            if len(data) > 65536:
                raise ConnectionClosed("oversized WebSocket upgrade response")
        return bytes(data).split(b"\r\n\r\n", 1)[0]

    def close(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def send_json(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._send_lock:
            self._send_frame(0x1, raw)

    def recv_json(self) -> dict[str, Any]:
        fragments = bytearray()
        first_opcode: int | None = None
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == 0x8:
                raise ConnectionClosed("WebSocket closed by App Server")
            if opcode == 0x9:
                with self._send_lock:
                    self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                first_opcode = opcode
                fragments.extend(payload)
            elif opcode == 0x0 and first_opcode is not None:
                fragments.extend(payload)
            else:
                continue
            if fin:
                if first_opcode != 0x1:
                    raise ConnectionClosed("unexpected binary WebSocket message")
                try:
                    value = json.loads(fragments.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ConnectionClosed("invalid JSON from App Server") from exc
                if not isinstance(value, dict):
                    raise ConnectionClosed("App Server message is not an object")
                return value

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._socket is None:
            raise ConnectionClosed("WebSocket is not connected")
        mask = os.urandom(4)
        size = len(payload)
        header = bytearray([0x80 | opcode])
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.extend((0x80 | 126,))
            header.extend(struct.pack("!H", size))
        else:
            header.extend((0x80 | 127,))
            header.extend(struct.pack("!Q", size))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def _recv_exact(self, size: int) -> bytes:
        if self._socket is None:
            raise ConnectionClosed("WebSocket is not connected")
        result = bytearray()
        while len(result) < size:
            chunk = self._socket.recv(size - len(result))
            if not chunk:
                raise ConnectionClosed("WebSocket closed while reading a frame")
            result.extend(chunk)
        return bytes(result)

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._recv_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack("!H", self._recv_exact(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(size)
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload


NotificationHandler = Callable[[dict[str, Any]], None]


class AppServerClient:
    """Thread-safe JSON-RPC client with reconnect and subscription restore."""

    def __init__(
        self,
        socket_path: str,
        *,
        client_name: str = "tmux-bridge",
        client_version: str = "0.1.0",
        rpc_timeout: float = 30.0,
        reconnect_delays: tuple[float, ...] = (0.5, 1.0, 2.0, 5.0, 10.0),
        transport_factory: Callable[[str], UnixWebSocket] | None = None,
    ):
        self.socket_path = socket_path
        self.client_name = client_name
        self.client_version = client_version
        self.rpc_timeout = rpc_timeout
        self.reconnect_delays = reconnect_delays
        self._transport_factory = transport_factory or (lambda path: UnixWebSocket(path))

        self._transport: UnixWebSocket | None = None
        self._connection_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._handlers: list[NotificationHandler] = []
        self._subscriptions: set[str] = set()
        self._next_id = 1
        self._generation = 0
        self._connected = threading.Event()
        self._closing = threading.Event()
        self._reconnecting = threading.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._handlers.append(handler)

    def connect(self) -> None:
        with self._connection_lock:
            if self.connected:
                return
            self._closing.clear()
            transport = self._transport_factory(self.socket_path)
            transport.connect()
            self._transport = transport
            self._generation += 1
            generation = self._generation
            self._connected.set()
            threading.Thread(
                target=self._reader_loop,
                args=(transport, generation),
                name="appserver-reader",
                daemon=True,
            ).start()

            try:
                self.rpc("initialize", {
                    "clientInfo": {
                        "name": self.client_name,
                        "title": "tmux-bridge Feishu bridge",
                        "version": self.client_version,
                    }
                })
                self.notify("initialized", {})
                subscriptions = tuple(self._subscriptions)
                for thread_id in subscriptions:
                    try:
                        self.rpc("thread/resume", {"threadId": thread_id})
                    except RpcError:
                        logger.exception("Failed to restore thread subscription %s", thread_id)
            except Exception:
                self._disconnect(transport, generation, schedule_reconnect=False)
                raise

    def close(self) -> None:
        self._closing.set()
        with self._connection_lock:
            transport = self._transport
            self._transport = None
            self._connected.clear()
            self._generation += 1
        if transport:
            transport.close()
        self._fail_pending(ConnectionClosed("App Server client closed"))

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.connected:
            self.connect()
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        try:
            self._send({"method": method, "id": request_id, "params": params or {}})
            try:
                response = response_queue.get(timeout=self.rpc_timeout)
            except queue.Empty as exc:
                raise AppServerError(f"RPC timeout: {method}") from exc
            if isinstance(response, BaseException):
                raise response
            if "error" in response:
                raise RpcError(response["error"])
            return response.get("result") or {}
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def _send(self, payload: dict[str, Any]) -> None:
        with self._send_lock:
            transport = self._transport
            if not self.connected or transport is None:
                raise ConnectionClosed("App Server is not connected")
            try:
                transport.send_json(payload)
            except Exception:
                self._disconnect(transport, self._generation)
                raise

    def _reader_loop(self, transport: UnixWebSocket, generation: int) -> None:
        try:
            while not self._closing.is_set():
                message = transport.recv_json()
                if "id" in message:
                    with self._pending_lock:
                        waiter = self._pending.get(message["id"])
                    if waiter:
                        waiter.put(message)
                    continue
                self._emit(message)
        except Exception as exc:
            if not self._closing.is_set():
                logger.warning("App Server connection lost: %s", exc)
                self._emit({"method": "connection/error", "params": {"message": str(exc)}})
        finally:
            self._disconnect(transport, generation)

    def _disconnect(
        self,
        transport: UnixWebSocket,
        generation: int,
        *,
        schedule_reconnect: bool = True,
    ) -> None:
        with self._connection_lock:
            if generation != self._generation or self._transport is not transport:
                return
            self._transport = None
            self._connected.clear()
            transport.close()
        self._fail_pending(ConnectionClosed("App Server connection lost"))
        if schedule_reconnect and not self._closing.is_set():
            self._schedule_reconnect()

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            waiters = tuple(self._pending.values())
        for waiter in waiters:
            try:
                waiter.put_nowait(error)
            except queue.Full:
                pass

    def _schedule_reconnect(self) -> None:
        if self._reconnecting.is_set():
            return
        self._reconnecting.set()

        def run() -> None:
            try:
                attempt = 0
                while not self._closing.is_set() and not self.connected:
                    delay = self.reconnect_delays[min(attempt, len(self.reconnect_delays) - 1)]
                    time.sleep(delay)
                    try:
                        self.connect()
                        self._emit({"method": "connection/restored", "params": {}})
                        return
                    except Exception as exc:
                        attempt += 1
                        logger.warning("App Server reconnect failed: %s", exc)
            finally:
                self._reconnecting.clear()

        threading.Thread(target=run, name="appserver-reconnect", daemon=True).start()

    def _emit(self, message: dict[str, Any]) -> None:
        for handler in tuple(self._handlers):
            try:
                handler(message)
            except Exception:
                logger.exception("App Server notification handler failed")

    # Typed protocol helpers -------------------------------------------------

    def start_thread(self, cwd: str) -> dict[str, Any]:
        result = self.rpc("thread/start", {"cwd": cwd})
        thread = result["thread"]
        self._subscriptions.add(thread["id"])
        return result

    def set_thread_name(self, thread_id: str, name: str) -> None:
        self.rpc("thread/name/set", {"threadId": thread_id, "name": name})

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        self._subscriptions.add(thread_id)
        return self.rpc("thread/resume", {"threadId": thread_id})

    def list_threads(
        self,
        *,
        cwd: str | None = None,
        search_term: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if cwd:
            params["cwd"] = cwd
        if search_term:
            params["searchTerm"] = search_term
        return self.rpc("thread/list", params).get("data", [])

    def read_thread(self, thread_id: str, *, include_turns: bool = False) -> dict[str, Any]:
        return self.rpc("thread/read", {
            "threadId": thread_id,
            "includeTurns": include_turns,
        })["thread"]

    def start_turn(self, thread_id: str, text: str) -> dict[str, Any]:
        return self.rpc("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        })["turn"]

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.rpc("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def archive_thread(self, thread_id: str) -> None:
        self.rpc("thread/archive", {"threadId": thread_id})
