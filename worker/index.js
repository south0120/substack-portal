const FEEDS_URL = "https://raw.githubusercontent.com/south0120/substack-portal/main/feeds.json";
const USER_AGENT = "find-your-letter/1.0 (+https://findyourletter.com)";
const FEEDS_PER_RUN = 30;
const INGEST_MAX = 22;
const INGEST_THROTTLE_MS = 60000;
const DB_BATCH_SIZE = 50;
const GITHUB_OWNER = "south0120";
const GITHUB_REPO = "substack-portal";
const GITHUB_API = "https://api.github.com";
const MASTER_CATEGORIES = new Set([
  "AI", "テクノロジー", "ビジネス", "投資・経済", "社会・文化", "ライフスタイル",
  "クリエイティブ", "キャリア・働き方", "健康・ウェルネス", "教育・学び",
  "エンタメ", "旅行・おでかけ", "グルメ・料理", "スポーツ", "子育て・家族",
  "マンガ・アニメ", "音楽", "読書", "ゲーム", "ファッション・美容", "その他",
]);
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, tally-signature",
};

export default {
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(refreshFeeds(env));
  },

  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    try {
      const url = new URL(request.url);
      if (url.pathname === "/api/apply" && request.method === "POST") {
        return handleApplication(request, env);
      }
      if (request.method !== "GET") {
        return jsonResponse({ error: "Method not allowed" }, 405, "no-store");
      }
      if (url.pathname === "/api/articles") return getArticles(url, env);
      if (url.pathname === "/api/writers") return getWriters(url, env);
      if (url.pathname === "/api/categories") return getCategories(env);
      if (url.pathname === "/api/health") return getHealth(env);
      if (url.pathname === "/api/ingest") return runIngest(url, env);
      if (url.pathname === "/api/applications") return getApplications(env);
      if (url.pathname === "/api/proxy") return handleProxy(url, request, env);
      return jsonResponse({ error: "Not found" }, 404);
    } catch (error) {
      console.error("API error", error);
      return jsonResponse({ error: "Internal server error" }, 500);
    }
  },
};

async function handleApplication(request, env) {
  try {
    const rawBody = await request.text();
    if (env.TALLY_SIGNING_SECRET) {
      const valid = await verifyTallySignature(
        rawBody,
        env.TALLY_SIGNING_SECRET,
        request.headers.get("tally-signature"),
      );
      if (!valid) return jsonResponse({ error: "invalid_signature" }, 401, "no-store");
    }

    let payload;
    try {
      payload = JSON.parse(rawBody);
    } catch {
      return jsonResponse({ error: "invalid_json" }, 400, "no-store");
    }

    const fields = extractTallyFields(payload);
    if (!fields.name || !fields.rawUrl) {
      return jsonResponse({ error: "name_and_url_required" }, 400, "no-store");
    }

    let feedUrl;
    try {
      feedUrl = normalizeFeedUrl(fields.rawUrl);
    } catch {
      return jsonResponse({ error: "invalid_feed_url" }, 400, "no-store");
    }
    const category = MASTER_CATEGORIES.has(fields.category) ? fields.category : "その他";

    if (!(await verifyJapaneseFeed(feedUrl))) {
      return jsonResponse({ error: "feed_unreachable_or_not_japanese" }, 422, "no-store");
    }

    const feedsResponse = await fetch(FEEDS_URL, {
      headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
      signal: AbortSignal.timeout(10000),
    });
    if (!feedsResponse.ok) throw new Error(`feeds.json: HTTP ${feedsResponse.status}`);
    const feedsPayload = await feedsResponse.json();
    const feeds = Array.isArray(feedsPayload.feeds) ? feedsPayload.feeds : [];
    const alreadyListed = feeds.some((feed) =>
      String(feed?.name || "").trim() === fields.name
      || comparableFeedUrl(feed?.feed_url) === feedUrl
    );
    if (alreadyListed) {
      return jsonResponse({ error: "already_listed" }, 409, "no-store");
    }

    const pending = await env.DB.prepare(`
      SELECT id FROM applications WHERE feed_url = ? AND status = 'pending' LIMIT 1
    `).bind(feedUrl).first();
    if (pending) {
      return jsonResponse({ error: "already_pending" }, 409, "no-store");
    }

    const timestamp = new Date().toISOString();
    const id = await sha256(`${feedUrl}${timestamp}`);
    await env.DB.prepare(`
      INSERT INTO applications (id, name, feed_url, category, bio, status, created_at)
      VALUES (?, ?, ?, ?, ?, 'pending', ?)
    `).bind(id, fields.name, feedUrl, category, fields.bio, timestamp).run();

    if (!env.GITHUB_TOKEN) {
      return jsonResponse(
        { ok: true, pr: null, note: "GITHUB_TOKEN未設定" },
        200,
        "no-store",
      );
    }

    const result = await publishApplication(env, {
      id,
      name: fields.name,
      feed_url: feedUrl,
      category,
      bio: fields.bio,
    });
    await env.DB.prepare(`
      UPDATE applications SET status = 'pr_created', pr_url = ? WHERE id = ?
    `).bind(result.url, id).run();
    return jsonResponse({ ok: true, pr: result.url }, 200, "no-store");
  } catch (error) {
    console.error("Application webhook failed", error);
    return jsonResponse({ error: "Internal server error" }, 500, "no-store");
  }
}

