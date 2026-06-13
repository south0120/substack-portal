#!/usr/bin/env python3
"""Fetch all RSS feeds and write directly to Cloudflare D1 via REST API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = ROOT / "feeds.json"

UA = "find-your-letter/1.0 (+https://findyourletter.com)"
TIMEOUT_SECONDS = 20
SLEEP_BETWEEN_FEEDS = 0.3
EXCERPT_LENGTH = 120
D1_BATCH_SIZE = 50

HIRAGANA = re.compile(r"[ぁ-ゟ]")
HTML_TAG = re.compile(r"<[^>]*>")
WHITESPACE = re.compile(r"\s+")


def sha256_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def strip_html(value: str) -> str:
    return WHITESPACE.sub(" ", unescape(HTML_TAG.sub(" ", value or ""))).strip()


def is_japanese(title: str, description: str) -> bool:
    return len(HIRAGANA.findall(f"{title}{description}")) >= 3


def parse_published(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return datetime.now(timezone.utc).isoformat()


def fallback_site_url(feed_url: str) -> str:
    parts = urlsplit(feed_url)
    path = parts.path
    if path.rstrip("/").endswith("/feed"):
        path = path.rstrip("/")[:-5] or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml"},
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS, context=_SSL_CONTEXT) as r:
                return r.read()
        except Exception:
            if attempt == 1:
                raise
            time.sleep(1.5)
    raise RuntimeError("unreachable")


def feed_categories(feed: dict) -> list[str]:
    value = feed.get("categories", feed.get("category", []))
    if isinstance(value, str):
        return [value] if value else []
    return [item for item in value if isinstance(item, str) and item]


def parse_feed(xml_bytes: bytes, feed: dict) -> tuple[str, str, list[dict]]:
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("RSS channel not found")

    site_url = (channel.findtext("link") or "").strip() or fallback_site_url(feed["feed_url"])
    avatar = (channel.findtext("image/url") or "").strip()
    articles: list[dict] = []

    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        desc = strip_html(item.findtext("description") or "")
        if not title or not url or not is_japanese(title, desc):
            continue
        enc = item.find("enclosure")
        image = (enc.get("url") or "").strip() if enc is not None else ""
        articles.append({
            "id": sha256_id(url),
            "url": url,
            "title": title,
            "excerpt": desc[:EXCERPT_LENGTH],
            "image": image,
            "published": parse_published(item.findtext("pubDate") or ""),
            "writer": feed["name"],
            "category": feed_categories(feed)[0],
        })

    articles.sort(key=lambda a: a["published"], reverse=True)
    return site_url, avatar, articles


def d1_query(account_id: str, database_id: str, token: str, queries: list[dict]) -> dict:
    """Send a batch of SQL queries to D1 REST API."""
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
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as r:
        return json.loads(r.read())


def upsert_writer(account_id: str, db_id: str, token: str, feed: dict, site_url: str, avatar: str) -> None:
    categories = feed_categories(feed)
    d1_query(account_id, db_id, token, {
        "sql": "INSERT INTO writers (name,url,feed_url,avatar,bio,categories,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET url=excluded.url,feed_url=excluded.feed_url,avatar=excluded.avatar,bio=excluded.bio,categories=excluded.categories,updated_at=excluded.updated_at",
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
            xml_bytes = fetch_bytes(feed["feed_url"])
            site_url, avatar, articles = parse_feed(xml_bytes, feed)
            upsert_writer(account_id, db_id, token, feed, site_url, avatar)
            if articles:
                upsert_articles(account_id, db_id, token, articles)
            total_articles += len(articles)
            successes += 1
            print(f"  → {len(articles)} articles")
        except Exception as e:
            print(f"  WARN: {e}", file=sys.stderr)

        if index < len(feeds) - 1:
            time.sleep(SLEEP_BETWEEN_FEEDS)

    # update last_run in meta
    d1_query(account_id, db_id, token, {
        "sql": "INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        "params": ["last_run", datetime.now(timezone.utc).isoformat()],
    })

    print(f"\nDone: {successes}/{len(feeds)} writers, {total_articles} articles upserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
