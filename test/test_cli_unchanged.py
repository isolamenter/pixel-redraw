#!/usr/bin/env python3
"""Regression gate: the web work must not change what the CLI produces.

The baselines in test/cli_baseline/pre_*/ were captured from the UNPATCHED
pixel_redraw.py, before the three additive edits the web layer needed.  Two of
those edits (typed exceptions, non-JSON body in the message) cannot affect
pixels.  The third -- scaling the pre-quantize colour count to the canvas --
is deliberately a no-op at 32x32 and 64x64 and deliberately different at 8x8
and 16x16, so this test asserts byte-identity where it must hold and only
structure where the change was intended.

Run:  python3 test/test_cli_unchanged.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "test" / "cli_baseline"
SOURCE = ROOT / "test" / "20260921-171120.ai.png"
OUT = Path("/tmp/pixel-cli-regression")

sys.path.insert(0, str(ROOT))
from PIL import Image  # noqa: E402

import pixel_redraw as pr  # noqa: E402


def run_cli(size: int, out_dir: Path) -> int:
    env = dict(os.environ, PIXEL_PALETTE="auto")
    result = subprocess.run(
        [sys.executable, str(ROOT / "pixel_redraw.py"), str(SOURCE),
         "--pixelize-only", "--size", f"{size}x{size}", "--scale", "8",
         "--out-dir", str(out_dir)],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"CLI failed at {size}x{size}:\n{result.stderr}")
    return result.returncode


class CliUnchanged(unittest.TestCase):
    def test_pixels_identical_where_the_change_is_a_noop(self):
        """32x32 and 64x64 are bit-identical to the pre-patch CLI.

        64x64 is the size whose output was already approved, so this is the
        assertion that protects the look the user signed off on.
        """
        for size in (32, 64):
            with self.subTest(size=size):
                out_dir = OUT / f"post_{size}"
                run_cli(size, out_dir)
                for name in ("pixel.png", "pixel_x8.png", "ai.png"):
                    before = (BASELINE / f"pre_{size}" / f"20260921-171120.ai.{name}").read_bytes()
                    after = (out_dir / f"20260921-171120.ai.{name}").read_bytes()
                    self.assertEqual(
                        before, after,
                        f"{size}x{size} {name} changed; the pre-quantize edit was "
                        f"supposed to be a no-op at and above 32x32",
                    )

    def test_small_sizes_change_by_design_but_stay_structurally_correct(self):
        """8x8 and 16x16 are EXPECTED to differ; they must still be right.

        Asserting the intended difference rather than byte-identity documents
        that the divergence is deliberate.
        """
        for size in (8, 16):
            with self.subTest(size=size):
                out_dir = OUT / f"post_{size}"
                run_cli(size, out_dir)
                before = (BASELINE / f"pre_{size}" / "20260921-171120.ai.pixel.png").read_bytes()
                after = (out_dir / "20260921-171120.ai.pixel.png").read_bytes()
                self.assertNotEqual(before, after, f"{size}x{size} did not change at all")
                with Image.open(out_dir / "20260921-171120.ai.pixel.png") as image:
                    self.assertEqual(image.size, (size, size))

    def test_error_messages_are_byte_identical(self):
        """The typed exceptions must not have reworded anything."""
        cases = [
            (pr.parse_size, "abc", "Invalid size 'abc'; use N or WIDTHxHEIGHT, e.g. 64x64"),
            (pr.parse_size, "9999", "Pixel canvas dimensions must be between 1 and 1024"),
            (pr.parse_palette, "12345", "Invalid palette color '12345'; use #RRGGBB values"),
        ]
        for func, arg, expected in cases:
            with self.subTest(func=func.__name__, arg=arg):
                with self.assertRaises(ValueError) as ctx:
                    func(arg)
                self.assertEqual(str(ctx.exception), expected)

    def test_every_typed_error_is_caught_by_mains_except_tuple(self):
        """main() catches (OSError, ValueError, RuntimeError); all must fit."""
        import inspect

        found = 0
        for _, obj in inspect.getmembers(pr, inspect.isclass):
            if obj is pr.PixelError or (issubclass(obj, pr.PixelError) and obj.__module__ == pr.__name__):
                found += 1
                self.assertTrue(
                    issubclass(obj, (OSError, ValueError, RuntimeError)),
                    f"{obj.__name__} escapes main()'s except tuple, so the CLI would "
                    f"print a traceback instead of 'Error: ...'",
                )
        self.assertGreaterEqual(found, 9, "expected the full typed-exception hierarchy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
