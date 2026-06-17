#!/usr/bin/env python3
"""Substack の推薦グラフを巡回して、未登録のアクティブな日本語ニュースレターを発見し
feeds.json に追加する自動ディスカバリー。

仕組み:
- 各publicationの推薦先は `https://substack.com/api/v1/recommendations/from/{publication_id}` で取れる
  （日本人ライターは日本人ライターを推薦しがち＝同類が辿れる）
- 既存feeds.jsonの一部を「種」に publication_id を取得 → BFSで推薦先を辿る
- 候補のうち「日本語 かつ 直近 ACTIVE_DAYS 日に投稿あり（=アクティブ）」だけを追加

実行:
  python3 scripts/discover.py                       # 既定の探索
  python3 scripts/discover.py --seeds 40 --max-new 80 --hops 2
  FYL_PROXY_URL=... FYL_PROXY_SECRET=... python3 scripts/discover.py   # Substack呼び出しをWorkerプロキシ経由に（GH Actions用）

GitHub IP は Substack に弾かれるため、Actionで回す時は FYL_PROXY_* を渡してプロキシ経由にする。
"""
import argparse, json, os, re, sys, time, urllib.parse, urllib.request, urllib.error
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEEDS = ROOT / "feeds.json"
HIRA = re.compile(r"[ぁ-ゟ]")
UA = "Mozilla/5.0 (compatible; find-your-letter-discover/1.0)"
PROXY_URL = os.environ.get("FYL_PROXY_URL", "").rstrip("/")
PROXY_SECRET = os.environ.get("FYL_PROXY_SECRET", "")

# 既定の探索パラメータ
ACTIVE_DAYS = 120         # 直近この日数に投稿があればアクティブとみなす
HIRA_MIN = 3              # 日本語判定（ひらがな最低数）


def http_get(url, timeout=20, as_json=False):
    """直接 or Workerプロキシ経由でGET。429はバックオフ。"""
    target = url
    headers = {"User-Agent": UA, "Accept": "application/json, text/html"}
    if PROXY_URL and PROXY_SECRET:
        target = f"{PROXY_URL}/api/proxy?url={urllib.parse.quote(url, safe='')}"
        headers["x-proxy-secret"] = PROXY_SECRET
    for attempt in range(5):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(target, headers=headers), timeout=timeout).read()
            text = raw.decode("utf-8", "replace")
            return json.loads(text) if as_json else text
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                time.sleep(5 * (attempt + 1)); continue
            return None
        except Exception:
            return None
    return None


def first_tag(seg, tag):
    m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", seg or "", re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()


def sub_to_feed(subdomain, custom_domain):
    base = f"https://{custom_domain}" if custom_domain else f"https://{subdomain}.substack.com"
    return base.rstrip("/") + "/feed"


def get_pub_id(subdomain):
    """publicationのホームページの _preloads から publication_id を得る。"""
    html = http_get(f"https://{subdomain}.substack.com/", timeout=20)
    if not html:
        return None
    m = re.search(r'window\._preloads\s*=\s*JSON\.parse\((\"(?:[^\"\\]|\\.)*\")\)', html)
    if not m:
        return None
    try:
        data = json.loads(json.loads(m.group(1)))
        pub = data.get("pub") or {}
        return pub.get("id")
    except Exception:
        return None


def recommendations(pub_id):
    """推薦先 publication のリスト [{id, subdomain, custom_domain, name, language}]。"""
    data = http_get(f"https://substack.com/api/v1/recommendations/from/{pub_id}", as_json=True)
    out = []
    if isinstance(data, list):
        for r in data:
            p = r.get("recommendedPublication") or {}
            if p.get("id"):
                out.append({
                    "id": p["id"], "subdomain": p.get("subdomain") or "",
                    "custom_domain": p.get("custom_domain"), "name": p.get("name") or "",
                    "language": p.get("language") or "",
                })
    return out


