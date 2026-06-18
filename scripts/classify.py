#!/usr/bin/env python3
"""Classify each writer's recent articles into topic ratios with Claude."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ssl
import urllib.request

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

ROOT = Path(__file__).resolve().parent.parent
TOPICS_FILE = ROOT / "docs" / "data" / "topics.json"
API_BASE = "https://fyl-api.south0120.workers.dev"

# 棚カテゴリ（index.html の CAT_STYLE）と揃えた分類ラベル。バーの成分がそのまま棚カテゴリに使われる。
TOPIC_LABELS = [
    "AI", "テクノロジー", "ビジネス", "投資・経済", "キャリア・働き方",
    "社会・文化", "ライフスタイル", "子育て・家族", "健康・ウェルネス",
    "教育・学び", "クリエイティブ", "音楽", "グルメ・料理", "旅行・おでかけ",
    "読書", "その他",
]
MAX_ARTICLES_PER_WRITER = 18
EXCERPT_LENGTH = 120
# プロンプト（判定ルール）を更新したらキャッシュを無効化して再分類するためのバージョン
PROMPT_VERSION = "v2"
# ラベル集合が変わったらキャッシュを無効化して再分類するための署名
LABELS_VERSION = hashlib.sha256("|".join(TOPIC_LABELS).encode("utf-8")).hexdigest()[:8]

SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "topic": {"type": "string", "enum": TOPIC_LABELS},
                },
                "required": ["index", "topic"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classifications"],
    "additionalProperties": False,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_existing() -> dict[str, Any]:
    if not TOPICS_FILE.exists():
        return {}
    try:
        data = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as error:
        print(f"Warning: could not read {TOPICS_FILE}: {error}", file=sys.stderr)
        return {}


def source_hash(articles: list[dict[str, Any]]) -> str:
    urls = sorted(str(article.get("url", "")) for article in articles)
    encoded = json.dumps(urls, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prompt_for(articles: list[dict[str, Any]]) -> str:
    lines = [
        "以下の各記事を、その記事の『主題』に最も合うトピック1つに分類してください。",
        "トピック: " + " / ".join(TOPIC_LABELS),
        "",
        "判定ルール:",
        "- 記事が主に何について書かれているか（主題）で選ぶ。手段・道具として軽く触れているだけの話題では選ばない。",
        "- 「AI」「テクノロジー」は、記事の主題がAI・技術そのものの場合のみ選ぶ。"
        "趣味・仕事・運動などでAIやアプリを道具として使っているだけなら、その趣味・仕事・運動の主題で分類する"
        "（例: AIで作ったトライアスロンの記録 → 健康・ウェルネス）。",
        "- スポーツ・運動・健康（トライアスロン/ランニング等）は『健康・ウェルネス』。"
        "集客・売上・マーケティング・流入・起業などは『ビジネス』。",
        "- タイトルだけで判断せず抜粋（内容）も踏まえる。どれにも明確に当てはまらない場合のみ『その他』。",
        "- すべてのindexを1回ずつ分類してください。",
        "",
    ]
    for index, article in enumerate(articles, start=1):
        title = str(article.get("title", "")).strip()
        excerpt = str(article.get("excerpt", "")).strip()[:EXCERPT_LENGTH]
        lines.append(f"{index}. {title} / {excerpt}")
    return "\n".join(lines)


def ratios_from(data: dict[str, Any], sample_n: int) -> list[dict[str, Any]]:
    classifications = data.get("classifications")
    if not isinstance(classifications, list):
        raise ValueError("classifications is not an array")

    by_index: dict[int, str] = {}
    for item in classifications:
        if not isinstance(item, dict):
            raise ValueError("classification item is not an object")
        index = item.get("index")
        topic = item.get("topic")
        if (
            not isinstance(index, int)
            or index < 1
            or index > sample_n
            or topic not in TOPIC_LABELS
            or index in by_index
        ):
            raise ValueError("classification contains an invalid or duplicate index/topic")
        by_index[index] = topic

    if set(by_index) != set(range(1, sample_n + 1)):
        raise ValueError("not every article index was classified")

    counts = Counter(by_index.values())
    topics = [
        {"label": label, "pct": round(count / sample_n * 100, 1)}
        for label, count in counts.items()
        if count
    ]
    return sorted(topics, key=lambda topic: (-topic["pct"], TOPIC_LABELS.index(topic["label"])))


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; skipping topic classification.")
        return 0

    import anthropic

    # Fetch writers from D1 API
    req = urllib.request.Request(f"{API_BASE}/api/writers", headers={"User-Agent": "fyl-classifier/1.0"})
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as r:
        writers_data = json.loads(r.read())
    writers = writers_data.get("writers", [])
    print(f"Fetched {len(writers)} writers from API")

    # Fetch all articles from D1 API via pagination
    articles: list[dict[str, Any]] = []
    page = 1
    while True:
        req = urllib.request.Request(
            f"{API_BASE}/api/articles?limit=200&page={page}",
            headers={"User-Agent": "fyl-classifier/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as r:
            data = json.loads(r.read())
        batch = data.get("articles", [])
        articles.extend(batch)
        if not data.get("hasMore"):
            break
        page += 1
    print(f"Fetched {len(articles)} articles from API")
    existing = load_existing()
    existing_writers = existing.get("writers", {})
    if not isinstance(existing_writers, dict):
        existing_writers = {}

    client = anthropic.Anthropic()
    output_writers: dict[str, Any] = {}

    for writer in writers:
        name = str(writer.get("name", ""))
        recent = sorted(
            (article for article in articles if article.get("writer") == name),
            key=lambda article: str(article.get("published", "")),
            reverse=True,
        )[:MAX_ARTICLES_PER_WRITER]
        if not recent:
            continue

        article_hash = source_hash(recent)
        previous = existing_writers.get(name)
        if (
            isinstance(previous, dict)
            and previous.get("source_hash") == article_hash
            and previous.get("labels_version") == LABELS_VERSION
            and previous.get("prompt_version") == PROMPT_VERSION
        ):
            output_writers[name] = previous
            print(f"Cached: {name}")
            continue

        print(f"Classifying: {name} ({len(recent)} article(s))")
        try:
            prompt_text = prompt_for(recent)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4000,
                system=(
                    "あなたは日本語ニュースレター記事のトピック分類器です。"
                    "各記事を、その記事の主題（主に何について書かれているか）に最も合うトピック1つに分類してください。"
                    "道具・手段として軽く触れているだけの要素では分類しないでください。"
                ),
                messages=[{"role": "user", "content": prompt_text}],
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            )
            text = next(b.text for b in resp.content if b.type == "text")
            data = json.loads(text)
            output_writers[name] = {
                "topics": ratios_from(data, len(recent)),
                "sample_n": len(recent),
                "source_hash": article_hash,
                "labels_version": LABELS_VERSION,
                "prompt_version": PROMPT_VERSION,
                "classified_at": now_iso(),
            }
        except anthropic.APIError as error:
            print(f"Warning: API classification failed for {name}: {error}", file=sys.stderr)
            if isinstance(previous, dict):
                output_writers[name] = previous
        except Exception as error:
            print(f"Warning: invalid classification for {name}: {error}", file=sys.stderr)
            if isinstance(previous, dict):
                output_writers[name] = previous

    result = {
        "generated_at": now_iso(),
        "labels": TOPIC_LABELS,
        "writers": output_writers,
    }
    TOPICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOPICS_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {TOPICS_FILE} with {len(output_writers)} writer topic entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
