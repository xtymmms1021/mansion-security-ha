"""Windows-only encrypted local state; no account password is stored."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path

STATE_DIR = Path(os.environ.get(
    "MANSION_SECURITY_STATE_DIR",
    str(Path(os.environ.get("LOCALAPPDATA", ".")) / "MansionSecurityHA"),
)).resolve()


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("The account setup tool requires Windows")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = DataBlob(len(data), buffer)
    target = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt32.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                       ctypes.POINTER(DataBlob)]
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        fn = crypt32.CryptProtectData
        fn.argtypes = [ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                       ctypes.POINTER(DataBlob)]
        args = (ctypes.byref(source), "Mansion Security HA session", None,
                None, None, 1, ctypes.byref(target))
    fn.restype = wintypes.BOOL
    if not fn(*args):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def save_session(session: dict) -> None:
    # All callers pass an allowlisted session, never a password.
    from session_codec import decode_session, encode_session

    normalized = decode_session(encode_session(session))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "session.dpapi"
    temp = path.with_suffix(".tmp")
    temp.write_bytes(_dpapi(json.dumps(normalized, ensure_ascii=False).encode("utf-8")))
    temp.replace(path)


def load_session() -> dict:
    return json.loads(_dpapi((STATE_DIR / "session.dpapi").read_bytes(), decrypt=True))


def read_status() -> dict:
    path = STATE_DIR / "registration-status.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def record(stage: str, **details) -> None:
    from datetime import datetime, timezone

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {"stage": stage, "time": datetime.now(timezone.utc).isoformat(), **details}
    (STATE_DIR / "registration-status.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    print("REGISTRATION_STAGE " + stage, flush=True)
