"""Explicit, single-session browser operations for the supervised local pilot.

Manual sign-in with bounded feed loading and passive response collection.
"""
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .browser import canonical_post_url
from .feed_traffic import FeedTraffic
from .notifications import normalize_published_at


FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com"}
SNAPSHOT = r"""
return {
  url: location.href,
  ready: document.readyState,
  visibility: document.visibilityState,
  text: (document.body?.innerText || '').slice(0, 20000),
  login: !!document.querySelector('input[name="email"],input[name="pass"]'),
  challenge: !!document.querySelector('iframe[src*="captcha"],iframe[src*="recaptcha"],[data-testid*="captcha"],input[name="approvals_code"],input[autocomplete="one-time-code"]')
};
"""

SCROLL_FEED = r"""
const feed = document.querySelector('[role="feed"]');
if (!feed && arguments[0] !== 'top') return {moved:false};
let container = feed;
while (container && container !== document.body) {
  const style = getComputedStyle(container);
  if (/(auto|scroll)/.test(style.overflowY) && container.scrollHeight > container.clientHeight + 30) break;
  container = container.parentElement;
}
container = container && container !== document.body ? container : document.scrollingElement;
if (!container) return {moved:false};
const before = container.scrollTop;
if (arguments[0] === 'top') container.scrollTo({top:0, behavior:'instant'});
else container.scrollBy({top:Math.min(900, Math.max(500, window.innerHeight * 0.85)), behavior:'instant'});
return {moved:container.scrollTop > before + 1};
"""

EXTRACTOR_PATH = Path(__file__).with_name("post_extractor.js")
METADATA_PATH = Path(__file__).with_name("post_metadata.js")


def post_extractor_script():
    # Keep independent sources independent: never pair a truncated DOM body with
    # a cached URL by text similarity. Metadata owns its message and identity.
    metadata = METADATA_PATH.read_text(encoding="utf-8")
    rendered = EXTRACTOR_PATH.read_text(encoding="utf-8")
    return (metadata + "\nconst renderedPosts=(function(){\n" + rendered +
            "\n}).apply(null,arguments);\n" +
            "return renderedPosts.concat(extractLoadedPostMetadata(arguments[0], arguments[1] || []));")


PILOT_EXTRACT_POSTS = post_extractor_script()


class PilotBrowserError(Exception):
    """A user-readable failure, with no WebDriver internals or credentials."""


class NeedsUserAction(PilotBrowserError):
    pass


class PilotTransientError(PilotBrowserError):
    """A bounded retry may recover while the confirmed group remains accessible."""


class PilotCancelled(PilotBrowserError):
    pass


def facebook_parts(value: str):
    try:
        parts = urlparse(value)
        if (parts.scheme != "https" or parts.hostname not in FACEBOOK_HOSTS
                or parts.username or parts.password or parts.port not in (None, 443)):
            raise ValueError
        return parts
    except (ValueError, TypeError) as exc:
        raise ValueError("請使用 Facebook 的 HTTPS 社團網址") from exc


def pilot_group_url(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"\d{5,}", value):
        value = f"https://www.facebook.com/groups/{value}"
    parts = facebook_parts(value)
    group = re.fullmatch(r"/groups/([A-Za-z0-9._-]+)/?", parts.path)
    share = re.fullmatch(r"/share/g/([A-Za-z0-9]+)/?", parts.path)
    if group and group[1].lower() not in {"feed", "discover", "create", "joins"}:
        return f"https://www.facebook.com/groups/{group[1]}"
    if share:
        return f"https://www.facebook.com/share/g/{share[1]}/"
    raise ValueError("請填社團首頁、社團 ID 或 /share/g/ 分享網址")


def actual_group_url(value: str) -> str:
    cleaned = pilot_group_url(value)
    if "/share/g/" in cleaned:
        raise ValueError("分享網址尚未解析，請在追蹤視窗打開社團首頁")
    return cleaned


def post_belongs_to_group(value, expected_group):
    try:
        parts = facebook_parts(value)
        group = urlparse(actual_group_url(expected_group)).path.split("/")[2]
        match = re.fullmatch(r"/groups/([^/]+)/(?:posts|permalink)/([^/]+)/?", parts.path)
        return bool(match and match[1] == group)
    except ValueError:
        return False


