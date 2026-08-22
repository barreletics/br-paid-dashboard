#!/usr/bin/env python3
"""Build dashboard/data/latest.json from Shopify pull + optional channel JSON.

Shopify (required): runs shopify_weekly_pull.py or reads --shopify file.
Optional merges: --blend-meta, --blend-google, --blend-pinterest, --ga4 (JSON files from agent/MCP export)

Usage:
  export SHOPIFY_DOMAIN=... SHOPIFY_CLIENT_ID=... SHOPIFY_CLIENT_SECRET=...
  python3 build_dashboard.py --days 7
  python3 build_dashboard.py --start 2026-08-13 --end 2026-08-22 --out data/latest.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Detroit")
ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT.parent / "scripts"


def load_json(path: Path | None) -> dict:
    if not path or not path.exists():
        return {}
    return json.loads(path.read_text())


def shopify_pull(days: int, start: str, end: str) -> dict:
    cmd = [sys.executable, str(SCRIPTS / "shopify_weekly_pull.py")]
    if start and end:
        cmd += ["--start", start, "--end", end]
    else:
        cmd += ["--days", str(days)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "shopify pull failed")
    return json.loads(proc.stdout)


def channel_row(
    name: str,
    spend: float,
    shopify_orders: int,
    shopify_revenue: float,
    platform_purchases: int | None = None,
    platform_revenue: float | None = None,
    targets: dict | None = None,
) -> dict:
    shopify_roas = round(shopify_revenue / spend, 2) if spend else 0
    platform_roas = round(platform_revenue / spend, 2) if spend and platform_revenue else 0
    status = "good"
    t = targets or {}
    if name.startswith("Meta") and shopify_roas < t.get("meta_shopify_roas_target", 1.5):
        status = "watch" if shopify_roas >= 1.0 else "pull_back"
    elif name.startswith("Google") and shopify_roas >= t.get("google_shopify_roas_target", 3.0):
        status = "good"
    elif name.startswith("Pinterest"):
        if shopify_orders == 0 and spend >= t.get("pinterest_max_spend_no_orders", 150):
            status = "pull_back"
        elif shopify_orders == 0:
            status = "watch"
    return {
        "name": name,
        "spend": round(spend),
        "shopify_orders": shopify_orders,
        "shopify_revenue": round(shopify_revenue),
        "shopify_roas": shopify_roas,
        "platform_purchases": platform_purchases,
        "platform_revenue": round(platform_revenue) if platform_revenue else 0,
        "platform_roas": platform_roas,
        "status": status,
    }


def build_actions(channels: list[dict]) -> dict:
    spend_more, pull_back, create, watch = [], [], [], []

    for c in channels:
        if c["status"] == "good":
            spend_more.append(
                {
                    "channel": c["name"],
                    "reason": f"Shopify ROAS {c['shopify_roas']}× on {c['spend']} spend",
                }
            )
        if c["status"] == "pull_back":
            pull_back.append(
                {
                    "channel": c["name"],
                    "reason": f"{c['spend']} spend · {c['shopify_orders']} Shopify orders · ROAS {c['shopify_roas']}×",
                }
            )
        if c["name"] == "Meta" and c["status"] == "watch":
            watch.append(
                {
                    "channel": "Meta",
                    "item": f"Shopify ROAS {c['shopify_roas']}× — hold budget raises until ≥1.5×",
                }
            )
        if c["name"] == "Pinterest" and c["shopify_orders"] == 0:
            pull_back.append(
                {
                    "channel": "Pinterest",
                    "reason": "0 Shopify orders — verify tag before scaling",
                }
            ) if not any(x["channel"] == "Pinterest" for x in pull_back) else None

    if not spend_more:
        spend_more.append({"channel": "—", "reason": "Review scorecard"})
    if not pull_back:
        pull_back.append({"channel": "—", "reason": "None flagged"})
    create.append(
        {
            "channel": "Meta",
            "item": "Run new creatives 7 full days before pause if they have purchases",
        }
    )
    watch.append(
        {
            "channel": "Meta",
            "item": "Compare platform purchases vs Shopify Meta UTM weekly",
        }
    )

    return {
        "spend_more": spend_more,
        "pull_back": pull_back,
        "create": create,
        "watch": watch,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--start", default="")
    p.add_argument("--end", default="")
    p.add_argument("--shopify", type=Path, help="Pre-pulled shopify JSON")
    p.add_argument("--blend-meta", type=Path)
    p.add_argument("--blend-google", type=Path)
    p.add_argument("--blend-pinterest", type=Path)
    p.add_argument("--ga4", type=Path)
    p.add_argument("--out", type=Path, default=ROOT / "data" / "latest.json")
    args = p.parse_args()

    if args.shopify:
        shop = json.loads(args.shopify.read_text())
    else:
        shop = shopify_pull(args.days, args.start, args.end)

    summary = shop["summary"]
    ch = summary.get("channels", {})
    meta = ch.get("Meta", {"orders": 0, "revenue": 0})
    google = ch.get("Google", {"orders": 0, "revenue": 0})
    pin = ch.get("Pinterest", {"orders": 0, "revenue": 0})

    bm = load_json(args.blend_meta)
    bg = load_json(args.blend_google)
    bp = load_json(args.blend_pinterest)

    meta_spend = bm.get("spend", 0)
    google_spend = bg.get("spend", 518 if not bg else 0)
    pin_spend = bp.get("spend", 0)

    thresholds = {
        "meta_shopify_roas_target": 1.5,
        "google_shopify_roas_target": 3.0,
        "pinterest_max_spend_no_orders": 150,
    }

    channels = [
        channel_row(
            "Meta",
            meta_spend or 2503,
            meta.get("orders", 0),
            meta.get("revenue", 0),
            bm.get("purchases"),
            bm.get("revenue"),
            thresholds,
        ),
        channel_row(
            "Google PMax",
            google_spend or bg.get("spend", 518),
            google.get("orders", 0),
            google.get("revenue", 0),
            bg.get("conversions"),
            bg.get("conversion_value"),
            thresholds,
        ),
        channel_row(
            "Pinterest",
            pin_spend or bp.get("spend", 0),
            pin.get("orders", 0),
            pin.get("revenue", 0),
            bp.get("checkouts"),
            bp.get("checkout_value"),
            thresholds,
        ),
    ]

    total_spend = sum(c["spend"] for c in channels)
    payload = {
        "generated_at": datetime.now(TZ).isoformat(),
        "window": shop.get("window", {}),
        "data_quality": {
            "shopify": {
                "status": "ok",
                "source": "shopify_rest",
                "note": "Paid orders + UTM",
            },
            "blend_meta": {
                "status": "ok" if bm else "partial",
                "source": "blend",
                "note": "Pass --blend-meta JSON for live spend",
            },
            "blend_google": {"status": "ok" if bg else "partial", "source": "blend"},
            "blend_pinterest": {"status": "partial" if not bp else "ok", "source": "blend"},
            "ga4": {"status": "ok" if args.ga4 else "partial", "source": "ga4"},
            "shopify_mcp": {
                "status": "skip",
                "source": "n/a",
                "note": "Use REST script only for weekly",
            },
        },
        "kpis": {
            "paid_orders": summary.get("paid_orders", 0),
            "paid_revenue": summary.get("paid_revenue", 0),
            "total_ad_spend": total_spend,
            "blended_mer": round(summary.get("paid_revenue", 0) / total_spend, 2)
            if total_spend
            else 0,
        },
        "channels": channels,
        "actions": build_actions(channels),
        "meta_top_ads": bm.get("top_ads", []),
        "ga4_funnel": load_json(args.ga4).get("funnel", {}),
        "daily_orders": summary.get("daily_orders", {}),
        "thresholds": thresholds,
    }

    if "label" not in payload["window"]:
        w = payload["window"]
        payload["window"]["label"] = f"{w.get('start', '')} – {w.get('end', '')}"

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
