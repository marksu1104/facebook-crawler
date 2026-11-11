import io
import json
import traceback
import urllib.error

import pytest

from app.telegram import TelegramClient, TelegramError, TelegramRateLimited


TOKEN = "123456:fake-token_for_tests"
POST = "https://www.facebook.com/groups/123456/posts/987654/"


class Response(io.BytesIO):
    status = 200
    headers = {}


class FakeTelegram:
    def __init__(self, result=None, *, error=None, ok=True, error_code=None, parameters=None):
        self.result, self.error = result, error
        self.ok, self.error_code, self.parameters = ok, error_code, parameters
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append({"url": request.full_url, "method": request.method,
                              "payload": json.loads(request.data), "timeout": timeout})
        if self.error is not None:
            raise self.error
        return Response(json.dumps({"ok": self.ok, "result": self.result,
                                    "error_code": self.error_code, "parameters": self.parameters}).encode())


def test_send_plain_text_with_original_link_button():
    transport = FakeTelegram({"message_id": 12, "date": 123})
    client = TelegramClient(TOKEN, 456, opener=transport)
    assert client.send_text("臨打 <b>今晚缺二</b>", POST) == {"message_id": 12, "chat_id": "456", "date": 123}
    request = transport.requests[0]
    assert request["url"].endswith("/sendMessage")
    assert request["payload"]["chat_id"] == "456"
    assert request["payload"]["text"] == "臨打 <b>今晚缺二</b>"
    assert "parse_mode" not in request["payload"]
    assert "disable_notification" not in request["payload"]
    assert request["payload"]["reply_markup"]["inline_keyboard"] == [[{"text": "查看原始貼文", "url": POST}]]
    assert request["timeout"] == 15
    assert TOKEN not in repr(client)


def test_formatted_digest_uses_html_without_truncating_escaped_content_or_links():
    transport = FakeTelegram({"message_id": 3})
    text = '<b>新貼文</b>\n<blockquote>第一行 &amp; 內容\n第二行</blockquote>\n<a href="' + POST + '">開啟原文 ↗</a>'
    TelegramClient(TOKEN, 456, opener=transport).send_text(text, parse_mode="HTML")
    payload = transport.requests[0]["payload"]
    assert payload["text"] == text and payload["parse_mode"] == "HTML"
    assert payload["link_preview_options"]["is_disabled"] is True
    assert "reply_markup" not in payload


def test_oversized_formatted_message_is_rejected_before_network_instead_of_cutting_a_tag():
    transport = FakeTelegram()
    with pytest.raises(TelegramError, match="先分段"):
        TelegramClient(TOKEN, 456, opener=transport).send_text("<b>" + "x" * 4096 + "</b>", parse_mode="HTML")
    assert not transport.requests


@pytest.mark.parametrize("chat_id", ["@channel", -100123, 0, "0", "-1", True, "123/456", ""])
def test_private_pilot_cannot_send_to_groups_or_channels(chat_id):
    with pytest.raises(TelegramError):
        TelegramClient(TOKEN, chat_id)


@pytest.mark.parametrize("token", ["", "abc:secret", "123:", "123:abc/def", "123:abc\nsecret", None])
def test_invalid_token_is_not_echoed(token):
    with pytest.raises(TelegramError) as caught:
        TelegramClient(token)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("link", ["http://www.facebook.com/x", "https://evil.test/x", "https://www.facebook.com.evil.test/x",
                                 "https://name@www.facebook.com/x", "https://www.facebook.com:8443/x", "javascript:alert(1)",
                                 "https://www.facebook.com/x\nInjected"])
def test_original_link_validation_happens_before_network(link):
    transport = FakeTelegram()
    with pytest.raises(TelegramError):
        TelegramClient(TOKEN, 456, opener=transport).send_text("新貼文", link)
    assert transport.requests == []


def test_long_unicode_message_is_bounded_without_splitting_emoji():
    transport = FakeTelegram({"message_id": 1})
    TelegramClient(TOKEN, 456, opener=transport).send_text("🏐" * 4096)
    message = transport.requests[0]["payload"]["text"]
    assert len(message.encode("utf-16-le")) <= 4096 * 2
    assert message.endswith("…")


