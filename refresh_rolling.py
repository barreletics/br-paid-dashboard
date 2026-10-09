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
import os
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
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from pull_common import is_config_estimate, looks_like_live_channel  # noqa: E402

DATA = ROOT / "data"
AD_SPEND_DIR = DATA / "ad_spend"
LATEST = DATA / "latest.json"
HISTORY = DATA / "history.json"
REFRESH_STATUS = DATA / "refresh_status.json"
CONFIG = ROOT / "config.json"
WINDOW_SIZES = (7, 14, 30)
CHANNEL_NAMES = ["Meta", "Google PMax", "Pinterest"]
CHANNEL_KEYS = ["Meta", "Google", "Pinterest"]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _pinterest_not_connected(err: str | None) -> bool:
    if not os.environ.get("PINTEREST_ACCESS_TOKEN", "").strip():
        return True
    if not err:
        return False
    low = err.lower()
    return "authentication failed" in low or "not set" in low or "pending" in low


def _pinterest_blend_dates(manual: dict) -> tuple[date | None, date | None]:
    if manual.get("spend_start") and manual.get("spend_end"):
        try:
            return (
                date.fromisoformat(str(manual["spend_start"])),
                date.fromisoformat(str(manual["spend_end"])),
            )
        except ValueError:
            pass
    raw = (manual.get("blend_window") or "").replace("–", "-")
    if " to " in raw:
        a, b = raw.split(" to ", 1)
        try:
            return date.fromisoformat(a.strip()), date.fromisoformat(b.strip())
        except ValueError:
            pass
    return None, None


def pinterest_manual_spend_for_range(start: date, end: date, manual: dict) -> tuple[float, bool]:
    """Prorate Blend total to overlap with spend_start..spend_end. partial=True if window ≠ spend coverage."""
    total = float(manual.get("spend") or 0)
    bs, be = _pinterest_blend_dates(manual)
    if not bs or not be or total <= 0:
        return 0.0, False
    overlap_start = max(start, bs)
    overlap_end = min(end, be)
    if overlap_start > overlap_end:
        return 0.0, False
    blend_days = (be - bs).days + 1
    overlap_days = (overlap_end - overlap_start).days + 1
    spend = round(total * overlap_days / blend_days, 2)
    window_covers_blend = start <= bs and end >= be
    partial = not window_covers_blend or overlap_days < (end - start).days + 1
    return spend, partial


def _pinterest_manual_channel(
    start: date, end: date, cfg: dict | None, err: str | None
) -> tuple[dict[str, dict] | None, dict[str, dict] | None]:
    if not _pinterest_not_connected(err):
        return None, None
    manual = (cfg or {}).get("pinterest_blend_manual") or {}
    spend, partial = pinterest_manual_spend_for_range(start, end, manual)
    if spend <= 0:
        return None, None
    note = manual.get("note") or "manual, via Blend"
    bs, be = _pinterest_blend_dates(manual)
    window_note = f"{note} · spend covers {bs.isoformat() if bs else '?'}–{be.isoformat() if be else '?'}"
    ch = {
        "spend": spend,
        "platform_purchases": 0,
        "platform_revenue": 0,
        "platform_roas": 0,
        "spend_source": "manual",
        "spend_partial_coverage": partial,
    }
    info = {
        "status": "ok",
        "source": "blend_manual",
        "window_end": end.isoformat(),
        "note": window_note,
    }
    return ch, info


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


def _channel_from_google_payload(g: dict) -> dict:
    spend = float(g.get("spend") or 0)
    val = float(g.get("conversion_value") or 0)
    conv = float(g.get("conversions") or 0)
    return {
        "spend": round(spend, 2),
        "platform_purchases": round(conv),
        "platform_revenue": round(val, 2),
        "platform_roas": roas(val, spend),
        "spend_source": "live",
    }


def _channel_from_pinterest_payload(p: dict) -> dict:
    spend = float(p.get("spend") or 0)
    val = float(p.get("checkout_value") or 0)
    chk = int(float(p.get("checkouts") or 0))
    return {
        "spend": round(spend, 2),
        "platform_purchases": chk,
        "platform_revenue": round(val, 2),
        "platform_roas": roas(val, spend),
        "spend_source": "live",
    }


def last_successful_live_end(source_key: str, cfg: dict | None = None) -> str | None:
    """Latest window end date with a non-estimate live pull for source_key."""
    rates = (cfg or {}).get("ad_daily_spend") or {}
    best: date | None = None
    if not AD_SPEND_DIR.is_dir():
        return None
    for path in AD_SPEND_DIR.glob("*.json"):
        parts = path.stem.split("_")
        if len(parts) != 2:
            continue
        try:
            w_start = date.fromisoformat(parts[0])
            w_end = date.fromisoformat(parts[1])
        except ValueError:
            continue
        days = (w_end - w_start).days + 1
        blob = json.loads(path.read_text())
        ch = (blob.get("channels") or {}).get(source_key) or {}
        prov = (blob.get("provenance") or {}).get(source_key) or {}
        if prov.get("status") == "estimate":
            continue
        if prov.get("status") == "ok" and float(ch.get("spend") or 0) > 0:
            if best is None or w_end > best:
                best = w_end
            continue
        daily = rates.get(source_key)
        if float(ch.get("spend") or 0) > 0 and looks_like_live_channel(ch, days, daily):
            if best is None or w_end > best:
                best = w_end
    return best.isoformat() if best else None


