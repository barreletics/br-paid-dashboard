#!/usr/bin/env python3
"""Read-only Meta Graph probe: Prospecting → Retargeting audience chain + funnel context.

GET requests only. Uses META_ACCESS_TOKEN like refresh_rolling.py.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from datetime import date, timedelta
from typing import Any

from pull_common import fetch_url, graph_error_message

GRAPH = "https://graph.facebook.com/v21.0"
RETARGETING_NAME = "BAR | Retargeting | US | Nov 2026"
PROSPECTING_NAME = "Barreletix | Prospecting | CBO | US | May"
OTHER_META = (
    "BAR | Creative Test | Broad | US",
    "BAR | Two-Pair Offer Test | US | Nov 2026",
)

AUDIENCE_FIELDS = (
    "id,name,subtype,approximate_count,approximate_count_lower_bound,"
    "approximate_count_upper_bound,retention_days,rule,data_source,description,time_updated"
)
INSIGHT_FIELDS = (
    "reach,impressions,clicks,inline_link_clicks,spend,actions,action_values"
)


def _token() -> str:
    return os.environ.get("META_ACCESS_TOKEN", "").strip()


def _acct() -> str:
    acct = os.environ.get("META_AD_ACCOUNT_ID", "act_10152741884925238").strip()
    if acct and not acct.startswith("act_"):
        acct = f"act_{acct}"
    return acct


def graph_get(path: str, params: dict[str, str]) -> tuple[dict, str | None]:
    """GET only."""
    q = dict(params)
    tok = _token()
    if not tok:
        return {}, "META_ACCESS_TOKEN is not set"
    q["access_token"] = tok
    url = f"{GRAPH}/{path}?{urllib.parse.urlencode(q)}"
    raw, transport_err = fetch_url(url, method="GET")
    if transport_err and not (raw or "").strip():
        return {}, transport_err
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}, "Meta returned non-JSON"
    err = graph_error_message(data)
    if err:
        return {}, err
    return data, None


def paginate(path: str, params: dict[str, str], cap: int = 500) -> tuple[list[dict], str | None]:
    out: list[dict] = []
    after: str | None = None
    while len(out) < cap:
        p = dict(params)
        if after:
            p["after"] = after
        data, err = graph_get(path, p)
        if err:
            return out, err
        out.extend(data.get("data") or [])
        curs = (data.get("paging") or {}).get("cursors") or {}
        after = curs.get("after")
        if not after or not (data.get("data") or []):
            break
    return out, None


def action_sum(actions: list[dict] | None, types: set[str]) -> float:
    total = 0.0
    for a in actions or []:
        if (a.get("action_type") or "") in types:
            total += float(a.get("value") or 0)
    return total


def campaign_insights_30d(campaign_id: str, start: date, end: date) -> tuple[dict, str | None]:
    data, err = graph_get(
        f"{campaign_id}/insights",
        {
            "fields": INSIGHT_FIELDS,
            "time_range": json.dumps({"since": start.isoformat(), "until": end.isoformat()}),
        },
    )
    if err:
        return {}, err
    rows = data.get("data") or []
    if not rows:
        return {}, "No insights rows"
    row = rows[0]
    lpv = action_sum(
        row.get("actions"),
        {"landing_page_view", "omni_landing_page_view"},
    )
    v3 = action_sum(row.get("actions"), {"video_view"})
    thru = action_sum(
        row.get("actions"),
        {"video_thruplay_watched_actions", "thruplay"},
    )
    return {
        "reach": int(float(row.get("reach") or 0)),
        "impressions": int(float(row.get("impressions") or 0)),
        "inline_link_clicks": int(float(row.get("inline_link_clicks") or 0)),
        "clicks": int(float(row.get("clicks") or 0)),
        "spend": round(float(row.get("spend") or 0), 2),
        "landing_page_views": int(lpv),
        "video_views_3s": int(v3),
        "video_thruplay": int(thru),
    }, None


def audience_detail(audience_id: str) -> tuple[dict, str | None]:
    data, err = graph_get(audience_id, {"fields": AUDIENCE_FIELDS})
    if err:
        return {}, err
    rule = data.get("rule")
    if isinstance(rule, str):
        try:
            rule = json.loads(rule)
        except json.JSONDecodeError:
            pass
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "subtype": data.get("subtype"),
        "approximate_count": data.get("approximate_count"),
        "approximate_count_lower_bound": data.get("approximate_count_lower_bound"),
        "approximate_count_upper_bound": data.get("approximate_count_upper_bound"),
        "retention_days": data.get("retention_days"),
        "rule": rule,
        "data_source": data.get("data_source"),
        "description": data.get("description"),
    }, None


def rule_mentions_prospecting(rule: Any) -> list[str]:
    """Return human-readable rule hits suggesting Prospecting feed."""
    hits: list[str] = []
    blob = json.dumps(rule, default=str).lower() if rule is not None else ""
    if "prospecting" in blob or "barreletix" in blob:
        hits.append("Rule text references Prospecting / Barreletix campaign naming")
    for kw in ("video", "ig_engagement", "page_engagement", "website", "pixel", "catalog"):
        if kw in blob:
            hits.append(f"Rule includes {kw} signal (shared with other Meta campaigns)")
    return hits


def ga4_sessions(start: date, end: date) -> tuple[dict[str, Any], str | None]:
    try:
        from google_spend_ga4 import _access_token, _load_sa_json
    except ImportError:
        return {}, "google_spend_ga4 not importable"
    sa = _load_sa_json()
    prop = os.environ.get("GA_PROPERTY_ID", "300437005").replace("properties/", "")
    if not sa:
        return {}, "GA_SERVICE_ACCOUNT_JSON not set"
    try:
        token = _access_token(sa)
    except Exception as exc:
        return {}, f"GA4 auth failed: {exc}"
    body = {
        "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
        "dimensions": [
            {"name": "sessionDefaultChannelGroup"},
            {"name": "sessionCampaignName"},
        ],
        "metrics": [{"name": "sessions"}],
        "limit": 500,
    }
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport"
    raw, transport_err = fetch_url(
        url,
        method="POST",
        body=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    if transport_err and not (raw or "").strip():
        return {}, transport_err
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}, "GA4 non-JSON"
    if data.get("error"):
        err = data["error"]
        return {}, err.get("message") if isinstance(err, dict) else str(err)
    buckets: dict[str, int] = {
        "meta_prospecting": 0,
        "meta_other_paid": 0,
        "google_paid": 0,
        "pinterest_paid": 0,
        "organic_direct_other": 0,
    }
    raw_rows: list[dict] = []
    for row in data.get("rows") or []:
        dims = row.get("dimensionValues") or []
        channel = (dims[0].get("value") if dims else "") or "(not set)"
        campaign = (dims[1].get("value") if len(dims) > 1 else "") or "(not set)"
        sessions = int(float((row.get("metricValues") or [{}])[0].get("value") or 0))
        raw_rows.append({"channel": channel, "campaign": campaign, "sessions": sessions})
        ch = channel.lower()
        camp = campaign.lower()
        if ch in ("direct", "organic search", "organic social", "email", "referral", "unassigned"):
            buckets["organic_direct_other"] += sessions
        elif "paid search" in ch or "google" in camp:
            buckets["google_paid"] += sessions
        elif "pinterest" in ch or "pinterest" in camp:
            buckets["pinterest_paid"] += sessions
        elif "paid social" in ch or "paid" in ch:
            if "prospecting" in camp or "barreletix" in camp:
                buckets["meta_prospecting"] += sessions
            else:
                buckets["meta_other_paid"] += sessions
        else:
            buckets["organic_direct_other"] += sessions
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "buckets": buckets,
        "rows_sample": raw_rows[:40],
        "source": "ga4_runReport",
    }, None


def verdict(
    audiences: list[dict],
    prospecting: dict,
    other_meta: list[dict],
    ga4: dict,
) -> tuple[str, str]:
    """Return (label, reason)."""
    explicit = []
    for aud in audiences:
        explicit.extend(rule_mentions_prospecting(aud.get("rule")))
    prospecting_reach = int(prospecting.get("reach") or 0)
    prospecting_lpv = int(prospecting.get("landing_page_views") or 0)
    other_reach = sum(int(c.get("reach") or 0) for c in other_meta)
    total_reach = prospecting_reach + other_reach
    share = (prospecting_reach / total_reach) if total_reach else None
    aud_sizes = [
        int(a.get("approximate_count") or a.get("approximate_count_upper_bound") or 0)
        for a in audiences
    ]
    max_aud = max(aud_sizes) if aud_sizes else 0

    if any("references Prospecting" in h for h in explicit):
        reason = (
            "Retargeting custom audience rule explicitly references Prospecting/Barreletix. "
            f"Prospecting 30d reach {prospecting_reach:,}, LPV {prospecting_lpv:,}."
        )
        return "PROVEN", reason

    if audiences and max_aud > 0 and prospecting_reach > 0:
        pct = f"{share * 100:.0f}%" if share is not None else "n/a"
        reason = (
            "Retargeting uses website/engagement/video audiences that also collect people from "
            f"all Meta campaigns — not Prospecting-only. Prospecting 30d reach {prospecting_reach:,} "
            f"vs other Meta campaigns reach {other_reach:,} (~{pct} of Meta reach). "
            "No audience rule tied only to Prospecting."
        )
        return "LIKELY" if share and share >= 0.15 else "NOT PROVEN", reason

    reason = "Could not load retargeting audiences or Prospecting reach from Meta Graph."
    return "NOT PROVEN", reason


def build_ads_connect(cfg: dict | None = None) -> dict[str, Any]:
    """Build ads_connect block for latest.json (read-only sources)."""
    end = date.today()
    start_30 = end - timedelta(days=29)
    acct = _acct()
    out: dict[str, Any] = {
        "generated_at": None,
        "meta_read_only": True,
        "window_30d": {"start": start_30.isoformat(), "end": end.isoformat()},
        "errors": [],
    }
    if not _token():
        out["verdict"] = "NOT PROVEN"
        out["verdict_reason"] = "META_ACCESS_TOKEN missing — run refresh on GitHub Actions with Meta secret."
        return out

    campaigns, err = paginate(
        f"{acct}/campaigns",
        {"fields": "id,name,status", "limit": "100"},
    )
    if err:
        out["errors"].append(f"campaigns: {err}")
        out["verdict"] = "NOT PROVEN"
        out["verdict_reason"] = err
        return out

    by_name = {c.get("name"): c for c in campaigns if c.get("name")}
    rt = by_name.get(RETARGETING_NAME)
    pr = by_name.get(PROSPECTING_NAME)
    if not rt:
        out["errors"].append(f"Campaign not found: {RETARGETING_NAME}")
    if not pr:
        out["errors"].append(f"Campaign not found: {PROSPECTING_NAME}")

    audiences: list[dict] = []
    adsets: list[dict] = []
    if rt:
        sets, err2 = paginate(
            f"{rt['id']}/adsets",
            {"fields": "id,name,status,targeting", "limit": "50"},
        )
        if err2:
            out["errors"].append(f"adsets: {err2}")
        else:
            adsets = sets
            seen: set[str] = set()
            for s in sets:
                tgt = s.get("targeting") or {}
                if isinstance(tgt, str):
                    try:
                        tgt = json.loads(tgt)
                    except json.JSONDecodeError:
                        tgt = {}
                for key in ("custom_audiences", "excluded_custom_audiences"):
                    for item in tgt.get(key) or []:
                        aid = str(item.get("id") or item)
                        if aid in seen:
                            continue
                        seen.add(aid)
                        det, aerr = audience_detail(aid)
                        if aerr:
                            out["errors"].append(f"audience {aid}: {aerr}")
                            continue
                        det["used_in"] = key
                        det["ad_set"] = s.get("name")
                        audiences.append(det)

    prospecting_metrics: dict = {}
    if pr:
        prospecting_metrics, perr = campaign_insights_30d(pr["id"], start_30, end)
        if perr:
            out["errors"].append(f"prospecting insights: {perr}")

    other_metrics: list[dict] = []
    for name in OTHER_META:
        c = by_name.get(name)
        if not c:
            continue
        met, oerr = campaign_insights_30d(c["id"], start_30, end)
        if oerr:
            out["errors"].append(f"{name} insights: {oerr}")
            continue
        other_metrics.append({"campaign": name, **met})

    retention = max([int(a.get("retention_days") or 0) for a in audiences] + [30])
    ga_start = end - timedelta(days=retention - 1)
    ga4, gerr = ga4_sessions(ga_start, end)
    if gerr:
        out["errors"].append(f"GA4: {gerr}")

    # Retargeting store close (7d) from cfg / campaign_status if present — filled by refresh caller
    ret_shop: dict[str, Any] = {}

    label, reason = verdict(audiences, prospecting_metrics, other_metrics, ga4)
    p_reach = int(prospecting_metrics.get("reach") or 0)
    o_reach = sum(int(c.get("reach") or 0) for c in other_metrics)
    tot = p_reach + o_reach
    prospecting_reach_share_pct = round(100 * p_reach / tot, 1) if tot else None

    out.update(
        {
            "prospecting_reach_share_pct_30d": prospecting_reach_share_pct,
            "retargeting_campaign": RETARGETING_NAME,
            "prospecting_campaign": PROSPECTING_NAME,
            "ad_sets": [
                {"id": s.get("id"), "name": s.get("name"), "status": s.get("status")}
                for s in adsets
            ],
            "custom_audiences": audiences,
            "prospecting_30d": prospecting_metrics,
            "other_meta_30d": other_metrics,
            "audience_retention_days_max": retention,
            "ga4_sessions": ga4,
            "retargeting_store_7d": ret_shop,
            "verdict": label,
            "verdict_reason": reason,
            "sources": [
                "Meta Graph API v21.0 GET (campaigns, adsets, customaudiences, insights)",
                "GA4 Data API runReport (sessions by channel + campaign)",
            ],
        }
    )
    return out


def enrich_retargeting_store(block: dict, campaign_status: list[dict]) -> None:
    for row in campaign_status:
        if row.get("campaign") == RETARGETING_NAME:
            m = re.search(
                r"(\d+)\s+orders?\s*·\s*\$([\d,]+\.?\d*)\s*·\s*([\d.]+)\s*×",
                row.get("key_metric") or "",
                re.I,
            )
            if m:
                block["retargeting_store_7d"] = {
                    "orders": int(m.group(1)),
                    "revenue": float(m.group(2).replace(",", "")),
                    "return_per_dollar": float(m.group(3)),
                    "spend": row.get("spend_period"),
                    "source": "campaign_status Shopify last-touch",
                }
            break


if __name__ == "__main__":
    b = build_ads_connect()
    print(json.dumps(b, indent=2)[:12000])
