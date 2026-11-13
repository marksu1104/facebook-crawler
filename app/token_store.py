"""Protect remembered tokens using DPAPI or a key held in the OS credential store."""
import ctypes
import hashlib
import os
import re
import sys
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


class SystemKeyringCipher:
    prefix = b"social-watch:keyring-fernet:v1\0"
    service = "facebook-crawler"

    def __init__(self, data_dir, backend=None):
        self.data_dir = data_dir
        self.account = hashlib.sha256(str(data_dir.resolve()).encode("utf-8")).hexdigest()
        self.backend = backend or system_keyring()
        self.supported = self.backend is not None

    def _fernet(self, account, *, create=False):
        from cryptography.fernet import Fernet

        if not self.supported:
            raise TokenStorageError("請先啟用並解鎖系統鑰匙圈，或取消記住 Token 後儲存。")
        if create:
            # Serialise first-key creation across app processes sharing this profile.
            # This path is used only on macOS and Linux.
            import fcntl

            self.data_dir.mkdir(parents=True, exist_ok=True)
            with (self.data_dir / "token-key.lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                key = self.backend.get_password(self.service, account)
                if key is None:
                    key = Fernet.generate_key().decode("ascii")
                    self.backend.set_password(self.service, account, key)
        else:
            key = self.backend.get_password(self.service, account)
        if not key:
            raise TokenStorageError("系統鑰匙圈中的加密金鑰無法讀取，請重新設定 Token。")
        return Fernet(key.encode("ascii"))

    def encrypt(self, token):
        value = token.encode("utf-8")
        if not 0 < len(value) <= WindowsTokenCipher.max_token_bytes:
            raise TokenStorageError("Token 長度不正確，請重新輸入。")
        try:
            encrypted = self._fernet(self.account, create=True).encrypt(value)
            return self.prefix + self.account.encode("ascii") + b"\0" + encrypted
        except Exception:
            raise TokenStorageError("無法使用系統鑰匙圈保存 Token。請確認已解鎖，或取消記住 Token 後儲存。") from None

    def decrypt(self, encrypted):
        if (not isinstance(encrypted, bytes) or not encrypted.startswith(self.prefix)
                or len(encrypted) > WindowsTokenCipher.max_blob_bytes):
            raise TokenStorageError("已儲存的 Token 格式不正確，請重新輸入並儲存。")
        try:
            account, value = encrypted[len(self.prefix):].split(b"\0", 1)
            if not re.fullmatch(rb"[a-f0-9]{64}", account):
                raise ValueError
            token = self._fernet(account.decode("ascii")).decrypt(value).decode("utf-8")
            if not 0 < len(token.encode("utf-8")) <= WindowsTokenCipher.max_token_bytes:
                raise ValueError
            return token
        except Exception:
            raise TokenStorageError("無法使用系統鑰匙圈讀取 Token，請確認已解鎖或重新設定。") from None


def system_keyring():
    """Use the native credential service directly, avoiding third-party fallbacks."""
    try:
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring
        elif sys.platform.startswith("linux"):
            from keyring.backends.SecretService import Keyring
        else:
            return None
        backend = Keyring()
        return backend if backend.priority > 0 else None
    except Exception:
        return None


def token_storage_supported():
    return WindowsTokenCipher.supported or system_keyring() is not None


def default_token_cipher(data_dir):
    return WindowsTokenCipher() if os.name == "nt" else SystemKeyringCipher(data_dir)