def consistent_post_content(left, right):
    """Compare bodies only after both sources prove the very same post ID."""
    normalize = lambda value: re.sub(r"[\s\u200b-\u200f\ufeff]", "", value)
    if normalize(left) == normalize(right):
        return True
    for visible, full in ((left, right), (right, left)):
        excerpt = re.sub(r"(?:查看更多|顯示更多|See more)\s*$", "", visible, flags=re.I)
        if excerpt == visible:
            continue
        excerpt = re.sub(r"(?:…|\.{3})\s*$", "", excerpt.strip())
        prefix = normalize(excerpt)
        if len(prefix) >= 30 and normalize(full).startswith(prefix):
            return True
    return False


def check_snapshot(snapshot: dict) -> None:
    try:
        parts = facebook_parts(snapshot.get("url", ""))
    except ValueError as exc:
        raise NeedsUserAction("目前頁面已離開 Facebook，已停止讀取。請回到原社團，再確認。") from exc
    if (snapshot.get("login") or snapshot.get("challenge") or
            any(part in parts.path.lower() for part in
                ("/login", "/checkpoint", "/two_step", "/recover", "/auth_platform"))):
        raise NeedsUserAction("需要你手動完成 Facebook 登入或驗證；自動追蹤已暫停。")
    text = str(snapshot.get("text", "")).casefold()
    restrictions = (
        "you're temporarily blocked", "you’re temporarily blocked", "temporarily restricted",
        "your account has been locked", "we limit how often", "confirm you're human",
        "請確認你是本人", "請完成安全檢查", "你已暫時遭到封鎖", "你的帳號已被鎖定",
        "你暫時無法使用此功能", "你目前無法使用此功能", "我們暫時限制", "我們限制了你使用",
    )
    if any(value in text for value in restrictions):
        raise NeedsUserAction("頁面可能顯示 Facebook 存取限制或安全檢查，已停止自動存取。請親自確認狀態。")


