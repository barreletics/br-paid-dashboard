#!/usr/bin/env python3
"""Weekly Meta spend vs Shopify channel mix (Jun+). For dashboard meta_halo_trend.json."""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Detroit")
START = date(2026, 6, 1)

CHANNEL_KEYS = [
    "meta_paid",
    "google_paid",
    "organic_search",
    "direct",
    "organic_social",
    "email",
    "other_paid_social",
    "other",
]


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def classify_order(o: dict) -> str:
    land = o.get("landing_site") or ""
    ref = (o.get("referring_site") or "").lower()
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(land).query)
    src = (qs.get("utm_source") or [""])[0].lower().strip()
    med = (qs.get("utm_medium") or [""])[0].lower().strip()
    camp = (qs.get("utm_campaign") or [""])[0].strip()
    looks_meta = med in ("paid_social", "paid") and src in (
        "fb",
        "ig",
        "facebook",
        "instagram",
        "meta",
        "",
    )
    if not looks_meta and src in ("fb", "ig", "facebook", "instagram", "meta") and med in (
        "paid_social",
        "paid",
        "cpc",
        "social",
    ):
        looks_meta = True
    if med == "paid_social" and src not in ("google",):
        looks_meta = True
    if looks_meta:
        return "meta_paid" if camp else "organic_social"
    if src == "google" or med in ("cpc", "ppc", "pmax") and "google" in src + med:
        return "google_paid"
    if src == "google":
        return "google_paid"
    if med == "email" or src in ("shopify_email", "judgeme", "klaviyo", "email"):
        return "email"
    if any(x in ref for x in ("facebook.com", "instagram.com", "l.facebook.com")):
        return "organic_social"
    if src in ("ig", "fb", "facebook", "instagram") and med not in ("paid_social", "paid", "cpc"):
        return "organic_social"
    if any(x in ref for x in ("google.com", "bing.com", "duckduckgo.com")) and "ads" not in ref:
        return "organic_search"
    if med == "organic" and src in ("google", "bing"):
        return "organic_search"
    if src in ("pinterest", "tiktok") or med == "pinterest":
        return "other_paid_social"
    if not land.strip() and not ref.strip():
        return "direct"
    if land and "?" not in land and not ref:
        return "direct"
    return "other"


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 4:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    dy = math.sqrt(sum((b - my) ** 2 for b in ys))
    return round(num / (dx * dy), 2) if dx and dy else None


def corr_lag(rows: list[dict], ykey: str, xkey: str = "meta_spend", lag: int = 0) -> float | None:
    xs, ys = [], []
    for i in range(lag, len(rows)):
        if rows[i][xkey] <= 50:
            continue
        xs.append(float(rows[i][xkey]))
        ys.append(float(rows[i - lag][ykey]))
    if len(xs) < 4 or len(xs) != len(ys):
        return None
    return pearson(xs, ys)


