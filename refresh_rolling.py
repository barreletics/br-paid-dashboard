#!/usr/bin/env python3
"""Refresh dashboard/data/latest.json for rolling windows (7 / 14 / 30 days).

Pulls Shopify for current, prior, and same-period-last-month for each window size.
Narrative sections are preserved until the weekly agent pass rewrites them.

Usage:
  export SHOPIFY_DOMAIN SHOPIFY_CLIENT_ID SHOPIFY_CLIENT_SECRET
  python3 refresh_rolling.py
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Detroit")
ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
LATEST = ROOT / "data" / "latest.json"
HISTORY = ROOT / "data" / "history.json"
CONFIG = ROOT / "config.json"
WINDOW_SIZES = (7, 14, 30)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def today_detroit() -> date:
    return datetime.now(TZ).date()


def window_for(days: int, through: str, end: date | None = None) -> tuple[date, date]:
    end_d = end or today_detroit()
    if through == "yesterday":
        end_d -= timedelta(days=1)
    start = end_d - timedelta(days=days - 1)
    return start, end_d


def format_label(start: date, end: date) -> str:
    if start.year == end.year:
        if start.month == end.month:
            return f"{start.strftime('%b')} {start.day}–{end.day}, {end.year}"
        return f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}, {end.year}"
    return f"{start.strftime('%b %d, %Y')} – {end.strftime('%b %d, %Y')}"


def shopify_pull(start: date, end: date) -> dict:
    cmd = [
        sys.executable,
        str(SCRIPTS / "shopify_weekly_pull.py"),
        "--start",
        start.isoformat(),
        "--end",
        end.isoformat(),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "shopify pull failed")
    return json.loads(proc.stdout)


def channel_metrics(summary: dict, name: str) -> tuple[int, float, float]:
    ch = summary.get("channels", {}).get(name, {})
    return (
        int(ch.get("orders", 0)),
        float(ch.get("revenue", 0)),
        float(ch.get("revenue_net", ch.get("revenue", 0))),
    )


def roas(revenue: float, spend: float) -> float:
    return round(revenue / spend, 2) if spend else 0.0


def scale_spend(base_spend: float, days: int, base_days: int = 7) -> float:
    if not base_spend:
        return 0.0
    return round(base_spend * days / base_days)


def build_channels(
    channels_base: list[dict],
    days: int,
    cur_s: dict,
    prior_s: dict,
    month_s: dict,
) -> list[dict]:
    rows = copy.deepcopy(channels_base)
    pairs = [
        ("Meta", 0),
        ("Google PMax", 1),
        ("Pinterest", 2),
    ]
    ch_keys = ["Meta", "Google", "Pinterest"]
    for (_name, idx), key in zip(pairs, ch_keys):
        if idx >= len(rows):
            continue
        ch = rows[idx]
        base_spend = float(ch.get("spend") or 0)
        base_last = float(ch.get("spend_last") or base_spend)
        spend = scale_spend(base_spend, days)
        spend_last = scale_spend(base_last, days)
        spend_month = spend  # same weekly rate assumption

        o, r, rn = channel_metrics(cur_s, key)
        ol, rl, rnl = channel_metrics(prior_s, key)
        om, rm, rnm = channel_metrics(month_s, key)

        ch.update(
            spend=spend,
            spend_last=spend_last,
            spend_month=spend_month,
            shopify_orders=o,
            shopify_orders_last=ol,
            shopify_orders_month=om,
            shopify_revenue=round(r),
            shopify_revenue_last=round(rl),
            shopify_revenue_month=round(rm),
            shopify_revenue_net=round(rn),
            shopify_revenue_net_last=round(rnl),
            shopify_revenue_net_month=round(rnm),
            shopify_roas=roas(r, spend),
            shopify_roas_last=roas(rl, spend_last),
            shopify_roas_month=roas(rm, spend_month),
            shopify_roas_net=roas(rn, spend),
            shopify_cpa=round(spend / o, 2) if o else None,
        )
    return rows


def build_snapshot(days: int, through: str, channels_base: list[dict]) -> dict:
    cur_start, cur_end = window_for(days, through)
    prior_end = cur_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=days - 1)
    month_end = cur_start - timedelta(days=28)
    month_start = month_end - timedelta(days=days - 1)

    cur = shopify_pull(cur_start, cur_end)
    prior = shopify_pull(prior_start, prior_end)
    month = shopify_pull(month_start, month_end)

    cur_s, prior_s, month_s = cur["summary"], prior["summary"], month["summary"]
    channels = build_channels(channels_base, days, cur_s, prior_s, month_s)
    total_spend = sum(float(c.get("spend") or 0) for c in channels)

    paid_orders = int(cur_s.get("paid_orders", 0))
    paid_revenue = float(cur_s.get("paid_revenue", 0))
    paid_revenue_net = float(cur_s.get("paid_revenue_net", paid_revenue))
    prior_orders = int(prior_s.get("paid_orders", 0))
    prior_revenue = float(prior_s.get("paid_revenue", 0))
    prior_revenue_net = float(prior_s.get("paid_revenue_net", prior_revenue))
    month_orders = int(month_s.get("paid_orders", 0))
    month_revenue = float(month_s.get("paid_revenue", 0))
    month_revenue_net = float(month_s.get("paid_revenue_net", month_revenue))
    prior_spend = sum(float(c.get("spend_last") or 0) for c in channels) or total_spend
    month_spend = total_spend

    kpis = {
        "paid_orders": paid_orders,
        "paid_revenue": round(paid_revenue, 2),
        "paid_revenue_net": round(paid_revenue_net, 2),
        "returns_adjusted": round(paid_revenue - paid_revenue_net, 2),
        "total_ad_spend": round(total_spend),
        "blended_mer": round(paid_revenue / total_spend, 2) if total_spend else 0,
        "blended_mer_net": round(paid_revenue_net / total_spend, 2) if total_spend else 0,
        "cpa_blended": round(total_spend / paid_orders, 2) if paid_orders else None,
        "aov": round(paid_revenue / paid_orders, 2) if paid_orders else None,
        "aov_net": round(paid_revenue_net / paid_orders, 2) if paid_orders else None,
        "spend_estimated": days != 7,
    }

    return {
        "window": {
            "start": cur_start.isoformat(),
            "end": cur_end.isoformat(),
            "label": format_label(cur_start, cur_end),
            "days": days,
            "mode": "rolling",
            "through": through,
            "prior_start": prior_start.isoformat(),
            "prior_end": prior_end.isoformat(),
            "prior_label": format_label(prior_start, prior_end),
            "month_start": month_start.isoformat(),
            "month_end": month_end.isoformat(),
            "month_label": format_label(month_start, month_end),
        },
        "kpis": kpis,
        "channels": channels,
        "daily_orders": cur_s.get("daily_orders", {}),
        "daily_revenue": cur_s.get("daily_revenue", {}),
        "daily_revenue_net": cur_s.get("daily_revenue_net", {}),
        "prior_period": {
            "window": {
                "start": prior_start.isoformat(),
                "end": prior_end.isoformat(),
                "label": format_label(prior_start, prior_end),
                "days": days,
            },
            "kpis": {
                "paid_orders": prior_orders,
                "paid_revenue": round(prior_revenue, 2),
                "paid_revenue_net": round(prior_revenue_net, 2),
                "total_ad_spend": round(prior_spend),
                "blended_mer": round(prior_revenue / prior_spend, 2) if prior_spend else 0,
                "blended_mer_net": round(prior_revenue_net / prior_spend, 2) if prior_spend else 0,
            },
        },
        "prior_month": {
            "window": {
                "start": month_start.isoformat(),
                "end": month_end.isoformat(),
                "label": format_label(month_start, month_end),
                "days": days,
            },
            "kpis": {
                "paid_orders": month_orders,
                "paid_revenue": round(month_revenue, 2),
                "paid_revenue_net": round(month_revenue_net, 2),
                "total_ad_spend": round(month_spend),
                "blended_mer": round(month_revenue / month_spend, 2) if month_spend else 0,
                "blended_mer_net": round(month_revenue_net / month_spend, 2) if month_spend else 0,
            },
        },
    }


def apply_snapshot(payload: dict, snap: dict) -> None:
    payload["window"] = snap["window"]
    payload["kpis"] = snap["kpis"]
    payload["channels"] = snap["channels"]
    payload["daily_orders"] = snap["daily_orders"]
    payload["daily_revenue"] = snap.get("daily_revenue", {})
    payload["daily_revenue_net"] = snap.get("daily_revenue_net", {})
    payload["prior_period"] = snap["prior_period"]
    payload["prior_month"] = snap["prior_month"]

    kpi_rows = payload.get("kpi_vs_target", [])
    k = snap["kpis"]
    ch = snap["channels"]
    prior_k = snap["prior_period"]["kpis"]
    kpi_map = {
        "Paid orders": (k["paid_orders"], prior_k["paid_orders"]),
        "Shopify revenue": (k["paid_revenue"], prior_k["paid_revenue"]),
        "Blended MER": (k["blended_mer"], prior_k["blended_mer"]),
        "Meta Shopify ROAS": (ch[0]["shopify_roas"] if ch else 0, ch[0].get("shopify_roas_last", 0) if ch else 0),
        "Google Shopify ROAS": (ch[1]["shopify_roas"] if len(ch) > 1 else 0, ch[1].get("shopify_roas_last", 0) if len(ch) > 1 else 0),
        "Pinterest Shopify orders": (ch[2]["shopify_orders"] if len(ch) > 2 else 0, ch[2].get("shopify_orders_last", 0) if len(ch) > 2 else 0),
    }
    for row in kpi_rows:
        key = row.get("kpi")
        if key not in kpi_map:
            continue
        this_v, last_v = kpi_map[key]
        unit = row.get("unit", "")
        if unit == "$":
            row["this_period"] = round(this_v)
            row["last_period"] = round(last_v)
        elif unit == "×":
            row["this_period"] = round(this_v, 2)
            row["last_period"] = round(last_v, 2)
        else:
            row["this_period"] = round(this_v)
            row["last_period"] = round(last_v)


def main() -> None:
    cfg = load_json(CONFIG) if CONFIG.exists() else {}
    through = cfg.get("window_ends", "today")
    default_days = int(cfg.get("report_window_days", 7))

    payload = load_json(LATEST) if LATEST.exists() else {}
    channels_base = payload.get("channels", [])

    windows: dict[str, dict] = {}
    for days in WINDOW_SIZES:
        windows[str(days)] = build_snapshot(days, through, channels_base)

    default_snap = windows[str(default_days)]
    apply_snapshot(payload, default_snap)
    payload["windows"] = windows
    payload["generated_at"] = datetime.now(TZ).isoformat()

    history = load_json(HISTORY) if HISTORY.exists() else []
    k = default_snap["kpis"]
    w = default_snap["window"]
    ch = default_snap["channels"]
    entry = {
        "label": w["label"],
        "start": w["start"],
        "end": w["end"],
        "days": w["days"],
        "orders": k["paid_orders"],
        "revenue": round(k["paid_revenue"]),
        "revenue_net": round(k.get("paid_revenue_net", k["paid_revenue"])),
        "ad_spend": k["total_ad_spend"],
        "mer": k["blended_mer"],
        "channels": {
            "Meta": {"orders": ch[0]["shopify_orders"], "revenue": ch[0]["shopify_revenue"]} if ch else {},
            "Google": {"orders": ch[1]["shopify_orders"], "revenue": ch[1]["shopify_revenue"]} if len(ch) > 1 else {},
            "Pinterest": {"orders": ch[2]["shopify_orders"], "revenue": ch[2]["shopify_revenue"]} if len(ch) > 2 else {},
        },
    }
    if history and history[-1].get("label") == entry["label"]:
        history[-1] = entry
    else:
        history.append(entry)
        history = history[-12:]
    save_json(HISTORY, history)
    payload["history_weekly"] = history

    save_json(LATEST, payload)
    print(
        f"Windows 7/14/30 refreshed · default {w['label']} · "
        f"{k['paid_orders']} orders · ${k['paid_revenue']:,.0f} gross · ${k.get('paid_revenue_net', k['paid_revenue']):,.0f} net"
    )


if __name__ == "__main__":
    main()
