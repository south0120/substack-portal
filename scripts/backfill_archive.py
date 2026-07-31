#!/usr/bin/env python3
"""全ライターの過去記事URLを Substack アーカイブAPI から収集し、D1 投入用 SQL を生成する。

- RSS は直近 ~20件しか返さないため、/api/v1/archive (offset ページング) で全履歴を取得
- 日本語フィルタ（ひらがな3文字以上）・url 重複は D1 側でも防護
- 出力: worker/backfill/backfill_XX.sql（2000行ごとに分割）

- 429（レート制限）は指数バックオフで再試行し、それでも駄目なら「取得失敗」として
  worker/backfill/failures.json に記録する。成功した書き手だけが collected.json に入るので、
  失敗した書き手は次回実行時に自動でリトライされる（「0件」と「取り損ね」を区別する）

使い方:
  python3 scripts/backfill_archive.py            # 収集 + SQL生成（前回の失敗ぶんも自動リトライ）
  python3 scripts/backfill_archive.py --refresh  # キャッシュを無視して再収集 + SQL生成
  npx wrangler d1 execute fyl-articles --remote --file=worker/backfill/backfill_01.sql  # 投入
"""

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEEDS = ROOT / "feeds.json"
OUT_DIR = ROOT / "worker" / "backfill"
STATE = OUT_DIR / "collected.json"  # 再実行時のレジューム用キャッシュ（成功した書き手だけ）
FAILURES = OUT_DIR / "failures.json"  # 取得に失敗した書き手（次回実行で自動リトライ）

UA = "Mozilla/5.0 (compatible; find-your-letter-backfill/1.0; +https://findyourletter.com)"
PAGE = 50
MAX_PAGES = 40          # 1ライターあたり最大 2000 記事
SLEEP = 1.5             # Substack の 429 を誘発しない間隔（0.4 だと大量に弾かれる）
ROWS_PER_FILE = 2000
HIRA = re.compile(r"[ぁ-ゟ]")
TAG = re.compile(r"<[^>]*>")


