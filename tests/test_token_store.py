import json
import os
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.pilot import Pilot, create_pilot_app
from app.token_store import SystemKeyringCipher, TokenStorageError, WindowsTokenCipher, system_keyring


TOKEN = "123456789:ISOLATED_NATIVE_DPAPI_TEST"


@pytest.mark.skipif(os.name != "nt", reason="Native DPAPI requires Windows")
def test_native_dpapi_roundtrip_across_instances_and_tamper_detection():
    encrypted = WindowsTokenCipher().encrypt(TOKEN)
    assert encrypted.startswith(WindowsTokenCipher.prefix)
    assert TOKEN.encode() not in encrypted
    assert WindowsTokenCipher().decrypt(encrypted) == TOKEN
    damaged = bytearray(encrypted)
    damaged[-1] ^= 1
    with pytest.raises(TokenStorageError):
        WindowsTokenCipher().decrypt(bytes(damaged))


@pytest.mark.skipif(os.name != "nt", reason="Native DPAPI requires Windows")
def test_normal_settings_api_persists_and_restores_native_ciphertext(tmp_path):
    class InertBrowser:
        driver = None

        def close(self):
            pass

    def app_factory(path):
        # Constructing TelegramClient validates locally. No Telegram operation is invoked.
        return Pilot(path, browser=InertBrowser())

    app = create_pilot_app(tmp_path, pilot_factory=app_factory)
    with TestClient(app, headers={"X-App-Request": "1"}) as client:
        response = client.post("/api/settings", json={"telegram_token": TOKEN, "remember_telegram_token": True})
        assert response.status_code == 200
        assert response.json()["settings"]["telegram_token_saved"] is True
        assert TOKEN not in response.text
        assert WindowsTokenCipher().decrypt(app.state.pilot.db.saved_token()) == TOKEN
    assert not app.state.pilot.token
    restarted_app = create_pilot_app(tmp_path, pilot_factory=app_factory)
    with TestClient(restarted_app, headers={"X-App-Request": "1"}) as client:
        state = client.get("/api/state").json()
        assert state["settings"]["telegram_configured"] is True
        assert state["settings"]["telegram_token_saved"] is True
        assert restarted_app.state.pilot.token == TOKEN
        assert not state["status"]["auto_running"] and not state["status"]["browser_open"]
        assert TOKEN not in json.dumps(state)
        assert client.post("/api/telegram/forget").status_code == 200
    assert not app_factory(tmp_path).token
    for path in tmp_path.glob("pilot.sqlite3*"):
        assert TOKEN.encode() not in path.read_bytes()


@pytest.mark.parametrize("value", [b"", b"invalid", WindowsTokenCipher.prefix, WindowsTokenCipher.prefix + b"x" * 65537])
def test_invalid_saved_data_is_rejected_before_decryption(value):
    with pytest.raises(TokenStorageError):
        WindowsTokenCipher().decrypt(value)


def test_unsupported_platform_has_no_plaintext_fallback(monkeypatch):
    cipher = WindowsTokenCipher()
    monkeypatch.setattr(cipher, "supported", False)
    with pytest.raises(TokenStorageError):
        cipher.encrypt(TOKEN)


@pytest.mark.parametrize("token", ["", "x" * 8193])
def test_invalid_token_length_is_rejected(token):
    with pytest.raises(TokenStorageError):
        WindowsTokenCipher().encrypt(token)


class MemoryKeyring:
    def __init__(self):
        self.values = {}
        self.locked = False

    def get_password(self, service, account):
        if self.locked:
            raise RuntimeError(TOKEN)
        return self.values.get((service, account))

    def set_password(self, service, account, password):
        if self.locked:
            raise RuntimeError(TOKEN)
        self.values[service, account] = password


@pytest.fixture
def native_keyring_double(monkeypatch):
    if os.name == "nt":
        # The production Windows path uses DPAPI. Exercise the portable format
        # here; native POSIX locking is covered on macOS and Linux.
        monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(LOCK_EX=2, flock=lambda *args: None))
    return MemoryKeyring()


def test_keyring_cipher_restores_across_instances_and_profile_moves(tmp_path, native_keyring_double):
    cipher = SystemKeyringCipher(tmp_path, backend=native_keyring_double)
    encrypted = cipher.encrypt(TOKEN)
    assert encrypted.startswith(SystemKeyringCipher.prefix)
    assert TOKEN.encode() not in encrypted
    assert TOKEN not in native_keyring_double.values.values()
    assert SystemKeyringCipher(tmp_path, backend=native_keyring_double).decrypt(encrypted) == TOKEN
    moved = SystemKeyringCipher(tmp_path / "moved", backend=native_keyring_double)
    assert moved.decrypt(encrypted) == TOKEN
    assert len(native_keyring_double.values) == 1


def test_keyring_cipher_reuses_its_key_and_isolates_profiles(tmp_path, native_keyring_double):
    cipher = SystemKeyringCipher(tmp_path, backend=native_keyring_double)
    first = cipher.encrypt(TOKEN)
    second = cipher.encrypt(TOKEN)
    assert first != second
    assert len(native_keyring_double.values) == 1
    other = SystemKeyringCipher(tmp_path / "other", backend=native_keyring_double)
    assert other.decrypt(other.encrypt(TOKEN)) == TOKEN
    assert len(native_keyring_double.values) == 2


