#!/usr/bin/env python3
"""Fetch Google Ads + Pinterest spend for a date range (optional env credentials).

Google: GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET,
        GOOGLE_ADS_REFRESH_TOKEN, GOOGLE_ADS_CUSTOMER_ID (2738579027)
Pinterest: PINTEREST_ACCESS_TOKEN, PINTEREST_AD_ACCOUNT_ID (549756523361)

Prints JSON: {"Google": {...}, "Pinterest": {...}}
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def google_spend(start: date, end: date) -> dict:
    dev = _env("GOOGLE_ADS_DEVELOPER_TOKEN")
    cid = _env("GOOGLE_ADS_CLIENT_ID")
    sec = _env("GOOGLE_ADS_CLIENT_SECRET")
    refresh = _env("GOOGLE_ADS_REFRESH_TOKEN")
    customer = _env("GOOGLE_ADS_CUSTOMER_ID", "2738579027")
    if not all([dev, cid, sec, refresh]):
        return {}
    token_body = urllib.parse.urlencode(
        {
            "client_id": cid,
            "client_secret": sec,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=token_body,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        access = json.loads(resp.read()).get("access_token")
    if not access:
        return {}
    query = (
        "SELECT metrics.cost_micros, metrics.conversions, metrics.conversions_value "
        f"FROM customer WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
    )
    body = json.dumps({"query": query}).encode()
    url = f"https://googleads.googleapis.com/v17/customers/{customer}/googleAds:searchStream"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {access}",
            "developer-token": dev,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        chunks = json.loads(resp.read())
    cost = conv = val = 0.0
    for chunk in chunks if isinstance(chunks, list) else [chunks]:
        for row in chunk.get("results") or []:
            m = row.get("metrics") or {}
            cost += int(m.get("costMicros") or 0)
            conv += float(m.get("conversions") or 0)
            val += float(m.get("conversionsValue") or 0)
    spend = cost / 1_000_000
    return {
        "spend": round(spend, 2),
        "conversions": round(conv, 2),
        "conversion_value": round(val, 2),
    }


def pinterest_spend(start: date, end: date) -> dict:
    token = _env("PINTEREST_ACCESS_TOKEN")
    acct = _env("PINTEREST_AD_ACCOUNT_ID", "549756523361")
    if not token:
        return {}
    params = urllib.parse.urlencode(
        {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "columns": "SPEND_IN_DOLLAR,TOTAL_CHECKOUT,TOTAL_CHECKOUT_VALUE_IN_MICRO_DOLLAR",
            "granularity": "TOTAL",
        }
    )
    url = f"https://api.pinterest.com/v5/ad_accounts/{acct}/analytics?{params}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    rows = data if isinstance(data, list) else data.get("data") or [data]
    if not rows:
        return {}
    row = rows[0] if isinstance(rows[0], dict) else {}
    spend = float(row.get("SPEND_IN_DOLLAR") or row.get("spend_in_dollar") or 0)
    chk = int(float(row.get("TOTAL_CHECKOUT") or row.get("total_checkout") or 0))
    val_micro = float(row.get("TOTAL_CHECKOUT_VALUE_IN_MICRO_DOLLAR") or 0)
    return {
        "spend": round(spend, 2),
        "checkouts": chk,
        "checkout_value": round(val_micro / 1_000_000, 2) if val_micro else 0,
    }


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    args = p.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    out = {}
    g = google_spend(start, end)
    if g:
        out["Google"] = g
    pin = pinterest_spend(start, end)
    if pin:
        out["Pinterest"] = pin
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
