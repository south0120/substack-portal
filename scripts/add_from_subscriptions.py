#!/usr/bin/env python3
"""さぶにゃん(や任意ユーザー)の購読欄から取った候補をRSSでアクティブ判定し、
- 全員 -> discovered_candidates.json（恒久バックログ。いずれ書き始めるかも枠含む）
- アクティブJA -> feeds_additions.json（feeds.jsonへのマージ候補・pushはサウスGO後）
に振り分ける。check_feed/guess_category は discover.py を再利用。
ローカルで直接叩く（GH IPはSubstackに弾かれるためproxy不要のローカル実行）。"""
import json, os, sys, time, re
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import discover  # __main__ ガードあり・安全にimport可

CAND_IN = os.environ.get("FYL_CAND_IN", "/tmp/fyl_sub_candidates.json")
SOURCE  = os.environ.get("FYL_SOURCE", "sabunyan_reads")
KWPATH  = ROOT / "scripts" / "_discover_kw.json"
KW = json.loads(KWPATH.read_text()) if KWPATH.exists() else {}

feeds_doc = json.loads((ROOT / "feeds.json").read_text())
feeds = feeds_doc["feeds"]
existing = set()
for f in feeds:
    m = re.search(r"https?://([a-z0-9-]+)\.substack\.com", f.get("feed_url", "") or "")
    if m: existing.add(m.group(1))

cands = json.loads(Path(CAND_IN).read_text())
print(f"candidates: {len(cands)} | existing FYL: {len(existing)}", flush=True)

backlog = []      # 全員
additions = []    # アクティブJAのみ (feeds.json 形式)
active_n = dead_n = inactive_n = skip_n = 0
for i, c in enumerate(cands, 1):
    sub = c["subdomain"]; name = c.get("name", "")
    if sub in existing:
        skip_n += 1; continue
    feed_url = f"https://{sub}.substack.com/feed"
    active, title, desc = discover.check_feed(feed_url)
    # check_feed: (日本語かつアクティブ, title, desc)。title空/channel無し=dead。
    if not title and not desc:
        status = "dead"; dead_n += 1
    elif active:
        status = "active"; active_n += 1
        cat = discover.guess_category((title + " " + desc), KW) or ["その他"]
        additions.append({"name": title or name, "feed_url": feed_url, "categories": cat, "bio": desc})
    else:
        status = "inactive"; inactive_n += 1
    backlog.append({"subdomain": sub, "name": name, "feed_url": feed_url,
                    "status": status, "title": title, "bio": desc, "source": SOURCE})
    if i % 25 == 0:
        print(f"  {i}/{len(cands)}  active={active_n} inactive={inactive_n} dead={dead_n}", flush=True)
    time.sleep(0.3)

(ROOT / "discovered_candidates.json").write_text(json.dumps(backlog, ensure_ascii=False, indent=1))
(ROOT / "feeds_additions.json").write_text(json.dumps(additions, ensure_ascii=False, indent=1))
print(f"\nDONE. checked={len(cands)} skip_existing={skip_n} | active(JA)={active_n} inactive={inactive_n} dead={dead_n}", flush=True)
print(f"-> discovered_candidates.json ({len(backlog)})  |  feeds_additions.json ({len(additions)})", flush=True)
print("sample additions:", json.dumps(additions[:5], ensure_ascii=False), flush=True)
