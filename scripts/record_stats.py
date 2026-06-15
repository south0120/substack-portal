#!/usr/bin/env python3
"""FYL 日次レポート: /api/health の記事数・ライター数を取得して
reports/daily_stats.md（人が読める表）と reports/daily_stats.csv（機械用）に追記する。

毎朝 8:00 JST に GitHub Actions から実行される想定。
同じ日付の記録が既にあれば上書き（手動再実行で重複しない）。
"""
import csv
import json
import os
import ssl
import urllib.request
from datetime import datetime, timezone, timedelta

HEALTH_URL = os.environ.get("FYL_HEALTH_URL", "https://fyl-api.south0120.workers.dev/api/health")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(ROOT, "reports")
CSV_PATH = os.path.join(REPORTS_DIR, "daily_stats.csv")
MD_PATH = os.path.join(REPORTS_DIR, "daily_stats.md")
JST = timezone(timedelta(hours=9))


def fetch_health():
    # テスト/オフライン用: 健康JSONを直接渡せる
    if os.environ.get("FYL_HEALTH_JSON"):
        return json.loads(os.environ["FYL_HEALTH_JSON"])
    ctx = None
    try:  # macOS / 一部環境のCA不備対策（CIでは pip install certifi 済み）
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = None
    req = urllib.request.Request(HEALTH_URL, headers={"User-Agent": "fyl-daily-stats"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read())


def load_rows():
    rows = []
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(row)
    return rows


def main():
    os.makedirs(REPORTS_DIR, exist_ok=True)
    health = fetch_health()
    now_jst = datetime.now(JST)
    date = now_jst.strftime("%Y-%m-%d")
    record = {
        "date": date,
        "articles": str(health.get("articles", "")),
        "writers": str(health.get("writers", "")),
        "cursor": str(health.get("cursor", "")),
        "recorded_at_jst": now_jst.strftime("%Y-%m-%d %H:%M"),
    }

    rows = load_rows()
    rows = [r for r in rows if r.get("date") != date]  # 同日があれば差し替え
    rows.append(record)
    rows.sort(key=lambda r: r.get("date", ""))

    fields = ["date", "articles", "writers", "cursor", "recorded_at_jst"]
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    # 人が読める Markdown 表（前日比つき）
    lines = [
        "# Find Your Letter — 日次レポート",
        "",
        "毎朝 8:00 JST に記事数・ライター数を自動記録しています（`scripts/record_stats.py`）。",
        "",
        "| 日付 | 記事数 | 前日比 | ライター数 | 前日比 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    prev_a = prev_w = None
    for r in rows:
        try:
            a = int(r["articles"]); w_ = int(r["writers"])
        except (ValueError, KeyError):
            a = w_ = None
        da = f"+{a - prev_a}" if (a is not None and prev_a is not None and a - prev_a >= 0) else (str(a - prev_a) if (a is not None and prev_a is not None) else "—")
        dw = f"+{w_ - prev_w}" if (w_ is not None and prev_w is not None and w_ - prev_w >= 0) else (str(w_ - prev_w) if (w_ is not None and prev_w is not None) else "—")
        lines.append(f"| {r['date']} | {r['articles']} | {da} | {r['writers']} | {dw} |")
        if a is not None: prev_a = a
        if w_ is not None: prev_w = w_
    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"recorded {date}: articles={record['articles']} writers={record['writers']}")


if __name__ == "__main__":
    main()
