"""End to end session over HTTP.

Launches the real remote server and speaks to it over a socket, just as the host will. This
proves the transport itself: the session that has to survive between independent requests,
the refusals that never reach the protocol (the shared secret, the missing session, the absent
SSE stream) and the fact that the very same domain answers on the other side.

Each test gets its own copy of a database built from the versioned seed extract, and its own
process on a free port, so freezing a card disturbs nothing else.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.message import Message
from pathlib import Path

import pytest

from mcp_core.transport.http import API_KEY_HEADER, PROTOCOL_HEADER, SESSION_HEADER

from conftest import needs_model, needs_seed

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_ACCOUNT = "4471"
API_KEY = "test-secret"

pytestmark = [needs_seed, needs_model]


@dataclass
class Answer:
    """One HTTP answer."""

    status: int
    headers: Message
    body: dict

    @property
    def session_id(self) -> str | None:
        return self.headers.get(SESSION_HEADER)


def send(url: str, method: str, payload: dict | str | None = None, **headers: str) -> Answer:
    """One HTTP request. Refusals come back as answers, not as exceptions."""
    data = None
    if payload is not None:
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        data = raw.encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body, status, head = response.read(), response.status, response.headers
    except urllib.error.HTTPError as error:
        body, status, head = error.read(), error.code, error.headers

    return Answer(status, head, json.loads(body) if body else {})


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class RemoteServer:
    """A running remote server, driven one HTTP request at a time."""

    def __init__(self, database: Path, log: Path) -> None:
        self.database = database
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = log
        self.next_id = 0
        self.session_id: str | None = None

        self.handle = log.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "fraud_mcp.entrypoints.http_main"],
            stdout=self.handle,
            stderr=subprocess.STDOUT,
            env=dict(
                os.environ,
                FRAUD_MCP_DB=str(database),
                FRAUD_MCP_HOST="127.0.0.1",
                PORT=str(self.port),
                FRAUD_MCP_API_KEY=API_KEY,
            ),
            cwd=REPO_ROOT,
        )
        self._wait_until_healthy()

    def _wait_until_healthy(self, timeout: float = 30.0) -> None:
        """The health check is also how a client knows the server finished booting."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"the server died while starting:\n{self.log.read_text()}")
            try:
                if send(f"{self.base}/health", "GET").status == 200:
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"the server never became healthy:\n{self.log.read_text()}")

    # --- MCP over HTTP ---------------------------------------------------------------

    def post(self, payload: dict | str, **headers: str) -> Answer:
        headers.setdefault(API_KEY_HEADER, API_KEY)
        if self.session_id is not None:
            headers.setdefault(SESSION_HEADER, self.session_id)
        return send(f"{self.base}/mcp", "POST", payload, **headers)

    def send_message(self, method: str, params: dict | None = None, **headers: str) -> Answer:
        self.next_id += 1
        message: dict = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        return self.post(message, **headers)

    def notify(self, method: str) -> Answer:
        return self.post({"jsonrpc": "2.0", "method": method})

    def handshake(self) -> Answer:
        answer = self.send_message(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        )
        self.session_id = answer.session_id
        return answer

    def call(self, name: str, arguments: dict) -> dict:
        answer = self.send_message("tools/call", {"name": name, "arguments": arguments})
        assert answer.status == 200, answer.body
        assert "result" in answer.body, answer.body
        return answer.body["result"]

    def close(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=15)
        self.handle.close()


@pytest.fixture
def remote(seeded, tmp_path: Path):
    """A remote server on its own copy of the database and its own port."""
    database = tmp_path / "fraud.db"
    shutil.copy(seeded.database, database)

    server = RemoteServer(database, tmp_path / "server.log")
    yield server
    server.close()


@pytest.fixture
def ready(remote: RemoteServer) -> RemoteServer:
    """A remote server past the handshake"""
    remote.handshake()
    remote.notify("notifications/initialized")
    return remote


def text_of(result: dict) -> str:
    return result["content"][0]["text"]


# --- The endpoint that is not MCP ----------------------------------------------------


def test_health_answers_without_the_shared_secret(remote: RemoteServer):
    answer = send(f"{remote.base}/health", "GET")

    assert answer.status == 200
    assert answer.body["status"] == "ok"
    assert answer.body["server"] == "fraud-monitoring"
    assert answer.body["tools"] == 6


# --- The session, which HTTP does not give for free ----------------------------------


def test_the_handshake_hands_out_a_session_id(remote: RemoteServer):
    answer = remote.handshake()
    result = answer.body["result"]

    assert answer.status == 200
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "fraud-monitoring"
    # The identifier the client must repeat from now on.
    assert answer.session_id and len(answer.session_id) == 32

    # A notification is taken and left unanswered (no body, and 202 rather than 200).
    accepted = remote.notify("notifications/initialized")
    assert accepted.status == 202
    assert accepted.body == {}

    tools = remote.send_message("tools/list").body["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "get_account",
        "list_transactions",
        "score_transaction",
        "freeze_card",
        "open_dispute",
        "get_case_status",
    ]


