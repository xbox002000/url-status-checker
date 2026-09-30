# Bulk URL Status Checker — Broken Links & Redirects

**Check a list of URLs for HTTP status, redirects, content-type and failure class — then get a SUMMARY of what is broken.**
Paste URLs, point at another Actor's dataset, or load a `DOC_TO_MARKDOWN_INPUT`-style key-value record (for example from the Sitemap URL Extractor). Each URL is probed with HEAD, falling back to GET when the server blocks HEAD. Default memory: 256 MB. No browser, no AI keys.

## What you get

- 🔗 **Bulk status check** — status code, final URL after redirects, content-type, content-length, timing
- 🧭 **HEAD → GET fallback** — many CDNs reject HEAD; the Actor retries with a streamed GET (body discarded)
- 🏷️ **Error class** — `dns`, `timeout`, `ssl`, `http`, `network`, or `none` when the request succeeded
- 📊 **SUMMARY report** — counts by status class (`2xx` / `3xx` / `4xx` / `5xx` / `error`) and by error class
- 🧺 **Output filter** — optionally save only broken / 3xx / 4xx / 5xx / network errors to the dataset (SUMMARY still covers every check)
- 🔌 **Chain from Sitemap** — `urls` accepts `{"url":…}` objects; or pass a Sitemap run's `datasetId` / `DOC_TO_MARKDOWN_INPUT` KV record
- 🐢 **Polite** — concurrency + per-host caps, optional delay, retries with backoff for 429 / 5xx
- 💾 **Light** — HTTP only, 256 MB default

## Measured results

Local + private cloud benches (2026-09-30 Asia/Taipei). PPE locked from the 1000-URL settled cost — see **Measured results** above.

| Test | Result |
|---|---|
| Local smoke: example.com + httpbingo 200/404/redirect + invalid DNS host | **5/5 checked in 0.3 s**, peak **75 MB**; classes `2xx×3`, `4xx×1`, `error×1` (DNS) |
| Local filter=`broken` (example.com + 404 + DNS-fail) | checked 3, **saved 2** (404 + DNS); charged 3 |
| Cloud smoke `4cmiGOIr4daLNLJFa` (5 URLs, 256 MB, build 0.1.1) | **SUCCEEDED**; wall 4.5 s; peak **83 MB**; settled **$0.000245** |
| Cloud bench `cKB1gqsrunccLI7Kx` (100 public URLs, build **0.1.2**) | **SUCCEEDED**; wall 8.9 s; peak **86 MB**; settled **$0.000798** (~$0.000008 / URL) |
| Cloud bench `9knsf0XkoShrTGXnM` (1000 public URLs, build **0.1.2**) | **SUCCEEDED**; wall 83 s; peak **94 MB**; settled **$0.006448** (~$0.00000645 / URL); ok 938 / issues 62 |

## Use cases

- **Broken-link audit** after a sitemap crawl
- **URL hygiene before RAG / crawling** — drop 4xx/5xx and dead hosts
- **Redirect inventory** — see where URLs finally land
- **Batch health check** for a known URL list

## How to use

1. Add URLs in **URLs to check**, and/or a **Source dataset ID** / **key-value store** from another run.
2. Optional: set **Max URLs**, **Dataset filter**, concurrency and timeout.
3. Click **Start**. Results appear in the **Dataset**; `SUMMARY` and `OUTPUT` are in the **Key-value store**.

### Input example

```json
{
  "urls": [
    { "url": "https://example.com/" },
    { "url": "https://httpbin.org/status/404" }
  ],
  "maxUrls": 100,
  "httpMethod": "head_then_get",
  "outputFilter": "all"
}
```

Chain from a Sitemap Actor dataset (same `url` field):

```json
{
  "datasetId": "<sitemap-run-default-dataset-id>",
  "maxUrls": 500,
  "outputFilter": "broken"
}
```

Or from `DOC_TO_MARKDOWN_INPUT`:

```json
{
  "keyValueStoreId": "<sitemap-run-default-kv-id>",
  "keyValueRecordKey": "DOC_TO_MARKDOWN_INPUT",
  "outputFilter": "all"
}
```

### Output example (one dataset item)

```json
{
  "url": "https://httpbin.org/status/404",
  "finalUrl": "https://httpbin.org/status/404",
  "httpStatus": 404,
  "statusClass": "4xx",
  "ok": false,
  "contentType": "text/html; charset=utf-8",
  "contentLength": null,
  "durationMs": 180,
  "methodUsed": "HEAD",
  "redirectCount": 0,
  "errorClass": "http",
  "error": "HTTP 404",
  "attempts": 1,
  "host": "httpbin.org"
}
```

### Key-value store records

| Key | Content |
|---|---|
| `SUMMARY` | Counts: `totalChecked`, `saved`, `ok`, `notOk`, `byStatusClass`, `byErrorClass`, filter, duration, peak memory |
| `OUTPUT` | Same summary (kept for consistency with sibling Actors) |

## Pricing

Pay per event:

| Event | Price |
|---|---|
| URL checked (primary) | **$0.0003** per URL (= $0.30 per 1,000) |
| Actor start (Apify synthetic) | $0.00005 per GB (platform default) |

**Worked example:** 1,000 URLs ≈ **$0.30** + one start event. Platform cost measured on a 1,000-URL public bench was about **$0.00645** (own run; not what you pay as revenue share).

Every URL that is actually checked is charged once — including 3xx/4xx/5xx and network errors, and including URLs filtered out of the dataset (`outputFilter`). Failed inputs that never become a checked row are not charged. See **Measured results** above for measured platform cost.

## Chaining: Sitemap → URL status

```python
from apify_client import ApifyClient

client = ApifyClient("<YOUR_APIFY_TOKEN>")

# 1) discover URLs
run = client.actor("YOUR_USERNAME/sitemap-url-discovery").call(run_input={
    "startUrls": [{"url": "https://www.example.com"}],
    "maxUrls": 100,
})

# 2) check their HTTP status (broken only in the dataset)
run2 = client.actor("YOUR_USERNAME/url-status-checker").call(run_input={
    "datasetId": run["defaultDatasetId"],
    "outputFilter": "broken",
    "maxUrls": 100,
})
for item in client.dataset(run2["defaultDatasetId"]).iterate_items():
    print(item["url"], item["httpStatus"], item["errorClass"])
```

## Known limits

- Does **not** render JavaScript or detect soft-404s (short error pages that return HTTP 200).
- Does **not** crawl HTML for in-page links; it only checks the URLs you give it (or load from another Actor).
- Some hosts rate-limit or block data-center IPs — use the proxy input if needed.
- `contentLength` is whatever the server sends in `Content-Length` (often missing on chunked responses).
- Soft-404 detection and HTML in-page link crawling are out of scope (see above).

## FAQ

**Why HEAD then GET?** Many servers (and WAFs) answer HEAD with 403/405. Falling back to GET reads headers only and discards the body.

**Are redirects “broken”?** No. With the default filter they are saved as `statusClass: "3xx"` (or as the final 2xx if redirects are followed). Use filter `redirects_and_errors` or `3xx` if you only want those rows.

**DNS / timeout / SSL?** Recorded with `statusClass: "error"` and `errorClass` set; the run continues.

## License & source

AGPL-3.0. See `LICENSE` and `CHANGELOG.md`.
