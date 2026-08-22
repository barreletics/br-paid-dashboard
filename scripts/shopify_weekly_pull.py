#!/usr/bin/env python3
"""Shopify ground-truth pull for weekly paid report.

Uses Admin REST + client credentials (same app as Cursor MCP).
Requires env: SHOPIFY_DOMAIN, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET

Usage:
  python3 shopify_weekly_pull.py --days 7
  python3 shopify_weekly_pull.py --start 2026-08-13 --end 2026-08-22
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Detroit")
API_VERSION = "2024-10"


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"Missing env {name}", file=sys.stderr)
        sys.exit(1)
    return v


def get_token(domain: str, client_id: str, client_secret: str) -> str:
    import subprocess

    proc = subprocess.run(
        [
            "curl",
            "-sS",
            "-X",
            "POST",
            f"https://{domain}/admin/oauth/access_token",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                }
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        data = json.loads(proc.stdout)
        token = data.get("access_token")
        if token:
            return token
    # fallback urllib
    body = json.dumps(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }
    ).encode()
    req = urllib.request.Request(
        f"https://{domain}/admin/oauth/access_token",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    token = data.get("access_token")
    if not token:
        raise RuntimeError("No access_token in Shopify response")
    return token


def fetch_orders_curl(
    domain: str, token: str, created_at_min: str, created_at_max: str
) -> list[dict[str, Any]]:
    import subprocess

    fields = (
        "id,name,created_at,total_price,current_total_price,tags,landing_site,referring_site,"
        "financial_status,source_name"
    )
    params = urllib.parse.urlencode(
        {
            "status": "any",
            "limit": "250",
            "order": "created_at desc",
            "created_at_min": created_at_min,
            "created_at_max": created_at_max,
            "fields": fields,
        }
    )
    url = f"https://{domain}/admin/api/{API_VERSION}/orders.json?{params}"
    proc = subprocess.run(
        ["curl", "-sS", url, "-H", f"X-Shopify-Access-Token: {token}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return json.loads(proc.stdout).get("orders", [])
    raise RuntimeError(proc.stderr or "Shopify orders curl failed")


def fetch_orders(
    domain: str, token: str, created_at_min: str, created_at_max: str
) -> list[dict[str, Any]]:
    try:
        return fetch_orders_curl(domain, token, created_at_min, created_at_max)
    except Exception:
        pass
    fields = (
        "id,name,created_at,total_price,current_total_price,tags,landing_site,referring_site,"
        "financial_status,source_name"
    )
    params = urllib.parse.urlencode(
        {
            "status": "any",
            "limit": "250",
            "order": "created_at desc",
            "created_at_min": created_at_min,
            "created_at_max": created_at_max,
            "fields": fields,
        }
    )
    url = f"https://{domain}/admin/api/{API_VERSION}/orders.json?{params}"
    req = urllib.request.Request(
        url, headers={"X-Shopify-Access-Token": token}, method="GET"
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data.get("orders", [])


def channel(o: dict[str, Any]) -> str:
    land = o.get("landing_site") or ""
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(land).query)
    src = (qs.get("utm_source") or [""])[0].lower()
    med = (qs.get("utm_medium") or [""])[0].lower()
    camp = (qs.get("utm_campaign") or [""])[0]
    if camp in (
        "626758797715",
        "626758433774",
        "626759617021",
    ) or "pinterest" in src:
        return "Pinterest"
    if med in ("paid_social", "paid") or (
        src in ("ig", "fb", "facebook", "instagram", "meta")
        and med in ("paid_social", "paid", "cpc", "social")
    ):
        return "Meta"
    if src == "google" or "pmax" in med or med in ("cpc", "ppc"):
        return "Google"
    ref = (o.get("referring_site") or "").lower()
    if "facebook" in ref or "instagram" in ref:
        return "Meta_ref"
    if "google" in ref:
        return "Google_ref"
    if not land and not o.get("referring_site"):
        return "Direct_blank"
    return "Other"


def order_net(o: dict[str, Any]) -> float:
    cur = o.get("current_total_price")
    if cur is not None and str(cur) != "":
        return float(cur)
    return float(o.get("total_price") or 0)


def summarize(orders: list[dict[str, Any]]) -> dict[str, Any]:
    paid = [
        o
        for o in orders
        if order_net(o) > 0
        and o.get("financial_status") == "paid"
        and "TEST ORDER" not in (o.get("tags") or "")
    ]
    by_ch: dict[str, list[float]] = defaultdict(list)
    by_ch_net: dict[str, list[float]] = defaultdict(list)
    daily = Counter()
    daily_rev: dict[str, float] = defaultdict(float)
    daily_rev_net: dict[str, float] = defaultdict(float)
    meta_camps = Counter()
    gross_total = 0.0
    net_total = 0.0
    for o in paid:
        c = channel(o)
        gross = float(o["total_price"])
        net = order_net(o)
        gross_total += gross
        net_total += net
        by_ch[c].append(gross)
        by_ch_net[c].append(net)
        day = o["created_at"][:10]
        daily[day] += 1
        daily_rev[day] += gross
        daily_rev_net[day] += net
        land = o.get("landing_site") or ""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(land).query)
        camp = (qs.get("utm_campaign") or [""])[0]
        med = (qs.get("utm_medium") or [""])[0]
        if med == "paid_social" and camp:
            meta_camps[camp] += 1

    channels = {
        k: {
            "orders": len(v),
            "revenue": round(sum(v), 2),
            "revenue_net": round(sum(by_ch_net[k]), 2),
        }
        for k, v in sorted(by_ch.items(), key=lambda x: -sum(x[1]))
    }
    return {
        "paid_orders": len(paid),
        "paid_revenue": round(gross_total, 2),
        "paid_revenue_net": round(net_total, 2),
        "returns_adjusted": round(gross_total - net_total, 2),
        "channels": channels,
        "daily_orders": dict(sorted(daily.items())),
        "daily_revenue": {k: round(v, 2) for k, v in sorted(daily_rev.items())},
        "daily_revenue_net": {k: round(v, 2) for k, v in sorted(daily_rev_net.items())},
        "meta_utm_campaigns": meta_camps.most_common(10),
        "source": "shopify_rest",
        "truth": True,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--start", type=str, default="")
    p.add_argument("--end", type=str, default="")
    p.add_argument("--out", type=str, default="")
    args = p.parse_args()

    domain = _env("SHOPIFY_DOMAIN")
    cid = _env("SHOPIFY_CLIENT_ID")
    sec = _env("SHOPIFY_CLIENT_SECRET")

    if args.start and args.end:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    else:
        end = datetime.now(TZ).date()
        start = end - timedelta(days=args.days - 1)

    created_min = f"{start.isoformat()}T00:00:00-04:00"
    created_max = f"{(end + timedelta(days=1)).isoformat()}T00:00:00-04:00"

    token = get_token(domain, cid, sec)
    orders = fetch_orders(domain, token, created_min, created_max)
    result = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "summary": summarize(orders),
        "order_count_raw": len(orders),
    }
    out = json.dumps(result, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out)
    print(out)


if __name__ == "__main__":
    main()
