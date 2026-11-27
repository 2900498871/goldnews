# -*- coding: utf-8 -*-
"""本端密钥安全存储。

Windows 优先用系统 DPAPI（CryptProtectData，按当前用户账户加密，无需第三方库）；
其它平台降级为机器绑定密钥的流混淆，并在 README 中明示其强度较低。
密钥永远不写入 config.json、SQLite、日志、邮件或导出文件，也不跨实例复制。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import sys
import uuid

REF_PREFIX = "secure-store://"


class _BLOB:
    pass


if platform.system() == "Windows":
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    def _dpapi_protect(data: bytes, entropy: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        ent = ctypes.create_string_buffer(entropy, len(entropy))
        inp = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        ein = _DATA_BLOB(len(entropy), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))
        out = _DATA_BLOB()
        ok = _crypt32.CryptProtectData(ctypes.byref(inp), None, ctypes.byref(ein),
                                       None, None, 0x01, ctypes.byref(out))
        if not ok:
            raise RuntimeError("DPAPI CryptProtectData 失败")
        try:
            blob = ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
        return blob

    def _dpapi_unprotect(blob: bytes, entropy: bytes) -> bytes:
        buf = ctypes.create_string_buffer(blob, len(blob))
        ent = ctypes.create_string_buffer(entropy, len(entropy))
        inp = _DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        ein = _DATA_BLOB(len(entropy), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))
        out = _DATA_BLOB()
        ok = _crypt32.CryptUnprotectData(ctypes.byref(inp), None, ctypes.byref(ein),
                                         None, None, 0x01, ctypes.byref(out))
        if not ok:
            raise RuntimeError("DPAPI CryptUnprotectData 失败（密钥可能由其它账户写入）")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def _fallback_key(entropy: bytes) -> bytes:
    material = (platform.node() + "|" + str(uuid.getnode()) + "|" + sys.platform).encode("utf-8")
    return hashlib.sha256(material + entropy).digest()


def _stream(data: bytes, key: bytes) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        key += hashlib.sha256(key + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(data, key))


class SecretsStore:
    """每个实例一个独立文件：data_dir/secrets.bin。"""

    def __init__(self, data_dir: str, instance_id: str):
        self.path = os.path.join(data_dir, "secrets.bin")
        self.entropy = ("golddesk|" + instance_id).encode("utf-8")
        self.windows = platform.system() == "Windows"
        self._data = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            out = {}
            for k, v in raw.items():
                blob = base64.b64decode(v)
                dec = _dpapi_unprotect(blob, self.entropy) if self.windows else _stream(blob, _fallback_key(self.entropy))
                out[k] = dec.decode("utf-8")
            return out
        except Exception:
            # 密钥文件损坏时返回空，而不是让程序崩溃；界面会提示重新录入
            return {}

    def _save(self):
        raw = {}
        for k, v in self._data.items():
            enc = _dpapi_protect(v.encode("utf-8"), self.entropy) if self.windows else _stream(v.encode("utf-8"), _fallback_key(self.entropy))
            raw[k] = base64.b64encode(enc).decode("ascii")
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, name: str) -> str | None:
        return self._data.get(name)

    def set(self, name: str, value: str):
        if value:
            self._data[name] = value
        else:
            self._data.pop(name, None)
        self._save()

    def has(self, name: str) -> bool:
        return bool(self._data.get(name))

    def delete(self, name: str):
        self._data.pop(name, None)
        self._save()

    def names(self) -> list[str]:
        return sorted(self._data)

    @staticmethod
    def ref(name: str) -> str:
        return REF_PREFIX + name

    @staticmethod
    def is_ref(value) -> bool:
        return isinstance(value, str) and value.startswith(REF_PREFIX)
