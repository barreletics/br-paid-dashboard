#!/usr/bin/env python3
"""Refresh dashboard/data/latest.json for the rolling report window.

Pulls Shopify for current + prior periods, updates window labels and Shopify-ground-truth
metrics. Narrative sections (executive summary, anomalies, etc.) are preserved until the
weekly agent pass rewrites them.

Usage:
  export SHOPIFY_DOMAIN SHOPIFY_CLIENT_ID SHOPIFY_CLIENT_SECRET
  python3 refresh_rolling.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Detroit")
ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT.parent / "scripts"
LATEST = ROOT / "data" / "latest.json"
HISTORY = ROOT / "data" / "history.json"
CONFIG = ROOT / "config.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def today_detroit() -> date:
    return datetime.now(TZ).date()


def window_for(days: int, through: str) -> tuple[date, date]:
    end = today_detroit()
    if through == "yesterday":
        end -= timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start, end


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


def channel_metrics(summary: dict, name: str) -> tuple[int, float]:
    ch = summary.get("channels", {}).get(name, {})
    return int(ch.get("orders", 0)), float(ch.get("revenue", 0))


def roas(revenue: float, spend: float) -> float:
    return round(revenue / spend, 2) if spend else 0.0


def main() -> None:
    cfg = load_json(CONFIG) if CONFIG.exists() else {}
    days = int(cfg.get("report_window_days", 7))
    through = cfg.get("window_ends", "today")

    cur_start, cur_end = window_for(days, through)
    prior_end = cur_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=days - 1)

    cur = shopify_pull(cur_start, cur_end)
    prior = shopify_pull(prior_start, prior_end)

    cur_s = cur["summary"]
    prior_s = prior["summary"]

    payload = load_json(LATEST) if LATEST.exists() else {}
    channels = payload.get("channels", [])

    meta_o, meta_r = channel_metrics(cur_s, "Meta")
    meta_o_last, meta_r_last = channel_metrics(prior_s, "Meta")
    goog_o, goog_r = channel_metrics(cur_s, "Google")
    goog_o_last, goog_r_last = channel_metrics(prior_s, "Google")
    pin_o, pin_r = channel_metrics(cur_s, "Pinterest")
    pin_o_last, pin_r_last = channel_metrics(prior_s, "Pinterest")

    for ch in channels:
        name = ch.get("name", "")
        spend = float(ch.get("spend") or 0)
        spend_last = float(ch.get("spend_last") or 0)
        if name == "Meta":
            ch.update(
                shopify_orders=meta_o,
                shopify_orders_last=meta_o_last,
                shopify_revenue=round(meta_r),
                shopify_revenue_last=round(meta_r_last),
                shopify_roas=roas(meta_r, spend),
                shopify_roas_last=roas(meta_r_last, spend_last),
                shopify_cpa=round(spend / meta_o, 2) if meta_o else None,
            )
        elif name.startswith("Google"):
            ch.update(
                shopify_orders=goog_o,
                shopify_orders_last=goog_o_last,
                shopify_revenue=round(goog_r),
                shopify_revenue_last=round(goog_r_last),
                shopify_roas=roas(goog_r, spend),
                shopify_roas_last=roas(goog_r_last, spend_last),
                shopify_cpa=round(spend / goog_o, 2) if goog_o else None,
            )
        elif name == "Pinterest":
            ch.update(
                shopify_orders=pin_o,
                shopify_orders_last=pin_o_last,
                shopify_revenue=round(pin_r),
                shopify_revenue_last=round(pin_r_last),
                shopify_roas=roas(pin_r, spend),
                shopify_roas_last=roas(pin_r_last, spend_last),
                shopify_cpa=round(spend / pin_o, 2) if pin_o else None,
            )

    total_spend = sum(float(c.get("spend") or 0) for c in channels)
    paid_orders = int(cur_s.get("paid_orders", 0))
    paid_revenue = float(cur_s.get("paid_revenue", 0))

    payload["generated_at"] = datetime.now(TZ).isoformat()
    payload["window"] = {
        "start": cur_start.isoformat(),
        "end": cur_end.isoformat(),
        "label": format_label(cur_start, cur_end),
        "days": days,
        "mode": "rolling",
        "through": through,
        "prior_start": prior_start.isoformat(),
        "prior_end": prior_end.isoformat(),
        "prior_label": format_label(prior_start, prior_end),
    }
    payload["channels"] = channels
    payload["kpis"] = {
        "paid_orders": paid_orders,
        "paid_revenue": round(paid_revenue, 2),
        "total_ad_spend": round(total_spend),
        "blended_mer": round(paid_revenue / total_spend, 2) if total_spend else 0,
        "cpa_blended": round(total_spend / paid_orders, 2) if paid_orders else None,
        "aov": round(paid_revenue / paid_orders, 2) if paid_orders else None,
    }
    payload["daily_orders"] = cur_s.get("daily_orders", {})

    kpi_rows = payload.get("kpi_vs_target", [])
    kpi_map = {
        "Paid orders": (paid_orders, int(prior_s.get("paid_orders", 0)), "orders"),
        "Shopify revenue": (paid_revenue, float(prior_s.get("paid_revenue", 0)), "$"),
        "Blended MER": (
            payload["kpis"]["blended_mer"],
            round(float(prior_s.get("paid_revenue", 0)) / total_spend, 2) if total_spend else 0,
            "×",
        ),
        "Meta Shopify ROAS": (channels[0]["shopify_roas"] if channels else 0, channels[0].get("shopify_roas_last", 0) if channels else 0, "×"),
        "Google Shopify ROAS": (channels[1]["shopify_roas"] if len(channels) > 1 else 0, channels[1].get("shopify_roas_last", 0) if len(channels) > 1 else 0, "×"),
        "Pinterest Shopify orders": (pin_o, pin_o_last, "orders"),
    }
    for row in kpi_rows:
        key = row.get("kpi")
        if key in kpi_map:
            this_v, last_v, _unit = kpi_map[key]
            row["this_period"] = round(this_v, 2) if isinstance(this_v, float) and _unit == "×" else round(this_v) if _unit != "$" else round(this_v)
            if _unit == "$":
                row["this_period"] = round(this_v)
                row["last_period"] = round(last_v)
            else:
                row["last_period"] = round(last_v, 2) if _unit == "×" else round(last_v)

    prior_orders = int(prior_s.get("paid_orders", 0))
    prior_revenue = float(prior_s.get("paid_revenue", 0))
    prior_spend = sum(float(c.get("spend_last") or 0) for c in channels) or total_spend

    payload["prior_period"] = {
        "window": {
            "start": prior_start.isoformat(),
            "end": prior_end.isoformat(),
            "label": format_label(prior_start, prior_end),
            "days": days,
        },
        "kpis": {
            "paid_orders": prior_orders,
            "paid_revenue": round(prior_revenue, 2),
            "total_ad_spend": round(prior_spend),
            "blended_mer": round(prior_revenue / prior_spend, 2) if prior_spend else 0,
            "aov": round(prior_revenue / prior_orders, 2) if prior_orders else None,
            "cpa_blended": round(prior_spend / prior_orders, 2) if prior_orders else None,
        },
        "channels": {
            "Meta": {
                "shopify_orders": meta_o_last,
                "shopify_revenue": round(meta_r_last),
                "shopify_roas": roas(meta_r_last, float(channels[0].get("spend_last") or 0) if channels else 0),
            },
            "Google": {
                "shopify_orders": goog_o_last,
                "shopify_revenue": round(goog_r_last),
                "shopify_roas": roas(goog_r_last, float(channels[1].get("spend_last") or 0) if len(channels) > 1 else 0),
            },
            "Pinterest": {
                "shopify_orders": pin_o_last,
                "shopify_revenue": round(pin_r_last),
                "shopify_roas": roas(pin_r_last, float(channels[2].get("spend_last") or 0) if len(channels) > 2 else 0),
            },
        },
    }

    history = load_json(HISTORY) if HISTORY.exists() else []
    entry = {
        "label": payload["window"]["label"],
        "start": cur_start.isoformat(),
        "end": cur_end.isoformat(),
        "orders": paid_orders,
        "revenue": round(paid_revenue),
        "ad_spend": round(total_spend),
        "mer": payload["kpis"]["blended_mer"],
        "channels": {
            "Meta": {"orders": meta_o, "revenue": round(meta_r), "roas": roas(meta_r, float(channels[0].get("spend") or 0) if channels else 0)},
            "Google": {"orders": goog_o, "revenue": round(goog_r), "roas": roas(goog_r, float(channels[1].get("spend") or 0) if len(channels) > 1 else 0)},
            "Pinterest": {"orders": pin_o, "revenue": round(pin_r), "roas": roas(pin_r, float(channels[2].get("spend") or 0) if len(channels) > 2 else 0)},
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
    print(f"Rolling window {payload['window']['label']} · {paid_orders} orders · ${paid_revenue:,.0f}")


if __name__ == "__main__":
    main()
