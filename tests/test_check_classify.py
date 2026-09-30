"""Unit tests for error classification and output filter (no network)."""
from __future__ import annotations

import httpx

from check import classify_exception, passes_filter, status_class


def test_status_class():
    assert status_class(200, None) == "2xx"
    assert status_class(301, None) == "3xx"
    assert status_class(404, "http") == "4xx"
    assert status_class(503, "http") == "5xx"
    assert status_class(None, "dns") == "error"


def test_classify_timeout():
    ec, msg = classify_exception(httpx.ReadTimeout("read"))
    assert ec == "timeout"
    assert "timeout" in msg.lower()


def test_filter_broken():
    assert passes_filter({"statusClass": "2xx", "errorClass": "none", "ok": True}, "broken") is False
    assert passes_filter({"statusClass": "4xx", "errorClass": "http", "ok": False}, "broken") is True
    assert passes_filter({"statusClass": "error", "errorClass": "dns", "httpStatus": None}, "errors_only") is True
    assert passes_filter({"statusClass": "3xx", "errorClass": "none"}, "redirects_and_errors") is True
