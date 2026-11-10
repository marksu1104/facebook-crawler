import base64
import json
from collections import deque

import pytest
from selenium.common.exceptions import UnknownMethodException

from app.feed_traffic import (
    MAX_BUFFER_BYTES,
    MAX_PENDING,
    MAX_READ_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SEEN,
    FeedTraffic,
)


GRAPHQL = "https://www.facebook.com/api/graphql/"


def event(method, request_id="request.1", **params):
    return {"message": json.dumps({"message": {"method": method, "params": {"requestId": request_id, **params}}})}


def response(request_id="request.1", url=GRAPHQL, *, loader_id="loader.1", frame_id="frame.1", **fields):
    return event("Network.responseReceived", request_id, loaderId=loader_id,
                 frameId=frame_id, response={"url": url, **fields})


def finished(request_id="request.1"):
    return event("Network.loadingFinished", request_id)


class FakeDriver:
    def __init__(self, batches=(), bodies=None, *, enable_error=None, log_error=None):
        self.batches = deque(batches)
        self.bodies = bodies or {}
        self.enable_error = enable_error
        self.log_error = log_error
        self.calls = []
        self.frame = {"id": "frame.1", "loaderId": "loader.1", "url": "https://www.facebook.com/groups/123456"}

    def get_log(self, name):
        self.calls.append(("get_log", name))
        if self.log_error:
            raise self.log_error
        return self.batches.popleft() if self.batches else []

    def execute_cdp_cmd(self, method, params):
        self.calls.append((method, params))
        if method == "Network.enable":
            if self.enable_error:
                raise self.enable_error
            return {}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": self.frame}}
        assert method == "Network.getResponseBody"
        result = self.bodies[params["requestId"]]
        if isinstance(result, Exception):
            raise result
        return result


def enabled(driver):
    traffic = FeedTraffic(driver)
    assert traffic.enable() is True
    return traffic


def body_reads(driver):
    return [params["requestId"] for method, params in driver.calls if method == "Network.getResponseBody"]


def test_enable_sets_only_network_buffer_options_once():
    driver = FakeDriver()
    traffic = enabled(driver)
    assert traffic.enable() is True
    assert driver.calls == [("Network.enable", {"maxTotalBufferSize": MAX_BUFFER_BYTES,
                                                "maxResourceBufferSize": MAX_RESPONSE_BYTES,
                                                "maxPostDataSize": 0})]
    assert traffic.supported is True
    assert traffic.status == "ready"


def test_received_and_finished_batch_returns_existing_payload_once():
    payload = '{"data":{"message":"排球臨打"}}'
    driver = FakeDriver([[response(mimeType="text/plain"), finished()], [response(), finished()]],
                        {"request.1": {"body": payload, "base64Encoded": False}})
    traffic = enabled(driver)
    assert traffic.read() == [payload]
    assert traffic.read() == []
    assert body_reads(driver) == ["request.1"]


def test_waits_for_loading_finished_across_log_drains():
    driver = FakeDriver([[response()], [], [finished()]], {"request.1": {"body": "{}"}})
    traffic = enabled(driver)
    assert traffic.read() == []
    assert traffic.read() == []
    assert body_reads(driver) == []
    assert traffic.read() == ["{}"]


@pytest.mark.parametrize("url", [
    "http://www.facebook.com/api/graphql/", "https://evil.test/api/graphql/",
    "https://www.facebook.com.evil.test/api/graphql/", "https://www.facebook.com/api/graphql/other",
    "https://name@www.facebook.com/api/graphql/", "https://www.facebook.com:444/api/graphql/",
    "https://www.facebook.com\n.evil.test/api/graphql/", "https://www.facebook.com/groups/123456",
])
def test_foreign_or_non_graphql_response_never_reads_body(url):
    driver = FakeDriver([[response(url=url), finished()]], {"request.1": {"body": "private"}})
    assert enabled(driver).read() == []
    assert body_reads(driver) == []


