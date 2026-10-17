"""Normal account registration, used only on explicit local form submission."""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from local_state import load_session, record, save_session

BASE_URL = "https://secom-dmc.smu.10fw.co.jp"


class ApiFailure(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code) else "unknownError"
        super().__init__(f"HTTP {status}: {self.code}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_api(path: str, *, session: dict | None = None, body: dict | None = None) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "okhttp/4.12.0"}
    if session:
        headers["Authorization"] = "GEAR-API-KEY " + session["api_key"]
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(BASE_URL + path, data=data, headers=headers)
    try:
        with build_opener(NoRedirect).open(request, timeout=20) as response:
            payload = json.load(response)
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            return payload
    except HTTPError as error:
        try:
            payload = json.loads(error.read(65536))
            code = str(payload.get("error", "unknownError"))
        except (ValueError, AttributeError):
            code = "unknownError"
        raise ApiFailure(error.code, code) from None


def pair_and_register(session: dict) -> dict:
    user = quote(session["user_id"], safe="")
    result = request_api(f"/v1/app_user/{user}/room/pairing", session=session, body={
        "platform": "android", "hash": session["pair_hash"],
        "osVersion": "android16", "phoneModel": "xxxxx",
    })
    for field in ("expireDate", "validTerm"):
        if field in result:
            session[field] = str(result[field])
    save_session(session)
    request_api(f"/v1/app_user/{user}/regist_push_token", session=session, body={
        "platform": "android", "token": session["push_token"],
    })
    return result


def read_room(session: dict) -> dict:
    user = quote(session["user_id"], safe="")
    return {
        "modes": request_api(f"/v1/app_user/{user}/room/autores_list", session=session),
        "state": request_api(f"/v1/app_user/{user}/room/setting", session=session),
    }


def finish_registration() -> dict:
    """Resume only pairing/reading after a confirmed password change."""
    session = load_session()
    try:
        record("final_pair_and_register", password_changed=True)
        pair_and_register(session)
        record("read_room", password_changed=True)
        result = read_room(session)
        record("complete", password_changed=True)
        return result
    except Exception as error:
        details = {"password_changed": True, "failed_stage": "finish_registration",
                   "error_type": type(error).__name__}
        if isinstance(error, ApiFailure):
            details.update(status=error.status, code=error.code)
        record("failed", **details)
        raise


def complete_registration(name: str, old_password: str, new_password: str, nickname: str):
    seed = load_session()
    stage = "login"
    password_changed = False
    try:
        record(stage)
        payload = request_api("/v1/app_user/login", body={"name": name, "password": old_password})
        if not payload.get("userId") or not payload.get("acApiKey"):
            raise ValueError("Unexpected login response")
        session = {"user_id": str(payload["userId"]), "api_key": str(payload["acApiKey"]),
                   "pair_hash": seed["pair_hash"], "push_token": seed["push_token"], "login_name": name}
        if session["user_id"] != seed["user_id"]:
            raise ValueError("The account differs from the imported session")
        save_session(session)
        stage = "initial_pair_and_register"
        record(stage)
        pair_and_register(session)
        stage = "refresh_before_initialization"
        record(stage)
        refreshed = request_api("/v1/app_user/login", body={"name": name, "password": old_password})
        if str(refreshed.get("userId")) != session["user_id"] or not refreshed.get("acApiKey"):
            raise ValueError("Login identity unexpectedly changed")
        session["api_key"] = str(refreshed["acApiKey"])
        save_session(session)
        stage = "initialize_password_and_nickname"
        record(stage)
        request_api(f"/v1/app_user/{quote(session['user_id'], safe='')}/init", session=session, body={
            "oldPassword": old_password, "newPassword": new_password, "nickName": nickname,
        })
        password_changed = True
        session["nickname"] = nickname
        save_session(session)
        record("password_changed", password_changed=True)
        stage = "final_pair_and_register"
        record(stage, password_changed=True)
        pair_and_register(session)
        stage = "read_room"
        record(stage, password_changed=True)
        result = read_room(session)
        record("complete", password_changed=True)
        return result
    except Exception as error:
        details = {"password_changed": password_changed, "failed_stage": stage,
                   "error_type": type(error).__name__,
                   "password_change_uncertain": stage == "initialize_password_and_nickname" and not isinstance(error, ApiFailure)}
        if isinstance(error, ApiFailure):
            details.update(status=error.status, code=error.code)
        record("failed", **details)
        raise
