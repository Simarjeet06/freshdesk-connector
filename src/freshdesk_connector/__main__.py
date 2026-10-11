"""Entry point: `freshdesk-connector [--transport stdio|http] [--port 8000]`."""

from __future__ import annotations

import argparse
import hmac
import logging
import sys

from .client import FreshdeskClient
from .config import Settings, load_dotenv
from .errors import ConfigError
from .server import mcp, set_client


class BearerAuthMiddleware:
    """Pure-ASGI guard for the HTTP transport. Agent Studio (or any MCP client) must send
    `Authorization: Bearer <CONNECTOR_AUTH_TOKEN>`. This protects the connector itself;
    the Freshdesk API key never leaves the server."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") != "/healthz":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Freshdesk MCP connector")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # Logs go to stderr so they never corrupt the stdio JSON-RPC stream.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        sys.exit(f"Configuration error: {exc}")

    transport = None
    if settings.mock:
        from .mock import MockFreshdesk

        transport = MockFreshdesk().transport()
        logging.getLogger(__name__).info("FRESHDESK_MOCK=1: serving fictional demo data")
    set_client(FreshdeskClient(settings, transport=transport))

    if args.transport == "stdio":
        mcp.run("stdio")
        return

    if not settings.connector_auth_token:
        sys.exit("Refusing to start the HTTP transport without CONNECTOR_AUTH_TOKEN set.")

    import uvicorn
    from starlette.responses import PlainTextResponse

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):
        return PlainTextResponse("ok")

    app = BearerAuthMiddleware(mcp.streamable_http_app(host=args.host), settings.connector_auth_token)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
