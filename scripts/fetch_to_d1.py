#!/usr/bin/env python3
"""Fetch all Substack feeds via substack-api and write directly to Cloudflare D1 via REST API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

try:
    from substack_api import Newsletter
    _HAS_SUBSTACK_API = True
except ImportError:
    _HAS_SUBSTACK_API = False

ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = ROOT / "feeds.json"

SLEEP_BETWEEN_FEEDS = 0.3
EXCERPT_LENGTH = 120
D1_BATCH_SIZE = 40
POSTS_PER_FEED = 20

HIRAGANA = re.compile(r"[ぁ-ゟ]")


def sha256_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def is_japanese(text: str) -> bool:
    return len(HIRAGANA.findall(text)) >= 3


def feed_categories(feed: dict) -> list[str]:
    value = feed.get("categories", feed.get("category", []))
    if isinstance(value, str):
        return [value] if value else []
    return [item for item in value if isinstance(item, str) and item]


def substack_base_url(feed_url: str) -> str:
    parts = urlsplit(feed_url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def fetch_via_api(feed: dict) -> tuple[str, str, list[dict]]:
    """Fetch recent posts via Substack JSON API (avoids RSS IP blocking)."""
    base_url = substack_base_url(feed["feed_url"])
    nl = Newsletter(base_url)
    items = nl._fetch_paginated_posts({"sort": "new"}, limit=POSTS_PER_FEED)

    avatar = ""
    if items and items[0].get("publishedBylines"):
        avatar = items[0]["publishedBylines"][0].get("photo_url") or ""

    categories = feed_categories(feed)
    category = categories[0] if categories else ""

    articles: list[dict] = []
    for item in items:
        title = (item.get("title") or "").strip()
        url = (item.get("canonical_url") or "").strip()
        body_text = (item.get("truncated_body_text") or "").strip()

        if not title or not url or not is_japanese(f"{title}{body_text}"):
            continue

        articles.append({
            "id": sha256_id(url),
            "url": url,
            "title": title,
            "excerpt": body_text[:EXCERPT_LENGTH],
            "image": item.get("cover_image") or "",
            "published": item.get("post_date") or datetime.now(timezone.utc).isoformat(),
            "writer": feed["name"],
            "category": category,
        })

    return base_url, avatar, articles


def d1_query(account_id: str, database_id: str, token: str, queries: list[dict]) -> dict:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
    body = json.dumps(queries[0] if len(queries) == 1 else queries).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"D1 API HTTP {e.code} for account={account_id}: {body_text}") from e


def upsert_writer(account_id: str, db_id: str, token: str, feed: dict, site_url: str, avatar: str) -> None:
    categories = feed_categories(feed)
    d1_query(account_id, db_id, token, {
        "sql": "INSERT INTO writers (name,url,feed_url,avatar,bio,categories,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET url=excluded.url,feed_url=excluded.feed_url,avatar=CASE WHEN excluded.avatar!='' THEN excluded.avatar ELSE avatar END,bio=excluded.bio,categories=excluded.categories,updated_at=excluded.updated_at",
        "params": [
            feed["name"], site_url, feed["feed_url"], avatar,
            feed.get("bio", ""), json.dumps(categories, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
        ],
    })


def upsert_articles(account_id: str, db_id: str, token: str, articles: list[dict]) -> None:
    for i in range(0, len(articles), D1_BATCH_SIZE):
        batch = articles[i:i + D1_BATCH_SIZE]
        placeholders = ",".join(["(?,?,?,?,?,?,?,?)"] * len(batch))
        params = [v for a in batch for v in (a["id"], a["url"], a["title"], a["excerpt"], a["image"], a["published"], a["writer"], a["category"])]
        d1_query(account_id, db_id, token, {
            "sql": f"INSERT INTO articles (id,url,title,excerpt,image,published,writer,category) VALUES {placeholders} ON CONFLICT(url) DO NOTHING",
            "params": params,
        })


def main() -> int:
    if not _HAS_SUBSTACK_API:
        print("ERROR: substack-api not installed. Run: pip install substack-api", file=sys.stderr)
        return 1

    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    db_id = os.environ.get("D1_DATABASE_ID", "059349fc-d32a-4422-93de-af77b7a7317f")
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")

    if not account_id or not token:
        print("ERROR: CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are required", file=sys.stderr)
        return 1

    feeds = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))["feeds"]
    total_articles = 0
    successes = 0

    for index, feed in enumerate(feeds):
        cats = feed_categories(feed)
        if not cats:
            print(f"  skip {feed['name']}: no categories", file=sys.stderr)
            continue

        print(f"[{index+1}/{len(feeds)}] {feed['name']}")
        try:
            site_url, avatar, articles = fetch_via_api(feed)
            upsert_writer(account_id, db_id, token, feed, site_url, avatar)
            if articles:
                upsert_articles(account_id, db_id, token, articles)
            total_articles += len(articles)
            successes += 1
            print(f"  → {len(articles)} articles")
        except RuntimeError as e:
            print(f"  WARN [D1]: {e}", file=sys.stderr)
        except Exception as e:
            print(f"  WARN [API]: {type(e).__name__}: {e}", file=sys.stderr)

        if index < len(feeds) - 1:
            time.sleep(SLEEP_BETWEEN_FEEDS)

    d1_query(account_id, db_id, token, {
        "sql": "INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        "params": ["last_run", datetime.now(timezone.utc).isoformat()],
    })

    print(f"\nDone: {successes}/{len(feeds)} writers, {total_articles} articles upserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
