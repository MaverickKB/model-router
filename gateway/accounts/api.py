"""Operator and portal endpoints for accounts; owns their request shapes and response summaries."""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, field_validator

from ..routing import decide, matches
from ..schema import AccountLevel, Configuration, Record
from ..store import Store
from .devices import canonical_address, shadowing
from .limits import AccountLimits
from .principal import derive_principal, level_for

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


class Login(Record):
    username: str = Field(max_length=64)
    password: str
    remember: bool = True


class Activate(Record):
    token: str
    password: str


class ChangePassword(Record):
    current: str
    new: str


class CreateKey(Record):
    name: str = Field(min_length=1, max_length=60)

    @field_validator("name", mode="before")
    @classmethod
    def strip(cls, value):
        return value.strip() if isinstance(value, str) else value


class RegisterDevice(Record):
    address: str = Field(max_length=64)
    name: str = Field(min_length=1, max_length=60)

    @field_validator("name", mode="before")
    @classmethod
    def strip(cls, value):
        return value.strip() if isinstance(value, str) else value


class DeviceEnabled(Record):
    enabled: bool


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


def level_view(
    config: Configuration, views: list, account: dict, level: AccountLevel
) -> dict:
    """What the level lets this account use, with readiness from the same decision as /v1/models."""
    principal = derive_principal(account, level)
    return {
        "id": level.id,
        "name": level.name,
        "description": level.description,
        "routes": [
            {
                "name": route.name,
                "purpose": route.purpose,
                "ready": bool(
                    decide(
                        config,
                        views,
                        principal,
                        {"model": route.name},
                        consider_capacity=False,
                        caller_key_present=True,
                    )["candidates"]
                ),
            }
            for route in config.routes
            if route.enabled and matches(route.name, level.route_names)
        ],
        "model_patterns": level.model_patterns,
        "allow_cloud": level.allow_cloud,
        "allow_direct_models": level.allow_direct_models,
        "token_budget": level.token_budget.model_dump() if level.token_budget else None,
        "max_concurrency": level.max_concurrency,
    }


def observed_devices(
    account: dict, callers: list[dict], keys: list[dict]
) -> list[dict]:
    """This account's own observed callers; other accounts' connections never appear."""
    names = {key["id"]: key["name"] for key in keys}
    prefix = f"{account['name']} · "
    return [
        {
            "source_address": caller["source_address"],
            "software": caller["software"],
            "reported_name": caller["reported_name"],
            "last_seen": caller["last_seen"],
            "last_path": caller["last_path"],
            "via": "device"
            if caller["identity_basis"] == "registered_device"
            else "key",
            # A revoked key keeps the name it was observed under.
            "credential_name": names.get(caller.get("key_id"))
            or caller["name"].removeprefix(prefix),
        }
        for caller in callers
        if caller.get("account_id") == account["id"]
    ]


def admin_router(store: Store, identity, discovery, proxy, portal=None) -> APIRouter:
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
        if status == "suspended" and portal is not None:
            portal.drop_account(account_id)
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
        # The store dropped the rows; the resolver's map must forget them too.
        if portal is not None:
            portal.drop_account(account_id)
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

    # Device rows stay manageable while either switch is off (F1): they are
    # inert then, not gone.
    @router.get("/devices")
    async def list_devices(request: Request):
        await identity.require_operator(request)
        config = store.config()
        names = {account["id"]: account["name"] for account in store.accounts()}
        return {
            "devices": [
                {
                    **device,
                    "account_name": names.get(device["account_id"], ""),
                    "shadows": shadowing(config, device["address"]),
                }
                for device in store.devices()
            ]
        }

    @router.put("/devices/{device_id}")
    async def set_device_enabled(device_id: str, body: DeviceEnabled, request: Request):
        await identity.require_operator(request, True)
        device = await asyncio.to_thread(
            store.set_device_enabled, device_id, body.enabled
        )
        if device is None:
            raise HTTPException(404, "Device does not exist")
        return {"device": device}

    @router.delete("/devices/{device_id}")
    async def remove_device(device_id: str, request: Request):
        await identity.require_operator(request, True)
        if not await asyncio.to_thread(store.remove_device, device_id):
            raise HTTPException(404, "Device does not exist")
        return {"ok": True}

    return router


