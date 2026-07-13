import queue
import threading

from appserver import AppServerClient, ConnectionClosed


class FakeTransport:
    def __init__(self, path):
        self.path = path
        self.incoming = queue.Queue()
        self.sent = []
        self.closed = False

    def connect(self):
        pass

    def close(self):
        self.closed = True
        self.incoming.put(ConnectionClosed("closed"))

    def send_json(self, payload):
        self.sent.append(payload)
        if "id" not in payload:
            return
        method = payload["method"]
        if method == "initialize":
            result = {"userAgent": "fake"}
        elif method == "thread/start":
            result = {"thread": {"id": "thread-1", "cwd": payload["params"]["cwd"]}}
        elif method == "thread/name/set":
            result = {}
        else:
            result = {}
        self.incoming.put({"id": payload["id"], "result": result})

    def recv_json(self):
        value = self.incoming.get(timeout=2)
        if isinstance(value, BaseException):
            raise value
        return value


def test_client_initializes_before_starting_thread():
    transports = []

    def factory(path):
        transport = FakeTransport(path)
        transports.append(transport)
        return transport

    client = AppServerClient("/tmp/fake.sock", transport_factory=factory, rpc_timeout=1)
    result = client.start_thread("/workspace")

    methods = [item["method"] for item in transports[0].sent]
    assert methods[:3] == ["initialize", "initialized", "thread/start"]
    assert result["thread"]["id"] == "thread-1"
    client.close()


def test_notifications_are_dispatched_separately_from_rpc_responses():
    transport = FakeTransport("/tmp/fake.sock")
    received = []
    ready = threading.Event()

    def handler(event):
        received.append(event)
        ready.set()

    client = AppServerClient(
        "/tmp/fake.sock",
        transport_factory=lambda _: transport,
        rpc_timeout=1,
    )
    client.add_notification_handler(handler)
    client.connect()
    transport.incoming.put({
        "method": "turn/completed",
        "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}},
    })

    assert ready.wait(1)
    assert received[0]["method"] == "turn/completed"
    client.close()