@pytest.mark.parametrize("damage", ["ciphertext", "account", "missing-key"])
def test_keyring_cipher_rejects_tampering_and_missing_keys_without_recreating_them(
        tmp_path, native_keyring_double, damage):
    cipher = SystemKeyringCipher(tmp_path, backend=native_keyring_double)
    encrypted = cipher.encrypt(TOKEN)
    if damage == "ciphertext":
        encrypted = encrypted[:-8] + b"damaged!"
    elif damage == "account":
        encrypted = cipher.prefix + b"x" * 64 + encrypted[len(cipher.prefix) + 64:]
    else:
        native_keyring_double.values.clear()
    before = dict(native_keyring_double.values)
    with pytest.raises(TokenStorageError):
        cipher.decrypt(encrypted)
    assert native_keyring_double.values == before


@pytest.mark.parametrize("value", [b"", b"invalid", SystemKeyringCipher.prefix,
                                  WindowsTokenCipher.prefix + b"foreign",
                                  SystemKeyringCipher.prefix + b"x" * 65537])
def test_keyring_cipher_rejects_invalid_and_foreign_data(tmp_path, native_keyring_double, value):
    with pytest.raises(TokenStorageError):
        SystemKeyringCipher(tmp_path, backend=native_keyring_double).decrypt(value)
    assert not native_keyring_double.values


def test_locked_keyring_errors_do_not_expose_tokens_or_overwrite_saved_data(tmp_path, native_keyring_double):
    cipher = SystemKeyringCipher(tmp_path, backend=native_keyring_double)
    encrypted = cipher.encrypt(TOKEN)
    native_keyring_double.locked = True
    with pytest.raises(TokenStorageError) as failure:
        cipher.encrypt(TOKEN)
    assert TOKEN not in str(failure.value)
    with pytest.raises(TokenStorageError) as failure:
        cipher.decrypt(encrypted)
    assert TOKEN not in str(failure.value)
    native_keyring_double.locked = False
    assert cipher.decrypt(encrypted) == TOKEN


def test_keyring_settings_api_restores_and_forgets_encrypted_tokens(tmp_path, native_keyring_double):
    class InertBrowser:
        driver = None

        def close(self):
            pass

    def app_factory(path):
        return Pilot(path, browser=InertBrowser(), token_cipher=SystemKeyringCipher(path, backend=native_keyring_double))

    app = create_pilot_app(tmp_path, pilot_factory=app_factory)
    with TestClient(app, headers={"X-App-Request": "1"}) as client:
        response = client.post("/api/settings", json={"telegram_token": TOKEN, "remember_telegram_token": True})
        assert response.status_code == 200
        assert response.json()["settings"]["telegram_token_saved"] is True
        assert TOKEN not in response.text
        saved = app.state.pilot.db.saved_token()
        native_keyring_double.locked = True
        response = client.post("/api/settings", json={"telegram_token": TOKEN, "remember_telegram_token": True,
                                                    "keywords": ["replacement"]})
        assert response.status_code == 400 and TOKEN not in response.text
        assert app.state.pilot.db.saved_token() == saved
        assert app.state.pilot.settings["keywords"] == []
        native_keyring_double.locked = False
    restarted = create_pilot_app(tmp_path, pilot_factory=app_factory)
    with TestClient(restarted, headers={"X-App-Request": "1"}) as client:
        assert restarted.state.pilot.token == TOKEN
        assert client.get("/api/state").json()["settings"]["telegram_configured"] is True
        assert client.post("/api/telegram/forget").status_code == 200
        assert restarted.state.pilot.db.saved_token() is None
    assert not app_factory(tmp_path).token
    for path in tmp_path.glob("pilot.sqlite3*"):
        assert TOKEN.encode() not in path.read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="POSIX locking is used by macOS and Linux")
def test_concurrent_key_creation_preserves_both_ciphertexts(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import time

    class SlowKeyring(MemoryKeyring):
        def set_password(self, *args):
            time.sleep(0.03)
            super().set_password(*args)

    backend = SlowKeyring()
    cipher = SystemKeyringCipher(tmp_path, backend=backend)
    with ThreadPoolExecutor(max_workers=2) as pool:
        encrypted = list(pool.map(cipher.encrypt, [TOKEN, TOKEN]))
    assert [cipher.decrypt(value) for value in encrypted] == [TOKEN, TOKEN]
    assert len(backend.values) == 1


@pytest.mark.skipif(os.environ.get("SOCIAL_WATCH_NATIVE_KEYRING") != "1", reason="Opt-in native OS keyring test")
def test_native_system_keyring_roundtrip(tmp_path):
    backend = system_keyring()
    assert backend is not None, "The native OS keyring must be running and unlocked."
    cipher = SystemKeyringCipher(tmp_path, backend=backend)
    try:
        encrypted = cipher.encrypt(TOKEN)
        assert SystemKeyringCipher(tmp_path, backend=backend).decrypt(encrypted) == TOKEN
        assert TOKEN.encode() not in encrypted
    finally:
        if backend.get_password(cipher.service, cipher.account) is not None:
            backend.delete_password(cipher.service, cipher.account)
