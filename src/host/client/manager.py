"""Loads the server list, opens a client for each one, and routes tool calls.

Key behaviours:

- A server that cannot be opened is reported and skipped, not fatal.
- Names are left exactly as each server publishes them unless two servers publish the
  same one, in which case both get their server's key as a prefix.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from mcp_core.msglog import MessageLog

from host.client.base import McpClient, McpClientError
from host.client.http_client import HttpClient
from host.client.stdio_client import StdioClient

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = REPO_ROOT / "config" / "servers.json"
DEFAULT_WORKSPACE = REPO_ROOT / "workspace"
DEFAULT_LOG = REPO_ROOT / "logs" / "mcp-session.log"


@dataclass(frozen=True)
class ServerSpec:
    """One entry of `config/servers.json`, with its variables already resolved."""

    key: str
    transport: str
    description: str = ""
    command: list[str] = field(default_factory=list)
    cwd: str = ""
    url: str = ""
    api_key: str | None = None
    enabled: bool = True


def _resolve(value: str, variables: dict[str, str]) -> str:
    """Expands `${VARIABLES}`. A missing one raises, which is how a server gets skipped."""
    return Template(value).substitute(variables)


def load_specs(
    path: Path | str | None = None,
    workspace: Path | str | None = None,
    select: Iterable[str] | None = None,
) -> tuple[list[ServerSpec], list[tuple[str, str]]]:
    """Reads the configuration. Returns the usable servers and why the rest were left out.

    `select` opens only the servers named. The fraud server is one server on two
    transports, so loading both at once would publish its six tools twice under prefixed
    names; the host picks a transport instead, and the tools keep the names the
    specification documents.
    """
    wanted = set(select) if select is not None else None
    location = Path(path) if path is not None else DEFAULT_CONFIG
    document = json.loads(location.read_text(encoding="utf-8"))

    variables = dict(os.environ)
    variables["REPO_ROOT"] = str(REPO_ROOT)
    variables["WORKSPACE"] = str(Path(workspace) if workspace is not None else DEFAULT_WORKSPACE)

    specs: list[ServerSpec] = []
    skipped: list[tuple[str, str]] = []

    for key, entry in document.get("servers", {}).items():
        if wanted is not None and key not in wanted:
            continue
        if not entry.get("enabled", True):
            skipped.append((key, "disabled in config/servers.json"))
            continue
        try:
            transport = entry["transport"]
            command = [_resolve(part, variables) for part in entry.get("command", [])]
            cwd = _resolve(entry["cwd"], variables) if entry.get("cwd") else ""
            url = _resolve(entry["url"], variables) if entry.get("url") else ""
            api_key_env = entry.get("api_key_env")
            api_key = os.environ.get(api_key_env) if api_key_env else None
        except KeyError as missing:
            # An unset variable is the usual case for the remote server before deployment.
            skipped.append((key, f"needs {missing.args[0]}, which is not set"))
            continue

        specs.append(
            ServerSpec(
                key=key,
                transport=transport,
                description=entry.get("description", ""),
                command=command,
                cwd=cwd,
                url=url,
                api_key=api_key,
            )
        )

    return specs, skipped


def _slug(key: str) -> str:
    """A server key made safe to use inside a tool name."""
    return re.sub(r"[^0-9A-Za-z_]", "_", key)


class ClientManager:
    """Every open server, the catalogue they add up to, and the routing between the two."""

    def __init__(
        self,
        specs: list[ServerSpec],
        log_path: Path | str | None = None,
        sink=None,
        skipped: list[tuple[str, str]] | None = None,
    ) -> None:
        self.specs = specs
        self.log_path = Path(log_path) if log_path is not None else DEFAULT_LOG
        self.sink = sink

        self.workspace = DEFAULT_WORKSPACE
        self.clients: dict[str, McpClient] = {}
        self.failures: list[tuple[str, str]] = list(skipped or [])
        self.tools: list[dict] = []
        self._routes: dict[str, tuple[McpClient, str]] = {}

    @classmethod
    def from_config(
        cls,
        path: Path | str | None = None,
        workspace: Path | str | None = None,
        log_path: Path | str | None = None,
        sink=None,
        select: Iterable[str] | None = None,
    ) -> ClientManager:
        specs, skipped = load_specs(path, workspace, select)
        manager = cls(specs, log_path=log_path, sink=sink, skipped=skipped)
        manager.workspace = Path(workspace) if workspace is not None else DEFAULT_WORKSPACE
        return manager

    # --- Opening -----------------------------------------------------------------------

    def open(self) -> None:
        """Connects to every server, collecting the ones that fail instead of stopping."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # The filesystem server refuses to start if its allowed root is missing, and the
        # git server needs somewhere to be launched from.
        self.workspace.mkdir(parents=True, exist_ok=True)

        for spec in self.specs:
            log = MessageLog(path=str(self.log_path), label=spec.key, sink=self.sink)
            try:
                client = self._build(spec, log)
                client.connect()
            except McpClientError as error:
                self.failures.append((spec.key, str(error)))
                log.note(f"unavailable: {error}")
                continue
            self.clients[spec.key] = client

        self._build_catalogue()

    def _build(self, spec: ServerSpec, log: MessageLog) -> McpClient:
        if spec.transport == "stdio":
            # A server launched inside the workspace resolves its relative paths there,
            # which is what keeps the git server away from this repository.
            return StdioClient(spec.key, log, spec.command, cwd=spec.cwd or REPO_ROOT)
        if spec.transport == "http":
            return HttpClient(spec.key, log, spec.url, api_key=spec.api_key)
        raise McpClientError(f"{spec.key}: unknown transport {spec.transport!r}")

    def _build_catalogue(self) -> None:
        """Turns every server's tools into one list of function declarations."""
        self.tools = []
        self._routes = {}

        published = Counter(
            tool["name"] for client in self.clients.values() for tool in client.tools
        )

        for key, client in self.clients.items():
            for tool in client.tools:
                name = tool["name"]
                exposed = name if published[name] == 1 else f"{_slug(key)}__{name}"
                self._routes[exposed] = (client, name)
                self.tools.append(
                    {
                        "type": "function",
                        "name": exposed,
                        "description": tool.get("description")
                        or tool.get("title")
                        or f"{name} ({key})",
                        "parameters": tool.get("inputSchema")
                        or {"type": "object", "properties": {}},
                    }
                )

    # --- Using -------------------------------------------------------------------------

    def route(self, name: str) -> tuple[McpClient, str]:
        """Which server owns a tool, and what it calls it."""
        if name not in self._routes:
            raise McpClientError(f"No server offers a tool called {name!r}")
        return self._routes[name]

    def call(self, name: str, arguments: dict) -> dict:
        client, original = self.route(name)
        return client.call_tool(original, arguments)

    def server_of(self, name: str) -> str:
        return self.route(name)[0].name

    def instructions(self) -> str:
        """What the servers said about themselves, for the system prompt."""
        pieces = [
            f"{client.title}: {client.instructions}"
            for client in self.clients.values()
            if client.instructions
        ]
        return "\n\n".join(pieces)

    def close(self) -> None:
        for client in self.clients.values():
            try:
                client.close()
            except McpClientError:
                pass
        self.clients = {}
        self._routes = {}
        self.tools = []

    def __enter__(self) -> ClientManager:
        self.open()
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()
