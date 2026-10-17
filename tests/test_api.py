"""Protocol tests using a local server; no real credentials or door hardware."""

import asyncio
from pathlib import Path
import sys

import aiohttp
from aiohttp import web
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from custom_components.mansion_security.api import (  # noqa: E402
    AuthSession, MansionApiError, MansionAuthError, MansionClient, MansionConnectionError,
    MansionProtocolError, MansionUnsafeModeError, MansionWriteVerificationError,
    Preset, RoomState,
)


@pytest_asyncio.fixture
async def backend(socket_enabled):
    state = {
        "mode": "0", "calls": [], "error": None, "delay": 0,
        "write_applies": True,
        "presets": [
            {"autoResPattern": "24", "title": "稍后再来", "unLock": False, "preset": True},
            {"autoResPattern": "23", "title": "请进", "unLock": True, "preset": True},
            {"autoResPattern": "99", "title": "仅限通话中", "unLock": False, "preset": False},
        ],
    }

    async def handle(request):
        body = await request.json() if request.method == "POST" else None
        state["calls"].append((request.method, request.path, body, request.headers.get("Authorization")))
        if request.path.endswith("/login"):
            assert body == {"name": "test-account", "password": "test-password-only"}
            assert "Authorization" not in request.headers
            return web.json_response({"userId": "test-user", "acApiKey": "test-key"})
        assert request.headers.get("Authorization") == "GEAR-API-KEY test-key"
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        if state["error"]:
            status, code = state["error"]
            return web.json_response({"error": code}, status=status)
        if request.path.endswith("/autores_list"):
            return web.json_response({"entrance": state["presets"], "doorphone": []})
        if request.path.endswith("/pairing"):
            assert body == {"platform": "android", "hash": "test-pairing-code", "osVersion": "android16", "phoneModel": "xxxxx"}
            return web.json_response({"expireDate": "2099/07/01", "validTerm": "6"})
        if request.path.endswith("/regist_push_token"):
            assert body == {"platform": "android", "token": "test-push-token"}
            return web.json_response({})
        if request.path.endswith("/init"):
            assert body == {"oldPassword": "test-current-password", "newPassword": "test-new-password", "nickName": "Home Assistant"}
            return web.json_response({})
        if request.path.endswith("/logout"):
            assert body == {}
            return web.json_response({})
        if request.method == "POST":
            assert set(body) == {"autoResponse"}
            assert isinstance(body["autoResponse"], str)
            if state["write_applies"]:
                state["mode"] = body["autoResponse"]
            return web.json_response({})
        return web.json_response({"autoRes": state["mode"] != "0", "autoResPattern": state["mode"]})

    app = web.Application()
    app.router.add_post("/v1/app_user/login", handle)
    app.router.add_get("/v1/app_user/{uid}/room/autores_list", handle)
    app.router.add_route("*", "/v1/app_user/{uid}/room/setting", handle)
    app.router.add_post("/v1/app_user/{uid}/room/pairing", handle)
    app.router.add_post("/v1/app_user/{uid}/regist_push_token", handle)
    app.router.add_post("/v1/app_user/{uid}/init", handle)
    app.router.add_post("/v1/app_user/{uid}/logout", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    async with aiohttp.ClientSession() as http:
        state["client"] = MansionClient(http, AuthSession("test-user", "test-key"),
                                          base_url=f"http://127.0.0.1:{port}")
        yield state
    await runner.cleanup()


async def test_login_and_read_real_state(backend):
    client = backend["client"]
    client.auth = None
    auth = await client.async_login("test-account", "test-password-only")
    assert auth.user_id == "test-user"
    assert "test-key" not in repr(auth)
    snapshot = await client.async_get_snapshot()
    assert snapshot.state.mode == "0"
    assert [item.pattern for item in snapshot.presets if item.preset] == ["24", "23"]


async def test_native_password_initialization_uses_current_session_without_retry(backend):
    await backend["client"].async_initialize_account("test-current-password", "test-new-password", "Home Assistant")
    assert len(backend["calls"]) == 1
    assert backend["calls"][0][1] == "/v1/app_user/test-user/init"


async def test_password_initialization_rejection_is_sanitized(backend):
    backend["error"] = (400, "changeFailed")
    with pytest.raises(MansionApiError) as caught:
        await backend["client"].async_initialize_account("test-current-password", "test-new-password", "Home Assistant")
    assert caught.value.code == "changeFailed"
    assert "test-current-password" not in str(caught.value) and "test-new-password" not in str(caught.value)
    assert len(backend["calls"]) == 1


async def test_explicit_logout_releases_only_current_session(backend):
    await backend["client"].async_logout()
    assert backend["client"].auth is None
    assert backend["calls"] == [("POST", "/v1/app_user/test-user/logout", {}, "GEAR-API-KEY test-key")]


async def test_set_and_clear_with_readback(backend):
    client = backend["client"]
    assert (await client.async_set_mode("24")).state.mode == "24"
    assert (await client.async_set_mode("0")).state.mode == "0"
    posts = [call for call in backend["calls"] if call[0] == "POST"]
    assert [call[2] for call in posts] == [{"autoResponse": "24"}, {"autoResponse": "0"}]


async def test_reapply_is_explicit_write(backend):
    backend["mode"] = "24"
    await backend["client"].async_set_mode("24")
    assert sum(call[0] == "POST" for call in backend["calls"]) == 1


@pytest.mark.parametrize("mode,exception", [
    ("23", MansionUnsafeModeError), ("99", MansionProtocolError),
    ("does-not-exist", MansionProtocolError),
])
async def test_unapproved_or_unavailable_mode_never_writes(backend, mode, exception):
    with pytest.raises(exception):
        await backend["client"].async_set_mode(mode)
    assert all(call[0] != "POST" for call in backend["calls"])


async def test_explicit_unlock_opt_in_only_in_mock(backend):
    result = await backend["client"].async_set_mode("23", allow_unlock=True)
    assert result.state.mode == "23"


async def test_changed_unlock_flag_is_checked_again(backend):
    await backend["client"].async_get_snapshot()
    backend["presets"][0]["unLock"] = True
    with pytest.raises(MansionUnsafeModeError):
        await backend["client"].async_set_mode("24")
    assert all(call[0] != "POST" for call in backend["calls"])


async def test_no_optimistic_success(backend):
    backend["write_applies"] = False
    with pytest.raises(MansionWriteVerificationError):
        await backend["client"].async_set_mode("24")
    assert sum(call[0] == "POST" for call in backend["calls"]) == 1


@pytest.mark.parametrize("status,code", [(400, "invalidUser"), (400, "authFailed"), (401, "expired")])
async def test_auth_failures_require_reauthentication(backend, status, code):
    backend["error"] = (status, code)
    with pytest.raises(MansionAuthError) as caught:
        await backend["client"].async_get_state()
    assert caught.value.code == code
    assert "test-key" not in str(caught.value)


async def test_timeout_is_not_retried(backend):
    await backend["client"].async_get_state()
    backend["calls"].clear()
    backend["delay"] = 0.2
    backend["client"]._timeout = aiohttp.ClientTimeout(total=0.05)
    with pytest.raises(MansionConnectionError):
        await backend["client"].async_get_state()
    assert len(backend["calls"]) == 1


async def test_concurrent_writes_are_serialized(backend):
    client = backend["client"]
    enabled, disabled = await asyncio.gather(client.async_set_mode("24"), client.async_set_mode("0"))
    assert enabled.state.mode == "24"
    assert disabled.state.mode == "0"


async def test_unknown_current_pattern_is_not_reported_off(backend):
    backend["mode"] = "new-mode"
    assert (await backend["client"].async_get_state()).mode == "new-mode"


async def test_duplicate_pattern_fails_before_write(backend):
    backend["presets"].append(dict(backend["presets"][0]))
    with pytest.raises(MansionProtocolError):
        await backend["client"].async_set_mode("24")
    assert all(call[0] != "POST" for call in backend["calls"])


@pytest.mark.parametrize("enabled", [True, "true"])
def test_boolean_representation(enabled):
    assert RoomState.from_json({"autoRes": enabled, "autoResPattern": "24"}).enabled


def test_missing_unlock_flag_is_not_assumed_safe():
    with pytest.raises(MansionProtocolError):
        Preset.from_json({"autoResPattern": "24", "title": "测试", "preset": True})


@pytest.mark.parametrize("payload", [{}, {"autoRes": "unknown"}, {"autoRes": True, "autoResPattern": ""}])
def test_invalid_state_is_not_fabricated(payload):
    with pytest.raises(MansionProtocolError):
        RoomState.from_json(payload)


async def test_pairing_renewal_uses_existing_auth_without_login(backend):
    result = await backend["client"].async_renew_pairing("test-pairing-code", "test-push-token")
    assert result.expires == "2099/07/01"
    assert [call[1] for call in backend["calls"]] == [
        "/v1/app_user/test-user/room/pairing", "/v1/app_user/test-user/regist_push_token"
    ]
