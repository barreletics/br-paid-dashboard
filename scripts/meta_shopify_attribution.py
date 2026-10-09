#!/usr/bin/env python3
"""Shopify last-touch Meta attribution (utm from customerJourneySummary.lastVisit).

Matches Shopify Sales Attribution / Zaki report: last visit utm_campaign + utm_content (ad).
Joins Meta insights spend by campaign_name and ad_name for the same date window.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections import defaultdict
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
COUNTABLE_FINANCIAL = frozenset({"PAID", "PARTIALLY_PAID", "PARTIALLY_REFUNDED"})

ORDERS_GQL = """
query MetaShopifyAttribution($cursor: String, $q: String!) {
  orders(first: 100, after: $cursor, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        name
        createdAt
        cancelledAt
        displayFinancialStatus
        tags
        currentTotalPriceSet { shopMoney { amount } }
        customerJourneySummary {
          lastVisit {
            landingPage
            utmParameters { source medium campaign content }
          }
        }
      }
    }
  }
}
"""


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing env {name}")
    return v


def shopify_token(domain: str, client_id: str, client_secret: str) -> str:
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()
    req = urllib.request.Request(
        f"https://{domain}/admin/oauth/access_token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Shopify token response missing access_token")
    return token


def graphql(domain: str, token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        f"https://{domain}/admin/api/{API_VERSION}/graphql.json",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())
    if body.get("errors"):
        raise RuntimeError(str(body["errors"][:2]))
    return body.get("data") or {}


def parse_tags(tags: list[str] | str) -> set[str]:
    if isinstance(tags, str):
        return {t.strip() for t in tags.split(",") if t.strip()}
    return {t.strip() for t in tags if t.strip()}


def is_meta_paid_last_touch(utm: dict[str, Any], landing_page: str) -> bool:
    camp = (utm.get("campaign") or "").strip()
    if not camp:
        return False
    src = (utm.get("source") or "").lower()
    med = (utm.get("medium") or "").lower()
    if med in ("paid_social", "paid", "cpc", "social") and src in (
        "facebook",
        "meta",
        "ig",
        "instagram",
        "fb",
    ):
        return True
    if med == "paid_social":
        return True
    lp = (landing_page or "").lower()
    if "fbclid=" in lp:
        return True
    return False


def campaign_label(name: str) -> str:
    """Short label for grouping (Shopify report style)."""
    n = (name or "").strip()
    if not n:
        return "(no campaign)"
    parts = [p.strip() for p in n.split("|") if p.strip()]
    if len(parts) >= 2:
        mid = parts[1]
        if mid:
            return mid
    return n


def norm_key(s: str) -> str:
    return " ".join((s or "").lower().split())


def match_meta_campaign(shopify_campaign: str, meta_names: dict[str, float]) -> str | None:
    sc = norm_key(shopify_campaign)
    sl = campaign_label(shopify_campaign).lower()
    best: str | None = None
    best_score = 0
    for name in meta_names:
        nk = norm_key(name)
        nl = campaign_label(name).lower()
        score = 0
        if nk == sc:
            score = 100
        elif sl and nl == sl:
            score = 90
        elif sl and sl in nk:
            score = 80
        elif nl and nl in sc:
            score = 70
        elif sc and sc in nk:
            score = 60
        if score > best_score:
            best_score = score
            best = name
    return best if best_score >= 60 else None


def meta_insights(
    start: date, end: date, level: str, fields: str, limit: int = 100
) -> list[dict[str, Any]]:
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    acct = os.environ.get("META_AD_ACCOUNT_ID", "act_10152741884925238")
    if not token:
        return []
    if not acct.startswith("act_"):
        acct = f"act_{acct}"
    params = urllib.parse.urlencode(
        {
            "fields": fields,
            "level": level,
            "limit": str(limit),
            "time_range": json.dumps({"since": start.isoformat(), "until": end.isoformat()}),
            "access_token": token,
        }
    )
    url = f"https://graph.facebook.com/v21.0/{acct}/insights?{params}"
    with urllib.request.urlopen(url, timeout=90) as resp:
        data = json.loads(resp.read())
    return list(data.get("data") or [])


def shopify_meta_orders(start: date, end: date) -> list[dict[str, Any]]:
    domain = _env("SHOPIFY_DOMAIN")
    token = shopify_token(domain, _env("SHOPIFY_CLIENT_ID"), _env("SHOPIFY_CLIENT_SECRET"))
    q = (
        f"created_at:>={start.isoformat()} "
        f"created_at:<{(end + timedelta(days=1)).isoformat()}"
    )
    cursor: str | None = None
    out: list[dict[str, Any]] = []
    pages = 0
    while pages < 40:
        pages += 1
        data = graphql(domain, token, ORDERS_GQL, {"cursor": cursor, "q": q})
        conn = data.get("orders") or {}
        for edge in conn.get("edges") or []:
            node = edge.get("node") or {}
            if node.get("cancelledAt"):
                continue
            tags = parse_tags(node.get("tags") or [])
            if tags & EXCLUDE_TAGS:
                continue
            if tags & WHOLESALE_TAGS or any("wholesale" in t.lower() for t in tags):
                continue
            fs = (node.get("displayFinancialStatus") or "").upper()
            if fs not in COUNTABLE_FINANCIAL:
                continue
            money = (node.get("currentTotalPriceSet") or {}).get("shopMoney") or {}
            rev = float(money.get("amount") or 0)
            if rev <= 0:
                continue
            lv = ((node.get("customerJourneySummary") or {}).get("lastVisit") or {})
            utm = lv.get("utmParameters") or {}
            if not is_meta_paid_last_touch(utm, lv.get("landingPage") or ""):
                continue
            camp = (utm.get("campaign") or "").strip() or "(no campaign)"
            ad = (utm.get("content") or "").strip() or "(no ad utm)"
            out.append(
                {
                    "order": node.get("name"),
                    "created_at": (node.get("createdAt") or "")[:10],
                    "revenue": round(rev, 2),
                    "utm_campaign": camp,
                    "utm_content": ad,
                    "campaign_label": campaign_label(camp),
                }
            )
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return out


def build_meta_shopify_attribution(
    start: date | None = None,
    end: date | None = None,
    days: int = 7,
) -> dict[str, Any]:
    end_d = end or datetime.now(TZ).date()
    start_d = start or (end_d - timedelta(days=days - 1))
    orders = shopify_meta_orders(start_d, end_d)

    camp_shop: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"orders": 0, "revenue": 0.0, "order_names": [], "order_dates": []}
    )
    ad_shop: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"orders": 0, "revenue": 0.0, "order_names": [], "utm_campaign": ""}
    )
    for o in orders:
        c = o["utm_campaign"]
        camp_shop[c]["orders"] += 1
        camp_shop[c]["revenue"] += o["revenue"]
        camp_shop[c]["order_names"].append(o["order"])
        camp_shop[c]["order_dates"].append(o["created_at"])
        camp_shop[c]["campaign_label"] = o["campaign_label"]
        key = (c, o["utm_content"])
        ad_shop[key]["orders"] += 1
        ad_shop[key]["revenue"] += o["revenue"]
        ad_shop[key]["order_names"].append(o["order"])
        ad_shop[key]["utm_campaign"] = c
        ad_shop[key]["utm_content"] = o["utm_content"]

    meta_camp_rows = meta_insights(
        start_d, end_d, "campaign", "campaign_name,spend", limit=50
    )
    meta_ad_rows = meta_insights(
        start_d, end_d, "ad", "ad_name,campaign_name,spend", limit=200
    )
    meta_spend_by_campaign = {
        (r.get("campaign_name") or "—"): float(r.get("spend") or 0) for r in meta_camp_rows
    }
    meta_spend_by_ad: dict[tuple[str, str], float] = {}
    for r in meta_ad_rows:
        cn = r.get("campaign_name") or "—"
        an = r.get("ad_name") or "—"
        meta_spend_by_ad[(cn, an)] = float(r.get("spend") or 0)

    campaigns_out: list[dict[str, Any]] = []
    seen_meta: set[str] = set()
    for shop_camp, agg in sorted(camp_shop.items(), key=lambda x: -x[1]["revenue"]):
        meta_name = match_meta_campaign(shop_camp, meta_spend_by_campaign)
        spend = round(meta_spend_by_campaign.get(meta_name or "", 0), 2) if meta_name else 0.0
        if meta_name:
            seen_meta.add(meta_name)
        rev = round(agg["revenue"], 2)
        dates = sorted(agg.get("order_dates") or [])
        campaigns_out.append(
            {
                "shopify_utm_campaign": shop_camp,
                "campaign_label": agg.get("campaign_label") or campaign_label(shop_camp),
                "meta_campaign_name": meta_name,
                "meta_spend": spend,
                "shopify_orders": agg["orders"],
                "shopify_revenue": rev,
                "shopify_roas": round(rev / spend, 2) if spend else None,
                "order_names": sorted(agg["order_names"]),
                "first_shopify_order_date": dates[0] if dates else None,
                "last_shopify_order_date": dates[-1] if dates else None,
            }
        )
    for meta_name, spend in sorted(meta_spend_by_campaign.items(), key=lambda x: -x[1]):
        if meta_name in seen_meta or spend <= 0:
            continue
        campaigns_out.append(
            {
                "shopify_utm_campaign": None,
                "campaign_label": campaign_label(meta_name),
                "meta_campaign_name": meta_name,
                "meta_spend": round(spend, 2),
                "shopify_orders": 0,
                "shopify_revenue": 0.0,
                "shopify_roas": 0.0,
                "order_names": [],
            }
        )
    campaigns_out.sort(key=lambda r: (-(r.get("meta_spend") or 0), -(r.get("shopify_revenue") or 0)))

    ads_out: list[dict[str, Any]] = []
    for (shop_camp, shop_ad), agg in sorted(ad_shop.items(), key=lambda x: -x[1]["revenue"]):
        meta_camp = match_meta_campaign(shop_camp, meta_spend_by_campaign)
        spend = 0.0
        meta_ad_name = None
        if meta_camp:
            for (mc, ma), sp in meta_spend_by_ad.items():
                if mc != meta_camp:
                    continue
                if norm_key(ma) == norm_key(shop_ad) or norm_key(shop_ad) in norm_key(ma):
                    spend = max(spend, sp)
                    meta_ad_name = ma
                elif shop_ad != "(no ad utm)" and norm_key(shop_ad)[:24] in norm_key(ma):
                    spend = max(spend, sp)
                    meta_ad_name = ma
        rev = round(agg["revenue"], 2)
        ads_out.append(
            {
                "shopify_utm_campaign": shop_camp,
                "shopify_utm_content": shop_ad,
                "meta_campaign_name": meta_camp,
                "meta_ad_name": meta_ad_name,
                "meta_spend": round(spend, 2),
                "shopify_orders": agg["orders"],
                "shopify_revenue": rev,
                "shopify_roas": round(rev / spend, 2) if spend else None,
                "order_names": sorted(agg["order_names"]),
            }
        )

    total_rev = round(sum(o["revenue"] for o in orders), 2)
    total_spend = round(sum(meta_spend_by_campaign.values()), 2)
    return {
        "window": {
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "label": f"{start_d.strftime('%b %d')} – {end_d.strftime('%b %d, %Y')}",
            "days": (end_d - start_d).days + 1,
        },
        "attribution": "shopify_last_visit_utm",
        "campaigns": campaigns_out,
        "ads": ads_out,
        "totals": {
            "meta_spend": total_spend,
            "shopify_orders": len(orders),
            "shopify_revenue": total_rev,
            "shopify_roas": round(total_rev / total_spend, 2) if total_spend else None,
        },
    }


if __name__ == "__main__":
    print(json.dumps(build_meta_shopify_attribution(), indent=2))
