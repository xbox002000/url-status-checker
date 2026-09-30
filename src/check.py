"""HTTP status check: HEAD with GET fallback, redirect final URL, error classification."""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
HEAD_FALLBACK_STATUS = {403, 405, 501}


@dataclass
class CheckResult:
    url: str
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    content_length: int | None = None
    duration_ms: int = 0
    method_used: str | None = None
    redirect_count: int = 0
    error_class: str | None = None  # dns | timeout | ssl | http | network | none
    error: str | None = None
    attempts: int = 0


def classify_exception(e: Exception) -> tuple[str, str]:
    """Return (error_class, short message)."""
    if isinstance(e, httpx.TimeoutException):
        return "timeout", f"timeout ({type(e).__name__})"
    if isinstance(e, httpx.ConnectError):
        msg = str(e) or type(e).__name__
        low = msg.lower()
        if any(k in low for k in ("certificate", "ssl", "tls", "hostname mismatch")):
            return "ssl", f"TLS/certificate error: {msg[:200]}"
        if any(
            k in low
            for k in (
                "name or service not known",
                "nodename nor servname",
                "no address associated",
                "name does not resolve",
                "getaddrinfo",
                "temporary failure in name resolution",
                "dns",
            )
        ):
            return "dns", f"DNS failure: {msg[:200]}"
        return "network", f"connection failed: {msg[:200]}"
    if isinstance(e, (httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProtocolError)):
        return "network", f"{type(e).__name__}: {str(e)[:200]}"
    return "network", f"{type(e).__name__}: {str(e)[:200]}"


def status_class(code: int | None, _error_class: str | None) -> str:
    if code is None:
        return "error"
    if 200 <= code < 300:
        return "2xx"
    if 300 <= code < 400:
        return "3xx"
    if 400 <= code < 500:
        return "4xx"
    if 500 <= code < 600:
        return "5xx"
    return "error"


def passes_filter(item: dict, filt: str) -> bool:
    sc = item.get("statusClass")
    ec = item.get("errorClass")
    if filt == "all":
        return True
    if filt == "broken":
        return sc in ("4xx", "5xx", "error") or (ec not in (None, "none"))
    if filt == "redirects_and_errors":
        return sc in ("3xx", "4xx", "5xx", "error")
    if filt in ("3xx", "4xx", "5xx"):
        return sc == filt
    if filt == "errors_only":
        return sc == "error" or (ec not in (None, "none") and item.get("httpStatus") is None)
    return True


@dataclass
class _Snap:
    status_code: int
    url: str
    content_type: str | None
    content_length: int | None
    redirect_count: int
    retry_after: str | None
    method: str


@dataclass
class Checker:
    client: httpx.AsyncClient
    timeout: float
    retries: int
    per_host: int
    min_delay_ms: int = 0
    follow_redirects: bool = True
    mode: str = "head_then_get"  # head_then_get | head | get
    _host_sems: dict[str, asyncio.Semaphore] = field(default_factory=dict)
    _host_next: dict[str, float] = field(default_factory=dict)
    requests: int = 0

    def _sem(self, url: str) -> asyncio.Semaphore:
        host = urlsplit(url).hostname or ""
        if host not in self._host_sems:
            self._host_sems[host] = asyncio.Semaphore(self.per_host)
        return self._host_sems[host]

    async def _pace(self, url: str) -> None:
        if self.min_delay_ms <= 0:
            return
        host = urlsplit(url).hostname or ""
        now = time.monotonic()
        nxt = self._host_next.get(host, 0.0)
        if now < nxt:
            await asyncio.sleep(nxt - now)
        self._host_next[host] = time.monotonic() + self.min_delay_ms / 1000.0

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = min(2**attempt, 20) + random.random()
        if retry_after and retry_after.strip().isdigit():
            delay = min(max(delay, int(retry_after)), 30)
        await asyncio.sleep(delay)

    def _snap_from_response(self, r: httpx.Response, method: str) -> _Snap:
        cl = r.headers.get("content-length")
        hist = getattr(r, "history", None) or []
        return _Snap(
            status_code=r.status_code,
            url=str(r.url),
            content_type=r.headers.get("content-type"),
            content_length=int(cl) if cl and cl.isdigit() else None,
            redirect_count=len(hist),
            retry_after=r.headers.get("retry-after"),
            method=method,
        )

    def _apply(self, res: CheckResult, snap: _Snap) -> None:
        res.http_status = snap.status_code
        res.final_url = snap.url
        res.method_used = snap.method
        res.content_type = snap.content_type
        res.content_length = snap.content_length
        res.redirect_count = snap.redirect_count
        if snap.status_code >= 400:
            res.error_class = "http"
            res.error = f"HTTP {snap.status_code}"
        else:
            res.error_class = None
            res.error = None

    async def _head(self, url: str) -> _Snap:
        self.requests += 1
        r = await self.client.head(url, follow_redirects=self.follow_redirects, timeout=self.timeout)
        return self._snap_from_response(r, "HEAD")

    async def _get_headers(self, url: str) -> _Snap:
        """GET with streaming; capture headers, discard body."""
        self.requests += 1
        async with self.client.stream(
            "GET", url, follow_redirects=self.follow_redirects, timeout=self.timeout
        ) as r:
            snap = self._snap_from_response(r, "GET")
            try:
                async for _ in r.aiter_bytes(1):
                    break
            except Exception:  # noqa: BLE001
                pass
            return snap

    async def check(self, url: str) -> CheckResult:
        res = CheckResult(url=url)
        t0 = time.time()
        mode = self.mode
        for attempt in range(self.retries + 1):
            res.attempts = attempt + 1
            retry_after = None
            try:
                async with self._sem(url):
                    await self._pace(url)
                    if mode == "get":
                        snap = await self._get_headers(url)
                    elif mode == "head":
                        snap = await self._head(url)
                    else:
                        snap = await self._head(url)
                        if snap.status_code in HEAD_FALLBACK_STATUS or (
                            snap.status_code >= 400
                            and snap.status_code not in RETRY_STATUS
                            and snap.status_code not in (401, 404, 410)
                        ):
                            snap = await self._get_headers(url)
                    self._apply(res, snap)
                    if snap.status_code in RETRY_STATUS and attempt < self.retries:
                        retry_after = snap.retry_after
                        raise _Retry()
                    break
            except _Retry:
                res.error_class = "http"
                res.error = f"HTTP {res.http_status}"
            except Exception as e:  # noqa: BLE001
                ec, msg = classify_exception(e)
                res.error_class, res.error = ec, msg
                res.http_status = None
                if ec in ("dns", "ssl"):
                    break
            if attempt < self.retries:
                await self._backoff(attempt, retry_after)
        res.duration_ms = int((time.time() - t0) * 1000)
        return res

    def to_item(self, res: CheckResult) -> dict:
        sc = status_class(res.http_status, res.error_class)
        if res.http_status is not None and 200 <= res.http_status < 300:
            ok = True
        else:
            ok = False
        return {
            "url": res.url,
            "finalUrl": res.final_url,
            "httpStatus": res.http_status,
            "statusClass": sc,
            "ok": ok,
            "contentType": res.content_type,
            "contentLength": res.content_length,
            "durationMs": res.duration_ms,
            "methodUsed": res.method_used,
            "redirectCount": res.redirect_count,
            "errorClass": res.error_class or "none",
            "error": res.error,
            "attempts": res.attempts,
            "host": urlsplit(res.url).hostname,
        }


class _Retry(Exception):
    pass
