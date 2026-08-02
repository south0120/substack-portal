#!/usr/bin/env python3
"""Substack の publication 検索を使って、未登録のアクティブな日本語ニュースレターを
発見し feeds.json に追加する。推薦グラフ巡回(discover.py)が枯渇した時の別経路。

仕組み:
- 検索ワード(_discover_search_kw.json, 例「はじめました」)ごとに
  `https://substack.com/api/v1/publication/search?query=<kw>` を叩き、
  マッチした publication の subdomain を集める。
- 既存 feeds.json に無い候補だけを、discover.py と同じ check_feed()
  （日本語 かつ 直近アクティブ）で絞って追加する。カテゴリは guess_category() で推定。

⚠️ 認証: publication/search は Substack ログインセッション必須（未認証だと 401 "Please sign in"）。
  cookie は 環境変数 FYL_SUBSTACK_COOKIE、無ければ ~/.claude/credentials/substack_fyl.json の "cookie"。
  🔴 substack.json ではない。あちらは south0120（週報）用。2026-08-02 に分離した。
     env が file より優先なので、env に古い値が残っていると file を直しても効かない。
     実行時に「[cookie] 出所: ...」を stderr に出すので、そこで必ず確認すること。
  ※Substack のセッション cookie は期限切れがあるため、GitHub Actions では Secret を定期更新する前提。

⚠️ GitHub Actions の IP は Substack に弾かれるため、本番では Worker プロキシ経由が要る。
  ただし現行プロキシ(/api/proxy)は Cookie を転送しない実装なので、Actions で使うには
  「プロキシに x-fwd-cookie 転送を足す」Worker 改修（サウスさんGOで別途デプロイ）が必要。
  本スクリプトは proxy 使用時に x-fwd-cookie を送る実装済み（Worker 対応後に有効化される）。
  proxy 無し(ローカル)では Cookie を直接付けて叩く（検証済で動作）。

実行:
  python3 scripts/discover_keyword.py --dry-run          # 追加せず候補だけ確認
  python3 scripts/discover_keyword.py --max-new 40
  FYL_PROXY_URL=... FYL_PROXY_SECRET=... FYL_SUBSTACK_COOKIE=... python3 scripts/discover_keyword.py  # Actions
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEEDS = ROOT / "feeds.json"
SEARCH_KW = ROOT / "scripts" / "_discover_search_kw.json"        # 検索ワード(配列)
CATEGORY_KW = ROOT / "scripts" / "_discover_kw.json"            # カテゴリ推定用(既存・別物)
UA = "Mozilla/5.0 (compatible; find-your-letter-discover/1.0)"

PROXY_URL = os.environ.get("FYL_PROXY_URL", "").rstrip("/")
PROXY_SECRET = os.environ.get("FYL_PROXY_SECRET", "")

# discover.py のヘルパを再利用（check_feed / guess_category / sub_to_feed / HIRA）
_spec = importlib.util.spec_from_file_location("_discover", ROOT / "scripts" / "discover.py")
_disc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_disc)
check_feed = _disc.check_feed
guess_category = _disc.guess_category
sub_to_feed = _disc.sub_to_feed
HIRA = _disc.HIRA

try:
    import certifi as _certifi
    SSL_CTX = ssl.create_default_context(cafile=_certifi.where())
except Exception:
    SSL_CTX = ssl._create_unverified_context()


def get_cookie():
    """(cookie, source) を返す。source は「どこから読んだか」の説明文字列。

    🔴 出所を必ず呼び出し側に返すこと。返り値を cookie だけにしないこと。
    2026-07-31、FYL の cookie が south0120 用の substack.json に書き込まれ、
    別 publication のセッションで動いていたのに誰も気づけなかった。
    どのファイルを読んだかが画面に出ていれば、その場で分かる事故だった。

    env が file より優先なのは GitHub Actions で Secret を使うため（この順序は変えない）。
    ただし env に古い値が残っていると file を直しても効かないので、
    「env を使った」ことは必ず表に出す。
    """
    c = os.environ.get("FYL_SUBSTACK_COOKIE", "")
    if c:
        return c, "env FYL_SUBSTACK_COOKIE"
    p = Path.home() / ".claude" / "credentials" / "substack_fyl.json"
    if p.exists():
        try:
            return json.loads(p.read_text()).get("cookie", ""), str(p)
        except Exception as e:
            return "", f"{p} (読めない: {e})"
    return "", f"{p} (存在しない)"


def search_publications(query, cookie, timeout=25):
    """publication/search で query にマッチする publication を返す（認証必須）。
    proxy がある時は x-fwd-cookie で cookie を転送（Worker が対応していれば有効）。
    proxy が無い時は Cookie を直接付ける（ローカル検証済）。"""
    api = f"https://substack.com/api/v1/publication/search?query={urllib.parse.quote(query)}"
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if PROXY_URL and PROXY_SECRET:
        target = f"{PROXY_URL}/api/proxy?url={urllib.parse.quote(api, safe='')}"
        headers["x-proxy-secret"] = PROXY_SECRET
        if cookie:
            headers["x-fwd-cookie"] = cookie
    else:
        target = api
        if cookie:
            headers["Cookie"] = cookie
    for attempt in range(4):
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(target, headers=headers), timeout=timeout, context=SSL_CTX
            ).read()
            data = json.loads(raw.decode("utf-8", "replace"))
            out = []
            for r in (data.get("results") or []):
                sub = r.get("subdomain")
                if sub:
                    out.append({
                        "subdomain": sub,
                        "custom_domain": r.get("custom_domain"),
                        "name": r.get("name") or "",
                        "language": r.get("language") or "",
                    })
            return out
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            if e.code == 401:
                print(f"  [401] '{query}': 認証切れ/cookie未転送。FYL_SUBSTACK_COOKIE を確認。", file=sys.stderr)
            else:
                print(f"  [{e.code}] '{query}': {e.reason}", file=sys.stderr)
            return []
        except Exception as e:
            print(f"  [err] '{query}': {type(e).__name__}", file=sys.stderr)
            return []
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=40, help="1回で追加する最大数")
    ap.add_argument("--dry-run", action="store_true", help="feeds.json に書かず候補だけ表示")
    args = ap.parse_args()

    cookie, cookie_src = get_cookie()
    print(f"[cookie] 出所: {cookie_src}", file=sys.stderr)
    if not cookie:
        print("WARN: Substack cookie 未設定 → publication/search は 401。"
              "FYL_SUBSTACK_COOKIE か ~/.claude/credentials/substack_fyl.json を用意してください。", file=sys.stderr)

    keywords = json.loads(SEARCH_KW.read_text()) if SEARCH_KW.exists() else \
        ["はじめました", "ニュースレター始めました", "Substack始めました", "書き始めました"]

    data = json.loads(FEEDS.read_text())
    feeds = data["feeds"]
    existing_subs = set()
    for f in feeds:
        m = re.search(r"https://([a-z0-9-]+)\.substack\.com", f.get("feed_url", "") or "")
        if m:
            existing_subs.add(m.group(1))
    existing_urls = {f.get("feed_url", "") for f in feeds}
    KW = json.loads(CATEGORY_KW.read_text()) if CATEGORY_KW.exists() else {}

    # 検索でサブドメイン候補を集める
    candidates = {}
    for q in keywords:
        subs = search_publications(q, cookie)
        print(f"'{q}': {len(subs)} hits", flush=True)
        for s in subs:
            sub = s["subdomain"]
            if sub in existing_subs or sub in candidates:
                continue
            feed_url = sub_to_feed(sub, s.get("custom_domain"))
            if feed_url in existing_urls:
                continue
            # language 優先、無ければ名前のひらがなで一次フィルタ（最終判定は check_feed）
            if s["language"] and s["language"] != "ja" and not HIRA.search(s["name"]):
                continue
            candidates[sub] = {"feed_url": feed_url, "name": s["name"]}
        time.sleep(0.5)
    print(f"raw candidates: {len(candidates)}", flush=True)

    # フィードを実取得して「日本語 かつ アクティブ」のみ採用
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


if __name__ == "__main__":
    main()
