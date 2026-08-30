"""HTTP transport for the client: `POST /mcp` against the remote server.

What it has to handle beyond sending a body is everything HTTP does not do by itself:

- `X-API-Key`, the shared secret the remote endpoint requires;
- `Mcp-Session-Id`, handed out by `initialize` and repeated from then on;
- `202` with no body, which is how a notification is accepted;
- `404`, which means the session is gone: the agreed signal to shake hands again.
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from mcp_core.msglog import MessageLog

from host.client.base import McpClient, McpTransportError

# A sleeping free-tier service (like the one I'm using) takes about
# a minute to wake up.
DEFAULT_TIMEOUT = 90.0

SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"
API_KEY_HEADER = "X-API-Key"


class HttpClient(McpClient):
    """The remote server, reached over one reusable HTTP/1.1 connection."""

    def __init__(
        self,
        name: str,
        log: MessageLog,
        url: str,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        keylog: str | None = None,
    ) -> None:
        super().__init__(name, log)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise McpTransportError(f"{name}: {url!r} is not an http(s) URL")

        self.url = url
        self.secure = parts.scheme == "https"
        self.host = parts.hostname
        self.port = parts.port
        self.path = parts.path or "/mcp"
        self.api_key = api_key or None
        self.timeout = timeout
        self.keylog = keylog if keylog is not None else os.environ.get("SSLKEYLOGFILE")

        self.session_id: str | None = None
        self.connection: http.client.HTTPConnection | None = None
        self._recovering = False

    # --- Transport ---------------------------------------------------------------------

    def _open(self) -> None:
        self._connect_socket()
        self.log.note(f"endpoint {self.url}" + (" · TLS keys exported" if self._exports_keys else ""))

    @property
    def _exports_keys(self) -> bool:
        return bool(self.secure and self.keylog)

    def _connect_socket(self) -> None:
        if self.connection is not None:
            self.connection.close()

        if self.secure:
            context = ssl.create_default_context()
            if self.keylog:
                # The whole Wireshark analysis hangs off this line: the client writes the
                # session secrets and Wireshark uses them to decrypt its own capture.
                Path(self.keylog).expanduser().parent.mkdir(parents=True, exist_ok=True)
                context.keylog_filename = str(Path(self.keylog).expanduser())
            self.connection = http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout, context=context
            )
        else:
            self.connection = http.client.HTTPConnection(
                self.host, self.port, timeout=self.timeout
            )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.api_key:
            headers[API_KEY_HEADER] = self.api_key
        # Absent on the first handshake, and cleared before a new one, which is exactly
        # when the server expects it to be missing.
        if self.session_id:
            headers[SESSION_HEADER] = self.session_id
        if self.protocol_version:
            headers[PROTOCOL_HEADER] = self.protocol_version
        return headers

    def _post(self, raw: str) -> tuple[int, http.client.HTTPMessage, str]:
        """One round trip, reconnecting once if the kept-alive connection went stale."""
        for attempt in (1, 2):
            if self.connection is None:
                self._connect_socket()
            try:
                assert self.connection is not None
                self.connection.request(
                    "POST", self.path, body=raw.encode("utf-8"), headers=self._headers()
                )
                response = self.connection.getresponse()
                body = response.read().decode("utf-8")
                return response.status, response.headers, body
            except (http.client.HTTPException, OSError, socket.timeout) as error:
                self.connection = None
                if attempt == 2:
                    raise McpTransportError(f"{self.name}: {error}") from error
                self.log.note(f"connection lost ({error}); reconnecting")
        raise McpTransportError(f"{self.name}: unreachable")  # pragma: no cover

    def _exchange(self, raw: str, expects_reply: bool) -> str | None:
        status, headers, body = self._post(raw)

        if status == 404 and not self._recovering:
            # The session no longer exists. Shake hands again and replay the message once;
            # its identifier is unchanged, so the answer still correlates.
            self.log.note("the server does not know this session; handshaking again")
            self._recovering = True
            try:
                self.session_id = None
                self.connect()
            finally:
                self._recovering = False
            status, headers, body = self._post(raw)

        if status == 401:
            raise McpTransportError(
                f"{self.name} refused the shared secret; check FRAUD_MCP_API_KEY"
            )
        if status == 202:
            return None  # notification accepted, and notifications carry no answer
        if status != 200:
            raise McpTransportError(f"{self.name} answered {status}: {body[:200]}")

        handed_out = headers.get(SESSION_HEADER)
        if handed_out and handed_out != self.session_id:
            self.session_id = handed_out
            self.log.note(f"session {handed_out}")

        if not expects_reply:
            return None
        return body

    def _close(self) -> None:
        if self.connection is None:
            return
        if self.session_id:
            try:
                self.connection.request("DELETE", self.path, headers=self._headers())
                self.connection.getresponse().read()
            except (http.client.HTTPException, OSError):
                pass
        self.connection.close()
        self.connection = None
        self.log.note("closed")
