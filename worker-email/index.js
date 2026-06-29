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
  return {
    subdomain: listMatch[1].toLowerCase(),
    url: u.toString(),
    title: parsed.subject || "",
    date: parsed.date || null,
  };
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
  const article = env.DB.prepare(`
    INSERT INTO articles
      (id, url, title, excerpt, image, published, writer, category, is_audio)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
    ON CONFLICT(url) DO UPDATE SET
      published = COALESCE(excluded.published, articles.published)
  `).bind(
    id,
    post.url,
    post.title,
    "",
    "",
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
    await upsertEmailArticle(env, post, feed);
    return { upserted: true, url: post.url, writer: feed.writer };
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
