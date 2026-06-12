# Find Your Letter Workers + D1 デプロイ手順

サウスさん向けの初回セットアップ手順です。

1. ログインします。
   ```sh
   cd worker
   npx wrangler login
   ```
2. D1 データベースを作成し、出力された `database_id` を `wrangler.toml` の `REPLACE_WITH_D1_ID` と置き換えます。
   ```sh
   npx wrangler d1 create fyl-articles
   ```
3. 本番 D1 にスキーマを適用します。
   ```sh
   npx wrangler d1 execute fyl-articles --remote --file=schema.sql
   ```
4. Worker をデプロイします。出力 URL（`https://fyl-api.<account>.workers.dev`）を `docs/config.js` の `FYL_WORKER_URL` に設定して commit します。
   ```sh
   npx wrangler deploy
   ```
5. 初回データ投入のために D1 execute を追加で行う必要はありません。cron を待つか、ローカルで scheduled handler を手動実行します。
   ```sh
   npx wrangler dev --test-scheduled
   curl http://localhost:8787/__scheduled
   ```

毎時 40 フィードずつ巡回するため、122名分は約3時間で一巡します。

## 毎時ローテーションにした理由

Cloudflare Workers 無料プランのサブリクエスト上限（50回/1実行）に収めるため、David の日次一括実行指定から、毎時40フィードのローテーション方式へ変更しています。各実行では `feeds.json` の取得に1回、RSS取得に最大40回を使い、D1 の `meta.cursor` に次回の開始位置を保存します。