def test_a_request_without_a_session_id_is_refused(remote: RemoteServer):
    answer = remote.send_message("tools/list")

    assert answer.status == 400
    assert SESSION_HEADER in answer.body["error"]["message"]


def test_an_unknown_session_id_asks_for_a_new_handshake(ready: RemoteServer):
    answer = ready.send_message("ping", **{SESSION_HEADER: "0" * 32})

    # 404 is the agreed signal (the client is expected to initialize again).
    assert answer.status == 404
    assert "initialize again" in answer.body["error"]["message"]


def test_deleting_a_session_ends_it(ready: RemoteServer):
    identifier = ready.session_id

    ended = send(
        f"{ready.base}/mcp", "DELETE", None, **{API_KEY_HEADER: API_KEY, SESSION_HEADER: identifier}
    )
    assert ended.status == 204

    assert ready.send_message("ping").status == 404
    assert send(
        f"{ready.base}/mcp", "DELETE", None, **{API_KEY_HEADER: API_KEY, SESSION_HEADER: identifier}
    ).status == 404


def test_two_sessions_are_served_at_the_same_time(remote: RemoteServer):
    """Two clients, two threads, one shared database connection behind a lock."""

    def investigate(name: str) -> tuple[str, dict]:
        opened = send(
            f"{remote.base}/mcp",
            "POST",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": name, "version": "1.0"},
                },
            },
            **{API_KEY_HEADER: API_KEY},
        )
        identifier = opened.session_id
        answer = send(
            f"{remote.base}/mcp",
            "POST",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_account", "arguments": {"account_id": DEMO_ACCOUNT}},
            },
            **{API_KEY_HEADER: API_KEY, SESSION_HEADER: identifier},
        )
        return identifier, answer.body["result"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(investigate, ["host-a", "host-b"])

    assert first[0] != second[0], "each client must get its own session"
    for _identifier, result in (first, second):
        assert result["isError"] is False
        assert result["structuredContent"]["account_id"] == DEMO_ACCOUNT


# --- Refusals that never reach the protocol ------------------------------------------


def test_the_shared_secret_is_required(remote: RemoteServer):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    missing = send(f"{remote.base}/mcp", "POST", payload)
    wrong = send(f"{remote.base}/mcp", "POST", payload, **{API_KEY_HEADER: "not-the-key"})

    assert missing.status == 401
    assert wrong.status == 401
    # It is a transport refusal, so it carries no JSON-RPC envelope.
    assert "jsonrpc" not in missing.body


def test_a_get_on_the_endpoint_declares_no_stream(remote: RemoteServer):
    """This server answers with plain JSON, so it must say it offers no SSE stream."""
    answer = send(f"{remote.base}/mcp", "GET", None, **{API_KEY_HEADER: API_KEY})

    assert answer.status == 405
    assert "POST" in answer.headers.get("Allow", "")


def test_an_unsupported_protocol_version_is_refused(ready: RemoteServer):
    answer = ready.send_message("ping", **{PROTOCOL_HEADER: "1999-01-01"})

    assert answer.status == 400
    assert "1999-01-01" in answer.body["error"]


def test_a_wrong_content_type_is_refused(ready: RemoteServer):
    answer = ready.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, **{"Content-Type": "text/plain"})

    assert answer.status == 415


def test_a_malformed_body_gets_a_parse_error(ready: RemoteServer):
    answer = ready.post("{ this is not json")

    assert answer.status == 400
    assert answer.body["error"]["code"] == -32700
    assert answer.body["id"] is None
    # The session survives a body it could not parse.
    assert ready.send_message("ping").body["result"] == {}


# --- The same server on the other side -----------------------------------------------


def test_the_same_verdict_travels_over_http(ready: RemoteServer):
    """The point of the remote server: same tools, same domain, different transport."""
    listing = ready.call("list_transactions", {"account_id": DEMO_ACCOUNT, "days": 7})
    transactions = listing["structuredContent"]["transactions"]
    abroad = next(item for item in transactions if item["country"] != "US")

    verdict = ready.call("score_transaction", {"transaction_id": abroad["transaction_id"]})[
        "structuredContent"
    ]
    assert verdict["risk_level"] == "high"
    fired = {rule["rule"] for rule in verdict["triggered_rules"]}
    assert "impossible_geovelocity" in fired

    # The two step control is enforced by the server, so HTTP cannot talk its way around it.
    refusal = ready.call(
        "freeze_card", {"account_id": DEMO_ACCOUNT, "reason": "Impossible geovelocity"}
    )
    assert refusal["isError"] is True
    assert refusal["structuredContent"]["confirmation_token"]

    # And a business refusal is still a result, not a JSON-RPC error.
    unknown = ready.call("get_account", {"account_id": "9999"})
    assert unknown["isError"] is True
    assert "does not exist" in text_of(unknown)
