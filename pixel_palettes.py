#!/usr/bin/env python3
"""Prescribed pixel-art palettes offered by the web UI.

Pure data plus a self-check.  Every value here was cross-checked against at
least one external source; the ``verified`` flag records how strong that
evidence is, and ``source`` records where it came from.  Two flags are False
because only one independent source could be found, NOT because the values are
known to be wrong -- the distinction matters, so it is carried in the data
rather than flattened into a boolean "trustworthy".

``self_check()`` runs at import time and asserts that every preset parses
through ``pixel_redraw.parse_palette()`` and comes back with the expected
number of colours, so a mistyped hex crashes the server at startup instead of
silently producing wrong colours on the user's first click.  It catches
SYNTAX only -- a wrong-but-well-formed hex passes it, which is exactly why the
``verified`` flag and ``source`` string exist.

Ordering is preserved and meaningful: ``nearest_palette()`` resolves an exact
distance tie to the LOWEST index, so the documented order is part of the
result.  Do not reorder these lists to influence output.
"""

from __future__ import annotations

import pixel_redraw

DEFAULT_PRESET = "pico8"
MAX_PALETTE_COLORS = 256

# ``weak_at`` lists the canvas sizes at which this palette measurably collapses
# on a low-contrast portrait: the mode vote hands one colour most of the canvas
# and the result flattens to a near-solid field.  Measured on the reference
# render, top-1 colour share at 8x8: aap64 0.375, pico8 0.422, endesga32 0.422,
# sweetie16 0.438 (all readable) versus c64_pepto 0.766, db16 0.812, slso8
# 0.812, gameboy_bgb 0.859, gameboy_dmg 0.859 (all collapse).  The failure mode
# is NOT "too few colours" -- sweetie16 has 16 and reads, slso8 has 8 and does
# not.  It is whether the palette owns a dark far enough from the source's
# dominant light tone to win whole blocks.
PALETTE_PRESETS: dict[str, dict] = {
    "pico8": {
        "label": "PICO-8",
        "label_zh": "PICO-8",
        "colors": (
            "#000000", "#1d2b53", "#7e2553", "#008751", "#ab5236", "#5f574f",
            "#c2c3c7", "#fff1e8", "#ff004d", "#ffa300", "#ffec27", "#00e436",
            "#29adff", "#83769c", "#ff77a8", "#ffccaa",
        ),
        "note": "Official fantasy-console palette, CC0. Four independent sources agree byte for byte. The only 16-colour preset with no near-duplicate pair (closest two are dE 13.63 apart), so no slot is wasted.",
        "note_zh": "官方幻想主机调色板，CC0。四个独立来源逐字一致。是唯一没有任何近似色对的 16 色预设（最接近的两色 ΔE 13.63），不浪费槽位。",
        "verified": True,
        "source": "https://lospec.com/palette-list/pico-8 ; https://pico-8.fandom.com/wiki/Palette ; https://www.lexaloffle.com/gfx/pico8_palette.png ; https://docs.rs/bulb/latest/src/bulb/dither/presets.rs.html",
    },
    "arne16": {
        "label": "ARNE 16",
        "label_zh": "ARNE 16",
        "colors": (
            "#000000", "#493c2b", "#be2633", "#e06f8b", "#9d9d9d", "#a46422",
            "#eb8931", "#f7e26b", "#ffffff", "#1b2632", "#2f484e", "#44891a",
            "#a3ce27", "#005784", "#31a2f2", "#b2dcef",
        ),
        "note": "Arne's general-purpose 16-colour palette: strong warm/cool ramps with a useful dark range.",
        "note_zh": "Arne 的通用 16 色：暖色、冷色和暗部层次都比较均衡，适合小尺寸像素画。",
        "verified": True,
        "source": "https://androidarts.com/palette/16pal.htm ; https://lospec.com/palette-list/arne-16",
    },
    "db32": {
        "label": "DawnBringer 32",
        "label_zh": "DawnBringer 32",
        "colors": (
            "#000000", "#222034", "#45283c", "#663931", "#8f563b", "#df7126",
            "#d9a066", "#eec39a", "#fbf236", "#99e550", "#6abe30", "#37946e",
            "#4b692f", "#524b24", "#323c39", "#3f3f74", "#306082", "#5b6ee1",
            "#639bff", "#5fcde4", "#cbdbfc", "#ffffff", "#9badb7", "#847e87",
            "#696a6a", "#595652", "#76428a", "#ac3232", "#d95763", "#d77bba",
            "#8f974a", "#8a6f30",
        ),
        "note": "DawnBringer's 32-colour multipurpose palette, with more hue and skin-tone room than DB16.",
        "note_zh": "DawnBringer 的 32 色通用调色板，比 DB16 有更多色相和肤色空间。",
        "verified": True,
        "source": "https://pixeljoint.com/forum/printer_friendly_posts.asp?TID=16247 ; https://lospec.com/palette-list/dawnbringer-32 ; https://github.com/aseprite/aseprite/blob/main/data/extensions/dawnbringer-palettes/db32.gpl",
    },
    "apollo": {
        "label": "Apollo 46",
        "label_zh": "Apollo 46",
        "colors": (
            "#172038", "#253a5e", "#3c5e8b", "#4f8fba", "#73bed3", "#a4dddb",
            "#19332d", "#25562e", "#468232", "#75a743", "#a8ca58", "#d0da91",
            "#4d2b32", "#7a4841", "#ad7757", "#c09473", "#d7b594", "#e7d5b3",
            "#341c27", "#602c2c", "#884b2b", "#be772b", "#de9e41", "#e8c170",
            "#241527", "#411d31", "#752438", "#a53030", "#cf573c", "#da863e",
            "#1e1d39", "#402751", "#7a367b", "#a23e8c", "#c65197", "#df84a5",
            "#090a14", "#10141f", "#151d28", "#202e37", "#394a50", "#577277",
            "#819796", "#a8b5b2", "#c7cfcc", "#ebede9",
        ),
        "note": "AdamCYounis's saturated 46-colour palette, arranged in compact blue, green, warm, purple and neutral ramps.",
        "note_zh": "AdamCYounis 的高饱和 46 色，覆盖蓝、绿、暖色、紫色和中性色渐变。",
        "verified": False,
        "source": "https://lospec.com/palette-list/apollo.txt ; https://lospec.com/adamcyounis ; https://www.youtube.com/watch?v=SQjeNhTp_Xg",
    },
    "resurrect64": {
        "label": "Resurrect 64",
        "label_zh": "Resurrect 64",
        "colors": (
            "#2e222f", "#3e3546", "#625565", "#966c6c", "#ab947a", "#694f62",
            "#7f708a", "#9babb2", "#c7dcd0", "#ffffff", "#6e2727", "#b33831",
            "#ea4f36", "#f57d4a", "#ae2334", "#e83b3b", "#fb6b1d", "#f79617",
            "#f9c22b", "#7a3045", "#9e4539", "#cd683d", "#e6904e", "#fbb954",
            "#4c3e24", "#676633", "#a2a947", "#d5e04b", "#fbff86", "#165a4c",
            "#239063", "#1ebc73", "#91db69", "#cddf6c", "#313638", "#374e4a",
            "#547e64", "#92a984", "#b2ba90", "#0b5e65", "#0b8a8f", "#0eaf9b",
            "#30e1b9", "#8ff8e2", "#323353", "#484a77", "#4d65b4", "#4d9be6",
            "#8fd3ff", "#45293f", "#6b3e75", "#905ea9", "#a884f3", "#eaaded",
            "#753c54", "#a24b6f", "#cf657f", "#ed8099", "#831c5d", "#c32454",
            "#f04f78", "#f68181", "#fca790", "#fdcbb0",
        ),
        "note": "Kerrie Lake's broad 64-colour palette, especially useful for colourful scenes and higher-density output.",
        "note_zh": "Kerrie Lake 的 64 色通用调色板，适合色彩丰富、密度较高的画面。",
        "verified": False,
        "source": "https://lospec.com/palette-list/resurrect-64 ; https://lospec.com/palette-list/resurrect-64.txt ; https://lospec.com/kerrielake",
    },
    "hw_cga": {
        "label": "CGA 16",
        "label_zh": "CGA 16",
        "colors": (
            "#000000", "#0000aa", "#00aa00", "#00aaaa", "#aa0000", "#aa00aa",
            "#aa5500", "#aaaaaa", "#555555", "#5555ff", "#55ff55", "#55ffff",
            "#ff5555", "#ff55ff", "#ffff55", "#ffffff",
        ),
        "note": "The classic IBM CGA 16-colour table: extremely bright and high-contrast, deliberately unlike modern art palettes.",
        "note_zh": "经典 IBM CGA 16 色：非常明亮、高对比，风格上明显区别于现代像素画调色板。",
        "verified": True,
        "source": "https://paulwratt.github.io/programmers-palettes/HW-CGA/HW-CGA.html ; https://paulwratt.github.io/programmers-palettes/HW-CGA/HW-CGA-hex.html ; https://github.com/denilsonsa/gimp-palettes",
    },
    "hw_msx": {
        "label": "MSX 16",
        "label_zh": "MSX 16",
        "colors": (
            "#000000", "#010101", "#3eb849", "#74d07d", "#5955e0", "#8076f1",
            "#b95e51", "#65dbef", "#db6559", "#ff897d", "#ccc35e", "#ded087",
            "#3aa241", "#b766b5", "#cccccc", "#ffffff",
        ),
        "note": "A documented HW-MSX 16-colour approximation; real MSX output varies by VDP and video path.",
        "note_zh": "资料化的 HW-MSX 16 色近似；真实 MSX 画面会因 VDP 和视频输出链路而不同。",
        "verified": False,
        "source": "https://paulwratt.github.io/programmers-palettes/HW-MSX/HW-MSX-palettes.html ; https://www.msx.org/forum/msx-talk/graphics-and-music/msx-1-vdp-definitive-hex-code-colors ; https://github.com/reidrac/8-bit-gimp-palettes",
    },
    "gameboy_dmg": {
        "label": "Game Boy (DMG)",
        "label_zh": "Game Boy（DMG 绿）",
        "colors": ("#0f380f", "#306230", "#8bac0f", "#9bbc0f"),
        "note": "The classic 4-shade green LCD. Its two lightest greens are only dE 4.71 apart, so it really offers about 3 usable levels.",
        "note_zh": "经典 4 级绿色 LCD。两个最亮的绿只差 ΔE 4.71，实际只有约 3 个可用层级。",
        "verified": True,
        "weak_at": (8, 16),
        "source": "https://docs.rs/bulb/latest/src/bulb/dither/presets.rs.html ; https://www.pixilart.com/palettes/gameboy-639 ; https://colorswall.com/palette/325349",
    },
    "gameboy_bgb": {
        "label": "Game Boy (bgb)",
        "label_zh": "Game Boy（bgb 模拟器）",
        "colors": ("#081820", "#346856", "#88c070", "#e0f8d0"),
        "note": "The bgb emulator's default 4-colour palette. A different palette from the DMG green above; both are commonly just called 'Game Boy'.",
        "note_zh": "bgb 模拟器的默认 4 色。与上面的 DMG 绿是不同的调色板，两者通常都被叫做「Game Boy」。",
        "verified": False,  # single source
        "weak_at": (8, 16),
        "source": "https://lospec.com/palette-list/nintendo-gameboy-bgb (single source; the DMG variant above is the better-attested 4-colour choice)",
    },
    "slso8": {
        "label": "SLSO8",
        "label_zh": "SLSO8",
        "colors": ("#0d2b45", "#203c56", "#544e68", "#8d697a", "#d08159", "#ffaa5e", "#ffd4a3", "#ffecd6"),
        "note": "A navy-to-cream 8-colour ramp by Luis Miguel Maldonado. Eight colours is tight for a portrait.",
        "note_zh": "Luis Miguel Maldonado 的深蓝到奶油色 8 级渐变。8 色对人像偏紧。",
        "verified": False,  # second source probably scraped lospec
        "weak_at": (8, 16),
        "source": "https://lospec.com/palette-list/slso8 (second source is a GitHub issue that appears to have scraped the same page)",
    },
    "sweetie16": {
        "label": "Sweetie 16",
        "label_zh": "Sweetie 16",
        "colors": (
            "#1a1c2c", "#5d275d", "#b13e53", "#ef7d57", "#ffcd75", "#a7f070",
            "#38b764", "#257179", "#29366f", "#3b5dc9", "#41a6f6", "#73eff7",
            "#f4f4f4", "#94b0c2", "#566c86", "#333c57",
        ),
        "note": "GrafxKid's 16-colour palette, widely used and well spread across hue and value.",
        "note_zh": "GrafxKid 的 16 色，使用广泛，色相和明度分布均匀。",
        "verified": False,  # original author post unreachable; both sources descend from lospec
        "source": "https://lospec.com/palette-list/sweetie-16 ; https://docs.fireflyzero.com/dev/graphics/ (the author's original post is no longer reachable)",
    },
    "db16": {
        "label": "DawnBringer 16",
        "label_zh": "DawnBringer 16",
        "colors": (
            "#140c1c", "#442434", "#30346d", "#4e4a4e", "#854c30", "#346524",
            "#d04648", "#757161", "#597dce", "#d27d2c", "#8595a1", "#6daa2c",
            "#d2aa99", "#6dc2ca", "#dad45e", "#deeed6",
        ),
        "note": "DawnBringer's 2012 classic. It has a documented history of one wrong value (the dark purple) being propagated by copies; #442434 is the corrected one.",
        "note_zh": "DawnBringer 2012 年的经典。它有一个已知的流传错误（暗紫色），#442434 是修正后的值。",
        "verified": True,
        "weak_at": (8, 16),
        "source": "https://lospec.com/palette-list/dawnbringer-16 ; http://pixeljoint.com/forum/forum_posts.asp?TID=12795 ; https://github.com/geoffb/dawnbringer-palettes",
    },
    "c64_pepto": {
        "label": "Commodore 64 (Pepto)",
        "label_zh": "Commodore 64（Pepto）",
        "colors": (
            "#000000", "#ffffff", "#68372b", "#70a4b2", "#6f3d86", "#588d43",
            "#352879", "#b8c76f", "#6f4f25", "#433900", "#9a6759", "#444444",
            "#6c6c6c", "#9ad284", "#6c5eb5", "#959595",
        ),
        "note": "Philip Timmermann's measured C64 palette. Contested in the wider world: some sites ship a different 16 under the same name with no credited author.",
        "note_zh": "Philip Timmermann 实测的 C64 调色板。业界有争议：有网站用同名发布了另一套 16 色且未署名作者。",
        "verified": True,
        "weak_at": (8, 16),
        "source": "https://patches.ffmpeg.org/doxygen/3.1/a64colors_8h_source.html ; https://docs.rs/bulb/latest/src/bulb/dither/presets.rs.html (both credit Pepto; lospec's 'commodore64' ships a different, unattributed table)",
    },
    "endesga32": {
        "label": "Endesga 32",
        "label_zh": "Endesga 32",
        "colors": (
            "#be4a2f", "#d77643", "#ead4aa", "#e4a672", "#b86f50", "#733e39",
            "#3e2731", "#a22633", "#e43b44", "#f77622", "#feae34", "#fee761",
            "#63c74d", "#3e8948", "#265c42", "#193c3e", "#124e89", "#0099db",
            "#2ce8f5", "#ffffff", "#c0cbdc", "#8b9bb4", "#5a6988", "#3a4466",
            "#262b44", "#181425", "#ff0044", "#68386c", "#b55088", "#f6757a",
            "#e8b796", "#c28569",
        ),
        "note": "ENDESGA's 32-colour palette, made for NYKRA. Carries real skin tones and pinks, so it keeps faces and blush closer to the source than a console palette can.",
        "note_zh": "ENDESGA 为 NYKRA 做的 32 色。含真正的肤色和粉色，比主机调色板更能保住脸部和腮红。",
        "verified": False,  # order confirmed only by two fetches of the same page
        "source": "https://lospec.com/palette-list/endesga-32 (canonical ORDER confirmed only by repeat fetches of this page; the gamercade_core crate ships a reshuffled list under the same name)",
    },
    "aap64": {
        "label": "AAP-64",
        "label_zh": "AAP-64",
        "colors": (
            "#060608", "#141013", "#3b1725", "#73172d", "#b4202a", "#df3e23",
            "#fa6a0a", "#f9a31b", "#ffd541", "#fffc40", "#d6f264", "#9cdb43",
            "#59c135", "#14a02e", "#1a7a3e", "#24523b", "#122020", "#143464",
            "#285cc4", "#249fde", "#20d6c7", "#a6fcdb", "#ffffff", "#fef3c0",
            "#fad6b8", "#f5a097", "#e86a73", "#bc4a9b", "#793a80", "#403353",
            "#242234", "#221c1a", "#322b28", "#71413b", "#bb7547", "#dba463",
            "#f4d29c", "#dae0ea", "#b3b9d1", "#8b93af", "#6d758d", "#4a5462",
            "#333941", "#422433", "#5b3138", "#8e5252", "#ba756a", "#e9b5a3",
            "#e3e6ff", "#b9bffb", "#849be4", "#588dbe", "#477d85", "#23674e",
            "#328464", "#5daf8d", "#92dcba", "#cdf7e2", "#e4d2aa", "#c7b08b",
            "#a08662", "#796755", "#5a4e44", "#423934",
        ),
        "note": "Adigun A. Polack's 64-colour palette. The widest option here, and the most faithful to a gradient-heavy source, but 64 slots on a 64-pixel canvas is one colour per pixel.",
        "note_zh": "Adigun A. Polack 的 64 色。这里最宽的选项，对渐变多的源图最保真——但在 64 像素的画布上 64 色等于一像素一色。",
        "verified": False,  # both checks were the same page
        "source": "https://lospec.com/palette-list/aap-64 (both verification passes were fetches of this same page)",
    },
}


