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

カテゴリは `AI`、`テクノロジー`、`ビジネス`、`投資・経済`、`社会・文化`、`ライフスタイル`、`クリエイティブ`、`キャリア・働き方`、`健康・ウェルネス`、`教育・学び`、`その他` のいずれかを使います。

GitHub Actions は6時間ごとに RSS を取得し、`docs/data/articles.json` を更新します。取得に失敗した書き手も棚には残り、記事だけが空になります。

## 最近の傾向

書き手カードのトピック比率バーを有効にするには、リポジトリの **Settings → Secrets and variables → Actions** で `ANTHROPIC_API_KEY` を Repository secret に設定します。週次ワークフローが直近の記事を分類して `docs/data/topics.json` を更新します。secret がなくてもサイトは動作し、トピック比率バーだけが表示されません。

## Google Analytics

`docs/index.html` 内の `G-XXXXXXXXXX` を Google Analytics の実際の測定 ID（例: `G-ABC123XYZ`）へ2か所とも置換してください。
