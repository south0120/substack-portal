# fyl-email — メール受信ワーカー（Stage 1）

Substack の新着投稿メールを受信し、生メール(.eml)をそのまま R2 に保存する。
イベント駆動取込のうち「受信して貯める」だけを担う。パース→D1取込は Stage 2（別途）。

本番 API ワーカー（`../worker`, `fyl-api`）とは**完全に独立**したワーカーなので、
こちらのデプロイが本番 API に影響することはない。

## セットアップ手順（サウスさん手元作業）

```bash
# 1. R2 バケット作成（生メール置き場）
npx wrangler r2 bucket create fyl-emails

# 2. このワーカーをデプロイ
cd worker-email && npx wrangler deploy
```

3. **Email Routing を有効化**（Cloudflare ダッシュボード）
   - `findyourletter.com` の **Email** > **Email Routing** を有効化（MXレコード等は自動追加）
   - **Routing Rules** で `inbox@findyourletter.com` → **Send to Worker: `fyl-email`** を追加
   - （または `npx wrangler email routing rules create` でも可）

4. **購読**: 各 Substack の購読フォーム/APIに `inbox@findyourletter.com` を登録。
   - Substack の無料購読は**メールアドレスだけでOK（Substackアカウント不要）**。
   - 購読開始日以降の新着が R2 (`fyl-emails`) に貯まり続ける。

## 動作確認

- `https://fyl-email.<account>.workers.dev/health` → `{ ok: true }`
- メール受信後、R2 `fyl-emails` の `inbox/YYYY/MM/DD/...eml` を確認

## 保存形式

- キー: `inbox/<YYYY>/<MM>/<DD>/<stamp>-<from>-<message-id>.eml`（message-id で冪等）
- customMetadata: `from`, `to`, `subject`, `date`, `messageId`, `rawSize`
- 本文は生 RFC822。Stage 2 のパーサーがここから記事URLを抽出して D1 に取り込む。
