#!/usr/bin/env python3
"""Build the GitHub Pages fallback data file (writers + categories) from feeds.json.

記事本体は Cloudflare Worker (fyl-api) が10分ごとに D1 へ取り込み、フロントは API から読む。
`docs/data/articles.json` は API が落ちたときのフォールバックで、そこで必要なのは
**書き手一覧とカテゴリ**（どちらも feeds.json だけで作れる）。

以前はここで全フィードのRSSを直接取得していたが、GitHub Actions の IP は Substack に
弾かれる（403）ため latest/articles は 1872誌中6誌しか埋まらず、毎時1866回の無駄な
リクエストで共有プロキシIPのレート制限を悪化させていた。よってネットワーク取得は廃止。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = ROOT / "feeds.json"
OUT_FILE = ROOT / "docs" / "data" / "articles.json"


def fallback_site_url(feed_url: str) -> str:
    """Derive a useful writer URL from its feed URL."""
    parts = urlsplit(feed_url)
    path = parts.path
    if path.rstrip("/").endswith("/feed"):
        path = path.rstrip("/")[:-5] or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def feed_categories(feed: dict[str, object]) -> list[str]:
    """Normalize current array and legacy string category schemas."""
    value = feed.get("categories", feed.get("category", []))
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def unique_categories(feeds: list[dict[str, object]]) -> list[str]:
    return list(dict.fromkeys(category for feed in feeds for category in feed_categories(feed)))


def main() -> int:
    feeds = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))["feeds"]
    writers: list[dict[str, object]] = []

    for feed in feeds:
        categories = feed_categories(feed)
        if not categories:
            print(f"Warning: skipped {feed['name']}: no categories configured", file=sys.stderr)
            continue
        writers.append(
            {
                "name": feed["name"],
                "url": fallback_site_url(feed["feed_url"]),
                "feed_url": feed["feed_url"],
                "categories": categories,
                "bio": feed.get("bio", ""),
                # avatar と latest は API（D1）が持つ。フォールバックでは空でよく、
                # フロント側も .latest-empty で空状態を描画できる。
                "avatar": "",
                "latest": [],
            }
        )

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "categories": unique_categories(feeds),
        "writers": writers,
        # 記事は API から取る。ここを埋めるには Substack への直接取得が要り、403で埋まらない。
        "articles": [],
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUT_FILE} with {len(writers)} writers (network-free; articles come from the API)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
