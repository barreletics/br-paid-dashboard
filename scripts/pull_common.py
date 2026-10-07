"""Shared helpers for ad platform pull scripts."""

from __future__ import annotations

import json
import subprocess
import urllib.request
from typing import Any


def fetch_url(url: str, *, method: str = "GET", body: bytes | None = None, headers: dict | None = None) -> tuple[str, str | None]:
    """Return (response_text, error_message). Prefers curl when available."""
    raw = ""
    hdrs = headers or {}
    try:
        cmd = ["curl", "-sS", "-X", method, url]
        for k, v in hdrs.items():
            cmd.extend(["-H", f"{k}: {v}"])
        if body is not None:
            cmd.extend(["-d", body.decode() if isinstance(body, bytes) else body])
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout, None
        if proc.stderr.strip():
            return proc.stdout or "", proc.stderr.strip()
    except Exception as exc:
        return "", str(exc)

    try:
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.read().decode(), None
    except Exception as exc:
        return raw, str(exc)


def graph_error_message(data: dict[str, Any]) -> str | None:
    err = data.get("error")
    if not err:
        return None
    if isinstance(err, dict):
        code = err.get("code")
        msg = err.get("message") or "Graph API error"
        typ = err.get("type") or "Error"
        return f"{typ}: {msg}" + (f" (code {code})" if code is not None else "")
    return str(err)


def is_config_estimate(spend: float, days: int, daily_rate: float | None) -> bool:
    if not daily_rate or days <= 0:
        return False
    expected = round(float(daily_rate) * days, 2)
    return abs(float(spend) - expected) < 0.02


def looks_like_live_meta(channel: dict[str, Any], days: int, daily_rate: float | None) -> bool:
    spend = float(channel.get("spend") or 0)
    if spend <= 0:
        return False
    if channel.get("spend_source") == "estimate":
        return False
    purch = int(channel.get("platform_purchases") or 0)
    if purch > 0:
        return True
    if channel.get("impressions") or channel.get("clicks"):
        return True
    return not is_config_estimate(spend, days, daily_rate)
