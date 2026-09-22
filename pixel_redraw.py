#!/usr/bin/env python3
"""pixel-redraw core: the Gemini generateContent protocol and the local
Pillow pass that turns a model's output into true pixel art.

This module is pure compute.  It reads no environment variables, opens no
files, and owns no socket, because it runs unchanged in two places: on CPython
for the unit tests, and inside Pyodide (WASM CPython) in the browser, where
there is no environment to read and no synchronous networking at all.

The transport is therefore injected: `generate()` takes an async
``call_upstream(payload) -> response`` callback and never knows whether the
bytes travelled over urllib, pyfetch, or a test double.  `pixel_pipeline.py`
supplies the browser transport.

Authentication is the x-goog-api-key header sent to ``Config.base_url``, which
defaults to the official endpoint, so an AI Studio key needs no endpoint
configured at all and any gateway speaking the same protocol works by pointing
``base_url`` at it.
"""

from __future__ import annotations

import base64
import io
import json
import re
import struct
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Iterable


DEFAULT_PROMPT = """Redraw the provided image as clean, deliberate pixel art.
Preserve the subject, silhouette, pose, composition, and important colors.
The target logical canvas is {width}x{height} pixels, derived from the source
image's aspect ratio and a 256x256 reference canvas.
Use hard pixel-aligned edges, a limited palette, crisp clusters, and no
anti-aliasing, blur, photographic texture, smooth gradients, text, or watermark.
Return one edited image only; do not return an explanation or a code block."""

REFINE_PROMPT = """Repaint the first image as finished, hand-placed pixel art.
The first image is a locally pixelized draft already reduced to the requested
logical grid and palette, then enlarged with nearest-neighbour sampling. Treat
its block structure as the target pixel design; do not add high-resolution
detail between those blocks. The second image is the original subject
reference, not a texture to trace.
Keep the draft's subject, pose, framing and recognizable features, but redesign
its pixel shapes at a {width}x{height} logical grid. Make the outer silhouette
read cleanly; use deliberate, rhythmic stair steps on curves and consistent
outline weight. Join fragmented pixels into confident clusters of light and
shadow. Simplify small hair strands and clothing folds while keeping the eyes,
face and other focal details readable. Remove stray pixels, uneven edge noise,
blur, anti-aliasing, soft gradients and high-resolution linework. Do not merely
sharpen, resize or recolor the draft: redraw its shapes. Return one final image
only, with no explanation or comparison sheet."""

REFERENCE_CANVAS = 256

DATA_URI_RE = re.compile(r"data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)")
URL_RE = re.compile(r"https?://[^\s)\]}>\"']+")


DEFAULT_API_VERSION = "v1beta"

# The official Gemini Developer API endpoint, and the same one the google-genai
# SDK targets when nothing overrides it.  Deliberately a bare origin:
# api_url() appends /{version}/models/{model}:generateContent, so a default
# carrying /v1beta would build /v1beta/v1beta/...
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

# A wall-clock budget, not a per-socket timeout: the browser transport wraps the
# whole request in asyncio.wait_for, so reaching this really does stop waiting.
DEFAULT_TIMEOUT = 180.0

# Formats Gemini accepts for inline image data. Anything else (GIF, BMP, TIFF,
# ...) has to be re-encoded before it can be sent.
GEMINI_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/heic",
    "image/heif",
}

# Extension -> MIME, for the browser path where the caller holds bytes and a
# name but has no mimetypes module available.
_MIME_BY_EXTENSION = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
}


# --------------------------------------------------------------------------
# limits and choices
# --------------------------------------------------------------------------
#
# These moved here from the deleted web layer.  They used to protect a server's
# memory; they now protect the browser's WASM heap, and the values are
# unchanged, so the same picture is accepted or refused as before.

DEFAULT_SIZE = "32x32"
DEFAULT_SCALE = 8
DEFAULT_MAX_COLORS = 16

SIZES = (8, 16, 32, 64, 128)
COLOR_CHOICES = (8, 12, 16, 24, 32, 48, 64)
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_DIMENSION = 4096

# Our own guard must fire before Pillow's, so the user gets a message naming the
# real numbers instead of a DecompressionBombError.  Pillow only *warns* at its
# default 89,478,485 and does not raise until 2x that, by which point a
# 144-megapixel image has already allocated ~585MB.
PILLOW_PIXEL_CEILING = MAX_IMAGE_PIXELS * 2

# Stages, in order, per run mode.  A real statement boundary sits between each
# pair; nothing is emitted on a timer.
STAGES_MODEL = (
    "received", "encoding", "upstream_wait", "upstream_response",
    "extract_start", "extracted", "refine_wait", "refine_response", "refined",
    "pixelizing", "verifying", "saving", "done",
)
STAGES_LOCAL = ("received", "encoding", "pixelizing", "verifying", "saving", "done")


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------
#
# ``kind`` is the error contract the frontend keys off.  It classifies a
# failure structurally, never by matching message wording: several of these
# raise sites have ``__cause__ is None``, so a classifier that inspected the
# cause could not tell a relay quota error apart from an internal bug.


