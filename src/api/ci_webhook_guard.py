"""Pre-read gate for the CI webhook (D15): token and size before the body.

FastAPI buffers and JSON-decodes a request body before it resolves any
dependency, so a dependency-only token check lets an unauthenticated caller
learn that the endpoint parses JSON (a malformed body draws a 400) and make a
worker buffer an arbitrarily large body. This pure-ASGI middleware answers
before `receive` is ever called:

- bad / missing `X-Gitlab-Token` → `401` (same check as the router dependency);
- any `Transfer-Encoding`, or no usable `Content-Length` → `411` — CI always
  sends a plain Content-Length body, and without one the size cannot be
  bounded up front;
- `Content-Length` above `CI_WEBHOOK_MAX_BODY_BYTES` → `413`.

`Transfer-Encoding` is refused even alongside a valid `Content-Length`: h11
(uvicorn's parser without httptools) lets chunked framing win, so a small
declared length would otherwise front an unbounded chunked body. With
Content-Length framing alone the server stops reading at the declared size.
Every other path passes straight through.
"""
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src import config
from src.api.deps import ci_webhook_token_valid

CI_WEBHOOK_PATH = "/internal/ci-webhook"


class CiWebhookGuard:
    def __init__(self, app: ASGIApp, path: str = CI_WEBHOOK_PATH):
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != self.path:
            await self.app(scope, receive, send)
            return
        rejection = self._reject(Headers(scope=scope))
        if rejection is not None:
            await rejection(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _reject(headers: Headers) -> JSONResponse | None:
        if not ci_webhook_token_valid(headers.get("x-gitlab-token")):
            return JSONResponse(
                {"detail": "Invalid or missing CI webhook token"}, status_code=401
            )
        if "transfer-encoding" in headers:
            return JSONResponse({"detail": "Content-Length required"}, status_code=411)
        try:
            length = int(headers.get("content-length", ""))
        except ValueError:
            length = -1
        if length < 0:
            return JSONResponse({"detail": "Content-Length required"}, status_code=411)
        if length > config.CI_WEBHOOK_MAX_BODY_BYTES:
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
        return None
