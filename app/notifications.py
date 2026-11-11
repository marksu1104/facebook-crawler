"""Build bounded post digests with explicit publication and detection times."""
import re
from datetime import datetime, timezone
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


def format_time(value, timezone_name):
    normalized = normalize_published_at(value)
    if not normalized:
        return "未提供"
    local = datetime.fromisoformat(normalized).astimezone(ZoneInfo(timezone_name))
    return local.strftime("%Y-%m-%d %H:%M:%S %z")


def preview(value, limit):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    encoded = text.encode("utf-16-le", errors="replace")
    if len(encoded) <= limit * 2:
        return text
    return encoded[:(limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def post_preview(post, index, timezone_name):
    published = format_time(post.get("published_at", ""), timezone_name)
    if published == "未提供":
        page_time = preview(post.get("posted_at"), 80)
        published = "未取得精確發文時間" + (f"（頁面顯示 {page_time}）" if page_time else "")
    return (
        f"{index}. {preview(post.get('author') or '未提供發文者', 60)}\n"
        f"發文時間 {published}\n"
        f"首次偵測 {format_time(post.get('first_seen', ''), timezone_name)}\n"
        f"{preview(post.get('content'), 240)}\n{_post_link(post['url'])}"
    )


def build_digests(posts, timezone_name, sent_at, explicit_test=False):
    title = "社團追蹤｜手動測試貼文" if explicit_test else f"社團追蹤｜新符合貼文，共 {len(posts)} 篇"
    heading = f"{title}\n通知時間 {format_time(sent_at, timezone_name)}\n時區 {timezone_name}"
    messages = []
    current_posts = []
    text = heading
    for index, post in enumerate(posts, 1):
        entry = "\n\n" + post_preview(post, index, timezone_name)
        if len((text + entry).encode("utf-16-le")) > 4000 * 2:
            if not current_posts:
                raise ValueError("單篇通知過長，無法保留完整原文連結。")
            messages.append((current_posts, text))
            current_posts, text = [], heading
        if len((text + entry).encode("utf-16-le")) > 4000 * 2:
            raise ValueError("單篇通知過長，無法保留完整原文連結。")
        current_posts.append(post)
        text += entry
    if current_posts:
        messages.append((current_posts, text))
    return messages