class PixelError(RuntimeError):
    """Base class for every failure this module raises deliberately."""

    kind = "internal"


class ConfigError(PixelError, ValueError):
    kind = "config"


class PaletteError(PixelError, ValueError):
    kind = "palette"


class SizeError(PixelError, ValueError):
    kind = "size"


class LimitsError(PixelError):
    kind = "limits"


class PaletteViolationError(PixelError):
    kind = "palette_violation"


class InputImageError(PixelError):
    kind = "input_image"


class UpstreamHTTPError(PixelError):
    kind = "upstream_http"

    def __init__(self, message: str, status: int | None = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail  # the FULL upstream body, not the 1200-char slice


class UpstreamTransportError(PixelError):
    kind = "upstream_transport"


class UpstreamTimeoutError(PixelError):
    kind = "upstream_timeout"


class UpstreamNonJSONError(PixelError):
    kind = "upstream_non_json"

    def __init__(self, message: str, status: int | None = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


class UpstreamProtocolError(PixelError):
    kind = "upstream_protocol"


class UpstreamErrorField(PixelError):
    kind = "upstream_error_field"


class NoImageExtracted(PixelError):
    kind = "no_image"


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    model: str
    api_version: str
    timeout: float
    size: tuple[int, int]
    scale: int
    max_colors: int
    palette: tuple[tuple[int, int, int], ...] | None
    prompt: str
    keep_raw: bool
    response_modalities: tuple[str, ...]
    image_size: str
    # ``size`` is the actual output size after the source dimensions are known.
    # ``base_size`` is the selected density on the reference canvas. Keeping both
    # prevents a 1024px source from being mistaken for a literal 64x64 output.
    base_size: tuple[int, int] | None = None
    source_size: tuple[int, int] | None = None
    prompt_template: str = ""
    refine_prompt: str = ""
    refine_prompt_template: str = ""
    passes: int = 2


# --------------------------------------------------------------------------
# configuration (explicit values, no environment)
# --------------------------------------------------------------------------


def make_config(
    *,
    model: str,
    api_key: str = "",
    base_url: str = DEFAULT_BASE_URL,
    api_version: str = DEFAULT_API_VERSION,
    timeout: float = DEFAULT_TIMEOUT,
    size: str = DEFAULT_SIZE,
    scale: int = DEFAULT_SCALE,
    max_colors: int = DEFAULT_MAX_COLORS,
    palette: str | tuple[tuple[int, int, int], ...] | None = None,
    prompt: str = "",
    refine_prompt: str = "",
    keep_raw: bool = True,
    response_modalities: tuple[str, ...] | None = None,
    image_size: str = "",
    passes: int = 2,
) -> Config:
    """Build a Config from explicit values.

    Every input the old build_config() read from the environment is now a
    keyword here, which is what lets one browser hold several users' settings
    without them colliding.

    Bounds are checked here rather than by the caller.  The deleted web layer
    had to replicate main()'s scale/max_colors checks because build_config
    accepted whatever the environment said; folding them in means there is now
    exactly one authority and no caller can forget.

    ``palette`` is either an explicit sequence of RGB triples, a hex string
    ("#RRGGBB,#RRGGBB"), or a falsy value for automatic quantisation.  Preset
    resolution lives in pixel_palettes, which imports this module, so taking a
    palette *spec* here would be a circular dependency.
    """
    if isinstance(palette, str):
        colors = parse_palette(palette)
    elif palette:
        colors = tuple(tuple(int(channel) for channel in color) for color in palette)
        if not colors:
            raise PaletteError("Palette must contain at least one color")
        if len(colors) > 256:
            raise PaletteError(f"Palette may hold at most 256 colors, got {len(colors)}")
    else:
        colors = None

    parsed_size = parse_size(size)

    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Timeout must be a number, got {timeout!r}") from exc
    if timeout_value <= 0:
        raise ConfigError("Timeout must be positive")

    if not isinstance(passes, int) or isinstance(passes, bool) or passes not in (1, 2):
        raise ConfigError("passes must be 1 or 2")
    if not 1 <= int(scale) <= 32:
        raise ConfigError(f"Preview scale must be between 1 and 32, got {scale}")
    if not 2 <= int(max_colors) <= 256:
        raise SizeError(f"Max colors must be between 2 and 256, got {max_colors}")

    prompt_template = prompt or DEFAULT_PROMPT
    refine_prompt_template = refine_prompt or REFINE_PROMPT
    modalities = ("TEXT", "IMAGE") if response_modalities is None else tuple(response_modalities)

    return Config(
        base_url=(base_url or DEFAULT_BASE_URL).strip(),
        api_key=api_key.strip(),
        model=model.strip(),
        api_version=(api_version or DEFAULT_API_VERSION).strip(),
        timeout=timeout_value,
        size=parsed_size,
        scale=int(scale),
        max_colors=int(max_colors),
        palette=colors,
        prompt=_fill_prompt(prompt_template, parsed_size),
        keep_raw=bool(keep_raw),
        response_modalities=modalities,
        image_size=(image_size or "").strip(),
        base_size=parsed_size,
        prompt_template=prompt_template,
        refine_prompt_template=refine_prompt_template,
        passes=passes,
    )


def validate_upstream(config: Config) -> None:
    """Whether a model call can be attempted at all.

    Kept separate from make_config() so the local-pixelize-only path needs no
    credentials -- that path is how the pipeline is smoke-tested without
    spending an API call, so it must not demand a key.
    """
    missing = [
        name
        for name, value in (("model", config.model), ("api key", config.api_key))
        if not value
    ]
    if missing:
        raise ConfigError("Missing " + " and ".join(missing) + ". Fill both in on the page.")


def _fill_prompt(template: str, size: tuple[int, int]) -> str:
    return template.replace("{width}", str(size[0])).replace("{height}", str(size[1]))


def parse_size(value: Any) -> tuple[int, int]:
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise SizeError(f"Invalid size {value!r}; use N or WIDTHxHEIGHT, e.g. 64x64")
        width, height = int(value[0]), int(value[1])
    else:
        match = re.fullmatch(r"\s*(\d+)(?:[xX](\d+))?\s*", str(value))
        if not match:
            raise SizeError(f"Invalid size {value!r}; use N or WIDTHxHEIGHT, e.g. 64x64")
        width = int(match.group(1))
        height = int(match.group(2) or match.group(1))
    if not (1 <= width <= 1024 and 1 <= height <= 1024):
        raise SizeError("Pixel canvas dimensions must be between 1 and 1024")
    return width, height


def proportional_size(source_size: tuple[int, int], base_size: tuple[int, int],
                      reference: int = REFERENCE_CANVAS) -> tuple[int, int]:
    """Map a source image onto a density measured on a reference canvas.

    A 256x256 source at density 64 becomes 64x64. A 1024x1024 source at the
    same density becomes 256x256. Width and height are scaled independently by
    the same reference, so non-square images keep their aspect ratio.
    """
    if reference < 1:
        raise SizeError("Reference canvas must be positive")
    source_width, source_height = source_size
    base_width, base_height = base_size
    if source_width < 1 or source_height < 1:
        raise SizeError("Source image dimensions must be positive")
    return (
        max(1, int(round(source_width * base_width / reference))),
        max(1, int(round(source_height * base_height / reference))),
    )


def configure_for_source(config: Config, source_size: tuple[int, int]) -> Config:
    """Resolve the actual output grid from the selected density and source size."""
    base_size = config.base_size or config.size
    output_size = proportional_size(source_size, base_size)
    template = config.prompt_template or config.prompt
    prompt = _fill_prompt(template, output_size)
    refine_template = config.refine_prompt_template or config.refine_prompt or REFINE_PROMPT
    refine_prompt = _fill_prompt(refine_template, output_size)
    return replace(
        config,
        size=output_size,
        base_size=base_size,
        source_size=source_size,
        prompt=prompt,
        refine_prompt=refine_prompt,
    )


def parse_palette(value: str) -> tuple[tuple[int, int, int], ...] | None:
    value = value.strip()
    if not value or value.lower() in {"auto", "none"}:
        return None

    colors: list[tuple[int, int, int]] = []
    for item in re.split(r"[;,\s]+", value):
        item = item.strip()
        if not item:
            continue
        if item.startswith("#"):
            item = item[1:]
        if len(item) == 3:
            item = "".join(char * 2 for char in item)
        if not re.fullmatch(r"[0-9a-fA-F]{6}", item):
            raise PaletteError(f"Invalid palette color {item!r}; use #RRGGBB values")
        colors.append(tuple(int(item[offset : offset + 2], 16) for offset in (0, 2, 4)))

    if not colors:
        raise PaletteError("Palette must contain at least one color")
    return tuple(colors)


# --------------------------------------------------------------------------
# Pillow and the request URL
# --------------------------------------------------------------------------


def require_pillow() -> tuple[Any, Any, Any]:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - the tests import Pillow
        raise RuntimeError("Pillow is required. Run: python3 -m pip install -r requirements.txt") from exc
    Image.MAX_IMAGE_PIXELS = PILLOW_PIXEL_CEILING
    return Image, ImageOps, None


def api_url(base_url: str, api_version: str, model: str) -> str:
    """Build the Gemini native generateContent URL: BASE/VERSION/models/MODEL:generateContent."""
    return (
        f"{base_url.rstrip('/')}/{api_version.strip('/')}"
        f"/models/{model}:generateContent"
    )


def safe_host(url: str) -> str:
    """Host and port only. A base URL may carry user:pass@ userinfo, and that is a
    credential the browser must never receive."""
    without_scheme = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", url or "")
    return without_scheme.split("@")[-1].split("/")[0].split("?")[0]


def guess_image_mime(filename: str, fallback: str = "") -> str:
    """Container MIME from a filename, without importing mimetypes."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _MIME_BY_EXTENSION.get(extension, fallback)


def prepare_image(data: bytes, mime_hint: str = "", filename: str = "") -> tuple[str, str]:
    """Return (mime_type, base64) ready to send as Gemini inline_data.

    Passes supported formats through untouched so PNG/JPEG are not needlessly
    re-encoded. Anything else -- an unsupported container, a file whose type
    cannot be inferred, or an animated image whose later frames would otherwise
    be silently dropped -- is flattened to a single PNG frame.
    """
    mime_type = (mime_hint or guess_image_mime(filename) or "").lower()
    if mime_type in GEMINI_IMAGE_MIME_TYPES:
        Image, _, _ = require_pillow()
        with Image.open(io.BytesIO(data)) as probe:
            animated = getattr(probe, "is_animated", False)
        if not animated:
            return mime_type, base64.b64encode(data).decode("ascii")

    Image, ImageOps, _ = require_pillow()
    try:
        with Image.open(io.BytesIO(data)) as source:
            frame = ImageOps.exif_transpose(source).convert("RGBA")
    except Exception as exc:
        raise InputImageError(f"Not a decodable image: {exc}") from exc
    buffer = io.BytesIO()
    frame.save(buffer, "PNG")
    return "image/png", base64.b64encode(buffer.getvalue()).decode("ascii")


def _pixelize_frame(source: Any, config: Config, size: tuple[int, int],
                    preserve_clusters: bool = False) -> Any:
    """Resize and quantize once; sample model-drawn clusters without averaging them."""
    Image, _, _ = require_pillow()
    rgba = source.convert("RGBA")
    solid = rgba.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
    sampler = Image.Resampling.NEAREST if preserve_clusters else Image.Resampling.BOX
    alpha = solid.resize(size, sampler).point(
        lambda value: 255 if value >= 128 else 0
    )
    rgb = rgba.convert("RGB").resize(size, sampler)

    if config.palette:
        rgb = nearest_palette(rgb, config.palette)
    else:
        rgb = rgb.quantize(
            colors=config.max_colors,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.NONE,
        ).convert("RGB")

    rgb.putalpha(alpha)
    return rgb


def build_reference_image(source_bytes: bytes, config: Config) -> tuple[str, str]:
    """Build the low-resolution pixel-grid guide sent alongside the source.

    The original image remains the primary input. The guide is only a second,
    nearest-neighbour-expanded reference that tells the model which broad
    shapes and colour clusters must survive the requested density.
    """
    Image, ImageOps, _ = require_pillow()
    with Image.open(io.BytesIO(source_bytes)) as source:
        frame = ImageOps.exif_transpose(source).convert("RGBA")
    logical = _pixelize_frame(frame, config, config.size)
    guide = logical.resize(frame.size, Image.Resampling.NEAREST)
    buffer = io.BytesIO()
    guide.save(buffer, "PNG")
    return "image/png", base64.b64encode(buffer.getvalue()).decode("ascii")


def build_payload(source_bytes: bytes, config: Config, draft: bytes | None = None,
                  draft_image: Any = None) -> dict[str, Any]:
    """Build the generateContent body for either pass.

    ``draft_image`` lets the caller hand in an already-pixelized draft instead of
    paying for the mapping again; it is purely an optimisation and the result is
    identical either way.
    """
    Image, _, _ = require_pillow()
    with Image.open(io.BytesIO(source_bytes)) as probe:
        if config.source_size != probe.size:
            config = configure_for_source(config, probe.size)
    mime_type, content = prepare_image(source_bytes)
    if draft is None:
        guide_mime, guide_content = build_reference_image(source_bytes, config)
        parts = [
            {"text": config.prompt},
            {"text": "The first image is the original source. Preserve its subject and composition."},
            {"inline_data": {"mime_type": mime_type, "data": content}},
            {"text": "The second image is a pixel-grid and colour-cluster guide. Use it to keep the requested density; do not treat its enlarged blocks as extra detail."},
            {"inline_data": {"mime_type": guide_mime, "data": guide_content}},
        ]
    else:
        review = _expand_to_review(
            draft_image if draft_image is not None else pixelized_draft(draft, config),
            config,
        )
        parts = [
            {"text": config.refine_prompt or REFINE_PROMPT},
            {"text": "The first image is the locally pixelized draft to repaint."},
            {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(review).decode("ascii")}},
            {"text": "The second image is the original subject reference."},
            {"inline_data": {"mime_type": mime_type, "data": content}},
        ]

    generation_config: dict[str, Any] = {"temperature": 0.2}
    if config.response_modalities:
        generation_config["responseModalities"] = list(config.response_modalities)
    image_config: dict[str, Any] = {}
    if config.image_size:
        image_config["imageSize"] = config.image_size
    if config.size[0] == config.size[1]:
        image_config["aspectRatio"] = "1:1"
    if image_config:
        generation_config["imageConfig"] = image_config

    return {
        "contents": [
            {
                "role": "user",
                "parts": parts,
            }
        ],
        "generationConfig": generation_config,
    }


def compact_json(value: Any, limit: int = 1000) -> str:
    safe = value_for_hint(value)
    text = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    return text[:limit] + ("..." if len(text) > limit else "")


def value_for_hint(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<base64 omitted>"
            if "b64" in key.lower()
            else value_for_hint(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [value_for_hint(item) for item in value[:5]]
    if isinstance(value, str) and len(value) > 240:
        return value[:240] + "..."
    return value


def candidate_images(value: Any) -> Iterable[bytes]:
    """Walk a Gemini response and yield decoded image bytes, in candidate order.

    Gemini returns generated images as base64 inside parts, using camelCase
    (``inlineData``/``mimeType``) upstream and snake_case (``inline_data``/
    ``mime_type``) in some tools and relays, so both spellings are accepted.

    A data URI embedded in prose is also accepted.  A bare URL is NOT fetched:
    the old CLI downloaded it, but a browser cannot make that cross-origin
    request without the other host's CORS consent, so extract_image() reports
    the URL as the reason instead of silently producing nothing.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            key_lower = str(key).lower()
            if key_lower in {"inlinedata", "inline_data"} and isinstance(item, dict):
                content = decode_inline_data(item)
                if content:
                    yield content
                continue
            yield from candidate_images(item)
    elif isinstance(value, list):
        for item in value:
            yield from candidate_images(item)
    elif isinstance(value, str):
        for match in DATA_URI_RE.finditer(value):
            content = decode_base64_image(match.group(1))
            if content:
                yield content


def decode_inline_data(part: dict[str, Any]) -> bytes | None:
    data = next(
        (part[key] for key in ("data",) if isinstance(part.get(key), str)),
        None,
    )
    if not data:
        return None
    mime_type = next(
        (
            part[key]
            for key in ("mimeType", "mime_type")
            if isinstance(part.get(key), str)
        ),
        "",
    )
    if mime_type and not mime_type.startswith("image/"):
        return None
    return decode_base64_image(data)


def decode_base64_image(value: str) -> bytes | None:
    try:
        return valid_image_bytes(base64.b64decode(re.sub(r"\s+", "", value), validate=False))
    except (ValueError, base64.binascii.Error):
        return None


def valid_image_bytes(content: bytes) -> bytes | None:
    try:
        Image, _, _ = require_pillow()
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        return content
    except Exception:
        return None


def extract_image(response: dict[str, Any]) -> bytes:
    seen: set[int] = set()
    for content in candidate_images(response):
        marker = hash(content)
        if marker in seen:
            continue
        seen.add(marker)
        return content

    blocked = (
        (response.get("promptFeedback") or {}).get("blockReason")
        or ((response.get("candidates") or [{}])[0].get("finishReason"))
    )
    urls = URL_RE.findall(compact_json(response, limit=20000))
    if urls:
        raise NoImageExtracted(
            "The endpoint returned an image URL instead of inline image data. "
            "A browser cannot fetch that cross-origin unless the host sends CORS "
            f"headers, so this tool does not follow it. URL seen: {urls[0]}"
        )
    raise NoImageExtracted(
        "The upstream call succeeded but no image could be extracted. "
        "Check that the model supports image output and that responseModalities "
        "includes IMAGE"
        + (f" (upstream reason: {blocked})" if blocked else "")
        + f". Response hint: {compact_json(response)}"
    )


def nearest_palette(image: Any, palette: tuple[tuple[int, int, int], ...]) -> Any:
    """Map each RGB pixel to its nearest explicit palette color.

    Ties resolve to the LOWEST palette index, and that is load-bearing: the
    documented preset order is part of the result, so the lists must not be
    reordered to influence output.  Both implementations below honour it --
    the pure-Python min() keeps the first minimum, and numpy's strict ``<``
    update never lets a later index displace an equal-distance earlier one.

    The numpy path exists because the fallback is genuinely too slow to ship:
    the loop costs ~33us per pixel at 64 colors, so a 2000x2000 output (the
    largest the density ladder can reach on a 4096px source) is over two
    minutes of a frozen browser tab.  numpy is loaded lazily in the browser and
    is absent from requirements.txt, so the fallback stays the reference
    implementation and the two are asserted equal in the tests.
    """
    try:
        return _nearest_palette_numpy(image, palette)
    except ImportError:
        pass

    Image, _, _ = require_pillow()
    output = Image.new("RGB", image.size)
    source = image.load()
    target = output.load()
    for y in range(image.height):
        for x in range(image.width):
            red, green, blue = source[x, y]
            target[x, y] = min(
                palette,
                key=lambda color: (
                    (red - color[0]) ** 2
                    + (green - color[1]) ** 2
                    + (blue - color[2]) ** 2
                ),
            )
    return output


def _nearest_palette_numpy(image: Any, palette: tuple[tuple[int, int, int], ...]) -> Any:
    """Vectorised nearest-color map: one pass per palette entry, strict improve.

    Distances are accumulated per channel in int32 rather than materialised as
    an HxWxKx3 tensor.  A 2000x2000 image against 64 colors would need ~3GB in
    that shape, which is not available to wasm32; the per-color pass needs one
    HxW plane instead.  Max distance is 3 * 255**2 = 195075, far inside int32.
    """
    import numpy as np

    Image, _, _ = require_pillow()
    source = np.asarray(image.convert("RGB"), dtype=np.int32)
    height, width = source.shape[:2]
    best = np.full((height, width), np.iinfo(np.int32).max, dtype=np.int32)
    index = np.zeros((height, width), dtype=np.int32)

    for position, color in enumerate(palette):
        red = source[:, :, 0] - int(color[0])
        green = source[:, :, 1] - int(color[1])
        blue = source[:, :, 2] - int(color[2])
        distance = red * red + green * green + blue * blue
        better = distance < best
        if better.any():
            index[better] = position
            best[better] = distance[better]

    lookup = np.asarray(palette, dtype=np.uint8)
    # No explicit mode: Pillow 13 removes the argument, and an HxWx3 uint8
    # array already infers RGB.
    return Image.fromarray(lookup[index])


def pixelize(content: bytes, config: Config, *, preserve_clusters: bool = False) -> Any:
    Image, ImageOps, _ = require_pillow()
    try:
        with Image.open(io.BytesIO(content)) as source:
            source = ImageOps.exif_transpose(source).convert("RGBA")
            return _pixelize_frame(source, config, config.size,
                                   preserve_clusters=preserve_clusters)
    except PixelError:
        raise
    except Exception as exc:
        raise InputImageError(f"Not a decodable image: {exc}") from exc


def pixelized_draft(content: bytes, config: Config) -> Any:
    """The first model output reduced to the target logical grid and palette.

    This is the intermediate the page shows between the two passes, and the same
    image the refine reference is enlarged from -- so it is computed once and
    passed to both, rather than paying for the (expensive, palette-ordered)
    nearest-colour mapping twice.
    """
    Image, ImageOps, _ = require_pillow()
    with Image.open(io.BytesIO(content)) as source:
        frame = ImageOps.exif_transpose(source).convert("RGBA")
    return _pixelize_frame(frame, config, config.size, preserve_clusters=True)


def _expand_to_review(image: Any, config: Config) -> bytes:
    Image, _, _ = require_pillow()
    target_size = config.source_size or image.size
    review = image.resize(target_size, Image.Resampling.NEAREST)
    buffer = io.BytesIO()
    review.save(buffer, "PNG")
    return buffer.getvalue()


def build_refine_reference(content: bytes, config: Config) -> bytes:
    """Build the image shown to the second model pass.

    The first model output is reduced with the same local pixelizer used for the
    final result. It is then enlarged with nearest-neighbour sampling so the
    model can inspect the subject at a useful size while seeing the exact block
    structure it is expected to refine. ``config.source_size`` keeps the review
    image aligned with the original aspect ratio.
    """
    return _expand_to_review(pixelized_draft(content, config), config)


# --------------------------------------------------------------------------
# guards that used to live behind the HTTP layer
# --------------------------------------------------------------------------


def sniff_dimensions(blob: bytes) -> tuple[int, int] | None:
    """Read width/height from the container header, without decoding.

    A byte-size cap does NOT stop a decompression bomb: a 12000x12000 PNG is
    about 450KB on the wire and decodes to 144 megapixels.  So the dimensions
    are read from the header first, in about a millisecond, and the image is
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


def sniff_content_type(blob: bytes) -> str:
    """The raw bytes are the model's output verbatim, so the real type matters.

    The old save_outputs() wrote them to a file named .ai.png regardless, which
    was a pre-existing quirk; labelling the download accurately is the honest
    thing to do now that the browser decides the filename.
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


def check_image_limits(blob: bytes) -> None:
    """Enforce the pixel budget for EVERY container, not just the four we sniff.

    sniff_dimensions() is a fast pre-decode guard, but it only understands PNG,
    JPEG, GIF and WebP -- a TIFF or BMP bomb passes it untouched. This falls
    back to Pillow's lazy header read for anything else, which reads the
    dimensions without decoding the pixels.
    """
    Image, _, _ = require_pillow()
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


def preview_scale_for(n: int) -> int:
    """ONE authority for the preview zoom on the 256px reference canvas.

    The actual output dimensions also depend on the uploaded image, so this is
    a readable default rather than a claim about the final file dimensions.
    """
    return max(1, min(32, round(REFERENCE_CANVAS / n)))


def assert_palette_subset(image: Any, palette: Any) -> None:
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


def palette_histogram(image: Any) -> list[dict[str, Any]]:
    """(hex, count) pairs, most used first.

    Computed here because the report's ``palette`` is a bare hex list -- it
    unpacks getcolors()'s (count, rgb) pairs and throws the count away.
    """
    pairs = image.convert("RGB").getcolors(maxcolors=1 << 20) or []
    return [
        {"hex": "#%02x%02x%02x" % rgb, "count": count}
        for count, rgb in sorted(pairs, reverse=True)
    ]


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


def _png_bytes(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def render_outputs(raw: bytes, pixel_image: Any, config: Config, *,
                   refinement_applied: bool = False,
                   input_name: str | None = None,
                   draft_png: bytes | None = None) -> dict[str, Any]:
    """Turn the run into downloadable bytes plus a report.

    The old save_outputs() wrote four files into a directory and returned paths
    to them.  There is no server filesystem now, so the bytes come back to the
    caller and the frontend makes Blob URLs out of them; the report keeps the
    same shape minus the path fields, which had nothing to point at.
    """
    Image, _, _ = require_pillow()
    buffer = io.BytesIO()
    pixel_image.save(buffer, "PNG")
    pixel_png = buffer.getvalue()

    preview_image = pixel_image.resize(
        (pixel_image.width * config.scale, pixel_image.height * config.scale),
        resample=Image.Resampling.NEAREST,
    )
    preview_buffer = io.BytesIO()
    preview_image.save(preview_buffer, "PNG")

    colors = pixel_image.convert("RGB").getcolors(maxcolors=1_000_000) or []
    report = {
        "input": input_name,
        "size": [pixel_image.width, pixel_image.height],
        "base_size": list(config.base_size or config.size),
        "reference_canvas": REFERENCE_CANVAS,
        "source_size": list(config.source_size) if config.source_size else None,
        "scale": config.scale,
        "color_count": len(colors),
        "max_colors": config.max_colors,
        "palette": ["#%02x%02x%02x" % color for _, color in colors],
        "palette_used": palette_histogram(pixel_image),
        "palette_requested": (
            ["#%02x%02x%02x" % color for color in config.palette] if config.palette else None
        ),
        "model": config.model or None,
        "protocol": f"gemini {config.api_version}",
        "refinement_applied": refinement_applied,
    }
    return {
        "pixel_png": pixel_png,
        "preview_png": preview_buffer.getvalue(),
        "raw_png": raw if config.keep_raw else None,
        # The intermediate the page can show between the two passes, when there
        # was a second pass at all.
        "draft_png": draft_png,
        "report": report,
    }


# --------------------------------------------------------------------------
# error envelope
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
    """Remove secret material from anything headed for the screen.

    Applied to EVERY string field of the error envelope -- message, hint,
    where, detail and traceback -- because a traceback can carry a frame's
    local variables and is therefore not exempt.

    The exact key and its base64 form are replaced first (we know the secret,
    so this is exact rather than heuristic); the pattern sweep afterwards is a
    backstop for credentials we do not hold, such as a key an upstream error
    echoes after a rotation.

    This mattered when the server held one key.  It matters more now that the
    key is the user's own and the error text is rendered in their browser: a
    gateway that echoes the request headers would otherwise print the key into
    the page, the copy button, and any screenshot of the console.
    """
    if not text:
        return text
    if api_key and len(api_key) >= 6:
        text = text.replace(api_key, "[redacted:api_key]")
        encoded = base64.b64encode(api_key.encode()).decode()
        text = text.replace(encoded, "[redacted:api_key:b64]")
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
    """Strip long strings from an upstream response so it is safe to keep.

    Any string over 2000 characters becomes a placeholder.  That removes the
    inline base64 image and any long echoed blob while leaving the model's
    short explanation text -- which is the whole point, because
    value_for_hint() truncates at 240 characters and compact_json at 1000, so
    a 900-character refusal would otherwise reach the user as 240.
    """
    if depth > 12:
        return "<depth limit>"
    if isinstance(value, dict):
        return {str(k): sanitize_response(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_response(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        # The budget has to clear a model's prose explanation (which is the
        # point of keeping this) while still removing an inline base64 image,
        # and no real image is under 2KB.
        if len(value) > 2000:
            return f"<{len(value)}-char string omitted>"
        return value
    return value


def classify(exc: BaseException, where: str = "") -> str:
    """Map an exception to an error kind, structurally.

    Pillow's exception types are matched by name rather than by isinstance so
    that importing this module never requires Pillow; classify() is used on the
    failure path, which is exactly where a Pillow import could itself fail.
    """
    if isinstance(exc, PixelError):
        return exc.kind
    if isinstance(exc, (TimeoutError,)) or type(exc).__name__ == "TimeoutException":
        return "upstream_timeout"
    name = type(exc).__name__
    if name == "DecompressionBombError":
        return "limits"
    if name == "UnidentifiedImageError":
        return "input_image"
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        # During the upstream exchange this is far more often the endpoint
        # dropping the connection than the user's browser.
        if where in ("upstream_wait", "refine_wait", "extract_image"):
            return "upstream_transport"
        return "client_disconnect"
    if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)):
        return "config"
    return "internal"


def to_envelope(
    exc: BaseException,
    api_key: str,
    where: str = "",
    phase: str | None = None,
    include_traceback: bool = False,
) -> dict[str, Any]:
    """Build the ONE error shape the frontend renders.

    The frontend classifies by ``kind`` and shows message/hint/detail/
    http_status/where/phase, so nothing here may change without changing
    static/js/errors.js.
    """
    import traceback

    kind = classify(exc, where)
    detail = getattr(exc, "detail", "") or ""
    status = getattr(exc, "status", None)

    envelope: dict[str, Any] = {
        "kind": kind,
        "phase": phase,
        "where": where,
        "exception": f"{type(exc).__module__}.{type(exc).__name__}",
        "message": redact(str(exc), api_key),
        "detail": redact(detail[:4000], api_key) + ("...[truncated]" if len(detail) > 4000 else ""),
        "http_status": status,
        "hint": "",
    }
    if kind == "upstream_http" and status:
        envelope["hint"] = f"上游返回 HTTP {status}。"
    if include_traceback:
        envelope["traceback"] = redact(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), api_key
        )
    return envelope


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Generation:
    """What one run produced: the model's raw bytes and the rendered artifacts."""

    raw: bytes
    refinement_applied: bool
    outputs: dict[str, Any]


EmitFn = Callable[..., None]
UpstreamFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def generate(
    config: Config,
    source_bytes: bytes,
    call_upstream: UpstreamFn,
    *,
    pixelize_only: bool = False,
    preserve_clusters: bool | None = None,
    emit: EmitFn | None = None,
    input_name: str | None = None,
) -> Generation:
    """Run the whole pipeline: model passes, pixelization, guards, rendering.

    This is where the CLI's main() and the deleted web layer's work() both
    collapse.  It is async because the transport is async in the browser, but
    the transport itself is injected, so the tests drive the identical code with
    a fake that returns canned responses and no network exists anywhere.

    Progress is emitted between awaits, never per pixel: a JS<->Python call
    costs tens of microseconds, so per-pixel events would be pure overhead.
    """
    def announce(phase: str, label: str, detail: str = "") -> None:
        if emit is not None:
            emit(phase, label, detail)

    announce("received", "已收到图片", input_name or "")

    Image, _, _ = require_pillow()
    check_image_limits(source_bytes)
    try:
        with Image.open(io.BytesIO(source_bytes)) as probe:
            source_size = probe.size
    except Exception as exc:
        raise InputImageError(f"Not a decodable image: {exc}") from exc
    config = configure_for_source(config, source_size)

    announce("encoding", "正在编码请求", f"{source_size[0]}x{source_size[1]} 源图")

    refinement_applied = False
    draft_png = None
    if pixelize_only:
        raw = source_bytes
    else:
        validate_upstream(config)

        announce("upstream_wait", "正在调用模型", f"{config.model} @ {safe_host(config.base_url)}")
        response = await call_upstream(build_payload(source_bytes, config))
        announce("upstream_response", "模型已返回", "")

        announce("extract_start", "正在解析模型输出", "")
        raw = extract_image(response)
        announce("extracted", "已取得模型输出", f"{len(raw)} 字节")

        if config.passes == 2:
            announce("refine_wait", "正在第二轮精修", "")
            # Computed once and reused: the same reduced draft is both the
            # reference the model refines from and the intermediate the page
            # displays. Recomputing it would double the palette-mapping cost,
            # which is the most expensive step in the pipeline.
            draft_image = pixelized_draft(raw, config)
            try:
                refined_response = await call_upstream(
                    build_payload(source_bytes, config, draft=raw, draft_image=draft_image)
                )
                announce("refine_response", "第二轮已返回", "")
                raw = extract_image(refined_response)
                refinement_applied = True
                draft_png = _png_bytes(draft_image)
                announce("refined", "精修完成", "")
            except (PixelError, ValueError) as exc:
                # A failed refinement is a degradation, not a failure: the first
                # draft is already a complete result, so the run continues and
                # the report says refinement_applied = false.
                announce("refine_response", "第二轮失败，沿用首轮草稿",
                         f"{type(exc).__name__}: {exc}")

    announce("pixelizing", "正在像素化", "")
    # Two different jobs share the pixelize-only path: reducing the user's own
    # photo (BOX averaging, because a photo's pixels should be averaged), and
    # re-rendering the model's output at a new density or palette (NEAREST,
    # because the model drew deliberate clusters that must not be smeared).
    if preserve_clusters is None:
        preserve_clusters = not pixelize_only
    pixel_image = pixelize(raw, config, preserve_clusters=preserve_clusters)

    announce("verifying", "正在校验调色板", "")
    assert_palette_subset(pixel_image, config.palette)

    announce("saving", "正在生成产物", "")
    outputs = render_outputs(
        raw, pixel_image, config,
        refinement_applied=refinement_applied,
        input_name=input_name,
        draft_png=draft_png,
    )

    announce("done", "完成", "")
    return Generation(raw=raw, refinement_applied=refinement_applied, outputs=outputs)