async function handleProxy(url, request, env) {
  const secret = request.headers.get("x-proxy-secret");
  if (!env.PROXY_SECRET || secret !== env.PROXY_SECRET) {
    return new Response("Unauthorized", { status: 401 });
  }
  const targetUrl = url.searchParams.get("url");
  if (!targetUrl) {
    return new Response("url parameter required", { status: 400 });
  }
  let parsedTarget;
  try {
    parsedTarget = new URL(targetUrl);
    if (parsedTarget.protocol !== "https:" && parsedTarget.protocol !== "http:") {
      throw new Error("invalid protocol");
    }
  } catch {
    return new Response("Invalid URL", { status: 400 });
  }
  const response = await fetch(parsedTarget.href, {
    headers: {
      "User-Agent": USER_AGENT,
      Accept: "application/rss+xml, application/atom+xml, application/xml, text/xml",
    },
    signal: AbortSignal.timeout(15000),
  });
  const body = await response.arrayBuffer();
  return new Response(body, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("Content-Type") || "application/xml; charset=utf-8",
    },
  });
}

async function getApplications(env) {
  const rows = await env.DB.prepare(`
    SELECT id, name, feed_url, category, status, pr_url, created_at
    FROM applications
    ORDER BY created_at DESC
    LIMIT 50
  `).all();
  return jsonResponse({ applications: rows.results || [] }, 200, "no-store");
}

export function extractTallyFields(payload) {
  const result = { name: "", rawUrl: "", category: "", bio: "" };
  const fields = Array.isArray(payload?.data?.fields) ? payload.data.fields : [];
  for (const field of fields) {
    const label = String(field?.label || "").toLowerCase().replace(/\s+/g, "");
    const value = Array.isArray(field?.value) ? field.value[0] : field?.value;
    const text = String(value ?? "").trim();
    if (!text) continue;
    if (!result.rawUrl && (label.includes("url") || label.includes("substack"))) {
      result.rawUrl = text;
    } else if (!result.category && (label.includes("カテゴリ") || label.includes("category"))) {
      result.category = text.slice(0, 30);
    } else if (!result.bio && (label.includes("自己紹介") || label.includes("紹介") || label.includes("bio"))) {
      result.bio = text.slice(0, 200);
    } else if (!result.name && (label.includes("掲載名") || label.includes("名前") || label.includes("name"))) {
      result.name = text.slice(0, 80);
    }
  }
  return result;
}

export function normalizeFeedUrl(rawUrl) {
  let value = String(rawUrl || "").trim();
  if (!/^[a-z][a-z\d+.-]*:\/\//i.test(value)) value = `https://${value}`;
  const url = new URL(value);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("Feed URL must use HTTP(S)");
  }
  url.protocol = "https:";
  url.username = "";
  url.password = "";
  url.pathname = /\/feed\/?$/i.test(url.pathname)
    ? url.pathname.replace(/\/+$/, "")
    : "/feed";
  url.search = "";
  url.hash = "";
  return url.href.replace(/\/$/, "");
}

function comparableFeedUrl(value) {
  try {
    return normalizeFeedUrl(value);
  } catch {
    return "";
  }
}

async function verifyTallySignature(rawBody, secret, signature) {
  if (!signature) return false;
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(rawBody)));
  return bytesToBase64(digest) === signature.trim();
}

async function verifyJapaneseFeed(feedUrl) {
  try {
    const response = await fetch(feedUrl, {
      headers: {
        "User-Agent": USER_AGENT,
        Accept: "application/rss+xml, application/atom+xml, application/xml, text/xml",
      },
      signal: AbortSignal.timeout(10000),
    });
    if (response.status !== 200) return false;
    const xml = await response.text();
    if (!/<(?:rss|feed)\b/i.test(xml)) return false;
    const itemBlocks = allTags(xml, "item");
    const entryBlocks = itemBlocks.length ? [] : allTags(xml, "entry");
    const titles = [...itemBlocks, ...entryBlocks]
      .slice(0, 3)
      .map((item) => cleanText(firstTag(item, "title")))
      .join("");
    return (titles.match(/[ぁ-ゟ]/g) || []).length >= 3;
  } catch {
    return false;
  }
}