def check_feed(feed_url):
    """フィードを取得して (is_japanese_and_active, title, description) を返す。"""
    xml = http_get(feed_url, timeout=20)
    if not xml or "<channel" not in xml:
        return False, "", ""
    ch = re.search(r"<channel>(.*?)<item", xml, re.S)
    seg = ch.group(1) if ch else xml[:3000]
    title, desc = first_tag(seg, "title"), first_tag(seg, "description")
    items = re.findall(r"<item>(.*?)</item>", xml, re.S)[:5]
    itext = " ".join(first_tag(it, "title") for it in items)
    if len(HIRA.findall(title + desc + itext)) < HIRA_MIN:
        return False, title, desc  # 日本語でない
    # アクティブ判定: 最新記事の pubDate が ACTIVE_DAYS 以内か
    cutoff = datetime.now(timezone.utc) - timedelta(days=ACTIVE_DAYS)
    newest = None
    for it in items:
        raw = first_tag(it, "pubDate")
        try:
            d = parsedate_to_datetime(raw)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            if newest is None or d > newest:
                newest = d
        except Exception:
            pass
    active = newest is not None and newest >= cutoff
    return active, title, desc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=40, help="種にする既存pub数（feeds.json先頭から）")
    ap.add_argument("--hops", type=int, default=3, help="推薦グラフを辿る深さ")
    ap.add_argument("--max-new", type=int, default=80, help="1回で追加する最大数")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = json.loads(FEEDS.read_text())
    feeds = data["feeds"]
    existing_subs = set()
    for f in feeds:
        m = re.search(r"https://([a-z0-9-]+)\.substack\.com", f.get("feed_url", "") or "")
        if m:
            existing_subs.add(m.group(1))
    existing_urls = {f.get("feed_url", "") for f in feeds}

    # 種を「カテゴリ横断」で多様に選ぶ（先頭固定だと推薦グラフの到達範囲が偏るため、
    # 各カテゴリから round-robin で均等に拾って seeds 件にする）。
    by_cat = defaultdict(list)
    for f in feeds:
        m = re.search(r"https://([a-z0-9-]+)\.substack\.com", f.get("feed_url", "") or "")
        if not m:
            continue
        cat = (f.get("categories") or ["その他"])[0]
        by_cat[cat].append(m.group(1))
    seed_subs = []
    seen_seed = set()
    pointers = {c: 0 for c in by_cat}
    cats = list(by_cat.keys())
    while len(seed_subs) < args.seeds:
        progressed = False
        for c in cats:
            if len(seed_subs) >= args.seeds:
                break
            p = pointers[c]
            if p < len(by_cat[c]):
                pointers[c] += 1
                progressed = True
                s = by_cat[c][p]
                if s not in seen_seed:
                    seen_seed.add(s)
                    seed_subs.append(s)
        if not progressed:
            break
    print(f"resolving {len(seed_subs)} seed publication ids...", flush=True)
    queue = deque()
    visited_ids = set()
    for i, sub in enumerate(seed_subs, 1):
        pid = get_pub_id(sub)
        if pid:
            queue.append((pid, 0))
            visited_ids.add(pid)
        if i % 10 == 0:
            print(f"  seeds {i}/{len(seed_subs)}", flush=True)
        time.sleep(0.3)
    print(f"seeds resolved: {len(queue)}", flush=True)

    # BFS で推薦先を辿り、未登録の日本語候補を集める
    candidates = {}  # subdomain -> {feed_url, name}
    while queue:
        pid, depth = queue.popleft()
        if depth >= args.hops:
            continue
        for rec in recommendations(pid):
            rid, sub, cdom = rec["id"], rec["subdomain"], rec["custom_domain"]
            if rid not in visited_ids:
                visited_ids.add(rid)
                queue.append((rid, depth + 1))
            if not sub or sub in existing_subs or sub in candidates:
                continue
            feed_url = sub_to_feed(sub, cdom)
            if feed_url in existing_urls:
                continue
            # language 優先、無ければ名前のひらがなで一次フィルタ（最終判定は check_feed）
            if rec["language"] and rec["language"] != "ja" and not HIRA.search(rec["name"]):
                continue
            candidates[sub] = {"feed_url": feed_url, "name": rec["name"]}
        time.sleep(0.3)
        if len(candidates) >= args.max_new * 3:  # 余裕を持って集めてから絞る
            break
    print(f"raw candidates: {len(candidates)}", flush=True)

    # フィードを実取得して「日本語 かつ アクティブ」のみ採用
    KW = json.loads((ROOT / "scripts" / "_discover_kw.json").read_text()) if (ROOT / "scripts" / "_discover_kw.json").exists() else {}
    added = 0
    checked = 0
    for sub, info in candidates.items():
        if added >= args.max_new:
            break
        active, title, desc = check_feed(info["feed_url"])
        checked += 1
        if not active:
            continue
        cat = guess_category(title + " " + desc, KW)
        feeds.append({"name": title or info["name"], "feed_url": info["feed_url"], "categories": cat, "bio": desc})
        added += 1
        print(f"  + {title or info['name']}  {cat}", flush=True)
        time.sleep(0.3)

    print(f"checked {checked} candidates, added {added} active JA writers (total {len(feeds)})")
    if not args.dry_run and added:
        FEEDS.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def guess_category(text, kw):
    best, score = "その他", 0
    for cat, kws in kw.items():
        s = sum(text.count(k) for k in kws)
        if s > score:
            best, score = cat, s
    return [best]


if __name__ == "__main__":
    main()
