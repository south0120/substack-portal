const FEEDS_URL = "https://raw.githubusercontent.com/south0120/substack-portal/main/feeds.json";
const USER_AGENT = "find-your-letter/1.0 (+https://findyourletter.com)";
const FEEDS_PER_RUN = 5; // Workers Free の CPU 制限対策で小さく保つ
const INGEST_MAX = 10;
const INGEST_THROTTLE_MS = 60000;
const DB_BATCH_SIZE = 50;
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

export default {
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(refreshFeeds(env));
  },

  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    if (request.method !== "GET") {
      return jsonResponse({ error: "Method not allowed" }, 405);
    }

    try {
      const url = new URL(request.url);
      if (url.pathname === "/api/articles") return getArticles(url, env);
      if (url.pathname === "/api/writers") return getWriters(url, env);
      if (url.pathname === "/api/categories") return getCategories(env);
      if (url.pathname === "/api/health") return getHealth(env);
      if (url.pathname === "/api/ingest") return runIngest(url, env);
      return jsonResponse({ error: "Not found" }, 404);
    } catch (error) {
      console.error("API error", error);
      return jsonResponse({ error: "Internal server error" }, 500);
    }
  },
};

async function refreshFeeds(env, perRun = FEEDS_PER_RUN) {
  // フィードを1件ずつ処理し、その都度 cursor を進める。
  // 途中で invocation が落ちても進捗が残り、次回が同じフィードをやり直し続けない。
  const stats = { feeds: 0, feedSuccesses: 0, articlesProcessed: 0, cursorStart: null, cursorEnd: null };
  try {
    const response = await fetch(FEEDS_URL, {
      headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) throw new Error(`feeds.json: HTTP ${response.status}`);
    const payload = await response.json();
    const feeds = Array.isArray(payload.feeds) ? payload.feeds : [];
    if (!feeds.length) {
      console.warn("No feeds configured");
      return stats;
    }

    const cursorRow = await env.DB.prepare("SELECT value FROM meta WHERE key = ?")
      .bind("cursor").first();
    let cursor = normalizeCursor(cursorRow?.value, feeds.length);
    stats.cursorStart = cursor;
    const count = Math.min(perRun, feeds.length);

    for (let index = 0; index < count; index += 1) {
      const feed = feeds[cursor];
      stats.feeds += 1;
      try {
        const parsed = await fetchAndParseFeed(feed);
        await upsertParsedFeed(env, parsed);
        stats.feedSuccesses += 1;
        stats.articlesProcessed += parsed.articles.length;
      } catch (error) {
        console.warn(`Feed failed: ${feed?.name || feed?.feed_url || "unknown"}`, error);
      }
      cursor = (cursor + 1) % feeds.length;
      await setMeta(env, "cursor", String(cursor));
    }
    stats.cursorEnd = cursor;
    await setMeta(env, "last_run", new Date().toISOString());
    console.log(JSON.stringify(stats));
  } catch (error) {
    console.error("Scheduled refresh failed", error);
  }
  return stats;
}

async function upsertParsedFeed(env, parsed) {
  const writerStatement = env.DB.prepare(`
    INSERT INTO writers (name, url, feed_url, avatar, bio, categories, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(name) DO UPDATE SET
      url = excluded.url,
      feed_url = excluded.feed_url,
      avatar = excluded.avatar,
      bio = excluded.bio,
      categories = excluded.categories,
      updated_at = excluded.updated_at
  `).bind(
    parsed.writer.name,
    parsed.writer.url,
    parsed.writer.feed_url,
    parsed.writer.avatar,
    parsed.writer.bio,
    JSON.stringify(parsed.writer.categories),
    parsed.writer.updated_at,
  );
  const articleStatements = parsed.articles.map((article) =>
    env.DB.prepare(`
      INSERT INTO articles
        (id, url, title, excerpt, image, published, writer, category)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(url) DO NOTHING
    `).bind(
      article.id,
      article.url,
      article.title,
      article.excerpt,
      article.image,
      article.published,
      article.writer,
      article.category,
    ),
  );
  await runBatches(env.DB, [writerStatement, ...articleStatements]);
}

async function setMeta(env, key, value) {
  await env.DB.prepare(`
    INSERT INTO meta (key, value) VALUES (?, ?)
    ON CONFLICT(key) DO UPDATE SET value = excluded.value
  `).bind(key, value).run();
}

async function getMeta(env, key) {
  const row = await env.DB.prepare("SELECT value FROM meta WHERE key = ?").bind(key).first();
  return row?.value ?? null;
}

// 手動バックフィル用: GET /api/ingest?n=5 （60秒スロットル付き）
async function runIngest(url, env) {
  const lastIngest = Number(await getMeta(env, "last_ingest_ms")) || 0;
  const now = Date.now();
  if (now - lastIngest < INGEST_THROTTLE_MS) {
    return jsonResponse({ error: "Throttled. Retry shortly.", retryAfterMs: INGEST_THROTTLE_MS - (now - lastIngest) }, 429, "no-store");
  }
  await setMeta(env, "last_ingest_ms", String(now));
  const n = Math.min(INGEST_MAX, Math.max(1, positiveInt(url.searchParams.get("n"), FEEDS_PER_RUN)));
  const stats = await refreshFeeds(env, n);
  return jsonResponse(stats, 200, "no-store");
}

async function fetchAndParseFeed(feed) {
  const categories = feedCategories(feed);
  if (!feed?.name || !feed?.feed_url || !categories.length) {
    throw new Error("Feed is missing name, feed_url, or categories");
  }
  const response = await fetch(feed.feed_url, {
    headers: {
      "User-Agent": USER_AGENT,
      Accept: "application/rss+xml, application/xml, text/xml",
    },
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const xml = await response.text();
  const channel = firstTag(xml, "channel");
  if (!channel) throw new Error("RSS channel not found");

  const channelWithoutItems = channel.replace(/<item\b[\s\S]*?<\/item>/gi, "");
  const imageBlock = firstTag(channelWithoutItems, "image");
  const writerUrl = cleanText(firstTag(channelWithoutItems, "link")) || fallbackSiteUrl(feed.feed_url);
  const avatar = cleanText(firstTag(imageBlock, "url"));
  const now = new Date().toISOString();
  const articles = [];

  for (const item of allTags(channel, "item")) {
    const title = cleanText(firstTag(item, "title"));
    const url = cleanText(firstTag(item, "link"));
    const excerpt = stripHtml(firstTag(item, "description")).slice(0, 120);
    const hiragana = `${title}${excerpt}`.match(/[ぁ-ゟ]/g) || [];
    if (!title || !url || hiragana.length < 3) continue;

    const enclosure = item.match(/<enclosure\b[^>]*\burl\s*=\s*(?:"([^"]*)"|'([^']*)')[^>]*>/i);
    const date = new Date(cleanText(firstTag(item, "pubDate")));
    articles.push({
      id: await sha256(url),
      url,
      title,
      excerpt,
      image: decodeEntities(enclosure?.[1] || enclosure?.[2] || ""),
      published: Number.isNaN(date.getTime()) ? now : date.toISOString(),
      writer: feed.name,
      category: categories[0],
    });
  }

  return {
    writer: {
      name: feed.name,
      url: writerUrl,
      feed_url: feed.feed_url,
      avatar,
      bio: String(feed.bio || ""),
      categories,
      updated_at: now,
    },
    articles,
  };
}

async function getArticles(url, env) {
  const category = (url.searchParams.get("category") || "").trim();
  const writer = (url.searchParams.get("writer") || "").trim();
  const query = (url.searchParams.get("q") || "").trim();
  const page = positiveInt(url.searchParams.get("page"), 1);
  const limit = Math.min(60, Math.max(1, positiveInt(url.searchParams.get("limit"), 30)));
  const clauses = [];
  const params = [];

  if (category && category !== "すべて") {
    clauses.push("category = ?");
    params.push(category);
  }
  if (writer) {
    clauses.push("writer = ?");
    params.push(writer);
  }
  if (query) {
    const pattern = `%${escapeLike(query)}%`;
    clauses.push("(title LIKE ? ESCAPE '\\' OR excerpt LIKE ? ESCAPE '\\' OR writer LIKE ? ESCAPE '\\')");
    params.push(pattern, pattern, pattern);
  }

  const where = clauses.length ? ` WHERE ${clauses.join(" AND ")}` : "";
  const offset = (page - 1) * limit;
  const [rows, countRow] = await Promise.all([
    env.DB.prepare(`
      SELECT id, url, title, excerpt, image, published, writer, category
      FROM articles${where}
      ORDER BY published DESC
      LIMIT ? OFFSET ?
    `).bind(...params, limit, offset).all(),
    env.DB.prepare(`SELECT COUNT(*) AS total FROM articles${where}`)
      .bind(...params).first(),
  ]);
  const total = Number(countRow?.total || 0);
  return jsonResponse({
    articles: rows.results || [],
    page,
    limit,
    total,
    hasMore: offset + limit < total,
  });
}

async function getWriters(url, env) {
  const category = (url.searchParams.get("category") || "").trim();
  const params = [];
  let where = "";
  if (category && category !== "すべて") {
    where = " WHERE categories LIKE ? ESCAPE '\\'";
    params.push(`%${escapeLike(JSON.stringify(category))}%`);
  }

  const [writersResult, latestResult] = await Promise.all([
    env.DB.prepare(`
      SELECT name, url, feed_url, avatar, bio, categories
      FROM writers${where}
      ORDER BY rowid
    `).bind(...params).all(),
    env.DB.prepare(`
      SELECT writer, title, url, published
      FROM (
        SELECT writer, title, url, published,
          ROW_NUMBER() OVER (PARTITION BY writer ORDER BY published DESC) AS position
        FROM articles
      )
      WHERE position <= 3
      ORDER BY writer, published DESC
    `).all(),
  ]);

  const latestByWriter = new Map();
  for (const article of latestResult.results || []) {
    if (!latestByWriter.has(article.writer)) latestByWriter.set(article.writer, []);
    latestByWriter.get(article.writer).push({
      title: article.title,
      url: article.url,
      published: article.published,
    });
  }
  const writers = (writersResult.results || []).map((writer) => ({
    ...writer,
    categories: parseCategories(writer.categories),
    latest: latestByWriter.get(writer.name) || [],
  }));
  return jsonResponse({ writers });
}

async function getCategories(env) {
  const [articleRows, writerRows] = await Promise.all([
    env.DB.prepare("SELECT DISTINCT category FROM articles WHERE category <> '' ORDER BY category").all(),
    env.DB.prepare("SELECT categories FROM writers ORDER BY rowid").all(),
  ]);
  const categories = [];
  const seen = new Set();
  for (const writer of writerRows.results || []) {
    for (const category of parseCategories(writer.categories)) {
      if (!seen.has(category)) {
        seen.add(category);
        categories.push(category);
      }
    }
  }
  for (const row of articleRows.results || []) {
    if (row.category && !seen.has(row.category)) {
      seen.add(row.category);
      categories.push(row.category);
    }
  }
  return jsonResponse(categories);
}

async function getHealth(env) {
  const [articleRow, writerRow, cursor, lastRun] = await Promise.all([
    env.DB.prepare("SELECT COUNT(*) AS n FROM articles").first(),
    env.DB.prepare("SELECT COUNT(*) AS n FROM writers").first(),
    getMeta(env, "cursor"),
    getMeta(env, "last_run"),
  ]);
  return jsonResponse({
    ok: true,
    articles: Number(articleRow?.n || 0),
    writers: Number(writerRow?.n || 0),
    cursor: Number(cursor) || 0,
    lastRun: lastRun || null,
  }, 200, "no-store");
}

function jsonResponse(body, status = 200, cache = "public, max-age=300") {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      ...CORS_HEADERS,
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": cache,
    },
  });
}

