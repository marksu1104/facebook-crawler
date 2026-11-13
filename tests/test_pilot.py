import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.browser import canonical_post_url
from app.models import match_keywords
from app.pilot import Cooldown, Pilot, PilotBusy, PilotDatabase, PilotSettings, create_pilot_app
from app.pilot_browser import NeedsUserAction, PilotBrowser, PilotBrowserError, PilotTransientError, check_snapshot, pilot_group_url, post_belongs_to_group
from app.telegram import TelegramClient, TelegramError, TelegramRateLimited
from app.token_store import TokenStorageError

GROUP = "https://www.facebook.com/groups/123456"
TOKEN = "123456789:LOCAL_TEST_TOKEN_NEVER_REAL"
CHAT = "12345678"
HEADERS = {"X-App-Request": "1"}


class TestTokenCipher:
    """Portable test double. Production encryption is tested against native DPAPI separately."""
    __test__ = False
    supported = True

    def encrypt(self, token):
        return b"test-cipher\0" + bytes(value ^ 0xA5 for value in token.encode())

    def decrypt(self, encrypted):
        if not encrypted.startswith(b"test-cipher\0"):
            raise TokenStorageError("測試資料無法解密。")
        return bytes(value ^ 0xA5 for value in encrypted[12:]).decode()


@pytest.fixture(autouse=True)
def portable_token_cipher(monkeypatch):
    monkeypatch.setattr("app.pilot.WindowsTokenCipher", TestTokenCipher)


@pytest.fixture(autouse=True)
def immediate_dom_wait(monkeypatch):
    from selenium.common.exceptions import TimeoutException
    import selenium.webdriver.support.ui

    class ImmediateWait:
        def __init__(self, driver, timeout, poll_frequency):
            self.driver = driver

        def until(self, condition):
            for _ in range(2):
                result = condition(self.driver)
                if result:
                    return result
            raise TimeoutException()

    monkeypatch.setattr(selenium.webdriver.support.ui, "WebDriverWait", ImmediateWait)


def source(post_id="1", content="週末臨打缺人"):
    return {"post_id": post_id, "content": content, "url": f"{GROUP}/posts/{post_id}/",
            "author": "測試同學", "posted_at": "3 分鐘前"}


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds=120):
        self.value += seconds


class FakeBrowser:
    def __init__(self):
        self.driver = None
        self.posts = [source()]
        self.calls = []
        self.error = None

    def open(self, url, browser):
        self.calls.append(("open", url))
        self.driver = object()

    def confirm(self, source_url, expected=None):
        if self.error:
            raise self.error
        return GROUP

    def collect(self, expected, cancel, refresh=False, max_posts=20):
        if self.error:
            raise self.error
        self.calls.append(("collect", refresh))
        return [dict(post) for post in self.posts[:max_posts]]

    def close(self):
        self.driver = None


class FakeTelegram:
    def __init__(self, transport, token, chat=None):
        validated = TelegramClient(token, chat)
        self.chat = validated.chat_id
        self.transport = transport

    def get_me(self):
        self.transport.calls.append("get_me")
        return {"id": 123, "username": "local_test_bot"}

    def list_private_chats(self):
        self.transport.calls.append("get_updates")
        return [{"id": CHAT, "name": "我"}]

    def send_text(self, text, link=None, *, parse_mode=None):
        if self.transport.before_send:
            self.transport.before_send()
        if self.transport.error:
            raise self.transport.error
        self.transport.calls.append({"text": text, "link": link, "chat": self.chat, "parse_mode": parse_mode})
        return {"message_id": len(self.transport.calls), "chat_id": self.chat}


class Transport:
    def __init__(self):
        self.calls = []
        self.error = None
        self.before_send = None

    def client(self, token, chat=None):
        return FakeTelegram(self, token, chat)