def self_check() -> None:
    """Fail loudly at import if any preset is malformed.

    Catches syntax and count only -- see the module docstring.
    """
    for preset_id, preset in PALETTE_PRESETS.items():
        colors = preset["colors"]
        parsed = pixel_redraw.parse_palette(",".join(colors))
        if parsed is None or len(parsed) != len(colors):
            raise AssertionError(
                f"palette {preset_id!r}: parse_palette returned "
                f"{0 if parsed is None else len(parsed)} colours for {len(colors)} hex values"
            )
        if len(colors) > MAX_PALETTE_COLORS:
            raise AssertionError(
                f"palette {preset_id!r}: {len(colors)} colours exceeds the "
                f"{MAX_PALETTE_COLORS}-colour cap"
            )
        for required in ("label", "label_zh", "note", "note_zh", "source", "verified"):
            if required not in preset:
                raise AssertionError(f"palette {preset_id!r} is missing {required!r}")
    if DEFAULT_PRESET not in PALETTE_PRESETS:
        raise AssertionError(f"DEFAULT_PRESET {DEFAULT_PRESET!r} is not in PALETTE_PRESETS")


def as_meta() -> list[dict]:
    """The preset registry, in the shape the frontend renders it from.

    ``pixel_pipeline.web_meta()`` carries this to the page at boot, so the
    palette list the user picks from is the same data the renderer resolves
    against -- there is no second copy to drift.
    """
    meta = [
        {
            "id": preset_id,
            "label": preset["label"],
            "label_zh": preset["label_zh"],
            "count": len(preset["colors"]),
            "colors": list(preset["colors"]),
            "note": preset["note"],
            "note_zh": preset["note_zh"],
            "verified": preset["verified"],
            "source": preset["source"],
            "weak_at": list(preset.get("weak_at", ())),
        }
        for preset_id, preset in PALETTE_PRESETS.items()
    ]
    # Keep the browser's fixed-palette picker useful at a glance: fewer colours
    # first, while preserving registry order for equal-sized palettes.
    return sorted(meta, key=lambda item: item["count"])


