"""The connection to the LLM, at the level of its API.

This is the module in the host that knows which model provider is in use; everything above
it works with the session's history and the tool catalogue and never imports the SDK.

The SDK is Google's `google-genai`. Note: this SDK provides MCP helpers (`google.genai._mcp_utils`),
which would hand it our servers directly, but they are never used.

Automatic function calling is switched off explicitly. The SDK can run tools by itself when
given Python callables, but for our purposes, the loop is ours.
"""

from __future__ import annotations

import os

from google import genai
from google.genai import types

# The current flash models allow twenty requests a day on the free tier. A single fraud
# investigation spends most of that. The lite tier is more generous and handles the
# investigation fine. For a demonstration, `--model gemini-3.5-flash` reasons harder,
# and twenty requests is plenty for one run.
DEFAULT_MODEL = "gemini-3.1-flash-lite"

# The free tier answers 503 when the model is busy and 429 when too many requests land in
# one minute. The SDK retries for us once it is told which codes are worth it. The budget
# is deliberately generous (roughly a minute and a half across six attempts) because the
# free tier's limit is a per-minute window.
RETRY = types.HttpRetryOptions(
    attempts=6,
    initial_delay=2.0,
    max_delay=30.0,
    http_status_codes=[429, 500, 502, 503, 504],
)

# What survives into a Gemini function declaration. The rest of JSON Schema is dropped: the
# dialect accepted here is narrower than the one MCP servers write, and nothing is lost by
# it, because the server validates every argument again when the call arrives. Keeping
# `description` and `enum` matters (they are what the model reads to fill the call).
SCHEMA_KEYWORDS = frozenset(
    {"type", "description", "properties", "required", "items", "enum", "format", "title"}
)


class LlmError(Exception):
    """Something went wrong talking to the model, said in words a user can act on."""


def simplify_schema(schema: dict) -> dict:
    """Trims a JSON Schema down to what a function declaration accepts.

    Constraint keywords (`minLength`, `maximum`, `additionalProperties`, `default`, …) go
    away. They are the server's job: `mcp_core.registry.validate` checks them on arrival and
    fills the defaults, so a model that overshoots gets a proper `-32602` back and can
    correct itself.
    """
    trimmed: dict = {}
    for key, value in schema.items():
        if key not in SCHEMA_KEYWORDS:
            continue
        if key == "properties" and isinstance(value, dict):
            trimmed[key] = {name: simplify_schema(sub) for name, sub in value.items()}
        elif key == "items" and isinstance(value, dict):
            trimmed[key] = simplify_schema(value)
        else:
            trimmed[key] = value
    return trimmed


def declare(catalogue: list[dict]) -> list[types.Tool]:
    """Turns the manager's aggregated catalogue into one tool declaration list.

    The catalogue entries carry a `"type": "function"` marker that belongs to the catalogue,
    not to the declaration, so only name, description and parameters cross over.
    """
    declarations = [
        types.FunctionDeclaration(
            name=entry["name"],
            description=entry["description"],
            parameters_json_schema=simplify_schema(entry["parameters"]),
        )
        for entry in catalogue
    ]
    return [types.Tool(function_declarations=declarations)] if declarations else []


class Llm:
    """The model, asked one question at a time with the whole conversation attached."""

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise LlmError(
                "No API key. Put GEMINI_API_KEY in .env (see .env.example); a free key comes "
                "from https://aistudio.google.com"
            )
        self.client = genai.Client(
            api_key=key, http_options=types.HttpOptions(retry_options=RETRY)
        )

    def respond(
        self, history: list[types.Content], system_prompt: str, tools: list[types.Tool]
    ) -> types.GenerateContentResponse:
        """One turn: the whole history in, one answer out, tool requests included."""
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=tools,
            # The loop is ours. Without this the SDK may try to run tools on its own.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            return self.client.models.generate_content(
                model=self.model, contents=history, config=config
            )
        except genai.errors.ClientError as error:
            raise LlmError(self._explain(error)) from error
        except genai.errors.ServerError as error:
            raise LlmError(
                f"The model is unavailable after {RETRY.attempts} attempts ({error.code}). "
                "This is on Google's side; try again in a moment."
            ) from error
        except genai.errors.APIError as error:
            raise LlmError(f"The model API failed: {error}") from error

    def _explain(self, error: genai.errors.ClientError) -> str:
        """Turns the SDK's 4xx into something the analyst can do something about."""
        code = getattr(error, "code", None)
        if code == 429:
            return "The free tier is rate limited right now. Wait a moment and try again."
        if code in (401, 403):
            return "The API key was refused. Check GEMINI_API_KEY in .env."
        if code == 404:
            return f"The model {self.model!r} does not exist. Available: {self.available()}"
        return f"The model API refused the request: {error}"

    def available(self) -> str:
        """The models this key can actually use, for when the configured one is wrong."""
        try:
            names = [model.name for model in self.client.models.list()]
        except Exception:  # noqa: BLE001 - this runs while reporting another error
            return "could not be listed"
        return ", ".join(sorted(name.removeprefix("models/") for name in names)[:12])
