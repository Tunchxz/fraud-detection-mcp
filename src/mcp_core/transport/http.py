"""HTTP transport.

The requirements of the MCP Streamable HTTP transport, all enforced here:

- a single endpoint path serves POST and GET;
- a JSON-RPC request is answered with `Content-Type: application/json` and one object;
- a JSON-RPC notification is answered with 202 Accepted and no body;
- the session identifier travels in the `Mcp-Session-Id` header of the `initialize` response,
  and the client repeats it on every later request;
- a `GET` on the endpoint answers 405, which is how a server declares it offers no SSE stream;
- the `Origin` header is validated.

Two things served here are not MCP:

- `GET /health`, which exists for the server's health check and tests (answers without
  authentication).
- The `X-API-Key` check.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from mcp_core import jsonrpc
from mcp_core.jsonrpc import INVALID_REQUEST, JsonRpcError, Request
from mcp_core.msglog import MessageLog
from mcp_core.protocol import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpSession,
    ServerInfo,
)
from mcp_core.registry import ToolRegistry

MCP_PATH = "/mcp"
HEALTH_PATH = "/health"

SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"
API_KEY_HEADER = "X-API-Key"

JSON_TYPE = "application/json"

# The largest tools/call payload of this server is a few hundred bytes, so anything near this
# size is a mistake or an abuse.
MAX_BODY_BYTES = 1 << 20

# A session whose client never disconnects is forgotten after this long without use.
SESSION_IDLE_SECONDS = 30 * 60


class SessionStore:
    """The live MCP sessions, keyed by the identifier that travels in `Mcp-Session-Id`."""

    def __init__(
        self,
        registry: ToolRegistry,
        info: ServerInfo,
        idle_seconds: float = SESSION_IDLE_SECONDS,
    ) -> None:
        self.registry = registry
        self.info = info
        self.idle_seconds = idle_seconds
        self._sessions: dict[str, McpSession] = {}
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def open(self) -> tuple[str, McpSession]:
        """Registers a new session and returns its identifier."""
        session = McpSession(self.registry, self.info)
        # Hexadecimal keeps the identifier inside the visible ASCII range the
        # specification demands.
        identifier = secrets.token_hex(16)

        with self._lock:
            self._evict()
            self._sessions[identifier] = session
            self._seen[identifier] = time.monotonic()
        return identifier, session

    def get(self, identifier: str) -> McpSession | None:
        with self._lock:
            session = self._sessions.get(identifier)
            if session is not None:
                self._seen[identifier] = time.monotonic()
            return session

    def drop(self, identifier: str) -> bool:
        """Terminates a session. False when it was not there to begin with."""
        with self._lock:
            self._seen.pop(identifier, None)
            return self._sessions.pop(identifier, None) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _evict(self) -> None:
        """Forgets the sessions nobody has used in a while. Called with the lock held."""
        deadline = time.monotonic() - self.idle_seconds
        for identifier in [key for key, seen in self._seen.items() if seen < deadline]:
            del self._sessions[identifier]
            del self._seen[identifier]


class HttpTransport:
    """Everything the request handler needs to turn a body into an answer."""

    def __init__(
        self,
        log: MessageLog,
        registry: ToolRegistry,
        info: ServerInfo,
        api_key: str | None = None,
        allowed_origins: Iterable[str] = (),
    ) -> None:
        self.log = log
        self.registry = registry
        self.info = info
        self.api_key = api_key or None
        self.allowed_origins = frozenset(allowed_origins)
        self.sessions = SessionStore(registry, info)

        # One lock for the whole dispatch. The tool context holds a single SQLite connection
        # and the message log a single stream, and neither is safe to use from two threads at
        # once.
        self._dispatch_lock = threading.Lock()

    def receive(self, raw: str) -> Request:
        """Decodes one inbound message, recording it first. Raises `JsonRpcError`."""
        self.log.inbound(raw)
        return jsonrpc.decode(raw)

    def dispatch(self, session: McpSession, request: Request) -> dict | None:
        with self._dispatch_lock:
            return session.handle(request)

    def encode(self, message: dict) -> bytes:
        """Serialises one outbound message, recording it first."""
        raw = jsonrpc.encode(message)
        self.log.outbound(raw)
        # `encode` does not escape non ASCII, so the length has to be measured in bytes.
        return raw.encode("utf-8")

    def authenticated(self, key: str | None) -> bool:
        if self.api_key is None:
            return True
        return key is not None and secrets.compare_digest(key, self.api_key)

    def origin_allowed(self, origin: str | None) -> bool:
        # No `Origin` at all is the normal case. Browsers add that header and the host of this
        # project is a terminal program. When one does arrive, it has to be on the list.
        return origin is None or origin in self.allowed_origins

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "server": self.info.name,
            "version": self.info.version,
            "protocol": LATEST_PROTOCOL_VERSION,
            "tools": len(self.registry.tools),
            "sessions": len(self.sessions),
        }


class McpRequestHandler(BaseHTTPRequestHandler):
    """Answers one HTTP request. The MCP meaning of the body is decided a layer above."""

    # HTTP/1.1 makes `Content-Length` mandatory and lets the connection be reused. It also
    # keeps the decrypted capture readable, without the binary framing of HTTP/2.
    protocol_version = "HTTP/1.1"

    @property
    def transport(self) -> HttpTransport:
        return self.server.transport  # type: ignore[attr-defined]

    def version_string(self) -> str:
        info = self.transport.info
        return f"{info.name}/{info.version}"

    # --- Routing ---------------------------------------------------------------------

    def route(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def do_GET(self) -> None:
        route = self.route()
        if route == HEALTH_PATH:
            self._send_json(200, self.transport.health())
        elif route == MCP_PATH:
            # This endpoint offers no stream of server initiated messages, and 405 is how the
            # specification says to declare exactly that.
            self._send_json(
                405,
                {"error": "This endpoint offers no SSE stream; send messages with POST"},
                headers={"Allow": "POST, DELETE"},
            )
        else:
            self._send_unknown_route()

    def do_DELETE(self) -> None:
        if self.route() != MCP_PATH:
            self._send_unknown_route()
            return
        if not self._allowed():
            return

        identifier = self.headers.get(SESSION_HEADER)
        if identifier and self.transport.sessions.drop(identifier):
            self._send_empty(204)
        else:
            self._send_json(404, {"error": "No such session"})

    def do_POST(self) -> None:
        if self.route() != MCP_PATH:
            self._discard_body()
            self._send_unknown_route()
            return
        if not self._allowed():
            return

        if self.headers.get_content_type() != JSON_TYPE:
            self._discard_body()
            self._send_json(415, {"error": f"Content-Type must be {JSON_TYPE}"})
            return

        body = self._read_body()
        if body is None:
            return

        try:
            raw = body.decode("utf-8")
        except UnicodeDecodeError:
            self._send_json(400, {"error": "The body must be UTF-8"})
            return

        # The negotiated version may also arrive as a header, and an unsupported one has to be
        # refused before the message is even looked at.
        version = self.headers.get(PROTOCOL_HEADER)
        if version is not None and version not in SUPPORTED_PROTOCOL_VERSIONS:
            self._send_json(400, {"error": f"Unsupported protocol version: {version}"})
            return

        try:
            request = self.transport.receive(raw)
        except JsonRpcError as error:
            # The one case where the identifier cannot be known, so it travels as null.
            self._send_message(400, jsonrpc.error_message(None, error))
            return

        session, identifier = self._resolve_session(request)
        if session is None:
            return

        response = self.transport.dispatch(session, request)
        if response is None:
            # A notification is never answered, and 202 is how HTTP says "taken, nothing back".
            self._send_empty(202, headers={SESSION_HEADER: identifier})
            return

        self._send_message(200, response, headers={SESSION_HEADER: identifier})

    # --- Session resolution ----------------------------------------------------------

    def _resolve_session(self, request: Request) -> tuple[McpSession | None, str]:
        """The session this message belongs to, answering the request itself when there is none."""
        identifier = self.headers.get(SESSION_HEADER)
        store = self.transport.sessions

        if identifier:
            session = store.get(identifier)
            if session is None:
                # 404 is the agreed signal for "that session is gone": the client is expected
                # to open a new one with an `initialize` that carries no identifier.
                self._send_message(
                    404,
                    jsonrpc.error_message(
                        request.id,
                        JsonRpcError(
                            INVALID_REQUEST,
                            "Unknown session: initialize again without a session id",
                        ),
                    ),
                )
                return None, ""
            return session, identifier

        if request.method == "initialize":
            identifier, session = store.open()
            return session, identifier

        self._send_message(
            400,
            jsonrpc.error_message(
                request.id,
                JsonRpcError(
                    INVALID_REQUEST,
                    f"Missing {SESSION_HEADER}: send 'initialize' first",
                ),
            ),
        )
        return None, ""

    # --- Guards ----------------------------------------------------------------------

    def _allowed(self) -> bool:
        """Origin and shared secret. Both are transport concerns, checked before anything else."""
        if not self.transport.origin_allowed(self.headers.get("Origin")):
            self._discard_body()
            self._send_json(403, {"error": "Origin not allowed"})
            return False

        if not self.transport.authenticated(self.headers.get(API_KEY_HEADER)):
            self._discard_body()
            self._send_json(401, {"error": f"A valid {API_KEY_HEADER} header is required"})
            return False

        return True

    def _discard_body(self) -> None:
        """Reads and throws away the body of a request that is being refused."""
        header = self.headers.get("Content-Length")
        if header is None:
            return  # no body announced, nothing to drain
        if not header.isdigit() or int(header) > MAX_BODY_BYTES:
            self.close_connection = True
            return
        self.rfile.read(int(header))

    def _read_body(self) -> bytes | None:
        """The whole body, or None when it was refused (the answer is already sent)."""
        header = self.headers.get("Content-Length")
        if header is None:
            self._send_json(411, {"error": "Content-Length is required"})
            return None

        try:
            length = int(header)
        except ValueError:
            self._send_json(400, {"error": "Content-Length is not a number"})
            return None

        if length > MAX_BODY_BYTES:
            # The body is not drained: the connection closes instead of reading megabytes
            # that are already known to be unwanted.
            self.close_connection = True
            self._send_json(413, {"error": f"The body may not exceed {MAX_BODY_BYTES} bytes"})
            return None

        return self.rfile.read(length)

    # --- Writing ---------------------------------------------------------------------

    def _send_message(self, status: int, message: dict, headers: dict | None = None) -> None:
        self._respond(status, self.transport.encode(message), headers)

    def _send_json(self, status: int, payload: dict, headers: dict | None = None) -> None:
        """Answers with a plain JSON body (health, and the refusals that never reach MCP)."""
        self._respond(status, jsonrpc.encode(payload).encode("utf-8"), headers)

    def _send_empty(self, status: int, headers: dict | None = None) -> None:
        self._respond(status, b"", headers)

    def _send_unknown_route(self) -> None:
        self._send_json(
            404, {"error": f"Unknown path: {self.path}. The MCP endpoint is {MCP_PATH}"}
        )

    def _respond(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(status)
        if body:
            self.send_header("Content-Type", JSON_TYPE)
        # Always announced, zero included (HTTP/1.1 needs it to reuse the connection).
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            if value:
                self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    # --- Logging ---------------------------------------------------------------------

    def log_request(self, code: str | int = "-", size: str | int = "-") -> None:
        # The message log already carries every MCP message; this line adds the HTTP status,
        # which is where the refusals of this layer (401, 404, 405) become visible. Health
        # checks are left out.
        if self.route() != HEALTH_PATH:
            self.transport.log.note(f"{self.command} {self.path} → {code}")

    def log_message(self, format: str, *args: Any) -> None:
        self.transport.log.note(format % args)


class McpHttpServer(ThreadingHTTPServer):
    """The threaded HTTP server that answers MCP messages."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], transport: HttpTransport) -> None:
        self.transport = transport
        super().__init__(address, McpRequestHandler)


def run(transport: HttpTransport, host: str, port: int) -> None:
    """Serves the endpoint until the process is interrupted."""
    httpd = McpHttpServer((host, port), transport)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