@pytest.mark.parametrize("url", ["https://facebook.com/api/graphql", "https://m.facebook.com/api/graphql/?q=1"])
def test_supported_facebook_hosts_and_endpoint_variants(url):
    driver = FakeDriver([[response(url=url), finished()]], {"request.1": {"body": "{}"}})
    assert enabled(driver).read() == ["{}"]


def test_base64_response_decodes_utf8():
    text = '{"data":"🏐臨打"}'
    encoded = base64.b64encode(text.encode()).decode()
    driver = FakeDriver([[response(), finished()]], {"request.1": {"body": encoded, "base64Encoded": True}})
    assert enabled(driver).read() == [text]


@pytest.mark.parametrize("body", [
    {"body": "%%%", "base64Encoded": True},
    {"body": "/w==", "base64Encoded": True},
    {"body": "{}", "base64Encoded": "false"},
    {"body": 42},
    {"body": "\ud800"},
])
def test_malformed_payload_is_skipped_with_sanitized_status(body):
    driver = FakeDriver([[response(), finished()]], {"request.1": body})
    traffic = enabled(driver)
    assert traffic.read() == []
    assert traffic.status == "invalid_payload"
    assert traffic.supported is True


def test_malformed_logs_and_request_events_are_ignored():
    malformed = [None, "wrong", {}, {"message": "not json"}, {"message": "[]"},
                 {"message": json.dumps({"message": {"method": "Network.responseReceived", "params": []}})},
                 {"message": json.dumps({"message": {"method": "Network.responseReceived", "params": {"requestId": 1}}})},
                 event("Network.requestWillBeSent", request={"postData": "never use this"}), finished()]
    driver = FakeDriver([malformed])
    traffic = enabled(driver)
    assert traffic.read() == []
    assert body_reads(driver) == []


def test_failed_response_does_not_retry_or_drop_other_completed_responses():
    driver = FakeDriver([[response("failed"), finished("failed"), response("good"), finished("good")],
                         [response("failed"), finished("failed")]],
                        {"failed": RuntimeError("sensitive URL or header"), "good": {"body": "{}"}})
    traffic = enabled(driver)
    assert traffic.read() == ["{}"]
    assert traffic.status == "response_unavailable"
    assert traffic.read() == []
    assert body_reads(driver) == ["failed", "good"]


def test_loading_failed_response_is_forgotten_without_body_read():
    driver = FakeDriver([[response(), event("Network.loadingFailed"), finished()]])
    traffic = enabled(driver)
    assert traffic.read() == []
    assert traffic.status == "response_unavailable"
    assert body_reads(driver) == []


def test_redirected_foreign_response_with_same_id_cannot_be_collected():
    driver = FakeDriver([[response(), response(url="https://evil.test/api/graphql/"), finished()]],
                        {"request.1": {"body": "foreign"}})
    assert enabled(driver).read() == []
    assert body_reads(driver) == []