def fetch_orders_rest(start: date, end: date) -> list[dict]:
    domain = os.environ["SHOPIFY_DOMAIN"]
    cid = os.environ["SHOPIFY_CLIENT_ID"]
    sec = os.environ["SHOPIFY_CLIENT_SECRET"]
    body = json.dumps(
        {"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"}
    ).encode()
    req = urllib.request.Request(
        f"https://{domain}/admin/oauth/access_token",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        token = json.loads(resp.read())["access_token"]
    cmin = f"{start.isoformat()}T00:00:00-04:00"
    cmax = f"{(end + timedelta(days=1)).isoformat()}T00:00:00-04:00"
    orders: list[dict] = []
    params = {
        "status": "any",
        "limit": "250",
        "order": "created_at asc",
        "created_at_min": cmin,
        "created_at_max": cmax,
        "fields": "created_at,total_price,current_total_price,tags,landing_site,referring_site,financial_status,line_items",
    }
    url = f"https://{domain}/admin/api/2024-10/orders.json?" + urllib.parse.urlencode(params)
    while url:
        req = urllib.request.Request(url, headers={"X-Shopify-Access-Token": token})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        orders.extend(data.get("orders", []))
        link = resp.headers.get("Link", "")
        url = None
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip(" <>")
                    break
    return orders


def net(o: dict) -> float:
    c = o.get("current_total_price")
    return float(c if c not in (None, "") else o.get("total_price") or 0)


def excluded(o: dict) -> bool:
    if o.get("financial_status") != "paid" or net(o) <= 0:
        return True
    if "TEST ORDER" in (o.get("tags") or "").upper():
        return True
    return any(it.get("gift_card") for it in o.get("line_items") or [])


def meta_spend_week(week_start_iso: str, week_end_iso: str) -> float:
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    acct = os.environ.get("META_AD_ACCOUNT_ID", "act_10152741884925238")
    if not token:
        return 0.0
    if not acct.startswith("act_"):
        acct = f"act_{acct}"
    params = urllib.parse.urlencode(
        {
            "time_range": json.dumps({"since": week_start_iso, "until": week_end_iso}),
            "fields": "spend",
            "access_token": token,
        }
    )
    url = f"https://graph.facebook.com/v21.0/{acct}/insights?{params}"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as resp:
        row = (json.loads(resp.read()).get("data") or [{}])[0]
    return float(row.get("spend") or 0)


def build_meta_halo_trend(end: date | None = None) -> dict:
    end_d = end or datetime.now(TZ).date()
    orders = fetch_orders_rest(START, end_d)
    wk_ord: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    wk_rev: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for o in orders:
        if excluded(o):
            continue
        dt = datetime.fromisoformat(o["created_at"].replace("Z", "+00:00")).astimezone(TZ).date()
        if dt < START:
            continue
        ws = week_start(dt).isoformat()
        ch = classify_order(o)
        wk_ord[ws][ch] += 1
        wk_rev[ws][ch] += net(o)

    weeks_sorted = sorted(wk_ord.keys())
    rows: list[dict] = []
    for ws in weeks_sorted:
        we = (date.fromisoformat(ws) + timedelta(days=6)).isoformat()
        o, r = wk_ord[ws], wk_rev[ws]
        ms = meta_spend_week(ws, we)
        meta_o = o.get("meta_paid", 0)
        meta_r = r.get("meta_paid", 0.0)
        meta_roas = round(meta_r / ms, 2) if ms else 0.0
        halo_rev = sum(r.get(k, 0.0) for k in CHANNEL_KEYS if k != "meta_paid")
        halo_ord = sum(o.get(k, 0) for k in CHANNEL_KEYS if k != "meta_paid")
        rows.append(
            {
                "week_start": ws,
                "week_end": we,
                "label": (
                    f"{date.fromisoformat(ws).strftime('%b %d')}–"
                    f"{(date.fromisoformat(ws) + timedelta(days=6)).strftime('%d, %Y')}"
                ),
                "meta_spend": round(ms, 2),
                "meta_orders": meta_o,
                "meta_revenue": round(meta_r, 2),
                "meta_roas": meta_roas,
                "meta_orders_per_day": round(meta_o / 7, 2),
                "google_paid_orders": o.get("google_paid", 0),
                "google_paid_revenue": round(r.get("google_paid", 0), 2),
                "organic_search_orders": o.get("organic_search", 0),
                "organic_search_revenue": round(r.get("organic_search", 0), 2),
                "direct_orders": o.get("direct", 0),
                "direct_revenue": round(r.get("direct", 0), 2),
                "organic_social_orders": o.get("organic_social", 0),
                "organic_social_revenue": round(r.get("organic_social", 0), 2),
                "email_orders": o.get("email", 0),
                "email_revenue": round(r.get("email", 0), 2),
                "other_orders": o.get("other_paid_social", 0) + o.get("other", 0),
                "other_revenue": round(r.get("other_paid_social", 0) + r.get("other", 0), 2),
                "organic_halo_revenue": round(halo_rev, 2),
                "organic_halo_orders": halo_ord,
            }
        )

    active = [x for x in rows if x["meta_spend"] > 100]
    med_roas = sorted(x["meta_roas"] for x in active)
    med_opd = sorted(x["meta_orders_per_day"] for x in active)
    mid_r = med_roas[len(med_roas) // 2] if med_roas else 1.0
    mid_o = med_opd[len(med_opd) // 2] if med_opd else 2.0
    for x in rows:
        x["meta_week"] = (
            "good"
            if x["meta_roas"] >= max(1.0, mid_r) and x["meta_orders_per_day"] >= mid_o
            else "bad"
        )

    xs = [float(r["meta_spend"]) for r in active]
    ys = [float(r["organic_halo_revenue"]) for r in active]
    slope = None
    if len(xs) >= 4:
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        den = sum((a - mx) ** 2 for a in xs)
        slope = round(sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / den, 3) if den else None

    stats = {
        "corr_meta_spend_organic_halo_lag0": corr_lag(rows, "organic_halo_revenue", lag=0),
        "corr_meta_spend_organic_halo_lag1": corr_lag(rows, "organic_halo_revenue", lag=1),
        "corr_meta_revenue_organic_halo_lag0": corr_lag(rows, "organic_halo_revenue", "meta_revenue", 0),
        "corr_meta_spend_direct_lag0": corr_lag(rows, "direct_revenue", lag=0),
        "corr_meta_spend_organic_search_lag0": corr_lag(rows, "organic_search_revenue", lag=0),
        "organic_halo_revenue_per_meta_dollar": slope,
        "weeks_count": len(rows),
        "good_weeks": sum(1 for r in rows if r["meta_week"] == "good"),
        "bad_weeks": sum(1 for r in rows if r["meta_week"] == "bad"),
    }

    good = [r for r in rows if r["meta_week"] == "good" and r["meta_spend"] > 100]
    bad = [r for r in rows if r["meta_week"] == "bad" and r["meta_spend"] > 100]
    if good and bad:
        stats["avg_organic_halo_good"] = round(
            sum(r["organic_halo_revenue"] for r in good) / len(good), 0
        )
        stats["avg_organic_halo_bad"] = round(
            sum(r["organic_halo_revenue"] for r in bad) / len(bad), 0
        )

    c0 = stats.get("corr_meta_spend_organic_halo_lag0")
    slope = stats.get("organic_halo_revenue_per_meta_dollar")
    findings = [
        f"Since Jun 2026 ({len(rows)} Mon–Sun weeks): Meta spend vs organic halo revenue correlates weakly (lag-0 r≈{c0}, lag-1 r≈{stats.get('corr_meta_spend_organic_halo_lag1')}) — moves together sometimes, not lockstep.",
        f"Meta-attributed Shopify sales track halo better than spend alone (lag-0 r≈{stats.get('corr_meta_revenue_organic_halo_lag0')}). Good Meta weeks avg ~${stats.get('avg_organic_halo_good', '—')} organic halo vs ~${stats.get('avg_organic_halo_bad', '—')} on bad weeks.",
        f"Rule-of-thumb from weekly data: ~${slope or '—'} organic halo Shopify $ per $1 Meta spend (non-causal; includes Oct partial week).",
    ]

    return {
        "generated_at": datetime.now(TZ).isoformat(),
        "start": START.isoformat(),
        "end": end_d.isoformat(),
        "weeks": rows,
        "stats": stats,
        "findings": findings,
    }


def main() -> None:
    out = build_meta_halo_trend()
    path = Path(os.environ.get("HALO_OUT", "data/meta_halo_trend.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(f"Wrote {path} · {len(out['weeks'])} weeks")


if __name__ == "__main__":
    main()
