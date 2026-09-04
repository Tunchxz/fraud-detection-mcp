"""The host's MCP clients, against the project's own servers.

What this covers is the client half of the protocol: the handshake, the identifier
correlation, the two classes of error, the HTTP session and its recovery, and the manager
that turns several servers into one catalogue.

The official Filesystem and Git servers are deliberately not exercised here. They are
downloaded by `npx` and `uvx` on first use, so putting them in the suite would make it need
the network and turn the pipeline slow and flaky.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from mcp_core.msglog import MessageLog

from host.client.base import McpError, McpTransportError, failed
from host.client.http_client import HttpClient
from host.client.manager import ClientManager, load_specs
from host.client.stdio_client import StdioClient

from conftest import needs_model, needs_seed
from test_http_session import API_KEY, RemoteServer

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_ACCOUNT = "4471"
FRAUD_TOOLS = [
    "get_account",
    "list_transactions",
    "score_transaction",
    "freeze_card",
    "open_dispute",
    "get_case_status",
]

pytestmark = [needs_seed, needs_model]


@pytest.fixture
def database(seeded, tmp_path: Path) -> Path:
    """Each test works on its own copy, so freezing a card disturbs nothing else."""
    copy = tmp_path / "fraud.db"
    shutil.copy(seeded.database, copy)
    return copy


@pytest.fixture
def log(tmp_path: Path) -> MessageLog:
    return MessageLog(path=str(tmp_path / "mcp.log"), label="fraud-local", sink=lambda _line: None)


@pytest.fixture
def local(database: Path, log: MessageLog):
    """The project's server as a subprocess, spoken to over pipes."""
    client = StdioClient(
        "fraud-local",
        log,
        [sys.executable, "-m", "fraud_mcp.entrypoints.stdio_main"],
        cwd=REPO_ROOT,
        env={"FRAUD_MCP_DB": str(database)},
    )
    client.connect()
    yield client
    client.close()


@pytest.fixture
def remote(database: Path, tmp_path: Path):
    """The same server over HTTP, on its own port."""
    server = RemoteServer(database, tmp_path / "server.log")
    client = HttpClient(
        "fraud-remote",
        MessageLog(path=str(tmp_path / "mcp-http.log"), label="fraud-remote", sink=lambda _l: None),
        f"{server.base}/mcp",
        api_key=API_KEY,
    )
    client.connect()
    yield client
    client.close()
    server.close()


# --- The handshake -------------------------------------------------------------------


def test_the_handshake_learns_what_the_server_offers(local: StdioClient):
    assert local.server_info["name"] == "fraud-monitoring"
    assert local.protocol_version == "2025-11-25"
    # The server describes its intended workflow here, and the agent puts it in the prompt.
    assert "fraud monitoring analyst" in local.instructions
    assert [tool["name"] for tool in local.tools] == FRAUD_TOOLS
    local.ping()


def test_the_client_refuses_a_protocol_version_it_does_not_speak(database: Path, log, monkeypatch):
    """Negotiation is the client's call: an answer outside our list ends the connection."""
    monkeypatch.setattr("host.client.base.SUPPORTED_PROTOCOL_VERSIONS", ("1999-01-01",))
    client = StdioClient(
        "fraud-local",
        log,
        [sys.executable, "-m", "fraud_mcp.entrypoints.stdio_main"],
        cwd=REPO_ROOT,
        env={"FRAUD_MCP_DB": str(database)},
    )
    with pytest.raises(McpTransportError, match="does not speak"):
        client.connect()
    client.close()


# --- The two classes of error --------------------------------------------------------


def test_a_business_refusal_comes_back_as_a_result(local: StdioClient):
    """A refusal has to reach the model, so it travels as a result and not as an exception."""
    result = local.call_tool("get_account", {"account_id": "9999"})

    assert failed(result) is True
    assert "does not exist" in result["content"][0]["text"]
    # `isError` is optional and absent means success, which is how foreign servers answer.
    assert failed({"content": []}) is False


