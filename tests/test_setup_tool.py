"""Test password-changing onboarding order and the private local UI offline."""

from http.client import HTTPConnection
from pathlib import Path
import sys
import threading
from urllib.parse import urlencode

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import account_setup as ui
import protocol_setup as setup
from protocol_setup import ApiFailure
from session_codec import decode_session


@pytest.fixture
def workflow(monkeypatch):
    trace = {"events": [], "saved": [], "records": [], "fail": None}
    seed = {"user_id": "test-user", "api_key": "test-key",
            "pair_hash": "test-pairing-code", "push_token": "test-push-token"}
    monkeypatch.setattr(setup, "load_session", lambda: dict(seed))
    monkeypatch.setattr(setup, "save_session", lambda session: trace["saved"].append(dict(session)))
    monkeypatch.setattr(setup, "record", lambda stage, **data: trace["records"].append((stage, data)))

    def request(path, *, session=None, body=None):
        if path.endswith("/login"):
            trace["events"].append("login")
            assert body == {"name": "test-account", "password": "test-current-password"}
            return {"userId": "test-user", "acApiKey": "test-key"}
        assert path == "/v1/app_user/test-user/init"
        trace["events"].append("init")
        assert body == {"oldPassword": "test-current-password", "newPassword": "test-new-password", "nickName": "Home Assistant"}
        if trace["fail"] == "init-timeout":
            raise TimeoutError
        return {}

    def pair(session):
        trace["events"].append("pair_and_register")
        if trace["fail"] == "initial-pair":
            raise ApiFailure(400, "authFailed")
        return {"expireDate": "2099/01/01"}

    def read(session):
        trace["events"].append("read")
        if trace["fail"] == "final-read":
            raise ApiFailure(400, "invalidUser")
        return {"state": {"autoRes": False, "autoResPattern": ""}}

    monkeypatch.setattr(setup, "request_api", request)
    monkeypatch.setattr(setup, "pair_and_register", pair)
    monkeypatch.setattr(setup, "read_room", read)
    return trace


def register():
    return setup.complete_registration("test-account", "test-current-password", "test-new-password", "Home Assistant")


def test_registration_order_and_no_password_storage(workflow):
    register()
    assert workflow["events"] == ["login", "pair_and_register", "login", "init", "pair_and_register", "read"]
    assert workflow["records"][-1] == ("complete", {"password_changed": True})
    assert all("test-new-password" not in repr(saved) and "test-current-password" not in repr(saved)
               for saved in workflow["saved"])


def test_pair_failure_stops_before_password_change(workflow):
    workflow["fail"] = "initial-pair"
    with pytest.raises(ApiFailure):
        register()
    assert "init" not in workflow["events"]
    assert workflow["records"][-1][1]["password_changed"] is False


def test_late_failure_preserves_password_change_progress(workflow):
    workflow["fail"] = "final-read"
    with pytest.raises(ApiFailure):
        register()
    assert workflow["records"][-1][1]["password_changed"] is True


def test_uncertain_password_change_is_not_retried(workflow):
    workflow["fail"] = "init-timeout"
    with pytest.raises(TimeoutError):
        register()
    assert workflow["events"].count("init") == 1
    assert workflow["records"][-1][1]["password_change_uncertain"] is True


def test_finish_does_not_login_or_change_password(workflow):
    setup.finish_registration()
    assert workflow["events"] == ["pair_and_register", "read"]
    assert workflow["records"][-1][0] == "complete"


@pytest.fixture
def local_ui(socket_enabled, monkeypatch, session_data):
    status = {"stage": "complete", "password_changed": True}
    monkeypatch.setattr(ui, "load_session", lambda: dict(session_data))
    monkeypatch.setattr(ui, "read_status", lambda: dict(status))
    server = ui.SetupServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, status
    server.shutdown()
    server.server_close()
    thread.join()


def request(server, suffix="", *, form=None, host=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    headers = {"Host": host or server.host_header}
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    connection.request("POST" if form is not None else "GET", server.route + suffix,
                       urlencode(form) if form is not None else None, headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read().decode("utf-8")
    connection.close()
    return result


def test_export_is_local_no_store_and_contains_valid_code(local_ui, session_data):
    server, _ = local_ui
    status, headers, body = request(server, "/export")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    code = body.split("autocomplete='off'>", 1)[1].split("</textarea>", 1)[0]
    assert decode_session(code) == session_data
    assert "MSHA1." not in request(server)[2]


def test_wrong_host_and_missing_csrf_are_rejected(local_ui):
    server, _ = local_ui
    assert request(server, "/export", host="untrusted.example")[0] == 404
    assert request(server, "/register", form={"csrf": "wrong"})[0] == 403


def test_uncertain_change_blocks_new_registration(local_ui, monkeypatch):
    server, status = local_ui
    status.update(stage="failed", password_changed=False, password_change_uncertain=True)
    monkeypatch.setattr(ui, "complete_registration", lambda *args: pytest.fail("Must not change password again"))
    response = request(server, "/register", form={"csrf": server.csrf})
    assert response[0] == 200
    assert "需要确认密码状态" in response[2]


def test_duplicate_password_form_is_not_replayed(local_ui, monkeypatch):
    server, _ = local_ui
    calls = []
    monkeypatch.setattr(ui, "complete_registration", lambda *args: calls.append(args))
    old_csrf = server.csrf
    form = {"csrf": old_csrf, "name": "test-account", "old_password": "old-password",
            "new_password": "new-password", "confirm": "new-password", "nickname": "HA", "confirm_change": "yes"}
    assert request(server, "/register", form=form)[0] == 200
    assert request(server, "/register", form=form)[0] == 403
    assert len(calls) == 1
