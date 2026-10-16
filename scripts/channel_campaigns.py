#!/usr/bin/env python3
"""PMax (GA4 ad cost by campaign) and Pinterest (Blend manual daily) vs Shopify last-visit UTM.

Same shape as the Meta creative windows: yesterday + last 7 complete days (ET)."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from meta_ads_by_creative import fmt_label, shopify_paid_orders, today_et  # noqa: E402

PINTEREST_CAMPAIGN_IDS = {"626758797715", "626758433774", "626759617021", "626760011313"}


def order_channel(o: dict[str, Any]) -> str:
    src = (o.get("utm_source") or "").lower()
    med = (o.get("utm_medium") or "").lower()
    camp = (o.get("utm_campaign") or "").strip()
    if camp in PINTEREST_CAMPAIGN_IDS or "pinterest" in src:
        return "Pinterest"
    if o.get("is_meta"):
        return "Meta"
    lp = (o.get("landing_page") or "").lower()
    if src == "google" or "pmax" in med or med in ("cpc", "ppc") or "gclid=" in lp:
        return "Google"
    return "Other"


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 2) if b else None


def _row(name: str, spend: float, p_purch: float, p_rev: float, orders: list[dict]) -> dict:
    srev = round(sum(o["revenue"] for o in orders), 2)
    so = len(orders)
    spend = round(spend, 2)
    return {
        "ad": name,
        "spend": spend,
        "platform_purch": round(p_purch, 1),
        "platform_revenue": round(p_rev, 2),
        "platform_roas": _ratio(p_rev, spend),
        "platform_cpa": _ratio(spend, p_purch),
        "shopify_orders": so,
        "shopify_revenue": srev,
        "shopify_roas": _ratio(srev, spend),
        "shopify_cpa": _ratio(spend, so),
        "order_names": sorted(o["order"] for o in orders),
    }


def ga4_campaign_daily(start: date, end: date) -> tuple[list[dict], str | None]:
    from google_spend_ga4 import _access_token, _load_sa_json, fetch_url

    sa = _load_sa_json()
    prop = os.environ.get("GA_PROPERTY_ID", "300437005").replace("properties/", "")
    if not sa:
        return [], "GA_SERVICE_ACCOUNT_JSON not set"
    token = _access_token(sa)
    body = {
        "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
        "dimensions": [
            {"name": "date"},
            {"name": "sessionGoogleAdsCampaignName"},
            {"name": "sessionGoogleAdsCampaignId"},
        ],
        "metrics": [{"name": "advertiserAdCost"}, {"name": "conversions"}, {"name": "purchaseRevenue"}],
        "limit": 1000,
    }
    raw, err = fetch_url(
        f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
        method="POST",
        body=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    if err and not raw.strip():
        return [], err
    data = json.loads(raw)
    if data.get("error"):
        return [], str(data["error"])
    out = []
    for r in data.get("rows") or []:
        dv = [x.get("value") or "" for x in r.get("dimensionValues") or []]
        mv = [float(x.get("value") or 0) for x in r.get("metricValues") or []]
        if len(dv) < 3 or dv[1] in ("(not set)", "(not provided)", ""):
            continue
        d = f"{dv[0][:4]}-{dv[0][4:6]}-{dv[0][6:8]}"
        out.append({"date": d, "campaign": dv[1], "campaign_id": dv[2],
                    "spend": mv[0], "conv": mv[1], "rev": mv[2]})
    return out, None


def _pmax_window(rows: list[dict], orders: list[dict], s: str, e: str) -> list[dict]:
    camps: dict[str, dict] = {}
    for r in rows:
        if s <= r["date"] <= e:
            c = camps.setdefault(r["campaign"], {"id": r["campaign_id"], "spend": 0.0, "conv": 0.0, "rev": 0.0, "orders": []})
            c["spend"] += r["spend"]
            c["conv"] += r["conv"]
            c["rev"] += r["rev"]
    id_to_name = {c["id"]: n for n, c in camps.items()}
    unmatched = []
    for o in orders:
        if not (s <= o["created_at"] <= e):
            continue
        camp = o.get("utm_campaign") or ""
        name = camp if camp in camps else id_to_name.get(camp)
        if name is None and len(camps) == 1:
            name = next(iter(camps))
        if name is None:
            unmatched.append(o)
        else:
            camps[name]["orders"].append(o)
    out = [_row(n, c["spend"], c["conv"], c["rev"], c["orders"]) for n, c in camps.items() if c["spend"] > 0 or c["orders"]]
    if unmatched:
        out.append(_row("Google (UTM not matched to a campaign)", 0, 0, 0, unmatched))
    out.sort(key=lambda r: -r["spend"])
    return out


def _split_days(block: dict) -> None:
    block["by_day"] = {k[4:]: block.pop(k) for k in [k for k in block if k.startswith("day:")]}


def build_channel_campaigns(cfg: dict, today: date | None = None) -> dict[str, Any]:
    today = today or today_et()
    y = today - timedelta(days=1)
    s7 = y - timedelta(days=6)
    s30 = y - timedelta(days=29)
    orders = shopify_paid_orders(s30, today)
    g_orders = [o for o in orders if order_channel(o) == "Google"]
    p_orders = [o for o in orders if order_channel(o) == "Pinterest"]
    windows = {"today": (today, today), "yesterday": (y, y), "7d": (s7, y), "30d": (s30, y)}
    d = s30
    while d <= today:
        windows["day:" + d.isoformat()] = (d, d)
        d += timedelta(days=1)
    out: dict[str, Any] = {"generated_for": today.isoformat()}

    ga_rows, ga_err = ga4_campaign_daily(s30, today)
    pmax: dict[str, Any] = {"source": "GA4 advertiserAdCost / conversions by Google Ads campaign + Shopify last-visit UTM",
                            "error": ga_err}
    for k, (a, b) in windows.items():
        pmax[k] = {"window": {"start": a.isoformat(), "end": b.isoformat(), "label": fmt_label(a, b)},
                   "rows": _pmax_window(ga_rows, g_orders, a.isoformat(), b.isoformat())}
    pmax["daily_budget"] = (cfg or {}).get("pmax_daily_budget")
    _split_days(pmax)
    out["pmax"] = pmax

    man = (cfg or {}).get("pinterest_blend_manual") or {}
    daily = man.get("daily") or {}
    ck = man.get("daily_checkouts") or {}
    ckv = man.get("daily_checkout_value") or {}
    last_day = max(daily) if daily else None
    pin: dict[str, Any] = {"source": "Pinterest spend + checkouts via Blend (manual) + Shopify last-visit UTM",
                           "spend_through": last_day}
    for k, (a, b) in windows.items():
        s, e = a.isoformat(), b.isoformat()
        sp = sum(v for d, v in daily.items() if s <= d <= e)
        pc = sum(v for d, v in ck.items() if s <= d <= e)
        pv = sum(v for d, v in ckv.items() if s <= d <= e)
        po = [o for o in p_orders if s <= o["created_at"] <= e]
        missing = [d.isoformat() for d in (a + timedelta(days=i) for i in range((b - a).days + 1)) if d.isoformat() not in daily]
        pin[k] = {"window": {"start": s, "end": e, "label": fmt_label(a, b)},
                  "missing_spend_days": missing,
                  "rows": [_row("Pinterest (all campaigns)", sp, pc, pv, po)] if (sp or po) else []}
    pin["daily_budget"] = (cfg or {}).get("pinterest_daily_budget")
    pin["budget_note"] = (cfg or {}).get("pinterest_budget_note")
    _split_days(pin)
    out["pinterest"] = pin
    return out
