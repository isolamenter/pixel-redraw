"""The browser entry point: the only module that knows about Pyodide.

Split away from pixel_redraw.py on purpose.  The core must stay importable on
plain CPython (that is how it is unit-tested), and it must never read the
environment or open a socket.  Everything that is specific to running inside
the browser -- the pyfetch transport, JSON marshalling across the JS boundary,
and the error envelope conversion -- lives here instead.

The JavaScript side calls exactly two things:

    web_meta()                                  -> a JSON string, once, at boot
    await run_pipeline(src_b64, req_json, cb)   -> a JSON string envelope

Both return JSON text rather than Python objects.  A dict crossing the boundary
arrives in JS as a PyProxy that must be converted and then destroyed; a str is
an immutable primitive and crosses as a plain JS string with nothing to clean
up.  That removes an entire class of leak and conversion bug.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pixel_palettes
import pixel_redraw as pr

VERSION = "0.4.0"

# The image models a user is most likely to want, offered as autocomplete in
# the UI.  Free text is still accepted: a gateway may serve anything.
KNOWN_MODELS = (
    "gemini-3.1-flash-image",
    "gemini-3.1-flash-lite-image",
    "gemini-3-pro-image",
)


def web_meta() -> str:
    """The boot payload, in the exact shape the page's applyMeta() consumed.

    The old GET /api/meta answered this from process env.  The user now supplies
    the model, endpoint and key, so those keys are gone: the page merges its own
    settings in.  Everything that is a property of the *program* rather than of
    the user is still answered here, from the same constants the pipeline uses,
    so the density ladder and the palette list cannot drift from the renderer.
    """
    return json.dumps(
        {
            "sizes": list(pr.SIZES),
            "reference_canvas": pr.REFERENCE_CANVAS,
            "color_choices": list(pr.COLOR_CHOICES),
            "default_size": int(pr.DEFAULT_SIZE.split("x")[0]),
            "preview_scales": {str(n): pr.preview_scale_for(n) for n in pr.SIZES},
            "presets": pixel_palettes.as_meta(),
            "default_preset": pixel_palettes.DEFAULT_PRESET,
            "max_palette_colors": pixel_palettes.MAX_PALETTE_COLORS,
            "max_upload_bytes": pr.MAX_UPLOAD_BYTES,
            "max_image_pixels": pr.MAX_IMAGE_PIXELS,
            "max_dimension": pr.MAX_DIMENSION,
            "default_base_url": pr.DEFAULT_BASE_URL,
            "default_api_version": pr.DEFAULT_API_VERSION,
            "default_timeout": pr.DEFAULT_TIMEOUT,
            "default_scale": pr.DEFAULT_SCALE,
            "default_max_colors": pr.DEFAULT_MAX_COLORS,
            "default_passes": 2,
            "keep_raw": True,
            "pixelize_only_available": True,
            "known_models": list(KNOWN_MODELS),
            "version": VERSION,
        },
        ensure_ascii=False,
    )


def _resolve_palette(spec: Any) -> tuple[str, Any]:
    """Preset id and colours for a request's palette spec, via the registry."""
    return pixel_palettes.resolve(spec)


class _Transcript:
    """Keeps the last request and response so a failure can be explained.

    The deleted server wrote request.json/response.json into the run directory
    on every failure, and the README calls the upstream body "the only evidence
    that a gateway rejected you".  In the browser there is no run directory, so
    the same two documents ride along inside the error envelope and the page
    offers them as downloads.  Both go through sanitize_response() first, which
    is what strips the inline base64 image out of the response.
    """

    def __init__(self) -> None:
        self.request: Any = None
        self.response: Any = None
        self.url = ""

    def record_request(self, url: str, payload: dict[str, Any]) -> None:
        self.url = url
        self.request = {"url": url, "body": pr.sanitize_response(payload)}

    def record_response(self, parsed: Any) -> None:
        self.response = pr.sanitize_response(parsed)

    def record_raw_response(self, text: str) -> None:
        self.response = {"raw": pr.sanitize_response(text)}


def _transport_message(exc: BaseException, base_url: str) -> str:
    """Explain a failure the browser will not explain.

    A rejected cross-origin request reaches JS as a bare TypeError with no
    status and no body, and Python sees only that.  So the message has to name
    the candidate causes itself rather than pretend to diagnose one.
    """
    host = pr.safe_host(base_url)
    return (
        f"Could not reach {host}: {exc}. "
        "In a browser this usually means one of three things: the endpoint did not "
        "send CORS headers for this page's origin, the endpoint is unreachable from "
        "this machine, or a proxy refused the request. If you configured a gateway "
        "rather than the official endpoint, it must send "
        "Access-Control-Allow-Origin and allow the x-goog-api-key header."
    )


