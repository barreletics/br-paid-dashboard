#!/usr/bin/env python3
"""Bake data/latest.json + config.json into report.html (works without a server)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
data = json.loads((ROOT / "data/latest.json").read_text())
config = json.loads((ROOT / "config.json").read_text())
html = (ROOT / "index.html").read_text()

pattern = re.compile(
    r"fetch\(CONFIG_URL.*?\n\}, 30 \* 60 \* 1000\);",
    re.DOTALL,
)
if not pattern.search(html):
    raise SystemExit("index.html layout changed — update build_standalone.py")

def bake_tail(_match: re.Match[str]) -> str:
    return f"reportConfig = {json.dumps(config)};\nrender({json.dumps(data)});"


standalone = pattern.sub(bake_tail, html)
(ROOT / "report.html").write_text(standalone)
print(f"Wrote {ROOT / 'report.html'}")