def until(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Pilot did not reach expected state")


@pytest.fixture
def session(tmp_path):
    clock, browser, telegram = Clock(), FakeBrowser(), Transport()
    app = create_pilot_app(tmp_path, pilot_factory=lambda path: Pilot(path, browser, telegram.client, clock))
    with TestClient(app, headers=HEADERS) as client:
        yield client, app.state.pilot, browser, telegram, clock, tmp_path


def ready(session, **settings):
    client, pilot, browser, telegram, clock, path = session
    body = {"group_url": GROUP, "telegram_token": TOKEN, "telegram_chat_id": CHAT, **settings}
    assert client.post("/api/settings", json=body).status_code == 200
    assert client.post("/api/browser/open").status_code == 202
    until(lambda: not pilot.pending)
    assert client.post("/api/browser/confirm").status_code == 202
    until(lambda: not pilot.pending)
    assert client.post("/api/check").status_code == 202
    until(lambda: not pilot.pending)
    assert pilot.baseline


def test_launch_and_configuration_are_inert_and_token_never_leaks_as_plaintext(session):
    client, pilot, browser, telegram, clock, path = session
    state = client.get("/api/state").json()
    assert state["status"]["auto_running"] is False
    assert browser.calls == telegram.calls == []
    assert state["settings"]["group_url"] == ""
    response = client.post("/api/settings", json={"group_url": GROUP, "telegram_token": TOKEN})
    assert response.status_code == 200
    assert TOKEN not in response.text
    assert TOKEN not in client.get("/api/state").text
    assert TOKEN.encode() not in (path / "pilot.sqlite3").read_bytes()
    assert pilot.db.saved_token() != TOKEN.encode()
    assert response.json()["settings"]["telegram_token_saved"] is True
    assert browser.calls == telegram.calls == []


def test_remembered_token_is_loaded_after_shutdown_without_browser_or_telegram_requests(session):
    client, pilot, browser, telegram, clock, path = session
    assert client.post("/api/settings", json={"telegram_token": TOKEN, "telegram_chat_id": CHAT}).status_code == 200
    pilot.close()
    assert pilot.token == ""
    restarted = Pilot(path, browser, telegram.client, clock)
    assert restarted.token == TOKEN
    assert restarted.state()["settings"]["telegram_token_saved"] is True
    assert not restarted.auto and not restarted.baseline and not restarted.pending
    assert browser.calls == telegram.calls == []
    assert TOKEN not in json.dumps(restarted.state())


def test_remember_preference_can_be_disabled_and_enabled_using_the_current_token(session):
    client, pilot, browser, telegram, clock, path = session
    client.post("/api/settings", json={"telegram_token": TOKEN})
    response = client.post("/api/settings", json={"remember_telegram_token": False})
    assert response.status_code == 200
    assert pilot.token == TOKEN and pilot.db.saved_token() is None
    assert Pilot(path, FakeBrowser(), telegram.client).token == ""
    response = client.post("/api/settings", json={"remember_telegram_token": True})
    assert response.status_code == 200
    assert Pilot(path, FakeBrowser(), telegram.client).token == TOKEN
    replacement = "987654321:REPLACEMENT_TEST_TOKEN"
    assert client.post("/api/settings", json={"telegram_token": replacement}).status_code == 200
    assert Pilot(path, FakeBrowser(), telegram.client).token == replacement
    assert telegram.calls == []


def test_explicit_forget_removes_token_from_memory_and_future_starts(session):
    client, pilot, browser, telegram, clock, path = session
    client.post("/api/settings", json={"telegram_token": TOKEN, "telegram_chat_id": CHAT})
    response = client.post("/api/telegram/forget")
    assert response.status_code == 200
    assert not pilot.token and pilot.db.saved_token() is None
    assert not response.json()["settings"]["telegram_configured"]
    assert pilot.settings["telegram_chat_id"] == CHAT
    assert Pilot(path, FakeBrowser(), telegram.client).token == ""
    assert browser.calls == telegram.calls == []


@pytest.mark.parametrize("busy", ["auto", "pending"])
def test_forget_is_blocked_while_tracking_or_an_operation_is_pending(session, busy):
    client, pilot, browser, telegram, clock, path = session
    client.post("/api/settings", json={"telegram_token": TOKEN})
    if busy == "auto":
        pilot.auto = True
    else:
        pilot.pending.add("sending")
    try:
        assert client.post("/api/telegram/forget").status_code == 409
        assert pilot.token == TOKEN and pilot.db.saved_token()
    finally:
        pilot.auto = False
        pilot.pending.clear()


def test_encryption_failure_preserves_previous_settings_and_token(session, monkeypatch):
    client, pilot, browser, telegram, clock, path = session
    client.post("/api/settings", json={"telegram_token": TOKEN, "keywords": ["原設定"]})
    previous_settings, previous_blob = dict(pilot.settings), pilot.db.saved_token()

    def fail(token):
        raise TokenStorageError("無法保存加密 Token。")

    monkeypatch.setattr(pilot.token_cipher, "encrypt", fail)
    response = client.post("/api/settings", json={"telegram_token": "999999:NEW_TEST_TOKEN", "keywords": ["新設定"]})
    assert response.status_code == 400
    assert pilot.settings == pilot.db.load_settings() == previous_settings
    assert pilot.token == TOKEN and pilot.db.saved_token() == previous_blob
    assert TOKEN not in response.text and "NEW_TEST_TOKEN" not in response.text


def test_unreadable_saved_token_can_be_replaced_and_does_not_prevent_startup(tmp_path):
    browser, telegram = FakeBrowser(), Transport()
    pilot = Pilot(tmp_path, browser, telegram.client)
    pilot.db.save_settings(pilot.settings, encrypted_token=b"damaged")
    restarted = Pilot(tmp_path, browser, telegram.client)
    assert not restarted.token and restarted.token_storage_error
    assert restarted.state()["settings"]["telegram_token_saved"] is True
    restarted.save_settings(PilotSettings(keywords=["保留錯誤提醒"]))
    assert restarted.token_storage_error
    restarted.save_settings(PilotSettings(telegram_token=TOKEN))
    assert restarted.token == TOKEN and not restarted.token_storage_error
    assert Pilot(tmp_path, browser, telegram.client).token == TOKEN
    assert browser.calls == telegram.calls == []


def test_setting_transaction_failure_leaves_previous_token_and_preferences_unchanged(session, monkeypatch):
    client, pilot, browser, telegram, clock, path = session
    ready(session, keywords=["原設定"])
    previous_settings, previous_blob = dict(pilot.settings), pilot.db.saved_token()

    def fail(*args):
        raise RuntimeError("simulate a failed reclassification")

    monkeypatch.setattr(pilot.db, "_reclassify", fail)
    with pytest.raises(RuntimeError):
        client.post("/api/settings", json={"group_url": GROUP, "keywords": ["新設定"], "telegram_token": "999999:NEW_TEST_TOKEN"})
    assert pilot.settings == pilot.db.load_settings() == previous_settings
    assert pilot.token == TOKEN and pilot.db.saved_token() == previous_blob


def test_baseline_does_not_push_and_manual_checks_share_refresh_cooldown(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    assert telegram.calls == []
    assert browser.calls[-1] == ("collect", False)
    assert client.get("/api/state").json()["posts"][0]["notification_status"] == "baseline"
    assert client.post("/api/check").status_code == 429
    assert len(browser.calls) == 2
    pilot.stop_auto()
    assert client.post("/api/check").status_code == 429
    client.post("/api/settings", json={"group_url": GROUP, "telegram_chat_id": CHAT, "interval_seconds": 60})
    assert client.post("/api/check").status_code == 429
    clock.advance()
    browser.posts.append(source("2"))
    assert client.post("/api/check").status_code == 202
    until(lambda: not pilot.pending)
    assert browser.calls[-1] == ("collect", True)
    assert len(telegram.calls) == 1
    assert GROUP + "/posts/2/" in telegram.calls[0]["text"]
    assert client.get("/api/state").json()["posts"][0]["notification_status"] == "sent"


def test_known_posts_do_not_repeat_after_relative_time_or_body_edit(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    browser.posts[0].update(posted_at="5 分鐘前", content="週末臨打，內容更新")
    clock.advance()
    client.post("/api/check")
    until(lambda: not pilot.pending)
    assert telegram.calls == []
    assert len(pilot.db.posts(GROUP)) == 1
    assert "更新" in pilot.db.posts(GROUP)[0]["content"]


def test_unlinked_posts_are_not_automatic_notifications_or_an_empty_baseline(session):
    client, pilot, browser, telegram, clock, path = session
    browser.posts = [{"post_id": "", "url": "", "content": "臨打"}]
    client.post("/api/settings", json={"group_url": GROUP})
    client.post("/api/browser/open")
    until(lambda: not pilot.pending)
    client.post("/api/browser/confirm")
    until(lambda: not pilot.pending)
    client.post("/api/check")
    until(lambda: not pilot.pending)
    assert pilot.latched and not pilot.baseline
    assert pilot.db.posts(GROUP) == []
    assert telegram.calls == []


def test_restriction_latches_and_queued_start_or_manual_buttons_cannot_resume(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    clock.advance()
    browser.error = NeedsUserAction("需要手動驗證")
    client.post("/api/check")
    until(lambda: not pilot.pending)
    assert pilot.latched and not pilot.auto
    assert client.post("/api/check").status_code == 409
    assert client.post("/api/auto/start").status_code == 409
    assert client.post("/api/auto/stop").status_code == 200
    assert client.post("/api/check").status_code == 409
    browser.error = None
    assert client.post("/api/browser/confirm").status_code == 202
    until(lambda: not pilot.pending)
    assert not pilot.latched
    assert client.post("/api/check").status_code == 429
    clock.advance()
    assert client.post("/api/check").status_code == 202
    until(lambda: not pilot.pending)


def test_finite_automatic_checks_stop_at_count(session):
    ready(session, tracking_mode="limited", max_checks=1)
    client, pilot, browser, telegram, clock, path = session
    clock.advance()
    assert client.post("/api/auto/start").status_code == 200
    until(lambda: not pilot.auto and not pilot.pending)
    assert browser.calls.count(("collect", True)) == 1
    clock.advance(600)
    time.sleep(0.3)
    assert browser.calls.count(("collect", True)) == 1


def test_continuous_mode_keeps_running_across_multiple_days_until_stopped(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    clock.advance()
    assert client.post("/api/auto/start").status_code == 200
    until(lambda: browser.calls.count(("collect", True)) == 1 and not pilot.pending)
    for number in range(2, 6):
        clock.advance(3 * 24 * 3600)
        until(lambda: browser.calls.count(("collect", True)) == number and not pilot.pending)
        assert pilot.auto and not pilot.latched
        assert pilot.state()["status"]["remaining_checks"] is None
    client.post("/api/auto/stop")
    clock.advance(7 * 24 * 3600)
    time.sleep(0.3)
    assert browser.calls.count(("collect", True)) == 5 and not pilot.auto


def test_continuous_mode_retries_a_transient_page_timeout_with_backoff(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    browser.error = PilotTransientError("社團載入逾時。")
    clock.advance()
    client.post("/api/auto/start")
    until(lambda: pilot.phase == "retrying" and not pilot.pending)
    assert pilot.auto and not pilot.latched
    retry_at = pilot.next_check
    pilot._schedule()
    assert not pilot.pending and pilot.next_check == retry_at
    browser.error = None
    clock.advance(120)
    until(lambda: pilot.phase == "ready" and not pilot.pending)
    assert pilot.auto and pilot.consecutive_failures == 0


def test_rate_limited_digest_waits_then_sends_pending_posts_once(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    browser.posts.extend([source("2"), source("3")])
    telegram.error = TelegramRateLimited(300)
    clock.advance()
    client.post("/api/auto/start")
    until(lambda: pilot.phase == "retrying" and not pilot.pending)
    assert pilot.auto and not pilot.latched and not telegram.calls
    assert len(pilot.db.pending_notifications(GROUP)) == 2
    assert pilot.next_check >= clock() + 300
    telegram.error = None
    clock.advance(301)
    until(lambda: len(telegram.calls) == 1 and not pilot.pending)
    assert len(pilot.db.pending_notifications(GROUP)) == 0
    clock.advance()
    until(lambda: not pilot.pending and browser.calls.count(("collect", True)) >= 3)
    assert len(telegram.calls) == 1


def test_partial_digest_failure_preserves_unsent_posts_without_replaying_sent_or_unknown_batches(session):
    ready(session, max_posts_per_check=50)
    client, pilot, browser, telegram, clock, path = session
    browser.posts.extend(source(str(index), "🏐" * 500) for index in range(2, 31))
    attempts = []

    def interrupt_second_message():
        attempts.append(True)
        clock.advance(2)
        if len(attempts) == 2:
            telegram.error = TelegramError("連線逾時")

    telegram.before_send = interrupt_second_message
    clock.advance()
    client.post("/api/auto/start")
    until(lambda: pilot.phase == "retrying" and not pilot.pending)
    statuses = {post["post_id"]: post["notification_status"] for post in pilot.db.posts(GROUP)}
    assert {"sent", "unknown", "pending"}.issubset(set(statuses.values()))
    unknown_links = [GROUP + f"/posts/{identity}/" for identity, status in statuses.items() if status == "unknown"]
    first_text = telegram.calls[0]["text"]
    telegram.error = None
    telegram.before_send = lambda: clock.advance(2)
    clock.advance(120)
    until(lambda: not pilot.pending and not pilot.db.pending_notifications(GROUP))
    remaining_text = "\n".join(call["text"] for call in telegram.calls[1:])
    assert all(link not in remaining_text for link in unknown_links)
    sent_links = [GROUP + f"/posts/{identity}/" for identity, status in statuses.items() if status == "sent"]
    assert all(link in first_text and link not in remaining_text for link in sent_links)


def test_restart_restores_baseline_and_sends_new_posts_since_previous_run(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    pilot.close()
    new_browser = FakeBrowser()
    new_browser.posts = [source(), source("2")]
    app = create_pilot_app(path, pilot_factory=lambda directory: Pilot(directory, new_browser, telegram.client, clock))
    with TestClient(app, headers=HEADERS) as restarted_client:
        restarted = app.state.pilot
        restarted_client.post("/api/browser/open")
        until(lambda: not restarted.pending)
        restarted_client.post("/api/browser/confirm")
        until(lambda: not restarted.pending)
        assert restarted.baseline
        assert not telegram.calls and not restarted.auto
        clock.advance()
        restarted_client.post("/api/check")
        until(lambda: not restarted.pending)
        assert len(telegram.calls) == 1 and GROUP + "/posts/2/" in telegram.calls[0]["text"]


def test_changed_recipient_holds_queued_notifications(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    queued, _ = pilot.db.observe(GROUP, source("2"), pilot.settings, False)
    response = client.post("/api/settings", json={"group_url": GROUP, "telegram_chat_id": "87654321"})
    assert response.status_code == 200
    assert pilot.db.post(queued["id"])["notification_status"] == "held"
    assert not pilot.db.pending_notifications(GROUP)


def test_old_database_adds_publication_time_without_losing_detection_or_delivery_state(tmp_path):
    path = tmp_path / "pilot.sqlite3"
    database = PilotDatabase(path)
    settings = PilotSettings(group_url=GROUP).model_dump()
    post, _ = database.observe(GROUP, source(), settings, True)
    with database.connect() as db:
        db.execute("ALTER TABLE posts DROP COLUMN published_at")
    migrated = PilotDatabase(path)
    restored = migrated.post(post["id"])
    assert restored["first_seen"] == post["first_seen"]
    assert restored["published_at"] == "" and restored["notification_status"] == "baseline"
    migrated.observe(GROUP, {**source(), "published_at": 1700000000}, settings, False)
    assert migrated.post(post["id"])["published_at"] == "2023-11-14T22:13:20+00:00"
    assert migrated.post(post["id"])["notification_status"] == "baseline"


def test_conflicting_publication_time_clears_previously_saved_precision(session):
    client, pilot, browser, telegram, clock, path = session
    settings = PilotSettings(group_url=GROUP).model_dump()
    post, _ = pilot.db.observe(GROUP, {**source(), "published_at": 1700000000}, settings, True)
    assert pilot.db.post(post["id"])["published_at"]
    pilot.db.observe(GROUP, {**source(), "timestamp_conflict": True}, settings, False)
    assert pilot.db.post(post["id"])["published_at"] == ""


def test_pending_post_that_no_longer_matches_is_not_sent(session):
    client, pilot, browser, telegram, clock, path = session
    settings = PilotSettings(group_url=GROUP, keywords=["臨打"]).model_dump()
    post, _ = pilot.db.observe(GROUP, source(), settings, False)
    pilot.db.observe(GROUP, source(content="活動取消"), settings, False)
    assert pilot.db.post(post["id"])["notification_status"] == "not_matched"
    assert not pilot.db.pending_notifications(GROUP)


def test_expired_queued_automatic_check_never_starts(tmp_path):
    clock, browser, telegram = Clock(), FakeBrowser(), Transport()
    pilot = Pilot(tmp_path, browser, telegram.client, clock)
    pilot.group, pilot.baseline, browser.driver = GROUP, True, object()
    pilot.auto, pilot.remaining, pilot.deadline = True, 1, clock() + 1
    pilot.pending.add("check")
    pilot.queue.put(("check", pilot.generation, True))
    clock.advance(2)
    pilot.start()
    try:
        until(lambda: not pilot.pending)
        assert not pilot.auto
        assert browser.calls == []
    finally:
        pilot.close()


def test_stop_prevents_a_queued_check_and_auto_send_dispatch(tmp_path):
    clock, browser, telegram = Clock(), FakeBrowser(), Transport()
    pilot = Pilot(tmp_path, browser, telegram.client, clock)
    pilot.auto, pilot.remaining, pilot.deadline = True, 1, clock() + 120
    pilot.pending.add("check")
    pilot.queue.put(("check", pilot.generation, True))
    pilot.stop_auto()
    pilot.start()
    try:
        until(lambda: not pilot.pending)
        assert browser.calls == []
        with pytest.raises(Exception) as error:
            pilot._telegram_call(lambda: telegram.calls.append("sent"), cancellable=True)
        assert error.type.__name__ == "PilotCancelled"
        assert telegram.calls == []
    finally:
        pilot.close()


def test_network_failure_marks_delivery_unknown_pauses_and_never_retries(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    browser.posts.append(source("2"))
    telegram.error = TelegramError("無法連線")
    clock.advance()
    client.post("/api/check")
    until(lambda: not pilot.pending)
    assert pilot.latched
    assert next(p for p in pilot.db.posts(GROUP) if p["post_id"] == "2")["notification_status"] == "unknown"
    telegram.error = None
    client.post("/api/browser/confirm")
    until(lambda: not pilot.pending)
    clock.advance()
    client.post("/api/check")
    until(lambda: not pilot.pending)
    assert telegram.calls == []


def test_new_matching_posts_are_combined_and_all_marked_sent(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    browser.posts.extend([source("2"), source("3")])
    clock.advance()
    client.post("/api/check")
    until(lambda: not pilot.pending)
    assert len(telegram.calls) == 1
    assert sorted(p["notification_status"] for p in pilot.db.posts(GROUP)) == ["baseline", "sent", "sent"]
    assert all(GROUP + f"/posts/{number}/" in telegram.calls[0]["text"] for number in ("2", "3"))
    assert "首次偵測" in telegram.calls[0]["text"] and "通知時間" in telegram.calls[0]["text"]
    assert telegram.calls[0]["parse_mode"] == "HTML"
    assert "<blockquote>" in telegram.calls[0]["text"] and "開啟原文 ↗</a>" in telegram.calls[0]["text"]


def test_delivery_state_survives_restart_without_automatic_replays(tmp_path):
    db = PilotDatabase(tmp_path / "pilot.sqlite3")
    settings = PilotSettings(group_url=GROUP).model_dump()
    for i, status in [("1", "sent"), ("2", "sending"), ("3", "pending")]:
        post, _ = db.observe(GROUP, source(i), settings, False)
        db.delivery(post["id"], status)
    restarted = PilotDatabase(tmp_path / "pilot.sqlite3")
    statuses = {post["post_id"]: post["notification_status"] for post in restarted.posts(GROUP)}
    assert statuses == {"1": "sent", "2": "unknown", "3": "pending"}
    browser, telegram = FakeBrowser(), Transport()
    pilot = Pilot(tmp_path, browser, telegram.client)
    assert not pilot.token and not pilot.auto and not pilot.baseline
    assert telegram.calls == browser.calls == []


def test_explicit_test_sends_selected_baseline_only_after_user_action(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    post = pilot.db.posts(GROUP)[0]
    assert telegram.calls == []
    response = client.post(f"/api/posts/{post['id']}/test-notification")
    assert response.status_code == 200
    assert len(telegram.calls) == 1
    assert "手動測試" in telegram.calls[0]["text"]
    assert pilot.db.post(post["id"])["notification_status"] == "baseline"
    assert len(browser.calls) == 2


def test_settings_cannot_change_recipient_during_telegram_send(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    results = []
    def while_sending():
        results.append(client.post("/api/settings", json={"group_url": GROUP, "telegram_chat_id": "87654321"}).status_code)
    telegram.before_send = while_sending
    assert client.post("/api/telegram/test").status_code == 200
    assert results == [409]
    assert telegram.calls[-1]["chat"] == CHAT


def test_validation_and_error_responses_never_echo_tokens(session):
    client, pilot, browser, telegram, clock, path = session
    response = client.post("/api/settings", json={"telegram_token": TOKEN, "interval_seconds": 1})
    assert response.status_code == 422 and TOKEN not in response.text
    response = client.post("/api/settings", json={"telegram_token": TOKEN + "/invalid"})
    assert response.status_code == 400 and TOKEN not in response.text
    assert client.post("/api/settings", json={"telegram_token": TOKEN}, headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.get("/api/state", headers={"Host": "evil.test"}).status_code == 400
    assert TOKEN not in json.dumps(pilot.db.events())


@pytest.mark.parametrize("url", [
    "http://www.facebook.com/groups/123456", "https://www.facebook.com.evil.test/groups/123456",
    "https://user@www.facebook.com/groups/123456", "https://www.facebook.com:444/groups/123456",
    "https://www.facebook.com/groups/123456/posts/99/", "https://www.facebook.com/share/p/123456/",
])
def test_share_and_group_validation_rejects_unrelated_urls(url):
    with pytest.raises(ValueError):
        pilot_group_url(url)


def test_share_url_tracking_parameters_are_removed():
    assert pilot_group_url("https://www.facebook.com/share/g/exampleGroup123/?mibextid=example") == "https://www.facebook.com/share/g/exampleGroup123/"


def test_permalink_cleaning_preserves_post_identity_and_removes_tracking_parameters():
    assert canonical_post_url("https://www.facebook.com/groups/123/posts/456/?__cft__=example#comment") == (
        "https://www.facebook.com/groups/123/posts/456/", "456")
    assert canonical_post_url("https://example.com/groups/123/posts/456") == ("", "")
    assert canonical_post_url(GROUP) == ("", "")
    assert post_belongs_to_group(GROUP + "/posts/99/?tracking=x", GROUP)
    assert not post_belongs_to_group("https://www.facebook.com/groups/999999/posts/99/", GROUP)
    assert not post_belongs_to_group("https://www.facebook.com/profile/posts/99/", GROUP)


@pytest.mark.parametrize("snapshot", [
    {"url": "https://www.facebook.com/checkpoint/", "text": ""},
    {"url": GROUP, "login": True, "text": ""},
    {"url": GROUP, "challenge": True, "text": ""},
    {"url": GROUP, "text": "You're temporarily blocked"},
    {"url": GROUP, "text": "你目前無法使用此功能"},
    {"url": "https://evil.test/groups/123456", "text": ""},
])
def test_login_challenge_and_restriction_detection(snapshot):
    with pytest.raises(NeedsUserAction):
        check_snapshot(snapshot)


class Driver:
    def __init__(self, url=GROUP):
        self.url = url
        self.refreshes = 0
        self.extractions = 0
        self.posts = [source()]

    def execute_script(self, script, *args):
        from app.pilot_browser import SNAPSHOT, PILOT_EXTRACT_POSTS, SCROLL_FEED
        if script == SNAPSHOT:
            return {"url": self.url, "ready": "complete", "text": "社團貼文"}
        if script == PILOT_EXTRACT_POSTS:
            self.extractions += 1
            return self.posts
        if script == SCROLL_FEED:
            return {"moved": False}
        return True

    def refresh(self):
        self.refreshes += 1

    def quit(self):
        pass


def test_manual_page_change_and_dead_driver_never_trigger_navigation_or_reopen(tmp_path):
    factories = []
    browser = PilotBrowser(tmp_path, factory=lambda preference: factories.append(preference))
    driver = Driver("https://www.facebook.com/groups/999999")
    browser.driver = driver
    with pytest.raises(NeedsUserAction):
        browser.collect(GROUP, threading.Event(), refresh=True)
    assert driver.refreshes == driver.extractions == 0
    assert factories == []
    driver.execute_script = lambda *args: (_ for _ in ()).throw(RuntimeError("session lost"))
    with pytest.raises(PilotBrowserError):
        browser.collect(GROUP, threading.Event(), refresh=True)
    assert factories == []


def test_browser_reads_only_loaded_posts_and_strips_wrong_group_links(tmp_path):
    driver = Driver()
    driver.posts = [{"author": "同學", "content": "臨打", "url": GROUP + "/posts/99/"},
                    {"author": "同學", "content": "其他連結", "url": "https://www.facebook.com/groups/999999/posts/88/"}]
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    result = browser.collect(GROUP, threading.Event(), refresh=False)
    assert driver.refreshes == 0 and driver.extractions >= 1
    assert result[0]["post_id"] == "99"
    assert result[1]["post_id"] == result[1]["url"] == ""


def test_browser_waits_for_readable_posts_without_an_additional_refresh(tmp_path):
    driver = Driver()
    original_execute = driver.execute_script

    def while_page_loads(script, *args):
        from app.pilot_browser import PILOT_EXTRACT_POSTS
        if script == PILOT_EXTRACT_POSTS and driver.extractions == 0:
            driver.extractions += 1
            return []
        return original_execute(script, *args)

    driver.execute_script = while_page_loads
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    result = browser.collect(GROUP, threading.Event(), refresh=False)
    assert result[0]["post_id"] == "1"
    assert driver.extractions >= 2 and driver.refreshes == 0


def test_conflicting_sources_for_one_post_are_excluded_instead_of_overwritten(tmp_path):
    driver = Driver()
    driver.posts = [source("1", "第一個來源"), source("1", "不同內容"), source("1", "第一個來源"), source("2", "有效貼文")]
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    assert [p["post_id"] for p in browser.collect(GROUP, threading.Event())] == ["2"]


def test_same_verified_post_id_can_use_its_loaded_full_record_without_mixing_fields(tmp_path):
    prefix = "相同貼文已透過兩個來源各自驗證了網址與識別碼，這段內容在畫面中被截斷"
    driver = Driver()
    driver.posts = [dict(source("1", prefix + "… 查看更多"), posted_at="3分鐘前"), source("1", prefix + "，這是完整內容。")]
    driver.posts[1]["posted_at"] = ""
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    result = browser.collect(GROUP, threading.Event())
    assert len(result) == 1 and result[0]["content"].endswith("這是完整內容。")
    assert result[0]["posted_at"] == "3分鐘前"


class ImmediateCancel(threading.Event):
    def wait(self, timeout=None):
        return self.is_set()


class FeedDriver(Driver):
    def __init__(self, batches):
        super().__init__()
        self.batches = batches
        self.batch = 0
        self.scrolls = 0

    def execute_script(self, script, *args):
        from app.pilot_browser import PILOT_EXTRACT_POSTS, SCROLL_FEED
        if script == PILOT_EXTRACT_POSTS:
            self.extractions += 1
            return self.batches[self.batch]
        if script == SCROLL_FEED:
            if args and args[0] == "top":
                self.batch = 0
                return {"moved": False}
            self.scrolls += 1
            if self.batch + 1 < len(self.batches):
                self.batch += 1
                return {"moved": True}
            return {"moved": False}
        return super().execute_script(script, *args)


def test_multi_post_read_accumulates_virtualized_batches_and_respects_the_target(tmp_path):
    driver = FeedDriver([[source("1"), {"content": "placeholder", "url": ""}],
                         [source("2")], [source("3"), source("4")]])
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    result = browser.collect(GROUP, ImmediateCancel(), max_posts=3)
    assert [post["post_id"] for post in result if post["url"]] == ["1", "2", "3"]
    assert driver.refreshes == 0 and driver.scrolls == 2
    assert browser.scan_summary["loaded_posts"] == 3
    assert browser.scan_summary["stop_reason"] == "post_limit"


def test_reaching_target_in_the_first_batch_needs_no_feed_scroll(tmp_path):
    driver = FeedDriver([[source("1"), source("2")], [source("3")]])
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    assert len(browser.collect(GROUP, ImmediateCancel(), max_posts=2)) == 2
    assert driver.scrolls == 0


def test_a_temporarily_unscrollable_feed_waits_for_its_pending_response(tmp_path):
    from collections import deque
    from app.pilot_browser import PILOT_EXTRACT_POSTS

    class PendingTraffic:
        def __init__(self):
            self.payloads = deque([[], ["later feed response"]])

        def read(self):
            return self.payloads.popleft() if self.payloads else []

    driver = Driver()
    original_execute = driver.execute_script

    def read_response(script, *args):
        if script == PILOT_EXTRACT_POSTS and len(args) > 1 and args[1]:
            return [source("2")]
        return original_execute(script, *args)

    driver.execute_script = read_response
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    browser.traffic = PendingTraffic()
    result = browser.collect(GROUP, ImmediateCancel(), max_posts=2)
    assert [post["post_id"] for post in result] == ["1", "2"]
    assert browser.scan_summary["scrolls"] == 0


def test_cancel_during_feed_loading_stops_before_the_next_read(tmp_path):
    from app.pilot_browser import PilotCancelled, SCROLL_FEED
    driver = FeedDriver([[source("1")], [source("2")]])
    cancel = ImmediateCancel()
    original_execute = driver.execute_script

    def cancel_on_scroll(script, *args):
        result = original_execute(script, *args)
        if script == SCROLL_FEED and not args:
            cancel.set()
        return result

    driver.execute_script = cancel_on_scroll
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    with pytest.raises(PilotCancelled):
        browser.collect(GROUP, cancel)
    assert driver.extractions == 1


def test_settings_created_before_multi_post_loading_receive_the_new_default(tmp_path):
    database = PilotDatabase(tmp_path / "pilot.sqlite3")
    database.save_settings({"group_url": GROUP})
    assert database.load_settings()["max_posts_per_check"] == 20


def test_no_growth_ends_after_two_bounded_loads(tmp_path, monkeypatch):
    from selenium.common.exceptions import TimeoutException
    import selenium.webdriver.support.ui

    class ImmediateWait:
        def __init__(self, driver, timeout, poll_frequency):
            self.driver = driver

        def until(self, condition):
            if not condition(self.driver):
                raise TimeoutException()

    monkeypatch.setattr(selenium.webdriver.support.ui, "WebDriverWait", ImmediateWait)
    class EndOfFeed(FeedDriver):
        def execute_script(self, script, *args):
            from app.pilot_browser import SCROLL_FEED
            if script == SCROLL_FEED and not args:
                self.scrolls += 1
                return {"moved": False}
            return super().execute_script(script, *args)

    driver = EndOfFeed([[source("1")]])
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    assert len(browser.collect(GROUP, ImmediateCancel())) == 1
    assert driver.scrolls == 2
    assert browser.scan_summary["stop_reason"] == "no_more_links"


def test_tall_received_batch_does_not_stop_before_the_next_feed_page(tmp_path):
    driver = FeedDriver([[source("1")], [source("1")], [source("1")], [source("2")]])
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    result = browser.collect(GROUP, ImmediateCancel(), max_posts=2)
    assert [post["post_id"] for post in result] == ["1", "2"]
    assert driver.scrolls == 3


def test_changing_group_during_feed_loading_rejects_the_entire_read(tmp_path):
    from app.pilot_browser import SCROLL_FEED
    driver = FeedDriver([[source("1")], [source("2")]])
    original_execute = driver.execute_script

    def change_group_on_scroll(script, *args):
        result = original_execute(script, *args)
        if script == SCROLL_FEED and not args:
            driver.url = "https://www.facebook.com/groups/999999"
        return result

    driver.execute_script = change_group_on_scroll
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    with pytest.raises(NeedsUserAction):
        browser.collect(GROUP, ImmediateCancel())


def test_increasing_read_scope_requires_a_new_baseline(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    settings = {**pilot.settings, "max_posts_per_check": 30}
    assert client.post("/api/settings", json=settings).status_code == 200
    assert not pilot.baseline


def test_rule_changes_recompute_saved_results_without_another_facebook_read(session):
    ready(session, keywords=["不會出現的詞"])
    client, pilot, browser, telegram, clock, path = session
    before_calls = list(browser.calls)
    settings = {**pilot.settings, "keywords": ["臨打"]}
    assert client.post("/api/settings", json=settings).status_code == 200
    post = client.get("/api/state").json()["posts"][0]
    assert post["matched"] and post["hits"] == ["臨打"]
    assert post["notification_status"] == "baseline"
    assert browser.calls == before_calls and telegram.calls == []
    assert not pilot.baseline


@pytest.mark.parametrize("author,excluded,expected", [
    ("王小明", ["王小明"], False),
    ("王小明二", ["王小明"], True),
    ("另一位作者", ["王小明"], True),
    ("  JOHN   DOE ", ["John Doe"], False),
    ("王小明、另一位作者", ["另一位作者"], False),
    ("Smith, John", ["Smith, John"], False),
    ("", [""], True),
])
def test_author_exclusions_use_full_names_without_treating_body_mentions_as_authors(author, excluded, expected):
    settings = {"keywords": ["臨打"], "excludes": [], "match_mode": "any", "excluded_authors": excluded}
    matched, hits = match_keywords("臨打，內文提到了王小明", settings, author)
    assert matched is expected
    assert hits == (["臨打"] if expected else [])


def test_legacy_settings_keep_the_baseline_when_no_authors_are_excluded(tmp_path):
    database = PilotDatabase(tmp_path / "pilot.sqlite3")
    settings = PilotSettings(group_url=GROUP).model_dump(exclude={"telegram_token", "excluded_authors"})
    old_scope = json.dumps({key: settings[key] for key in ("keywords", "excludes", "match_mode", "max_posts_per_check")}, sort_keys=True)
    database.save_settings(settings)
    with database.connect() as db:
        db.execute("INSERT INTO baselines VALUES(?,?,?)", (GROUP, old_scope, "2026-11-01T00:00:00Z"))
    restored = database.load_settings()
    assert restored["excluded_authors"] == []
    assert database.has_baseline(GROUP, restored)


def test_author_list_normalizes_whitespace_and_case_duplicates():
    settings = PilotSettings(excluded_authors=[" 王小明 ", "王小明", " JOHN  DOE ", "john doe", "", "Smith, John"])
    assert settings.excluded_authors == ["王小明", "JOHN DOE", "Smith, John"]
    with pytest.raises(ValueError):
        PilotSettings(excluded_authors=["x" * 121])


def test_excluded_authors_are_filtered_from_normal_merged_notifications(session):
    ready(session, keywords=["臨打"], excluded_authors=["排除同學"])
    client, pilot, browser, telegram, clock, path = session
    browser.posts.extend([
        {**source("2"), "author": "排除同學"},
        {**source("3", "臨打，內文提及排除同學"), "author": "其他同學"},
        {**source("4"), "author": "排除同學二"},
    ])
    clock.advance()
    assert client.post("/api/check").status_code == 202
    until(lambda: not pilot.pending)
    assert len(telegram.calls) == 1
    assert GROUP + "/posts/2/" not in telegram.calls[0]["text"]
    assert all(GROUP + f"/posts/{identity}/" in telegram.calls[0]["text"] for identity in ("3", "4"))
    blocked = next(post for post in client.get("/api/state").json()["posts"] if post["post_id"] == "2")
    assert not blocked["matched"] and blocked["author_excluded"]
    assert blocked["notification_status"] == "not_matched"


def test_author_rule_changes_reclassify_existing_and_queued_posts_without_sending_old_posts(session):
    ready(session)
    client, pilot, browser, telegram, clock, path = session
    before = list(browser.calls)
    queued, _ = pilot.db.observe(GROUP, source("2"), pilot.settings, False)
    response = client.post("/api/settings", json={**pilot.settings, "excluded_authors": ["測試同學"]})
    assert response.status_code == 200
    assert all(not post["matched"] and post["author_excluded"] for post in response.json()["posts"])
    assert pilot.db.post(queued["id"])["notification_status"] == "held"
    assert not pilot.db.pending_notifications(GROUP) and not pilot.baseline
    assert browser.calls == before and telegram.calls == []
    response = client.post("/api/settings", json={**pilot.settings, "excluded_authors": []})
    assert response.status_code == 200
    assert all(post["matched"] and not post["author_excluded"] for post in response.json()["posts"])
    assert telegram.calls == []
    assert Pilot(path, FakeBrowser(), telegram.client, clock).settings["excluded_authors"] == []


def test_author_metadata_can_be_completed_later_and_is_retained_when_next_read_omits_it(tmp_path):
    database = PilotDatabase(tmp_path / "pilot.sqlite3")
    settings = PilotSettings(group_url=GROUP, excluded_authors=["測試同學"]).model_dump()
    post, _ = database.observe(GROUP, {**source(), "author": ""}, settings, True)
    assert post["author"] == ""
    updated, _ = database.observe(GROUP, source(), settings, False)
    assert updated["author"] == "測試同學" and not updated["matched"]
    retained, _ = database.observe(GROUP, {**source(), "author": ""}, settings, False)
    assert retained["author"] == "測試同學" and not retained["matched"]
    database.reclassify(GROUP, settings)
    assert not database.post(post["id"])["matched"]


def test_document_cache_supports_rereading_rules_and_resets_with_the_document(tmp_path):
    class Traffic:
        document_key = "document-1"

        def read(self):
            return []

    driver = Driver()
    driver.posts = [source("1"), source("2")]
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    browser.traffic = Traffic()
    assert len(browser.collect(GROUP, ImmediateCancel(), max_posts=2)) == 2
    driver.posts = [source("1")]
    assert len(browser.collect(GROUP, ImmediateCancel(), max_posts=2)) == 2
    browser.traffic.document_key = "document-2"
    assert len(browser.collect(GROUP, ImmediateCancel(), max_posts=2)) == 1


def test_background_loading_uses_browser_focus_emulation(tmp_path):
    driver = Driver()
    commands = []

    def execute_cdp(method, params):
        commands.append((method, params))
        return {}

    driver.execute_cdp_cmd = execute_cdp
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    browser.collect(GROUP, ImmediateCancel(), max_posts=1)
    assert commands == [("Emulation.setFocusEmulationEnabled", {"enabled": True})]
    assert browser.background_loading


def test_background_page_is_not_reported_as_a_success_when_activation_is_unsupported(tmp_path):
    from app.pilot_browser import SNAPSHOT
    driver = Driver()
    original_execute = driver.execute_script

    def hidden_page(script, *args):
        result = original_execute(script, *args)
        if script == SNAPSHOT:
            result["visibility"] = "hidden"
        return result

    driver.execute_script = hidden_page
    browser = PilotBrowser(tmp_path)
    browser.driver = driver
    with pytest.raises(NeedsUserAction, match="背景"):
        browser.collect(GROUP, ImmediateCancel())
    assert driver.extractions == 0
