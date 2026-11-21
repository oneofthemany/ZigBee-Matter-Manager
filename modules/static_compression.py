"""Gzip for the /static mount only.

Scoped to text assets so streams, media and Range responses elsewhere are
never buffered or re-encoded; images, fonts and video are already compressed.
"""
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

TEXT_SUFFIXES = (".js", ".mjs", ".css", ".html", ".json", ".svg", ".map", ".txt")


class StaticCompression:
    """Route text assets without a Range header through gzip, the rest straight through."""

    # Level 6 is most of level 9's ratio at a fraction of the J3455's CPU.
    def __init__(self, app: ASGIApp, minimum_size: int = 1024, compresslevel: int = 6):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._compressible(scope):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    @staticmethod
    def _compressible(scope: Scope) -> bool:
        if not scope.get("path", "").lower().endswith(TEXT_SUFFIXES):
            return False
        # A gzipped body can't honour byte offsets into the original file.
        return not any(k == b"range" for k, _ in scope.get("headers", []))
