"""End to end session over stdio.

Launches the real server process and speaks to it through pipes (just as a host would).
This is what proves the transport, the handshake and the tools work together; the other test
modules exercise the pieces in isolation.

The server runs against a copy of the database so freezing a card and opening disputes
here does not disturb the one used for manual testing.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from fraud_mcp.domain import policies

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = REPO_ROOT / "data" / "fraud.db"
MODEL = REPO_ROOT / "artifacts" / "model.json"
DEMO_ACCOUNT = "4471"

pytestmark = pytest.mark.skipif(
    not SOURCE_DB.exists() or not MODEL.exists(),
    reason="needs data/fraud.db (scripts/seed_db.py) and artifacts/model.json (scripts/train_model.py)",
)


class ServerProcess:
    """A running server, driven one message at a time."""

    def __init__(self, database: Path) -> None:
        self.database = database
        environment = dict(os.environ, FRAUD_MCP_DB=str(database))
        self.process = subprocess.Popen(
            [sys.executable, "-m", "fraud_mcp.entrypoints.stdio_main"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
            cwd=REPO_ROOT,
        )
        self.next_id = 0

    def send(self, method: str, params: dict | None = None) -> dict:
        """Sends a request and returns the parsed response."""
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params

        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

        line = self.process.stdout.readline()
        assert line, f"the server closed stdout while answering {method}"
        return json.loads(line)

    def notify(self, method: str) -> None:
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.process.stdin.flush()

    def send_raw(self, payload: str) -> dict:
        self.process.stdin.write(payload + "\n")
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())

    def call(self, name: str, arguments: dict) -> dict:
        response = self.send("tools/call", {"name": name, "arguments": arguments})
        assert "result" in response, response
        return response["result"]

    def close(self) -> tuple[int, str]:
        self.process.stdin.close()
        stderr = self.process.stderr.read()
        return self.process.wait(timeout=15), stderr


@pytest.fixture
def server(tmp_path: Path):
    database = tmp_path / "fraud.db"
    shutil.copy(SOURCE_DB, database)

    process = ServerProcess(database)
    process.send(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    )
    process.notify("notifications/initialized")

    yield process

    process.close()


def text_of(result: dict) -> str:
    return result["content"][0]["text"]


def test_the_handshake_negotiates_and_advertises_tools(tmp_path: Path):
    database = tmp_path / "fraud.db"
    shutil.copy(SOURCE_DB, database)
    process = ServerProcess(database)

    response = process.send(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest"}},
    )
    result = response["result"]

    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "fraud-monitoring"
    assert "instructions" in result

    code, stderr = process.close()
    assert code == 0
    # stderr carries the message log, which must never travel on stdout.
    assert "ready on stdio" in stderr


def test_tools_list_reports_the_six_tools(server: ServerProcess):
    tools = server.send("tools/list")["result"]["tools"]

    assert [tool["name"] for tool in tools] == [
        "get_account",
        "list_transactions",
        "score_transaction",
        "freeze_card",
        "open_dispute",
        "get_case_status",
    ]
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools)


def test_a_malformed_line_gets_a_parse_error(server: ServerProcess):
    response = server.send_raw("{ this is not json")
    assert response["error"]["code"] == -32700
    assert response["id"] is None
    # The session keeps working afterwards.
    assert server.send("ping")["result"] == {}


def test_the_investigation_walks_from_account_to_case(server: ServerProcess):
    """The demonstration scenario of the proposal, end to end."""
    account = server.call("get_account", {"account_id": DEMO_ACCOUNT})
    assert account["isError"] is False
    assert account["structuredContent"]["card"]["status"] == "active"

    listing = server.call("list_transactions", {"account_id": DEMO_ACCOUNT, "days": 7})
    transactions = listing["structuredContent"]["transactions"]
    assert transactions, "the seeded account must have recent activity"
    # The fraud label of the dataset must never leak to the client.
    assert all("is_fraud" not in item for item in transactions)

    abroad = next(item for item in transactions if item["country"] != "US")
    assessment = server.call("score_transaction", {"transaction_id": abroad["transaction_id"]})
    verdict = assessment["structuredContent"]

    assert verdict["risk_level"] == "high"
    fired = {rule["rule"] for rule in verdict["triggered_rules"]}
    assert "impossible_geovelocity" in fired
    geovelocity = next(
        rule for rule in verdict["triggered_rules"] if rule["rule"] == "impossible_geovelocity"
    )
    assert geovelocity["evidence"]["implied_speed_kmh"] > 900

    # A freeze without a token is refused, and hands back the token to confirm with.
    reason = f"Impossible geovelocity on {abroad['transaction_id']}"
    refusal = server.call("freeze_card", {"account_id": DEMO_ACCOUNT, "reason": reason})
    assert refusal["isError"] is True
    token = refusal["structuredContent"]["confirmation_token"]
    assert server.call("get_account", {"account_id": DEMO_ACCOUNT})["structuredContent"]["card"][
        "status"
    ] == "active", "the card must not be frozen until the token comes back"

    # A token from a different reason is refused.
    mismatch = server.call(
        "freeze_card",
        {"account_id": DEMO_ACCOUNT, "reason": "Some other reason entirely", "confirmation_token": token},
    )
    assert mismatch["isError"] is True

    frozen = server.call(
        "freeze_card",
        {"account_id": DEMO_ACCOUNT, "reason": reason, "confirmation_token": token},
    )
    assert frozen["isError"] is False
    assert frozen["structuredContent"]["status"] == "frozen"

    # The same token cannot be replayed.
    replay = server.call(
        "freeze_card",
        {"account_id": DEMO_ACCOUNT, "reason": reason, "confirmation_token": token},
    )
    assert replay["isError"] is True

    # A dispute over the risky transaction is accepted.
    dispute = server.call(
        "open_dispute",
        {
            "transaction_ids": [abroad["transaction_id"]],
            "reason": "Card-present purchase abroad minutes after a domestic one",
        },
    )
    assert dispute["isError"] is False
    case_id = dispute["structuredContent"]["case_id"]
    assert dispute["structuredContent"]["disputed_amount"] > 0

    # And a second one over the same transaction is not.
    repeat = server.call(
        "open_dispute",
        {"transaction_ids": [abroad["transaction_id"]], "reason": "Trying the same claim twice"},
    )
    assert repeat["isError"] is True
    assert "already disputed" in text_of(repeat)

    status = server.call("get_case_status", {"case_id": case_id})
    assert status["structuredContent"]["status"] == "open"
    assert len(status["structuredContent"]["disputes"]) == 1


def test_a_stale_transaction_is_refused_by_the_claim_window(server: ServerProcess):
    # `list_transactions` returns the newest page of a window, so the transaction is looked
    # up in the database; any charge past the window must be refused whatever its risk.
    # The margin over the window is generous on purpose; SQLite's `now` is UTC while the
    # stored timestamps are local, and the window is measured in whole days.
    with sqlite3.connect(server.database) as connection:
        stale_id = connection.execute(
            "SELECT txn_id FROM transactions WHERE account_id = ? "
            "AND ts < datetime('now', ?) ORDER BY ts DESC LIMIT 1",
            (DEMO_ACCOUNT, f"-{policies.DISPUTE_WINDOW_DAYS + 30} days"),
        ).fetchone()[0]

    refusal = server.call(
        "open_dispute",
        {"transaction_ids": [stale_id], "reason": "Claiming an old charge"},
    )

    assert refusal["isError"] is True
    assert "claim window" in text_of(refusal)


def test_unknown_identifiers_are_reported_as_tool_errors(server: ServerProcess):
    """Business failures come back as results, not as JSON-RPC errors."""
    unknown_account = server.call("get_account", {"account_id": "9999"})
    assert unknown_account["isError"] is True
    assert "does not exist" in text_of(unknown_account)

    unknown_case = server.call("get_case_status", {"case_id": "CASE-9999"})
    assert unknown_case["isError"] is True


def test_invalid_arguments_are_reported_as_protocol_errors(server: ServerProcess):
    response = server.send(
        "tools/call", {"name": "list_transactions", "arguments": {"account_id": "4471", "days": 0}}
    )
    assert response["error"]["code"] == -32602
