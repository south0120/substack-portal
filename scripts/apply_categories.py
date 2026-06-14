#!/usr/bin/env python3
"""topics.json のバー成分から feeds.json の categories を決める。

ルール: 成分(pct)が 30% 以上のカテゴリを上位から最大3つ採用。
- 「その他」以外が1つでも該当すれば「その他」は落とす。
- 30%以上が1つも無い（混在）場合は最上位1カテゴリのみ。
- categories[0]（主カテゴリ＝記事に使う）は最上位成分。

scope:
  --scope new (既定): 今セッションで追加した書き手だけ更新（既存の手書きカテゴリは維持）。
                      基準は --baseline コミット(既定 16603c4)の feeds.json に無い feed_url。
  --scope all       : topics を持つ全書き手のカテゴリをバー由来に上書き。
"""
import argparse, json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEEDS = ROOT / "feeds.json"
TOPICS = ROOT / "docs" / "data" / "topics.json"
THRESHOLD = 30.0
MAX_CATS = 3

def derive(topics):
    """topics: [{label,pct}] (pct降順前提) -> categories list"""
    qualifying = [t["label"] for t in topics if t.get("pct", 0) >= THRESHOLD]
    non_other = [l for l in qualifying if l != "その他"]
    chosen = (non_other or qualifying)[:MAX_CATS]
    if not chosen and topics:
        chosen = [topics[0]["label"]]
    return chosen

def baseline_urls(rev):
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "show", f"{rev}:feeds.json"],
                             capture_output=True, text=True, check=True).stdout
        return {f.get("feed_url", "") for f in json.loads(out)["feeds"]}
    except Exception as e:
        print(f"baseline read failed ({e}); treating all topic-having writers as in-scope", file=sys.stderr)
        return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["new", "all"], default="new")
    ap.add_argument("--baseline", default="16603c4")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    feeds_data = json.loads(FEEDS.read_text())
    feeds = feeds_data["feeds"]
    twriters = json.loads(TOPICS.read_text()).get("writers", {})

    in_scope = None
    if args.scope == "new":
        base = baseline_urls(args.baseline)
        if base is not None:
            in_scope = {f["feed_url"] for f in feeds if f["feed_url"] not in base}

    changed = 0
    for f in feeds:
        if in_scope is not None and f["feed_url"] not in in_scope:
            continue
        entry = twriters.get(f["name"])
        if not entry or not entry.get("topics"):
            continue
        new_cats = derive(entry["topics"])
        if new_cats and new_cats != f.get("categories"):
            print(f"  {f['name']}: {f.get('categories')} -> {new_cats}")
            if not args.dry_run:
                f["categories"] = new_cats
            changed += 1

    print(f"{'[dry-run] would change' if args.dry_run else 'changed'} {changed} writer(s) (scope={args.scope})")
    if not args.dry_run:
        FEEDS.write_text(json.dumps(feeds_data, ensure_ascii=False, indent=1) + "\n")

if __name__ == "__main__":
    main()