def portal_router(store: Store, identity, portal, discovery, proxy) -> APIRouter:
    router = APIRouter()

    @router.get("/status")
    async def status(request: Request):
        settings = store.config().accounts
        signed_in = False
        if settings.enabled:
            try:
                await portal.require_account(request)
                signed_in = True
            except HTTPException:
                pass
        return {
            "enabled": settings.enabled,
            "device_registration_enabled": (
                settings.enabled and settings.device_registration_enabled
            ),
            "signed_in": signed_in,
        }

    @router.post("/activate")
    async def activate(body: Activate, request: Request):
        return await portal.activate(request, body)

    @router.post("/login")
    async def login(body: Login, request: Request):
        return await portal.login(request, body)

    @router.post("/logout")
    async def logout(request: Request):
        return await portal.logout(request)

    @router.get("/me")
    async def me(request: Request):
        account = await portal.require_account(request)
        config, now = store.config(), time.time()
        level = level_for(config, account["level_id"])

        def load():
            keys = store.account_keys(account["id"])
            return keys, observed_devices(account, store.callers(), keys)

        keys, observed = await asyncio.to_thread(load)
        # Registration is a list only while the dev-mode switch is on; the
        # page hides the whole registration surface on null.
        registered = (
            [
                {**device, "shadowed": bool(shadowing(config, device["address"]))}
                for device in store.devices_for(account["id"])
            ]
            if config.accounts.device_registration_enabled
            else None
        )
        return {
            "account": {
                field: account[field]
                for field in (
                    "id",
                    "username",
                    "name",
                    "status",
                    "created",
                    "activated_at",
                    "last_login",
                )
            },
            "level": (
                level_view(config, discovery.views(), account, level) if level else None
            ),
            "usage": proxy.limits.snapshot(account["id"], level, now)
            if level
            else None,
            "keys": keys,
            "devices": {"observed": observed, "registered": registered},
            "source_address": request.client.host if request.client else "",
            "base_url": public_url(request) + "/v1",
        }

    @router.put("/password")
    async def change_password(body: ChangePassword, request: Request):
        return await portal.change_password(request, body)

    @router.post("/keys", status_code=201)
    async def create_key(body: CreateKey, request: Request):
        account = await portal.require_account(request, True)
        try:
            key, record = await asyncio.to_thread(
                store.create_account_key, account["id"], body.name
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"key": key, "record": record}

    @router.delete("/keys/{key_id}")
    async def revoke_key(key_id: str, request: Request):
        account = await portal.require_account(request, True)
        if not await asyncio.to_thread(store.revoke_account_key, account["id"], key_id):
            raise HTTPException(404, "Key does not exist")
        return {"ok": True}

    def require_registration() -> None:
        # Checked after the session so the answer differs only for signed-in users.
        if not store.config().accounts.device_registration_enabled:
            raise HTTPException(404, "Device registration is not enabled")

    @router.post("/devices", status_code=201)
    async def register_device(body: RegisterDevice, request: Request):
        account = await portal.require_account(request, True)
        require_registration()
        try:
            address = canonical_address(body.address)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        try:
            device = await asyncio.to_thread(
                store.register_device, account["id"], address, body.name
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        config = store.config()
        return {"device": {**device, "shadowed": bool(shadowing(config, address))}}

    @router.delete("/devices/{device_id}")
    async def remove_device(device_id: str, request: Request):
        account = await portal.require_account(request, True)
        require_registration()
        # Scoped to the session's account: another account's device is "not found".
        if not await asyncio.to_thread(store.remove_device, device_id, account["id"]):
            raise HTTPException(404, "Device does not exist")
        return {"ok": True}

    return router
