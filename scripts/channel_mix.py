#!/usr/bin/env python3
"""Monthly paid-channel Shopify mix (Jun+) + last-30d share for diversification dashboard."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Detroit")
START = date(2026, 6, 1)

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from halo_trend import fetch_orders_rest  # noqa: E402
from shopify_weekly_pull import channel  # noqa: E402


def month_key(iso: str) -> str:
    return iso[:7]


def paid_bucket(ch: str) -> str | None:
    if ch == "Meta":
        return "meta"
    if ch == "Google":
        return "google"
    if ch == "Pinterest":
        return "pinterest"
    return None


def fetch_dtc_orders(start: date, end: date) -> list[dict]:
    return fetch_orders_rest(start, end)


def aggregate_monthly(orders: list[dict]) -> list[dict]:
    by_m: dict[str, dict[str, float]] = defaultdict(
        lambda: {"meta": 0.0, "google": 0.0, "pinterest": 0.0, "meta_o": 0, "google_o": 0, "pinterest_o": 0}
    )
    EX = {"TEST ORDER", "TEST", "Samples", "Promo Item", "ReturnZap Exchanged", "WHOLESALE ORDER"}
    for o in orders:
        if o.get("cancelled_at"):
            continue
        tags = {t.strip() for t in (o.get("tags") or "").split(",") if t.strip()}
        if tags & EX or any("wholesale" in t.lower() for t in tags):
            continue
        fs = (o.get("financial_status") or "").lower()
        if fs not in ("paid", "partially_paid", "partially_refunded"):
            continue
        rev = float(o.get("current_total_price") or o.get("total_price") or 0)
        if rev <= 0:
            continue
        pb = paid_bucket(channel(o))
        if not pb:
            continue
        m = month_key(o["created_at"])
        by_m[m][pb] += rev
        by_m[m][f"{pb}_o"] += 1
    rows = []
    for m in sorted(by_m.keys()):
        d = by_m[m]
        total = d["meta"] + d["google"] + d["pinterest"]
        rows.append(
            {
                "month": m,
                "meta_revenue": round(d["meta"], 2),
                "google_revenue": round(d["google"], 2),
                "pinterest_revenue": round(d["pinterest"], 2),
                "meta_orders": int(d["meta_o"]),
                "google_orders": int(d["google_o"]),
                "pinterest_orders": int(d["pinterest_o"]),
                "total_ad_revenue": round(total, 2),
                "meta_share_pct": round(d["meta"] / total * 100) if total else None,
                "google_share_pct": round(d["google"] / total * 100) if total else None,
                "pinterest_share_pct": round(d["pinterest"] / total * 100) if total else None,
            }
        )
    return rows


def last_30d_share(channels: list[dict]) -> dict:
    rev = {"meta": 0.0, "google": 0.0, "pinterest": 0.0}
    for c in channels or []:
        name = (c.get("name") or "").lower()
        r = float(c.get("shopify_revenue") or 0)
        if "meta" in name:
            rev["meta"] = r
        elif "google" in name:
            rev["google"] = r
        elif "pinterest" in name:
            rev["pinterest"] = r
    total = sum(rev.values())
    return {
        "meta_revenue": round(rev["meta"], 2),
        "google_revenue": round(rev["google"], 2),
        "pinterest_revenue": round(rev["pinterest"], 2),
        "total_ad_revenue": round(total, 2),
        "meta_share_pct": round(rev["meta"] / total * 100) if total else None,
        "google_share_pct": round(rev["google"] / total * 100) if total else None,
        "pinterest_share_pct": round(rev["pinterest"] / total * 100) if total else None,
    }


def build_channel_mix(windows_30_channels: list[dict] | None = None) -> dict:
    end = datetime.now(TZ).date()
    orders = fetch_dtc_orders(START, end)
    monthly = aggregate_monthly(orders)
    mix_30 = last_30d_share(windows_30_channels or [])

    return {
        "generated_at": datetime.now(TZ).isoformat(),
        "shopify_attribution": "landing_site UTM · Meta paid requires utm_campaign",
        "monthly_from": START.isoformat(),
        "monthly": monthly,
        "last_30d": mix_30,
        "pinterest": {
            "platform_analysis_note": "Jul 14, 2026 report · Pinterest Ads Manager / historical export (not Shopify)",
            "platform_winners": [
                {
                    "campaign": "02.06.25 | Conversions (Performance+)",
                    "period": "Nov 2025 – mid 2026 (paused)",
                    "platform_spend": 5569,
                    "platform_conversions": 109,
                    "platform_revenue": 28354,
                    "platform_roas": 5.09,
                },
                {
                    "ad": "BEST GRIP EVER!! Barre-Pilates",
                    "platform_spend": 3015,
                    "platform_conversions": 55,
                    "platform_roas": 7.88,
                },
                {
                    "ad": "The Entire Shoe Is Grippy!",
                    "platform_spend": 165,
                    "platform_conversions": 10,
                    "platform_roas": 7.12,
                },
                {
                    "campaign": "Shopping Ads (catalog)",
                    "period": "2026",
                    "platform_spend": 725,
                    "platform_conversions": 16,
                    "platform_roas": 8.95,
                },
            ],
            "shopify_utm_by_month": [
                {
                    "month": r["month"],
                    "orders": r["pinterest_orders"],
                    "revenue": r["pinterest_revenue"],
                }
                for r in monthly
                if r["pinterest_orders"] or r["pinterest_revenue"]
            ],
            "current_plan": {
                "as_of": "Oct 2026",
                "campaigns": [
                    "Catalog Sales Prospecting · $10/day",
                    "Sales Winners · $20/day",
                ],
                "creatives": [
                    "The Entire Shoe Is Grippy",
                    "BEST GRIP EVER Barre-Pilates",
                    "No socks. Just grip.",
                ],
                "reinstating": "Winner-based Sales / catalog structure from Jul 2026 analysis — not reviving poisoned old Conversions; fresh campaigns with proven grip creatives.",
                "blend_spend_sep25_oct8": 64.72,
            },
        },
        "tiktok": {
            "status": "not_started",
            "headline": "Not started: next step, starter test",
            "plan": "3–5 concepts adapted from Hear The Grip · small test budget · judge on Shopify orders (utm_source=tiktok)",
        },
    }


if __name__ == "__main__":
    print(json.dumps(build_channel_mix(), indent=2))
