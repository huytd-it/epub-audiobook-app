// Vercel Edge relay: chuyển tiếp một request tới provider bị giới hạn theo vùng.
//
// App gửi  POST/GET https://<relay>/api/relay  kèm hai header:
//   x-relay-key     secret dùng chung, phải trùng env RELAY_SECRET của bản deploy này
//   x-relay-target  URL đầy đủ của provider (https://generativelanguage.googleapis.com/...)
// Method, body và các header còn lại được chuyển nguyên sang provider; response được
// stream ngược về. Lỗi do chính relay sinh ra luôn kèm header x-relay-error để app phân
// biệt với lỗi thật của provider (xem app/egress.py).
//
// Đây là open proxy nếu thiếu hai lớp chặn dưới đây, nên cả hai đều bắt buộc:
// secret (ai được gọi) và allowlist host (được gọi tới đâu).

// `regions` ghim nơi function chạy. Edge Function mặc định chạy ở vùng gần người gọi
// nhất — tức là ngay trong vùng đang bị chặn — nên phải ghim tường minh. Giá trị này
// phải là hằng tĩnh (Vercel đọc lúc build): muốn vùng khác thì sửa ở đây rồi deploy
// thành một project khác (xem README.md).
export const config = { runtime: "edge", regions: ["sin1"] };

const DEFAULT_ALLOWED_HOSTS = [
  "generativelanguage.googleapis.com",
  "api.openai.com",
  "openrouter.ai",
  "api.groq.com",
  // Dùng cho nút "Kiểm tra" ở trang Mạng & Proxy: trả về IP thoát của relay.
  "api.ipify.org",
];

// Header chỉ có nghĩa giữa hai chặng kề nhau, hoặc do relay tự thêm: không chuyển tiếp.
const DROP_REQUEST_HEADERS = new Set([
  "host",
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "content-length",
  "x-relay-key",
  "x-relay-target",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-proto",
  "x-real-ip",
  "forwarded",
]);

const DROP_RESPONSE_HEADERS = new Set([
  "connection",
  "keep-alive",
  "transfer-encoding",
  "content-encoding",
  "content-length",
]);

function relayError(status: number, code: string): Response {
  return new Response(JSON.stringify({ error: code }), {
    status,
    headers: { "content-type": "application/json", "x-relay-error": code },
  });
}

function allowedHosts(): string[] {
  const raw = process.env.RELAY_ALLOWED_HOSTS;
  if (!raw) return DEFAULT_ALLOWED_HOSTS;
  return raw
    .split(",")
    .map((host) => host.trim().toLowerCase())
    .filter(Boolean);
}

// So sánh không rò thời gian: luôn duyệt hết độ dài của chuỗi dài hơn.
function safeEqual(a: string, b: string): boolean {
  const encoder = new TextEncoder();
  const left = encoder.encode(a);
  const right = encoder.encode(b);
  let diff = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let i = 0; i < length; i++) {
    diff |= (left[i] ?? 0) ^ (right[i] ?? 0);
  }
  return diff === 0;
}

export default async function handler(request: Request): Promise<Response> {
  const secret = process.env.RELAY_SECRET;
  if (!secret) return relayError(500, "relay_not_configured");

  const key = request.headers.get("x-relay-key") ?? "";
  if (!safeEqual(key, secret)) return relayError(401, "bad_key");

  let target: URL;
  try {
    target = new URL(request.headers.get("x-relay-target") ?? "");
  } catch {
    return relayError(400, "bad_target");
  }
  if (target.protocol !== "https:") return relayError(400, "https_only");
  if (!allowedHosts().includes(target.hostname.toLowerCase())) {
    return relayError(403, "host_not_allowed");
  }

  const headers = new Headers();
  request.headers.forEach((value, name) => {
    const lower = name.toLowerCase();
    if (DROP_REQUEST_HEADERS.has(lower) || lower.startsWith("x-vercel-")) return;
    headers.set(name, value);
  });

  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  let upstream: Response;
  try {
    upstream = await fetch(target.toString(), {
      method: request.method,
      headers,
      body: hasBody ? await request.arrayBuffer() : undefined,
      // Không tự đi theo redirect: đích mới có thể nằm ngoài allowlist.
      redirect: "manual",
    });
  } catch {
    return relayError(502, "upstream_unreachable");
  }

  const responseHeaders = new Headers();
  upstream.headers.forEach((value, name) => {
    if (!DROP_RESPONSE_HEADERS.has(name.toLowerCase())) responseHeaders.set(name, value);
  });
  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
}
