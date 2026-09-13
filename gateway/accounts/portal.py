"""Portal sessions, sign-in, activation and passwords for account holders; never operator authority."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..identity import Identity, cookie_secure, origin_allowed
from ..security.credentials import digest, verify
from ..security.limits import RateLimit
from ..store import Store, token_digest

COOKIE = "router_portal"
MAX_SESSIONS = 1024
MAX_ACCOUNT_SESSIONS = 16
DISABLED = "User accounts are not enabled on this router"
SUSPENDED = "Account is suspended. Contact the administrator."
SIGN_IN = "Sign in to the portal"
# Unknown and pending usernames verify against this hash, so a wrong password
# and a missing account cost the same work and produce the same answer.
DUMMY_VERIFIER = digest(secrets.token_urlsafe(32))


def check_password(password: str, username: str) -> None:
    if len(password) < 12:
        raise HTTPException(422, "Use at least 12 characters")
    if len(password) > 256:
        raise HTTPException(422, "Use at most 256 characters")
    if password.casefold() == username.casefold():
        raise HTTPException(422, "Password must not be the username")


class PortalIdentity:
    """Reads only the portal cookie; a portal session never carries management authority."""

    def __init__(self, store: Store, identity: Identity):
        self.store = store
        self.identity = identity
        self.sessions: OrderedDict[str, tuple[str, float]] = OrderedDict(
            store.portal_sessions()
        )
        self.login_limit = RateLimit(capacity=10, period=60)
        self.user_limit = RateLimit(capacity=10, period=60)

    async def _revoke(self, key: str) -> None:
        self.sessions.pop(key, None)
        await asyncio.to_thread(self.store.revoke_portal_session, key)

    def drop_account(self, account_id: str) -> None:
        """Forget an account's sessions after their rows were removed."""
        for key in [
            k for k, (owner, _) in self.sessions.items() if owner == account_id
        ]:
            self.sessions.pop(key, None)

    async def require_account(self, request: Request, mutation=False) -> dict:
        if not self.store.config().accounts.enabled:
            # The cookie is left in place so re-enabling restores unexpired sessions.
            raise HTTPException(403, DISABLED)
        now = time.time()
        for key in [k for k, (_, expires) in self.sessions.items() if expires <= now]:
            self.sessions.pop(key, None)
        key = token_digest(request.cookies.get(COOKIE, ""))
        session = self.sessions.get(key)
        if session is None:
            raise HTTPException(401, SIGN_IN)
        account = self.store.account_snapshot(session[0])
        if account is None or account["status"] != "active":
            await self._revoke(key)
            if account is not None and account["status"] == "suspended":
                raise HTTPException(403, SUSPENDED)
            raise HTTPException(401, SIGN_IN)
        self.sessions.move_to_end(key)
        # The portal has no bearer path, so every write needs a matching Origin.
        if mutation and not origin_allowed(request):
            raise HTTPException(403, "Portal changes require a same-origin request")
        return account

    async def login(self, request: Request, body) -> JSONResponse:
        if not self.store.config().accounts.enabled:
            raise HTTPException(403, DISABLED)
        if not origin_allowed(request):
            raise HTTPException(403, "Open the portal to sign in")
        source = request.client.host if request.client else "unknown"
        username = body.username.strip().lower()
        self.login_limit.take(source)
        self.user_limit.take("user:" + username)
        account = self.store.account_by_username(username)
        verifier = None
        if account is not None and account["status"] != "pending":
            verifier = await asyncio.to_thread(
                self.store.account_verifier, account["id"]
            )
        async with self.identity.verification_slots:
            valid = await asyncio.to_thread(
                verify, verifier or DUMMY_VERIFIER, body.password
            )
        if valid and account is not None:
            # Verification ran against a verifier read before the expensive work;
            # a password change or reset link that landed meanwhile must not be
            # answered with the password it replaced.
            valid = verifier == await asyncio.to_thread(
                self.store.account_verifier, account["id"]
            )
            account = self.store.account_snapshot(account["id"])
        if not valid or account is None or account["status"] == "pending":
            raise HTTPException(401, "Username or password is incorrect")
        if account["status"] == "suspended":
            # Reached only with a correct password, so a guesser learns nothing.
            raise HTTPException(403, SUSPENDED)
        self.login_limit.reset(source)
        await asyncio.to_thread(self.store.touch_login, account["id"])
        return await self.new_session(
            request, account["id"], {"ok": True}, remember=body.remember
        )

    async def activate(self, request: Request, body) -> JSONResponse:
        if not self.store.config().accounts.enabled:
            raise HTTPException(403, DISABLED)
        if not origin_allowed(request):
            raise HTTPException(403, "Open the portal to sign in")
        self.login_limit.take(request.client.host if request.client else "unknown")
        account_id = await asyncio.to_thread(self.store.activation_account, body.token)
        account = self.store.account_snapshot(account_id) if account_id else None
        if account is None:
            raise HTTPException(400, "Activation link is invalid or expired")
        # The policy is checked before the one-time link is consumed.
        check_password(body.password, account["username"])
        verifier = await asyncio.to_thread(digest, body.password)
        activated = await asyncio.to_thread(
            self.store.activate_account, body.token, verifier
        )
        if activated is None:
            raise HTTPException(400, "Activation link is invalid or expired")
        self.drop_account(account["id"])
        return await self.new_session(request, account["id"], {"ok": True})

    async def logout(self, request: Request) -> JSONResponse:
        await self.require_account(request, True)
        await self._revoke(token_digest(request.cookies.get(COOKIE, "")))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    async def change_password(self, request: Request, body) -> JSONResponse:
        account = await self.require_account(request, True)
        check_password(body.new, account["username"])
        # Checking the current password is a sign-in, so it spends the same budgets.
        source = request.client.host if request.client else "unknown"
        self.login_limit.take(source)
        self.user_limit.take("user:" + account["username"])
        verifier = await asyncio.to_thread(self.store.account_verifier, account["id"])
        async with self.identity.verification_slots:
            valid = await asyncio.to_thread(
                verify, verifier or DUMMY_VERIFIER, body.current
            )
        # A concurrent change or reset link that replaced the verifier while
        # this one was being checked wins; the stale current password is refused.
        if not valid or verifier != await asyncio.to_thread(
            self.store.account_verifier, account["id"]
        ):
            raise HTTPException(401, "Current password is incorrect")
        self.login_limit.reset(source)
        replacement = await asyncio.to_thread(digest, body.new)
        await asyncio.to_thread(
            self.store.set_account_verifier, account["id"], replacement
        )
        # Every other browser signs out; API keys are untouched.
        await asyncio.to_thread(self.store.revoke_account_sessions, account["id"])
        self.drop_account(account["id"])
        return await self.new_session(request, account["id"], {"ok": True})

    async def new_session(
        self, request: Request, account_id: str, body: dict, remember=True
    ) -> JSONResponse:
        await self._revoke(token_digest(request.cookies.get(COOKIE, "")))
        owned = [k for k, (owner, _) in self.sessions.items() if owner == account_id]
        for key in owned[: max(0, len(owned) - MAX_ACCOUNT_SESSIONS + 1)]:
            await self._revoke(key)
        while len(self.sessions) >= MAX_SESSIONS:
            evicted, _ = self.sessions.popitem(last=False)
            await asyncio.to_thread(self.store.revoke_portal_session, evicted)
        token = secrets.token_urlsafe(32)
        key = token_digest(token)
        lifetime = (
            self.store.config().accounts.session_hours * 3600 if remember else 3600
        )
        expires = time.time() + lifetime
        self.sessions[key] = (account_id, expires)
        await asyncio.to_thread(
            self.store.save_portal_session, key, account_id, expires
        )
        response = JSONResponse(body)
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=cookie_secure(request),
            max_age=lifetime,
            path="/",
        )
        return response
