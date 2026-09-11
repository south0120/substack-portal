#!/usr/bin/env python3
"""FYL: cursor を細かく記録する（周回の曖昧さを消すため）。

🔑 なぜ要るか（2026-09-11）:
    `reports/daily_stats.csv` は **1日1回**しか記録しない。
    cursor は feeds 数（約2,700）で**折り返す**ので、日次スナップショットの差では
    「1回も進まなかった」と「ちょうど1周した」が **原理的に区別できない**。
    実例: 08-29 と 08-30 の cursor がどちらも 1222。どちらの解釈も成り立ってしまった。
    → **記録を細かくすれば、折り返しが見える**。

🔴 記録するのは cursor が主目的。articles/writers は【キャッシュ値】なので列名で明示する。
    2026-09-08 の修正(c3f3ccb)以降、/api/health の articles/writers は meta のキャッシュで、
    TTL 20時間 ＝ 実質1日1回しか更新されない。
    🛑 **1時間おきに記録しても同じ値が並ぶ**ので、新しい値の顔で並べないこと。
    🛑 正確な件数が欲しいからといって COUNT(*) を戻さないこと（**元の障害に戻る**）。

🛑 /api/health を叩くが、c3f3ccb 以降は meta を数行 読むだけなので安い。
    （修正前なら 1回 約73,743行＝1時間おきで1日177万行。この記録は成立しなかった）
"""
import csv, datetime, json, os, pathlib, urllib.request

HEALTH_URL = os.environ.get("FYL_HEALTH_URL", "https://fyl-api.south0120.workers.dev/api/health")
OUT = pathlib.Path(__file__).resolve().parent.parent / "reports" / "cursor_hourly.csv"
FIELDS = ["recorded_at_jst", "http", "cursor", "articles_cached", "counts_at", "last_run"]


def fetch():
    """(http_status, body_or_None) を返す。**例外で落ちない**。

    🔴🔴 落ちてはいけない理由（2026-09-11 実際に踏んだ）:
        D1 が枯れている時間帯は /api/health が 500 を返す。
        例外で落ちると **その回の行が1つも残らない** ＝ ファイル上は「何も無かった」に見える。
        ＝ **いちばん観測したい時間帯（枯れている時）だけ、記録が欠ける**。
        ＝ 朝の健全な窓だけが残り、**私たちが避けようとしたサンプリングの偏りが、そのまま出る**。
    ✅ so 失敗も**1行として記録する**（http 列に status を入れる）。
       欠測と「落ちていた」は別物。**行が在って http=500 なら、それは立派なデータ**。
    """
    req = urllib.request.Request(HEALTH_URL, headers={"User-Agent": "fyl-cursor-hourly"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None          # 0 = 到達できなかった（DNS/timeout 等）


def main():
    status, h = fetch()
    h = h or {}
    jst = datetime.timezone(datetime.timedelta(hours=9))
    row = {
        "recorded_at_jst": datetime.datetime.now(jst).strftime("%Y-%m-%d %H:%M"),
        "http": str(status),
        "cursor": str(h.get("cursor", "")),
        "articles_cached": str(h.get("articles", "")),   # 🔴 キャッシュ値
        "counts_at": str(h.get("countsAt") or ""),        # 🔑 その件数がいつ時点か
        "last_run": str(h.get("lastRun") or ""),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    new = not OUT.exists()
    with OUT.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    print(f"recorded {row['recorded_at_jst']}: http={row['http']} cursor={row['cursor']} "
          f"articles_cached={row['articles_cached']} counts_at={row['counts_at']}")


if __name__ == "__main__":
    main()
