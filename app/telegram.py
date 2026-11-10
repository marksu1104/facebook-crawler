"""Small Telegram adapter for the local pilot; no network work on import.

The caller owns credentials, recipient selection, consent and scheduling. This
module never retries, logs requests, changes webhooks, or sends to group chats.
"""
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse


class TelegramError(Exception):
    """An error safe to show in the local UI without exposing the bot token."""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


class TelegramRateLimited(TelegramError):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Telegram 要求暫停發送，請至少等待 {retry_after} 秒後再試。", code=429)


def _private_chat_id(value) -> str:
    if isinstance(value, bool):
        raise TelegramError("請選擇接收通知的個人聊天室。")
    value = str(value).strip()
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise TelegramError("此驗證版只接受個人聊天室 ID，不接受群組或頻道。")
    return value


def _post_link(value: str) -> str:
    try:
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise ValueError
        parts = urlparse(value)
        if (parts.scheme != "https" or parts.hostname not in
                {"facebook.com", "www.facebook.com", "m.facebook.com"}
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443)):
            raise ValueError
    except (TypeError, ValueError):
        raise TelegramError("原始貼文連結必須是有效的 Facebook HTTPS 網址。") from None
    return value


def _message_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TelegramError("通知內容不能留空。")
    value = value.strip()
    # Telegram entities use UTF-16 offsets. This conservative limit also avoids
    # splitting an emoji when shortening a long, untrusted post body.
    encoded = value.encode("utf-16-le", errors="replace")
    if len(encoded) <= 4096 * 2:
        return value
    return encoded[:4095 * 2].decode("utf-16-le", errors="ignore") + "…"


class TelegramClient:
    def __init__(self, token: str, chat_id=None, *, opener=None, timeout: float = 15):
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token.strip()):
            raise TelegramError("Telegram Bot Token 格式不正確，請在本機設定頁重新輸入。")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 60:
            raise TelegramError("Telegram 連線逾時設定必須介於 0 與 60 秒之間。")
        self._token = token.strip()
        self.chat_id = _private_chat_id(chat_id) if chat_id is not None else None
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout

    def __repr__(self):
        return "TelegramClient(configured=True)"

    @staticmethod
    def _decode(raw):
        if len(raw) > 65536:
            raise TelegramError("Telegram 回應過大，請稍後再試。")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, AttributeError):
            raise TelegramError("Telegram 回應格式異常，請稍後再試。") from None
        if not isinstance(data, dict):
            raise TelegramError("Telegram 回應格式異常，請稍後再試。")
        return data

    @staticmethod
    def _api_error(code, data, headers=None):
        if code == 429:
            params = data.get("parameters")
            delay = params.get("retry_after") if isinstance(params, dict) else None
            if type(delay) is not int or delay <= 0:
                try:
                    delay = int(headers.get("Retry-After", "60")) if headers else 60
                except (TypeError, ValueError):
                    delay = 60
            raise TelegramRateLimited(max(1, delay)) from None
        messages = {
            400: "Telegram 無法接受這次請求，請確認聊天室設定並先向 Bot 傳送 /start。",
            401: "Telegram Bot Token 無效，請在本機設定頁更新。",
            403: "這個個人聊天室目前無法接收 Bot 訊息，請確認已啟動 Bot 且未封鎖它。",
            409: "此 Bot 已有其他收訊設定，請確認使用的是這個工具專用的 Bot。",
        }
        message = messages.get(code, "Telegram 服務暫時無法完成請求，請稍後再試。")
        raise TelegramError(message, code=code if type(code) is int else None) from None

    def _call(self, method: str, payload: dict):
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = getattr(response, "status", 200)
                headers = getattr(response, "headers", None)
                data = self._decode(response.read(65537))
        except urllib.error.HTTPError as exc:
            status, headers = exc.code, exc.headers
            try:
                data = self._decode(exc.read(65537))
            except Exception:
                data = {}
            finally:
                try:
                    exc.close()
                except Exception:
                    pass
            self._api_error(status, data, headers)
        except TelegramError:
            raise
        except Exception:
            # Transport exceptions often include the complete URL (and token).
            # Never expose their text or chained traceback to UI/log consumers.
            raise TelegramError("無法連上 Telegram 或連線逾時，請檢查網路後再試。") from None
        if status >= 400 or data.get("ok") is not True:
            self._api_error(data.get("error_code", status), data, headers)
        return data.get("result")

    def get_me(self) -> dict:
        result = self._call("getMe", {})
        if not isinstance(result, dict) or result.get("is_bot") is not True or not isinstance(result.get("id"), int):
            raise TelegramError("Telegram 未回傳有效的 Bot 資訊。")
        return {key: result[key] for key in ("id", "username", "first_name") if key in result}

    def test_connection(self) -> dict:
        return self.get_me()

    def list_private_chats(self) -> list[dict]:
        """Discover private /start senders; the local user must choose a recipient.

        This does not verify ownership of a chat. It neither advances the update
        offset nor modifies webhook settings. Call only at the user's request.
        """
        result = self._call("getUpdates", {"limit": 100, "timeout": 0, "allowed_updates": ["message"]})
        if not isinstance(result, list):
            raise TelegramError("Telegram 未回傳有效的聊天室清單。")
        chats = {}
        for update in reversed(result):
            if not isinstance(update, dict):
                continue
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            text = message.get("text", "")
            chat = message.get("chat")
            if (not isinstance(text, str) or not re.fullmatch(r"/start(?:@[A-Za-z0-9_]+)?(?:\s+.*)?", text)
                    or not isinstance(chat, dict) or chat.get("type") != "private"):
                continue
            try:
                chat_id = _private_chat_id(chat.get("id"))
            except TelegramError:
                continue
            if chat_id in chats:
                continue
            name = " ".join(str(chat.get(key) or "").strip() for key in ("first_name", "last_name")).strip()
            if chat.get("username"):
                name = f"{name} (@{chat['username']})".strip()
            chats[chat_id] = {"id": chat_id, "name": (name or "個人聊天室")[:160]}
        return list(chats.values())

    def send_text(self, text: str, link: str | None = None) -> dict:
        if self.chat_id is None:
            raise TelegramError("請先選擇接收通知的個人聊天室。")
        payload = {"chat_id": self.chat_id, "text": _message_text(text),
                   "link_preview_options": {"is_disabled": True}}
        if link is not None:
            payload["reply_markup"] = {"inline_keyboard": [[
                {"text": "查看原始貼文", "url": _post_link(link)}
            ]]}
        result = self._call("sendMessage", payload)
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise TelegramError("Telegram 未確認訊息是否送出，請先到手機確認，避免重複發送。")
        return {"message_id": result["message_id"], "chat_id": self.chat_id, "date": result.get("date")}
