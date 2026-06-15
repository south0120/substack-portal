// FYL メール受信ワーカー（Stage 1: 受信して貯めるだけ）。
// Cloudflare Email Routing で inbox@findyourletter.com をこのワーカーにルーティングする。
// 各 Substack の新着投稿メールが届いたら、生メール(.eml)をそのまま R2 に保存する。
// パース/D1取込は Stage 2 で別途。ここは「1通も損しない」ことだけが責務。

const SAN = /[^A-Za-z0-9._@-]+/g;

function sanitize(value, max = 80) {
  return String(value || "").replace(SAN, "_").slice(0, max) || "unknown";
}

function pad(n) {
  return String(n).padStart(2, "0");
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
    return new Response("fyl-email: inbound email -> R2 store. See /health.", {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};
