#!/usr/bin/env python3
"""FYL サイトのアクセス数を GA4 から取って docs/data/analytics.json に書く。

管理ダッシュボード（docs/admin.html）はこの JSON を読むだけ。
🔑 ブラウザに認証情報を置かないための構成: 鍵はこの Mac の中だけに在り、
   外に出るのは**集計済みの数字**だけ。

使い方:
    python3 scripts/fetch_ga4.py            # 直近28日
    python3 scripts/fetch_ga4.py --days 90

🔴 数字の定義（ダッシュボードにも同じ言葉で出すこと）
   sessions      … 訪問の回数（同じ人が翌日来たら2）
   activeUsers   … 人数（GA4 の推定。端末やブラウザが変わると別人に見える）
   screenPageViews … ページが表示された回数
   ＝ **3つは別物**。「アクセス数」とだけ書くと、どれの話か分からなくなる。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

from google.oauth2 import service_account
import google.auth.transport.requests as gart

PROPERTY_ID = "541711580"          # Find Your Letter（正本: memory reference_analytics_accounts）
KEY = pathlib.Path.home() / ".claude/credentials/ga4_reptilelab.json"
OUT = pathlib.Path(__file__).resolve().parents[1] / "docs/data/analytics.json"
JST = timezone(timedelta(hours=9))


def token() -> str:
    if not KEY.exists():
        sys.exit(f"ERROR: 鍵が無い: {KEY}")
    creds = service_account.Credentials.from_service_account_file(
        str(KEY), scopes=["https://www.googleapis.com/auth/analytics.readonly"])
    creds.refresh(gart.Request())
    return creds.token


def run_report(tok: str, body: dict) -> dict:
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{PROPERTY_ID}:runReport"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {tok}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def rows_of(res: dict) -> list:
    return res.get("rows", [])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28)
    a = ap.parse_args()
    rng = [{"startDate": f"{a.days}daysAgo", "endDate": "yesterday"}]
    tok = token()

    # ① 合計
    total = run_report(tok, {"dateRanges": rng, "metrics": [
        {"name": "sessions"}, {"name": "activeUsers"}, {"name": "screenPageViews"}]})
    t = rows_of(total)
    totals = ({"sessions": int(t[0]["metricValues"][0]["value"]),
               "users": int(t[0]["metricValues"][1]["value"]),
               "pageviews": int(t[0]["metricValues"][2]["value"])}
              if t else {"sessions": 0, "users": 0, "pageviews": 0})

    # ② 日次推移
    daily = run_report(tok, {"dateRanges": rng,
                             "dimensions": [{"name": "date"}],
                             "metrics": [{"name": "sessions"}, {"name": "activeUsers"}],
                             "orderBys": [{"dimension": {"dimensionName": "date"}}]})
    series = [{"date": f"{r['dimensionValues'][0]['value'][:4]}-"
                       f"{r['dimensionValues'][0]['value'][4:6]}-"
                       f"{r['dimensionValues'][0]['value'][6:]}",
               "sessions": int(r["metricValues"][0]["value"]),
               "users": int(r["metricValues"][1]["value"])} for r in rows_of(daily)]

    # ③ 流入元
    ch = run_report(tok, {"dateRanges": rng,
                          "dimensions": [{"name": "sessionDefaultChannelGroup"}],
                          "metrics": [{"name": "sessions"}],
                          "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
                          "limit": 8})
    channels = [{"name": r["dimensionValues"][0]["value"],
                 "sessions": int(r["metricValues"][0]["value"])} for r in rows_of(ch)]

    # ④ よく見られたページ
    pg = run_report(tok, {"dateRanges": rng,
                          "dimensions": [{"name": "pagePath"}],
                          "metrics": [{"name": "screenPageViews"}],
                          "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
                          "limit": 10})
    pages = [{"path": r["dimensionValues"][0]["value"],
              "views": int(r["metricValues"][0]["value"])} for r in rows_of(pg)]

    data = {
        "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "period_days": a.days,
        "property_id": PROPERTY_ID,
        "totals": totals,
        "daily": series,
        "channels": channels,
        "pages": pages,
        # 🔴 「取れなかった日」と「0だった日」を混ぜないための印
        "status": "ok",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"書き出し: {OUT}")
    print(f"  直近{a.days}日  セッション {totals['sessions']} / ユーザー {totals['users']} / PV {totals['pageviews']}")
    print(f"  日次 {len(series)} 日ぶん / 流入元 {len(channels)} 種 / ページ {len(pages)} 件")
    if not series:
        print("🔴 日次が0件。取得失敗と『本当に0』は別なので、status を確認すること")


if __name__ == "__main__":
    main()
