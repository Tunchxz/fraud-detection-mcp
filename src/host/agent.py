"""The conversation loop: the model asks for tools, this runs them and hands back results.

The loop is written out rather than delegated to the SDK's automatic function calling, so
the model instructs and the host executes. It also keeps the two error worlds apart, which
is what lets the model correct itself.

Nothing here draws anything. The agent reports what it is doing to a view, so the terminal
interface can be swapped or, in the tests, be silent.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

from google.genai import types

from host.client.base import McpClientError, failed
from host.client.manager import ClientManager
from host.llm import Llm, declare
from host.session import Session

# A conversation that has not settled after this many rounds of tools is looping. The cap
# is about the free tier as much as about sanity: every round is a request.
#
# Twenty is preferred to a dozen because the fraud investigation measured about six rounds
# when the model groups its independent calls and three times that when it does not, and being
# cut off mid-investigation is worse than one round too many.
MAX_ROUNDS = 20

# Tools of the fraud server whose effect the analyst confirms first. `freeze_card` appears
# here but is only gated on its second call (the first one merely mints a token).
CONFIRMED_TOOLS = frozenset({"freeze_card", "open_dispute"})


class View:
    """What the agent tells the outside world."""

    def thinking(self):
        return contextlib.nullcontext()

    def tool_started(self, server: str, tool: str, arguments: dict) -> None:
        pass

    def tool_finished(
        self, server: str, tool: str, seconds: float, ok: bool, summary: str
    ) -> None:
        pass

    def confirm(self, server: str, tool: str, arguments: dict) -> bool:
        return True

    def answer(self, text: str) -> None:
        pass

    def warning(self, text: str) -> None:
        pass


def text_of(response: types.GenerateContentResponse) -> str:
    """Every text part of an answer, joined. Read part by part because a turn that also
    asks for tools has both kinds, and the SDK's shortcut doesn't handle that."""
    pieces = []
    for candidate in response.candidates or []:
        for part in (candidate.content.parts if candidate.content else None) or []:
            if part.text:
                pieces.append(part.text)
    return "\n".join(pieces).strip()


def summarise(result: dict) -> str:
    """The first line of what a tool answered, for the one-line trace in the interface."""
    for block in result.get("content") or []:
        text = block.get("text")
        if text:
            return text.splitlines()[0]
    return ""


class Agent:
    """One analyst, one model, every tool of every connected server."""

    def __init__(
        self,
        llm: Llm,
        manager: ClientManager,
        session: Session,
        view: View | None = None,
        workspace: Path | str | None = None,
        max_rounds: int = MAX_ROUNDS,
    ) -> None:
        self.llm = llm
        self.manager = manager
        self.session = session
        self.view = view or View()
        self.workspace = Path(workspace or manager.workspace).resolve()
        self.max_rounds = max_rounds
        self.tools = declare(manager.tools)

    def ask(self, message: str) -> str:
        """One turn of the analyst, however many rounds of tools it takes."""
        self.session.add_user(message)

        for _round in range(self.max_rounds):
            with self.view.thinking():
                response = self.llm.respond(
                    self.session.history, self.session.system_prompt, self.tools
                )

            candidate = (response.candidates or [None])[0]
            self.session.add_model(candidate.content if candidate else None)

            calls = response.function_calls or []
            if not calls:
                answer = text_of(response)
                if not answer:
                    # A turn with neither text nor tools means the answer was cut short.
                    reason = getattr(candidate, "finish_reason", None)
                    self.view.warning(f"The model returned nothing ({reason}).")
                self.view.answer(answer)
                return answer

            self.session.add_tool_results([self.run(call) for call in calls])

        self.view.warning(
            f"Stopped after {self.max_rounds} rounds of tools without a final answer."
        )
        return ""

    # --- One tool call -----------------------------------------------------------------

    def run(self, call: types.FunctionCall) -> types.Part:
        """Runs one requested tool and packages its result for the model.

        Everything that can go wrong comes back as a result rather than an exception,
        because the model has to see it to do anything about it.
        """
        name = call.name or ""
        arguments = dict(call.args or {})

        try:
            client, original = self.manager.route(name)
        except McpClientError as error:
            return self.answer_with(call, True, str(error))
        server = client.name

        outside = self.outside_workspace(server, arguments)
        if outside:
            self.view.tool_finished(server, name, 0.0, False, outside)
            return self.answer_with(call, True, outside)

        if self.needs_confirmation(server, original, arguments) and not self.view.confirm(
            server, name, arguments
        ):
            declined = "The analyst declined this action. Explain why it was proposed and stop."
            self.view.tool_finished(server, name, 0.0, False, "declined by the analyst")
            return self.answer_with(call, True, declined)

        self.view.tool_started(server, name, arguments)
        started = time.monotonic()
        try:
            result = self.manager.call(name, arguments)
        except McpClientError as error:
            elapsed = time.monotonic() - started
            self.view.tool_finished(server, name, elapsed, False, str(error))
            return self.answer_with(call, True, str(error))

        elapsed = time.monotonic() - started
        broke = failed(result)
        self.view.tool_finished(server, name, elapsed, not broke, summarise(result))
        return self.answer_with(call, broke, summarise(result), result)

    def answer_with(
        self, call: types.FunctionCall, broke: bool, text: str, result: dict | None = None
    ) -> types.Part:
        """Builds the function response the next request carries back."""
        payload: dict = {"isError": broke}
        if result is not None:
            payload["content"] = "\n".join(
                block.get("text", "") for block in result.get("content") or []
            )
            if "structuredContent" in result:
                payload["data"] = result["structuredContent"]
        else:
            payload["content"] = text

        response = types.FunctionResponse(
            id=call.id, name=call.name, response=payload
        )
        return types.Part(function_response=response)

    # --- The two guards ----------------------------------------------------------------

    def needs_confirmation(self, server: str, tool: str, arguments: dict) -> bool:
        """Whether the analyst has to approve this call before it happens.

        Only the fraud server's writing tools, and `freeze_card` only on the call that
        actually freezes (without a token it just mints one).
        """
        if not server.startswith("fraud") or tool not in CONFIRMED_TOOLS:
            return False
        if tool == "freeze_card":
            return bool(arguments.get("confirmation_token"))
        return True

    def outside_workspace(self, server: str, arguments: dict) -> str:
        """Refuses a git call aimed outside the workspace. Empty string means it is fine.

        The git server cannot be confined (its tools take a `repo_path` on every call, and
        it refuses to start when `--repository` is not already a repository), so the
        boundary its configuration declares is enforced here instead. Relative paths are
        resolved the same way the server does it: against the directory it was launched in.
        """
        raw = arguments.get("repo_path")
        if server != "git" or not raw:
            return ""

        target = Path(raw)
        target = (target if target.is_absolute() else self.workspace / target).resolve()
        if target == self.workspace or self.workspace in target.parents:
            return ""
        return (
            f"Refused: {target} is outside the workspace ({self.workspace}). "
            "Git may only be used inside the workspace directory."
        )
