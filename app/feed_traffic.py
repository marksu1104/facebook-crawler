"""Read completed Facebook GraphQL responses already received by Chromium.

Performance logging must be enabled when the owner creates WebDriver. All calls
belong on that driver's owning thread. This adapter never issues page requests,
reads request payloads, or interacts with the page.
"""
import base64
import binascii
import json
import re
from collections import OrderedDict
from urllib.parse import urlparse

from selenium.common.exceptions import UnknownMethodException


MAX_BUFFER_BYTES = 20 * 1024 * 1024
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_READ_BYTES = 15 * 1024 * 1024
MAX_PENDING = 128
MAX_SEEN = 256
MAX_LOG_ENTRY_BYTES = 1024 * 1024
FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com"}


def _graphql_response_url(value):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return False
    try:
        parts = urlparse(value)
        return (parts.scheme == "https" and parts.hostname in FACEBOOK_HOSTS
                and parts.username is None and parts.password is None
                and parts.port in (None, 443)
                and parts.path in {"/api/graphql", "/api/graphql/"})
    except ValueError:
        return False


def _group_document_url(value):
    if not isinstance(value, str):
        return False
    try:
        parts = urlparse(value)
        return (parts.scheme == "https" and parts.hostname in FACEBOOK_HOSTS
                and parts.username is None and parts.password is None
                and parts.port in (None, 443)
                and re.fullmatch(r"/(?:groups/[A-Za-z0-9._-]+|share/g/[A-Za-z0-9]+)/?", parts.path) is not None)
    except ValueError:
        return False


def _performance_event(entry):
    if not isinstance(entry, dict):
        return None
    raw = entry.get("message")
    if not isinstance(raw, str) or len(raw) > MAX_LOG_ENTRY_BYTES:
        return None
    try:
        envelope = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(envelope, dict):
        return None
    event = envelope.get("message", envelope)
    if not isinstance(event, dict) or not isinstance(event.get("params"), dict):
        return None
    request_id = event["params"].get("requestId")
    if not isinstance(request_id, str) or not 0 < len(request_id) <= 256:
        return None
    return event


class FeedTraffic:
    def __init__(self, driver):
        self.driver = driver
        self.supported = False
        self.status = "not_enabled"
        self._enable_attempted = False
        self._pending = OrderedDict()
        self._seen = OrderedDict()
        self._document = None

    @property
    def document_key(self):
        return self._document

    def enable(self) -> bool:
        if self._enable_attempted:
            return self.supported
        self._enable_attempted = True
        try:
            self.driver.execute_cdp_cmd("Network.enable", {
                "maxTotalBufferSize": MAX_BUFFER_BYTES,
                "maxResourceBufferSize": MAX_RESPONSE_BYTES,
                "maxPostDataSize": 0,
            })
        except Exception:
            self._unsupported()
            return False
        self.supported = True
        self.status = "ready"
        return True

    def clear(self):
        """Drain old log events before clearing this document's request state."""
        try:
            if self.supported:
                self.driver.get_log("performance")
                self.status = "ready"
        except Exception:
            self._unsupported()
        finally:
            self._pending.clear()
            self._seen.clear()
            self._document = None

    def _unsupported(self):
        self.supported = False
        self.status = "unsupported"
        self._pending.clear()
        self._seen.clear()
        self._document = None

    def _current_document(self):
        try:
            tree = self.driver.execute_cdp_cmd("Page.getFrameTree", {})
            frame = tree["frameTree"]["frame"]
            document = (frame["id"], frame["loaderId"])
            if not all(isinstance(value, str) and value for value in document):
                raise ValueError
            if not _group_document_url(frame.get("url")):
                raise ValueError
        except Exception:
            self.status = "document_unavailable"
            self._pending.clear()
            self._document = None
            return False
        if document != self._document:
            self._pending.clear()
            self._seen.clear()
            self._document = document
        return True

    def _remember(self, request_id):
        self._seen[request_id] = None
        self._seen.move_to_end(request_id)
        while len(self._seen) > MAX_SEEN:
            self._seen.popitem(last=False)

    def _observe(self, event):
        params = event["params"]
        request_id = params["requestId"]
        method = event.get("method")
        if method == "Network.responseReceived":
            if (params.get("frameId"), params.get("loaderId")) != self._document:
                return
            response = params.get("response")
            if not isinstance(response, dict) or not _graphql_response_url(response.get("url")):
                if request_id in self._pending:
                    del self._pending[request_id]
                    self._remember(request_id)
                return
            if request_id not in self._seen:
                self._pending.setdefault(request_id, False)
                while len(self._pending) > MAX_PENDING:
                    expired, _ = self._pending.popitem(last=False)
                    self._remember(expired)
        elif method == "Network.loadingFinished" and request_id in self._pending:
            self._pending[request_id] = True
        elif method == "Network.loadingFailed" and request_id in self._pending:
            del self._pending[request_id]
            self._remember(request_id)
            self.status = "response_unavailable"

    def _body(self, request_id):
        try:
            result = self.driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
        except (AttributeError, NotImplementedError, UnknownMethodException):
            self._unsupported()
            return None
        except Exception:
            self.status = "response_unavailable"
            return None
        if not isinstance(result, dict) or not isinstance(result.get("body"), str):
            self.status = "invalid_payload"
            return None
        body = result["body"]
        encoded = result.get("base64Encoded", False)
        if not isinstance(encoded, bool):
            self.status = "invalid_payload"
            return None
        if encoded:
            if len(body) > ((MAX_RESPONSE_BYTES + 2) // 3) * 4:
                self.status = "response_too_large"
                return None
            try:
                raw = base64.b64decode(body, validate=True)
            except (ValueError, binascii.Error):
                self.status = "invalid_payload"
                return None
        else:
            if len(body) > MAX_RESPONSE_BYTES:
                self.status = "response_too_large"
                return None
            try:
                raw = body.encode("utf-8")
            except UnicodeError:
                self.status = "invalid_payload"
                return None
        if len(raw) > MAX_RESPONSE_BYTES:
            self.status = "response_too_large"
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            self.status = "invalid_payload"
            return None
        return (text, len(raw)) if text else None

    def read(self) -> list[str]:
        if not self.supported:
            return []
        self.status = "ready"
        if not self._current_document():
            return []
        try:
            entries = self.driver.get_log("performance")
        except Exception:
            self._unsupported()
            return []
        if not isinstance(entries, list):
            self.status = "invalid_logs"
            return []
        for entry in entries:
            event = _performance_event(entry)
            if event is not None:
                self._observe(event)
        payloads, total = [], 0
        for request_id, finished in list(self._pending.items()):
            if not finished:
                continue
            # Reserve a complete maximum-sized resource before another CDP read.
            # Unread completed requests stay pending for the next drain.
            if MAX_READ_BYTES - total < MAX_RESPONSE_BYTES:
                self.status = "read_limit"
                break
            del self._pending[request_id]
            self._remember(request_id)
            body = self._body(request_id)
            if not self.supported:
                return []
            if body is not None:
                text, size = body
                payloads.append(text)
                total += size
        return payloads
