const DEFAULT_MODEL = 'gemini-3.1-flash-lite-image';

export function onRequestGet({ env }) {
  return Response.json({
    model: String(env.GEMINI_MODEL || '').trim() || DEFAULT_MODEL,
    ready: Boolean(String(env.GEMINI_API_KEY || '').trim()),
  }, {
    headers: { 'Cache-Control': 'no-store' },
  });
}
