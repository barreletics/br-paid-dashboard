#!/usr/bin/env python3
"""Pull ad spend + platform metrics for a date range.

Meta: Meta Graph API (META_ACCESS_TOKEN + META_AD_ACCOUNT_ID).
Google / Pinterest: JSON via AD_SPEND_GOOGLE_JSON / AD_SPEND_PINTEREST_JSON env or file path.

Usage:
  python3 ad_spend_pull.py --start 2026-08-20 --end 2026-08-26
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import date


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def meta_insights(start: date, end: date) -> dict:
    token = _env("META_ACCESS_TOKEN")
    acct = _env("META_AD_ACCOUNT_ID", "act_10152741884925238")
    if not token:
        return {}
    if not acct.startswith("act_"):
        acct = f"act_{acct}"
    params = urllib.parse.urlencode(
        {
            "fields": "spend,actions,action_values",
            "time_range": json.dumps({"since": start.isoformat(), "until": end.isoformat()}),
            "access_token": token,
        }
    )
    url = f"https://graph.facebook.com/v21.0/{acct}/insights?{params}"
    raw = ""
    try:
        import subprocess

        proc = subprocess.run(["curl", "-sS", url], capture_output=True, text=True, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            raw = proc.stdout
    except Exception:
        pass
    if not raw:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
    data = json.loads(raw)
    rows = data.get("data") or []
    if not rows:
        return {}
    row = rows[0]
    spend = float(row.get("spend") or 0)
    purch = 0
    purch_val = 0.0
    for a in row.get("actions") or []:
        if a.get("action_type") in ("purchase", "omni_purchase"):
            purch = max(purch, int(float(a.get("value") or 0)))
    for a in row.get("action_values") or []:
        if a.get("action_type") in ("purchase", "omni_purchase"):
            purch_val = max(purch_val, float(a.get("value") or 0))
    return {
        "spend": round(spend, 2),
        "platform_purchases": purch,
        "platform_revenue": round(purch_val, 2),
        "platform_roas": round(purch_val / spend, 2) if spend else 0,
    }


def load_override(env_name: str) -> dict:
    raw = _env(env_name)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        if os.path.isfile(raw):
            return json.loads(open(raw, encoding="utf-8").read())
    return {}


def pull_range(start: date, end: date) -> dict[str, dict]:
    out: dict[str, dict] = {}
    meta = meta_insights(start, end)
    if meta:
        out["Meta"] = meta

    google = load_override("AD_SPEND_GOOGLE_JSON")
    if google:
        spend = float(google.get("spend") or 0)
        conv = float(google.get("conversions") or 0)
        val = float(google.get("conversion_value") or 0)
        out["Google"] = {
            "spend": round(spend, 2),
            "platform_purchases": round(conv),
            "platform_revenue": round(val, 2),
            "platform_roas": round(val / spend, 2) if spend else 0,
        }

    pin = load_override("AD_SPEND_PINTEREST_JSON")
    if pin:
        spend = float(pin.get("spend") or 0)
        chk = int(float(pin.get("checkouts") or 0))
        val = float(pin.get("checkout_value") or 0)
        out["Pinterest"] = {
            "spend": round(spend, 2),
            "platform_purchases": chk,
            "platform_revenue": round(val, 2),
            "platform_roas": round(val / spend, 2) if spend else 0,
        }
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    args = p.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    out = pull_range(start, end)
    print(json.dumps({"start": start.isoformat(), "end": end.isoformat(), "channels": out}, indent=2))


if __name__ == "__main__":
    main()
