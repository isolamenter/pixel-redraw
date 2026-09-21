#!/usr/bin/env python3
"""Minimal NewAPI (Gemini) image-to-pixel-art converter.

The NewAPI call is intentionally kept small and configurable.  It speaks the
Gemini native generateContent protocol, so the model is asked for image output
and the local Pillow pass enforces the final pixel-art constraints.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_PROMPT = """Redraw the provided image as clean, deliberate pixel art.
Preserve the subject, silhouette, pose, composition, and important colors.
The target logical canvas is {width}x{height} pixels.
Use hard pixel-aligned edges, a limited palette, crisp clusters, and no
anti-aliasing, blur, photographic texture, smooth gradients, text, or watermark.
Return one edited image only; do not return an explanation or a code block."""

DATA_URI_RE = re.compile(r"data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)")
URL_RE = re.compile(r"https?://[^\s)\]}>\"']+")


DEFAULT_API_VERSION = "v1beta"

# Formats Gemini accepts for inline image data. Anything else (GIF, BMP, TIFF,
# ...) has to be re-encoded before it can be sent.
GEMINI_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/heic",
    "image/heif",
}


class PixelError(RuntimeError):
    """Base class for every failure this module raises deliberately.

    Subclasses RuntimeError so that main()'s existing
    ``except (OSError, ValueError, RuntimeError)`` still catches every one of
    them and the CLI's exit code and message text are unchanged.  ``kind``
    exists so a frontend can classify a failure structurally instead of by
    matching message wording: pixel_web.py maps it to the error envelope, and
    several of these raise sites have ``__cause__ is None``, so a classifier
    that inspected the cause could not tell a relay quota error apart from an
    internal bug.
    """

    kind = "internal"


class ConfigError(PixelError, ValueError):
    kind = "config"


class PaletteError(PixelError, ValueError):
    kind = "palette"


class SizeError(PixelError, ValueError):
    kind = "size"


class NewAPIHTTPError(PixelError):
    kind = "upstream_http"

    def __init__(self, message: str, status: int | None = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail  # the FULL upstream body, not the 1200-char slice


class NewAPITransportError(PixelError):
    kind = "upstream_transport"


class NewAPINonJSONError(PixelError):
    kind = "upstream_non_json"

    def __init__(self, message: str, status: int | None = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


class NewAPIProtocolError(PixelError):
    kind = "upstream_protocol"


class NewAPIErrorField(PixelError):
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


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load simple KEY=value entries without overwriting shell variables."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        reference = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
        if reference:
            value = os.getenv(reference.group(1), "")
        os.environ.setdefault(key, value)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)(?:[xX](\d+))?\s*", value)
    if not match:
        raise SizeError(f"Invalid size {value!r}; use N or WIDTHxHEIGHT, e.g. 64x64")
    width = int(match.group(1))
    height = int(match.group(2) or match.group(1))
    if not (1 <= width <= 1024 and 1 <= height <= 1024):
        raise SizeError("Pixel canvas dimensions must be between 1 and 1024")
    return width, height


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


def build_config(args: argparse.Namespace) -> Config:
    size = parse_size(args.size or os.getenv("PIXEL_SIZE", "64x64"))
    palette = parse_palette(args.palette or os.getenv("PIXEL_PALETTE", "auto"))
    prompt = args.prompt or os.getenv("PIXEL_PROMPT", DEFAULT_PROMPT)
    prompt = prompt.replace("{width}", str(size[0])).replace("{height}", str(size[1]))

    base_url = os.getenv("NEWAPI_BASE_URL", "").strip()
    api_key = os.getenv("NEWAPI_API_KEY", "").strip()
    model = os.getenv("NEWAPI_MODEL", "").strip()
    api_version = os.getenv("NEWAPI_API_VERSION", DEFAULT_API_VERSION).strip() or DEFAULT_API_VERSION

    # Unset -> ask for an image, since that is why this tool exists. Set but
    # empty -> send no responseModalities at all, for gateways that reject the
    # field. (An empty value must not silently fall back, or there is no way to
    # turn the field off.)
    modalities_raw = os.getenv("NEWAPI_RESPONSE_MODALITIES")
    if modalities_raw is None:
        response_modalities = ("TEXT", "IMAGE")
    else:
        response_modalities = tuple(
            item.strip().upper() for item in modalities_raw.split(",") if item.strip()
        )

    if not args.pixelize_only:
        missing = [
            name
            for name, value in (
                ("NEWAPI_BASE_URL", base_url),
                ("NEWAPI_API_KEY", api_key),
                ("NEWAPI_MODEL", model),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing .env values: " + ", ".join(missing) + ". "
                "Copy .env.example to .env and fill them in."
            )

    return Config(
        base_url=base_url,
        api_key=api_key,
        model=model,
        api_version=api_version,
        timeout=float(os.getenv("NEWAPI_TIMEOUT", "180")),
        size=size,
        scale=args.scale if args.scale is not None else int(os.getenv("PIXEL_SCALE", "8")),
        max_colors=(
            args.max_colors
            if args.max_colors is not None
            else int(os.getenv("PIXEL_MAX_COLORS", "16"))
        ),
        palette=palette,
        prompt=prompt,
        keep_raw=args.keep_raw or env_bool("PIXEL_KEEP_RAW", True),
        response_modalities=response_modalities,
        image_size=os.getenv("NEWAPI_IMAGE_SIZE", "").strip(),
    )


def require_pillow() -> tuple[Any, Any, Any]:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("Pillow is required. Run: python3 -m pip install -r requirements.txt") from exc
    return Image, ImageOps, None


def api_url(base_url: str, api_version: str, model: str) -> str:
    """Build the Gemini native generateContent URL: BASE/VERSION/models/MODEL:generateContent."""
    return (
        f"{base_url.rstrip('/')}/{api_version.strip('/')}"
        f"/models/{model}:generateContent"
    )


def prepare_image(path: Path) -> tuple[str, str]:
    """Return (mime_type, base64) ready to send as Gemini inline_data.

    Passes supported formats through untouched so PNG/JPEG are not needlessly
    re-encoded. Anything else -- an unsupported container, a file whose type
    cannot be inferred, or an animated image whose later frames would otherwise
    be silently dropped -- is flattened to a single PNG frame.
    """
    mime_type = (mimetypes.guess_type(path.name)[0] or "").lower()
    if mime_type in GEMINI_IMAGE_MIME_TYPES:
        Image, _, _ = require_pillow()
        with Image.open(path) as probe:
            animated = getattr(probe, "is_animated", False)
        if not animated:
            return mime_type, base64.b64encode(path.read_bytes()).decode("ascii")

    Image, ImageOps, _ = require_pillow()
    with Image.open(path) as source:
        frame = ImageOps.exif_transpose(source).convert("RGBA")
    buffer = io.BytesIO()
    frame.save(buffer, "PNG")
    return "image/png", base64.b64encode(buffer.getvalue()).decode("ascii")


def build_payload(input_path: Path, config: Config) -> dict[str, Any]:
    mime_type, content = prepare_image(input_path)

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
                "parts": [
                    {"text": config.prompt},
                    {"inline_data": {"mime_type": mime_type, "data": content}},
                ],
            }
        ],
        "generationConfig": generation_config,
    }


def call_newapi(input_path: Path, config: Config) -> dict[str, Any]:
    url = api_url(config.base_url, config.api_version, config.model)
    headers = {
        "x-goog-api-key": config.api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "pixel-redraw-mvp/0.1",
    }
    body = json.dumps(build_payload(input_path, config)).encode("utf-8")

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        # The message keeps the 1200-byte slice the CLI has always printed; the
        # full body rides on .detail for callers that can show more than a line.
        body = exc.read().decode("utf-8", errors="replace")
        raise NewAPIHTTPError(
            f"NewAPI HTTP {exc.code}: {body[:1200]}", status=exc.code, detail=body
        ) from exc
    except urllib.error.URLError as exc:
        raise NewAPITransportError(f"NewAPI connection failed: {exc.reason}") from exc

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        # An HTML error page from a reverse proxy is the most common gateway
        # failure, and "HTTP 200" alone gives the reader nothing to act on.
        raise NewAPINonJSONError(
            f"NewAPI returned non-JSON response (HTTP {status}): "
            + raw[:500].decode("utf-8", errors="replace"),
            status=status,
            detail=raw[:4000].decode("utf-8", errors="replace"),
        ) from exc
    if not isinstance(parsed, dict):
        raise NewAPIProtocolError("NewAPI response must be a JSON object")
    if parsed.get("error"):
        raise NewAPIErrorField(f"NewAPI returned an error: {compact_json(parsed['error'])}")
    return parsed


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
        # Some relays inline the image as a data URI or a plain URL instead.
        for match in DATA_URI_RE.finditer(value):
            content = decode_base64_image(match.group(1))
            if content:
                yield content
        for match in URL_RE.finditer(value):
            content = download_image(match.group(0).rstrip(".,"))
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


def download_image(url: str) -> bytes | None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "pixel-redraw-mvp/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return valid_image_bytes(response.read())
    except (urllib.error.URLError, ValueError):
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
    raise NoImageExtracted(
        "NewAPI returned successfully but no image could be extracted. "
        "Check that NEWAPI_MODEL supports image output and that "
        "NEWAPI_RESPONSE_MODALITIES includes IMAGE"
        + (f" (upstream reason: {blocked})" if blocked else "")
        + f". Response hint: {compact_json(response)}"
    )


def nearest_palette(image: Any, palette: tuple[tuple[int, int, int], ...]) -> Any:
    """Map each RGB pixel to its nearest explicit palette color."""
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


# Mode-voting needs a working palette wide enough that a black outline keeps its
# own entry instead of merging into dark greys, but narrow enough that "most
# common color in this block" is still meaningful. Measured on line art, 64
# reproduces the source's outline coverage far better than 16 or 32 -- at 64x64.
#
# The constant is a CAP, not the working size: downscale_preserving_dominance()
# narrows it to 2*max(blocks) for small canvases, giving 8x8 -> 16 and
# 16x16 -> 32 while leaving 32x32 and 64x64 at 64.  At 8x8 the wide palette makes
# the block vote close to a coin flip (measured winner share 0.461 at 64 versus
# 0.543 at 16) and scatters single-tile black specks; at 32 and above the narrow
# palette over-represents dark instead, which is the "outline too heavy" failure
# this constant was originally tuned against.
MODE_PREQUANTIZE_COLORS = 64


def dominant_block_colors(image: Any, blocks_x: int, blocks_y: int, block_size: int) -> Any:
    """Downscale by picking the most common color in each block, not the average.

    Averaging is wrong for line art: a bold black outline spread across a block
    gets diluted into grey and the outline disappears. Voting keeps the color
    that actually covers most of the block, so outlines survive.
    """
    Image, _, _ = require_pillow()
    indices = list(image.getdata())
    palette = image.getpalette()
    stride = blocks_x * block_size
    output = Image.new("RGB", (blocks_x, blocks_y))
    target = output.load()

    for block_y in range(blocks_y):
        for block_x in range(blocks_x):
            counts: dict[int, int] = {}
            for y in range(block_y * block_size, (block_y + 1) * block_size):
                start = y * stride + block_x * block_size
                for index in indices[start : start + block_size]:
                    counts[index] = counts.get(index, 0) + 1
            winner = max(counts, key=counts.__getitem__)
            offset = winner * 3
            target[block_x, block_y] = (
                palette[offset],
                palette[offset + 1],
                palette[offset + 2],
            )
    return output


def downscale_preserving_dominance(image: Any, size: tuple[int, int]) -> Any:
    """Shrink `image` to `size` while preserving flat regions and hard outlines.

    Works in two stages: a generous pre-quantization flattens anti-aliasing so
    that "most common color" is meaningful per block, then each block votes.
    Pre-quantizing too aggressively merges the outline into dark greys, which is
    why the working palette is wider than the final one.
    """
    Image, _, _ = require_pillow()
    width, height = image.size
    blocks_x, blocks_y = size

    block_size = max(1, round(max(width, height) / max(blocks_x, blocks_y)))
    if block_size == 1:
        # Nothing to vote over; plain area averaging is the best available.
        return image.resize(size, Image.Resampling.BOX)

    working = (blocks_x * block_size, blocks_y * block_size)
    if working != (width, height):
        image = image.resize(working, Image.Resampling.BOX)

    # Scale the working palette to the canvas, capped by the tuned constant.
    colors = min(MODE_PREQUANTIZE_COLORS, 2 * max(blocks_x, blocks_y))
    quantized = image.quantize(
        colors=colors,
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.NONE,
    )
    return dominant_block_colors(quantized, blocks_x, blocks_y, block_size)


def pixelize(content: bytes, config: Config) -> Any:
    Image, ImageOps, _ = require_pillow()
    with Image.open(io.BytesIO(content)) as source:
        source = ImageOps.exif_transpose(source).convert("RGBA")
        # Binarize alpha before shrinking so a block counts as opaque when most
        # of it is opaque -- the majority vote for transparency.
        solid = source.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
        alpha = solid.resize(config.size, Image.Resampling.BOX).point(
            lambda value: 255 if value >= 128 else 0
        )
        rgb = downscale_preserving_dominance(source.convert("RGB"), config.size)

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


def save_outputs(input_path: Path, raw: bytes, pixel_image: Any, config: Config, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = input_path.stem
    raw_path = out_dir / f"{stem}.ai.png"
    logical_path = out_dir / f"{stem}.pixel.png"
    preview_path = out_dir / f"{stem}.pixel_x{config.scale}.png"
    report_path = out_dir / f"{stem}.report.json"

    if config.keep_raw:
        raw_path.write_bytes(raw)
    pixel_image.save(logical_path, "PNG")
    preview = pixel_image.resize(
        (pixel_image.width * config.scale, pixel_image.height * config.scale),
        resample=require_pillow()[0].Resampling.NEAREST,
    )
    preview.save(preview_path, "PNG")

    colors = pixel_image.convert("RGB").getcolors(maxcolors=1_000_000) or []
    report = {
        "input": str(input_path),
        "logical_output": str(logical_path),
        "preview_output": str(preview_path),
        "raw_output": str(raw_path) if config.keep_raw else None,
        "size": [pixel_image.width, pixel_image.height],
        "scale": config.scale,
        "color_count": len(colors),
        "max_colors": config.max_colors,
        "palette": ["#%02x%02x%02x" % color for _, color in colors],
        "model": config.model or None,
        "protocol": f"gemini {config.api_version}",
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Redraw an image as true pixel art through NewAPI")
    parser.add_argument("input", type=Path, help="Input image path")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory; defaults to PIXEL_OUT_DIR or output")
    parser.add_argument("--size", default=None, help="Logical size, e.g. 64 or 64x48; defaults to PIXEL_SIZE")
    parser.add_argument("--scale", type=int, default=None, help="Nearest-neighbor preview scale")
    parser.add_argument("--max-colors", type=int, default=None, help="Maximum colors when PIXEL_PALETTE=auto")
    parser.add_argument("--palette", default=None, help="auto or comma-separated #RRGGBB values")
    parser.add_argument("--prompt", default=None, help="Extra/full redraw prompt")
    parser.add_argument("--keep-raw", action="store_true", help="Keep the raw image returned by NewAPI")
    parser.add_argument("--pixelize-only", action="store_true", help="Skip NewAPI and pixelize the input image")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv()
    try:
        config = build_config(args)
        input_path = args.input.expanduser().resolve()
        if not input_path.is_file():
            raise ValueError(f"Input image does not exist: {input_path}")
        if config.scale < 1 or config.scale > 32:
            raise ValueError("Scale must be between 1 and 32")
        if config.max_colors < 2 or config.max_colors > 256:
            raise ValueError("Max colors must be between 2 and 256")

        if args.pixelize_only:
            raw = input_path.read_bytes()
        else:
            print(
                f"Calling NewAPI model {config.model!r} via {config.api_version} generateContent ...",
                file=sys.stderr,
            )
            response = call_newapi(input_path, config)
            raw = extract_image(response)

        out_dir = Path(args.out_dir or os.getenv("PIXEL_OUT_DIR", "output")).expanduser()
        report = save_outputs(input_path, raw, pixelize(raw, config), config, out_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
