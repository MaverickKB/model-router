"""Operator endpoints for accounts, activation links and usage; owns their request shapes and summaries."""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import field_validator

from ..schema import Configuration, Record
from ..store import Store
from .limits import AccountLimits
from .principal import level_for

USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
USERNAME_RULE = (
    "Usernames are 2–64 lowercase letters, digits, dots, dashes or underscores"
)


class CreateAccount(Record):
    username: str
    name: str | None = None
    level_id: str

    @field_validator("username")
    @classmethod
    def lowercase(cls, value: str) -> str:
        return value.strip().lower()


class UpdateAccount(Record):
    name: str | None = None
    level_id: str | None = None
    status: Literal["active", "suspended"] | None = None


def require_username(username: str) -> str:
    if not USERNAME.match(username):
        raise HTTPException(422, USERNAME_RULE)
    return username


def store_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if message == "Username is already taken":
        return HTTPException(409, message)
    if message == "Account does not exist":
        return HTTPException(404, message)
    return HTTPException(422, message)


def public_url(request: Request) -> str:
    return (os.environ.get("MODEL_ROUTER_PUBLIC_URL") or str(request.base_url)).rstrip(
        "/"
    )


def activation_link(request: Request, token: str, expires: float) -> dict:
    # The token rides in the fragment so it never reaches server logs or Referer.
    return {
        "token": token,
        "url": f"{public_url(request)}/portal/activate#token={token}",
        "expires": expires,
    }


def account_summary(
    account: dict,
    store: Store,
    config: Configuration,
    limits: AccountLimits,
    now: float,
) -> dict:
    level = level_for(config, account["level_id"])
    activation = store.activation_for(account["id"])
    return {
        **account,
        # A link counts as pending only while it can still activate the account.
        "activation_pending": (
            activation is not None
            and activation["expires"] > now
            and account["status"] == "pending"
        ),
        "activation_expires": activation["expires"] if activation else None,
        "key_count": len(store.account_keys(account["id"])),
        "device_count": len(store.devices_for(account["id"])),
        "usage": limits.snapshot(account["id"], level, now) if level else None,
    }


def admin_router(store: Store, identity, discovery, proxy) -> APIRouter:
    router = APIRouter()

    def summaries(accounts: list[dict]) -> list[dict]:
        config, now = store.config(), time.time()
        return [
            account_summary(account, store, config, proxy.limits, now)
            for account in accounts
        ]

    def existing(account_id: str) -> dict:
        account = store.account_snapshot(account_id)
        if account is None:
            raise HTTPException(404, "Account does not exist")
        return account

    @router.get("/accounts")
    async def list_accounts(request: Request):
        await identity.require_operator(request)
        return {"accounts": await asyncio.to_thread(summaries, store.accounts())}

    @router.get("/accounts/{account_id}")
    async def account_detail(account_id: str, request: Request):
        await identity.require_operator(request)
        account = existing(account_id)

        def load():
            return {
                "account": summaries([account])[0],
                "keys": store.account_keys(account_id),
                "devices": store.devices_for(account_id),
                "usage_windows": store.usage_windows(account_id),
            }

        return await asyncio.to_thread(load)

    @router.post("/accounts", status_code=201)
    async def create_account(body: CreateAccount, request: Request):
        await identity.require_operator(request, True)
        username = require_username(body.username)

        def provision():
            account = store.create_account(
                username, body.name or username, body.level_id
            )
            token, expires = store.issue_activation(account["id"], "activate")
            return summaries([account])[0], token, expires

        try:
            account, token, expires = await asyncio.to_thread(provision)
        except ValueError as exc:
            raise store_error(exc)
        return {
            "account": account,
            "activation": activation_link(request, token, expires),
        }

    @router.put("/accounts/{account_id}")
    async def update_account(account_id: str, body: UpdateAccount, request: Request):
        await identity.require_operator(request, True)
        account = existing(account_id)
        status = body.status
        if (
            status == "active"
            and account["status"] != "active"
            and await asyncio.to_thread(store.account_verifier, account_id) is None
        ):
            if account["status"] == "pending":
                raise HTTPException(
                    422, "Activate the account with its link before enabling it"
                )
            # Suspended before it was ever activated: re-enabling returns the
            # account to pending. The link issued before suspension is voided
            # first so it is never live again; the operator issues a fresh one.
            status = "pending"
            await asyncio.to_thread(store.clear_activation, account_id)
        try:
            updated = await asyncio.to_thread(
                store.update_account,
                account_id,
                name=body.name,
                level_id=body.level_id,
                status=status,
            )
        except ValueError as exc:
            raise store_error(exc)
        return {"account": (await asyncio.to_thread(summaries, [updated]))[0]}

    @router.post("/accounts/{account_id}/activation")
    async def issue_activation(account_id: str, request: Request):
        await identity.require_operator(request, True)
        account = existing(account_id)
        if account["status"] == "suspended":
            raise HTTPException(409, "Enable the account before issuing a link")
        purpose = "activate" if account["status"] == "pending" else "reset"
        try:
            token, expires = await asyncio.to_thread(
                store.issue_activation, account_id, purpose
            )
        except ValueError as exc:
            raise store_error(exc)
        return activation_link(request, token, expires)

    @router.delete("/accounts/{account_id}")
    async def delete_account(account_id: str, request: Request):
        await identity.require_operator(request, True)
        existing(account_id)
        await asyncio.to_thread(store.delete_account, account_id)
        return {"ok": True}

    @router.delete("/accounts/{account_id}/keys/{key_id}")
    async def revoke_account_key(account_id: str, key_id: str, request: Request):
        await identity.require_operator(request, True)
        existing(account_id)
        if not await asyncio.to_thread(store.revoke_account_key, account_id, key_id):
            raise HTTPException(404, "Key does not exist")
        return {"ok": True}

    return router
