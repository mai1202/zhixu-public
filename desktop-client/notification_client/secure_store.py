"""用 Windows 当前用户 DPAPI 保护设备凭证和本地队列正文。"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class SecretProtector:
    """客户端敏感数据保护接口，便于合成测试注入假实现。"""

    def protect(self, value: bytes) -> bytes:
        raise NotImplementedError

    def unprotect(self, value: bytes) -> bytes:
        raise NotImplementedError


class WindowsDpapiProtector(SecretProtector):
    """将数据加密绑定到当前 Windows 用户，不把密钥写进配置文件。"""

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    _ENTROPY = b"notification-management-client-v1"

    @staticmethod
    def _blob(value: bytes):
        buffer = ctypes.create_string_buffer(max(1, len(value)))
        if value:
            ctypes.memmove(buffer, value, len(value))
        blob = WindowsDpapiProtector._Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        return blob, buffer

    def protect(self, value: bytes) -> bytes:
        if os.name != "nt":
            raise RuntimeError("设备凭证只能在 Windows DPAPI 环境中保护。")
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(self._Blob), wintypes.LPCWSTR, ctypes.POINTER(self._Blob),
            wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(self._Blob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        source, source_buffer = self._blob(value)
        entropy, entropy_buffer = self._blob(self._ENTROPY)
        target = self._Blob()
        success = crypt32.CryptProtectData(
            ctypes.byref(source), "Notification Management", ctypes.byref(entropy), None, None,
            0x1, ctypes.byref(target),
        )
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            kernel32.LocalFree(target.pbData)

    def unprotect(self, value: bytes) -> bytes:
        if os.name != "nt":
            raise RuntimeError("设备凭证只能在 Windows DPAPI 环境中解密。")
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(self._Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(self._Blob),
            wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(self._Blob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        source, source_buffer = self._blob(value)
        entropy, entropy_buffer = self._blob(self._ENTROPY)
        target = self._Blob()
        success = crypt32.CryptUnprotectData(
            ctypes.byref(source), None, ctypes.byref(entropy), None, None,
            0x1, ctypes.byref(target),
        )
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            kernel32.LocalFree(target.pbData)


class TestProtector(SecretProtector):
    """仅供合成测试使用的可逆编码器，不可用于真实凭证。"""

    _PREFIX = b"test-only:"

    def protect(self, value: bytes) -> bytes:
        return self._PREFIX + value

    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(self._PREFIX):
            raise ValueError("合成保护数据格式无效")
        return value[len(self._PREFIX):]