def get_json(url, _tries=6):
    """429 は指数バックオフ（5s→10s→…最大120s）で再試行する。他のエラーは即座に投げる。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    delay = 5
    for attempt in range(_tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < _tries - 1:
                print(f"    429 → {delay}s待って再試行 ({attempt + 1}/{_tries - 1})", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            raise


def base_url(feed_url):
    return re.sub(r"/feed/?$", "", feed_url.rstrip("/"))


def is_ja(text):
    return len(HIRA.findall(text or "")) >= 3


def excerpt_of(desc):
    text = TAG.sub(" ", desc or "")
    return re.sub(r"\s+", " ", text).strip()[:120]


def _real_audio(items):
    return any((it or {}).get("type") != "tts" for it in (items or []))


def collect_writer(feed):
    """(rows, error) を返す。error が None のときだけ「全ページ辿り切れた完全な結果」。

    途中のページ取得に失敗した場合、部分的な rows を正常値として返してはいけない。
    返してしまうと「本当に0件の書き手」と「429で取り損ねた書き手」が区別できなくなり、
    欠損に気づけないまま collected.json に取得済みとして焼き付いてしまう。
    """
    rows = []
    base = base_url(feed["feed_url"])
    cats = feed.get("categories") or [feed.get("category", "その他")]
    category = cats[0] if isinstance(cats, list) else cats
    # archive?sort=new は offset=0 で 50未満(例23件)を返すことがあるが、ページは続く。
    # 固定の page*PAGE では「最初の短いページ＝最後」と誤判定して打ち切ってしまうため、
    # 実際の取得件数で offset を進め、空が返るまで続ける（重複は呼び出し側の seen で除外）。
    offset = 0
    for _ in range(MAX_PAGES * 2):  # 反復回数の安全上限
        url = f"{base}/api/v1/archive?sort=new&offset={offset}&limit={PAGE}"
        try:
            posts = get_json(url)
        except Exception as e:
            # バックオフ後もダメだった＝この書き手は「取り損ね」。部分結果は確定させない。
            print(f"    offset {offset}: {e}", file=sys.stderr)
            return rows, f"offset {offset}: {e}"
        if not posts:
            break
        for p in posts:
            title = (p.get("title") or "").strip()
            curl = (p.get("canonical_url") or "").strip()
            desc = excerpt_of(p.get("description") or "")
            if not title or not curl or not is_ja(f"{title}{desc}"):
                continue
            published = p.get("post_date")
            try:
                published = datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            except (AttributeError, TypeError, ValueError):
                published = None
            is_audio = 1 if (p.get("podcast_url") or p.get("type") == "podcast" or _real_audio(p.get("audio_items"))) else 0
            rows.append({
                "id": hashlib.sha256(curl.encode()).hexdigest(),
                "url": curl,
                "title": title,
                "excerpt": desc,
                "image": (p.get("cover_image") or "").strip(),
                "published": published,
                "writer": feed["name"],
                "category": category,
                "is_audio": is_audio,
            })
        offset += len(posts)  # 実際の取得件数で前進（offset=0 の短いページでも続行）
        time.sleep(SLEEP)
    return rows, None


def sql_quote(value):
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="ignore collected.json and re-fetch every writer")
    args = parser.parse_args()

    feeds = json.loads(FEEDS.read_text())["feeds"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    collected = {} if args.refresh else (json.loads(STATE.read_text()) if STATE.exists() else {})
    failures = {} if args.refresh else (json.loads(FAILURES.read_text()) if FAILURES.exists() else {})
    if failures:
        print(f"前回失敗した {len(failures)} 名を再取得します")

    succeeded = 0
    for i, feed in enumerate(feeds, 1):
        name = feed["name"]
        if name in collected:
            continue
        print(f"[{i}/{len(feeds)}] {name}")
        rows, error = collect_writer(feed)
        if error is None:
            collected[name] = rows
            failures.pop(name, None)  # 復活したので失敗リストから外す
            succeeded += 1
            print(f"    {len(rows)} 記事")
        else:
            # collected には入れない＝次回実行で自動的に再取得対象になる
            failures[name] = {
                "reason": error,
                "last_attempt": datetime.now(timezone.utc).isoformat(),
                "partial_rows": len(rows),
            }
            print(f"    FAILED: {error}（部分取得 {len(rows)} 件は破棄）", file=sys.stderr)
        if not args.refresh:
            STATE.write_text(json.dumps(collected, ensure_ascii=False))
            FAILURES.write_text(json.dumps(failures, ensure_ascii=False, indent=2))
        time.sleep(SLEEP)

    FAILURES.write_text(json.dumps(failures, ensure_ascii=False, indent=2))
    if failures:
        print(f"\n⚠️ 取得できなかった書き手: {len(failures)} 名", file=sys.stderr)
        for name in list(failures)[:20]:
            print(f"    - {name}: {failures[name]['reason']}", file=sys.stderr)
        if len(failures) > 20:
            print(f"    …ほか {len(failures) - 20} 名", file=sys.stderr)
        print(f"{FAILURES} に記録しました。再実行で自動リトライされます。", file=sys.stderr)

    if args.refresh and failures:
        print(f"refresh aborted: {len(failures)} writer(s) failed; cache and SQL were not replaced", file=sys.stderr)
        return 1
    STATE.write_text(json.dumps(collected, ensure_ascii=False))

    # 重複排除して SQL 生成
    seen = set()
    rows = []
    for name, items in collected.items():
        for r in items:
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            rows.append(r)
    rows.sort(key=lambda r: r["published"] or "", reverse=True)
    print(f"\n成功 {succeeded}名 / 失敗 {len(failures)}名 / 記事 {len(rows)}件（重複除外後）")

    for f in OUT_DIR.glob("backfill_*.sql"):
        f.unlink()
    for chunk_index in range(0, len(rows), ROWS_PER_FILE):
        chunk = rows[chunk_index:chunk_index + ROWS_PER_FILE]
        lines = []
        for r in chunk:
            values = ", ".join(sql_quote(r[k]) for k in ("id", "url", "title", "excerpt", "image", "published", "writer", "category", "is_audio"))
            lines.append(
                f"INSERT INTO articles (id, url, title, excerpt, image, published, writer, category, is_audio) VALUES ({values}) "
                "ON CONFLICT(url) DO UPDATE SET "
                "published=COALESCE(excluded.published, articles.published), "
                "title=excluded.title, excerpt=excluded.excerpt, image=excluded.image, is_audio=excluded.is_audio;"
            )
        out = OUT_DIR / f"backfill_{chunk_index // ROWS_PER_FILE + 1:02d}.sql"
        out.write_text("\n".join(lines) + "\n")
        print(f"wrote {out.name}: {len(chunk)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