def resolve(spec: object) -> tuple[str, tuple[tuple[int, int, int], ...]] | tuple[None, None]:
    """Turn a request's palette spec into (label, colors).

    ``spec`` is what the browser sent: ``{"preset": "pico8"}``,
    ``{"colors": ["#rrggbb", ...]}``, or ``None`` for the auto path.  Raises
    ``pixel_redraw.PaletteError`` with the core's own wording so the browser
    sees the same message the CLI would print.
    """
    if spec is None:
        return None, None
    if not isinstance(spec, dict):
        raise pixel_redraw.PaletteError(
            f"Palette must be an object with 'preset' or 'colors', not {type(spec).__name__}"
        )

    preset_id = spec.get("preset")
    if preset_id is not None:
        preset = PALETTE_PRESETS.get(str(preset_id))
        if preset is None:
            raise pixel_redraw.PaletteError(
                f"Unknown palette preset {preset_id!r}; known ids: "
                + ", ".join(PALETTE_PRESETS)
            )
        return str(preset_id), pixel_redraw.parse_palette(",".join(preset["colors"]))

    colors = spec.get("colors")
    if colors is None:
        raise pixel_redraw.PaletteError("Palette needs either 'preset' or 'colors'")
    if isinstance(colors, str):
        text = colors
    elif isinstance(colors, list):
        text = ",".join(str(item) for item in colors)
    else:
        raise pixel_redraw.PaletteError(
            f"'colors' must be a string or a list, not {type(colors).__name__}"
        )
    parsed = pixel_redraw.parse_palette(text)
    if parsed is None:
        return None, None
    if len(parsed) > MAX_PALETTE_COLORS:
        raise pixel_redraw.PaletteError(
            f"调色板最多 {MAX_PALETTE_COLORS} 色，当前 {len(parsed)} 色"
        )
    return "custom", parsed


self_check()
