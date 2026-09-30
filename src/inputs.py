"""Input helpers: normalize URL lists and load chained Apify dataset / KV sources.

Handles the `requestListSources` editor ([{"url": ...}] and [{"requestsFromUrl": ...}]),
string lists, DOC_TO_MARKDOWN_INPUT-shaped records ({"urls":[{"url":...}]}), and plain
arrays of URL strings or objects with a `url` field (Sitemap Actor dataset rows).
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

import httpx

_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def normalize_url(raw: str, default_scheme: str = "https") -> str | None:
    """Trim, add a scheme to bare hostnames, drop fragments. Returns None for junk."""
    s = (raw or "").strip().strip("\"'<>")
    if not s or s.startswith("#"):
        return None
    if not _URL_RE.match(s):
        if " " in s or "." not in s:
            return None
        s = f"{default_scheme}://{s}"
    if not s.lower().startswith(("http://", "https://")):
        return None
    return s.split("#", 1)[0]


def _split(text: str) -> list[str]:
    return [p for p in re.split(r"[\s,]+", text) if p]


def urls_from_payload(payload: Any) -> list[str]:
    """Extract raw URL strings from a KV/dataset JSON payload."""
    raw: list[str] = []
    if payload is None:
        return raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return _split(payload)
    if isinstance(payload, dict):
        if "urls" in payload:
            return urls_from_payload(payload["urls"])
        if payload.get("url"):
            raw.append(str(payload["url"]))
            return raw
        # single DOC_TO_MARKDOWN_INPUT-like object without urls key — nothing else
        return raw
    if isinstance(payload, list):
        for v in payload:
            if isinstance(v, dict) and v.get("url"):
                raw.append(str(v["url"]))
            elif isinstance(v, str):
                raw.extend(_split(v))
    return raw


async def collect_urls(
    values: Iterable[Any] | str | None,
    *,
    client: httpx.AsyncClient | None = None,
    max_remote_list_bytes: int = 5_000_000,
) -> tuple[list[str], list[str]]:
    """Return (urls, warnings). Deduplicates while preserving order."""
    warnings: list[str] = []
    raw: list[str] = []
    if values is None:
        values = []
    if isinstance(values, str):
        values = [values]
    for v in values:
        if isinstance(v, dict):
            if v.get("url"):
                raw.append(str(v["url"]))
            elif v.get("requestsFromUrl"):
                if client is None:
                    warnings.append(f"Remote URL list ignored (no HTTP client): {v['requestsFromUrl']}")
                    continue
                try:
                    r = await client.get(v["requestsFromUrl"], follow_redirects=True, timeout=30)
                    r.raise_for_status()
                    raw.extend(re.findall(r"https?://[^\s\"'<>]+", r.text[:max_remote_list_bytes]))
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"Could not load remote URL list {v['requestsFromUrl']}: {e}")
        elif isinstance(v, str):
            raw.extend(_split(v))
    seen: set[str] = set()
    out: list[str] = []
    for r in raw:
        u = normalize_url(r)
        if u is None:
            warnings.append(f"Ignored invalid URL: {r[:200]}")
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out, warnings


def merge_unique(existing: list[str], extra: list[str]) -> list[str]:
    seen = set(existing)
    out = list(existing)
    for u in extra:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
