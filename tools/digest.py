"""Print a digest of the core's output for a fixed synthetic input.

Run under native CPython, and inside Pyodide via smoke.mjs, to compare the two
runtimes.  It hashes the RGB pixel data rather than the PNG bytes on purpose:
the two runtimes carry different Pillow versions, so the encoder can differ
while the quantization is identical -- and the quantization is the thing under
test.

Usage: python3 tools/digest.py
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pixel_redraw as pr  # noqa: E402


def fixture(size=(64, 64)) -> bytes:
    """A deterministic many-colour image, so MEDIANCUT has real work to do."""
    image = Image.new("RGB", size)
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            pixels[x, y] = ((x * 4) % 256, (y * 4) % 256, ((x + y) * 2) % 256)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def digest(image) -> dict:
    rgb = image.convert("RGB")
    return {
        "size": list(rgb.size),
        "colors": len(rgb.getcolors(maxcolors=1 << 20) or []),
        "sha256": hashlib.sha256(rgb.tobytes()).hexdigest()[:32],
    }


async def main() -> dict:
    source = fixture()
    report = {}

    auto = pr.make_config(model="m", api_key="k", size="16x16", max_colors=16)
    generation = await pr.generate(auto, source, None, pixelize_only=True)
    with Image.open(io.BytesIO(generation.outputs["pixel_png"])) as pixel:
        report["auto_mediancut"] = digest(pixel)

    palette = pr.make_config(
        model="m", api_key="k", size="16x16", palette="#ff0000,#00ff00,#0000ff,#ffff00"
    )
    generation = await pr.generate(palette, source, None, pixelize_only=True)
    with Image.open(io.BytesIO(generation.outputs["pixel_png"])) as pixel:
        report["fixed_palette"] = digest(pixel)

    # The numpy fast path must equal the pure-Python reference, ties included.
    with Image.open(io.BytesIO(source)) as image:
        reference = image.convert("RGB")
    colors = ((0, 0, 0), (10, 10, 10), (200, 30, 30), (255, 255, 255))
    vectorised = pr.nearest_palette(reference, colors)
    hidden = pr._nearest_palette_numpy
    try:
        pr._nearest_palette_numpy = lambda *a, **k: (_ for _ in ()).throw(ImportError())
        looped = pr.nearest_palette(reference, colors)
    finally:
        pr._nearest_palette_numpy = hidden

    report["nearest_palette_fast_path_matches_loop"] = (
        vectorised.tobytes() == looped.tobytes()
    )
    report["numpy_available"] = "numpy" in sys.modules or _numpy_present()

    return report


def _numpy_present() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


if __name__ == "__main__":
    result = asyncio.run(main())
    result["pillow"] = Image.__version__
    result["python"] = sys.version.split()[0]
    print(json.dumps(result, indent=2, sort_keys=True))