def test_a_protocol_problem_raises(local: StdioClient):
    with pytest.raises(McpError) as unknown:
        local.call_tool("no_such_tool", {})
    assert unknown.value.code == -32601

    with pytest.raises(McpError) as invalid:
        local.call_tool("list_transactions", {"account_id": DEMO_ACCOUNT, "days": 0})
    assert invalid.value.code == -32602

    # The session survives both.
    local.ping()


def test_the_investigation_runs_over_the_client(local: StdioClient):
    listing = local.call_tool("list_transactions", {"account_id": DEMO_ACCOUNT, "days": 7})
    abroad = next(
        item
        for item in listing["structuredContent"]["transactions"]
        if item["country"] != "US"
    )

    verdict = local.call_tool("score_transaction", {"transaction_id": abroad["transaction_id"]})
    assert verdict["structuredContent"]["risk_level"] == "high"

    # The two step control is enforced by the server; the client just relays the refusal.
    refusal = local.call_tool(
        "freeze_card", {"account_id": DEMO_ACCOUNT, "reason": "Impossible geovelocity"}
    )
    assert failed(refusal) is True
    token = refusal["structuredContent"]["confirmation_token"]

    frozen = local.call_tool(
        "freeze_card",
        {
            "account_id": DEMO_ACCOUNT,
            "reason": "Impossible geovelocity",
            "confirmation_token": token,
        },
    )
    assert failed(frozen) is False
    assert frozen["structuredContent"]["status"] == "frozen"


# --- The HTTP session ----------------------------------------------------------------


def test_the_http_client_holds_a_session(remote: HttpClient):
    assert remote.session_id and len(remote.session_id) == 32
    assert [tool["name"] for tool in remote.tools] == FRAUD_TOOLS

    result = remote.call_tool("get_account", {"account_id": DEMO_ACCOUNT})
    assert failed(result) is False


def test_an_expired_session_is_picked_up_again(remote: HttpClient):
    """A 404 is the agreed signal to shake hands again."""
    first = remote.session_id
    remote.session_id = "0" * 32

    result = remote.call_tool("get_account", {"account_id": DEMO_ACCOUNT})

    assert failed(result) is False, "the call must succeed after recovering"
    assert remote.session_id not in (first, "0" * 32), "a new session must have been opened"


def test_the_keylog_directory_is_made_before_the_ssl_context(tmp_path: Path):
    """A missing directory must not stop the client from connecting.

    `ssl.create_default_context()` reads SSLKEYLOGFILE itself and raises when the directory
    is not there, which is the state of a clean clone the first time somebody sets up a
    Wireshark capture. Nothing here touches the network: building the connection object
    does not open it.
    """
    destination = tmp_path / "captures" / "sslkeys.log"
    assert not destination.parent.exists()

    client = HttpClient(
        "fraud-remote",
        MessageLog(path=str(tmp_path / "mcp.log")),
        "https://example.invalid/mcp",
        keylog=str(destination),
    )
    client._connect_socket()

    assert destination.parent.is_dir()


def test_the_wrong_shared_secret_is_refused(database: Path, tmp_path: Path):
    server = RemoteServer(database, tmp_path / "server.log")
    client = HttpClient(
        "fraud-remote",
        MessageLog(path=str(tmp_path / "mcp.log")),
        f"{server.base}/mcp",
        api_key="not-the-key",
    )
    try:
        with pytest.raises(McpTransportError, match="shared secret"):
            client.connect()
    finally:
        client.close()
        server.close()


# --- The manager ---------------------------------------------------------------------


def write_config(path: Path, servers: dict) -> Path:
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return path


def stdio_entry() -> dict:
    """The project's own server. Which database it opens comes from FRAUD_MCP_DB."""
    return {
        "transport": "stdio",
        "description": "the project's own server",
        "command": [sys.executable, "-m", "fraud_mcp.entrypoints.stdio_main"],
    }


