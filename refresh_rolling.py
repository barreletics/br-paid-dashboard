#!/usr/bin/env python3
"""Refresh dashboard/data/latest.json for rolling windows (7 / 14 / 30 days).

Pulls Shopify + ad spend (Meta Graph API; Google/Pinterest via API or cache)
for the same date window. Regenerates KPI rows and executive summary numbers.

Usage:
  export SHOPIFY_DOMAIN SHOPIFY_CLIENT_ID SHOPIFY_CLIENT_SECRET
  export META_ACCESS_TOKEN
  python3 refresh_rolling.py
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Detroit")
ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
DATA = ROOT / "data"
AD_SPEND_DIR = DATA / "ad_spend"
LATEST = DATA / "latest.json"
HISTORY = DATA / "history.json"
CONFIG = ROOT / "config.json"
WINDOW_SIZES = (7, 14, 30)
CHANNEL_NAMES = ["Meta", "Google PMax", "Pinterest"]
CHANNEL_KEYS = ["Meta", "Google", "Pinterest"]


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


def cache_path(start: date, end: date) -> Path:
    AD_SPEND_DIR.mkdir(parents=True, exist_ok=True)
    return AD_SPEND_DIR / f"{start.isoformat()}_{end.isoformat()}.json"


def ad_spend_for_range(start: date, end: date, cfg: dict | None = None) -> dict[str, dict]:
    """Live Meta + Google/Pinterest for exact date range; fill gaps from cache or daily rates."""
    path = cache_path(start, end)
    cached: dict[str, dict] = {}
    if path.exists():
        cached = json.loads(path.read_text()).get("channels") or {}

    channels: dict[str, dict] = {}
    cmd = [
        sys.executable,
        str(SCRIPTS / "ad_spend_pull.py"),
        "--start",
        start.isoformat(),
        "--end",
        end.isoformat(),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        channels = json.loads(proc.stdout).get("channels") or {}

    gp = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "google_pinterest_pull.py"),
            "--start",
            start.isoformat(),
            "--end",
            end.isoformat(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if gp.returncode == 0 and gp.stdout.strip():
        extra = json.loads(gp.stdout)
        if extra.get("Google"):
            g = extra["Google"]
            spend = float(g.get("spend") or 0)
            val = float(g.get("conversion_value") or 0)
            conv = float(g.get("conversions") or 0)
            channels["Google"] = {
                "spend": round(spend, 2),
                "platform_purchases": round(conv),
                "platform_revenue": round(val, 2),
                "platform_roas": roas(val, spend),
            }
        if extra.get("Pinterest"):
            p = extra["Pinterest"]
            spend = float(p.get("spend") or 0)
            val = float(p.get("checkout_value") or 0)
            chk = int(float(p.get("checkouts") or 0))
            channels["Pinterest"] = {
                "spend": round(spend, 2),
                "platform_purchases": chk,
                "platform_revenue": round(val, 2),
                "platform_roas": roas(val, spend),
            }

    if "Google" not in channels:
        ga = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "google_spend_ga4.py"),
                "--start",
                start.isoformat(),
                "--end",
                end.isoformat(),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if ga.returncode == 0 and ga.stdout.strip():
            g = json.loads(ga.stdout)
            spend = float(g.get("spend") or 0)
            val = float(g.get("conversion_value") or 0)
            conv = float(g.get("conversions") or 0)
            if spend:
                channels["Google"] = {
                    "spend": round(spend, 2),
                    "platform_purchases": round(conv),
                    "platform_revenue": round(val, 2),
                    "platform_roas": roas(val, spend),
                }

    for key in ("Meta", "Google", "Pinterest"):
        if key not in channels and key in cached:
            channels[key] = cached[key]

    rates = (cfg or {}).get("ad_daily_spend") or {}
    days = (end - start).days + 1
    for key, daily in rates.items():
        if key in channels and float(channels[key].get("spend") or 0) > 0:
            continue
        if daily:
            spend = round(float(daily) * days, 2)
            channels[key] = {
                "spend": spend,
                "platform_purchases": cached.get(key, {}).get("platform_purchases", 0),
                "platform_revenue": cached.get(key, {}).get("platform_revenue", 0),
                "platform_roas": cached.get(key, {}).get("platform_roas", 0),
            }

    if channels:
        path.write_text(json.dumps({"channels": channels}, indent=2) + "\n")

    return channels


def channel_status(name: str, shopify_orders: int, shopify_roas: float, spend: float, targets: dict) -> str:
    if name.startswith("Meta"):
        if shopify_roas >= targets.get("meta_shopify_roas_target", 1.5):
            return "good"
        return "watch" if shopify_roas >= 1.0 else "pull_back"
    if name.startswith("Google"):
        return "good" if shopify_roas >= targets.get("google_shopify_roas_target", 3.0) else "watch"
    if shopify_orders == 0 and spend >= targets.get("pinterest_max_spend_no_orders", 150):
        return "pull_back"
    if shopify_orders == 0:
        return "watch"
    return "good" if shopify_roas >= 1.0 else "watch"


def build_channels(
    cur_s: dict,
    prior_s: dict,
    month_s: dict,
    cur_ad: dict[str, dict],
    prior_ad: dict[str, dict],
    month_ad: dict[str, dict],
    channels_base: list[dict],
    targets: dict,
) -> list[dict]:
    rows = []
    for i, (name, key) in enumerate(zip(CHANNEL_NAMES, CHANNEL_KEYS)):
        base = channels_base[i] if i < len(channels_base) else {}
        o, r, rn = channel_metrics(cur_s, key)
        ol, rl, rnl = channel_metrics(prior_s, key)
        om, rm, rnm = channel_metrics(month_s, key)

        ca, pa, ma = cur_ad.get(key, {}), prior_ad.get(key, {}), month_ad.get(key, {})
        spend = float(ca.get("spend") or 0)
        spend_last = float(pa.get("spend") or 0)
        spend_month = float(ma.get("spend") or spend)

        shopify_roas = roas(r, spend)
        status = channel_status(name, o, shopify_roas, spend, targets)
        rows.append(
            {
                **copy.deepcopy(base),
                "name": name,
                "spend": round(spend),
                "spend_last": round(spend_last),
                "spend_month": round(spend_month),
                "shopify_orders": o,
                "shopify_orders_last": ol,
                "shopify_orders_month": om,
                "shopify_revenue": round(r),
                "shopify_revenue_last": round(rl),
                "shopify_revenue_month": round(rm),
                "shopify_revenue_net": round(rn),
                "shopify_revenue_net_last": round(rnl),
                "shopify_revenue_net_month": round(rnm),
                "shopify_roas": shopify_roas,
                "shopify_roas_last": roas(rl, spend_last),
                "shopify_roas_month": roas(rm, spend_month),
                "shopify_roas_net": roas(rn, spend),
                "shopify_cpa": round(spend / o, 2) if o else None,
        "platform_purchases": ca.get("platform_purchases", base.get("platform_purchases")),
        "platform_revenue": ca.get("platform_revenue", base.get("platform_revenue")),
        "platform_roas": ca.get("platform_roas", base.get("platform_roas")),
        "status": status,
        "interpretation": "",
    }
        )
    return rows


def meta_top_ads(start: date, end: date) -> list[dict]:
    import os

    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    acct = os.environ.get("META_AD_ACCOUNT_ID", "act_10152741884925238")
    if not token:
        return []
    if not acct.startswith("act_"):
        acct = f"act_{acct}"
    params = urllib.parse.urlencode(
        {
            "fields": "ad_name,campaign_name,spend,actions,action_values",
            "level": "ad",
            "sort": "spend_descending",
            "limit": "5",
            "time_range": json.dumps({"since": start.isoformat(), "until": end.isoformat()}),
            "access_token": token,
        }
    )
    url = f"https://graph.facebook.com/v21.0/{acct}/insights?{params}"
    raw = ""
    try:
        proc = subprocess.run(["curl", "-sS", url], capture_output=True, text=True, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            raw = proc.stdout
    except Exception:
        pass
    if not raw:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
    try:
        data = json.loads(raw)
    except Exception:
        return []
    ads = []
    for row in data.get("data") or []:
        spend = float(row.get("spend") or 0)
        purch = 0
        for a in row.get("actions") or []:
            if a.get("action_type") in ("purchase", "omni_purchase"):
                purch = max(purch, int(float(a.get("value") or 0)))
        purch_val = 0.0
        for a in row.get("action_values") or []:
            if a.get("action_type") in ("purchase", "omni_purchase"):
                purch_val = max(purch_val, float(a.get("value") or 0))
        ads.append(
            {
                "campaign": row.get("campaign_name") or "—",
                "ad": row.get("ad_name") or "—",
                "spend": round(spend),
                "purch": purch,
                "roas": roas(purch_val, spend),
            }
        )
    return ads


def regenerate_executive_summary(snap: dict, payload: dict) -> None:
    k = snap["kpis"]
    ch = snap["channels"]
    w = snap["window"]
    prior = snap["prior_period"]["kpis"]
    meta = ch[0] if ch else {}
    goog = ch[1] if len(ch) > 1 else {}
    pin = ch[2] if len(ch) > 2 else {}
    rev_chg = (
        round((k["paid_revenue"] - prior["paid_revenue"]) / prior["paid_revenue"] * 100)
        if prior.get("paid_revenue")
        else 0
    )
    ord_chg = (
        round((k["paid_orders"] - prior["paid_orders"]) / prior["paid_orders"] * 100)
        if prior.get("paid_orders")
        else 0
    )
    payload["executive_summary"] = [
        f"Store {k['paid_orders']} orders · {fmt_money(k['paid_revenue'])} ({rev_chg:+d}% revenue vs {w['prior_label']}).",
        f"Google PMax · Shopify ROAS {goog.get('shopify_roas', 0)}× on {fmt_money(goog.get('shopify_revenue', 0))} — best paid channel.",
        f"Meta · {meta.get('shopify_orders', 0)} Shopify UTM orders · ROAS {meta.get('shopify_roas', 0)}× (platform claims {meta.get('platform_purchases', '—')} purch).",
        f"Pinterest · {pin.get('shopify_orders', 0)} Shopify orders · {fmt_money(pin.get('spend', 0))} spend.",
        "Hold Meta budget until Shopify UTM ROAS ≥1.5× for 7 days · keep Google running.",
    ]


def regenerate_strategy_todos(snap: dict, payload: dict, cfg: dict) -> None:
    """Replace stale manual todos on every refresh — numbers drive the list."""
    ch = snap["channels"]
    meta = ch[0] if ch else {}
    goog = ch[1] if len(ch) > 1 else {}
    pin = ch[2] if len(ch) > 2 else {}
    director = cfg.get("director", "Andrew")
    agency = cfg.get("agency_owner", "Zaki")
    todos: list[dict] = []
    n = 1

    if float(meta.get("shopify_roas") or 0) < 1.5:
        todos.append(
            {
                "id": n,
                "status": "watch",
                "action": "Hold Meta budget — no raises until Shopify UTM ROAS ≥1.5× for 7 days",
                "owner": agency,
                "due": "Ongoing",
            }
        )
        n += 1

    if float(goog.get("shopify_roas") or 0) >= 3:
        todos.append(
            {
                "id": n,
                "status": "open",
                "action": f"Keep Google PMax running — {goog.get('shopify_roas', 0)}× Shopify ROAS",
                "owner": agency,
                "due": "Ongoing",
            }
        )
        n += 1

    pin_orders = int(pin.get("shopify_orders") or 0)
    pin_spend = float(pin.get("spend") or 0)
    if pin_orders == 0 and pin_spend >= 150:
        todos.append(
            {
                "id": n,
                "status": "watch",
                "action": f"Pinterest — {fmt_money(pin_spend)} spend, 0 Shopify orders; keep Checkout off",
                "owner": agency,
                "due": "Ongoing",
            }
        )
    elif pin_orders > 0:
        todos.append(
            {
                "id": n,
                "status": "open",
                "action": f"Pinterest — {pin_orders} Shopify orders; read Creative Test and scale or cut",
                "owner": agency,
                "due": "This week",
            }
        )
    else:
        todos.append(
            {
                "id": n,
                "status": "watch",
                "action": "Pinterest — low volume; watch spend vs Shopify orders",
                "owner": agency,
                "due": "Weekly",
            }
        )
    n += 1

    plat = int(meta.get("platform_purchases") or 0)
    shop = int(meta.get("shopify_orders") or 0)
    if plat > shop + 2:
        todos.append(
            {
                "id": n,
                "status": "watch",
                "action": f"Meta attribution gap — platform {plat} purch vs {shop} Shopify UTM orders",
                "owner": director,
                "due": "Weekly",
            }
        )

    payload["strategy_todos"] = todos
    payload["next_week_priorities"] = [
        {
            "priority": i + 1,
            "action": t["action"],
            "owner": t["owner"],
            "due": t["due"],
            "expected": "",
        }
        for i, t in enumerate(todos[:3])
    ]


def fmt_money(n: float) -> str:
    return f"${int(round(n)):,}"


def scale_spend(base_spend: float, days: int, base_days: int = 7) -> float:
    if not base_spend:
        return 0.0
    return round(base_spend * days / base_days)


def build_snapshot(days: int, through: str, channels_base: list[dict], targets: dict, cfg: dict) -> dict:
    cur_start, cur_end = window_for(days, through)
    prior_end = cur_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=days - 1)
    month_end = cur_start - timedelta(days=28)
    month_start = month_end - timedelta(days=days - 1)

    cur = shopify_pull(cur_start, cur_end)
    prior = shopify_pull(prior_start, prior_end)
    month = shopify_pull(month_start, month_end)

    cur_ad = ad_spend_for_range(cur_start, cur_end, cfg)
    prior_ad = ad_spend_for_range(prior_start, prior_end, cfg)
    month_ad = ad_spend_for_range(month_start, month_end, cfg)

    cur_s, prior_s, month_s = cur["summary"], prior["summary"], month["summary"]
    channels = build_channels(cur_s, prior_s, month_s, cur_ad, prior_ad, month_ad, channels_base, targets)
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
        "spend_estimated": False,
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
        if key == "Pinterest Shopify orders":
            t, target = row.get("this_period", 0), row.get("target", 0)
            row["status"] = "good" if t >= target else ("watch" if t > 0 else "bad")


def main() -> None:
    cfg = load_json(CONFIG) if CONFIG.exists() else {}
    through = cfg.get("window_ends", "today")
    default_days = int(cfg.get("report_window_days", 7))
    targets = {
        "meta_shopify_roas_target": 1.5,
        "google_shopify_roas_target": 3.0,
        "pinterest_max_spend_no_orders": 150,
    }

    payload = load_json(LATEST) if LATEST.exists() else {}
    channels_base = payload.get("channels", [])

    windows: dict[str, dict] = {}
    for days in WINDOW_SIZES:
        windows[str(days)] = build_snapshot(days, through, channels_base, targets, cfg)

    default_snap = windows[str(default_days)]
    apply_snapshot(payload, default_snap)
    regenerate_executive_summary(default_snap, payload)
    regenerate_strategy_todos(default_snap, payload, cfg)

    w = default_snap["window"]
    cur_start = date.fromisoformat(w["start"])
    cur_end = date.fromisoformat(w["end"])
    top_ads = meta_top_ads(cur_start, cur_end)
    if top_ads:
        payload["meta_top_ads"] = top_ads

    has_google = bool(default_snap["channels"][1].get("spend")) if len(default_snap["channels"]) > 1 else False
    has_pin = bool(default_snap["channels"][2].get("spend")) if len(default_snap["channels"]) > 2 else False
    dq = payload.setdefault("data_quality", {})
    dq["shopify"] = {"status": "ok", "source": "shopify_rest", "note": "Paid orders + UTM attribution"}
    dq["blend_meta"] = {"status": "ok", "source": "meta_graph_api", "note": f"Spend for {w['label']}"}
    dq["blend_google"] = {
        "status": "ok" if has_google else "partial",
        "source": "ga4_ad_cost",
        "note": f"Live Google spend for {w['label']}" if has_google else "Google spend not returned — check GA secret",
    }
    dq["blend_pinterest"] = {
        "status": "ok" if has_pin else "partial",
        "source": "pinterest_api",
        "note": f"Spend for {w['label']}" if has_pin else "Pinterest spend not returned — check credentials",
    }

    payload["windows"] = windows
    payload["generated_at"] = datetime.now(TZ).isoformat()
    payload["ad_spend_window"] = {"start": w["start"], "end": w["end"], "label": w["label"]}

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
