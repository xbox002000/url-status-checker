"""Apify Actor: bulk URL status / broken-link checker.

Accepts a URL list and/or another Actor's dataset / KV record (Sitemap dataset rows,
DOC_TO_MARKDOWN_INPUT). Checks each URL with HEAD → GET fallback, records status,
final URL, content-type, timing and error class. Writes a SUMMARY report.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import resource
import time
from collections import Counter

import httpx
from apify import Actor

from charging import Charger
from check import Checker, passes_filter
from inputs import collect_urls, merge_unique, normalize_url, urls_from_payload

EVENT = "url-checked"
UA = "Mozilla/5.0 (compatible; UrlStatusChecker/0.1; +https://apify.com)"
PUSH_BATCH = 100


async def load_from_dataset(dataset_id: str) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    raw: list[str] = []
    try:
        ds = await Actor.open_dataset(id=dataset_id)
        async for item in ds.iterate_items():
            if isinstance(item, dict) and item.get("url"):
                raw.append(str(item["url"]))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"Could not read dataset {dataset_id}: {e}")
        return [], warnings
    out: list[str] = []
    seen: set[str] = set()
    for r in raw:
        u = normalize_url(r)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
        elif u is None:
            warnings.append(f"Ignored invalid URL from dataset: {r[:200]}")
    return out, warnings


async def load_from_kv(store_id: str, record_key: str) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    try:
        store = await Actor.open_key_value_store(id=store_id)
        rec = await store.get_value(record_key)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"Could not read KV {store_id}/{record_key}: {e}")
        return [], warnings
    if rec is None:
        warnings.append(f"KV record {record_key} not found in store {store_id}")
        return [], warnings
    raw = urls_from_payload(rec)
    out: list[str] = []
    seen: set[str] = set()
    for r in raw:
        u = normalize_url(r)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    if not out:
        warnings.append(f"No URLs found in KV record {record_key}")
    return out, warnings


async def main() -> None:
    async with Actor:
        t0 = time.time()
        inp = await Actor.get_input() or {}
        charger = Charger(EVENT)

        proxy_url = None
        if (inp.get("proxyConfiguration") or {}).get("useApifyProxy") or (
            inp.get("proxyConfiguration") or {}
        ).get("proxyUrls"):
            pc = await Actor.create_proxy_configuration(actor_proxy_input=inp["proxyConfiguration"])
            proxy_url = await pc.new_url() if pc else None

        conc = max(1, min(int(inp.get("maxConcurrency", 8)), 50))
        per_host = max(1, min(int(inp.get("maxConcurrencyPerHost", 2)), 10))
        timeout = float(inp.get("requestTimeoutSecs", 20))
        retries = int(inp.get("maxRetries", 2))
        min_delay = int(inp.get("minDelayMsPerHost", 0) or 0)
        mode = inp.get("httpMethod", "head_then_get") or "head_then_get"
        follow = bool(inp.get("followRedirects", True))
        out_filter = inp.get("outputFilter", "all") or "all"
        headers = {
            "User-Agent": inp.get("userAgent") or UA,
            "Accept": "*/*",
        }
        limits = httpx.Limits(max_connections=conc * 2, max_keepalive_connections=conc)

        async with httpx.AsyncClient(
            headers=headers, limits=limits, proxy=proxy_url, http2=False, follow_redirects=True
        ) as client:
            urls, warns = await collect_urls(inp.get("urls"), client=client)
            for w in warns:
                Actor.log.warning(w)

            if inp.get("datasetId"):
                extra, w2 = await load_from_dataset(str(inp["datasetId"]))
                for w in w2:
                    Actor.log.warning(w)
                urls = merge_unique(urls, extra)
                Actor.log.info(f"Loaded {len(extra)} URL(s) from dataset {inp['datasetId']}")

            if inp.get("keyValueStoreId"):
                key = inp.get("keyValueRecordKey") or "DOC_TO_MARKDOWN_INPUT"
                extra, w3 = await load_from_kv(str(inp["keyValueStoreId"]), str(key))
                for w in w3:
                    Actor.log.warning(w)
                urls = merge_unique(urls, extra)
                Actor.log.info(f"Loaded {len(extra)} URL(s) from KV {inp['keyValueStoreId']}/{key}")

            if not urls:
                raise ValueError(
                    "Provide at least one URL in `urls`, or a `datasetId` / `keyValueStoreId` from another Actor."
                )

            max_urls = int(inp.get("maxUrls", 1000) or 0) or None
            budget = charger.budget(EVENT)
            if budget is not None and (max_urls is None or budget < max_urls):
                Actor.log.info(f"Spending limit allows about {budget} URL(s); capping maxUrls.")
                max_urls = budget
            if max_urls is not None:
                urls = urls[:max_urls]

            checker = Checker(
                client,
                timeout=timeout,
                retries=retries,
                per_host=per_host,
                min_delay_ms=min_delay,
                follow_redirects=follow,
                mode=mode,
            )
            sem = asyncio.Semaphore(conc)
            class_counts: Counter = Counter()
            error_counts: Counter = Counter()
            checked = 0
            saved = 0
            ok_n = broken_n = 0
            lock = asyncio.Lock()
            pending: list[dict] = []

            async def flush(force: bool = False) -> None:
                nonlocal saved, pending
                if not pending:
                    return
                if not force and len(pending) < PUSH_BATCH:
                    return
                batch = pending
                pending = []
                # Charge for every checked URL in this batch; dataset may be a filtered subset.
                # We push only filtered rows; charge count = len(batch) via charge() then push_free.
                # Simpler path when filter is "all": push_and_charge.
                if out_filter == "all":
                    n = await charger.push_and_charge(batch, EVENT)
                    saved += n
                    if n < len(batch):
                        charger.limit_reached = True
                else:
                    await charger.charge(EVENT, count=len(batch))
                    await charger.push_free(batch)
                    saved += len(batch)

            async def one(url: str) -> None:
                nonlocal checked, ok_n, broken_n
                if charger.limit_reached:
                    return
                async with sem:
                    if charger.limit_reached:
                        return
                    try:
                        res = await checker.check(url)
                        item = checker.to_item(res)
                    except Exception as e:  # noqa: BLE001
                        item = {
                            "url": url,
                            "finalUrl": None,
                            "httpStatus": None,
                            "statusClass": "error",
                            "ok": False,
                            "contentType": None,
                            "contentLength": None,
                            "durationMs": 0,
                            "methodUsed": None,
                            "redirectCount": 0,
                            "errorClass": "network",
                            "error": f"{type(e).__name__}: {str(e)[:200]}",
                            "attempts": 0,
                            "host": None,
                        }
                async with lock:
                    checked += 1
                    class_counts[item["statusClass"]] += 1
                    error_counts[item.get("errorClass") or "none"] += 1
                    if item["ok"]:
                        ok_n += 1
                    else:
                        broken_n += 1
                    if passes_filter(item, out_filter):
                        pending.append(item)
                        await flush()
                    else:
                        # Still charge for checked-but-filtered-out URLs
                        await charger.charge(EVENT, count=1)
                    if checked % 25 == 0 or checked == len(urls):
                        await Actor.set_status_message(
                            f"Checked {checked}/{len(urls)} — ok {ok_n}, issues {broken_n}"
                        )

            Actor.log.info(
                f"{len(urls)} URL(s); concurrency={conc}/{per_host} per host; mode={mode}; filter={out_filter}"
            )
            await Actor.set_status_message(f"Checking {len(urls)} URL(s)…")
            await asyncio.gather(*(one(u) for u in urls))
            async with lock:
                await flush(force=True)

        peak_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        summary = {
            "totalChecked": checked,
            "saved": saved,
            "ok": ok_n,
            "notOk": broken_n,
            "byStatusClass": dict(class_counts),
            "byErrorClass": dict(error_counts),
            "outputFilter": out_filter,
            "httpMethod": mode,
            "followRedirects": follow,
            "httpRequests": checker.requests,
            "inputUrls": len(urls),
            "maxUrlsReached": max_urls is not None and checked >= max_urls,
            "charged": dict(charger.counts),
            "durationSecs": round(time.time() - t0, 1),
            "peakMemoryMb": peak_mb,
            "finishedAt": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
        store = await Actor.open_key_value_store()
        await store.set_value("SUMMARY", summary)
        await store.set_value("OUTPUT", summary)
        msg = (
            f"Done: checked {checked}, saved {saved} (filter={out_filter}); "
            f"ok {ok_n}, issues {broken_n}; by class {dict(class_counts)}. "
            f"Charged: {charger.counts or 'nothing'}."
        )
        Actor.log.info(msg + f" Peak memory {peak_mb} MB, {summary['durationSecs']} s.")
        await Actor.set_status_message(msg, is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
