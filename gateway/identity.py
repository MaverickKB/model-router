"""Authentication only. Routing permissions are enforced separately by routing.decide."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .request_policy import resolve_unkeyed_policy
from .schema import Client
from .security.credentials import bootstrap_key, digest, verify
from .security.limits import RateLimit
from .store import Store


class Identity:
    def __init__(self, store: Store):
        self.store = store
        self.sessions: OrderedDict[str, float] = OrderedDict(store.operator_sessions())
        self.verification_slots = asyncio.Semaphore(4)
        self.login_limit = RateLimit(capacity=10, period=60)
        self.operator_token_digest: bytes | None = None
        self.registration_limit = RateLimit(capacity=10, period=60)
        supplied = os.environ.get("MODEL_ROUTER_ADMIN_TOKEN")
        self.admin_verifier = store.operator_verifier()
        if supplied or not self.admin_verifier:
            token = supplied or bootstrap_key(store.directory)
            self.admin_verifier = digest(token)
            store.set_operator_verifier(self.admin_verifier)

    def origin_allowed(self, request: Request) -> bool:
        origin = urlsplit(request.headers.get("Origin", ""))
        # The browser may reach the same service through its canonical URL or an
        # SSH tunnel. Authentication is checked separately from this CSRF test.
        targets = [str(request.base_url), os.environ.get("MODEL_ROUTER_PUBLIC_URL", "")]
        return bool(origin.scheme and origin.netloc) and any(
            (origin.scheme, origin.netloc)
            == (urlsplit(target).scheme, urlsplit(target).netloc)
            for target in targets
            if target
        )

    @staticmethod
    def setup_source_allowed(request: Request) -> bool:
        """Allow first-use setup only from a source that is not publicly routed."""
        try:
            source = ipaddress.ip_address(request.client.host)
        except (ValueError, AttributeError):
            return False
        return not source.is_global

    async def operator(self, request: Request) -> bool:
        if self.store.setup_required and self.setup_source_allowed(request):
            return True
        policy = self.store.config().security
        if not policy.operator_auth_enabled:
            try:
                source = ipaddress.ip_address(request.client.host)
                allowed_hosts = {
                    "localhost",
                    "127.0.0.1",
                    "::1",
                    urlsplit(os.environ.get("MODEL_ROUTER_PUBLIC_URL", "")).hostname,
                }
                if request.url.hostname in allowed_hosts and any(
                    source in ipaddress.ip_network(value)
                    for value in policy.operator_networks
                ):
                    return True
            except (ValueError, AttributeError):
                pass
        now = time.time()
        expired = [key for key, expires in self.sessions.items() if expires <= now]
        for key in expired:
            self.sessions.pop(key, None)
        supplied = request.headers.get("Authorization", "")
        if supplied:
            if not supplied.startswith("Bearer "):
                return False
            token_digest = hashlib.sha256(supplied[7:].encode()).digest()
            if self.operator_token_digest and hmac.compare_digest(
                self.operator_token_digest, token_digest
            ):
                return True
            source = request.client.host if request.client else "unknown"
            self.login_limit.take(source)
            async with self.verification_slots:
                valid = await asyncio.to_thread(
                    verify, self.admin_verifier, supplied[7:]
                )
            if valid:
                self.operator_token_digest = token_digest
                self.login_limit.reset(source)
            return valid
        session = request.cookies.get("router_operator", "")
        key = hashlib.sha256(session.encode()).hexdigest()
        if key in self.sessions:
            self.sessions.move_to_end(key)
            return True
        return False

    async def require_operator(self, request: Request, mutation=False):
        if not await self.operator(request):
            raise HTTPException(401, "Sign in with an operator key")
        # Bearer clients do not rely on ambient browser credentials. Cookie writes
        # require a matching Origin even when the request omits it.
        if (
            mutation
            and (
                request.headers.get("Origin")
                or not request.headers.get("Authorization")
            )
            and not self.origin_allowed(request)
        ):
            raise HTTPException(403, "Settings require a same-origin operator request")

    async def identify(self, request: Request) -> Client:
        request.state.authentication_error = None
        auth = request.headers.get("Authorization", "")
        unusable_auth = bool(auth)
        if auth and auth[:7].casefold() == "bearer ":
            # A number of OpenAI-compatible clients always send an API-key
            # header, even when the operator has not configured caller keys.
            # Resolve usable keys here, but let an unusable header continue
            # through the ordinary unkeyed policy path. Route gates decide
            # whether that request may proceed. This keeps open routes
            # compatible with those clients without weakening a gated route.
            async with self.verification_slots:
                client_id = await asyncio.to_thread(self.store.key_client, auth[7:])
            # Key verification yields to configuration updates. Evaluate the
            # policy as it exists after that work, including disable/removal.
            config = self.store.config()
            configured = next((c for c in config.clients if c.id == client_id), None)
            if configured is not None and not configured.enabled:
                raise HTTPException(401, "Client key is invalid or revoked")
            client = configured
            if client is not None:
                if client.source_networks:
                    try:
                        source = ipaddress.ip_address(request.client.host)
                        if not any(
                            source in ipaddress.ip_network(n, strict=False)
                            for n in client.source_networks
                        ):
                            raise HTTPException(
                                403, "Client key is not allowed from this source address"
                            )
                    except (ValueError, AttributeError):
                        raise HTTPException(
                            403, "Client key requires a permitted source address"
                        )
                request.state.identity_basis = "api_key"
                request.state.caller_key_present = True
                return client
        client, basis = resolve_unkeyed_policy(
            self.store.config(), request.client.host if request.client else None
        )
        request.state.identity_basis = "unassigned" if unusable_auth else basis
        request.state.caller_key_present = False
        return client

    async def login(self, request: Request):
        if not self.origin_allowed(request):
            raise HTTPException(403, "Open the router interface to sign in")
        self.login_limit.take(request.client.host if request.client else "unknown")
        body = await request.json()
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("token"), str)
            or len(body["token"]) > 4096
        ):
            raise HTTPException(400, "Operator key is required")
        async with self.verification_slots:
            valid = await asyncio.to_thread(verify, self.admin_verifier, body["token"])
        if not valid:
            raise HTTPException(401, "Operator key is incorrect")
        self.store.complete_setup()
        return await self.new_session(
            request, {"ok": True}, remember=body.get("remember", True)
        )

    async def new_session(self, request: Request, body: dict, remember=True):
        old = hashlib.sha256(
            request.cookies.get("router_operator", "").encode()
        ).hexdigest()
        self.sessions.pop(old, None)
        await asyncio.to_thread(self.store.revoke_operator_session, old)
        if len(self.sessions) >= 128:
            evicted, _ = self.sessions.popitem(last=False)
            await asyncio.to_thread(self.store.revoke_operator_session, evicted)
        session = secrets.token_urlsafe(32)
        key = hashlib.sha256(session.encode()).hexdigest()
        lifetime = (
            self.store.config().security.session_hours * 3600 if remember else 3600
        )
        self.sessions[key] = time.time() + lifetime
        await asyncio.to_thread(
            self.store.save_operator_session, key, self.sessions[key]
        )
        response = JSONResponse(body)
        secure = (
            urlsplit(
                request.headers.get("Origin")
                or os.environ.get("MODEL_ROUTER_PUBLIC_URL", str(request.url))
            ).scheme
            == "https"
        )
        response.set_cookie(
            "router_operator",
            session,
            httponly=True,
            samesite="strict",
            secure=secure,
            max_age=lifetime,
        )
        return response

    async def rotate_key(self, request: Request):
        await self.require_operator(request, True)
        if os.environ.get("MODEL_ROUTER_ADMIN_TOKEN"):
            raise HTTPException(
                409,
                "The operator key is managed by MODEL_ROUTER_ADMIN_TOKEN; update that deployment setting to replace it",
            )
        self.store.complete_setup()
        token = secrets.token_urlsafe(32)
        verifier = await asyncio.to_thread(digest, token)
        await asyncio.to_thread(self.store.set_operator_verifier, verifier)
        self.admin_verifier = verifier
        self.operator_token_digest = None
        self.sessions.clear()
        await asyncio.to_thread(self.store.clear_operator_sessions)
        (self.store.directory / "operator-bootstrap.key").unlink(missing_ok=True)
        return await self.new_session(request, {"key": token})

    async def logout(self, request: Request):
        await self.require_operator(request, True)
        session = request.cookies.get("router_operator", "")
        key = hashlib.sha256(session.encode()).hexdigest()
        self.sessions.pop(key, None)
        await asyncio.to_thread(self.store.revoke_operator_session, key)
        response = JSONResponse({"ok": True})
        response.delete_cookie("router_operator")
        return response
