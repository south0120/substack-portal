#!/usr/bin/env python3
"""Classify articles and calculate each writer's topic ratios with Claude."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ssl
import time
import urllib.error
import urllib.request

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

ROOT = Path(__file__).resolve().parent.parent
TOPICS_FILE = ROOT / "docs" / "data" / "topics.json"
ARTICLE_CATEGORIES_FILE = ROOT / "docs" / "data" / "article_categories.json"
USAGE_FILE = ROOT / "docs" / "data" / "api_usage.json"
API_BASE = "https://fyl-api.south0120.workers.dev"

# 分類は Google Gemini API（無料tier対象・従量でも安価）。MODEL_NAMEを変えれば差し替え可。
# 安さ優先なら "gemini-2.0-flash-lite"、精度寄りなら "gemini-2.0-flash"。
MODEL_NAME = "gemini-2.0-flash"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# Gemini 2.0 Flash の概算料金（USD / 100万トークン）。無料tier内なら実課金は$0。変わったらここを更新。
RATE_IN_USD_PER_MTOK = 0.10
RATE_OUT_USD_PER_MTOK = 0.40

# 棚カテゴリ（index.html の CAT_STYLE）と揃えた分類ラベル。バーの成分がそのまま棚カテゴリに使われる。
TOPIC_LABELS = [
    "AI", "テクノロジー", "ビジネス", "投資・経済", "社会・文化",
    "ライフスタイル", "クリエイティブ", "キャリア・働き方", "健康・ウェルネス",
    "教育・学び", "エンタメ", "旅行・おでかけ", "グルメ・料理", "スポーツ",
    "子育て・家族", "マンガ・アニメ", "音楽", "読書", "ゲーム",
    "ファッション・美容", "その他",
]
EXCERPT_LENGTH = 500
# プロンプト（判定ルール）を更新したらキャッシュを無効化して再分類するためのバージョン
PROMPT_VERSION = "v3"
# ラベル集合が変わったらキャッシュを無効化して再分類するための署名
LABELS_VERSION = hashlib.sha256("|".join(TOPIC_LABELS).encode("utf-8")).hexdigest()[:8]
ARTICLE_CACHE_VERSION = 1
BATCH_SIZE = 30
WRITE_BACK_BATCH_SIZE = 2000

# Gemini の responseSchema（OpenAPI サブセット。型は大文字、additionalProperties/enumはそのまま使える）。
GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "classifications": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "index": {"type": "INTEGER"},
                    "topic": {"type": "STRING", "enum": TOPIC_LABELS},
                },
                "required": ["index", "topic"],
            },
        }
    },
    "required": ["classifications"],
}


def gemini_generate(api_key: str, system_text: str, user_text: str, schema: dict,
                    max_out: int = 4000, retries: int = 4) -> tuple[str, int, int]:
    """Gemini generateContent を叩いて (JSONテキスト, 入力トークン, 出力トークン) を返す。
    429/5xx はバックオフして再試行（無料tierのレート制限対策）。"""
    url = f"{GEMINI_API_BASE}/{MODEL_NAME}:generateContent?key={api_key}"
    body = {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0,
            "maxOutputTokens": max_out,
        },
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "fyl-classifier/2.0"},
            )
            with urllib.request.urlopen(req, timeout=120, context=_SSL_CONTEXT) as r:
                payload = json.loads(r.read())
            candidates = payload.get("candidates") or []
            if not candidates:
                raise ValueError(f"no candidates: {json.dumps(payload)[:200]}")
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
            if not text:
                raise ValueError(f"empty text (finishReason={candidates[0].get('finishReason')})")
            um = payload.get("usageMetadata") or {}
            return text, int(um.get("promptTokenCount", 0) or 0), int(um.get("candidatesTokenCount", 0) or 0)
        except urllib.error.HTTPError as error:
            last_err = error
            if error.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(min((2 ** attempt) * 3, 30))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as error:
            last_err = error
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise last_err if last_err else RuntimeError("gemini_generate failed")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_article_cache() -> dict[str, Any]:
    if not ARTICLE_CATEGORIES_FILE.exists():
        return {"version": ARTICLE_CACHE_VERSION, "articles": {}}
    try:
        data = json.loads(ARTICLE_CATEGORIES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("articles"), dict):
            return {"version": ARTICLE_CACHE_VERSION, "articles": {}}
        return data
    except (OSError, json.JSONDecodeError) as error:
        print(f"Warning: could not read {ARTICLE_CATEGORIES_FILE}: {error}", file=sys.stderr)
        return {"version": ARTICLE_CACHE_VERSION, "articles": {}}


def source_hash(articles: list[dict[str, Any]]) -> str:
    urls = sorted(str(article.get("url", "")) for article in articles)
    encoded = json.dumps(urls, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def article_hash(article: dict[str, Any]) -> str:
    title = str(article.get("title", ""))
    excerpt = str(article.get("excerpt", ""))[:EXCERPT_LENGTH]
    return hashlib.sha256((title + excerpt).encode("utf-8")).hexdigest()


def prompt_for(articles: list[dict[str, Any]]) -> str:
    lines = [
        "以下の各記事を、その記事の『主題』に最も合うトピック1つに分類してください。",
        "トピック: " + " / ".join(TOPIC_LABELS),
        "",
        "判定ルール:",
        "- 記事が主に何について書かれているか（主題）で選ぶ。手段・道具として軽く触れているだけの話題では選ばない。",
        "- 「AI」「テクノロジー」は、記事の主題がAI・技術そのものの場合のみ選ぶ。"
        "趣味・仕事・運動などでAIやアプリを道具として使っているだけなら、その趣味・仕事・運動の主題で分類する"
        "（例: AIで作ったトライアスロンの記録 → スポーツ）。",
        "- ランニング・トライアスロンなど競技や運動が主題なら『スポーツ』。"
        "健康管理・医療・心身のウェルビーイングが主題なら『健康・ウェルネス』。",
        "- 『マンガ・アニメ』『ゲーム』『エンタメ』『ファッション・美容』は、それぞれ独立したカテゴリとして区別する。",
        "- 集客・売上・マーケティング・流入・起業などは『ビジネス』。",
        "- タイトルだけで判断せず抜粋（内容）も踏まえる。どれにも明確に当てはまらない場合のみ『その他』。",
        "- すべてのindexを1回ずつ分類してください。",
        "",
    ]
    for index, article in enumerate(articles, start=1):
        title = str(article.get("title", "")).strip()
        excerpt = str(article.get("excerpt", "")).strip()[:EXCERPT_LENGTH]
        lines.append(f"{index}. {title} / {excerpt}")
    return "\n".join(lines)


def classifications_from(data: dict[str, Any], sample_n: int) -> dict[int, str]:
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

    return by_index


def ratios_from(categories: list[str]) -> list[dict[str, Any]]:
    sample_n = len(categories)
    counts = Counter(categories)
    topics = [
        {"label": label, "pct": round(count / sample_n * 100, 1)}
        for label, count in counts.items()
        if count
    ]
    return sorted(topics, key=lambda topic: (-topic["pct"], TOPIC_LABELS.index(topic["label"])))


def _cost_usd(input_tokens: int, output_tokens: int) -> float:
    return round(
        input_tokens / 1_000_000 * RATE_IN_USD_PER_MTOK
        + output_tokens / 1_000_000 * RATE_OUT_USD_PER_MTOK,
        4,
    )


def update_usage(input_tokens: int, output_tokens: int, api_calls: int, classified: int) -> None:
    """このrunのトークン使用量・コストを api_usage.json に累積する（ダッシュボード表示用）。"""
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    try:
        usage = json.loads(USAGE_FILE.read_text(encoding="utf-8")) if USAGE_FILE.exists() else {}
    except (OSError, json.JSONDecodeError):
        usage = {}
    cum = usage.get("cumulative") or {}
    monthly = usage.get("monthly") or {}
    mrow = monthly.get(month) or {}

    def add(dst: dict[str, Any]) -> dict[str, Any]:
        it = int(dst.get("input_tokens", 0)) + input_tokens
        ot = int(dst.get("output_tokens", 0)) + output_tokens
        return {
            "input_tokens": it,
            "output_tokens": ot,
            "api_calls": int(dst.get("api_calls", 0)) + api_calls,
            "cost_usd": _cost_usd(it, ot),
        }

    monthly[month] = add(mrow)
    usage.update({
        "updated_at": now.isoformat(),
        "model": MODEL_NAME,
        "rates_usd_per_mtok": {"input": RATE_IN_USD_PER_MTOK, "output": RATE_OUT_USD_PER_MTOK},
        "cumulative": add(cum),
        "monthly": monthly,
        "last_run": {
            "at": now.isoformat(),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": _cost_usd(input_tokens, output_tokens),
            "writers_classified": classified,
        },
    })
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps(usage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Usage this run: in={input_tokens} out={output_tokens} cost=${_cost_usd(input_tokens, output_tokens)}")


def apply_article_categories(items: list[dict[str, str]], token: str) -> None:
    for start in range(0, len(items), WRITE_BACK_BATCH_SIZE):
        chunk = items[start:start + WRITE_BACK_BATCH_SIZE]
        body = json.dumps({"items": chunk}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{API_BASE}/api/admin/apply-article-categories",
            data=body,
            headers={
                "User-Agent": "fyl-classifier/1.0",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60, context=_SSL_CONTEXT) as response:
                data = json.loads(response.read())
            print(
                "Applied category chunk: "
                f"received={data.get('received', 0)} "
                f"applied={data.get('applied', 0)} "
                f"skipped={data.get('skipped', 0)}"
            )
        except urllib.error.HTTPError as error:
            print(
                f"Warning: category write-back failed with HTTP {error.code}: {error.reason}",
                file=sys.stderr,
            )
        except Exception as error:
            print(f"Warning: category write-back failed: {error}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    env_limit = os.environ.get("CLASSIFY_LIMIT", "").strip()
    default_limit = int(env_limit) if env_limit else None
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=default_limit)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("GEMINI_API_KEY is not set; skipping topic classification.")
        return 0

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

    cache = load_article_cache()
    cached_articles = cache["articles"]
    to_classify: list[dict[str, Any]] = []
    for article in articles:
        url = str(article.get("url", "")).strip()
        if not url:
            print("Warning: skipping article without URL", file=sys.stderr)
            continue
        current_hash = article_hash(article)
        previous = cached_articles.get(url)
        if (
            not isinstance(previous, dict)
            or previous.get("hash") != current_hash
            or previous.get("labels_version") != LABELS_VERSION
            or previous.get("prompt_version") != PROMPT_VERSION
        ):
            to_classify.append(article)

    if args.limit is not None:
        to_classify = to_classify[:max(args.limit, 0)]
    print(f"Articles needing classification this run: {len(to_classify)}")

    run_in = run_out = api_calls = classified = 0
    updated_items: list[dict[str, str]] = []
    api_articles: list[dict[str, Any]] = []

    for article in to_classify:
        title = str(article.get("title", "")).strip()
        excerpt = str(article.get("excerpt", "")).strip()
        if title or excerpt:
            api_articles.append(article)
            continue
        url = str(article.get("url", "")).strip()
        cached_articles[url] = {
            "category": "その他",
            "hash": article_hash(article),
            "labels_version": LABELS_VERSION,
            "prompt_version": PROMPT_VERSION,
        }
        updated_items.append({"url": url, "category": "その他"})
        classified += 1

    for start in range(0, len(api_articles), BATCH_SIZE):
        batch = api_articles[start:start + BATCH_SIZE]
        print(f"Classifying article batch: {start + 1}-{start + len(batch)}")
        try:
            prompt_text = prompt_for(batch)
            system_text = (
                "あなたは日本語ニュースレター記事のトピック分類器です。"
                "各記事を、その記事の主題（主に何について書かれているか）に最も合うトピック1つに分類してください。"
                "道具・手段として軽く触れているだけの要素では分類しないでください。"
            )
            text, in_tok, out_tok = gemini_generate(api_key, system_text, prompt_text, GEMINI_SCHEMA)
            run_in += in_tok
            run_out += out_tok
            api_calls += 1
            data = json.loads(text)
            by_index = classifications_from(data, len(batch))
            for index, article in enumerate(batch, start=1):
                url = str(article.get("url", "")).strip()
                category = by_index[index]
                cached_articles[url] = {
                    "category": category,
                    "hash": article_hash(article),
                    "labels_version": LABELS_VERSION,
                    "prompt_version": PROMPT_VERSION,
                }
                updated_items.append({"url": url, "category": category})
                classified += 1
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            print(f"Warning: Gemini API classification failed (HTTP {error.code}) for article batch: {detail}", file=sys.stderr)
        except Exception as error:
            print(f"Warning: invalid article batch classification: {error}", file=sys.stderr)

    if api_calls:
        update_usage(run_in, run_out, api_calls, classified)

    cache["version"] = ARTICLE_CACHE_VERSION
    ARTICLE_CATEGORIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    ARTICLE_CATEGORIES_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {ARTICLE_CATEGORIES_FILE} with {len(cached_articles)} article entries")

    token = os.environ.get("CLASSIFY_TOKEN")
    if args.dry_run:
        print("Dry run; skipping category write-back.")
    elif not token:
        print("Warning: CLASSIFY_TOKEN is not set; skipping category write-back.", file=sys.stderr)
    elif updated_items:
        apply_article_categories(updated_items, token)

    writer_names = {
        str(writer.get("name", "")).strip()
        for writer in writers
        if str(writer.get("name", "")).strip()
    }
    writer_names.update(
        str(article.get("writer", "")).strip()
        for article in articles
        if str(article.get("writer", "")).strip()
    )
    output_writers: dict[str, Any] = {}
    classified_at = now_iso()
    for name in sorted(writer_names):
        writer_articles = [article for article in articles if str(article.get("writer", "")).strip() == name]
        categories: list[str] = []
        for article in writer_articles:
            url = str(article.get("url", "")).strip()
            cached = cached_articles.get(url)
            if (
                isinstance(cached, dict)
                and cached.get("category") in TOPIC_LABELS
                and cached.get("hash") == article_hash(article)
                and cached.get("labels_version") == LABELS_VERSION
                and cached.get("prompt_version") == PROMPT_VERSION
            ):
                categories.append(cached["category"])
        if not categories:
            continue
        output_writers[name] = {
            "topics": ratios_from(categories),
            "sample_n": len(categories),
            "source_hash": source_hash(writer_articles),
            "labels_version": LABELS_VERSION,
            "prompt_version": PROMPT_VERSION,
            "classified_at": classified_at,
        }

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
