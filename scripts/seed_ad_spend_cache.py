#!/usr/bin/env python3
"""Seed ad_spend cache files from Blend snapshots (same window as dashboard)."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AD = ROOT / "data" / "ad_spend"
AD.mkdir(parents=True, exist_ok=True)

# Blend pulls — Aug 26, 2026
SNAPSHOTS = {
    "2026-08-20_2026-08-26": {
        "Meta": {"spend": 1850.33, "platform_purchases": 34, "platform_revenue": 3018.24, "platform_roas": 1.63},
        "Google": {"spend": 417.69, "platform_purchases": 11, "platform_revenue": 863.50, "platform_roas": 2.07},
        "Pinterest": {"spend": 241.11, "platform_purchases": 3, "platform_revenue": 374.00, "platform_roas": 1.55},
    },
    "2026-08-13_2026-08-19": {
        "Meta": {"spend": 1843.73, "platform_purchases": 40, "platform_revenue": 3979.85, "platform_roas": 2.16},
        "Google": {"spend": 395.38, "platform_purchases": 21, "platform_revenue": 2051.89, "platform_roas": 5.19},
        "Pinterest": {"spend": 204.71, "platform_purchases": 0, "platform_revenue": 0, "platform_roas": 0},
    },
    "2026-08-09_2026-08-22": {
        "Meta": {"spend": 3815.22, "platform_purchases": 79, "platform_revenue": 7497.15, "platform_roas": 1.97},
        "Google": {"spend": 690.35, "platform_purchases": 32, "platform_revenue": 2901.28, "platform_roas": 4.20},
        "Pinterest": {"spend": 456.28, "platform_purchases": 2, "platform_revenue": 148.00, "platform_roas": 0.32},
    },
    "2026-07-24_2026-08-26": {
        "Meta": {"spend": 9371.10, "platform_purchases": 181, "platform_revenue": 17939.94, "platform_roas": 1.91},
        "Google": {"spend": 1662.93, "platform_purchases": 63, "platform_revenue": 5670.96, "platform_roas": 3.41},
        "Pinterest": {"spend": 985.82, "platform_purchases": 8, "platform_revenue": 994.95, "platform_roas": 1.01},
    },
}


def main() -> None:
    for key, channels in SNAPSHOTS.items():
        path = AD / f"{key}.json"
        path.write_text(json.dumps({"channels": channels}, indent=2) + "\n")
        print("Wrote", path.name)


if __name__ == "__main__":
    main()
