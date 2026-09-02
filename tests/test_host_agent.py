"""The conversation loop, driven by a scripted model.

No request ever leaves the machine: the model is a stand-in that returns prepared answers,
so the suite needs no API key and spends no quota. What is being checked is that the host,
not the SDK, decides when a tool runs, what comes back to the model and what the analyst
gets a say in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from google.genai import types

from host.agent import Agent, View
from host.llm import declare, simplify_schema
from host.session import Session, build_system_prompt

DEMO_ACCOUNT = "4471"


# --- The stand-ins -------------------------------------------------------------------


def model_turn(*parts: types.Part, finish: str = "STOP") -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=list(parts)), finish_reason=finish)
        ]
    )


def says(text: str) -> types.GenerateContentResponse:
    return model_turn(types.Part.from_text(text=text))


def asks(name: str, call_id: str = "c1", **arguments) -> types.Part:
    return types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=arguments))


class ScriptedLlm:
    """Hands back prepared turns and remembers the history it was given each time."""

    def __init__(self, *turns: types.GenerateContentResponse) -> None:
        self.turns = list(turns)
        self.seen: list[list[types.Content]] = []

    def respond(self, history, system_prompt, tools):
        # A copy: the agent keeps appending to the same list.
        self.seen.append(list(history))
        self.system_prompt = system_prompt
        self.tools = tools
        return self.turns.pop(0) if self.turns else says("done")


class FakeClient:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeManager:
    """Routes by a table and records what was called."""

    def __init__(self, owners: dict[str, str], results: dict[str, dict] | None = None) -> None:
        self.owners = owners
        self.results = results or {}
        self.calls: list[tuple[str, dict]] = []
        self.workspace = Path("/tmp/workspace")
        self.tools = [
            {
                "type": "function",
                "name": name,
                "description": f"the {name} tool",
                "parameters": {"type": "object", "properties": {}},
            }
            for name in owners
        ]

    def route(self, name: str):
        if name not in self.owners:
            from host.client.base import McpClientError

            raise McpClientError(f"No server offers a tool called {name!r}")
        return FakeClient(self.owners[name]), name

    def call(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        return self.results.get(
            name, {"content": [{"type": "text", "text": f"{name} ok"}], "isError": False}
        )

    def instructions(self) -> str:
        return ""


class RecordingView(View):
    """A view that answers confirmations from a script and remembers what it was shown."""

    def __init__(self, approve: bool = True) -> None:
        self.approve = approve
        self.asked: list[tuple[str, str, dict]] = []
        self.answers: list[str] = []
        self.warnings: list[str] = []
        self.finished: list[tuple[str, bool, str]] = []

    def confirm(self, server, tool, arguments):
        self.asked.append((server, tool, arguments))
        return self.approve

    def answer(self, text):
        self.answers.append(text)

    def warning(self, text):
        self.warnings.append(text)

    def tool_finished(self, server, tool, seconds, ok, summary):
        self.finished.append((tool, ok, summary))


@pytest.fixture
def manager() -> FakeManager:
    return FakeManager(
        {
            "get_account": "fraud-local",
            "list_transactions": "fraud-local",
            "freeze_card": "fraud-local",
            "open_dispute": "fraud-local",
            "git_status": "git",
            "git_commit": "git",
        }
    )


def build(llm, manager, view=None, **kwargs) -> Agent:
    session = Session(system_prompt=build_system_prompt("", str(manager.workspace)))
    return Agent(llm, manager, session, view=view or RecordingView(), **kwargs)


def responses_in(content: types.Content) -> list[dict]:
    return [part.function_response.response for part in content.parts if part.function_response]


# --- The loop ------------------------------------------------------------------------


def test_a_question_without_tools_is_answered_from_the_model(manager: FakeManager):
    """The agent must be able to answer from its own training, tools or no tools."""
    llm = ScriptedLlm(says("Alan Turing was a British mathematician."))
    agent = build(llm, manager)

    answer = agent.ask("Who was Alan Turing?")

    assert answer.startswith("Alan Turing")
    assert manager.calls == []
    assert agent.view.answers == [answer]


def test_the_loop_runs_the_tool_and_comes_back(manager: FakeManager):
    llm = ScriptedLlm(
        model_turn(asks("get_account", account_id=DEMO_ACCOUNT)),
        says("Account 4471 belongs to Micheal Walters."),
    )
    agent = build(llm, manager)

    answer = agent.ask("Look up account 4471")

    assert manager.calls == [("get_account", {"account_id": DEMO_ACCOUNT})]
    assert "Micheal Walters" in answer
    # The second request carried the result back: user, model, tool results.
    assert [content.role for content in llm.seen[1]] == ["user", "model", "user"]
    assert responses_in(llm.seen[1][2])[0]["isError"] is False


def test_a_batch_of_calls_travels_in_a_single_turn(manager: FakeManager):
    """Splitting them across turns teaches the model to stop asking for parallel calls."""
    llm = ScriptedLlm(
        model_turn(
            asks("get_account", "a", account_id=DEMO_ACCOUNT),
            asks("list_transactions", "b", account_id=DEMO_ACCOUNT),
            asks("git_status", "c", repo_path="."),
        ),
        says("Here is the picture."),
    )
    agent = build(llm, manager)

    agent.ask("Give me everything about 4471")

    assert len(manager.calls) == 3
    results = llm.seen[1][-1]
    assert results.role == "user"
    assert len(responses_in(results)) == 3, "the three results must share one turn"


def test_a_refusal_from_a_tool_reaches_the_model(manager: FakeManager):
    """A business refusal is a result, and the model has to see it to correct itself."""
    manager.results["open_dispute"] = {
        "content": [{"type": "text", "text": "Past the 120 day claim window."}],
        "isError": True,
    }
    llm = ScriptedLlm(
        model_turn(asks("open_dispute", transaction_ids=["txn_1"], reason="stale charge")),
        says("That charge is too old to dispute."),
    )
    agent = build(llm, manager)

    agent.ask("Dispute that old charge")

    returned = responses_in(llm.seen[1][-1])[0]
    assert returned["isError"] is True
    assert "claim window" in returned["content"]


def test_an_unknown_tool_comes_back_as_a_result(manager: FakeManager):
    llm = ScriptedLlm(model_turn(asks("teleport")), says("I cannot do that."))
    agent = build(llm, manager)

    agent.ask("teleport me")

    returned = responses_in(llm.seen[1][-1])[0]
    assert returned["isError"] is True
    assert "No server offers" in returned["content"]


def test_a_loop_that_never_settles_is_cut_off(manager: FakeManager):
    """Every round is a request; on a free tier a runaway loop is not free."""
    forever = [model_turn(asks("get_account", account_id=DEMO_ACCOUNT)) for _ in range(30)]
    llm = ScriptedLlm(*forever)
    agent = build(llm, manager, max_rounds=4)

    answer = agent.ask("go in circles")

    assert answer == ""
    assert len(manager.calls) == 4
    assert "4 rounds" in agent.view.warnings[0]


def test_the_context_survives_between_turns(manager: FakeManager):
    """Memory between questions (the second question refers to the first)."""
    llm = ScriptedLlm(says("Alan Turing was a British mathematician."), says("23 June 1912."))
    agent = build(llm, manager)

    agent.ask("Who was Alan Turing?")
    agent.ask("What date was he born?")

    sent = llm.seen[1]
    assert [content.role for content in sent] == ["user", "model", "user"]
    assert "Who was Alan Turing?" in sent[0].parts[0].text
    assert "British mathematician" in sent[1].parts[0].text


# --- The two guards ------------------------------------------------------------------


def test_the_freeze_that_takes_effect_asks_the_analyst(manager: FakeManager):
    llm = ScriptedLlm(
        model_turn(asks("freeze_card", account_id=DEMO_ACCOUNT, reason="geovelocity", confirmation_token="abc")),
        says("Card frozen."),
    )
    view = RecordingView(approve=True)
    agent = build(llm, manager, view)

    agent.ask("freeze it")

    assert [tool for _server, tool, _args in view.asked] == ["freeze_card"]
    assert manager.calls[0][0] == "freeze_card"


def test_the_first_freeze_call_is_not_worth_interrupting_for(manager: FakeManager):
    """Without a token it only mints one; there is nothing yet for the analyst to approve."""
    llm = ScriptedLlm(
        model_turn(asks("freeze_card", account_id=DEMO_ACCOUNT, reason="geovelocity")),
        says("Here is the token."),
    )
    view = RecordingView()
    agent = build(llm, manager, view)

    agent.ask("freeze it")

    assert view.asked == []
    assert manager.calls[0][0] == "freeze_card"


def test_a_declined_action_is_a_result_and_not_a_crash(manager: FakeManager):
    llm = ScriptedLlm(
        model_turn(asks("open_dispute", transaction_ids=["txn_1"], reason="fraudulent charge")),
        says("Understood, I will not open it."),
    )
    view = RecordingView(approve=False)
    agent = build(llm, manager, view)

    agent.ask("open the dispute")

    assert manager.calls == [], "a declined tool must not run"
    returned = responses_in(llm.seen[1][-1])[0]
    assert returned["isError"] is True
    assert "declined" in returned["content"]


def test_git_outside_the_workspace_is_refused(manager: FakeManager):
    """The boundary the configuration declares and the git server cannot enforce."""
    llm = ScriptedLlm(
        model_turn(asks("git_commit", repo_path="/home/sebas/github/fraud-detection-mcp")),
        says("I cannot commit there."),
    )
    agent = build(llm, manager)

    agent.ask("commit the project")

    assert manager.calls == [], "the call must never reach the server"
    returned = responses_in(llm.seen[1][-1])[0]
    assert returned["isError"] is True
    assert "outside the workspace" in returned["content"]


@pytest.mark.parametrize("repo_path", [".", "demo", "demo/nested", "/tmp/workspace/demo"])
def test_git_inside_the_workspace_goes_through(manager: FakeManager, repo_path: str):
    llm = ScriptedLlm(model_turn(asks("git_status", repo_path=repo_path)), says("clean"))
    agent = build(llm, manager)

    agent.ask("status")

    assert manager.calls == [("git_status", {"repo_path": repo_path})]


def test_the_boundary_only_applies_to_git(manager: FakeManager):
    llm = ScriptedLlm(model_turn(asks("get_account", repo_path="/etc")), says("ok"))
    agent = build(llm, manager)

    agent.ask("look")

    assert manager.calls, "a repo_path on another server's tool is none of our business"


# --- The declarations ----------------------------------------------------------------


def test_the_schema_keeps_what_the_model_reads_and_drops_the_rest():
    trimmed = simplify_schema(
        {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Four digits", "minLength": 1},
                "days": {"type": "integer", "minimum": 1, "default": 7},
                "channel": {"type": "string", "enum": ["online", "in_person"]},
                "ids": {"type": "array", "items": {"type": "string", "maxLength": 64}},
            },
            "required": ["account_id"],
            "additionalProperties": False,
        }
    )

    # What the model needs to fill the call correctly stays.
    assert trimmed["required"] == ["account_id"]
    assert trimmed["properties"]["account_id"]["description"] == "Four digits"
    assert trimmed["properties"]["channel"]["enum"] == ["online", "in_person"]
    assert trimmed["properties"]["ids"]["items"]["type"] == "string"
    # The constraints go: the server checks them again on arrival and fills the defaults.
    assert "additionalProperties" not in trimmed
    assert trimmed["properties"]["days"] == {"type": "integer"}
    assert "minLength" not in trimmed["properties"]["account_id"]


def test_the_catalogue_becomes_one_tool_declaration(manager: FakeManager):
    tools = declare(manager.tools)

    assert len(tools) == 1
    declared = tools[0].function_declarations
    assert [item.name for item in declared] == list(manager.owners)
    # The catalogue's own marker is not part of a declaration.
    assert all(item.parameters_json_schema.get("type") == "object" for item in declared)


def test_no_tools_means_no_declarations():
    assert declare([]) == []
