"""The conversation: what the model is sent, and what the analyst has been told.

The context within a session is kept here rather than at the provider; since the API is
stateless, every turn carries the whole history back.

The history is stored as the SDK's own `Content` objects because that list is the
conversation: a model turn that asks for three tools and the user turn that answers with
three results are the same structure the API expects back, and translating them into
something neutral and back again would only produce bugs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from google.genai import types

# How the assistant is told to behave. The tools describe themselves, and the servers
# describe their own workflow, so this only covers what neither of them can say.
PERSONA = """\
You are the assistant of a bank fraud monitoring analyst, working in a terminal. The data \
is simulated: no real cardholder or card is involved.

How to work:
- Reach for the tools rather than guessing. When a question can be answered from the tools, \
answer from what they return, not from memory.
- Chain them: the result of one usually tells you what to ask next.
- Ask for independent calls together, in the same turn. Scoring six transactions is one turn with six calls, not six turns: every turn is a request, and the free tier counts them.
- When you report a decision, quote the rules that triggered it and the evidence behind \
them. An analyst has to justify a block to a customer and to a regulator.
- If a tool refuses, read the reason and act on it. A refusal is information, not a wall to \
push against: never try the same call again unchanged.
- Answer in the language the analyst writes in. Be brief; this is a terminal, not a report.

Questions unrelated to the investigation are fine to answer from your own knowledge."""

WORKSPACE_RULE = """\
Files and git: you may only work inside the workspace directory shown above. Never touch the \
repository that contains this project, and never run git against a path outside the \
workspace."""


def build_system_prompt(server_instructions: str, workspace: str) -> str:
    """The persona, what the servers said about themselves, and the workspace boundary."""
    pieces = [PERSONA, f"Workspace directory: {workspace}", WORKSPACE_RULE]
    if server_instructions:
        pieces.insert(1, f"What the connected servers say about themselves:\n\n{server_instructions}")
    return "\n\n".join(pieces)


@dataclass
class Session:
    """One conversation. Everything the model needs to keep its train of thought."""

    system_prompt: str
    history: list[types.Content] = field(default_factory=list)
    turns: int = 0

    def add_user(self, text: str) -> None:
        self.history.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
        self.turns += 1

    def add_model(self, content: types.Content | None) -> None:
        """Stores the model's turn, tool requests and all.

        It goes back verbatim: the identifiers of the calls it asked for have to match the
        results answering them, so rebuilding this by hand would break the pairing.
        """
        if content is not None:
            self.history.append(content)

    def add_tool_results(self, parts: list[types.Part]) -> None:
        """Answers a whole batch of tool calls in a single turn.

        One turn, not one per result. Splitting them teaches the model that its parallel
        calls are not welcome, and it stops asking for them.
        """
        if parts:
            self.history.append(types.Content(role="user", parts=parts))

    def reset(self) -> None:
        self.history.clear()
        self.turns = 0

    @property
    def messages(self) -> int:
        return len(self.history)
