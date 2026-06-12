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
5. 初回データ投入（バックフィル）は本番の `/api/ingest` を叩くのが最速です（60秒に1回まで）。
   ```sh
   # 5フィードずつ進む。約25回（=25分強）で全122名分が入る
   curl "https://fyl-api.<account>.workers.dev/api/ingest?n=5"
   ```
   進捗は `/api/health` の `cursor` / `articles` / `writers` / `lastRun` で確認できます。

## 巡回方式（Workers Free 対応）

- 毎時 cron で **5フィードずつ** 巡回（`FEEDS_PER_RUN=5`）。約25時間で一巡し、以降は差分のみ追加されます。
- 無料プランの CPU・サブリクエスト制限で実行が途中終了しても進捗が失われないよう、**1フィード処理するごとに `meta.cursor` を更新**します（途中死しても次回は続きから）。
- `GET /api/ingest?n=1..10` で手動巡回も可能（60秒スロットル付き）。初期投入や障害後の追い上げに使います。
