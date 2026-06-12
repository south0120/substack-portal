#!/usr/bin/env python3
"""全ライターの過去記事URLを Substack アーカイブAPI から収集し、D1 投入用 SQL を生成する。

- RSS は直近 ~20件しか返さないため、/api/v1/archive (offset ページング) で全履歴を取得
- 日本語フィルタ（ひらがな3文字以上）・url 重複は ON CONFLICT DO NOTHING で D1 側でも防護
- 出力: worker/backfill/backfill_XX.sql（2000行ごとに分割）

使い方:
  python3 scripts/backfill_archive.py            # 収集 + SQL生成
  npx wrangler d1 execute fyl-articles --remote --file=worker/backfill/backfill_01.sql  # 投入
"""

import hashlib
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEEDS = ROOT / "feeds.json"
OUT_DIR = ROOT / "worker" / "backfill"
STATE = OUT_DIR / "collected.json"  # 再実行時のレジューム用キャッシュ

UA = "Mozilla/5.0 (compatible; find-your-letter-backfill/1.0; +https://findyourletter.com)"
PAGE = 50
MAX_PAGES = 40          # 1ライターあたり最大 2000 記事
SLEEP = 0.4
ROWS_PER_FILE = 2000
HIRA = re.compile(r"[ぁ-ゟ]")
TAG = re.compile(r"<[^>]*>")


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read())


def base_url(feed_url):
    return re.sub(r"/feed/?$", "", feed_url.rstrip("/"))


def is_ja(text):
    return len(HIRA.findall(text or "")) >= 3


def excerpt_of(desc):
    text = TAG.sub(" ", desc or "")
    return re.sub(r"\s+", " ", text).strip()[:120]


def collect_writer(feed):
    rows = []
    base = base_url(feed["feed_url"])
    cats = feed.get("categories") or [feed.get("category", "その他")]
    category = cats[0] if isinstance(cats, list) else cats
    for page in range(MAX_PAGES):
        url = f"{base}/api/v1/archive?sort=new&offset={page * PAGE}&limit={PAGE}"
        try:
            posts = get_json(url)
        except Exception as e:
            print(f"    page {page}: {e}", file=sys.stderr)
            break
        if not posts:
            break
        for p in posts:
            title = (p.get("title") or "").strip()
            curl = (p.get("canonical_url") or "").strip()
            desc = excerpt_of(p.get("description") or "")
            if not title or not curl or not is_ja(f"{title}{desc}"):
                continue
            published = p.get("post_date") or ""
            try:
                published = datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            except Exception:
                published = datetime.now(timezone.utc).isoformat()
            rows.append({
                "id": hashlib.sha256(curl.encode()).hexdigest(),
                "url": curl,
                "title": title,
                "excerpt": desc,
                "image": (p.get("cover_image") or "").strip(),
                "published": published,
                "writer": feed["name"],
                "category": category,
            })
        if len(posts) < PAGE:
            break
        time.sleep(SLEEP)
    return rows


def sql_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def main():
    feeds = json.loads(FEEDS.read_text())["feeds"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    collected = json.loads(STATE.read_text()) if STATE.exists() else {}

    for i, feed in enumerate(feeds, 1):
        name = feed["name"]
        if name in collected:
            continue
        print(f"[{i}/{len(feeds)}] {name}")
        try:
            collected[name] = collect_writer(feed)
            print(f"    {len(collected[name])} 記事")
        except Exception as e:
            print(f"    FAILED: {e}", file=sys.stderr)
            collected[name] = []
        STATE.write_text(json.dumps(collected, ensure_ascii=False))
        time.sleep(SLEEP)

    # 重複排除して SQL 生成
    seen = set()
    rows = []
    for name, items in collected.items():
        for r in items:
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            rows.append(r)
    rows.sort(key=lambda r: r["published"], reverse=True)
    print(f"total unique articles: {len(rows)}")

    for f in OUT_DIR.glob("backfill_*.sql"):
        f.unlink()
    for chunk_index in range(0, len(rows), ROWS_PER_FILE):
        chunk = rows[chunk_index:chunk_index + ROWS_PER_FILE]
        lines = []
        for r in chunk:
            values = ", ".join(sql_quote(r[k]) for k in ("id", "url", "title", "excerpt", "image", "published", "writer", "category"))
            lines.append(
                f"INSERT INTO articles (id, url, title, excerpt, image, published, writer, category) VALUES ({values}) ON CONFLICT(url) DO NOTHING;"
            )
        out = OUT_DIR / f"backfill_{chunk_index // ROWS_PER_FILE + 1:02d}.sql"
        out.write_text("\n".join(lines) + "\n")
        print(f"wrote {out.name}: {len(chunk)} rows")


if __name__ == "__main__":
    main()
