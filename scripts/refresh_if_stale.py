#!/usr/bin/env python3
"""Exit 0 = skip refresh (data fresh). Exit 1 = run refresh_rolling.py.

Schedule runs every ~15m; skip when latest.json is newer than STALE_MINUTES.
workflow_dispatch with FORCE_REFRESH=1 always refreshes.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Detroit")
STALE_MINUTES = int(os.environ.get("REFRESH_STALE_MINUTES", "50"))
LATEST = Path(os.environ.get("LATEST_PATH", "data/latest.json"))


def main() -> int:
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event in ("workflow_dispatch", "repository_dispatch"):
        print(f"{event} — always run refresh")
        return 1
    if os.environ.get("FORCE_REFRESH", "").strip().lower() in ("1", "true", "yes"):
        print("FORCE_REFRESH — running refresh")
        return 1
    if not LATEST.is_file():
        print(f"{LATEST} missing — running refresh")
        return 1
    try:
        data = json.loads(LATEST.read_text())
    except json.JSONDecodeError:
        print("latest.json invalid — running refresh")
        return 1
    iso = data.get("generated_at")
    if not iso:
        print("no generated_at — running refresh")
        return 1
    age_min = (datetime.now(TZ) - datetime.fromisoformat(iso)).total_seconds() / 60
    if age_min >= STALE_MINUTES:
        print(f"Data {age_min:.0f}m old (>= {STALE_MINUTES}m) — running refresh")
        return 1
    print(f"Data {age_min:.0f}m old — skip refresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
