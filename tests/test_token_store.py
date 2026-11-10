import json
import os

import pytest
from fastapi.testclient import TestClient

from app.pilot import Pilot, create_pilot_app
from app.token_store import TokenStorageError, WindowsTokenCipher


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
