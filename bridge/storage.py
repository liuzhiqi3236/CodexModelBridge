"""本地原子文件操作与 Windows 当前用户凭据保护。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .errors import BridgeError


def atomic_write(path: Path, content: bytes) -> None:
    """同目录临时文件落盘后替换，避免中断留下半份配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bridge-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value: object) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(content: bytes, *, decrypt: bool = False) -> bytes:
    """DPAPI 绑定当前 Windows 用户；禁止保存明文的失败替代路径。"""
    if os.name != "nt":
        raise BridgeError("凭据保护需要 Windows 当前用户环境。")
    buffer = ctypes.create_string_buffer(content)
    source = DataBlob(len(content), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = DataBlob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = library.CryptUnprotectData
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        fn = library.CryptProtectData
        ok = fn(ctypes.byref(source), "ModelBridge", None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise BridgeError("Windows 凭据加密或解密失败，请使用保存此配置的 Windows 账号。")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def mask_key(value: str) -> str:
    return value[:3] + "••••••••" + value[-4:] if len(value) >= 10 else "••••••••"
