#!/usr/bin/env python3
"""Fetch curated writers' RSS feeds into the single GitHub Pages data file."""

from __future__ import annotations

import json
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
OUT_FILE = ROOT / "docs" / "data" / "articles.json"

UA = "substack-shelf/1.0 (+https://github.com/south0120/substack-shelf)"
TIMEOUT_SECONDS = 20
SLEEP_SECONDS = 0.5
MAX_ARTICLES = 400
LATEST_PER_WRITER = 3
EXCERPT_LENGTH = 120

HIRAGANA = re.compile(r"[\u3041-\u309f]")
HTML_TAG = re.compile(r"<[^>]*>")
WHITESPACE = re.compile(r"\s+")


def strip_html(value: str) -> str:
    """Return compact plain text suitable for excerpts and language checks."""
    text = HTML_TAG.sub(" ", value or "")
    return WHITESPACE.sub(" ", unescape(text)).strip()


def is_japanese(title: str, description: str) -> bool:
    """Keep articles containing at least three hiragana characters."""
    return len(HIRAGANA.findall(f"{title}{description}")) >= 3


def parse_published(value: str) -> str:
    """Normalize an RSS date to an ISO 8601 UTC timestamp."""
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return datetime.now(timezone.utc).isoformat()


def fallback_site_url(feed_url: str) -> str:
    """Derive a useful writer URL before a feed has been fetched."""
    parts = urlsplit(feed_url)
    path = parts.path
    if path.rstrip("/").endswith("/feed"):
        path = path.rstrip("/")[:-5] or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=_SSL_CONTEXT) as response:
        return response.read()


def feed_categories(feed: dict[str, object]) -> list[str]:
    """Normalize current array and legacy string category schemas."""
    value = feed.get("categories", feed.get("category", []))
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def parse_feed(xml_bytes: bytes, writer: dict[str, object]) -> tuple[str, str, list[dict[str, str]]]:
    """Parse one RSS 2.0 feed and return site URL, avatar, and Japanese articles."""
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("RSS channel not found")

    site_url = (channel.findtext("link") or "").strip() or fallback_site_url(writer["feed_url"])
    avatar = (channel.findtext("image/url") or "").strip()
    articles: list[dict[str, str]] = []

    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        description = strip_html(item.findtext("description") or "")
        if not title or not url or not is_japanese(title, description):
            continue

        image = ""
        enclosure = item.find("enclosure")
        if enclosure is not None:
            image = (enclosure.get("url") or "").strip()

        articles.append(
            {
                "title": title,
                "url": url,
                "excerpt": description[:EXCERPT_LENGTH],
                "published": parse_published(item.findtext("pubDate") or ""),
                "image": image,
                "writer": writer["name"],
                "category": str(writer["categories"][0]),
            }
        )

    articles.sort(key=lambda article: article["published"], reverse=True)
    return site_url, avatar, articles


def unique_categories(feeds: list[dict[str, object]]) -> list[str]:
    return list(dict.fromkeys(category for feed in feeds for category in feed_categories(feed)))


def main() -> int:
    feeds = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))["feeds"]
    writers: list[dict[str, object]] = []
    all_articles: list[dict[str, str]] = []

    for index, feed in enumerate(feeds):
        categories = feed_categories(feed)
        if not categories:
            print(f"Warning: skipped {feed['name']}: no categories configured", file=sys.stderr)
            continue
        writer: dict[str, object] = {
            "name": feed["name"],
            "url": fallback_site_url(feed["feed_url"]),
            "feed_url": feed["feed_url"],
            "categories": categories,
            "bio": feed["bio"],
            "avatar": "",
            "latest": [],
        }
        print(f"Fetching: {feed['feed_url']}")
        try:
            site_url, avatar, articles = parse_feed(fetch_bytes(feed["feed_url"]), feed)
            writer["url"] = site_url
            writer["avatar"] = avatar
            writer["latest"] = [
                {key: article[key] for key in ("title", "url", "published")}
                for article in articles[:LATEST_PER_WRITER]
            ]
            all_articles.extend(articles)
            print(f"  kept {len(articles)} Japanese article(s)")
        except Exception as error:
            print(f"Warning: skipped {feed['name']}: {error}", file=sys.stderr)

        writers.append(writer)
        if index < len(feeds) - 1:
            time.sleep(SLEEP_SECONDS)

    all_articles.sort(key=lambda article: article["published"], reverse=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "categories": unique_categories(feeds),
        "writers": writers,
        "articles": all_articles[:MAX_ARTICLES],
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUT_FILE} with {len(writers)} writers and {len(payload['articles'])} articles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