async function publishApplication(env, application) {
  const token = env.GITHUB_TOKEN;
  const contents = await githubRequest(token, `/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/feeds.json?ref=main`);
  const parsed = JSON.parse(decodeBase64Utf8(contents.content));
  const feeds = Array.isArray(parsed.feeds) ? parsed.feeds : [];
  feeds.push({
    name: application.name,
    feed_url: application.feed_url,
    categories: [application.category],
    bio: application.bio,
  });
  parsed.feeds = feeds;
  const encodedContent = encodeBase64Utf8(`${JSON.stringify(parsed, null, 1)}\n`);
  const message = `apply: ${application.name} を追加`;

  if (env.AUTO_COMMIT === "true") {
    await githubRequest(
      token,
      `/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/feeds.json`,
      {
        method: "PUT",
        body: {
          message,
          content: encodedContent,
          sha: contents.sha,
          branch: "main",
        },
      },
    );
    return { url: null };
  }

  const mainRef = await githubRequest(
    token,
    `/repos/${GITHUB_OWNER}/${GITHUB_REPO}/git/ref/heads/main`,
  );
  const slug = application.name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 40) || "writer";
  const branch = `apply/${slug}-${application.id.slice(0, 8)}`;
  await githubRequest(token, `/repos/${GITHUB_OWNER}/${GITHUB_REPO}/git/refs`, {
    method: "POST",
    body: { ref: `refs/heads/${branch}`, sha: mainRef.object.sha },
  });
  await githubRequest(token, `/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/feeds.json`, {
    method: "PUT",
    body: {
      message,
      content: encodedContent,
      sha: contents.sha,
      branch,
    },
  });
  const pull = await githubRequest(token, `/repos/${GITHUB_OWNER}/${GITHUB_REPO}/pulls`, {
    method: "POST",
    body: {
      title: `掲載申請: ${application.name}`,
      body: [
        "## 申請内容",
        "",
        `- 掲載名: ${application.name}`,
        `- Feed URL: ${application.feed_url}`,
        `- カテゴリ: ${application.category}`,
        `- 自己紹介: ${application.bio || "（未記入）"}`,
        "",
        "## 確認チェックリスト",
        "",
        "- [ ] フィードと掲載名が申請者のものか",
        "- [ ] 日本語コンテンツとして掲載可能か",
        "- [ ] カテゴリと自己紹介が適切か",
      ].join("\n"),
      head: branch,
      base: "main",
    },
  });
  return { url: pull.html_url || null };
}

