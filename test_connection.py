"""Smoke test for the RIT REST API.

Hits /case, /securities, and /news with the configured key and reports
pass/fail per endpoint plus a snapshot of the current state. Uses stdlib
only so it runs without `pip install`.

Env vars (all optional):
    RIT_HOST      default "localhost"
    RIT_PORT      default "9999"
    RIT_API_KEY   default "18P31QOO"  (the key baked into the sim's Swagger URL)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


HOST = os.environ.get("RIT_HOST", "localhost")
PORT = os.environ.get("RIT_PORT", "9999")
KEY = os.environ.get("RIT_API_KEY", "18P31QOO")
BASE = f"http://{HOST}:{PORT}/v1"
TIMEOUT = 5.0


def get(path: str):
    req = urllib.request.Request(f"{BASE}{path}", headers={"X-API-Key": KEY})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, json.loads(resp.read().decode())


def check(label: str, path: str, summarize):
    try:
        status, body = get(path)
    except urllib.error.HTTPError as e:
        print(f"  FAIL  {label:14} {path:22} HTTP {e.code} {e.reason}")
        return False
    except urllib.error.URLError as e:
        print(f"  FAIL  {label:14} {path:22} {e.reason}")
        return False
    except Exception as e:  # timeout, json decode, etc.
        print(f"  FAIL  {label:14} {path:22} {type(e).__name__}: {e}")
        return False
    print(f"  OK    {label:14} {path:22} {summarize(body)}")
    return True


def summarize_case(c):
    return (
        f"{c.get('name')!r} status={c.get('status')} "
        f"tick={c.get('tick')}/{c.get('ticks_per_period')} "
        f"period={c.get('period')}/{c.get('total_periods')}"
    )


def summarize_securities(rows):
    if not rows:
        return "(no securities)"
    parts = []
    for r in rows:
        parts.append(
            f"{r['ticker']} pos={r.get('position')} "
            f"bid={r.get('bid')} ask={r.get('ask')} last={r.get('last')}"
        )
    return " | ".join(parts)


def summarize_news(rows):
    if not rows:
        return "(no news yet)"
    latest = rows[0]
    head = (latest.get("headline") or "").strip()
    if len(head) > 60:
        head = head[:57] + "..."
    return f"{len(rows)} item(s); latest tick={latest.get('tick')} \"{head}\""


def main() -> int:
    print(f"RIT connection test -> {BASE} (key {KEY[:3]}***)")
    results = [
        check("case",       "/case",                summarize_case),
        check("securities", "/securities",          summarize_securities),
        check("news",       "/news?limit=5",        summarize_news),
    ]
    ok = all(results)
    print()
    print("PASS: all endpoints reachable." if ok else "FAIL: one or more endpoints unreachable.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
