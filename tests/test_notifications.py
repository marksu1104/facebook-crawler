import re
from html.parser import HTMLParser

import pytest

from app.notifications import build_digests, format_time, normalize_published_at


POST = {"id": "one", "author": "測試作者", "content": "今晚臨打缺人", "posted_at": "3 分鐘前",
        "first_seen": "2026-11-11T11:06:07+00:00", "published_at": "2026-11-11T11:00:00+00:00",
        "url": "https://www.facebook.com/groups/123456/posts/111/"}


def test_digest_separates_publication_detection_and_notification_times():
    messages = build_digests([POST], "Asia/Taipei", "2026-11-11T11:07:08+00:00")
    assert len(messages) == 1
    text = messages[0][1]
    assert "發文時間 2026-11-11 19:00:00" in text
    assert "首次偵測 2026-11-11 19:06:07" in text
    assert "通知時間 2026-11-11 19:07:08" in text
    assert "台北時間" in text and "+0800" not in text
    assert POST["url"] in text and POST["content"] in text


def test_missing_publication_time_is_explicit_without_estimating_from_relative_text():
    post = {**POST, "published_at": ""}
    text = build_digests([post], "Asia/Taipei", POST["first_seen"])[0][1]
    assert "精確時間未取得（頁面顯示 3 分鐘前）" in text
    assert "首次偵測 2026-11-11 19:06:07" in text


def test_large_digest_preserves_all_previews_and_links_across_valid_message_sizes():
    posts = [{**POST, "id": str(index), "content": "🏐" * 500,
              "url": f"https://www.facebook.com/groups/123456/posts/{index}/"} for index in range(1, 51)]
    messages = build_digests(posts, "Asia/Taipei", POST["first_seen"])
    assert 1 < len(messages) < len(posts)
    assert [post["id"] for batch, _ in messages for post in batch] == [post["id"] for post in posts]
    for _, text in messages:
        assert len(text.encode("utf-16-le")) <= 4096 * 2
        assert "🏐" in text
        assert "第 " in text and " 則</b>" in text
    links = re.findall(r"https://www.facebook.com/groups/123456/posts/[0-9]+/", "\n".join(text for _, text in messages))
    assert links == [post["url"] for post in posts]


def test_digest_preserves_paragraphs_and_escapes_post_markup_in_complete_html_chunks():
    class Structure(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.links = []

        def handle_starttag(self, tag, attrs):
            assert tag in {"b", "i", "blockquote", "a"}
            self.stack.append(tag)
            if tag == "a":
                self.links.append(dict(attrs)["href"])

        def handle_endtag(self, tag):
            assert self.stack.pop() == tag

    dangerous = {**POST, "author": '作者 <script>alert("x")</script>',
                 "content": '第一行 & <b>文字</b>\n第二行\n\n第三行' + '\n&<>"' * 200,
                 "url": POST["url"] + '?a=1&b=2'}
    posts = [{**dangerous, "id": str(index)} for index in range(12)]
    messages = build_digests(posts, "Asia/Taipei", POST["first_seen"])
    assert len(messages) > 1
    assert "第一行 &amp; &lt;b&gt;文字&lt;/b&gt;\n第二行\n\n第三行" in messages[0][1]
    assert "&lt;script&gt;" in messages[0][1]
    assert "────────────" in messages[0][1]
    for batch, text in messages:
        parser = Structure()
        parser.feed(text)
        parser.close()
        assert not parser.stack
        assert parser.links == [post["url"] for post in batch]
        assert len(text.encode("utf-16-le")) <= 4096 * 2


@pytest.mark.parametrize("value", [True, False, None, {}, "3 分鐘前", "2026-11-11T11:00:00", float("nan"), 0, 999999999999999999])
def test_ambiguous_or_invalid_time_is_not_treated_as_a_precise_publication_time(value):
    assert normalize_published_at(value) == ""


def test_numeric_timestamps_and_iana_timezone_are_supported():
    assert normalize_published_at(1700000000) == "2023-11-14T22:13:20+00:00"
    assert normalize_published_at("1700000000000") == "2023-11-14T22:13:20+00:00"
    assert format_time("2026-07-01T12:00:00Z", "America/New_York") == "2026-07-01 08:00:00 -0400"
