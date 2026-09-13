"""Bound management JSON before parsing without buffering inference streams."""

from starlette.responses import JSONResponse


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or scope.get("method") not in {"POST", "PUT", "PATCH"}
            or not (path.startswith("/api/") or path.endswith("/register"))
        ):
            return await self.app(scope, receive, send)
        small = ("/login", "/operator/key", "/portal/activate", "/portal/password")
        limit = 8192 if path.endswith(small) else 2 * 1024 * 1024
        parts, length = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            length += len(message.get("body", b""))
            if length > limit:
                return await JSONResponse(
                    {"detail": "Management request is too large"}, status_code=413
                )(scope, receive, send)
            parts.append(message)
            if not message.get("more_body"):
                break
        iterator = iter(parts)

        async def replay():
            return next(iterator, None) or await receive()

        await self.app(scope, replay, send)
