#!/usr/bin/env python3
"""Fetch all Substack RSS feeds via fyl-api Worker proxy and write to Cloudflare D1."""

from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = ROOT / "feeds.json"

SLEEP_BETWEEN_FEEDS = 0.3
EXCERPT_LENGTH = 120
D1_BATCH_SIZE = 40
POSTS_PER_FEED = 20

HIRAGANA = re.compile(r"[ぁ-ゟ]")
USER_AGENT = "find-your-letter/1.0 (+https://findyourletter.com)"


def sha256_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def is_japanese(text: str) -> bool:
    return len(HIRAGANA.findall(text)) >= 3


def feed_categories(feed: dict) -> list[str]:
    value = feed.get("categories", feed.get("category", []))
    if isinstance(value, str):
        return [value] if value else []
    return [item for item in value if isinstance(item, str) and item]


# --- XML helpers ---

def _first_tag(xml: str, tag: str) -> str:
    m = re.search(rf"<{re.escape(tag)}\b[^>]*>([\s\S]*?)</{re.escape(tag)}>", xml, re.IGNORECASE)
    return m.group(1) if m else ""


def _all_tags(xml: str, tag: str) -> list[str]:
    return [m.group(1) for m in re.finditer(
        rf"<{re.escape(tag)}\b[^>]*>([\s\S]*?)</{re.escape(tag)}>", xml, re.IGNORECASE
    )]


def _clean_text(value: str) -> str:
    cdata = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", value or "")
    text = re.sub(r"<[^>]*>", " ", cdata)
    text = re.sub(r"\s+", " ", text).strip()
    return _decode_entities(text)


def _strip_html(value: str) -> str:
    cdata = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", value or "")
    text = _decode_entities(cdata)
    text = re.sub(r"<[^>]*>", " ", text)
    return _decode_entities(re.sub(r"\s+", " ", text).strip())


_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


def _decode_entities(value: str) -> str:
    def replace(m: re.Match) -> str:
        code = m.group(1)
        lower = code.lower()
        if lower in _ENTITIES:
            return _ENTITIES[lower]
        if lower.startswith("#x"):
            n = int(lower[2:], 16)
        elif lower.startswith("#"):
            n = int(lower[1:])
        else:
            return m.group(0)
        return chr(n) if 0 <= n <= 0x10FFFF else m.group(0)
    return re.sub(r"&(#x[0-9a-fA-F]+|#\d+|amp|lt|gt|quot|apos);", replace, value or "")


# --- Proxy fetch ---

def fetch_via_proxy(feed: dict, proxy_base: str, proxy_secret: str) -> tuple[str, str, list[dict]]:
    """Fetch RSS via fyl-api Worker proxy (bypasses Substack IP blocking of GitHub Actions)."""
    feed_url = feed["feed_url"]
    target = f"{proxy_base}/api/proxy?url={urllib.parse.quote(feed_url, safe='')}"

    req = urllib.request.Request(
        target,
        headers={
            "x-proxy-secret": proxy_secret,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as r:
            xml = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Proxy HTTP {e.code} for {feed_url}") from e

    channel = _first_tag(xml, "channel")
    if not channel:
        raise RuntimeError(f"RSS channel not found for {feed_url}")

    channel_no_items = re.sub(r"<item\b[\s\S]*?</item>", "", channel, flags=re.IGNORECASE)
    image_block = _first_tag(channel_no_items, "image")
    site_url = _clean_text(_first_tag(channel_no_items, "link")) or _fallback_site_url(feed_url)
    avatar = _clean_text(_first_tag(image_block, "url"))

    categories = feed_categories(feed)
    category = categories[0] if categories else ""
    now = datetime.now(timezone.utc).isoformat()
    articles: list[dict] = []

    for item in _all_tags(channel, "item"):
        title = _clean_text(_first_tag(item, "title"))
        url = _clean_text(_first_tag(item, "link"))
        excerpt = _strip_html(_first_tag(item, "description"))[:EXCERPT_LENGTH]
        if not title or not url or not is_japanese(f"{title}{excerpt}"):
            continue
        enc = re.search(
            r'<enclosure\b[^>]*\burl\s*=\s*(?:"([^"]*)"|\'([^\']*)\')[^>]*>', item, re.IGNORECASE
        )
        pub_raw = _clean_text(_first_tag(item, "pubDate"))
        try:
            pub = email.utils.parsedate_to_datetime(pub_raw).isoformat()
        except Exception:
            pub = now
        articles.append({
            "id": sha256_id(url),
            "url": url,
            "title": title,
            "excerpt": excerpt,
            "image": _decode_entities(enc.group(1) or enc.group(2)) if enc else "",
            "published": pub,
            "writer": feed["name"],
            "category": category,
        })

    return site_url, avatar, articles


def _fallback_site_url(feed_url: str) -> str:
    try:
        p = urllib.parse.urlsplit(feed_url)
        return urllib.parse.urlunsplit((p.scheme, p.netloc, "/", "", ""))
    except Exception:
        return feed_url


# --- D1 helpers ---

def d1_query(account_id: str, database_id: str, token: str, queries: list[dict] | dict) -> dict:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
    body = json.dumps(queries).encode()
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
        raise RuntimeError(f"D1 API HTTP {e.code}: {body_text}") from e


def upsert_writer(account_id: str, db_id: str, token: str, feed: dict, site_url: str, avatar: str) -> None:
    categories = feed_categories(feed)
    d1_query(account_id, db_id, token, {
        "sql": (
            "INSERT INTO writers (name,url,feed_url,avatar,bio,categories,updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET url=excluded.url,feed_url=excluded.feed_url,"
            "avatar=CASE WHEN excluded.avatar!='' THEN excluded.avatar ELSE avatar END,"
            "bio=excluded.bio,categories=excluded.categories,updated_at=excluded.updated_at"
        ),
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
        params = [
            v for a in batch
            for v in (a["id"], a["url"], a["title"], a["excerpt"], a["image"], a["published"], a["writer"], a["category"])
        ]
        d1_query(account_id, db_id, token, {
            "sql": f"INSERT INTO articles (id,url,title,excerpt,image,published,writer,category) VALUES {placeholders} ON CONFLICT(url) DO NOTHING",
            "params": params,
        })


def main() -> int:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    db_id = os.environ.get("D1_DATABASE_ID", "059349fc-d32a-4422-93de-af77b7a7317f")
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    proxy_base = os.environ.get("FYL_PROXY_URL", "https://fyl-api.south0120.workers.dev").rstrip("/")
    proxy_secret = os.environ.get("FYL_PROXY_SECRET", "")

    if not account_id or not token:
        print("ERROR: CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are required", file=sys.stderr)
        return 1
    if not proxy_secret:
        print("ERROR: FYL_PROXY_SECRET is required", file=sys.stderr)
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
            site_url, avatar, articles = fetch_via_proxy(feed, proxy_base, proxy_secret)
            upsert_writer(account_id, db_id, token, feed, site_url, avatar)
            if articles:
                upsert_articles(account_id, db_id, token, articles)
            total_articles += len(articles)
            successes += 1
            print(f"  → {len(articles)} articles")
        except RuntimeError as e:
            print(f"  WARN [D1]: {e}", file=sys.stderr)
        except Exception as e:
            print(f"  WARN [RSS]: {type(e).__name__}: {e}", file=sys.stderr)

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
