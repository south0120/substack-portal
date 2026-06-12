# Substack Portal

日本語で書く Substack の書き手を、本屋の棚のようにカテゴリ別で紹介する GitHub Pages サイトです。表示順は `feeds.json` の固定順で、ランキングやおすすめアルゴリズムはありません。記事一覧は補助的なビューとして提供します。

## セットアップ

1. このディレクトリを GitHub リポジトリへ push します。
2. **Settings → Pages** で `main` ブランチの `/docs` を公開元に設定します。
3. **Settings → Actions → General → Workflow permissions** を **Read and write permissions** に設定します。
4. **Actions → Update shelf data → Run workflow** で初回データを生成します。

## 書き手を追加する

`feeds.json` の `feeds` 配列へ次の形式で追加します。配列内の位置が棚での表示順です。

```json
{
  "name": "ニュースレター名",
  "feed_url": "https://example.substack.com/feed",
  "category": "AI",
  "bio": "棚カードに表示する短い紹介文。"
}
```

`feeds.json` のカテゴリには `docs/index.html` の `MASTER_CATEGORIES` にある値を使えます。新しいカテゴリの書き手を追加すると、そのカテゴリはサイト上に自動で表示されます。

GitHub Actions は6時間ごとに RSS を取得し、`docs/data/articles.json` を更新します。取得に失敗した書き手も棚には残り、記事だけが空になります。

## 最近の傾向

書き手カードのトピック比率バーを有効にするには、リポジトリの **Settings → Secrets and variables → Actions** で `ANTHROPIC_API_KEY` を Repository secret に設定します。週次ワークフローが直近の記事を分類して `docs/data/topics.json` を更新します。secret がなくてもサイトは動作し、トピック比率バーだけが表示されません。

## Google Analytics

`docs/index.html` 内の `G-XXXXXXXXXX` を Google Analytics の実際の測定 ID（例: `G-ABC123XYZ`）へ2か所とも置換してください。

## Tally 掲載申請 Webhook

Tally でフォームを作成し、フィールドのラベルを `掲載名`、`Substack URL`、`カテゴリ`、`自己紹介` にします。Tally の **Integrations → Webhooks** で送信先を `https://fyl-api.<account>.workers.dev/api/apply` に設定してください。

Signing secret を設定する場合は、同じ値を Worker に登録します。

```sh
npx wrangler secret put TALLY_SIGNING_SECRET
```

GitHub には `south0120/substack-portal` のみを対象にした fine-grained personal access token を作成し、Repository permissions で **Contents: Read and write** と **Pull requests: Read and write** を許可します。トークンは Worker の secret に登録します。

```sh
npx wrangler secret put GITHUB_TOKEN
```

申請を受信すると、フィードの到達性と日本語記事タイトルを検証し、D1 の `applications` テーブルへ記録した後、`feeds.json` に書き手を追加する Pull Request を自動作成します。Pull Request を確認してマージすると、既存の毎時 cron が新しいフィードの取得を開始します。

通常はスパム対策のため Pull Request を作成します。Worker の環境変数に `AUTO_COMMIT=true` を設定すると、Pull Request を作らず `main` へ直接コミットします。
