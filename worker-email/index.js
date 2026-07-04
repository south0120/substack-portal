import PostalMime from "postal-mime";

// FYL メール受信ワーカー（Stage 2: 受信保存 + 投稿メールだけD1へ取込）。
// Cloudflare Email Routing で inbox@findyourletter.com をこのワーカーにルーティングする。
// 各 Substack の新着投稿メールが届いたら、生メール(.eml)をそのまま R2 に保存する。
// パース/D1取込は失敗しても受信保存を止めない。「1通も損しない」ことを優先。

const SAN = /[^A-Za-z0-9._@-]+/g;

function sanitize(value, max = 80) {
  return String(value || "").replace(SAN, "_").slice(0, max) || "unknown";
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function headerValue(headers, name) {
  const target = name.toLowerCase();
  if (!headers) return "";
  if (typeof headers.get === "function") return headers.get(name) || headers.get(target) || "";
  const found = headers.find?.((h) => String(h?.key || h?.name || "").toLowerCase() === target);
  return found?.value || "";
}

function isoDate(value) {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

async function sha256(value) {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function extractPost(rawArrayBuffer) {
  const parsed = await PostalMime.parse(rawArrayBuffer);
  const listId = headerValue(parsed.headers, "list-id");
  const listMatch = String(listId).match(/<?([a-z0-9-]+)\.substack\.com>?/i);
  if (!listMatch) return null;

  // 正規の /p/ 直リンクは text パートにある。HTML側はトラッキング転送(redirect)で
  // 包まれていて直リンクが無いことが多いので、text→html の順で連結して探索する。
  const body = `${parsed.text || ""}\n${parsed.html || ""}`;
  const urlMatch = body.match(/https?:\/\/([a-z0-9-]+)\.substack\.com\/p\/[a-z0-9\-%]+/i);
  if (!urlMatch) return null;

  const u = new URL(urlMatch[0]);
  u.search = "";
  u.hash = "";

  // カバー/本文画像: メールHTMLに埋まっている substackcdn の post-media 画像の先頭。
  // 巡回(cron)が後追いで来ない可能性があるので、メール時点でサムネを確保する。
  const imgs = String(parsed.html || "").match(/https:\/\/substackcdn\.com\/image\/fetch\/[^"'\s)]+/gi) || [];
  const image = imgs.find((x) => /substack-post-media/.test(x)) || "";

  // excerpt: 本文テキストから定型文(「web で見る」リンク・購読のお願い等)を除いた最初の中身。
  const excerpt = String(parsed.text || "")
    .split(/\n/).map((s) => s.trim())
    .filter((s) => s && !/View this post on the web|無料購読|有料購読|皆さまの支援によって|今後の配信も見逃さない/.test(s))
    .join(" ").replace(/\s+/g, " ").slice(0, 180);

  return {
    subdomain: listMatch[1].toLowerCase(),
    url: u.toString(),
    title: parsed.subject || "",
    date: parsed.date || null,
    image,
    excerpt,
  };
}

// メール本文にカバー画像が無い投稿(テキスト主体・一部テンプレ)向けのフォールバック。
// 投稿ページの og:image / twitter:image を取得する。失敗しても空文字を返すだけ。
function metaContent(html, key) {
  const tag = String(html).match(
    new RegExp(`<meta[^>]+(?:property|name)=["']${key}["'][^>]*>`, "i")
  );
  if (!tag) return "";
  const c = tag[0].match(/content=["']([^"']+)["']/i);
  return c ? c[1] : "";
}

// Substack はUA無しのリクエストを弾くことがあるため、外向きfetchは共通の明示UAを付ける。
const FYL_UA = "Mozilla/5.0 (compatible; FYLBot/1.0; +https://findyourletter.com)";

async function fetchOgImage(pageUrl) {
  const res = await fetch(pageUrl, {
    headers: { "user-agent": FYL_UA },
    cf: { cacheTtl: 300, cacheEverything: true },
  });
  if (!res.ok) return "";
  const html = await res.text();
  return metaContent(html, "og:image") || metaContent(html, "twitter:image") || "";
}

// ---- RSSフィードからカバー画像を取得（メール本文にもインライン画像が無い時の中段フォールバック）----
// Cloudflare Workers に DOMParser が無いので、既存の metaContent 同様に正規表現で最小パースする。

function stripCdata(s) {
  return String(s || "").replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1").trim();
}

function decodeEntities(s) {
  return String(s || "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#0*39;|&apos;/g, "'")
    .trim();
}

// クエリ/フラグメント/末尾スラッシュ/大小文字の揺れを吸収して記事URLを突き合わせる。
function normFeedUrl(u) {
  const raw = decodeEntities(u);
  try {
    const x = new URL(raw);
    x.search = "";
    x.hash = "";
    return x.toString().replace(/\/+$/, "").toLowerCase();
  } catch {
    return raw.split(/[?#]/)[0].replace(/\/+$/, "").toLowerCase();
  }
}

// URL末尾に埋まった _幅x高 から、細帯(区切り線/バナー)っぽい画像を弾く。判定不能なら通す。
function looksLikeCover(url) {
  const dim = String(url).match(/_(\d{2,5})x(\d{2,5})\.(?:jpe?g|png|webp|gif|avif)/i);
  if (!dim) return true;
  const w = +dim[1], h = +dim[2];
  if (h < 200) return false;                 // 高さ200px未満は区切り/バナーとみなす
  if (w / h > 4 || h / w > 4) return false;   // 極端なアスペクト比も区切り扱い
  return true;
}

// <media:content>/<enclosure> タグ群から「画像」だけを拾う。podcastのenclosure(audio/mpeg)等は弾く。
function pickImageFromTags(tags) {
  for (const tag of tags) {
    const url = (tag.match(/\burl=["']([^"']+)["']/i) || [])[1];
    if (!url) continue;
    const type = (tag.match(/\btype=["']([^"']+)["']/i) || [])[1] || "";
    const medium = (tag.match(/\bmedium=["']([^"']+)["']/i) || [])[1] || "";
    if (/^(?:audio|video)\//i.test(type) || /^(?:audio|video)$/i.test(medium)) continue; // 明示的に非画像
    const isImage =
      /^image\//i.test(type) ||
      /^image$/i.test(medium) ||
      (!type && !medium && /substackcdn\.com\/image\/|substack-post-media|\.(?:jpe?g|png|webp|gif|avif)(?:[?#]|$)/i.test(url));
    if (isImage) return decodeEntities(url);
  }
  return "";
}

// フィードの1<item>からカバー画像URLを取り出す。優先順: media:content → enclosure → content:encoded内のカバーimg。
function imageFromFeedItem(item) {
  const m1 = pickImageFromTags(item.match(/<media:content\b[^>]*>/gi) || []);
  if (m1) return m1;
  const m2 = pickImageFromTags(item.match(/<enclosure\b[^>]*>/gi) || []);
  if (m2) return m2;
  const ce = item.match(/<content:encoded\b[^>]*>([\s\S]*?)<\/content:encoded>/i);
  if (ce) {
    const html = stripCdata(ce[1]);
    const srcs = (html.match(/<img\b[^>]*\bsrc=["']([^"']+)["']/gi) || [])
      .map((tag) => (tag.match(/\bsrc=["']([^"']+)["']/i) || [])[1])
      .filter(Boolean);
    // カバーは substack-post-media を優先し、細帯バナーは除外。良い候補が無ければ空を返し og:image に委ねる。
    const media = srcs.filter((s) => /substack-post-media/.test(s));
    const cover = media.find(looksLikeCover) || srcs.find(looksLikeCover) || "";
    if (cover) return decodeEntities(cover);
  }
  return "";
}

async function fetchFeedImage(subdomain, postUrl) {
  try {
    const res = await fetch(`https://${subdomain}.substack.com/feed`, {
      headers: { "user-agent": FYL_UA },
      cf: { cacheTtl: 300, cacheEverything: true },
    });
    if (!res.ok) return "";
    const xml = await res.text();
    const items = xml.match(/<item\b[\s\S]*?<\/item>/gi) || [];
    if (!items.length) return "";
    const target = normFeedUrl(postUrl);
    // 記事URLが一致する item のみ採用（先頭item決め打ちは誤マッチの元なのでしない）。
    const item = items.find((it) => {
      const m = it.match(/<link\b[^>]*>([\s\S]*?)<\/link>/i);
      const link = m ? normFeedUrl(stripCdata(m[1])) : "";
      return link && link === target;
    });
    if (!item) return "";
    return imageFromFeedItem(item);
  } catch {
    return "";
  }
}

async function knownFeed(env, subdomain) {
  const feedUrl = `https://${subdomain}.substack.com/feed`;
  const row = await env.DB.prepare(
    "SELECT name, categories FROM writers WHERE feed_url = ? LIMIT 1"
  ).bind(feedUrl).first();
  if (!row) return null;

  let categories = [];
  try {
    categories = JSON.parse(row.categories || "[]");
  } catch {
    categories = [];
  }
  return { writer: row.name, category: categories[0] || "その他" };
}

async function upsertEmailArticle(env, post, feed) {
  const id = await sha256(post.url);
  // 既存行(巡回が入れた本データ)は壊さない。excerpt/image は「空のときだけ」メール値で補完する。
  const article = env.DB.prepare(`
    INSERT INTO articles
      (id, url, title, excerpt, image, published, writer, category, is_audio)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
    ON CONFLICT(url) DO UPDATE SET
      published = COALESCE(excluded.published, articles.published),
      excerpt = CASE WHEN articles.excerpt IS NULL OR articles.excerpt = '' THEN excluded.excerpt ELSE articles.excerpt END,
      image = CASE WHEN articles.image IS NULL OR articles.image = '' THEN excluded.image ELSE articles.image END
  `).bind(
    id,
    post.url,
    post.title,
    post.excerpt || "",
    post.image || "",
    isoDate(post.date),
    feed.writer,
    feed.category,
  );
  const writer = env.DB.prepare("UPDATE writers SET updated_at=datetime('now') WHERE name=?").bind(feed.writer);
  await env.DB.batch([article, writer]);
  return id;
}

async function processPost(env, rawArrayBuffer) {
  try {
    const post = await extractPost(rawArrayBuffer);
    if (!post) return { skipped: "not-a-post" };
    const feed = await knownFeed(env, post.subdomain);
    if (!feed) return { skipped: "unknown-feed", subdomain: post.subdomain };
    // カバー画像のフォールバック: メール本文 → RSSフィード → 投稿ページの og:image。
    let imageSource = post.image ? "email" : "none";
    if (!post.image) {
      post.image = await fetchFeedImage(post.subdomain, post.url).catch(() => "");
      if (post.image) imageSource = "feed";
    }
    if (!post.image) {
      post.image = await fetchOgImage(post.url).catch(() => "");
      if (post.image) imageSource = "og";
    }
    await upsertEmailArticle(env, post, feed);
    return { upserted: true, url: post.url, writer: feed.writer, image: post.image ? "yes" : "no", imageSource };
  } catch (e) {
    return { error: String(e) };
  }
}

export default {
  // Email Routing から呼ばれる受信ハンドラ
  async email(message, env, ctx) {
    // message.raw は single-use。必ず一度だけ buffer する。
    const raw = await new Response(message.raw).arrayBuffer();

    const now = new Date();
    const y = now.getUTCFullYear();
    const m = pad(now.getUTCMonth() + 1);
    const d = pad(now.getUTCDate());
    const stamp = `${y}${m}${d}T${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}`;

    const from = message.from || "unknown";
    const messageId = message.headers.get("message-id") || "";
    // message-id があれば冪等キーに使う（同じメールの二重保存を防ぐ）。無ければ時刻+サイズ。
    const idPart = messageId ? sanitize(messageId.replace(/[<>]/g, ""), 60) : `${stamp}-${raw.byteLength}`;
    const key = `inbox/${y}/${m}/${d}/${stamp}-${sanitize(from, 50)}-${idPart}.eml`;

    await env.EMAILS.put(key, raw, {
      httpMetadata: { contentType: "message/rfc822" },
      customMetadata: {
        from,
        to: message.to || "",
        subject: (message.headers.get("subject") || "").slice(0, 256),
        date: message.headers.get("date") || now.toISOString(),
        messageId,
        rawSize: String(raw.byteLength),
      },
    });
    // D1取込は保存後に裏で走らせる。失敗しても受信/R2保存には影響させない。
    if (env.DB) ctx.waitUntil(processPost(env, raw).catch(() => {}));
    // ハンドラは raw を消費済みなのでメールは破棄されず保存完了。
  },

  // 動作確認用の簡易エンドポイント（ルーティングには無関係）
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return new Response(JSON.stringify({ ok: true, worker: "fyl-email", role: "store-raw-to-r2" }), {
        headers: { "content-type": "application/json" },
      });
    }
    // R2に貯まった受信メールの一覧（監視用）。LIST_TOKEN secret で保護
    if (url.pathname === "/list") {
      if (!env.LIST_TOKEN || url.searchParams.get("token") !== env.LIST_TOKEN) {
        return new Response("forbidden", { status: 403 });
      }
      const listed = await env.EMAILS.list({ limit: 100, include: ["customMetadata"] });
      const items = listed.objects
        .map((o) => ({
          key: o.key,
          size: o.size,
          uploaded: o.uploaded,
          from: o.customMetadata?.from || "",
          subject: o.customMetadata?.subject || "",
          date: o.customMetadata?.date || "",
        }))
        .sort((a, b) => String(b.uploaded).localeCompare(String(a.uploaded)));
      return Response.json({ count: items.length, truncated: listed.truncated, items });
    }
    // R2全件を再処理してD1へ反映（staging検証用）。LIST_TOKEN secret で保護
    if (url.pathname === "/reprocess") {
      if (!env.LIST_TOKEN || url.searchParams.get("token") !== env.LIST_TOKEN) {
        return new Response("forbidden", { status: 403 });
      }
      if (!env.DB) return Response.json({ error: "DB binding missing" }, { status: 500 });

      let cursor;
      let total = 0;
      let upserted = 0;
      let skippedUnknown = 0;
      let skippedNotPost = 0;
      let errors = 0;
      const samples = [];

      do {
        const listed = await env.EMAILS.list({ limit: 1000, cursor });
        for (const item of listed.objects) {
          total++;
          const obj = await env.EMAILS.get(item.key);
          const result = obj ? await processPost(env, await obj.arrayBuffer()) : { error: "not found" };
          if (result.upserted) upserted++;
          else if (result.skipped === "unknown-feed") skippedUnknown++;
          else if (result.skipped === "not-a-post") skippedNotPost++;
          else if (result.error) errors++;
          if (samples.length < 10) samples.push({ key: item.key, result });
        }
        cursor = listed.truncated ? listed.cursor : undefined;
      } while (cursor);

      return Response.json({ total, upserted, skippedUnknown, skippedNotPost, errors, samples });
    }
    // 受信メールの生本文を取得（監視/デバッグ用）。i=新しい順のインデックス
    if (url.pathname === "/get") {
      if (!env.LIST_TOKEN || url.searchParams.get("token") !== env.LIST_TOKEN) {
        return new Response("forbidden", { status: 403 });
      }
      const listed = await env.EMAILS.list({ limit: 100 });
      const sorted = listed.objects.sort((a, b) => String(b.uploaded).localeCompare(String(a.uploaded)));
      const i = Math.max(0, parseInt(url.searchParams.get("i") || "0", 10));
      const obj = sorted[i];
      if (!obj) return new Response("no object", { status: 404 });
      const data = await env.EMAILS.get(obj.key);
      if (!data) return new Response("not found", { status: 404 });
      return new Response(data.body, { headers: { "content-type": "text/plain; charset=utf-8" } });
    }
    return new Response("fyl-email: inbound email -> R2 store. See /health.", {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};
