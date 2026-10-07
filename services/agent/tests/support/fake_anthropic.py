"""A local HTTP server that answers as the Messages and token-counting endpoints do.

The model client is tested against real HTTP exchanges, as the chain adapter's errors are in stage
2.3: a server on a free port, scripted per endpoint, that records every request it receives. A
script entry can answer, answer late, or never answer until the test ends — which is how a timeout
is caused by the server rather than simulated in the client. Nothing here reaches the network.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MESSAGES = "/v1/messages"
COUNT_TOKENS = "/v1/messages/count_tokens"


@dataclass(frozen=True)
class Answer:
    status: int = 200
    body: bytes = b"{}"
    content_type: str = "application/json"
    headers: Mapping[str, str] = field(default_factory=dict)
    #: Seconds to wait before answering; None waits until the server is stopped.
    delay_s: float | None = 0.0


@dataclass(frozen=True)
class Received:
    path: str
    headers: Mapping[str, str]
    body: Any


def json_answer(status: int, document: Any, **kwargs: Any) -> Answer:
    return Answer(status, json.dumps(document).encode(), **kwargs)


def message(
    text: str | None,
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-sonnet-5-5",
    input_tokens: int = 1_200,
    output_tokens: int = 300,
    cache_read: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    request_id: str = "req_fake_01",
    extra: Mapping[str, Any] | None = None,
) -> Answer:
    """A Messages API success, with an empty thinking block first, as adaptive thinking returns."""
    content: list[dict[str, Any]] = [{"type": "thinking", "thinking": "", "signature": "c2ln"}]
    if text is not None:
        content.append({"type": "text", "text": text})
    document = {
        "id": "msg_fake_01",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write_5m + cache_write_1h,
            "cache_creation": {
                "ephemeral_5m_input_tokens": cache_write_5m,
                "ephemeral_1h_input_tokens": cache_write_1h,
            },
        },
        **(extra or {}),
    }
    return json_answer(200, document, headers={"request-id": request_id})


def token_count(input_tokens: int) -> Answer:
    return json_answer(200, {"input_tokens": input_tokens})


def provider_error(status: int, error_type: str, text: str = "fake provider error") -> Answer:
    document = {"type": "error", "error": {"type": error_type, "message": text}}
    return json_answer(status, document, headers={"request-id": "req_fake_error"})


class FakeAnthropic:
    """Answers each endpoint from its script, in order; the last entry repeats."""

    def __init__(self) -> None:
        self.scripts: dict[str, list[Answer]] = {
            COUNT_TOKENS: [token_count(1_200)],
            MESSAGES: [
                message('{"decision": {"action": "walk_away", "reason": "terms_unacceptable"}}')
            ],
        }
        self.received: list[Received] = []
        self.released = threading.Event()
        self._lock = threading.Lock()

    def script(self, path: str, *answers: Answer) -> None:
        self.scripts[path] = list(answers)

    def requests_to(self, path: str) -> list[Received]:
        return [request for request in self.received if request.path == path]

    def next_answer(self, path: str, headers: Mapping[str, str], body: Any) -> Answer:
        with self._lock:
            self.received.append(Received(path, headers, body))
            script = self.scripts.get(path)
            if not script:
                return provider_error(404, "not_found_error")
            return script.pop(0) if len(script) > 1 else script[0]


def _handler(fake: FakeAnthropic) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            raw = self.rfile.read(length)
            try:
                body: Any = json.loads(raw)
            except json.JSONDecodeError:
                body = raw.decode("utf-8", "replace")
            headers = {name.lower(): value for name, value in self.headers.items()}
            answer = fake.next_answer(self.path, headers, body)
            if answer.delay_s is None:
                fake.released.wait()
                return
            if answer.delay_s and fake.released.wait(answer.delay_s):
                return
            self.send_response(answer.status)
            self.send_header("content-type", answer.content_type)
            self.send_header("content-length", str(len(answer.body)))
            for name, value in answer.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(answer.body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  reason: signature
            return

    return Handler


@contextmanager
def serve(fake: FakeAnthropic) -> Iterator[str]:
    """Serve `fake` on a free local port; yields the base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        fake.released.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def unused_base_url() -> str:
    """A local URL nothing listens on, for a connection that is refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"