def ad_spend_for_range(
    start: date, end: date, cfg: dict | None = None
) -> tuple[dict[str, dict], dict[str, dict], dict[str, str]]:
    """Live Meta + Google/Pinterest; cache only prior live pulls; label config fallbacks as estimates."""
    path = cache_path(start, end)
    cached: dict[str, dict] = {}
    provenance: dict[str, dict] = {}
    if path.exists():
        blob = json.loads(path.read_text())
        cached = blob.get("channels") or {}
        provenance = blob.get("provenance") or {}

    rates = (cfg or {}).get("ad_daily_spend") or {}
    days = (end - start).days + 1
    channels: dict[str, dict] = {}
    source_info: dict[str, dict] = {}
    pull_errors: dict[str, str] = {}
    pulled_at = datetime.now(TZ).isoformat()

    cmd = [
        sys.executable,
        str(SCRIPTS / "ad_spend_pull.py"),
        "--start",
        start.isoformat(),
        "--end",
        end.isoformat(),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
            channels.update(payload.get("channels") or {})
            pull_errors.update(payload.get("errors") or {})
        except json.JSONDecodeError:
            pull_errors["Meta"] = "ad_spend_pull.py returned invalid JSON"
    elif proc.returncode != 0:
        pull_errors["Meta"] = proc.stderr.strip() or f"ad_spend_pull.py exited {proc.returncode}"

    if channels.get("Meta"):
        source_info["Meta"] = {
            "status": "ok",
            "source": "meta_graph_api",
            "pulled_at": pulled_at,
            "window_end": end.isoformat(),
        }

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
    if gp.stdout.strip():
        try:
            extra = json.loads(gp.stdout)
            pull_errors.update(extra.get("errors") or {})
            if extra.get("Google"):
                channels["Google"] = _channel_from_google_payload(extra["Google"])
                source_info["Google"] = {
                    "status": "ok",
                    "source": "google_ads_api",
                    "pulled_at": pulled_at,
                    "window_end": end.isoformat(),
                }
            if extra.get("Pinterest"):
                channels["Pinterest"] = _channel_from_pinterest_payload(extra["Pinterest"])
                source_info["Pinterest"] = {
                    "status": "ok",
                    "source": "pinterest_api",
                    "pulled_at": pulled_at,
                    "window_end": end.isoformat(),
                }
        except json.JSONDecodeError:
            pull_errors["Google"] = pull_errors.get("Google") or "google_pinterest_pull.py returned invalid JSON"

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
        if ga.stderr.strip():
            try:
                ga_err = json.loads(ga.stderr.strip())
                if ga_err.get("error"):
                    pull_errors["Google"] = ga_err["error"]
            except json.JSONDecodeError:
                pull_errors["Google"] = ga.stderr.strip()
        if ga.returncode == 0 and ga.stdout.strip():
            g = json.loads(ga.stdout)
            spend = float(g.get("spend") or 0)
            if spend:
                channels["Google"] = _channel_from_google_payload(g)
                source_info["Google"] = {
                    "status": "ok",
                    "source": "ga4_ad_cost",
                    "pulled_at": pulled_at,
                    "window_end": end.isoformat(),
                }

    for key in ("Meta", "Google", "Pinterest"):
        if key in channels and float(channels[key].get("spend") or 0) > 0:
            continue
        if key not in cached:
            continue
        csp = float(cached[key].get("spend") or 0)
        if csp <= 0:
            continue
        daily = rates.get(key)
        if not looks_like_live_channel(cached[key], days, daily):
            continue
        channels[key] = copy.deepcopy(cached[key])
        prov = provenance.get(key) or {}
        source_info[key] = {
            "status": "cached",
            "source": prov.get("source") or "ad_spend_cache",
            "pulled_at": prov.get("pulled_at"),
            "window_end": end.isoformat(),
            "note": f"Using cached live pull for {start.isoformat()}–{end.isoformat()}",
        }
        channels[key]["spend_source"] = "cached"

    for key, daily in rates.items():
        if key in channels and float(channels[key].get("spend") or 0) > 0:
            continue
        err = pull_errors.get(key)
        if key == "Pinterest" and _pinterest_not_connected(err):
            pin_ch, pin_info = _pinterest_manual_channel(start, end, cfg, err)
            if pin_ch and pin_info:
                channels["Pinterest"] = pin_ch
                source_info["Pinterest"] = pin_info
            else:
                source_info["Pinterest"] = {
                    "status": "pending",
                    "source": "pinterest_api",
                    "window_end": end.isoformat(),
                    "note": "Not connected — Pinterest app pending approval (no live spend)",
                    "error": err,
                }
                channels["Pinterest"] = {
                    "spend": 0,
                    "platform_purchases": 0,
                    "platform_revenue": 0,
                    "platform_roas": 0,
                    "spend_source": "pending",
                }
            continue
        if daily:
            spend = round(float(daily) * days, 2)
            channels[key] = {
                "spend": spend,
                "platform_purchases": 0,
                "platform_revenue": 0,
                "platform_roas": 0,
                "spend_source": "estimate",
            }
            source_info[key] = {
                "status": "estimate",
                "source": "config_daily_rate",
                "window_end": end.isoformat(),
                "note": f"Estimate: config.json ${daily}/day × {days} days (not live platform data)",
                "error": err,
            }
        elif err:
            source_info[key] = {
                "status": "error",
                "source": None,
                "error": err,
                "window_end": end.isoformat(),
            }

    for key in ("Meta", "Google", "Pinterest"):
        if key in source_info:
            continue
        if pull_errors.get(key):
            source_info[key] = {
                "status": "error",
                "source": None,
                "error": pull_errors[key],
                "window_end": end.isoformat(),
            }

    pin_err = pull_errors.get("Pinterest")
    if _pinterest_not_connected(pin_err):
        pin_ch, pin_info = _pinterest_manual_channel(start, end, cfg, pin_err)
        if pin_ch and pin_info:
            channels["Pinterest"] = pin_ch
            source_info["Pinterest"] = pin_info
        else:
            source_info["Pinterest"] = {
                "status": "pending",
                "source": "pinterest_api",
                "window_end": end.isoformat(),
                "note": "No Blend spend in this date window",
                "error": pin_err,
            }
            channels["Pinterest"] = {
                "spend": 0,
                "platform_purchases": 0,
                "platform_revenue": 0,
                "platform_roas": 0,
                "spend_source": "pending",
            }

    live_write: dict[str, dict] = {}
    live_prov: dict[str, dict] = {}
    for key, ch in channels.items():
        if key == "Pinterest" and ch.get("spend_source") == "manual":
            continue
        if source_info.get(key, {}).get("status") == "ok":
            live_write[key] = ch
            live_prov[key] = source_info[key]

    if live_write:
        merged = {**cached, **live_write}
        merged_prov = {**provenance, **live_prov}
        path.write_text(
            json.dumps({"channels": merged, "provenance": merged_prov}, indent=2) + "\n"
        )

    for key, ch in channels.items():
        st = source_info.get(key, {}).get("status")
        if st == "ok":
            ch.setdefault("spend_source", "live")
        elif st == "cached":
            ch.setdefault("spend_source", "cached")
        elif st == "estimate":
            ch.setdefault("spend_source", "estimate")
        elif st == "pending":
            ch.setdefault("spend_source", "pending")

    return channels, source_info, pull_errors


def calendar_mtd_range(through: str) -> tuple[date, date]:
    end_d = today_detroit()
    if through == "yesterday":
        end_d -= timedelta(days=1)
    return end_d.replace(day=1), end_d


def build_budget_pacing(
    cfg: dict,
    mtd_start: date,
    mtd_end: date,
    mtd_channels: dict[str, dict],
    mtd_status: dict[str, dict],
) -> dict:
    plans = cfg.get("monthly_plan") or {}
    rates = cfg.get("ad_daily_spend") or {}
    month_key = f"{mtd_end.year}-{mtd_end.month:02d}"
    if mtd_end.month == 12:
        first_next = date(mtd_end.year + 1, 1, 1)
    else:
        first_next = date(mtd_end.year, mtd_end.month + 1, 1)
    days_in_month = (first_next - timedelta(days=1)).day
    mtd_days = (mtd_end - mtd_start).days + 1
    channel_rows = []
    total_mtd = 0.0
    any_estimate = False
    channel_specs = (
        ("Meta", "Meta"),
        ("Google", "Google PMax"),
        ("Pinterest", "Pinterest"),
    )
    for key, plan_name in channel_specs:
        ch = mtd_channels.get(key) or {}
        st = mtd_status.get(key) or {}
        mtd_spend = float(ch.get("spend") or 0)
        total_mtd += mtd_spend
        if st.get("status") == "estimate" or ch.get("spend_source") == "estimate":
            any_estimate = True
        plan = float(plans.get(plan_name) or 0)
        used_pct = round(mtd_spend / plan * 100) if plan else None
        pace = "on_track"
        if plan and mtd_days > 0:
            projected = mtd_spend / mtd_days * days_in_month
            if projected > plan * 1.1:
                pace = "over"
            elif projected < plan * 0.85:
                pace = "under"
        row_status = pace
        if st.get("status") == "estimate":
            row_status = "estimate"
        elif st.get("status") == "error":
            row_status = "error"
        channel_rows.append(
            {
                "channel": plan_name if key == "Google" else key,
                "mtd_spend": round(mtd_spend),
                "monthly_plan": round(plan),
                "status": row_status,
                "spend_source": ch.get("spend_source") or st.get("status"),
                "used_pct": used_pct,
            }
        )
    total_plan = float(plans.get("total") or 0) or sum(float(plans.get(n) or 0) for _, n in channel_specs)
    projected = round(total_mtd / mtd_days * days_in_month) if mtd_days else 0
    note_parts = [f"Calendar MTD {mtd_start.isoformat()}–{mtd_end.isoformat()} ({mtd_days} days)"]
    if any_estimate:
        note_parts.append(
            "includes config.json daily-rate estimates where live API pulls failed — not for audit"
        )
    else:
        note_parts.append("from live platform pulls for this calendar month")
    return {
        "month": month_key,
        "mtd_start": mtd_start.isoformat(),
        "mtd_end": mtd_end.isoformat(),
        "month_to_date_spend": round(total_mtd),
        "monthly_plan": round(total_plan),
        "projected_month_spend": projected,
        "pacing_status": "estimate" if any_estimate else ("on_track" if projected <= total_plan * 1.05 else "over"),
        "spend_includes_estimates": any_estimate,
        "note": " · ".join(note_parts),
        "by_channel": channel_rows,
    }


def regenerate_anomalies(snap: dict) -> list[dict]:
    """Replace stale hand-written anomalies with rolling-window deltas."""

    def pct(cur: float, prev: float) -> int | None:
        if not prev:
            return None
        return round((cur - prev) / prev * 100)

    k = snap.get("kpis") or {}
    prior_k = (snap.get("prior_period") or {}).get("kpis") or {}
    w = snap.get("window") or {}
    prior_label = w.get("prior_label") or "prior period"
    out: list[dict] = []

    ord_pct = pct(float(k.get("paid_orders") or 0), float(prior_k.get("paid_orders") or 0))
    if ord_pct is not None and abs(ord_pct) >= 12:
        out.append(
            {
                "metric": "Store orders",
                "change": f"{ord_pct:+d}%",
                "vs": prior_label,
                "cause": "Rolling store order volume vs the same-length prior window.",
                "response": "Confirm paid vs organic; check Meta/Google UTMs before changing budgets.",
            }
        )

    rev_pct = pct(float(k.get("paid_revenue") or 0), float(prior_k.get("paid_revenue") or 0))
    if rev_pct is not None and abs(rev_pct) >= 12:
        out.append(
            {
                "metric": "Store revenue",
                "change": f"{rev_pct:+d}%",
                "vs": prior_label,
                "cause": "Rolling DTC revenue vs prior window (Shopify REST).",
                "response": "Use channel compare for attribution; do not react to a single metric alone.",
            }
        )

    for c in snap.get("channels") or []:
        name = c.get("name") or "Channel"
        roas_now = float(c.get("shopify_roas") or 0)
        roas_last = float(c.get("shopify_roas_last") or 0)
        roas_pct = pct(roas_now, roas_last)
        if roas_pct is not None and abs(roas_pct) >= 15:
            out.append(
                {
                    "metric": f"{name} Shopify ROAS",
                    "change": f"{roas_pct:+d}%",
                    "vs": prior_label,
                    "cause": "UTM-attributed Shopify revenue ÷ spend for this window.",
                    "response": "Judge on Shopify UTM ROAS; verify spend is live not estimated.",
                }
            )
        if c.get("spend_source") == "estimate":
            out.append(
                {
                    "metric": f"{name} ad spend",
                    "change": "estimate",
                    "vs": w.get("label") or "this window",
                    "cause": "Live platform pull failed; spend is config.json daily rate × days.",
                    "response": "Renew API tokens in GitHub Actions — do not use for budget decisions.",
                }
            )
            break

    return out[:5]


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
        pin_partial = key == "Pinterest" and ca.get("spend_partial_coverage")
        pin_manual = key == "Pinterest" and ca.get("spend_source") == "manual"
        roas_last_val = roas(rl, spend_last) if spend_last else None
        if pin_manual and spend_last <= 0 and ol > 0:
            roas_last_val = None
        if pin_partial and spend > 0:
            shopify_roas = None
        status = channel_status(name, o, shopify_roas if shopify_roas is not None else 0, spend, targets)
        row_extra: dict[str, Any] = {}
        if pin_partial and spend > 0:
            row_extra["shopify_roas_partial"] = True
            row_extra["shopify_roas_label"] = "n/a (partial spend)"
            row_extra["shopify_roas_net"] = None
        rows.append(
            {
                **copy.deepcopy(base),
                "name": name,
                "spend": round(spend, 2),
                "spend_last": round(spend_last, 2),
                "spend_month": round(spend_month, 2),
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
                "shopify_roas_last": roas_last_val,
                "shopify_roas_month": roas(rm, spend_month),
                "shopify_roas_net": None if pin_partial else roas(rn, spend),
                **row_extra,
                "shopify_cpa": round(spend / o, 2) if o else None,
        "platform_purchases": ca.get("platform_purchases", base.get("platform_purchases")),
        "platform_revenue": ca.get("platform_revenue", base.get("platform_revenue")),
        "platform_roas": ca.get("platform_roas", base.get("platform_roas")),
        "spend_source": ca.get("spend_source", base.get("spend_source")),
        "impressions": ca.get("impressions", base.get("impressions")),
        "clicks": ca.get("clicks", base.get("clicks")),
        "ctr": ca.get("ctr", base.get("ctr")),
        "cpc": ca.get("cpc", base.get("cpc")),
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
    wh_line = ""
    if k.get("wholesale_orders"):
        wh_line = (
            f" Wholesale {k['wholesale_orders']} orders · {fmt_money(k.get('wholesale_revenue', 0))} — not in DTC totals."
        )
    payload["executive_summary"] = [
        f"DTC store {k['paid_orders']} orders · {fmt_money(k['paid_revenue'])} ({rev_chg:+d}% vs {w['prior_label']}).{wh_line}",
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


def regenerate_action_rollup(snap: dict, payload: dict, cfg: dict) -> None:
    """Top-of-dashboard rollup: domestic + intl snapshots and actionable items."""
    k = snap["kpis"]
    ch = snap["channels"]
    w = snap["window"]
    prior = snap["prior_period"]["kpis"]
    meta = ch[0] if ch else {}
    goog = ch[1] if len(ch) > 1 else {}
    pin = ch[2] if len(ch) > 2 else {}
    agency = cfg.get("agency_owner", "Zaki")
    director = cfg.get("director", "Andrew")

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
    spend_chg = (
        round((k["total_ad_spend"] - prior["total_ad_spend"]) / prior["total_ad_spend"] * 100)
        if prior.get("total_ad_spend")
        else 0
    )

    ad_rev = sum(float(c.get("shopify_revenue") or 0) for c in ch)
    ad_orders = sum(int(c.get("shopify_orders") or 0) for c in ch)
    ad_rev_prior = sum(float(c.get("shopify_revenue_last") or 0) for c in ch)
    ad_roas = round(ad_rev / k["total_ad_spend"], 2) if k.get("total_ad_spend") else 0
    ad_rev_chg = (
        round((ad_rev - ad_rev_prior) / ad_rev_prior * 100) if ad_rev_prior else 0
    )
    unattributed_rev = round(k["paid_revenue"] - ad_rev)
    unattributed_orders = k["paid_orders"] - ad_orders

    intl = payload.get("intl_performance") or {}

    items: list[dict] = []
    meta_roas = float(meta.get("shopify_roas") or 0)
    sync = payload.get("agency_intl_sync") or {}
    us_meta = sync.get("us_meta") or {}
    if us_meta.get("gate_passed"):
        items.append(
            {
                "lane": "US Meta",
                "action": (
                    f"Shopify ROAS {us_meta.get('shopify_roas')}× — gate passed. "
                    f"{us_meta.get('note') or 'Hold Prospecting $100/day until Andrew approves a raise from Creative Test trims.'}"
                ),
                "owner": agency,
            }
        )
    elif meta_roas < 1.5:
        items.append(
            {
                "lane": "US Meta",
                "action": (
                    f"Shopify ROAS {meta_roas}× — below 1.5× gate. Hold blanket raises; "
                    "Meta recommends Prospecting $100→$172 — fund from Creative Test trims only."
                ),
                "owner": agency,
            }
        )
    else:
        items.append(
            {
                "lane": "US Meta",
                "action": f"Shopify ROAS {meta_roas}× — gate passed; Zaki may propose budget moves.",
                "owner": agency,
            }
        )

    items.append(
        {
            "lane": "Google",
            "action": f"Keep PMax running — {goog.get('shopify_roas', 0)}× Shopify ROAS on {fmt_money(goog.get('spend', 0))}",
            "owner": agency,
        }
    )
    items.append(
        {
            "lane": "US Meta",
            "action": "Creative Test: keep Closed Sole + Welcome to the movement; cut ads with 0 purchases",
            "owner": agency,
        }
    )
    items.append(
        {
            "lane": "US Meta",
            "action": "Retargeting stays PAUSED — do not restore July setup",
            "owner": agency,
        }
    )
    items.append(
        {
            "lane": "US Meta",
            "action": (
                "Do NOT cut US Prospecting ($100/day). Meta recommends $100→$172 — "
                "fund from Creative Test dead ads only, not from intl."
            ),
            "owner": agency,
        }
    )

    pin_roas = float(pin.get("shopify_roas") or 0)
    pin_ord = int(pin.get("shopify_orders") or 0)
    pin_spend = float(pin.get("spend") or 0)
    if pin_roas < 1.0 and pin_ord <= 1:
        items.append(
            {
                "lane": "Pinterest",
                "action": (
                    f"Decision needed: {pin_roas}× Shopify ROAS · {pin_ord} order on "
                    f"{fmt_money(pin_spend)} — hold Shopping ~$10/day 1 more week or pause. "
                    "Zaki has no Pinterest access; Andrew/Stefanie call."
                ),
                "owner": director,
            }
        )
    else:
        items.append(
            {
                "lane": "Pinterest",
                "action": "Shopping Ads only (~$10/day) — Creative Test stays paused",
                "owner": agency,
            }
        )

    intl_test_rules = (
        "International test: about 14 days per country (default). Pause at $100 spend or day 14 "
        "with zero Shopify orders to that country — unless signals are clearly weak earlier (pause "
        "sooner) or clearly strong (keep or scale before day 14). Judge on Shopify orders only, "
        "not what Meta reports. International budget is extra only — do not cut United States spend."
    )
    if intl:
        intl["test_rules"] = intl_test_rules

    if intl.get("shopify"):
        items.append({"lane": "Intl", "action": intl_test_rules, "owner": agency})
        if sync.get("dashboard_actions"):
            for row in sync["dashboard_actions"]:
                items.append(
                    {
                        "lane": row.get("lane") or "Intl",
                        "action": row["action"],
                        "owner": row.get("owner") or agency,
                    }
                )
        else:
            items.append(
                {
                    "lane": "Intl",
                    "action": "Pause Italy intl ad set ($139 · 0 purch).",
                    "owner": agency,
                }
            )
            items.append(
                {
                    "lane": "Intl",
                    "action": "Keep AU + CA + UAE — purchases and ROAS above kill threshold.",
                    "owner": agency,
                }
            )
            items.append(
                {
                    "lane": "Intl",
                    "action": (
                        "Intl budget stays additive (~$44/day). Reallocate Italy $8/day to AU winner — "
                        "do NOT add MX / DE / SG / ES yet; only 4 of 8 countries were live."
                    ),
                    "owner": agency,
                }
            )

    plat = int(meta.get("platform_purchases") or 0)
    shop = int(meta.get("shopify_orders") or 0)
    if plat > shop + 2:
        items.append(
            {
                "lane": "Attribution",
                "action": f"Meta platform {plat} purch vs {shop} Shopify UTM — judge on Shopify only",
                "owner": director,
            }
        )

    goog_ord_delta = int(goog.get("shopify_orders") or 0) - int(goog.get("shopify_orders_last") or 0)
    if goog_ord_delta <= -3:
        items.append(
            {
                "lane": "Google",
                "action": f"Google orders down {abs(goog_ord_delta)} WoW — monitor delivery; no tROAS experiments",
                "owner": agency,
            }
        )

    mer = round(k["paid_revenue"] / k["total_ad_spend"], 2) if k.get("total_ad_spend") else 0
    mer_net = round(k["paid_revenue_net"] / k["total_ad_spend"], 2) if k.get("total_ad_spend") else 0
    rr = resolve_returns_rollup(snap, cfg)
    exchange_cost = float(rr.get("exchange_outbound_cost_modeled") or 0)
    store_net = float(k.get("paid_revenue_net") or 0)
    mer_after_exchange = (
        round((store_net - exchange_cost) / k["total_ad_spend"], 2)
        if k.get("total_ad_spend")
        else 0
    )
    ad_net = round(ad_rev - k["total_ad_spend"])
    ad_net_prior = round(ad_rev_prior - prior.get("total_ad_spend", 0))
    net_chg = (
        round((ad_net - ad_net_prior) / abs(ad_net_prior) * 100) if ad_net_prior else 0
    )
    if intl:
        intl["efficiency_actions"] = [i["action"] for i in items if i.get("lane") == "Intl"]
        prospect = next(
            (i["action"] for i in items if i.get("lane") == "US Meta" and "Prospecting" in i.get("action", "")),
            None,
        )
        if prospect:
            intl["efficiency_actions"].insert(0, prospect)

    hit_list = (payload.get("executive_summary") or [])[:4]
    zaki_confirm = sync.get("confirm_banner") if sync.get("aligned") else (
        f"{agency} — reply yes/no on each item below, or note what you'd change."
    )
    payload["action_rollup"] = {
        "title": "Action rollup",
        "period": w.get("label", ""),
        "zaki_confirm": zaki_confirm,
        "hit_list": hit_list,
        "intl_test_rules": intl_test_rules if intl else "",
        "top_line": {
            "ad_revenue": round(ad_rev),
            "ad_spend": round(k["total_ad_spend"]),
            "ad_revenue_delta_pct": ad_rev_chg,
            "ad_spend_delta_pct": spend_chg,
            "net": ad_net,
            "net_delta_pct": net_chg,
            "ad_roas": ad_roas,
            "total_roas": mer,
            "total_roas_net": mer_net,
            "total_roas_after_exchange_cost": mer_after_exchange,
            "returns": rr,
            "ad_orders": ad_orders,
            "store_revenue": round(k["paid_revenue"]),
            "store_orders": k["paid_orders"],
            "store_revenue_delta_pct": rev_chg,
            "unattributed_revenue": unattributed_rev,
            "unattributed_orders": unattributed_orders,
        },
        "headline": intl.get("headline") or (
            f"{fmt_money(ad_rev)} ad revenue on {fmt_money(k['total_ad_spend'])} spend · "
            f"{fmt_money(ad_net)} net · {mer}× total ROAS (store ÷ spend) · "
            f"total sales {fmt_money(k['paid_revenue'])} ({k['paid_orders']} orders, ads + other)"
        ),
        "items": items,
    }


def resolve_returns_rollup(snap: dict, cfg: dict) -> dict:
    """Returns/exchanges in window; full counts from shopify returns_rollup after refresh."""
    k = snap["kpis"]
    rr = k.get("returns_rollup")
    if rr:
        return rr
    rm = cfg.get("returns_model") or {}
    fee = float(rm.get("refund_return_fee_usd", 7.95))
    exch_ship = float(rm.get("exchange_outbound_ship_usd", 9))
    ue = k.get("unit_economics") or {}
    ex = snap.get("excluded") or {}
    by = ex.get("by_reason") or {}
    exchanges = int(by.get("exchange") or ue.get("returnzap_exchange_orders") or 0)
    refunds_cash = float(ue.get("refunds_recorded") or k.get("returns_adjusted") or 0)
    return {
        "refund_return_orders": None,
        "exchange_orders": exchanges,
        "refunds_cash_out": round(refunds_cash, 2),
        "return_fee_kept_est": None,
        "exchange_outbound_cost_modeled": round(exchanges * exch_ship, 2),
        "returns_cash_cost": round(refunds_cash + exchanges * exch_ship, 2),
        "policy_note": (
            f"Refund returns: ${fee:.2f} kept per return. Exchanges: $0 to customer; "
            f"~${exch_ship:.0f}/exchange outbound modeled. Run refresh for exact return counts."
        ),
        "partial": True,
    }


def build_director_economics(snap: dict, cfg: dict, rz: dict) -> dict:
    """Shopify unit economics + MER/halo/contribution model for director view."""
    k = snap["kpis"]
    ch = snap["channels"]
    ue = k.get("unit_economics") or {}
    rr = resolve_returns_rollup(snap, cfg)
    model = cfg.get("economics_model") or {}
    cogs_pct = float(model.get("cogs_pct_of_net_dtc", 0.38))
    ship_cost = float(model.get("fulfillment_cost_per_order_usd", 9.0))
    ad_spend = float(k.get("total_ad_spend") or 0)
    gross = float(k.get("paid_revenue") or 0)
    net = float(k.get("paid_revenue_net") or gross)
    orders = int(k.get("paid_orders") or 0)
    ad_rev = sum(float(c.get("shopify_revenue") or 0) for c in ch)
    ad_orders = sum(int(c.get("shopify_orders") or 0) for c in ch)
    halo_rev = gross - ad_rev
    halo_orders = max(0, orders - ad_orders)
    est_cogs = net * cogs_pct
    est_fulfillment = ship_cost * orders
    contribution = net - est_cogs - est_fulfillment - ad_spend
    cont_mer = round(contribution / ad_spend, 2) if ad_spend else 0
    shopify_roas = round(ad_rev / ad_spend, 2) if ad_spend else 0

    strategy: list[str] = []
    mer = float(k.get("blended_mer") or 0)
    if mer >= 3:
        strategy.append(f"MER {mer}× — total store supports ad spend; watch modeled contribution before scaling.")
    elif mer >= 2:
        strategy.append(f"MER {mer}× — acceptable; improve tagged Shopify ROAS or reduce waste (Pinterest / dead intl).")
    else:
        strategy.append(f"MER {mer}× — total store not covering spend enough; hold scale.")
    if halo_rev > 0 and ad_spend:
        strategy.append(
            f"Halo ~{fmt_money(halo_rev)} ({halo_orders} orders) not ad-tagged — likely organic/direct/email; MER includes this."
        )
    if shopify_roas and shopify_roas < 1.5:
        strategy.append(f"Tagged Shopify ROAS {shopify_roas}× — measurable ads below gate; do not trust Meta purchase counts alone.")
    elif shopify_roas >= 1.5:
        strategy.append(f"Tagged Shopify ROAS {shopify_roas}× — measurable ads at/above gate.")

    ad_spend_de = float(k.get("total_ad_spend") or 0)
    store_net = float(k.get("paid_revenue_net") or 0)
    exchange_cost = float(rr.get("exchange_outbound_cost_modeled") or 0)
    store_after_exchange_cost = round(store_net - exchange_cost, 2)
    mer_after_exchange_cost = (
        round(store_after_exchange_cost / ad_spend_de, 2) if ad_spend_de else 0
    )

    return {
        "window_label": snap["window"].get("label", ""),
        "returns_rollup": rr,
        "mer_gross": k.get("blended_mer"),
        "mer_net": k.get("blended_mer_net"),
        "mer_after_exchange_cost": mer_after_exchange_cost,
        "store_after_exchange_cost": store_after_exchange_cost,
        "shopify_roas_blended": shopify_roas,
        "halo_revenue": round(halo_rev, 2),
        "halo_orders": halo_orders,
        "halo_index": round(halo_rev / ad_spend, 2) if ad_spend else 0,
        "ad_spend": round(ad_spend),
        "store_revenue_gross": round(gross, 2),
        "store_revenue_net": round(net, 2),
        "refunds_and_adjustments": ue.get("returns_adjusted_on_orders") or k.get("returns_adjusted"),
        "refunds_recorded": ue.get("refunds_recorded"),
        "shipping_collected": ue.get("shipping_collected_from_customer"),
        "discounts": ue.get("discounts"),
        "returnzap_exchanges": (k.get("returns_rollup") or rr).get("exchange_orders")
        or ue.get("returnzap_exchange_orders"),
        "returnzap_status": rz.get("status", "shopify_tags_only"),
        "returnzap_note": (
            "Exchanges: Shopify tag ReturnZap Exchanged. "
            "Refund RMAs: Shopify Return in progress / refund $. ReturnZap API not wired."
        ),
        "intl_ship_to_orders": ue.get("intl_ship_to_orders"),
        "fulfilled_pct": ue.get("fulfilled_pct"),
        "modeled_cogs_pct": cogs_pct,
        "modeled_fulfillment_per_order": ship_cost,
        "estimated_contribution": round(contribution, 2),
        "contribution_mer": cont_mer,
        "model_disclaimer": model.get(
            "label",
            "Modeled contribution — not Xero truth.",
        ),
        "strategy_lines": strategy,
    }


def regenerate_director_economics(snap: dict, payload: dict, cfg: dict) -> None:
    payload["director_economics"] = build_director_economics(
        snap, cfg, payload.get("returnzap") or {}
    )


def fmt_money(n: float) -> str:
    return f"${int(round(n)):,}"


def scale_spend(base_spend: float, days: int, base_days: int = 7) -> float:
    if not base_spend:
        return 0.0
    return round(base_spend * days / base_days)


def _quality_entry(
    key: str,
    ad_status: dict[str, dict],
    default_source: str,
    last_live: str | None,
    window_label: str,
) -> dict:
    st = ad_status.get(key, {})
    raw = st.get("status") or "error"
    if raw == "ok":
        status = "ok"
    elif raw == "cached":
        status = "partial"
    elif raw == "estimate":
        status = "estimate"
    elif raw == "pending":
        status = "pending"
    else:
        status = "error"
    notes: list[str] = []
    if raw == "ok":
        notes.append(f"Live pull for {window_label}")
    elif st.get("note"):
        notes.append(st["note"])
    if st.get("error"):
        notes.append(st["error"])
    if last_live and raw != "ok":
        notes.append(f"Last successful live window ended {last_live}")
    return {
        "status": status,
        "source": st.get("source") or default_source,
        "note": " · ".join(notes) if notes else f"No live data for {window_label}",
        "last_live_through": last_live,
        "pulled_at": st.get("pulled_at"),
    }


def _ga4_quality(ad_status: dict[str, dict], pull_errors: dict[str, str], window_label: str) -> dict:
    google = ad_status.get("Google") or {}
    src = google.get("source")
    if google.get("status") == "ok" and src == "ga4_ad_cost":
        return {
            "status": "ok",
            "source": "ga4_ad_cost",
            "note": f"GA4 advertiserAdCost for {window_label}",
            "pulled_at": google.get("pulled_at"),
        }
    if google.get("status") == "ok" and src == "google_ads_api":
        return {
            "status": "skip",
            "source": "ga4_ad_cost",
            "note": f"Not used — Google Ads API supplied spend for {window_label}",
        }
    err = pull_errors.get("Google") or google.get("error")
    if google.get("status") == "estimate":
        return {
            "status": "estimate",
            "source": "ga4_ad_cost",
            "note": err or f"GA4 did not return spend for {window_label}",
        }
    return {
        "status": "error",
        "source": "ga4_ad_cost",
        "note": err or f"GA4 pull failed for {window_label}",
    }


def build_data_quality(
    ad_status: dict[str, dict],
    cfg: dict,
    window_label: str,
    pull_errors: dict[str, str] | None = None,
    shopify_pulled_at: str | None = None,
) -> dict:
    pull_errors = pull_errors or {}
    last_meta = last_successful_live_end("Meta", cfg)
    last_google = last_successful_live_end("Google", cfg)
    last_pin = last_successful_live_end("Pinterest", cfg)
    meta = _quality_entry("Meta", ad_status, "meta_graph_api", last_meta, window_label)
    google = _quality_entry("Google", ad_status, "google_spend", last_google, window_label)
    if ad_status.get("Google", {}).get("source") == "google_ads_api":
        google["source"] = "google_ads_api"
    elif ad_status.get("Google", {}).get("source") == "ga4_ad_cost":
        google["source"] = "ga4_ad_cost"
    pin = _quality_entry("Pinterest", ad_status, "pinterest_api", last_pin, window_label)
    ga4 = _ga4_quality(ad_status, pull_errors, window_label)
    shopify_note = "DTC paid orders + UTM; wholesale separate; excludes cancelled, test, promo, exchanges"
    if shopify_pulled_at:
        shopify_note += f" · pulled {shopify_pulled_at}"
    return {
        "shopify": {
            "status": "ok",
            "source": "shopify_rest",
            "note": shopify_note,
            "pulled_at": shopify_pulled_at,
        },
        "meta": meta,
        "google": google,
        "pinterest": pin,
        "ga4": ga4,
        "blend_meta": meta,
        "blend_google": google,
        "blend_pinterest": pin,
    }


def build_snapshot(days: int, through: str, channels_base: list[dict], targets: dict, cfg: dict) -> dict:
    cur_start, cur_end = window_for(days, through)
    prior_end = cur_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=days - 1)
    month_end = cur_start - timedelta(days=28)
    month_start = month_end - timedelta(days=days - 1)

    cur = shopify_pull(cur_start, cur_end)
    prior = shopify_pull(prior_start, prior_end)
    month = shopify_pull(month_start, month_end)

    cur_ad, ad_source_status, ad_pull_errors = ad_spend_for_range(cur_start, cur_end, cfg)
    prior_ad, _, _ = ad_spend_for_range(prior_start, prior_end, cfg)
    month_ad, _, _ = ad_spend_for_range(month_start, month_end, cfg)

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

    cur_wh = cur_s.get("wholesale") or {}
    prior_wh = prior_s.get("wholesale") or {}
    month_wh = month_s.get("wholesale") or {}

    kpis = {
        "paid_orders": paid_orders,
        "paid_revenue": round(paid_revenue, 2),
        "paid_revenue_net": round(paid_revenue_net, 2),
        "returns_adjusted": round(paid_revenue - paid_revenue_net, 2),
        "wholesale_orders": int(cur_wh.get("orders", 0)),
        "wholesale_revenue": round(float(cur_wh.get("revenue", 0)), 2),
        "wholesale_revenue_net": round(float(cur_wh.get("revenue_net", 0)), 2),
        "total_ad_spend": round(total_spend),
        "blended_mer": round(paid_revenue / total_spend, 2) if total_spend else 0,
        "blended_mer_net": round(paid_revenue_net / total_spend, 2) if total_spend else 0,
        "cpa_blended": round(total_spend / paid_orders, 2) if paid_orders else None,
        "aov": round(paid_revenue / paid_orders, 2) if paid_orders else None,
        "aov_net": round(paid_revenue_net / paid_orders, 2) if paid_orders else None,
        "spend_estimated": any(
            ad_source_status.get(k, {}).get("status") in ("estimate", "error")
            for k in ("Meta", "Google")
        ),
        "unit_economics": cur_s.get("unit_economics") or {},
        "returns_rollup": cur_s.get("returns_rollup") or {},
    }

    return {
        "ad_source_status": ad_source_status,
        "ad_pull_errors": ad_pull_errors,
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
        "wholesale": cur_wh,
        "excluded": cur_s.get("excluded") or {},
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
                "wholesale_orders": int(prior_wh.get("orders", 0)),
                "wholesale_revenue": round(float(prior_wh.get("revenue", 0)), 2),
                "total_ad_spend": round(prior_spend),
                "blended_mer": round(prior_revenue / prior_spend, 2) if prior_spend else 0,
                "blended_mer_net": round(prior_revenue_net / prior_spend, 2) if prior_spend else 0,
            },
            "wholesale": prior_wh,
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
                "wholesale_orders": int(month_wh.get("orders", 0)),
                "wholesale_revenue": round(float(month_wh.get("revenue", 0)), 2),
                "total_ad_spend": round(month_spend),
                "blended_mer": round(month_revenue / month_spend, 2) if month_spend else 0,
                "blended_mer_net": round(month_revenue_net / month_spend, 2) if month_spend else 0,
            },
            "wholesale": month_wh,
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
    payload["wholesale"] = snap.get("wholesale") or {}
    payload["excluded"] = snap.get("excluded") or {}

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
    rz = payload.get("returnzap") or {}
    for days in WINDOW_SIZES:
        snap = build_snapshot(days, through, channels_base, targets, cfg)
        snap["director_economics"] = build_director_economics(snap, cfg, rz)
        windows[str(days)] = snap

    default_snap = windows[str(default_days)]
    apply_snapshot(payload, default_snap)
    regenerate_executive_summary(default_snap, payload)
    regenerate_strategy_todos(default_snap, payload, cfg)
    regenerate_action_rollup(default_snap, payload, cfg)
    regenerate_director_economics(default_snap, payload, cfg)

    w = default_snap["window"]
    cur_start = date.fromisoformat(w["start"])
    cur_end = date.fromisoformat(w["end"])
    top_ads = meta_top_ads(cur_start, cur_end)
    if top_ads:
        payload["meta_top_ads"] = top_ads

    ad_status = default_snap.get("ad_source_status") or {}
    pull_errors = default_snap.get("ad_pull_errors") or {}
    generated_at = datetime.now(TZ).isoformat()
    dq = payload.setdefault("data_quality", {})
    dq.update(build_data_quality(ad_status, cfg, w["label"], pull_errors, generated_at))

    mtd_start, mtd_end = calendar_mtd_range(through)
    mtd_ch, mtd_st, _ = ad_spend_for_range(mtd_start, mtd_end, cfg)
    payload["budget_pacing"] = build_budget_pacing(cfg, mtd_start, mtd_end, mtd_ch, mtd_st)
    payload["anomalies"] = regenerate_anomalies(default_snap)

    payload["windows"] = windows
    # index.html "Last 7 days" strip reads windows["7"] (Shopify UTM + live Meta/Google spend vs prior 7d).
    try:
        from meta_shopify_attribution import build_meta_shopify_attribution

        w7 = windows["7"]["window"]
        payload["meta_shopify_attribution_7d"] = build_meta_shopify_attribution(
            start=date.fromisoformat(w7["start"]),
            end=date.fromisoformat(w7["end"]),
        )
    except Exception as exc:
        print(f"meta_shopify_attribution skipped: {exc}", file=sys.stderr)
    try:
        from meta_ads_by_creative import build_meta_ads_by_creative

        payload["meta_ads_by_creative"] = build_meta_ads_by_creative()
    except Exception as exc:
        print(f"meta_ads_by_creative skipped: {exc}", file=sys.stderr)
    payload["generated_at"] = generated_at
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

    try:
        sys.path.insert(0, str(SCRIPTS))
        from halo_trend import build_meta_halo_trend

        halo = build_meta_halo_trend()
        payload["meta_halo_trend"] = halo
        save_json(DATA / "meta_halo_trend.json", halo)
    except Exception as exc:
        print(f"halo_trend skipped: {exc}", file=sys.stderr)

    save_json(LATEST, payload)
    print(
        f"Windows 7/14/30 refreshed · default {w['label']} · "
        f"{k['paid_orders']} orders · ${k['paid_revenue']:,.0f} gross · ${k.get('paid_revenue_net', k['paid_revenue']):,.0f} net"
    )

    failures: list[str] = []
    warnings: list[str] = []
    for key, secret in (
        ("Meta", "META_ACCESS_TOKEN"),
        ("Google", "GA_SERVICE_ACCOUNT_JSON (or Google Ads OAuth secrets)"),
    ):
        st = ad_status.get(key, {})
        if st.get("status") in ("error", "estimate"):
            msg = st.get("error") or st.get("note") or st.get("status")
            failures.append(f"{key}: {msg} (check GitHub Actions secret {secret})")
    pin_st = ad_status.get("Pinterest") or {}
    if pin_st.get("status") == "pending":
        warnings.append(
            "Pinterest: not connected — app pending approval (not blocking refresh)"
        )
    elif pin_st.get("status") in ("error", "estimate"):
        msg = pin_st.get("error") or pin_st.get("note") or pin_st.get("status")
        warnings.append(f"Pinterest: {msg}")
    save_json(
        REFRESH_STATUS,
        {
            "ok": not failures,
            "failures": failures,
            "warnings": warnings,
            "generated_at": datetime.now(TZ).isoformat(),
        },
    )
    for line in warnings:
        print(f"SOURCE WARNING: {line}", file=sys.stderr)
    if failures:
        for line in failures:
            print(f"SOURCE FAILURE: {line}", file=sys.stderr)


if __name__ == "__main__":
    main()
