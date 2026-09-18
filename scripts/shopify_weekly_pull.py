#!/usr/bin/env python3
"""Shopify ground-truth pull for weekly paid report.

Uses Admin REST + client credentials (same app as Cursor MCP).
Requires env: SHOPIFY_DOMAIN, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET

DTC totals exclude: cancelled/refunded, test, promo/samples, ReturnZap exchanges,
wholesale (reported separately). See accounting/FLOW.md.

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

EXCLUDE_TAGS = frozenset(
    {
        "TEST ORDER",
        "TEST",
        "od-converted",
        "Samples",
        "Promo Item",
        "ReturnZap Exchanged",
    }
)

WHOLESALE_TAGS = frozenset(
    {
        "WHOLESALE ORDER",
        "Wholesale Studio",
        "Wholesale instructor",
        "Xero Invoiced",
    }
)

COUNTABLE_FINANCIAL = frozenset({"paid", "partially_paid", "partially_refunded"})

ORDER_FIELDS = (
    "id,name,created_at,total_price,current_total_price,total_refunded,total_discounts,"
    "total_shipping_price_set,tags,landing_site,referring_site,financial_status,"
    "cancelled_at,source_name,shipping_address,fulfillment_status"
)


def shipping_collected(o: dict[str, Any]) -> float:
    raw = o.get("total_shipping_price_set") or {}
    shop = raw.get("shop_money") or raw.get("shopMoney") or {}
    if shop.get("amount") is not None:
        return float(shop["amount"])
    return 0.0


def ship_to_country(o: dict[str, Any]) -> str:
    addr = o.get("shipping_address") or {}
    return (addr.get("country") or addr.get("country_code") or "").strip()


def is_intl_ship_to(o: dict[str, Any]) -> bool:
    c = ship_to_country(o)
    return bool(c and c not in ("United States", "US", "USA"))


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"Missing env {name}", file=sys.stderr)
        sys.exit(1)
    return v


def parse_tags(tags_str: str) -> set[str]:
    return {t.strip() for t in (tags_str or "").split(",") if t.strip()}


def is_wholesale(tags: set[str]) -> bool:
    if tags & WHOLESALE_TAGS:
        return True
    return any("wholesale" in t.lower() for t in tags)


def order_net(o: dict[str, Any]) -> float:
    cur = o.get("current_total_price")
    if cur is not None and str(cur) != "":
        return float(cur)
    return float(o.get("total_price") or 0)


def classify_order(o: dict[str, Any]) -> str:
    """Return segment: dtc | wholesale | excluded."""
    tags = parse_tags(o.get("tags") or "")

    if is_wholesale(tags):
        return "wholesale"

    if tags & EXCLUDE_TAGS:
        return "excluded"

    if o.get("cancelled_at"):
        return "excluded"

    fs = (o.get("financial_status") or "").lower()
    if fs in ("refunded", "voided"):
        return "excluded"

    if order_net(o) <= 0:
        return "excluded"

    if fs not in COUNTABLE_FINANCIAL:
        return "excluded"

    return "dtc"


def exclude_bucket(o: dict[str, Any]) -> str:
    tags = parse_tags(o.get("tags") or "")
    if tags & {"TEST ORDER", "TEST", "od-converted"}:
        return "test"
    if tags & {"Samples", "Promo Item"}:
        return "promo"
    if "ReturnZap Exchanged" in tags:
        return "exchange"
    if o.get("cancelled_at") or (o.get("financial_status") or "").lower() in (
        "refunded",
        "voided",
    ):
        return "cancelled"
    if order_net(o) <= 0:
        return "zero"
    return "other"


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

    params = urllib.parse.urlencode(
        {
            "status": "any",
            "limit": "250",
            "order": "created_at desc",
            "created_at_min": created_at_min,
            "created_at_max": created_at_max,
            "fields": ORDER_FIELDS,
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
    params = urllib.parse.urlencode(
        {
            "status": "any",
            "limit": "250",
            "order": "created_at desc",
            "created_at_min": created_at_min,
            "created_at_max": created_at_max,
            "fields": ORDER_FIELDS,
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


def _rollup_segment(orders: list[dict[str, Any]], track_daily: bool) -> dict[str, Any]:
    gross_total = 0.0
    net_total = 0.0
    refunded_sum = 0.0
    shipping_sum = 0.0
    discounts_sum = 0.0
    returnzap_exchanges = 0
    intl_orders = 0
    fulfilled = 0
    by_ch: dict[str, list[float]] = defaultdict(list)
    by_ch_net: dict[str, list[float]] = defaultdict(list)
    daily: Counter[str] = Counter()
    daily_rev: dict[str, float] = defaultdict(float)
    daily_rev_net: dict[str, float] = defaultdict(float)
    meta_camps: Counter[str] = Counter()

    for o in orders:
        gross = float(o.get("total_price") or 0)
        net = order_net(o)
        gross_total += gross
        net_total += net
        refunded_sum += float(o.get("total_refunded") or 0)
        shipping_sum += shipping_collected(o)
        discounts_sum += float(o.get("total_discounts") or 0)
        tags = parse_tags(o.get("tags") or "")
        if "ReturnZap Exchanged" in tags:
            returnzap_exchanges += 1
        if is_intl_ship_to(o):
            intl_orders += 1
        if (o.get("fulfillment_status") or "").lower() == "fulfilled":
            fulfilled += 1
        if track_daily:
            c = channel(o)
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

    n = len(orders)
    out: dict[str, Any] = {
        "orders": n,
        "revenue": round(gross_total, 2),
        "revenue_net": round(net_total, 2),
        "unit_economics": {
            "refunds_recorded": round(refunded_sum, 2),
            "returns_adjusted_on_orders": round(gross_total - net_total, 2),
            "shipping_collected_from_customer": round(shipping_sum, 2),
            "discounts": round(discounts_sum, 2),
            "returnzap_exchange_orders": returnzap_exchanges,
            "intl_ship_to_orders": intl_orders,
            "fulfilled_orders": fulfilled,
            "fulfilled_pct": round(fulfilled / n * 100) if n else 0,
            "avg_shipping_collected": round(shipping_sum / n, 2) if n else 0,
        },
    }
    if track_daily:
        out["channels"] = channels
        out["daily_orders"] = dict(sorted(daily.items()))
        out["daily_revenue"] = {k: round(v, 2) for k, v in sorted(daily_rev.items())}
        out["daily_revenue_net"] = {
            k: round(v, 2) for k, v in sorted(daily_rev_net.items())
        }
        out["meta_utm_campaigns"] = meta_camps.most_common(10)
    return out


RETURNS_SKIP_TAGS = frozenset(
    {"TEST ORDER", "TEST", "od-converted", "Samples", "Promo Item"}
)


def returns_rollup(
    orders: list[dict[str, Any]],
    *,
    refund_return_fee_usd: float = 7.95,
    exchange_outbound_ship_usd: float = 9.0,
) -> dict[str, Any]:
    """All non-wholesale orders in window — includes excluded refund/exchange rows."""
    refund_returns = 0
    exchanges = 0
    refunds_cash_out = 0.0
    for o in orders:
        tags = parse_tags(o.get("tags") or "")
        if is_wholesale(tags) or tags & RETURNS_SKIP_TAGS:
            continue
        if (
            "ReturnZap Exchanged" not in tags
            and float(o.get("total_refunded") or 0) <= 0
            and order_net(o) <= 0
        ):
            continue
        if "ReturnZap Exchanged" in tags:
            exchanges += 1
            continue
        ref = float(o.get("total_refunded") or 0)
        if ref > 0:
            refund_returns += 1
            refunds_cash_out += ref
    fee_kept = round(refund_returns * refund_return_fee_usd, 2)
    exchange_cost = round(exchanges * exchange_outbound_ship_usd, 2)
    return {
        "refund_return_orders": refund_returns,
        "exchange_orders": exchanges,
        "refunds_cash_out": round(refunds_cash_out, 2),
        "return_fee_kept_est": fee_kept,
        "exchange_outbound_cost_modeled": exchange_cost,
        "returns_cash_cost": round(refunds_cash_out + exchange_cost, 2),
        "policy_note": (
            "Refund returns: $7.95 kept per return (deducted from refund). "
            "Exchanges: $0 to customer; outbound replacement ship is modeled."
        ),
    }


def summarize(orders: list[dict[str, Any]]) -> dict[str, Any]:
    dtc: list[dict[str, Any]] = []
    wholesale: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    excluded_buckets: Counter[str] = Counter()

    for o in orders:
        segment = classify_order(o)
        if segment == "dtc":
            dtc.append(o)
        elif segment == "wholesale":
            wholesale.append(o)
        else:
            excluded.append(o)
            excluded_buckets[exclude_bucket(o)] += 1

    dtc_roll = _rollup_segment(dtc, track_daily=True)
    wholesale_roll = _rollup_segment(wholesale, track_daily=False)

    excluded_summary = {
        "orders": len(excluded),
        "by_reason": dict(sorted(excluded_buckets.items())),
        "samples": [
            {
                "name": o.get("name"),
                "created_at": (o.get("created_at") or "")[:10],
                "total_price": float(o.get("total_price") or 0),
                "financial_status": o.get("financial_status"),
                "cancelled_at": bool(o.get("cancelled_at")),
                "reason": exclude_bucket(o),
            }
            for o in sorted(
                excluded,
                key=lambda x: float(x.get("total_price") or 0),
                reverse=True,
            )[:8]
        ],
    }

    gross_total = dtc_roll["revenue"]
    net_total = dtc_roll["revenue_net"]

    return {
        "paid_orders": dtc_roll["orders"],
        "paid_revenue": gross_total,
        "paid_revenue_net": net_total,
        "returns_adjusted": round(gross_total - net_total, 2),
        "returns_rollup": returns_rollup(orders),
        "unit_economics": dtc_roll.get("unit_economics") or {},
        "channels": dtc_roll.get("channels", {}),
        "daily_orders": dtc_roll.get("daily_orders", {}),
        "daily_revenue": dtc_roll.get("daily_revenue", {}),
        "daily_revenue_net": dtc_roll.get("daily_revenue_net", {}),
        "meta_utm_campaigns": dtc_roll.get("meta_utm_campaigns", []),
        "wholesale": wholesale_roll,
        "excluded": excluded_summary,
        "segment_note": "paid_orders and paid_revenue are DTC only; wholesale separate; excludes cancelled, test, promo, exchanges",
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