async def run_pipeline(source_b64: str, request_json: str, on_progress: Any = None) -> str:
    """Run one generation and return a JSON envelope: {"ok": ...}.

    Nothing raises across the boundary.  A Python exception that escaped to JS
    would arrive as a PythonError whose only readable field is a traceback
    string, and the frontend classifies failures by the ``kind`` attribute --
    so every failure is converted here, with the same classify()/to_envelope()
    the old server used, and returned as data.
    """
    api_key = ""
    phase = ""
    transcript = _Transcript()

    def emit(phase_name: str, label: str, detail: str = "") -> None:
        nonlocal phase
        phase = phase_name
        if on_progress is not None:
            on_progress(phase_name, label, detail)

    try:
        request = json.loads(request_json)
        source_bytes = base64.b64decode(request.get("image") or "")
        if not source_bytes:
            raise pr.ConfigError("No image was supplied.")

        upstream = request.get("upstream") or {}
        api_key = str(upstream.get("api_key") or "")
        _preset, colors = _resolve_palette(request.get("palette"))

        size = request.get("size")
        config = pr.make_config(
            model=str(upstream.get("model") or ""),
            api_key=api_key,
            base_url=str(upstream.get("base_url") or pr.DEFAULT_BASE_URL),
            api_version=str(upstream.get("api_version") or pr.DEFAULT_API_VERSION),
            timeout=upstream.get("timeout") or pr.DEFAULT_TIMEOUT,
            size=f"{int(size)}x{int(size)}" if isinstance(size, int) else (size or pr.DEFAULT_SIZE),
            scale=pr.preview_scale_for(int(size)) if isinstance(size, int) else pr.DEFAULT_SCALE,
            max_colors=request.get("max_colors"),
            palette=colors,
            prompt=str(request.get("prompt") or ""),
            refine_prompt=str(request.get("refine_prompt") or ""),
            passes=request.get("passes") or 2,
            reducer_config=request.get("reducer_config"),
        )
        pixelize_only = bool(request.get("pixelize_only"))
        is_repixelize = bool(request.get("is_repixelize"))

        async def call_upstream(payload: dict[str, Any]) -> dict[str, Any]:
            from pyodide.http import pyfetch  # imported here: this module must import anywhere

            url = pr.api_url(config.base_url, config.api_version, config.model)
            transcript.record_request(url, payload)
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                # No User-Agent: browsers forbid scripts setting it, and a
                # silently dropped header is worse than an absent one.
                "x-goog-api-key": config.api_key,
            }
            try:
                response = await asyncio.wait_for(
                    pyfetch(url, method="POST", headers=headers, body=json.dumps(payload)),
                    timeout=config.timeout,
                )
            except asyncio.TimeoutError as exc:
                raise pr.UpstreamTimeoutError(
                    f"No response within {config.timeout:g}s (wall clock). "
                    "The upstream request may still be running and may still be billed."
                ) from exc
            except Exception as exc:
                raise pr.UpstreamTransportError(
                    _transport_message(exc, config.base_url)
                ) from exc

            if not response.ok:
                text = await response.text()
                transcript.record_raw_response(text)
                raise pr.UpstreamHTTPError(
                    f"Upstream HTTP {response.status}: {text[:1200]}",
                    status=response.status,
                    detail=text,
                )

            raw = await response.bytes()  # json.loads accepts bytes; no round-trip through str
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                text = raw[:4000].decode("utf-8", errors="replace")
                transcript.record_raw_response(text)
                raise pr.UpstreamNonJSONError(
                    "Upstream returned non-JSON response "
                    f"(HTTP {response.status}): " + raw[:500].decode("utf-8", errors="replace"),
                    status=response.status,
                    detail=text,
                ) from exc
            if not isinstance(parsed, dict):
                raise pr.UpstreamProtocolError("Upstream response must be a JSON object")
            transcript.record_response(parsed)
            if parsed.get("error"):
                raise pr.UpstreamErrorField(
                    f"Upstream returned an error: {pr.compact_json(parsed['error'])}"
                )
            return parsed

        generation = await pr.generate(
            config, source_bytes, call_upstream,
            pixelize_only=pixelize_only,
            is_repixelize=is_repixelize,
            emit=emit,
            input_name=str(request.get("filename") or "") or None,
        )

        outputs = generation.outputs
        raw = outputs["raw_png"]
        pass1_raw = outputs.get("pass1_raw_png")
        pass2_raw = outputs.get("pass2_raw_png")
        draft = outputs.get("draft_png")
        guide = outputs.get("guide_png")
        guide_size = outputs.get("guide_size")
        return json.dumps(
            {
                "ok": True,
                "result": {
                    "report": outputs["report"],
                    "pixel_png": base64.b64encode(outputs["pixel_png"]).decode("ascii"),
                    "preview_png": base64.b64encode(outputs["preview_png"]).decode("ascii"),
                    "raw_png": base64.b64encode(raw).decode("ascii") if raw else None,
                    "raw_mime": pr.sniff_content_type(raw) if raw else None,
                    "pass1_raw_png": base64.b64encode(pass1_raw).decode("ascii") if pass1_raw else None,
                    "pass1_raw_mime": pr.sniff_content_type(pass1_raw) if pass1_raw else None,
                    "pass2_raw_png": base64.b64encode(pass2_raw).decode("ascii") if pass2_raw else None,
                    "pass2_raw_mime": pr.sniff_content_type(pass2_raw) if pass2_raw else None,
                    "draft_png": base64.b64encode(draft).decode("ascii") if draft else None,
                    "guide_png": base64.b64encode(guide).decode("ascii") if guide else None,
                    "guide_size": guide_size,
                    "refinement_applied": generation.refinement_applied,
                },
            },
            ensure_ascii=False,
        )
    except BaseException as exc:  # noqa: BLE001 - nothing may cross the boundary
        envelope = pr.to_envelope(exc, api_key, where=phase or "run_pipeline", phase=phase or None)
        if transcript.request is not None:
            envelope["request"] = transcript.request
        if transcript.response is not None:
            envelope["response"] = transcript.response
        return json.dumps({"ok": False, "error": envelope}, ensure_ascii=False)
