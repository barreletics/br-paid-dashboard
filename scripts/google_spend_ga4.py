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
import urllib.request
from datetime import date


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


def google_spend_ga4(start: date, end: date) -> dict:
    sa = _load_sa_json()
    prop = os.environ.get("GA_PROPERTY_ID", "300437005").replace("properties/", "")
    if not sa or not prop:
        return {}
    token = _access_token(sa)
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
    payload = json.dumps(body)
    raw = ""
    try:
        import subprocess

        proc = subprocess.run(
            [
                "curl",
                "-sS",
                "-X",
                "POST",
                url,
                "-H",
                f"Authorization: Bearer {token}",
                "-H",
                "Content-Type: application/json",
                "-d",
                payload,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw = proc.stdout
    except Exception:
        pass
    if not raw:
        req = urllib.request.Request(
            url,
            data=payload.encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode()
    data = json.loads(raw)
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
        return {}
    return {
        "spend": round(spend, 2),
        "conversions": round(conv, 2),
        "conversion_value": round(rev, 2),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    args = p.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    out = google_spend_ga4(start, end)
    if not out:
        sys.exit(1)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
