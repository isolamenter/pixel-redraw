const API_ROOT = 'https://generativelanguage.googleapis.com/v1beta/models/';
const DEFAULT_MODEL = 'gemini-3.1-flash-lite-image';
const MAX_REQUEST_BYTES = 32 * 1024 * 1024;

function jsonError(message, status) {
  return Response.json({ error: { message } }, {
    status,
    headers: { 'Cache-Control': 'no-store' },
  });
}

export async function onRequestPost({ request, env }) {
  const apiKey = String(env.GEMINI_API_KEY || '').trim();
  if (!apiKey) return jsonError('服务器尚未配置 Gemini API Key。', 503);

  const contentType = request.headers.get('content-type') || '';
  if (!contentType.toLowerCase().startsWith('application/json')) {
    return jsonError('请求必须使用 application/json。', 415);
  }

  const contentLength = Number(request.headers.get('content-length') || 0);
  if (contentLength > MAX_REQUEST_BYTES) {
    return jsonError('请求内容超过 32 MiB 上限。', 413);
  }

  const origin = request.headers.get('origin');
  if (origin && origin !== new URL(request.url).origin) {
    return jsonError('仅接受本站发起的请求。', 403);
  }

  const model = String(env.GEMINI_MODEL || '').trim() || DEFAULT_MODEL;
  const url = API_ROOT + encodeURIComponent(model) + ':generateContent';
  let upstream;
  try {
    upstream = await fetch(url, {
      method: 'POST',
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'x-goog-api-key': apiKey,
      },
      body: request.body,
    });
  } catch (_) {
    return jsonError('无法连接 Gemini 官方端点。', 502);
  }

  if (!upstream.ok) {
    const body = (await upstream.text()).split(apiKey).join('[redacted:api_key]');
    return new Response(body, {
      status: upstream.status,
      headers: {
        'Content-Type': upstream.headers.get('content-type') || 'application/json',
        'Cache-Control': 'no-store',
      },
    });
  }

  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      'Content-Type': upstream.headers.get('content-type') || 'application/json',
      'Cache-Control': 'no-store',
    },
  });
}
