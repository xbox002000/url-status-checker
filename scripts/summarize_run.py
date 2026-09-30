"""Summarize the last local run: dataset items and simulated PPE charges (from the charging-log dataset)."""
import collections
import glob
import json


def load(pattern):
    return [json.load(open(f)) for f in sorted(glob.glob(pattern)) if not f.endswith("__metadata__.json")]


items = load("storage/datasets/default/*.json")
print(f"{len(items)} dataset item(s)")
by_status = collections.Counter(i.get("status") or i.get("statusClass") or "-" for i in items)
print("  by status/statusClass:", dict(by_status))
prices = {k: v["eventPriceUsd"] for k, v in json.load(open(".actor/pay_per_event.json")).items()}
counts = collections.Counter()
for e in load("storage/datasets/charging-log/*.json"):
    counts[e["event_name"]] += e["charged_count"]
total = 0.0
for k, n in counts.items():
    if k not in prices:
        print(f"  (simulated at $0) {k}: {n} x  <- keep apify-default-dataset-item REMOVED from the live pricing")
        continue
    total += n * prices[k]
    print(f"  charge {k}: {n} x ${prices[k]} = ${n * prices[k]:.5f}")
print(f"  TOTAL simulated custom-event charge: ${total:.5f}")
