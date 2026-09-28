var DEFAULT_MODEL = "gemini-3.1-flash-lite-image";

export var settings = {
  model: DEFAULT_MODEL,
  available: false,
  configError: "",
  timeout: 180,
  prompt: "",
  refine_prompt: "",
  passes: 2
};

/* Keys saved by earlier versions should not remain in this browser after the
   UI switches to the server-managed key. */
export async function loadSettings() {
  try { sessionStorage.removeItem("pixel_settings"); } catch (e) {}
  try { localStorage.removeItem("pixel_settings_remembered"); } catch (e) {}

  try {
    var response = await fetch("/api/config", { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    var config = await response.json();
    settings.model = config.model || DEFAULT_MODEL;
    settings.available = config.ready === true;
    settings.configError = "";
  } catch (error) {
    settings.available = false;
    settings.configError = String((error && error.message) || error);
  }
  return settings;
}

export function redactSecrets(text) {
  var out = String(text === null || text === undefined ? "" : text);
  out = out.replace(/(x-goog-api-key\s*[:=]\s*)\S+/gi, "$1[redacted]");
  out = out.replace(/(Authorization\s*[:=]\s*)\S+/gi, "$1[redacted]");
  out = out.replace(/(Bearer\s+)\S+/gi, "$1[redacted]");
  out = out.replace(/\bAIza[0-9A-Za-z_\-]{35}\b/g, "[redacted:key-shaped]");
  out = out.replace(/(https?:\/\/)[^/@\s]+@/g, "$1[redacted]@");
  return out;
}

export function upstreamBlock() {
  return {
    model: settings.model,
    proxy_url: new URL("/api/generate", window.location.origin).href,
    timeout: settings.timeout
  };
}

export function upstreamReady() {
  return settings.available;
}

export function upstreamHost() {
  return "generativelanguage.googleapis.com";
}