async function githubRequest(token, path, options = {}) {
  const response = await fetch(`${GITHUB_API}${path}`, {
    method: options.method || "GET",
    headers: {
      Authorization: `Bearer ${token}`,
      "User-Agent": USER_AGENT,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`GitHub ${options.method || "GET"} ${path}: HTTP ${response.status} ${detail.slice(0, 500)}`);
  }
  return response.json();
}

function encodeBase64Utf8(value) {
  return bytesToBase64(new TextEncoder().encode(value));
}

function decodeBase64Utf8(value) {
  const binary = atob(String(value || "").replace(/\s+/g, ""));
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function bytesToBase64(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function refreshFeeds(env, perRun = FEEDS_PER_RUN) {
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

    const cursorValue = await getMeta(env, "cursor");
    let cursor = normalizeCursor(cursorValue, feeds.length);
    stats.cursorStart = cursor;
    const count = Math.min(perRun, feeds.length);
    const feedsByUrl = new Map(feeds.map((feed) => [feed.feed_url, feed]));
    let retryQueue = [];
    try {
      const parsedRetryQueue = JSON.parse(await getMeta(env, "retry_queue") || "[]");
      if (Array.isArray(parsedRetryQueue)) {
        retryQueue = parsedRetryQueue.filter((feedUrl) => feedsByUrl.has(feedUrl));
      }
    } catch {
      console.warn("Invalid retry_queue meta value; resetting");
    }

    const batch = [];
    const batchUrls = new Set();
    for (const feedUrl of retryQueue) {
      if (batch.length >= count || batchUrls.has(feedUrl)) continue;
      batch.push(feedsByUrl.get(feedUrl));
      batchUrls.add(feedUrl);
    }

    let cursorAdvances = 0;
    const normalCursorValues = new Map();
    while (batch.length < count && cursorAdvances < feeds.length) {
      const feed = feeds[cursor];
      cursor = (cursor + 1) % feeds.length;
      cursorAdvances += 1;
      if (!batchUrls.has(feed.feed_url)) {
        batch.push(feed);
        batchUrls.add(feed.feed_url);
        normalCursorValues.set(feed.feed_url, cursor);
      }
    }

    const failedUrls = [];
    for (const feed of batch) {
      stats.feeds += 1;
      try {
        const parsed = await fetchAndParseFeed(feed);
        await upsertParsedFeed(env, parsed);
        stats.feedSuccesses += 1;
        stats.articlesProcessed += parsed.articles.length;
      } catch (error) {
        failedUrls.push(feed.feed_url);
        console.warn(`Feed failed: ${feed?.name || feed?.feed_url || "unknown"}`, error);
      }
      if (normalCursorValues.has(feed.feed_url)) {
        await setMeta(env, "cursor", String(normalCursorValues.get(feed.feed_url)));
      }
    }
    const unprocessedRetryUrls = retryQueue.filter((feedUrl) => !batchUrls.has(feedUrl));
    const uniqueFailedUrls = [...new Set(failedUrls)];
    const retainedRetryUrls = unprocessedRetryUrls.slice(-(60 - uniqueFailedUrls.length));
    const nextRetryQueue = [...uniqueFailedUrls, ...retainedRetryUrls];
    await setMeta(env, "retry_queue", JSON.stringify(nextRetryQueue));
    stats.cursorEnd = cursor;
    await setMeta(env, "last_run", new Date().toISOString());
    console.log(JSON.stringify(stats));
  } catch (error) {
    console.error("Scheduled refresh failed", error);
  }
  return stats;
}

async function upsertParsedFeed(env, parsed) {
  // パブリケーション名の変更(rename)対応 + 重複の自己修復。書き手はnameをキーに
  // 記録しているが、Substackは名前を変えてもfeed_url(サブドメイン)は不変。
  // 同じfeed_urlが別名で登録されていたら、その旧記事を現名へ張り替え(relink)し、
  // 最後に同一feed_urlの別名行をまとめて削除する(cleanupStatement)。これで名前変更や
  // 表記ゆれで生じた重複(旧名+新名)を毎回の取込で確実に1人へ統合する。
  const renameStatements = [];
  if (parsed.writer.feed_url) {
    const priors = await env.DB.prepare(
      "SELECT name FROM writers WHERE feed_url = ? AND name <> ?"
    ).bind(parsed.writer.feed_url, parsed.writer.name).all();
    for (const row of priors.results || []) {
      if (!row.name) continue;
      renameStatements.push(
        env.DB.prepare("UPDATE articles SET writer = ? WHERE writer = ?").bind(parsed.writer.name, row.name),
      );
    }
  }
  const cleanupStatement = parsed.writer.feed_url
    ? env.DB.prepare("DELETE FROM writers WHERE feed_url = ? AND name <> ?").bind(parsed.writer.feed_url, parsed.writer.name)
    : null;
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
      ON CONFLICT(url) DO UPDATE SET
        published = COALESCE(excluded.published, articles.published)
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
  await runBatches(env.DB, [
    ...renameStatements,
    writerStatement,
    ...articleStatements,
    ...(cleanupStatement ? [cleanupStatement] : []),
  ]);
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
  // Substack が Cloudflare の共有IPを確率的に拒否するため、429は指数バックオフでリトライ
  let response = null;
  let rateLimitRetries = 0;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      response = await fetch(feed.feed_url, {
        headers: {
          "User-Agent": USER_AGENT,
          Accept: "application/rss+xml, application/xml, text/xml",
        },
        signal: AbortSignal.timeout(15000),
      });
      if (response.ok) break;
      if (response.status === 429 && rateLimitRetries < 2 && attempt < 2) {
        await new Promise((resolve) => setTimeout(resolve, 2000 * (2 ** rateLimitRetries)));
        rateLimitRetries += 1;
        continue;
      }
    } catch (error) {
      if (attempt >= 1) throw error;
      response = null;
    }
    if (attempt === 0) await new Promise((resolve) => setTimeout(resolve, 1500));
    else break;
  }
  if (!response || !response.ok) throw new Error(`HTTP ${response ? response.status : "fetch_failed"}`);
  const xml = await response.text();
  const channel = firstTag(xml, "channel");
  if (!channel) throw new Error("RSS channel not found");

  const channelWithoutItems = channel.replace(/<item\b[\s\S]*?<\/item>/gi, "");
  const imageBlock = firstTag(channelWithoutItems, "image");
  const writerUrl = cleanText(firstTag(channelWithoutItems, "link")) || fallbackSiteUrl(feed.feed_url);
  const avatar = cleanText(firstTag(imageBlock, "url"));
  // 書き手名は feeds.json の整形済み name を使う（安定）。RSS の channel title は
  // フェッチ毎に表記ゆれ（記号/空白差）があり、採用すると名前が揺れて重複の原因に
  // なるため使わない。意図的な改名は feeds.json を更新して反映する運用。
  const writerName = feed.name;
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
      published: Number.isNaN(date.getTime()) ? null : date.toISOString(),
      writer: writerName,
      category: categories[0],
    });
  }

  return {
    writer: {
      name: writerName,
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
