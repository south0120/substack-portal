#!/usr/bin/env python3
"""エンゲージメント・グラフ発掘（A）。

既存の書き手の最近の投稿の「コメント欄」を辿って、まだ未登録の日本語アクティブ書き手を
見つける。推薦グラフ(discover.py)では届かない「"反応"で繋がってる人」を掘る別経路。
（文化祭のコメント欄から55人見つけた手法の仕組み化）

使い方:
  FYL_PROXY_URL=... FYL_PROXY_SECRET=... python3 scripts/discover_engagement.py \
    --writers 30 --posts 3 --max-new 40

GitHub IP は Substack に弾かれるため、Action で回す時は FYL_PROXY_* を渡してプロキシ経由に。
"""
import argparse
import json
import re
import time
from collections import defaultdict

import discover as D  # http_get / check_feed / guess_category / HIRA / FEEDS / ROOT を再利用


def recent_post_ids(pub, n):
    data = D.http_get(f"https://{pub}.substack.com/api/v1/posts?limit={n}", as_json=True)
    if isinstance(data, dict):
        data = data.get("posts")
    out = []
    for p in data or []:
        if isinstance(p, dict) and p.get("id"):
            out.append(p["id"])
    return out


def commenters(pub, post_id):
    data = D.http_get(
        f"https://{pub}.substack.com/api/v1/post/{post_id}/comments?all_comments=true",
        as_json=True,
    )
    comments = (data.get("comments") if isinstance(data, dict) else None) or []
    out = []

    def walk(items):
        for c in items:
            if not isinstance(c, dict):
                continue
            handle = c.get("handle") or (c.get("user") or {}).get("handle")
            if handle:
                out.append({"handle": handle, "name": c.get("name")})
            walk(c.get("children") or [])

    walk(comments)
    return out


def pick_sources(feeds, want):
    """起点にする既存書き手を、カテゴリ横断で均等に選ぶ。"""
    by_cat = defaultdict(list)
    for f in feeds:
        m = re.search(r"https://([a-z0-9-]+)\.substack\.com", f.get("feed_url", "") or "")
        if not m:
            continue
        by_cat[(f.get("categories") or ["その他"])[0]].append(m.group(1))
    sources, seen = [], set()
    pointers = {c: 0 for c in by_cat}
    cats = list(by_cat.keys())
    while len(sources) < want:
        progressed = False
        for c in cats:
            if len(sources) >= want:
                break
            p = pointers[c]
            if p < len(by_cat[c]):
                pointers[c] += 1
                progressed = True
                s = by_cat[c][p]
                if s not in seen:
                    seen.add(s)
                    sources.append(s)
        if not progressed:
            break
    return sources


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--writers", type=int, default=30, help="起点にする既存書き手の数")
    ap.add_argument("--posts", type=int, default=3, help="各書き手の最近の投稿をいくつ見るか")
    ap.add_argument("--max-new", type=int, default=40, help="1回で追加する最大数")
    args = ap.parse_args()

    data = json.loads(D.FEEDS.read_text())
    feeds = data["feeds"]
    existing_subs = set()
    for f in feeds:
        m = re.search(r"https://([a-z0-9-]+)\.substack\.com", f.get("feed_url", "") or "")
        if m:
            existing_subs.add(m.group(1))
    existing_urls = {f.get("feed_url", "") for f in feeds}

    sources = pick_sources(feeds, args.writers)
    print(f"mining comments from {len(sources)} writers' recent posts...", flush=True)

    handles = {}  # handle -> name（未登録のコメント主）
    for i, pub in enumerate(sources, 1):
        for pid in recent_post_ids(pub, args.posts):
            for c in commenters(pub, pid):
                h = c["handle"]
                if h and h not in existing_subs and h not in handles:
                    handles[h] = c["name"]
            time.sleep(0.3)
        if i % 5 == 0:
            print(f"  source {i}/{len(sources)}, candidate handles: {len(handles)}", flush=True)
        time.sleep(0.3)
    print(f"collected {len(handles)} candidate handles", flush=True)

    kw_path = D.ROOT / "scripts" / "_discover_kw.json"
    KW = json.loads(kw_path.read_text()) if kw_path.exists() else {}
    added = 0
    checked = 0
    for h, name in handles.items():
        if added >= args.max_new:
            break
        feed_url = f"https://{h}.substack.com/feed"
        if feed_url in existing_urls:
            continue
        active, title, desc = D.check_feed(feed_url)
        checked += 1
        if not active:
            continue
        cat = D.guess_category((title or "") + " " + (desc or ""), KW)
        feeds.append({"name": title or name or h, "feed_url": feed_url, "categories": cat, "bio": desc})
        existing_urls.add(feed_url)
        added += 1
        print(f"  + {title or name}  {cat}", flush=True)
        time.sleep(0.3)

    print(f"checked {checked} candidates, added {added} active JA writers (total {len(feeds)})")
    data["feeds"] = feeds
    D.FEEDS.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    main()
