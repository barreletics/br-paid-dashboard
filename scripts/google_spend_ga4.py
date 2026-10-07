#!/usr/bin/env python3
"""Google Ads spend via GA4 linked reports (service account — no Ads OAuth).

Requires GA_SERVICE_ACCOUNT_JSON (JSON string or file path) and GA_PROPERTY_ID (300437005).

Usage:
  python3 google_spend_ga4.py --start 2026-08-20 --end 2026-08-26
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

from pull_common import fetch_url


def _load_sa_json() -> dict:
    raw = os.environ.get("GA_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        path = os.environ.get("GA_SERVICE_ACCOUNT_JSON_PATH", "").strip()
        if path and os.path.isfile(path):
            return json.loads(open(path, encoding="utf-8").read())
        return {}
    if raw.startswith("{"):
        return json.loads(raw)
    if os.path.isfile(raw):
        return json.loads(open(raw, encoding="utf-8").read())
    return {}


def _access_token(sa: dict) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(
        sa,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    creds.refresh(Request())
    return creds.token


def google_spend_ga4(start: date, end: date) -> tuple[dict, str | None]:
    sa = _load_sa_json()
    prop = os.environ.get("GA_PROPERTY_ID", "300437005").replace("properties/", "")
    if not sa:
        return {}, "GA_SERVICE_ACCOUNT_JSON is not set (GitHub Actions secret GA_SERVICE_ACCOUNT_JSON)"
    if not prop:
        return {}, "GA_PROPERTY_ID is not set"
    try:
        token = _access_token(sa)
    except Exception as exc:
        return {}, f"GA4 service account auth failed: {exc}"
    body = {
        "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
        "dimensions": [{"name": "sessionGoogleAdsCampaignName"}],
        "metrics": [
            {"name": "advertiserAdCost"},
            {"name": "conversions"},
            {"name": "purchaseRevenue"},
        ],
        "limit": 100,
    }
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport"
    payload = json.dumps(body).encode()
    raw, transport_err = fetch_url(
        url,
        method="POST",
        body=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    if transport_err and not raw.strip():
        return {}, f"GA4 request failed: {transport_err}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}, "GA4 returned non-JSON response"
    if data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return {}, f"GA4 API error: {msg}"
    spend = conv = rev = 0.0
    for row in data.get("rows") or []:
        dim = (row.get("dimensionValues") or [{}])[0].get("value") or ""
        if dim in ("(not set)", "(not provided)", ""):
            continue
        vals = row.get("metricValues") or []
        if len(vals) >= 3:
            spend += float(vals[0].get("value") or 0)
            conv += float(vals[1].get("value") or 0)
            rev += float(vals[2].get("value") or 0)
    if not spend:
        return {}, "GA4 returned zero advertiserAdCost for this range (check GA ↔ Google Ads link)"
    return {
        "spend": round(spend, 2),
        "conversions": round(conv, 2),
        "conversion_value": round(rev, 2),
    }, None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    args = p.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    out, err = google_spend_ga4(start, end)
    if err:
        print(json.dumps({"error": err}), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