@pytest.mark.parametrize("body", [
    {"body": "a" * (MAX_RESPONSE_BYTES + 1)},
    {"body": "臨" * (MAX_RESPONSE_BYTES // 3 + 1)},
    {"body": base64.b64encode(b"a" * (MAX_RESPONSE_BYTES + 1)).decode(), "base64Encoded": True},
])
def test_oversized_single_response_is_rejected(body):
    driver = FakeDriver([[response(), finished()]], {"request.1": body})
    traffic = enabled(driver)
    assert traffic.read() == []
    assert traffic.status == "response_too_large"


def test_total_read_budget_defers_completed_requests_to_next_drain():
    entries = []
    bodies = {}
    for index in range(4):
        key = str(index)
        entries.extend([response(key), finished(key)])
        bodies[key] = {"body": "x" * MAX_RESPONSE_BYTES}
    driver = FakeDriver([entries], bodies)
    traffic = enabled(driver)
    first = traffic.read()
    assert sum(len(value.encode()) for value in first) == MAX_READ_BYTES
    assert len(first) == 3
    assert traffic.status == "read_limit"
    assert len(traffic.read()) == 1
    assert len(body_reads(driver)) == 4


def test_pending_and_seen_ids_are_bounded():
    entries = [response(str(index)) for index in range(MAX_PENDING + MAX_SEEN + 20)]
    driver = FakeDriver([entries])
    traffic = enabled(driver)
    assert traffic.read() == []
    assert len(traffic._pending) == MAX_PENDING
    assert len(traffic._seen) == MAX_SEEN


def test_clear_drains_previous_document_and_resets_request_state():
    driver = FakeDriver([[response()], [finished()], [response(), finished()]],
                        {"request.1": {"body": "{}"}})
    traffic = enabled(driver)
    assert traffic.read() == []
    traffic.clear()
    assert not traffic._pending
    assert not traffic._seen
    assert traffic.read() == ["{}"]
    assert body_reads(driver) == ["request.1"]


def test_unsupported_network_enable_and_log_failures_degrade_without_raw_errors():
    driver = FakeDriver(enable_error=RuntimeError("secret token"))
    traffic = FeedTraffic(driver)
    assert traffic.enable() is False
    assert traffic.enable() is False
    assert traffic.read() == []
    assert traffic.status == "unsupported"
    assert "secret" not in traffic.status
    assert len(driver.calls) == 1

    driver = FakeDriver(log_error=RuntimeError("secret URL"))
    traffic = enabled(driver)
    assert traffic.read() == []
    assert traffic.supported is False
    assert traffic.status == "unsupported"
    assert traffic.read() == []


def test_unsupported_body_command_returns_no_partial_payloads():
    driver = FakeDriver([[response("good"), finished("good"), response("unsupported"), finished("unsupported")]],
                        {"good": {"body": "{}"}, "unsupported": UnknownMethodException("secret URL")})
    traffic = enabled(driver)
    assert traffic.read() == []
    assert traffic.supported is False
    assert traffic.status == "unsupported"


def test_clear_log_failure_is_safe_and_clears_pending():
    driver = FakeDriver([[response()]])
    traffic = enabled(driver)
    assert traffic.read() == []
    driver.log_error = RuntimeError("secret URL")
    traffic.clear()
    assert not traffic._pending
    assert not traffic._seen
    assert traffic.supported is False
    assert traffic.status == "unsupported"


def test_delayed_previous_document_response_is_rejected_after_refresh():
    driver = FakeDriver([[response("old")], [],
                         [response("old"), finished("old"),
                          response("new", loader_id="loader.2"), finished("new")]],
                        {"old": {"body": "previous document"}, "new": {"body": "current document"}})
    traffic = enabled(driver)
    assert traffic.read() == []
    traffic.clear()
    driver.frame["loaderId"] = "loader.2"
    assert traffic.read() == ["current document"]
    assert body_reads(driver) == ["new"]


def test_document_change_discards_pending_bodies_without_explicit_clear():
    driver = FakeDriver([[response("old")], [finished("old"),
                         response("new", loader_id="loader.2"), finished("new")]],
                        {"old": {"body": "previous"}, "new": {"body": "current"}})
    traffic = enabled(driver)
    assert traffic.read() == []
    driver.frame["loaderId"] = "loader.2"
    assert traffic.read() == ["current"]
    assert body_reads(driver) == ["new"]


def test_response_from_another_frame_is_excluded():
    driver = FakeDriver([[response(frame_id="other-frame"), finished()]], {"request.1": {"body": "iframe"}})
    assert enabled(driver).read() == []
    assert body_reads(driver) == []


def test_unconfirmed_document_never_reads_response_bodies():
    driver = FakeDriver([[response(), finished()]], {"request.1": {"body": "private"}})
    driver.frame["url"] = "https://evil.test/groups/123456"
    traffic = enabled(driver)
    assert traffic.read() == []
    assert traffic.status == "document_unavailable"
    assert body_reads(driver) == []
