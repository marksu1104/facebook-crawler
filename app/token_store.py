"""Protect a locally remembered token with the current Windows user's DPAPI."""
import ctypes
import os
from ctypes import wintypes


class TokenStorageError(Exception):
    """A storage failure whose message is safe to display."""


class WindowsTokenCipher:
    supported = os.name == "nt"
    prefix = b"social-watch:dpapi-user:v1\0"
    max_token_bytes = 8192
    max_blob_bytes = 65536

    def _transform(self, value: bytes, *, decrypt: bool) -> bytes:
        if not self.supported:
            raise TokenStorageError("此系統尚未支援加密保存 Token，請取消記住 Token 後儲存。")

        class DataBlob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        buffer = ctypes.create_string_buffer(value, len(value))
        incoming = DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        outgoing = DataBlob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        operation.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
        operation.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        try:
            # UI_FORBIDDEN only. LOCAL_MACHINE would permit other Windows users.
            if not operation(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
                raise TokenStorageError("無法使用目前的 Windows 帳戶讀寫加密 Token，請重新設定。")
            if not outgoing.data or not 0 < outgoing.size <= self.max_blob_bytes:
                raise TokenStorageError("加密 Token 資料格式不正確，請重新設定。")
            return ctypes.string_at(outgoing.data, outgoing.size)
        finally:
            ctypes.memset(ctypes.addressof(buffer), 0, len(value))
            if outgoing.data:
                if decrypt:
                    ctypes.memset(outgoing.data, 0, outgoing.size)
                kernel.LocalFree(outgoing.data)

    def encrypt(self, token: str) -> bytes:
        value = token.encode("utf-8")
        if not 0 < len(value) <= self.max_token_bytes:
            raise TokenStorageError("Token 長度不正確，請重新輸入。")
        return self.prefix + self._transform(value, decrypt=False)

    def decrypt(self, encrypted: bytes) -> str:
        if not isinstance(encrypted, bytes) or not encrypted.startswith(self.prefix):
            raise TokenStorageError("已儲存的 Token 格式不正確，請重新輸入並儲存。")
        value = encrypted[len(self.prefix):]
        if not 0 < len(value) <= self.max_blob_bytes:
            raise TokenStorageError("已儲存的 Token 格式不正確，請重新輸入並儲存。")
        try:
            token = self._transform(value, decrypt=True).decode("utf-8")
        except UnicodeError:
            raise TokenStorageError("已儲存的 Token 格式不正確，請重新輸入並儲存。") from None
        if not token or len(token.encode("utf-8")) > self.max_token_bytes:
            raise TokenStorageError("已儲存的 Token 格式不正確，請重新輸入並儲存。")
        return token
