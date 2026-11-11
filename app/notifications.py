"""Build bounded post digests with explicit publication and detection times."""
import re
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from .telegram import _post_link


def normalize_published_at(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            seconds = float(value)
            if seconds >= 1_000_000_000_000:
                seconds /= 1000
            result = datetime.fromtimestamp(seconds, timezone.utc)
        else:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or not 2004 <= result.year <= 2100:
            return ""
        return result.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


def format_time(value, timezone_name, *, with_offset=True):
    normalized = normalize_published_at(value)
    if not normalized:
        return "未提供"
    local = datetime.fromisoformat(normalized).astimezone(ZoneInfo(timezone_name))
    return local.strftime("%Y-%m-%d %H:%M:%S %z" if with_offset else "%Y-%m-%d %H:%M:%S")


def preview(value, limit, *, preserve_lines=False):
    text = str(value or "")
    if preserve_lines:
        lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    else:
        text = re.sub(r"\s+", " ", text).strip()
    encoded = text.encode("utf-16-le", errors="replace")
    if len(encoded) <= limit * 2:
        return text
    return encoded[:(limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def post_preview(post, index, timezone_name):
    published = format_time(post.get("published_at", ""), timezone_name, with_offset=False)
    if published == "未提供":
        page_time = preview(post.get("posted_at"), 80)
        published = "精確時間未取得" + (f"（頁面顯示 {page_time}）" if page_time else "")
    author = escape(preview(post.get("author") or "未提供發文者", 60))
    content = escape(preview(post.get("content"), 240, preserve_lines=True))
    detected = format_time(post.get("first_seen", ""), timezone_name, with_offset=False)
    url = escape(_post_link(post["url"]), quote=True)
    return (
        f"<b>{index} · {author}</b>\n"
        f"<blockquote>{content}</blockquote>\n"
        f"<i>發文時間 {escape(published)}\n首次偵測 {escape(detected)}</i>\n"
        f'<a href="{url}">開啟原文 ↗</a>'
    )


def build_digests(posts, timezone_name, sent_at, explicit_test=False):
    title = "🧪 貼文通知手動測試" if explicit_test else "🔔 找到新貼文"
    zone_label = "台北時間" if timezone_name == "Asia/Taipei" else timezone_name
    heading = (f"<b>{title} · {len(posts)} 篇</b>\n"
               f"<i>通知時間 {escape(format_time(sent_at, timezone_name, with_offset=False))}"
               f" · {escape(zone_label)}</i>")
    messages = []
    current_posts = []
    text = heading
    for index, post in enumerate(posts, 1):
        entry = post_preview(post, index, timezone_name)
        separator = "\n\n────────────\n\n" if current_posts else "\n\n"
        if len((text + separator + entry).encode("utf-16-le")) > 3900 * 2:
            if not current_posts:
                raise ValueError("單篇通知過長，無法保留完整原文連結。")
            messages.append((current_posts, text))
            current_posts, text = [], heading
            separator = "\n\n"
        if len((text + separator + entry).encode("utf-16-le")) > 3900 * 2:
            raise ValueError("單篇通知過長，無法保留完整原文連結。")
        current_posts.append(post)
        text += separator + entry
    if current_posts:
        messages.append((current_posts, text))
    if len(messages) > 1:
        messages = [(batch, text.replace("</b>", f" · 第 {index}/{len(messages)} 則</b>", 1))
                    for index, (batch, text) in enumerate(messages, 1)]
    return messages
