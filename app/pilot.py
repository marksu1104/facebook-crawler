"""Local group tracking, persistent deduplication, and Telegram post digests."""
import hashlib
import json
import queue
import re
import sqlite3
import threading
import time
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .models import match_keywords
from .notifications import build_digests, format_time, normalize_published_at
from .pilot_browser import PilotBrowser, PilotBrowserError, PilotCancelled, PilotTransientError, pilot_group_url
from .telegram import TelegramClient, TelegramError, TelegramRateLimited
from .token_store import TokenStorageError, WindowsTokenCipher

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class PilotSettings(BaseModel):
    group_url: str = ""
    browser: str = "auto"
    keywords: list[str] = Field(default_factory=list, max_length=30)
    excludes: list[str] = Field(default_factory=list, max_length=30)
    match_mode: str = "any"
    interval_seconds: int = Field(default=120, ge=60, le=3600)
    tracking_mode: str = "continuous"
    max_checks: int = Field(default=3, ge=1, le=10)
    duration_minutes: int = Field(default=10, ge=1, le=60)
    max_posts_per_check: int = Field(default=20, ge=1, le=50)
    notification_timezone: str = "Asia/Taipei"
    telegram_chat_id: str = ""
    telegram_token: SecretStr | None = None
    remember_telegram_token: bool = Field(default_factory=lambda: WindowsTokenCipher.supported)

    @field_validator("tracking_mode")
    @classmethod
    def valid_tracking_mode(cls, value):
        if value not in {"continuous", "limited"}:
            raise ValueError("請選擇持續追蹤或限定次數與時間")
        return value

    @field_validator("notification_timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("請填有效的時區，例如 Asia/Taipei") from None
        return value

    @field_validator("group_url")
    @classmethod
    def clean_url(cls, value):
        return pilot_group_url(value) if value.strip() else ""

    @field_validator("browser")
    @classmethod
    def valid_browser(cls, value):
        if value not in {"auto", "edge", "chrome"}:
            raise ValueError("請選擇 auto、edge 或 chrome")
        return value

    @field_validator("keywords", "excludes")
    @classmethod
    def clean_words(cls, values):
        result = list(dict.fromkeys(word.strip() for word in values if word.strip()))
        if any(len(word) > 60 for word in result):
            raise ValueError("每個關鍵字最多 60 字")
        return result

    @field_validator("match_mode")
    @classmethod
    def valid_mode(cls, value):
        if value not in {"any", "all"}:
            raise ValueError("比對方式須為 any 或 all")
        return value

    @field_validator("telegram_chat_id")
    @classmethod
    def valid_chat(cls, value):
        value = value.strip()
        if value and not re.fullmatch(r"[1-9][0-9]{0,29}", value):
            raise ValueError("此驗證版只接受個人聊天室 ID")
        return value


class PilotBusy(Exception):
    pass


class Cooldown(PilotBusy):
    pass


class PilotDatabase:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, encrypted_value BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS posts (
                    id TEXT PRIMARY KEY, group_url TEXT NOT NULL, post_id TEXT NOT NULL,
                    author TEXT NOT NULL, content TEXT NOT NULL, url TEXT NOT NULL, posted_at TEXT NOT NULL,
                    matched INTEGER NOT NULL, hits TEXT NOT NULL, first_seen TEXT NOT NULL,
                    is_baseline INTEGER NOT NULL, notification_status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                    UNIQUE(group_url,post_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS baselines (
                    group_url TEXT PRIMARY KEY, scope TEXT NOT NULL, established_at TEXT NOT NULL
                );
                UPDATE posts SET notification_status='unknown',error='前次執行在發送途中結束，送達狀態不明；未自動重送。'
                    WHERE notification_status='sending';
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(posts)")}
            if "published_at" not in columns:
                db.execute("ALTER TABLE posts ADD COLUMN published_at TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def load_settings(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        # Settings contain only the remember preference. The token lives in a DPAPI blob.
        values = json.loads(row[0]) if row else {}
        values.pop("telegram_token", None)
        return PilotSettings(**values).model_dump(exclude={"telegram_token"})

    def saved_token(self):
        with self.connect() as db:
            row = db.execute("SELECT encrypted_value FROM secrets WHERE name='telegram'").fetchone()
        return bytes(row[0]) if row else None

    def clear_token(self):
        with self.connect() as db:
            db.execute("DELETE FROM secrets WHERE name='telegram'")

    def save_settings(self, values, reclassify_group=None, encrypted_token=None, hold_pending=False):
        clean = {key: value for key, value in values.items() if key != "telegram_token"}
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES(1,?)", (json.dumps(clean, ensure_ascii=False),))
            if not clean.get("remember_telegram_token", True):
                db.execute("DELETE FROM secrets WHERE name='telegram'")
            elif encrypted_token is not None:
                db.execute("INSERT OR REPLACE INTO secrets VALUES('telegram',?)", (encrypted_token,))
            if reclassify_group:
                self._reclassify(db, reclassify_group, clean)
            if hold_pending:
                db.execute("UPDATE posts SET notification_status='held',error='追蹤條件或接收設定已變更，保留供手動選擇。' WHERE notification_status='pending'")

    @staticmethod
    def scope(settings):
        return json.dumps({key: settings[key] for key in
                           ("keywords", "excludes", "match_mode", "max_posts_per_check")}, sort_keys=True)

    def has_baseline(self, group, settings):
        with self.connect() as db:
            row = db.execute("SELECT scope FROM baselines WHERE group_url=?", (group,)).fetchone()
        return bool(row and row[0] == self.scope(settings))

    def establish_baseline(self, group, settings):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO baselines VALUES(?,?,?)", (group, self.scope(settings), timestamp()))

    def pending_notifications(self, group):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM posts WHERE group_url=? AND notification_status='pending' AND matched=1 ORDER BY first_seen,rowid LIMIT 50", (group,))
            return [self.convert(row) for row in rows]

    @staticmethod
    def _reclassify(db, group, settings):
        rows = db.execute("SELECT id,content,notification_status FROM posts WHERE group_url=?", (group,)).fetchall()
        for row in rows:
            matched, hits = match_keywords(row["content"], settings)
            delivery_status = row["notification_status"]
            if delivery_status in {"preview", "not_matched"}:
                delivery_status = "preview" if matched else "not_matched"
            db.execute("UPDATE posts SET matched=?,hits=?,notification_status=? WHERE id=?",
                       (matched, json.dumps(hits, ensure_ascii=False), delivery_status, row["id"]))

    def reclassify(self, group, settings):
        with self.connect() as db:
            self._reclassify(db, group, settings)

    def event(self, message, level="info"):
        with self.connect() as db:
            db.execute("INSERT INTO events(at,level,message) VALUES(?,?,?)", (timestamp(), level, message))
            db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 100)")

    def events(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT at,level,message FROM events ORDER BY id DESC LIMIT 25")]

    def observe(self, group, post, task, baseline):
        post_id = str(post.get("post_id", ""))
        if not post_id or not post.get("url"):
            return None, False
        identity = hashlib.sha256((group + "\n" + post_id).encode()).hexdigest()[:32]
        matched, hits = match_keywords(post["content"], task)
        published = "" if post.get("timestamp_conflict") else normalize_published_at(post.get("published_at", ""))
        status = "baseline" if baseline else "pending" if matched else "not_matched"
        with self.connect() as db:
            result = db.execute("""INSERT OR IGNORE INTO posts
                (id,group_url,post_id,author,content,url,posted_at,matched,hits,first_seen,is_baseline,notification_status,published_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (identity, group, post_id, post.get("author", ""),
                post["content"], post["url"], post.get("posted_at", ""), matched,
                json.dumps(hits, ensure_ascii=False), timestamp(), baseline, status, published))
            inserted = bool(result.rowcount)
            if not inserted:
                db.execute("""UPDATE posts SET content=?,matched=?,hits=?,posted_at=?,
                           published_at=CASE WHEN ? THEN '' WHEN ?<>'' THEN ? ELSE published_at END,
                           notification_status=CASE WHEN notification_status='pending' AND ?=0 THEN 'not_matched'
                                                    ELSE notification_status END WHERE id=?""",
                           (post["content"], matched, json.dumps(hits, ensure_ascii=False), post.get("posted_at", ""), bool(post.get("timestamp_conflict")), published, published, matched, identity))
        return self.post(identity), inserted

    @staticmethod
    def convert(row):
        if row is None:
            return None
        result = dict(row)
        result["hits"] = json.loads(result["hits"])
        result["matched"] = bool(result["matched"])
        result["is_baseline"] = bool(result["is_baseline"])
        return result

    def post(self, identity):
        with self.connect() as db:
            return self.convert(db.execute("SELECT * FROM posts WHERE id=?", (identity,)).fetchone())

    def posts(self, group):
        if not group:
            return []
        with self.connect() as db:
            return [self.convert(row) for row in db.execute(
                "SELECT * FROM posts WHERE group_url=? ORDER BY first_seen DESC,id DESC LIMIT 60", (group,))]

    def delivery(self, identity, status, error=""):
        self.deliveries([identity], status, error)

    def deliveries(self, identities, status, error=""):
        with self.connect() as db:
            db.executemany("UPDATE posts SET notification_status=?,error=? WHERE id=?",
                           [(status, error, identity) for identity in identities])


class Pilot:
    def __init__(self, data_dir, browser=None, telegram_factory=TelegramClient, clock=time.monotonic, token_cipher=None):
        self.db = PilotDatabase(data_dir / "pilot.sqlite3")
        self.settings = self.db.load_settings()
        self.browser = browser or PilotBrowser(data_dir / "browser-profile")
        self.telegram_factory = telegram_factory
        self.clock = clock
        self.lock = threading.RLock()
        self.telegram_lock = threading.Lock()
        self.token = ""
        self.token_cipher = token_cipher or WindowsTokenCipher()
        self.token_storage_error = ""
        encrypted_token = self.db.saved_token()
        if encrypted_token and self.settings["remember_telegram_token"]:
            try:
                restored = self.token_cipher.decrypt(encrypted_token)
                self.telegram_factory(restored, self.settings["telegram_chat_id"] or None)
                self.token = restored
            except (TokenStorageError, TelegramError):
                self.token_storage_error = "先前儲存的 Token 無法讀取，請重新貼入並儲存一次。"
        self.queue = queue.Queue()
        self.pending = set()
        self.cancel = threading.Event()
        self.shutdown = threading.Event()
        self.generation = 0
        self.cooldown = 0.0
        self.telegram_due = 0.0
        self.group = None
        self.baseline = False
        self.preview_order = []
        self.latched = False
        self.auto = False
        self.remaining = 0
        self.deadline = 0.0
        self.next_check = 0.0
        self.last_checked = None
        self.started_at = None
        self.consecutive_failures = 0
        self.phase = "idle"
        self.message = "先儲存設定，開啟社團視窗並親自登入。尚未進行自動讀取。"
        self.thread = threading.Thread(target=self._loop, name="supervised-pilot", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.shutdown.set()
        self.stop_auto()
        self.thread.join(timeout=40)

    def event(self, message, level="info"):
        # Never pass upstream exceptions or credentials to event storage.
        self.db.event(message, level)

    def state(self):
        with self.lock:
            epoch = time.time()
            settings = {**self.settings, "telegram_configured": bool(self.token),
                        "telegram_token_saved": self.db.saved_token() is not None,
                        "telegram_storage_supported": self.token_cipher.supported,
                        "telegram_storage_error": self.token_storage_error}
            status = {"phase": self.phase, "message": self.message,
                      "browser_open": self.browser.driver is not None,
                      "group_confirmed": bool(self.group) and not self.latched,
                      "resolved_group_url": self.group, "baseline_ready": self.baseline,
                      "auto_running": self.auto, "remaining_checks": self.remaining if self.deadline else None,
                      "started_at": self.started_at, "consecutive_failures": self.consecutive_failures,
                      "pending": sorted(self.pending),
                      "cooldown_until": epoch + max(0, self.cooldown - self.clock()),
                      "next_check_at": epoch + max(0, self.next_check - self.clock()) if self.auto else None,
                      "last_checked": self.last_checked,
                      "preview_post_ids": list(self.preview_order),
                      "scan_summary": dict(getattr(self.browser, "scan_summary", {}))}
            group = self.group
        return {"settings": settings, "status": status, "posts": self.db.posts(group), "events": self.db.events()}

    def save_settings(self, body: PilotSettings):
        with self.lock:
            if self.auto or self.pending:
                raise PilotBusy("請先停止追蹤並等待目前操作結束，再儲存設定。")
            values = body.model_dump(exclude={"telegram_token"})
            if (values["group_url"] != self.settings["group_url"] or values["browser"] != self.settings["browser"]) and self.browser.driver is not None:
                raise PilotBusy("更換社團或瀏覽器前，請先關閉追蹤瀏覽器。")
            proposed_token = body.telegram_token.get_secret_value().strip() if body.telegram_token else ""
            token = proposed_token or self.token
            if token:
                # Validate locally; this does not call Telegram.
                self.telegram_factory(token, values["telegram_chat_id"] or None)
            encrypted_token = None
            if values["remember_telegram_token"] and token:
                encrypted_token = self.token_cipher.encrypt(token)
            changed_scope = any(values[key] != self.settings[key] for key in
                                ("group_url", "keywords", "excludes", "match_mode", "max_posts_per_check"))
            group_changed = values["group_url"] != self.settings["group_url"]
            delivery_changed = (values["telegram_chat_id"] != self.settings["telegram_chat_id"] or
                                (bool(proposed_token) and proposed_token != self.token))
            self.db.save_settings(values, reclassify_group=self.group if changed_scope and not group_changed else None,
                                  encrypted_token=encrypted_token, hold_pending=changed_scope or delivery_changed)
            if group_changed:
                self.group = None
                self.preview_order = []
            if changed_scope:
                self.baseline = False
            self.settings, self.token = values, token
            if encrypted_token is not None or not values["remember_telegram_token"] or self.db.saved_token() is None:
                self.token_storage_error = ""
            self.message = "設定已儲存。Token 已加密保存，重開程式會自動載入。" if encrypted_token else "設定已儲存。"
        return self.state()

    def forget_token(self):
        with self.lock:
            if self.auto or self.pending:
                raise PilotBusy("請先停止追蹤並等待目前操作結束，再清除 Token。")
            self.db.clear_token()
            self.token = ""
            self.token_storage_error = ""
            self.message = "已清除 Token，重新設定後即可使用 Telegram 通知。"
        return self.state()

    def _require_ready(self):
        if self.latched:
            raise PilotBusy("追蹤已因異常暫停。請親自處理頁面，再按確認登入／限制已處理。")
        if not self.group or self.browser.driver is None:
            raise PilotBusy("請先開啟社團，並確認已登入且能正常看到貼文。")

    def _require_cooldown(self):
        wait = self.cooldown - self.clock()
        if wait > 0:
            raise Cooldown(f"請再等 {int(wait) + 1} 秒後檢查；手動與自動讀取共用冷卻時間。")

    def enqueue(self, action):
        with self.lock:
            if self.pending:
                raise PilotBusy("已有操作進行中，請等待完成。")
            if action == "open":
                if not self.settings["group_url"]:
                    raise PilotBusy("請先填入並儲存要驗證的社團網址。")
                if self.browser.driver is not None:
                    raise PilotBusy("追蹤瀏覽器已開啟。")
                self._require_cooldown()
            if action == "check":
                self._require_ready()
                if self.auto:
                    raise PilotBusy("請先停止有限次追蹤，再手動檢查。")
                if self.baseline:
                    self._require_cooldown()
            if action == "confirm" and self.browser.driver is None:
                raise PilotBusy("請先開啟追蹤瀏覽器。")
            if action == "close":
                self.stop_auto()
            self.pending.add(action)
            self.queue.put((action, self.generation, False))
        return {"queued": True}

    def start_auto(self):
        with self.lock:
            self._require_ready()
            if self.pending:
                raise PilotBusy("請等待目前操作結束。")
            if not self.baseline:
                raise PilotBusy("請先讀取目前頁面建立基準，第一輪不發送歷史貼文。")
            self._client()
            if self.clock() < self.telegram_due:
                raise Cooldown("Telegram 仍在冷卻中，請稍後再開始。")
            if self.auto:
                return {"ok": True}
            limited = self.settings["tracking_mode"] == "limited"
            if limited and self.cooldown - self.clock() >= self.settings["duration_minutes"] * 60:
                raise PilotBusy("冷卻時間超過本次測試時限，請稍後再開始或延長測試時間。")
            self.cancel.clear()
            self.auto = True
            self.remaining = self.settings["max_checks"] if limited else 0
            self.deadline = self.clock() + self.settings["duration_minutes"] * 60 if limited else 0.0
            self.started_at = timestamp()
            self.consecutive_failures = 0
            self.next_check = max(self.clock(), self.cooldown)
            self.phase = "ready"
            self.message = "限定追蹤已開始。" if limited else "持續追蹤已開始，會運行到你停止或需要處理登入驗證。"
        self.event(self.message)
        return {"ok": True}

    def stop_auto(self):
        with self.lock:
            self.auto = False
            self.cancel.set()
            self.generation += 1
            if not self.latched:
                self.phase, self.message = "ready" if self.group else "idle", "已停止追蹤；進行中的載入會在可中止時停止。"
        return {"ok": True}

    def _pause(self, message):
        with self.lock:
            self.auto = False
            self.cancel.set()
            self.generation += 1
            self.latched = True
            self.phase, self.message = "paused", message
        self.event(message, "error")

    def _schedule(self):
        with self.lock:
            if not self.auto or self.pending:
                return
            current = self.clock()
            if self.deadline and (current >= self.deadline or self.remaining <= 0):
                self.auto = False
                self.phase, self.message = "ready", "已達本次設定的追蹤上限。"
                self.event(self.message)
                return
            if current >= max(self.next_check, self.cooldown):
                self.pending.add("check")
                self.queue.put(("check", self.generation, True))

    def _recover(self, message, scheduled):
        with self.lock:
            if not scheduled or not self.auto or self.settings["tracking_mode"] != "continuous":
                self._pause(message)
                return
            self.consecutive_failures += 1
            delay = max(self.settings["interval_seconds"], min(3600, 60 * 2 ** min(self.consecutive_failures - 1, 6)))
            self.next_check = max(self.clock() + delay, self.cooldown, self.telegram_due)
            self.phase = "retrying"
            self.message = f"{message} 約 {int(self.next_check - self.clock())} 秒後再檢查。"
        self.event(self.message, "warning")

    def _loop(self):
        try:
            while not self.shutdown.is_set():
                try:
                    action, generation, scheduled = self.queue.get(timeout=0.25)
                except queue.Empty:
                    self._schedule()
                    continue
                try:
                    with self.lock:
                        if generation != self.generation or (scheduled and not self.auto) or self.shutdown.is_set():
                            continue
                        if scheduled and self.deadline and self.clock() >= self.deadline:
                            self.auto = False
                            self.phase, self.message = "ready", "已到達測試時間上限，未開始新的檢查。"
                            continue
                        self.cancel.clear()
                        if scheduled and self.deadline:
                            self.remaining -= 1
                    self._execute(action)
                except PilotCancelled:
                    with self.lock:
                        if not self.latched:
                            self.phase, self.message = "ready" if self.group else "idle", "本次檢查已取消。"
                except Cooldown as exc:
                    with self.lock:
                        self.message = str(exc)
                except PilotTransientError as exc:
                    self._recover(str(exc), scheduled)
                except TelegramRateLimited:
                    self._recover("Telegram 要求冷卻，未送出的貼文已保留。", scheduled)
                except TelegramError as exc:
                    if exc.code is None or exc.code >= 500:
                        self._recover("Telegram 連線暫時異常，請核對送達狀態未明的通知。", scheduled)
                    else:
                        self._pause(str(exc))
                except PilotBrowserError as exc:
                    self._pause(str(exc))
                except Exception:
                    # Do not log exception strings/tracebacks that might contain transport secrets.
                    self._pause("執行失敗，已暫停。請檢查設定與瀏覽器狀態，再明確確認後重試。")
                finally:
                    with self.lock:
                        self.pending.discard(action)
                    self.queue.task_done()
                self._schedule()
        finally:
            self.browser.close()
            self.token = ""

    def _execute(self, action):
        if action == "open":
            with self.lock:
                self._require_cooldown()
                self.cooldown = self.clock() + self.settings["interval_seconds"]
                self.phase, self.message = "opening", "正在開啟社團。首次可能需要下載瀏覽器驅動程式。"
            self.browser.open(self.settings["group_url"], self.settings["browser"])
            with self.lock:
                if self.cancel.is_set():
                    raise PilotCancelled()
                self.group, self.baseline = None, False
                self.preview_order = []
                self.phase, self.message = "awaiting_login", "請親自登入、打開社團首頁並選擇新貼文，再按確認。"
            return
        if action == "close":
            self.browser.close()
            with self.lock:
                self.group, self.baseline = None, False
                self.preview_order = []
                self.phase, self.message = "idle", "追蹤瀏覽器已關閉，獨立登入狀態保留在本機。"
            return
        if action == "confirm":
            group = self.browser.confirm(self.settings["group_url"], self.group)
            self.db.reclassify(group, self.settings)
            with self.lock:
                self.group, self.latched = group, False
                self.baseline = self.db.has_baseline(group, self.settings)
                self.phase = "ready"
                self.message = "已確認社團，沿用上次追蹤起點與去重紀錄。" if self.baseline else "已確認目前社團。首次請讀取頁面建立基準。"
            self.event("已由使用者確認登入與社團頁面。")
            return
        self._check()

    def _check(self):
        with self.lock:
            self._require_ready()
            baseline = not self.baseline
            if not baseline:
                self._require_cooldown()
                self.cooldown = self.clock() + self.settings["interval_seconds"]
            self.phase, self.message = "checking", "正在載入社團貼文並取得原文網址…"
        posts = self.browser.collect(self.group, self.cancel, refresh=not baseline,
                                     max_posts=self.settings["max_posts_per_check"])
        if self.cancel.is_set():
            raise PilotCancelled()
        if not any(post.get("post_id") and post.get("url") for post in posts):
            raise PilotBrowserError("貼文沒有可驗證的原始連結，尚未建立基準或發送通知。請先確認擷取方式。")
        added, matches = 0, 0
        for source in posts:
            if self.cancel.is_set():
                raise PilotCancelled()
            post, inserted = self.db.observe(self.group, source, self.settings, baseline)
            if not post or not inserted:
                continue
            added += 1
            matches += int(post["matched"])
            if baseline or not post["matched"]:
                continue
            if not self.token or not self.settings["telegram_chat_id"]:
                self.db.delivery(post["id"], "preview", "尚未完成 Telegram 設定；只收錄，不補發。")
                continue
        self.db.establish_baseline(self.group, self.settings)
        with self.lock:
            self.baseline = True
            self.preview_order = [source["post_id"] for source in posts if source.get("post_id") and source.get("url")]
            self.last_checked = timestamp()
        sent = self._deliver_posts(self.db.pending_notifications(self.group)) if not baseline and self.token and self.settings["telegram_chat_id"] else 0
        with self.lock:
            self.consecutive_failures = 0
            self.next_check = max(self.clock() + self.settings["interval_seconds"], self.cooldown)
            self.phase = "ready"
            self.message = (f"基準已建立：收錄 {added} 篇，未發送歷史貼文。" if baseline else
                            f"檢查完成：首次收錄 {added} 篇，符合 {matches} 篇，合併送出 {sent} 則通知。")
        self.event(self.message)

    def _client(self, require_chat=True):
        if not self.token:
            raise PilotBusy("請先在本機設定頁填入 Telegram Bot Token。")
        chat = self.settings["telegram_chat_id"]
        if require_chat and not chat:
            raise PilotBusy("請先選擇你自己的 Telegram 私人聊天室並儲存。")
        return self.telegram_factory(self.token, chat or None)

    def _telegram_call(self, method, *, wait=False, cancellable=False):
        with self.telegram_lock:
            remaining = self.telegram_due - self.clock()
            if remaining > 0:
                if not wait:
                    raise Cooldown(f"Telegram 請稍等 {int(remaining) + 1} 秒再操作。")
                if cancellable and self.cancel.wait(remaining):
                    raise PilotCancelled()
                if not cancellable:
                    threading.Event().wait(remaining)
            self.telegram_due = self.clock() + 1.1
            if cancellable and (self.cancel.is_set() or self.shutdown.is_set()):
                raise PilotCancelled()
            try:
                return method()
            except TelegramRateLimited as exc:
                self.telegram_due = max(self.telegram_due, self.clock() + exc.retry_after)
                raise

    @contextmanager
    def _telegram_action(self, name):
        with self.lock:
            if self.auto or self.pending:
                raise PilotBusy("請先停止追蹤並等待目前操作結束，再設定或測試 Telegram。")
            self.pending.add(name)
        try:
            yield
        finally:
            with self.lock:
                self.pending.discard(name)

    def discover(self):
        with self._telegram_action("telegram-discover"):
            client = self._client(require_chat=False)
            bot = self._telegram_call(client.get_me)
            chats = self._telegram_call(client.list_private_chats, wait=True)
            return {"bot": bot, "chats": chats}

    def test_telegram(self):
        with self._telegram_action("telegram-test"):
            client = self._client()
            now = format_time(timestamp(), self.settings["notification_timezone"])
            self._telegram_call(lambda: client.send_text(f"社團追蹤｜本機測試通知\n通知時間 {now}\n時區 {self.settings['notification_timezone']}\n這是你在設定頁主動發送的測試訊息。"))
            self.event("已主動發送一則 Telegram 連線測試訊息。")
            return {"ok": True}

    def _deliver_post(self, post, explicit_test=False):
        return self._deliver_posts([post], explicit_test=explicit_test)

    def _deliver_posts(self, posts, explicit_test=False):
        if not posts:
            return 0
        client = self._client()
        messages = build_digests(posts, self.settings["notification_timezone"], timestamp(), explicit_test)
        for batch, text in messages:
            identities = [post["id"] for post in batch]

            def send():
                if not explicit_test:
                    self.db.deliveries(identities, "sending")
                return client.send_text(text, parse_mode="HTML")

            try:
                self._telegram_call(send, wait=not explicit_test, cancellable=not explicit_test)
            except TelegramRateLimited:
                if not explicit_test:
                    self.db.deliveries(identities, "pending", "Telegram 要求冷卻，等待下一次檢查。")
                raise
            except TelegramError:
                if not explicit_test:
                    self.db.deliveries(identities, "unknown", "Telegram 未確認送達，請核對；未自動重送這則通知。")
                raise
            if not explicit_test:
                self.db.deliveries(identities, "sent")
        return len(messages)

    def test_post(self, identity):
        with self._telegram_action("telegram-post-test"):
            post = self.db.post(identity)
            if post is None or post["group_url"] != self.group:
                raise PilotBusy("找不到目前社團的這篇貼文。")
            self._deliver_post(post, explicit_test=True)
            self.event("已依使用者選擇發送一篇貼文的測試通知。")
            return {"ok": True}


def create_pilot_app(data_dir=None, pilot_factory=Pilot):
    data_dir = Path(data_dir) if data_dir else ROOT / "data" / "pilot"
    pilot = pilot_factory(data_dir)

    @asynccontextmanager
    async def lifespan(app):
        pilot.start()
        yield
        pilot.close()

    app = FastAPI(title="社團追蹤｜本機驗證", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.pilot = pilot
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic's default errors include input values; never echo token-bearing request bodies.
        fields = sorted({str(error["loc"][-1]) for error in exc.errors() if error.get("loc")})
        return JSONResponse({"detail": "設定格式不正確，請檢查：" + "、".join(fields)}, status_code=422)

    @app.exception_handler(PilotBusy)
    async def busy(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=429 if isinstance(exc, Cooldown) else 409)

    @app.exception_handler(TelegramError)
    async def telegram_error(request, exc):
        if isinstance(exc, TelegramRateLimited):
            pilot._pause("Telegram 要求冷卻，追蹤已暫停。請等待後再操作。")
        return JSONResponse({"detail": str(exc)}, status_code=429 if isinstance(exc, TelegramRateLimited) else 400)

    @app.exception_handler(TokenStorageError)
    async def token_storage_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if request.headers.get("x-app-request") != "1" or (origin and origin != str(request.base_url).rstrip("/")):
                return JSONResponse({"detail": "請從本機驗證頁操作"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
        return response

    @app.get("/api/health")
    def health():
        return {"app": "group-watch-pilot", "version": "0.1.0"}

    @app.get("/api/state")
    def state():
        return pilot.state()

    @app.post("/api/settings")
    def settings(body: PilotSettings):
        return pilot.save_settings(body)

    @app.post("/api/browser/{action}", status_code=202)
    def browser_action(action: str):
        if action not in {"open", "confirm", "close"}:
            raise HTTPException(404, "沒有這個操作")
        return pilot.enqueue(action)

    @app.post("/api/check", status_code=202)
    def check():
        return pilot.enqueue("check")

    @app.post("/api/auto/start")
    def start_auto():
        return pilot.start_auto()

    @app.post("/api/auto/stop")
    def stop_auto():
        return pilot.stop_auto()

    @app.post("/api/telegram/discover")
    def discover():
        return pilot.discover()

    @app.post("/api/telegram/test")
    def test_telegram():
        return pilot.test_telegram()

    @app.post("/api/telegram/forget")
    def forget_telegram():
        return pilot.forget_token()

    @app.post("/api/posts/{identity}/test-notification")
    def test_post(identity: str):
        return pilot.test_post(identity)

    @app.post("/api/shutdown")
    def shutdown():
        callback = getattr(app.state, "on_quit", None)
        if callback is None:
            raise PilotBusy("請從啟動視窗按 Ctrl+C 結束。")
        pilot.stop_auto()
        callback()
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "pilot.html")

    return app
