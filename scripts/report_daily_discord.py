#!/usr/bin/env python3
"""FYL 日次Discordレポート用のメッセージ本文を生成する。

/api/health（累計の記事数・ライター数）と /api/writers（書き手名一覧）を取得し、
前回スナップショット（固定パス）と比較して「新着記事数・新規発見ライター（名前付き）」
を算出。Discord投稿用の本文を標準出力に出す。実行のたびにスナップショットを更新する。

cron（毎朝8:00 JST）から実行し、出力をDiscordへ投稿する想定。
"""
import json, os, ssl, sys, urllib.request
from datetime import datetime, timezone, timedelta

HEALTH = os.environ.get("FYL_HEALTH_URL", "https://fyl-api.south0120.workers.dev/api/health")
WRITERS = os.environ.get("FYL_WRITERS_URL", "https://fyl-api.south0120.workers.dev/api/writers?limit=2000")
SNAP = os.environ.get("FYL_SNAPSHOT", "/Users/dev/agents/alex/fyl_daily_snapshot.json")
JST = timezone(timedelta(hours=9))


def _ctx(verify=True):
    if not verify:
        return ssl._create_unverified_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fyl-daily-report"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ctx()) as r:
            return json.loads(r.read())
    except (ssl.SSLError, urllib.error.URLError):
        # certifi が無い環境向けフォールバック（urllibはSSLErrorをURLErrorで包むため両方捕捉）
        with urllib.request.urlopen(req, timeout=30, context=_ctx(verify=False)) as r:
            return json.loads(r.read())


def main():
    health = get(HEALTH)
    try:
        wdata = get(WRITERS)
        names = sorted({w.get("name") for w in (wdata.get("writers") or []) if w.get("name")})
    except Exception:
        names = []

    today = {
        "date": datetime.now(JST).strftime("%Y-%m-%d"),
        "articles": int(health.get("articles", 0)),
        "writers": int(health.get("writers", 0)),
        "names": names,
    }

    prev = None
    if os.path.exists(SNAP):
        try:
            prev = json.load(open(SNAP, encoding="utf-8"))
        except Exception:
            prev = None

    d = datetime.now(JST).strftime("%-m/%-d")
    lines = [f"📊 **Find Your Letter 日次レポート（{d}）**", ""]
    if prev:
        da = today["articles"] - int(prev.get("articles", 0))
        dw = today["writers"] - int(prev.get("writers", 0))
        new_names = [n for n in names if n not in set(prev.get("names") or [])] if names else []
        lines.append(f"🆕 新着記事数：**+{da}**　（累計 {today['articles']:,}）")
        lines.append(f"👤 新規発見ライター：**+{dw}**　（累計 {today['writers']}）")
        if new_names:
            shown = new_names[:25]
            lines.append("")
            lines.append("**新しく加わった書き手：**")
            lines.append("・" + "\n・".join(shown))
            if len(new_names) > len(shown):
                lines.append(f"…ほか {len(new_names) - len(shown)} 名")
    else:
        lines.append(f"記事数：**{today['articles']:,}** ／ ライター数：**{today['writers']}**")
        lines.append("_（初回記録。明日から前日比＋新規書き手を表示します）_")

    json.dump(today, open(SNAP, "w", encoding="utf-8"), ensure_ascii=False)
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