def test_429_surfaces_retry_delay_without_retrying():
    transport = FakeTelegram(ok=False, error_code=429, parameters={"retry_after": 87})
    with pytest.raises(TelegramRateLimited) as caught:
        TelegramClient(TOKEN, 456, opener=transport).send_text("新貼文")
    assert caught.value.retry_after == 87
    assert caught.value.code == 429
    assert len(transport.requests) == 1


def test_http_error_body_can_supply_retry_after():
    error = urllib.error.HTTPError(f"https://api.telegram.org/bot{TOKEN}/sendMessage", 429, TOKEN, {},
                                  io.BytesIO(json.dumps({"parameters": {"retry_after": 123}}).encode()))
    transport = FakeTelegram(error=error)
    with pytest.raises(TelegramRateLimited) as caught:
        TelegramClient(TOKEN, 456, opener=transport).send_text("新貼文")
    assert caught.value.retry_after == 123
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("exception", [urllib.error.URLError(f"broken URL contains {TOKEN}"), TimeoutError(TOKEN), RuntimeError(TOKEN)])
def test_transport_error_and_traceback_never_reveal_token(exception):
    transport = FakeTelegram(error=exception)
    with pytest.raises(TelegramError) as caught:
        TelegramClient(TOKEN, 456, opener=transport).send_text("新貼文")
    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(caught.value)
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    assert len(transport.requests) == 1


def test_get_me_checks_identity_without_sending_a_message():
    transport = FakeTelegram({"id": 123, "is_bot": True, "username": "example_bot", "first_name": "提醒", "unrelated": TOKEN})
    identity = TelegramClient(TOKEN, opener=transport).test_connection()
    assert identity == {"id": 123, "username": "example_bot", "first_name": "提醒"}
    assert transport.requests[0]["url"].endswith("/getMe")


def test_discovery_uses_private_start_messages_only_and_does_not_advance_offset():
    updates = [
        {"message": {"text": "/start", "chat": {"id": -100, "type": "supergroup", "title": "群組"}}},
        {"message": {"text": "hello", "chat": {"id": 111, "type": "private", "first_name": "其他"}}},
        {"message": {"text": "/start", "chat": {"id": 456, "type": "private", "first_name": "測試", "username": "user"}}},
        {"message": {"text": "/start", "chat": {"id": 456, "type": "private", "first_name": "更新名稱", "username": "user"}}},
    ]
    transport = FakeTelegram(updates)
    chats = TelegramClient(TOKEN, opener=transport).list_private_chats()
    assert chats == [{"id": "456", "name": "更新名稱 (@user)"}]
    payload = transport.requests[0]["payload"]
    assert "offset" not in payload
    assert payload["timeout"] == 0
    assert payload["allowed_updates"] == ["message"]
    assert len(transport.requests) == 1


def test_missing_recipient_never_calls_network():
    transport = FakeTelegram()
    with pytest.raises(TelegramError):
        TelegramClient(TOKEN, opener=transport).send_text("新貼文")
    assert transport.requests == []


def test_upstream_error_description_is_not_exposed():
    def transport(request, timeout):
        return Response(json.dumps({"ok": False, "error_code": 401, "description": TOKEN}).encode())
    with pytest.raises(TelegramError) as caught:
        TelegramClient(TOKEN, opener=transport).get_me()
    assert caught.value.code == 401
    assert TOKEN not in str(caught.value)


def test_http_error_body_failure_never_exposes_request_url():
    class BrokenError(urllib.error.HTTPError):
        def read(self, *args):
            raise OSError(f"broken response for {TOKEN}")

    error = BrokenError(f"https://api.telegram.org/bot{TOKEN}/sendMessage", 502, TOKEN, {}, None)
    transport = FakeTelegram(error=error)
    with pytest.raises(TelegramError) as caught:
        TelegramClient(TOKEN, 456, opener=transport).send_text("新貼文")
    assert caught.value.code == 502
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


def test_unknown_send_outcome_is_explicit_not_retried():
    transport = FakeTelegram({"unexpected": "result"})
    with pytest.raises(TelegramError, match="未確認訊息是否送出"):
        TelegramClient(TOKEN, 456, opener=transport).send_text("新貼文")
    assert len(transport.requests) == 1