async function runBatches(db, statements) {
  for (let index = 0; index < statements.length; index += DB_BATCH_SIZE) {
    await db.batch(statements.slice(index, index + DB_BATCH_SIZE));
  }
}

function normalizeCursor(value, length) {
  const cursor = Number.parseInt(value, 10);
  return Number.isFinite(cursor) && cursor >= 0 ? cursor % length : 0;
}

function positiveInt(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function escapeLike(value) {
  return value.replace(/[\\%_]/g, "\\$&");
}

function feedCategories(feed) {
  const value = feed?.categories ?? feed?.category ?? [];
  if (typeof value === "string") return value ? [value] : [];
  return Array.isArray(value) ? value.filter((item) => typeof item === "string" && item) : [];
}

function parseCategories(value) {
  try {
    const parsed = JSON.parse(value || "[]");
    return Array.isArray(parsed) ? parsed.filter((item) => typeof item === "string" && item) : [];
  } catch {
    return [];
  }
}

function firstTag(xml, tag) {
  if (!xml) return "";
  const match = xml.match(new RegExp(`<${tag}\\b[^>]*>([\\s\\S]*?)<\\/${tag}>`, "i"));
  return match?.[1] || "";
}

function allTags(xml, tag) {
  return [...xml.matchAll(new RegExp(`<${tag}\\b[^>]*>([\\s\\S]*?)<\\/${tag}>`, "gi"))]
    .map((match) => match[1]);
}

function cleanText(value) {
  const cdata = String(value || "").replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1");
  return decodeEntities(cdata.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim());
}

function stripHtml(value) {
  const cdata = String(value || "").replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1");
  const xmlDecoded = decodeEntities(cdata);
  return decodeEntities(xmlDecoded.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim());
}

function decodeEntities(value) {
  return String(value || "").replace(
    /&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos);/gi,
    (entity, code) => {
      const named = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };
      const lower = code.toLowerCase();
      if (named[lower]) return named[lower];
      const numeric = lower.startsWith("#x")
        ? Number.parseInt(lower.slice(2), 16)
        : Number.parseInt(lower.slice(1), 10);
      return Number.isFinite(numeric) && numeric >= 0 && numeric <= 0x10ffff
        ? String.fromCodePoint(numeric)
        : entity;
    },
  );
}

function fallbackSiteUrl(feedUrl) {
  try {
    const url = new URL(feedUrl);
    url.pathname = url.pathname.replace(/\/feed\/?$/, "/");
    url.search = "";
    url.hash = "";
    return url.href;
  } catch {
    return feedUrl;
  }
}

async function sha256(value) {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
