# キーワード検索ディスカバリー 本番セットアップ手順（サウスさん用）

`discover_keyword.py`（Substackのキーワード検索で新しい書き手を発見）をGitHub Actionsで
週次稼働させるための3ステップ。**外向き操作（Workerデプロイ・Secret追加・push）はサウスさん本人GO必須**
なので、alexは準備まで済ませてあります。以下をサウスさんが実行（Discordから `! コマンド` でもOK）。

---

## ステップ1: Worker(fyl-api)をデプロイ

**何が変わるか**: プロキシ(`/api/proxy`)が `x-fwd-cookie` ヘッダを受け取った時だけ、
Substackのセッションcookieを転送する小改修（**substack.com宛のみ**・RSS取得は従来どおり・後方互換）。
これでGitHub ActionsからでもSubstackの認証必須API(`publication/search`)を叩ける。

```bash
cd /Users/dev/agents/alex/work/fyl_weekly/worker && npx wrangler deploy
```
（Discordから: `! cd /Users/dev/agents/alex/work/fyl_weekly/worker && npx wrangler deploy`）

→ `Deployed fyl-api ... https://fyl-api.south0120.workers.dev` が出れば成功。

---

## ステップ2: GitHub Secret `FYL_SUBSTACK_COOKIE` を追加

Substackのログインcookie（`~/.claude/credentials/substack.json` の `cookie`）を、
**値を画面に出さずに**GitHub Secretへ登録するコマンド:

```bash
gh secret set FYL_SUBSTACK_COOKIE --repo south0120/substack-portal \
  --body "$(python3 -c "import json,pathlib;print(json.loads(pathlib.Path.home().joinpath('.claude/credentials/substack.json').read_text())['cookie'])")"
```
（Discordから: 上記を `! ` 付きで）

→ `✓ Set secret FYL_SUBSTACK_COOKIE` が出れば成功。
※ `FYL_PROXY_SECRET` は既存workflowで設定済みなので追加不要。

⚠️ **cookieは失効します**。Actionsのログに `[401] 認証切れ` が出たら、Substackに再ログインして
`substack.json` のcookieを更新 → 上のコマンドを再実行してSecretを更新してください。

---

## ステップ3: 3ファイルをpush（alexが代行可 / GO後）

```
scripts/discover_keyword.py
scripts/_discover_search_kw.json
.github/workflows/discover_keyword.yml
worker/index.js               ← ステップ1のWorker改修
DISCOVER_KEYWORD_SETUP.md      ← この手順書
```
「pushしてOK」とGOをもらえれば alex が commit & push します
（コミットメッセージ: `feat: keyword-search writer discovery + worker cookie forwarding`）。

---

## 検証（ステップ1〜3完了後）

GitHubの Actions タブ → **「Discover writers (keyword search)」** → **Run workflow**（手動実行）。
ログに `'はじめました': N hits` … `added M active JA writers` が出て、feeds.json更新のcommitが入れば本番稼働OK。
（ローカルdry-runでは既に8名の新規書き手を発見済み）