def test_the_manager_aggregates_and_routes(database: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FRAUD_MCP_DB", str(database))
    config = write_config(tmp_path / "servers.json", {"fraud-local": stdio_entry()})

    with ClientManager.from_config(
        config, workspace=tmp_path / "workspace", log_path=tmp_path / "mcp.log"
    ) as manager:
        assert [tool["name"] for tool in manager.tools] == FRAUD_TOOLS
        # The declarations carry the server's own schema, untouched.
        declaration = manager.tools[0]
        assert declaration["type"] == "function"
        assert declaration["parameters"]["properties"]["account_id"]["type"] == "string"

        assert manager.server_of("get_account") == "fraud-local"
        assert failed(manager.call("get_account", {"account_id": DEMO_ACCOUNT})) is False
        assert "fraud monitoring analyst" in manager.instructions()

        with pytest.raises(Exception, match="No server offers"):
            manager.call("nothing_offers_this", {})


def test_two_servers_publishing_the_same_tool_get_prefixed(database: Path, tmp_path: Path, monkeypatch):
    """The safety net for genuine collisions; the host avoids it by picking one transport."""
    monkeypatch.setenv("FRAUD_MCP_DB", str(database))
    config = write_config(
        tmp_path / "servers.json",
        {"one": stdio_entry(), "two": stdio_entry()},
    )

    with ClientManager.from_config(
        config, workspace=tmp_path / "workspace", log_path=tmp_path / "mcp.log"
    ) as manager:
        names = {tool["name"] for tool in manager.tools}
        assert "one__get_account" in names and "two__get_account" in names
        assert "get_account" not in names


def test_a_server_that_will_not_open_is_reported_and_skipped(database: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FRAUD_MCP_DB", str(database))
    config = write_config(
        tmp_path / "servers.json",
        {
            "broken": {"transport": "stdio", "command": ["definitely-not-a-program"]},
            "fraud-local": stdio_entry(),
        },
    )

    with ClientManager.from_config(
        config, workspace=tmp_path / "workspace", log_path=tmp_path / "mcp.log"
    ) as manager:
        assert [key for key, _why in manager.failures] == ["broken"]
        # The rest of the host carries on.
        assert "fraud-local" in manager.clients
        assert manager.tools


def test_a_server_missing_its_variables_is_left_out(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("FRAUD_MCP_REMOTE_URL", raising=False)
    config = write_config(
        tmp_path / "servers.json",
        {"fraud-remote": {"transport": "http", "url": "${FRAUD_MCP_REMOTE_URL}"}},
    )

    specs, skipped = load_specs(config, workspace=tmp_path)

    assert specs == []
    assert skipped == [("fraud-remote", "needs FRAUD_MCP_REMOTE_URL, which is not set")]


def test_selecting_a_transport_keeps_the_documented_tool_names(database: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FRAUD_MCP_DB", str(database))
    config = write_config(
        tmp_path / "servers.json",
        {"fraud-local": stdio_entry(), "fraud-remote": stdio_entry()},
    )

    specs, _skipped = load_specs(config, workspace=tmp_path, select=["fraud-local"])

    assert [spec.key for spec in specs] == ["fraud-local"]


def test_the_log_keeps_both_directions_with_the_server_label(database: Path, tmp_path: Path, monkeypatch):
    """Requirement: every request and every answer, kept and attributable."""
    monkeypatch.setenv("FRAUD_MCP_DB", str(database))
    config = write_config(tmp_path / "servers.json", {"fraud-local": stdio_entry()})
    destination = tmp_path / "mcp.log"

    with ClientManager.from_config(
        config, workspace=tmp_path / "workspace", log_path=destination
    ) as manager:
        manager.call("get_account", {"account_id": DEMO_ACCOUNT})

    written = destination.read_text(encoding="utf-8").splitlines()
    sent = [line for line in written if "←" in line and "fraud-local" in line]
    received = [line for line in written if "→" in line and "fraud-local" in line]

    assert any('"method":"initialize"' in line for line in sent)
    assert any('"method":"tools/call"' in line for line in sent)
    assert any('"serverInfo"' in line for line in received)
    assert any("structuredContent" in line for line in received)
