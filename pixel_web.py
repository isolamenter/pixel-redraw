#!/usr/bin/env python3
"""Localhost web front end for pixel-redraw.

This module adds an HTTP layer and NOTHING else: every pixel decision is still
made by pixel_redraw.py, called through its real public functions.  If you find
yourself reimplementing downscaling, quantisation or palette snapping here,
stop -- that logic belongs in the core and duplicating it is how the two
front ends drift apart.

Three things shape the design:

* The API key and model come from the environment only (requirement 5).  No
  route reads them from a request, and redact() is the single choke point that
  keeps the key out of every response, log line and debug artifact.
* Errors are shown verbatim in the browser (requirement 6).  That fights
  requirement 5 directly -- a gateway is free to echo the request header back
  inside a 401 body -- so the rule is: errors are verbatim EXCEPT secrets, and
  the UI says so.
* The one slow step is a single opaque blocking upstream call with no
  sub-progress signal, so the progress UI reports stages and elapsed time and
  never a percentage.

Binds 127.0.0.1 only.  Do not change that: this process holds the API key, and
a non-loopback address would also break the clipboard API in the browser,
which requires a secure context (localhost qualifies; a bare LAN IP does not).
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import io
import json
import os
import re
import struct
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from PIL import Image

import pixel_palettes
import pixel_redraw as pr


class RequestError(pr.PixelError):
    """A malformed request from the browser. Distinct from a core failure."""

    kind = "request"


class BusyError(pr.PixelError):
    kind = "busy"


class ForbiddenError(pr.PixelError):
    kind = "forbidden"


class LimitsError(pr.PixelError):
    """Input too large to process. Distinct from a bad size request."""

    kind = "limits"


class NotFoundError(pr.PixelError):
    kind = "not_found"


class PaletteViolationError(pr.PixelError):
    """The subset guarantee broke. Must be reported, never shipped silently."""

    kind = "palette_violation"

VERSION = "0.2.0"
ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"

SIZES = (8, 16, 32, 64)
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_DIMENSION = 4096
MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES * 2  # base64 inflation + JSON envelope headroom

# Our own guard must fire before Pillow's, so the user gets a message naming the
# real numbers instead of a DecompressionBombError.  Pillow only *warns* at its
# default 89,478,485 and does not raise until 2x that, by which point a
# 144-megapixel image has already allocated ~585MB.
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS * 2

# Stages, in order, per run mode.  A real statement boundary sits between each
# pair; nothing is emitted on a timer.
STAGES_MODEL = (
    "received", "encoding", "upstream_wait", "upstream_response",
    "extract_start", "extracted", "pixelizing", "verifying", "saving", "done",
)
STAGES_LOCAL = ("received", "encoding", "pixelizing", "verifying", "saving", "done")

# Error kind -> HTTP status.  Classification is structural (the typed exceptions
# the core raises), never a match on message wording: several core raise sites
# have __cause__ = None, so a cause-based classifier would report a routine
# relay quota failure to the user as an internal bug.
KIND_STATUS = {
    "config": 500,
    "palette": 400,
    "size": 400,
    "limits": 413,
    "input_image": 400,
    "upstream_http": 502,
    "upstream_transport": 502,
    "upstream_timeout": 504,
    "upstream_non_json": 502,
    "upstream_protocol": 502,
    "upstream_error_field": 502,
    "no_image": 502,
    "palette_violation": 500,
    "invariant": 500,
    "busy": 409,
    "request": 400,
    "not_found": 404,
    "forbidden": 403,
    "client_disconnect": 499,
    "internal": 500,
}

KIND_HINT = {
    "config": "检查 .env：NEWAPI_BASE_URL / NEWAPI_API_KEY / NEWAPI_MODEL 三项都必须有值。密钥只能通过环境变量配置，不要填到网页里。",
    "palette": "调色板写法：#RRGGBB 逗号分隔，也接受 #RGB 简写。",
    "size": "尺寸只能是 8、16、32、64。",
    "limits": "图片超过了服务端限制。请先缩小图片再上传。",
    "input_image": "这个文件不是可识别的图片，或者内容已损坏。",
    "upstream_http": "网关拒绝了请求。若报错里提到 responseModalities 或 imageConfig，把对应的 .env 变量清空后重试。",
    "upstream_transport": "连不上网关。确认 NEWAPI_BASE_URL 指向的地址在运行、且本机能访问。",
    "upstream_timeout": "单次 socket 操作超过 NEWAPI_TIMEOUT 秒仍未完成。上游请求可能仍在生成，且可能仍在计费——NEWAPI_TIMEOUT 是逐 socket 超时，不是墙钟总时限，所以不要因为等待超过预算就判定失败。",
    "upstream_non_json": "网关返回了非 JSON（通常是 HTML 错误页）。检查网关地址是否指到了反向代理或错误路由。",
    "upstream_protocol": "网关返回的 JSON 结构不是对象，可能不是 Gemini 协议端点。",
    "upstream_error_field": "网关在 HTTP 200 里返回了 error 对象，通常是配额或权限问题。",
    "no_image": "确认 NEWAPI_MODEL 支持出图，且 NEWAPI_RESPONSE_MODALITIES 含 IMAGE。完整响应见 response.json。",
    "palette_violation": "这是内部一致性断言失败，说明降采样或量化出现了回归。像素本应严格来自调色板。",
    "invariant": "内部一致性断言失败。",
    "busy": "已经有一个任务在运行。可以点「查看进度」接上它。",
    "request": "请求格式不对。",
    "not_found": "这个 run_id 不存在，或者服务端重启过（运行记录在内存里）。",
    "client_disconnect": "客户端断开了连接，或者上游中断了传输。",
    "forbidden": "用 http://127.0.0.1:PORT 或 http://localhost:PORT 访问。本服务只绑定回环地址。",
    "internal": "这是服务端自身的缺陷，不是上游问题。",
}


# --------------------------------------------------------------------------
# secret redaction
# --------------------------------------------------------------------------

_SECONDARY_PATTERNS = (
    re.compile(r"(x-goog-api-key\s*[:=]\s*)\S+", re.IGNORECASE),
    re.compile(r'("(?:api[_-]?key|apikey|key|token)"\s*:\s*")([^"]{6,})(")', re.IGNORECASE),
    re.compile(r"(Authorization\s*[:=]\s*)\S+", re.IGNORECASE),
    re.compile(r"(Bearer\s+)\S+", re.IGNORECASE),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
)
_LONG_BASE64 = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")
_USERINFO = re.compile(r"(https?://)[^/@\s]+@")


def redact(text: str, api_key: str) -> str:
    """Remove secret material from anything headed for the browser or a log.

    Applied to EVERY string field of the error envelope -- message, hint,
    where, detail and traceback -- because a traceback can carry a frame's
    local variables and is therefore not exempt.

    The exact key and its base64 form are replaced first (we know the secret,
    so this is exact rather than heuristic); the pattern sweep afterwards is a
    backstop for credentials we do not hold, such as a key an upstream error
    echoes after a rotation.
    """
    if not text:
        return text
    if api_key and len(api_key) >= 6:
        text = text.replace(api_key, "[redacted:NEWAPI_API_KEY]")
        encoded = base64.b64encode(api_key.encode()).decode()
        text = text.replace(encoded, "[redacted:NEWAPI_API_KEY:b64]")
    for pattern in _SECONDARY_PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(lambda m: m.group(1) + "[redacted]" + m.group(3), text)
        elif pattern.groups >= 1:
            text = pattern.sub(lambda m: m.group(1) + "[redacted]", text)
        else:
            text = pattern.sub("[redacted:key-shaped]", text)
    text = _USERINFO.sub(r"\1[redacted]@", text)
    text = _LONG_BASE64.sub(lambda m: f"[base64 {len(m.group(0))} chars omitted]", text)
    return text


def sanitize_response(value: Any, depth: int = 0) -> Any:
    """Strip long strings from an upstream response so it is safe to persist.

    Any string over 2000 characters becomes a placeholder.  That removes the
    inline base64 image and any long echoed blob while leaving the model's
    short explanation text -- which is the whole point, because
    value_for_hint() truncates at 240 characters and compact_json at 1000, so
    a 900-character refusal reaches the CLI's error message as 240.
    """
    if depth > 12:
        return "<depth limit>"
    if isinstance(value, dict):
        return {str(k): sanitize_response(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_response(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        # The budget has to clear a model's prose explanation (which is the
        # point of keeping this file) while still removing an inline base64
        # image, and no real image is under 2KB.
        if len(value) > 2000:
            return f"<{len(value)}-char string omitted>"
        return value
    return value


# --------------------------------------------------------------------------
# error envelope
# --------------------------------------------------------------------------


def classify(exc: BaseException, where: str) -> str:
    """Map an exception to an error kind, structurally."""
    if isinstance(exc, pr.PixelError):
        return exc.kind
    # These escape unwrapped from call_newapi: urllib's timeout is a bare
    # TimeoutError with __cause__ = None, and it is the most common slow-gateway
    # failure.  Before this mapping the user saw a Python traceback.
    if isinstance(exc, TimeoutError):
        return "upstream_timeout"
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        # During the upstream exchange this is far more often the gateway
        # dropping the connection than the browser.
        if where in ("call_newapi", "extract_image", "download_image"):
            return "upstream_transport"
        return "client_disconnect"
    if isinstance(exc, Image.DecompressionBombError):
        return "limits"
    if isinstance(exc, Image.UnidentifiedImageError):
        return "input_image"
    if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)):
        return "config"
    return "internal"


def to_envelope(
    exc: BaseException,
    where: str,
    api_key: str,
    run_id: str | None = None,
    phase: str | None = None,
    debug: bool = True,
) -> dict[str, Any]:
    """Build the ONE error shape used by both the HTTP body and the SSE frame."""
    kind = classify(exc, where)
    detail = getattr(exc, "detail", "") or ""
    status = getattr(exc, "status", None)

    message = redact(str(exc), api_key)
    hint = KIND_HINT.get(kind, "")
    if kind == "upstream_http" and status:
        hint = f"网关返回 HTTP {status}。" + hint

    envelope: dict[str, Any] = {
        "kind": kind,
        "phase": phase,
        "where": where,
        "exception": f"{type(exc).__module__}.{type(exc).__name__}",
        "message": message,
        "detail": redact(detail[:4000], api_key) + ("...[truncated]" if len(detail) > 4000 else ""),
        "http_status": status,
        "hint": hint,
        "run_id": run_id,
    }
    if debug:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        envelope["traceback"] = redact(tb, api_key)
    if run_id:
        envelope["debug_urls"] = {
            "response": f"/api/runs/{run_id}/response.json",
            "request": f"/api/runs/{run_id}/request.json",
            "state": f"/api/runs/{run_id}/state",
        }
    return envelope


# --------------------------------------------------------------------------
# upload dimension sniffing (before any decode)
# --------------------------------------------------------------------------


def sniff_dimensions(blob: bytes) -> tuple[int, int] | None:
    """Read width/height from the container header, without decoding.

    A byte-size cap does NOT stop a decompression bomb: a 12000x12000 PNG is
    about 450KB on the wire and decodes to 144 megapixels.  So the dimensions
    are read from the header first, in about a millisecond, and the request is
    rejected before Image.open() can allocate anything.
    """
    try:
        if blob[:8] == b"\x89PNG\r\n\x1a\n" and blob[12:16] == b"IHDR":
            width, height = struct.unpack(">II", blob[16:24])
            return int(width), int(height)
        if blob[:6] in (b"GIF87a", b"GIF89a"):
            width, height = struct.unpack("<HH", blob[6:10])
            return int(width), int(height)
        if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            chunk = blob[12:16]
            if chunk == b"VP8X":
                width = 1 + int.from_bytes(blob[24:27], "little")
                height = 1 + int.from_bytes(blob[27:30], "little")
                return width, height
            if chunk == b"VP8 ":
                width = int.from_bytes(blob[26:28], "little") & 0x3FFF
                height = int.from_bytes(blob[28:30], "little") & 0x3FFF
                return width, height
            if chunk == b"VP8L":
                bits = int.from_bytes(blob[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if blob[:2] == b"\xff\xd8":
            offset = 2
            while offset + 9 < len(blob):
                if blob[offset] != 0xFF:
                    offset += 1
                    continue
                marker = blob[offset + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    offset += 2
                    continue
                length = struct.unpack(">H", blob[offset + 2 : offset + 4])[0]
                # SOF0..SOF15, excluding the non-frame markers DHT/JPG/DAC.
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", blob[offset + 5 : offset + 9])
                    return int(width), int(height)
                offset += 2 + length
    except (struct.error, IndexError):
        return None
    return None


# --------------------------------------------------------------------------
# run state
# --------------------------------------------------------------------------


class Run:
    """One generation job. The worker owns the pixel work; this owns the state.

    Threading invariant: the worker only appends events under ``cond`` and
    calls notify_all(); it NEVER touches a socket.  The SSE handler thread only
    reads the event list and writes.  Exactly one thread writes any given
    socket -- which is why the model call must not run on the SSE handler's
    thread, or the progress bar freezes for the whole 180 seconds.
    """

    def __init__(self, run_id: str, mode: str):
        self.id = run_id
        self.mode = mode
        self.cond = threading.Condition()
        self.events: list[dict[str, Any]] = []
        self.status = "running"  # running | done | error
        self.error: dict[str, Any] | None = None
        self.result: dict[str, Any] | None = None
        self.artifacts: dict[str, tuple[bytes, str]] = {}
        self.has_raw = False
        self.started_monotonic = time.monotonic()
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.dir = RUNS_DIR / run_id
        self.raw_bytes: bytes | None = None
        self.config: pr.Config | None = None

    # -- events ------------------------------------------------------------
    def emit(self, phase: str, label: str, detail: dict[str, Any] | None = None) -> None:
        with self.cond:
            self.events.append(
                {
                    "seq": len(self.events),
                    "phase": phase,
                    "label": label,
                    "detail": detail or {},
                    "elapsed_ms": int((time.monotonic() - self.started_monotonic) * 1000),
                }
            )
            self.cond.notify_all()

    def finish(self, error: dict[str, Any] | None = None, result: dict[str, Any] | None = None) -> None:
        with self.cond:
            self.status = "error" if error else "done"
            self.error = error
            self.result = result
            payload = error or result or {}
            event = {
                "seq": len(self.events),
                "phase": "error" if error else "done",
                "label": "失败" if error else "完成",
                "detail": payload,
                "elapsed_ms": int((time.monotonic() - self.started_monotonic) * 1000),
            }
            # Shape parity with /state: the client reads frame.result /
            # frame.error directly, so a terminal frame that only carried
            # `detail` would be silently dropped by both of its code paths.
            if error:
                event["error"] = error
            else:
                event["result"] = result or {}
            self.events.append(event)
            self.cond.notify_all()

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)

    def state(self) -> dict[str, Any]:
        stages = STAGES_MODEL if self.mode == "model" else STAGES_LOCAL
        done_phases = [e["phase"] for e in self.events]
        current = done_phases[-1] if done_phases else stages[0]
        index = stages.index(current) if current in stages else 0
        return {
            "run_id": self.id,
            "status": self.status,
            "mode": self.mode,
            "phase": current,
            "phase_index": index,
            "phase_total": len(stages),
            "elapsed_ms": self.elapsed_ms,
            "started_at": self.started_at,
            "has_raw": self.has_raw,
            "result": self.result,
            "error": self.error,
        }


_RUNS: dict[str, Run] = {}
_JOB_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# the worker
# --------------------------------------------------------------------------


def check_image_limits(blob: bytes) -> None:
    """Enforce the pixel budget for EVERY container, not just the four we sniff.

    sniff_dimensions() is a fast pre-decode guard, but it only understands PNG,
    JPEG, GIF and WebP -- a TIFF or BMP bomb passes it untouched. This falls
    back to Pillow's lazy header read for anything else, which reads the
    dimensions without decoding the pixels.
    """
    dimensions = sniff_dimensions(blob)
    if dimensions is None:
        try:
            with Image.open(io.BytesIO(blob)) as probe:
                dimensions = probe.size
        except Image.DecompressionBombError as exc:
            raise LimitsError(f"Image is too large to decode safely: {exc}") from exc
        except Exception:
            return  # not an image at all; prepare_image() reports that properly
    width, height = dimensions
    if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_IMAGE_PIXELS:
        raise LimitsError(
            f"Image is {width}x{height} ({width * height:,} pixels); the limit is "
            f"{MAX_IMAGE_PIXELS:,} pixels ({MAX_DIMENSION} per side)"
        )


def safe_host(url: str) -> str:
    """Host and port only. A gateway URL may carry user:pass@ userinfo, and
    that is a credential the browser must never receive."""
    without_scheme = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", url or "")
    return without_scheme.split("@")[-1].split("/")[0].split("?")[0]


def preview_scale_for(n: int) -> int:
    """ONE authority for the preview zoom. 8->32, 16->32, 32->16, 64->8.

    64->8 matches the CLI's PIXEL_SCALE=8 and the already-approved
    test/20260921-171120.pixel_x8.png, so the approved look does not change.
    """
    return max(1, min(32, round(512 / n)))


def build_web_config(size: int, palette_spec: Any, max_colors: int, pixelize_only: bool):
    """Build a real pixel_redraw.Config for the web request.

    Everything goes through the core's own build_config(), reached by
    fabricating the argparse.Namespace its CLI would have produced.  That is
    deliberate: build_config substitutes {width}/{height} into the prompt from
    args.size, so routing size through it keeps the prompt and the pixelizer in
    agreement.  Building a config from env and then reassigning .size would ask
    the model to redraw for 64x64 while the pixelizer produced 8x8, destroying
    exactly the detail the model bothered to preserve.
    """
    if size not in SIZES:
        raise pr.SizeError(f"Unsupported size {size!r}; choose 8, 16, 32 or 64.")

    # main() does these bounds checks, not build_config -- verified: build_config
    # accepts scale=64 and max_colors=1 when the env says so. The web layer has
    # to replicate them rather than assume.
    scale = preview_scale_for(size)
    if not 1 <= scale <= 32:
        raise pr.ConfigError(f"Preview scale {scale} is outside 1..32")
    if not 2 <= max_colors <= 256:
        raise pr.SizeError(f"Max colors must be between 2 and 256, got {max_colors}")

    args = argparse.Namespace(
        size=f"{size}x{size}",
        scale=scale,
        max_colors=max_colors,
        palette=None,      # resolved below, via pixel_palettes
        prompt=None,       # None means PIXEL_PROMPT from .env still wins
        keep_raw=False,    # keep_raw OR env, so this cannot force it off
        pixelize_only=pixelize_only,
    )
    config = pr.build_config(args)

    _, colors = pixel_palettes.resolve(palette_spec)
    return dataclasses.replace(config, palette=colors)


def assert_palette_subset(image: Any, palette) -> None:
    """Assert requirement 1 positively rather than trusting it.

    nearest_palette() copies a palette member verbatim, so membership holds by
    construction -- but "only ever" is the requirement, so a regression in the
    downscale or the quantizer must be REPORTED instead of shipped.  Note that
    transparent (alpha=0) pixels still carry a palette RGB value, so a future
    post-processing quantizer is the one way this can break.
    """
    if not palette:
        return
    allowed = set(palette)
    present = {rgb for _, rgb in (image.convert("RGB").getcolors(maxcolors=1 << 20) or [])}
    offending = present - allowed
    if offending:
        hexes = ", ".join("#%02x%02x%02x" % rgb for rgb in sorted(offending)[:8])
        raise PaletteViolationError(
            f"Palette subset assertion failed: {len(offending)} colour(s) outside the "
            f"palette reached the output ({hexes}). This is a regression in the "
            f"downscale or quantize step, not a user error."
        )


def palette_report(image: Any) -> list[dict[str, Any]]:
    """(hex, count) pairs, most used first.

    Computed here because save_outputs' report['palette'] is a bare hex list --
    it unpacks getcolors()'s (count, rgb) pairs and throws the count away.
    """
    pairs = image.convert("RGB").getcolors(maxcolors=1 << 20) or []
    return [
        {"hex": "#%02x%02x%02x" % rgb, "count": count}
        for count, rgb in sorted(pairs, reverse=True)
    ]


def repro_command(run: Run) -> str:
    """The exact CLI line that reproduces this run's failure.

    The input file is deliberately left in the run directory on failure, so
    this closes the loop on requirement 6: the user can paste it into a shell
    and get the identical error without the web layer in the way.
    """
    if not run.config:
        return ""
    suffix = ".png"
    if run.artifacts:  # the worker records the real name once it has written it
        for candidate in run.dir.glob(f"{run.id}.*"):
            if candidate.suffix not in (".png", ".json") or candidate.name.endswith(".ai.png"):
                continue
            if candidate.name in (f"{run.id}.pixel.png", f"{run.id}.report.json"):
                continue
            if not candidate.name.startswith(f"{run.id}.pixel_x"):
                suffix = candidate.suffix
                break
    bits = [f"python3 pixel_redraw.py runs/{run.id}/{run.id}{suffix}", "--pixelize-only"]
    bits.append(f"--size {run.config.size[0]}x{run.config.size[1]}")
    if run.config.palette:
        hexes = ",".join("#%02x%02x%02x" % c for c in run.config.palette)
        bits.append(f"--palette {hexes!r}")
    else:
        bits.append(f"--max-colors {run.config.max_colors}")
    return " ".join(bits)


def work(run: Run, upload: bytes, mime_type: str, filename: str, size: int,
         palette_spec: Any, max_colors: int, pixelize_only: bool) -> None:
    """The background worker. Runs on its own thread; never touches a socket."""
    api_key = os.getenv("NEWAPI_API_KEY", "").strip()
    where = "work"
    try:
        run.dir.mkdir(parents=True, exist_ok=True)
        run.emit("received", "收到图片", {
            "bytes": len(upload), "mime_type": mime_type, "filename": filename,
        })

        where = "build_config"
        config = build_web_config(size, palette_spec, max_colors, pixelize_only)
        run.config = config
        run.raw_bytes = upload

        # prepare_image() keys off the file extension, so the suffix is derived
        # from the declared MIME to keep its pass-through branch alive for
        # png/jpeg/webp/heic. Anything else falls to the Pillow normalisation
        # path -- including octet-stream, which mimetypes does report for an
        # unknown suffix rather than returning None.
        suffix = {
            "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
            "image/heic": ".heic", "image/heif": ".heif", "image/gif": ".gif",
        }.get(mime_type, ".bin")
        input_path = run.dir / f"{run.id}{suffix}"
        input_path.write_bytes(upload)

        where = "prepare_image"
        encoded_mime = pr.prepare_image(input_path)[0]
        notes = []
        with Image.open(input_path) as probe:
            if getattr(probe, "is_animated", False):
                notes.append("animated GIF: first frame only")
        run.emit("encoding", "已编码", {"mime_type": encoded_mime, "notes": notes})

        if pixelize_only:
            raw = upload
        else:
            where = "call_newapi"
            run.emit("upstream_wait", "调用模型中", {
                "model": config.model,
                "timeout_s": config.timeout,
                "host": safe_host(config.base_url),
            })
            response = pr.call_newapi(input_path, config)
            run.emit("upstream_response", "模型已返回", {
                "bytes": len(json.dumps(response, ensure_ascii=False)),
            })

            # Emitted BEFORE extract_image, not after: extract_image can reach
            # download_image() for a URL-shaped response, and that carries its
            # own 60s per-URL timeout. Without this event the UI shows
            # unexplained silence for up to a minute.
            where = "extract_image"
            run.emit("extract_start", "解析响应中", {})
            run.artifacts["response.json"] = (
                redact(
                    json.dumps(
                        {"sanitized": sanitize_response(response),
                         "note": "strings over 200 chars are replaced with a placeholder; "
                                 "this is the full upstream response otherwise"},
                        ensure_ascii=False, indent=2,
                    ),
                    api_key,
                ).encode("utf-8"),
                "application/json",
            )
            raw = pr.extract_image(response)
            run.emit("extracted", "已取得图片", {"bytes": len(raw)})

        where = "pixelize"
        run.emit("pixelizing", "像素化中", {"size": size, "palette": palette_spec})
        image = pr.pixelize(raw, config)

        where = "verify"
        run.emit("verifying", "校验调色板中", {})
        assert_palette_subset(image, config.palette)

        where = "save_outputs"
        run.emit("saving", "写盘中", {})
        pr.save_outputs(input_path, raw, image, config, run.dir)
        run.has_raw = True

        run.artifacts["pixel.png"] = (_png_bytes(image), "image/png")
        preview = image.resize(
            (image.width * config.scale, image.height * config.scale),
            resample=Image.Resampling.NEAREST,
        )
        run.artifacts["preview.png"] = (_png_bytes(preview), "image/png")
        run.artifacts["raw.png"] = (raw, _sniff_content_type(raw))

        # Built from build_payload() a second time (~2ms) purely for debugging:
        # it answers "what did we actually ask for".
        where = "build_payload"
        payload = pr.build_payload(input_path, config)
        for part in payload.get("contents", [{}])[0].get("parts", []):
            inline = part.get("inline_data")
            if inline:
                inline["data"] = f"<base64 {len(inline['data'])} chars>"
        run.artifacts["request.json"] = (
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            "application/json",
        )

        result = {
            "run_id": run.id,
            "mode": run.mode,
            "size": [image.width, image.height],
            "scale": config.scale,
            "palette_requested": palette_spec,
            "palette_used": palette_report(image),
            "subset_ok": True,
            "color_count": len(palette_report(image)),
            "has_raw": True,
            "elapsed_s": round((time.monotonic() - run.started_monotonic), 3),
            "urls": {
                "pixel": f"/api/runs/{run.id}/pixel.png",
                "preview": f"/api/runs/{run.id}/preview.png",
                "raw": f"/api/runs/{run.id}/raw.png",
                "response": f"/api/runs/{run.id}/response.json",
                "request": f"/api/runs/{run.id}/request.json",
            },
            "repro": repro_command(run),
        }
        run.finish(result=result)
    except BaseException as exc:  # noqa: BLE001 - every failure must reach the browser
        sys.stderr.write(
            f"run {run.id} failed in {where}: {type(exc).__name__}: "
            f"{redact(str(exc), api_key)}\n"
        )
        _persist_failure_artifacts(run, exc, api_key)
        envelope = to_envelope(exc, where, api_key, run.id, phase=where)
        # The repro command is the escape hatch requirement 6 exists for, and it
        # is MOST needed on a failure. Without it here the frontend falls back to
        # reconstructing the command from fields a failed run never populated and
        # emits --palette 'auto', which reproduces a different run than the one
        # that failed.
        if run.config is not None:
            envelope["repro"] = repro_command(run)
        # Only advertise debug artifacts that actually exist. On an upstream
        # HTTP error -- the most common failure of all -- the parsed response
        # never existed, so the old unconditional link led the user to a second
        # error instead of to the evidence.
        urls = envelope.get("debug_urls")
        if urls:
            envelope["debug_urls"] = {
                name: url for name, url in urls.items()
                if name == "state" or f"{name}.json" in run.artifacts
            }
        run.finish(error=envelope)
    # The input file is deliberately LEFT in place on failure so the repro
    # command the UI offers actually works.


def _sniff_content_type(blob: bytes) -> str:
    """The raw bytes are the model's output verbatim, so the real type matters.

    save_outputs() writes them to a file named .ai.png regardless, which is a
    pre-existing quirk -- serving them as image/png would be a new lie.
    """
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:2] == b"\xff\xd8":
        return "image/jpeg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


def _persist_failure_artifacts(run: Run, exc: BaseException, api_key: str) -> None:
    """Write the debug artifacts for a FAILED run.

    These matter more on failure than on success: the raw upstream body is the
    only thing that explains a gateway rejection, and it is already in hand as
    the exception's .detail. Every step is guarded -- a failure while recording
    a failure must not replace the original error.
    """
    detail = getattr(exc, "detail", "")
    if detail and "response.json" not in run.artifacts:
        try:
            run.artifacts["response.json"] = (
                redact(json.dumps({
                    "upstream_body_verbatim": detail,
                    "http_status": getattr(exc, "status", None),
                    "note": "the upstream response body, captured from the exception "
                            "because the request failed before a response was parsed",
                }, ensure_ascii=False, indent=2), api_key).encode("utf-8"),
                "application/json",
            )
        except Exception:  # noqa: BLE001
            pass
    if "request.json" not in run.artifacts and run.config is not None:
        try:
            for candidate in run.dir.glob(f"{run.id}.*"):
                if candidate.name in (f"{run.id}.pixel.png", f"{run.id}.report.json") \
                        or candidate.name.startswith(f"{run.id}.pixel_x") \
                        or candidate.name.endswith(".ai.png"):
                    continue
                payload = pr.build_payload(candidate, run.config)
                for part in payload.get("contents", [{}])[0].get("parts", []):
                    inline = part.get("inline_data")
                    if inline:
                        inline["data"] = f"<base64 {len(inline['data'])} chars>"
                run.artifacts["request.json"] = (
                    json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
                    "application/json",
                )
                break
        except Exception:  # noqa: BLE001
            pass


def _png_bytes(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def start_run(upload: bytes, mime_type: str, filename: str, size: int,
              palette_spec: Any, max_colors: int, pixelize_only: bool) -> Run:
    """Claim the single job slot, then start the worker.

    The lock is acquired HERE, in the request handler, before the 202 is
    returned. Acquiring it inside the worker instead would let two rapid POSTs
    both receive a run id while the documented 409 stayed unreachable.
    """
    if not _JOB_LOCK.acquire(blocking=False):
        raise BusyError("已有任务在运行")
    try:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        run = Run(run_id, "local" if pixelize_only else "model")
        _RUNS[run_id] = run
        thread = threading.Thread(
            target=_worker_then_release,
            args=(run, upload, mime_type, filename, size, palette_spec, max_colors, pixelize_only),
            name=f"run-{run_id}",
            daemon=True,
        )
        thread.start()
        return run
    except BaseException:
        _JOB_LOCK.release()
        raise


def _worker_then_release(run: Run, upload: bytes, mime_type: str, filename: str, size: int,
                         palette_spec: Any, max_colors: int, pixelize_only: bool) -> None:
    try:
        work(run, upload, mime_type, filename, size, palette_spec, max_colors, pixelize_only)
    finally:
        _JOB_LOCK.release()


def repixelize(run: Run, size: int, palette_spec: Any, max_colors: int) -> dict[str, Any]:
    """Re-run ONLY pixelize() against the cached semantic-layer output.

    Requirements 1 and 2 together make size x palette a combinatorial search --
    36 combinations here -- and without this every click would cost a model
    call. This is NOT --pixelize-only: the model ran, and we are re-crushing
    its output.
    """
    source = run.dir / f"{run.id}.ai.png"
    if not source.is_file():
        raise pr.PixelError(f"Run {run.id} has no cached model output to re-pixelize")
    config = build_web_config(size, palette_spec, max_colors, pixelize_only=False)
    started = time.monotonic()
    image = pr.pixelize(source.read_bytes(), config)
    assert_palette_subset(image, config.palette)
    run.config = config
    run.artifacts["pixel.png"] = (_png_bytes(image), "image/png")
    preview = image.resize(
        (image.width * config.scale, image.height * config.scale),
        resample=Image.Resampling.NEAREST,
    )
    run.artifacts["preview.png"] = (_png_bytes(preview), "image/png")
    used = palette_report(image)
    result = {
        "run_id": run.id,
        "mode": run.mode,
        "size": [image.width, image.height],
        "scale": config.scale,
        "palette_requested": palette_spec,
        "palette_used": used,
        "subset_ok": True,
        "color_count": len(used),
        "has_raw": run.has_raw,
        "elapsed_s": round(time.monotonic() - started, 3),
        "urls": {
            "pixel": f"/api/runs/{run.id}/pixel.png",
            "preview": f"/api/runs/{run.id}/preview.png",
            "raw": f"/api/runs/{run.id}/raw.png",
        },
        "repro": repro_command(run),
    }
    run.result = result
    return result


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"pixel-web/{VERSION}"
    # Bounds how long a client may hold a thread without completing a request.
    # stream_sse() raises this for the duration of a stream, which legitimately
    # idles for as long as the model takes.
    timeout = 30

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        # Method / path / status / byte count only. NEVER a body, never a
        # header value, never a Config -- the key travels in a header.
        with _LOG_LOCK:
            sys.stderr.write(
                f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} "
                f"{redact(fmt % args, self.api_key)}\n"
            )

    def log_error(self, fmt: str, *args: Any) -> None:
        self.log_message(fmt, *args)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    @property
    def api_key(self) -> str:
        return os.getenv("NEWAPI_API_KEY", "").strip()

    def host_ok(self) -> bool:
        """Unconditional Host allowlist -- this is what closes DNS rebinding.

        A page on evil.example can resolve its own hostname to 127.0.0.1 and
        then talk to this server with credentials the browser attaches. Pinning
        the Host header stops that, and it costs nothing for a loopback tool.
        """
        host = (self.headers.get("Host") or "").strip()
        port = getattr(self.server, "server_port", 0)
        allowed = {
            f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}",
            "127.0.0.1", "localhost", "[::1]",
        }
        return host in allowed

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, payload: Any) -> None:
        self.send_json(status, payload)

    def send_bytes(self, status: int, payload: bytes, content_type: str,
                   download_name: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(payload)

    def read_json_body(self) -> dict[str, Any]:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise RequestError(
                f"Content-Type must be application/json, got {content_type or '(none)'}"
            )
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REQUEST_BYTES:
            raise LimitsError(
                f"请求体 {length:,} 字节超过上限 {MAX_REQUEST_BYTES:,} 字节"
                f"（图片上限 {MAX_UPLOAD_BYTES:,} 字节，base64 后约放大 1.33 倍）。"
            )
        raw = self.rfile.read(length) if length else b"{}"
        self._body_consumed = True
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RequestError("请求体必须是一个 JSON 对象")
        return parsed

    def guard(self) -> bool:
        if not self.host_ok():
            self.fail(ForbiddenError(
                f"Host {self.headers.get('Host')!r} is not allowed. This server binds "
                "127.0.0.1 and accepts only localhost Host headers."
            ), "guard")
            return False
        return True

    # -- routing -----------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        # No CORS headers, ever. Because POSTs require
        # Content-Type: application/json, a cross-site request must preflight,
        # and the preflight gets nothing -- so cross-site writes stop at the door.
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self.guard():
            return
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                return self.serve_index()
            if path == "/api/meta":
                return self.send_json(200, self.meta())
            if path == "/api/health":
                return self.send_json(200, {
                    "ok": True, "version": VERSION,
                    "upstream_configured": self.upstream_configured(),
                })
            match = re.fullmatch(r"/api/runs/([A-Za-z0-9\-]+)/(\w+\.png|\w+\.json|events|state)", path)
            if match:
                return self.serve_run(match.group(1), match.group(2))
            raise NotFoundError(f"No route for GET {path}")
        except BaseException as exc:  # noqa: BLE001
            self.fail(exc, "do_GET")

    def do_POST(self) -> None:  # noqa: N802
        self._body_consumed = False
        if not self.guard():
            return
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/generate":
                return self.api_generate()
            match = re.fullmatch(r"/api/runs/([A-Za-z0-9\-]+)/(repixelize|cancel)", path)
            if match:
                return self.api_run_action(match.group(1), match.group(2))
            raise NotFoundError(f"No route for POST {path}")
        except BaseException as exc:  # noqa: BLE001
            self.fail(exc, "do_POST")

    def fail(self, exc: BaseException, where: str, run_id: str | None = None) -> None:
        # If we are rejecting a request whose body we never drained, the unread
        # bytes would be parsed as the next request on a keep-alive connection.
        # Closing is the only correct answer.
        if not getattr(self, "_body_consumed", True) and self.command in ("POST", "PUT", "PATCH"):
            self.close_connection = True
        status = KIND_STATUS.get(classify(exc, where), 500)
        envelope = to_envelope(exc, where, self.api_key, run_id,
                               debug=os.getenv("PIXEL_WEB_DEBUG", "1") != "0")
        try:
            self.send_json(status, {"error": envelope})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # -- endpoints ---------------------------------------------------------
    def upstream_configured(self) -> bool:
        return all(os.getenv(name, "").strip() for name in
                   ("NEWAPI_BASE_URL", "NEWAPI_API_KEY", "NEWAPI_MODEL"))

    def meta(self) -> dict[str, Any]:
        """Everything the page needs to boot. Never carries the key or the URL.

        The base URL is reduced to scheme+host+port because a gateway URL may
        carry userinfo; upstream_configured answers the only question the UI
        actually has.
        """
        default_size = 64
        try:
            configured = pr.parse_size(os.getenv("PIXEL_SIZE", "64x64"))
            if configured[0] == configured[1] and configured[0] in SIZES:
                default_size = configured[0]
        except ValueError:
            pass
        host = safe_host(os.getenv("NEWAPI_BASE_URL", ""))
        return {
            "sizes": list(SIZES),
            "default_size": default_size,
            "preview_scales": {str(n): preview_scale_for(n) for n in SIZES},
            "presets": pixel_palettes.as_meta(),
            "default_preset": pixel_palettes.DEFAULT_PRESET,
            "max_palette_colors": pixel_palettes.MAX_PALETTE_COLORS,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "max_image_pixels": MAX_IMAGE_PIXELS,
            "max_dimension": MAX_DIMENSION,
            "model": os.getenv("NEWAPI_MODEL", "").strip(),
            "api_version": os.getenv("NEWAPI_API_VERSION", "v1beta").strip(),
            "upstream_configured": self.upstream_configured(),
            "upstream_host": host,
            "timeout_seconds": float(os.getenv("NEWAPI_TIMEOUT", "180")),
            "keep_raw": pr.env_bool("PIXEL_KEEP_RAW", True),
            "pixelize_only_available": True,
            "version": VERSION,
        }

    def serve_index(self) -> None:
        index = ROOT / "static" / "index.html"
        if not index.is_file():
            return self.send_json(500, {"error": {
                "kind": "internal",
                "message": f"static/index.html is missing at {index}",
                "hint": "前端文件没找到。确认 static/index.html 存在。",
            }})
        self.send_bytes(200, index.read_bytes(), "text/html; charset=utf-8")

    def api_generate(self) -> None:
        body = self.read_json_body()
        image_field = body.get("image")
        if not isinstance(image_field, str) or not image_field:
            raise RequestError("请求缺少 'image' 字段（data URI 或裸 base64）")
        upload = decode_image_field(image_field)

        mime_type = str(body.get("mime_type") or "image/png")
        filename = str(body.get("filename") or "upload")

        if len(upload) > MAX_UPLOAD_BYTES:
            raise LimitsError(
                f"图片 {len(upload):,} 字节超过上限 {MAX_UPLOAD_BYTES:,} 字节"
            )
        check_image_limits(upload)

        size = body.get("size")
        try:
            size = int(size)
        except (TypeError, ValueError):
            raise pr.SizeError(f"Unsupported size {size!r}; choose 8, 16, 32 or 64.") from None

        max_colors = body.get("max_colors", 16)
        try:
            max_colors = int(max_colors)
        except (TypeError, ValueError):
            raise pr.SizeError(f"max_colors must be an integer, got {max_colors!r}") from None

        # Validate BEFORE claiming the job slot, so a typo answers with a 400
        # the browser can show immediately rather than a 202 followed by an
        # asynchronous failure. The worker re-validates through
        # build_web_config(); this is a fast path, not a second rulebook --
        # both call the same function, so they cannot disagree.
        pixelize_only = bool(body.get("pixelize_only"))
        build_web_config(size, body.get("palette"), max_colors, pixelize_only)
        if not pixelize_only and not self.upstream_configured():
            raise pr.ConfigError(
                "Missing .env values: NEWAPI_BASE_URL, NEWAPI_API_KEY or NEWAPI_MODEL. "
                "Copy .env.example to .env and fill them in."
            )

        run = start_run(
            upload, mime_type, filename, size,
            body.get("palette"), max_colors, pixelize_only,
        )
        self.send_json(202, {
            "run_id": run.id,
            "events_url": f"/api/runs/{run.id}/events",
            "state_url": f"/api/runs/{run.id}/state",
        })

    def api_run_action(self, run_id: str, action: str) -> None:
        run = _RUNS.get(run_id)
        if run is None:
            raise NotFoundError(f"Unknown run {run_id!r}")
        if action == "cancel":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, MAX_REQUEST_BYTES))
            self._body_consumed = True
            # Honest: urllib gives no hook to abort a blocking read, so this
            # only stops the UI from watching. Reporting it as a real
            # cancellation would imply money was saved when it was not.
            return self.send_json(202, {
                "cancelled": True,
                "note": "the in-flight upstream request cannot be interrupted and may still be billed",
            })
        body = self.read_json_body()
        size = int(body.get("size", run.config.size[0] if run.config else 64))
        max_colors = int(body.get("max_colors", 16))
        try:
            result = repixelize(run, size, body.get("palette"), max_colors)
        except BaseException as exc:  # noqa: BLE001
            return self.fail(exc, "repixelize", run_id)
        self.send_json(200, result)

    def serve_run(self, run_id: str, what: str) -> None:
        run = _RUNS.get(run_id)
        if run is None:
            raise NotFoundError(f"Unknown run {run_id!r}")
        if what == "state":
            return self.send_json(200, run.state())
        if what == "events":
            return self.stream_sse(run)
        artifact = run.artifacts.get(what)
        if artifact is None:
            raise pr.PixelError(f"Run {run_id} has no artifact {what!r} yet")
        payload, content_type = artifact
        name = None
        if what == "pixel.png" and run.config:
            name = f"{run_id}_{run.config.size[0]}x{run.config.size[1]}.png"
        elif what == "preview.png" and run.config:
            name = f"{run_id}_x{run.config.scale}.png"
        elif what == "raw.png":
            name = f"{run_id}_ai.png"
        self.send_bytes(200, payload, content_type, name)

    # -- SSE ---------------------------------------------------------------
    def stream_sse(self, run: Run) -> None:
        """Stream events with hand-written chunked framing.

        Why not WebSocket: the stdlib has no server implementation and
        bidirectional traffic is useless here. Why not polling: 500ms polling
        for a minute is 120 requests and delays every stage boundary by up to
        500ms.

        Threading: this handler thread ONLY reads the run's event list and
        writes to its own socket. The worker appends under the same Condition
        and never writes here. That separation is load-bearing -- if the model
        call ran on this thread, every event would arrive in one batch at the
        end, i.e. a progress bar frozen for the whole timeout.
        """
        try:
            last_id = int(self.headers.get("Last-Event-ID") or -1)
        except ValueError:
            last_id = -1

        self.connection.settimeout(max(300.0, getattr(self.server, "upstream_timeout", 180.0) * 2))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        cursor = 0
        last_write = time.monotonic()
        try:
            while True:
                with run.cond:
                    pending = run.events[cursor:]
                    terminal = run.status != "running"
                    if not pending and not terminal:
                        run.cond.wait(timeout=1.0)
                        pending = run.events[cursor:]
                        terminal = run.status != "running"
                for event in pending:
                    cursor += 1
                    if event["seq"] <= last_id:
                        continue
                    self.write_chunk(
                        f"id: {event['seq']}\nevent: {event['phase']}\n"
                        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    )
                    last_write = time.monotonic()
                if terminal and cursor >= len(run.events):
                    # Terminating chunk. Without it a chunked reader blocks
                    # until its own timeout, so the browser would sit on a
                    # finished run forever.
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return
                if time.monotonic() - last_write > 10:
                    # A comment frame: keeps intermediaries awake and lets the
                    # client distinguish "alive but silent" from "disconnected".
                    self.write_chunk(": ping\n\n")
                    last_write = time.monotonic()
        except (BrokenPipeError, ConnectionResetError):
            # Detected lazily on the next write, not at the moment of
            # disconnect. The worker is unaffected: it holds no socket, so the
            # run finishes and its artifacts stay retrievable via /state.
            self.close_connection = True

    def write_chunk(self, text: str) -> None:
        payload = text.encode("utf-8")
        self.wfile.write(f"{len(payload):X}\r\n".encode("ascii") + payload + b"\r\n")
        # stdlib's StreamRequestHandler is unbuffered so this is not strictly
        # needed today, but it is required the instant this moves behind any
        # proxy layer, and it documents the intent.
        self.wfile.flush()


def decode_image_field(value: str) -> bytes:
    """Accept a data URI or bare base64 (standard or URL-safe alphabet)."""
    if value.startswith("data:"):
        _, _, value = value.partition(",")
    cleaned = re.sub(r"\s+", "", value)
    try:
        return base64.b64decode(cleaned, validate=False)
    except (ValueError, base64.binascii.Error) as exc:
        raise RequestError(f"'image' is not valid base64: {exc}") from exc


# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Localhost web UI for pixel-redraw")
    parser.add_argument("--port", type=int, default=None, help="Port; defaults to PIXEL_WEB_PORT or 8770")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; loopback only")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # load_dotenv's default is CWD-relative, and the server must work when
    # launched from anywhere, so the path is pinned to this file's directory.
    pr.load_dotenv(ROOT / ".env")
    args = parse_args(argv)

    port = args.port or int(os.getenv("PIXEL_WEB_PORT", "8770"))
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write(
            f"Refusing to bind {args.host}: this process holds the API key and must "
            "stay on loopback. (A non-loopback address would also break the browser's "
            "clipboard API, which needs a secure context.)\n"
        )
        return 2

    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    # macOS ships sharingd on 8770, so the default can collide on a stock Mac.
    # Failing hard would leave the user to discover --port; silently moving would
    # break a bookmark. So: walk forward a few ports, and say loudly which one
    # was taken and why -- the URL is printed either way, and that is where the
    # user reads it from.
    server = None
    for candidate in range(port, port + 10):
        try:
            server = ThreadingHTTPServer((args.host, candidate), Handler)
        except OSError as exc:
            if exc.errno != 48:
                raise
            continue
        if candidate != port:
            print(f"port {port} was already in use; using {candidate} instead", flush=True)
        port = candidate
        break
    if server is None:
        sys.stderr.write(
            f"Ports {port}-{port + 9} are all in use. Pick one with --port N or "
            f"PIXEL_WEB_PORT.\n"
        )
        return 2

    url = f"http://127.0.0.1:{port}/"
    configured = all(os.getenv(n, "").strip() for n in
                     ("NEWAPI_BASE_URL", "NEWAPI_API_KEY", "NEWAPI_MODEL"))
    server.upstream_timeout = float(os.getenv("NEWAPI_TIMEOUT", "180"))
    # flush=True on every line: stdout is block-buffered when redirected, and
    # reading this URL off the terminal IS the tool's launch workflow, so a
    # banner that appears only on exit is useless.
    print(f"pixel-web {VERSION}")
    print(f"  {url}")
    print(f"  model: {os.getenv('NEWAPI_MODEL', '(unset)')}   upstream: "
          f"{'configured' if configured else 'NOT configured — set .env'}")
    print(f"  runs: {RUNS_DIR}")
    print("  Ctrl+C to stop. The key and model come from .env only.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
