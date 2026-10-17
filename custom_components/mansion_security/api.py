"""Independent client for the endpoints observed in Mansion Security 2.0.0."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import json
import re
from urllib.parse import quote

import aiohttp

BASE_URL = "https://secom-dmc.smu.10fw.co.jp"
OFF = "0"


class MansionError(Exception):
    """Base exception; messages never contain credentials or response bodies."""


class MansionConnectionError(MansionError):
    pass


class MansionProtocolError(MansionError):
    pass


class MansionApiError(MansionError):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code) else "unknownError"
        super().__init__(f"HTTP {status}: {self.code}")


class MansionAuthError(MansionApiError):
    pass


class MansionWriteVerificationError(MansionError):
    pass


@dataclass(frozen=True)
class AuthSession:
    user_id: str
    api_key: str = field(repr=False)


def _boolean(value: object, name: str) -> bool:
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise MansionProtocolError(f"Missing or invalid {name}")


@dataclass(frozen=True)
class Preset:
    pattern: str
    title: str
    unlocks: bool
    preset: bool

    @classmethod
    def from_json(cls, data: object) -> Preset:
        if not isinstance(data, dict):
            raise MansionProtocolError("Invalid preset")
        pattern = data.get("autoResPattern")
        title = data.get("title")
        if not isinstance(pattern, str) or not pattern or pattern == OFF:
            raise MansionProtocolError("Invalid preset pattern")
        if not isinstance(title, str) or not title:
            raise MansionProtocolError("Invalid preset title")
        return cls(pattern, title, _boolean(data.get("unLock"), "unLock"),
                   _boolean(data.get("preset"), "preset"))


@dataclass(frozen=True)
class RoomState:
    enabled: bool
    pattern: str

    @property
    def mode(self) -> str:
        return self.pattern if self.enabled else OFF

    @classmethod
    def from_json(cls, data: object) -> RoomState:
        if not isinstance(data, dict):
            raise MansionProtocolError("Invalid setting response")
        enabled = _boolean(data.get("autoRes"), "autoRes")
        pattern = data.get("autoResPattern", "")
        if not isinstance(pattern, str) or (enabled and pattern in ("", OFF)):
            raise MansionProtocolError("Invalid current pattern")
        return cls(enabled, pattern)


@dataclass(frozen=True)
class Snapshot:
    state: RoomState
    presets: tuple[Preset, ...]


@dataclass(frozen=True)
class PairingInfo:
    expires: str
    valid_term: str


class MansionClient:
    def __init__(self, http: aiohttp.ClientSession, auth: AuthSession | None = None,
                 *, base_url: str = BASE_URL, timeout: float = 20):
        self._http = http
        self.auth = auth
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._operation_lock = asyncio.Lock()

    def _room_path(self, suffix: str) -> str:
        if self.auth is None:
            raise MansionAuthError(401, "notAuthenticated")
        return f"/v1/app_user/{quote(self.auth.user_id, safe='')}/room/{suffix}"

    async def _request(self, method: str, path: str, *, body: dict | None = None,
                       authenticated: bool = True) -> object:
        headers = {"Accept": "application/json", "User-Agent": "okhttp/4.12.0"}
        if authenticated:
            if self.auth is None:
                raise MansionAuthError(401, "notAuthenticated")
            headers["Authorization"] = "GEAR-API-KEY " + self.auth.api_key
        try:
            async with self._http.request(
                method, self._base_url + path, headers=headers, json=body,
                timeout=self._timeout, allow_redirects=False,
            ) as response:
                raw = await response.read()
                try:
                    data = json.loads(raw) if raw else {}
                except (ValueError, UnicodeError):
                    data = None
                if response.status != 200:
                    code = str(data.get("error", "unknownError")) if isinstance(data, dict) else "unknownError"
                    auth_codes = {"authFailed", "invalidUser", "loginFailed", "alreadyLoggedIn", "deactivated"}
                    error = MansionAuthError if response.status in (401, 403) or code in auth_codes else MansionApiError
                    raise error(response.status, code)
                if not isinstance(data, dict):
                    raise MansionProtocolError("Expected a JSON object")
                return data
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise MansionConnectionError("Unable to communicate with Mansion Security") from None

    async def async_login(self, name: str, password: str) -> AuthSession:
        async with self._operation_lock:
            data = await self._request("POST", "/v1/app_user/login", authenticated=False,
                                       body={"name": name, "password": password})
            user_id, api_key = data.get("userId"), data.get("acApiKey")
            if not isinstance(user_id, str) or not user_id or not isinstance(api_key, str) or not api_key:
                raise MansionProtocolError("Login did not return a usable session")
            self.auth = AuthSession(user_id, api_key)
            return self.auth

    async def async_initialize_account(self, current_password: str, new_password: str,
                                       nickname: str) -> None:
        """Complete native registration; only invoked by a configuration flow."""
        async with self._operation_lock:
            if self.auth is None:
                raise MansionAuthError(401, "notAuthenticated")
            path = f"/v1/app_user/{quote(self.auth.user_id, safe='')}/init"
            await self._request("POST", path, body={
                "oldPassword": current_password, "newPassword": new_password, "nickName": nickname,
            })

    async def async_logout(self) -> None:
        """Release this client's own session after an explicit user action."""
        async with self._operation_lock:
            if self.auth is None:
                raise MansionAuthError(401, "notAuthenticated")
            path = f"/v1/app_user/{quote(self.auth.user_id, safe='')}/logout"
            await self._request("POST", path, body={})
            self.auth = None

    async def _get_presets(self) -> tuple[Preset, ...]:
        data = await self._request("GET", self._room_path("autores_list"))
        items = data.get("entrance")
        if not isinstance(items, list):
            raise MansionProtocolError("Missing entrance preset list")
        presets = tuple(Preset.from_json(item) for item in items)
        if len({item.pattern for item in presets}) != len(presets):
            raise MansionProtocolError("Duplicate preset patterns")
        return presets

    async def _get_state(self) -> RoomState:
        return RoomState.from_json(await self._request("GET", self._room_path("setting")))

    async def async_get_snapshot(self) -> Snapshot:
        async with self._operation_lock:
            presets = await self._get_presets()
            state = await self._get_state()
            return Snapshot(state, presets)

    async def async_get_state(self) -> RoomState:
        async with self._operation_lock:
            return await self._get_state()

    async def async_set_mode(self, pattern: str) -> Snapshot:
        async with self._operation_lock:
            # Recheck the current list before writing: only persistent presets
            # that are still available can be selected.
            presets = await self._get_presets()
            if pattern != OFF:
                preset = next((item for item in presets if item.pattern == pattern and item.preset), None)
                if preset is None:
                    raise MansionProtocolError("Not an available persistent preset")
            await self._request("POST", self._room_path("setting"), body={"autoResponse": pattern})
            state = await self._get_state()
            if state.mode != pattern:
                raise MansionWriteVerificationError("The server did not confirm the requested mode")
            return Snapshot(state, presets)

    async def _pair_room(self, pair_hash: str) -> PairingInfo:
        data = await self._request("POST", self._room_path("pairing"), body={
            "platform": "android", "hash": pair_hash,
            "osVersion": "android16", "phoneModel": "xxxxx",
        })
        expires = data.get("expireDate")
        if not isinstance(expires, str):
            raise MansionProtocolError("Missing pairing expiry")
        try:
            datetime.strptime(expires, "%Y/%m/%d")
        except ValueError:
            raise MansionProtocolError("Invalid pairing expiry") from None
        return PairingInfo(expires, str(data.get("validTerm", "")))

    async def _register_device(self, token: str) -> None:
        if self.auth is None:
            raise MansionAuthError(401, "notAuthenticated")
        path = f"/v1/app_user/{quote(self.auth.user_id, safe='')}/regist_push_token"
        await self._request("POST", path, body={"platform": "android", "token": token})

    async def async_pair_room(self, pair_hash: str) -> PairingInfo:
        async with self._operation_lock:
            return await self._pair_room(pair_hash)

    async def async_register_device(self, token: str) -> None:
        async with self._operation_lock:
            await self._register_device(token)

    async def async_renew_pairing(self, pair_hash: str, push_token: str) -> PairingInfo:
        """Use the account's existing pairing credentials, without another login."""
        async with self._operation_lock:
            info = await self._pair_room(pair_hash)
            await self._register_device(push_token)
            return info