class PilotBrowser:
    def __init__(self, profile_dir: Path, factory=None):
        self.profile_dir = profile_dir
        self.factory = factory
        self.driver = None
        self.browser_name = None
        self.traffic = None
        self.scan_summary = {}
        self.background_loading = False
        self.last_visibility = "unknown"
        self.page_sort = ""
        self._cached_document = None
        self._cached_posts = {}

    def _keep_page_active(self):
        # Chromium's focus emulation keeps background page loading active without
        # bringing its native window in front of the user's other applications.
        try:
            self.driver.execute_cdp_cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})
            self.background_loading = True
        except Exception:
            self.background_loading = False

    def _new_driver(self, preference):
        from selenium import webdriver

        browser = preference
        if browser == "auto":
            edge = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
            browser = "edge" if edge.exists() else "chrome"
        options = webdriver.EdgeOptions() if browser == "edge" else webdriver.ChromeOptions()
        profile = self.profile_dir / browser
        profile.mkdir(parents=True, exist_ok=True)
        options.add_argument(f"--user-data-dir={profile.resolve()}")
        options.add_argument("--lang=zh-TW")
        options.add_argument("--window-size=1280,900")
        logging_key = "ms:loggingPrefs" if browser == "edge" else "goog:loggingPrefs"
        options.set_capability(logging_key, {"performance": "ALL"})
        options.add_experimental_option("perfLoggingPrefs", {"enableNetwork": True, "enablePage": False})
        os.environ.setdefault("SE_AVOID_STATS", "true")
        driver = webdriver.Edge(options=options) if browser == "edge" else webdriver.Chrome(options=options)
        self.browser_name = browser
        return driver

    def open(self, url: str, preference="auto"):
        url = pilot_group_url(url)
        if self.driver is not None:
            raise PilotBrowserError("追蹤視窗已開啟。請使用確認按鈕；如需重開，先關閉追蹤瀏覽器。")
        try:
            self.driver = self.factory(preference) if self.factory else self._new_driver(preference)
            self.browser_name = self.browser_name or preference
            self.driver.set_page_load_timeout(30)
            self.driver.set_script_timeout(10)
            self.traffic = FeedTraffic(self.driver)
            self.traffic.enable()
            self.scan_summary = {}
            self._cached_document = None
            self._cached_posts = {}
            self.driver.get(url)
            snapshot = self.snapshot()
            facebook_parts(snapshot.get("url", ""))
        except PilotBrowserError:
            raise
        except Exception:
            raise PilotBrowserError("無法開啟社團視窗。請確認瀏覽器與網路可用；不會自動重開或重試。") from None

    def snapshot(self):
        if self.driver is None:
            raise PilotBrowserError("追蹤瀏覽器未開啟，請先按開啟社團。")
        try:
            return self.driver.execute_script(SNAPSHOT)
        except Exception:
            raise PilotBrowserError("瀏覽器連線已中斷，已暫停。請明確關閉並重新開啟追蹤視窗。") from None

    def confirm(self, source_url, expected_group=None):
        snapshot = self.snapshot()
        check_snapshot(snapshot)
        self.last_visibility = snapshot.get("visibility", "unknown")
        self.page_sort = parse_qs(urlparse(snapshot["url"]).query).get("sorting_setting", [""])[0]
        try:
            actual = actual_group_url(snapshot["url"])
        except ValueError:
            raise NeedsUserAction("請在追蹤瀏覽器打開社團首頁，確認貼文可見並切為新貼文，再按確認。") from None
        source = pilot_group_url(source_url)
        expected = expected_group or (source if "/groups/" in source else None)
        if expected and actual != expected:
            raise NeedsUserAction("目前開啟的是另一個社團，已停止讀取。請回到原追蹤社團。")
        return actual

    def collect(self, expected_group, cancel: threading.Event, refresh=False, max_posts=20):
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.support.ui import WebDriverWait

        max_posts = max(1, min(50, int(max_posts)))
        self.confirm(expected_group, expected_group)
        if cancel.is_set():
            raise PilotCancelled("檢查已取消。")
        self._keep_page_active()
        self.confirm(expected_group, expected_group)
        if self.last_visibility == "hidden":
            self.background_loading = False
            raise NeedsUserAction("瀏覽器暫停了背景頁面載入。請把追蹤視窗切到前景，再確認後讀取。")
        try:
            extractor = post_extractor_script()
        except OSError:
            raise PilotBrowserError("貼文擷取程式無法讀取，已暫停。請確認程式檔案完整。") from None
        if refresh:
            try:
                self._cached_document = None
                self._cached_posts = {}
                if self.traffic:
                    self.traffic.clear()
                self.driver.refresh()
            except TimeoutException:
                self.confirm(expected_group, expected_group)
                raise PilotTransientError("社團載入逾時。") from None
            except Exception:
                raise PilotBrowserError("重新載入失敗，已暫停；不會立即重試。") from None
        try:
            self.driver.execute_script(SCROLL_FEED, "top")
        except Exception:
            raise PilotBrowserError("無法回到社團貼文開頭，請重新確認追蹤視窗。") from None
        posts, unlinked, conflicts = {}, {}, set()
        scrolls = 0
        stop_reason = "page_end"

        def update_summary():
            self.scan_summary = {"loaded_posts": min(max_posts, len(posts)),
                                 "unlinked_posts": len(unlinked), "scrolls": scrolls,
                                 "target_posts": max_posts, "stop_reason": stop_reason}

        def merge_sources(items):
            for item in items:
                content = str(item.get("content", "")).strip()
                if not content:
                    continue
                raw_url = str(item.get("url", ""))
                url, post_id = canonical_post_url(raw_url) if post_belongs_to_group(raw_url, expected_group) else ("", "")
                post = {"post_id": post_id, "url": url, "content": content,
                        "author": str(item.get("author", "")).strip(), "posted_at": str(item.get("posted_at", "")),
                        "published_at": normalize_published_at(item.get("published_at", ""))}
                if item.get("timestamp_conflict"):
                    post["timestamp_conflict"] = True
                if not post_id:
                    if len(unlinked) < 100:
                        unlinked[(post["author"], content)] = post
                    continue
                if post_id in conflicts:
                    continue
                previous = posts.get(post_id)
                if previous:
                    author_conflict = previous["author"] and post["author"] and previous["author"] != post["author"]
                    if author_conflict or not consistent_post_content(previous["content"], content):
                        posts.pop(post_id)
                        conflicts.add(post_id)
                        continue
                    if len(previous["content"]) > len(content):
                        post["content"] = previous["content"]
                    post["published_at"] = post["published_at"] or previous.get("published_at", "")
                    post["posted_at"] = post["posted_at"] or previous["posted_at"]
                    if previous.get("timestamp_conflict"):
                        post["timestamp_conflict"] = True
                if post.get("timestamp_conflict"):
                    post["published_at"] = ""
                posts[post_id] = post
            update_summary()

        def read_loaded_posts(driver):
            if cancel.is_set():
                return True
            payloads = self.traffic.read() if self.traffic else []
            document_key = getattr(self.traffic, "document_key", None)
            cache_key = (document_key, self.page_sort) if document_key is not None else None
            if cache_key != self._cached_document:
                posts.clear()
                unlinked.clear()
                conflicts.clear()
                self._cached_posts = {}
                self._cached_document = cache_key
            if cache_key is not None:
                merge_sources(list(self._cached_posts.values()))
            items = driver.execute_script(extractor, expected_group, payloads)
            if not isinstance(items, list):
                raise PilotBrowserError("貼文資料格式不正確，請重新確認社團頁面。")
            merge_sources(items)
            if cache_key is not None:
                self._cached_posts = dict(list(posts.items())[:50])
            return bool(posts or unlinked)

        update_summary()
        try:
            WebDriverWait(self.driver, 15, poll_frequency=0.5).until(read_loaded_posts)
        except TimeoutException:
            self.confirm(expected_group, expected_group)
            raise PilotTransientError("等待社團貼文載入逾時。") from None
        except Exception:
            # Catch a login/checkpoint that appeared while the page was loading.
            self.confirm(expected_group, expected_group)
            raise PilotBrowserError("未讀取到可用的貼文內容，已暫停。請確認頁面已載入；也可能需要更新頁面選取器。") from None
        if cancel.is_set():
            raise PilotCancelled("檢查已取消。")
        self.confirm(expected_group, expected_group)
        deadline = time.monotonic() + 35
        no_growth = 0
        max_scrolls = 10 if refresh else 20
        load_attempts = 0
        while len(posts) < max_posts and load_attempts < max_scrolls:
            if cancel.is_set():
                raise PilotCancelled("檢查已取消。")
            if time.monotonic() >= deadline:
                stop_reason = "time_limit"
                break
            self.confirm(expected_group, expected_group)
            load_attempts += 1
            try:
                moved = self.driver.execute_script(SCROLL_FEED)
            except Exception:
                raise PilotBrowserError("載入更多貼文失敗，請重新確認追蹤視窗。") from None
            did_scroll = isinstance(moved, dict) and bool(moved.get("moved"))
            scrolls += int(did_scroll)
            if did_scroll and cancel.wait(1.2):
                raise PilotCancelled("檢查已取消。")
            previous_count = len(posts)

            def read_next_batch(driver):
                read_loaded_posts(driver)
                return cancel.is_set() or len(posts) > previous_count

            if did_scroll:
                # A received feed batch may contain several tall stories. Walk
                # through them before expecting another network batch.
                read_loaded_posts(self.driver)
                no_growth = 0
            else:
                try:
                    WebDriverWait(self.driver, 4, poll_frequency=0.6).until(read_next_batch)
                    no_growth = 0
                except TimeoutException:
                    no_growth += 1
            if cancel.is_set():
                raise PilotCancelled("檢查已取消。")
            self.confirm(expected_group, expected_group)
            if no_growth >= 2:
                stop_reason = "no_more_links"
                break
        if cancel.is_set():
            raise PilotCancelled("檢查已取消。")
        if len(posts) >= max_posts:
            stop_reason = "post_limit"
        elif load_attempts >= max_scrolls:
            stop_reason = "load_limit"
        update_summary()
        if not posts and not unlinked:
            raise PilotBrowserError("沒有可讀取的貼文，未建立空基準。請確認社團內容與擷取方式。")
        return list(posts.values())[:max_posts] + list(unlinked.values())

    def close(self):
        driver, self.driver = self.driver, None
        self.browser_name = None
        self.traffic = None
        self.scan_summary = {}
        self.background_loading = False
        self._cached_document = None
        self._cached_posts = {}
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
