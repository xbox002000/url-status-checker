# Changelog

## 0.1.2 — 2026-09-30

- Cloud benches (100 + 1000 public URLs); PPE locked at $0.0003 / `url-checked`
- Charge policy documented (errors + filtered still charged; unchecked inputs free)
- Store title / SEO / categories set via API; icon added; publish checklist


## 0.1 (2026-09-30)
- First private scaffold (D046-B): bulk URL status check with HEAD→GET fallback, redirect final URL,
  content-type / content-length / timing, error class (dns/timeout/ssl/http/network), SUMMARY report,
  output filter, dataset from `urls` / dataset / KV (`DOC_TO_MARKDOWN_INPUT` shape). Draft PPE only.
