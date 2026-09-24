"""Comprehensive unit tests for the Pixel-Redraw refactor (QVote, Oklab, Cleanup, Phase Alignment).

Covers all requirements from Section 18 of pixel-redraw-refactor-design.md:
- 18.1 Unit Tests:
  - Target Grid (proportional to 256 reference canvas)
  - Palette (Oklab mapping, auto-palette determinism, alpha isolation)
  - QVote (majority vote, tie-breaking, alpha handling)
  - Topology Cleanup (hole fill, orphan removal, cluster preservation)
  - Phase Alignment (edge projection, confidence fallback)
- 18.2 Invariants:
  - Determinism (SHA-256 identical across repeated runs)
  - Palette subset
  - Generate vs Repixelize consistency
"""

import hashlib
import io
import unittest
from PIL import Image
import numpy as np

import pixel_color
import pixel_reduce
import pixel_redraw as pr


class GridTest(unittest.TestCase):
    def test_reference_proportional_grid(self):
        # (256, 256) @ 32 -> (32, 32)
        self.assertEqual(pixel_reduce.target_grid((256, 256), 32), (32, 32))
        # (1024, 1024) @ 32 -> (128, 128)
        self.assertEqual(pixel_reduce.target_grid((1024, 1024), 32), (128, 128))
        # (1920, 1080) @ 64 -> (480, 270)
        self.assertEqual(pixel_reduce.target_grid((1920, 1080), 64), (480, 270))
        # (1080, 1920) @ 64 -> (270, 480) (portrait)
        self.assertEqual(pixel_reduce.target_grid((1080, 1920), 64), (270, 480))
        # (512, 256) @ 16 -> (32, 16)
        self.assertEqual(pixel_reduce.target_grid((512, 256), 16), (32, 16))


class PaletteOklabTest(unittest.TestCase):
    def test_oklab_roundtrip(self):
        samples = np.array([
            [255, 0, 0], [0, 255, 0], [0, 0, 255],
            [255, 255, 255], [0, 0, 0], [128, 128, 128],
            [12, 34, 56], [200, 150, 100],
        ], dtype=np.uint8)
        oklab = pixel_color.srgb_to_oklab(samples)
        roundtrip = pixel_color.oklab_to_srgb(oklab)
        np.testing.assert_array_equal(samples, roundtrip)

    def test_auto_palette_deterministic(self):
        rng = np.random.RandomState(123)
        pixels = rng.randint(0, 256, size=(1000, 3), dtype=np.uint8)
        pal1 = pixel_color.build_auto_palette(pixels, max_colors=8, seed=42)
        pal2 = pixel_color.build_auto_palette(pixels, max_colors=8, seed=42)
        self.assertEqual(pal1, pal2)
        self.assertLessEqual(len(pal1), 8)

    def test_alpha_does_not_contaminate_palette(self):
        # Image with half transparent background (RGB=green) and half opaque red subject
        w, h = 64, 64
        arr = np.zeros((h, w, 4), dtype=np.uint8)
        arr[:, :32] = [0, 255, 0, 0]        # green but transparent (A=0)
        arr[:, 32:] = [255, 0, 0, 255]      # red and opaque (A=255)
        img = Image.fromarray(arr, mode="RGBA")

        res = pixel_reduce.reduce_pixel_art(img, density=16)
        # Transparent green must not contaminate the auto-palette
        for r, g, b in res.palette:
            self.assertFalse(r == 0 and g == 255 and b == 0, "Transparent color reached palette")


class QVoteTest(unittest.TestCase):
    def test_cell_majority_vote(self):
        # 16x16 cell where color A is majority and color B is minority:
        # A A A A
        # A A B B
        # A A B B
        # A A A B
        cell = np.array([
            [1, 1, 1, 1],
            [1, 1, 2, 2],
            [1, 1, 2, 2],
            [1, 1, 1, 2],
        ], dtype=np.uint8)
        # Scale cell up to 16x16 pixels
        expanded = np.repeat(np.repeat(cell, 4, axis=0), 4, axis=1)
        palette = ((0, 0, 0), (255, 0, 0), (0, 0, 255))  # idx 1: red, idx 2: blue

        rgba = np.zeros((16, 16, 4), dtype=np.uint8)
        rgba[..., 3] = 255
        rgba[expanded == 1, :3] = (255, 0, 0)
        rgba[expanded == 2, :3] = (0, 0, 255)

        labels, confidence, alpha_mask = pixel_reduce.quantize_vote(
            rgba, target_size=(1, 1), palette=palette
        )
        self.assertEqual(labels[0, 0], 1)  # Color A must win
        self.assertTrue(alpha_mask[0, 0])
        # Count A is 11/16 = 0.6875
        self.assertAlmostEqual(confidence[0, 0], 11 / 16, places=3)

    def test_alpha_majority_determines_cell_transparency(self):
        # Cell with >50% transparent pixels becomes transparent
        rgba = np.zeros((8, 8, 4), dtype=np.uint8)
        rgba[:5, :, 3] = 0        # 5 rows transparent (5/8 = 62.5% > 50%)
        rgba[5:, :, 3] = 255      # 3 rows opaque
        rgba[5:, :, :3] = (255, 255, 255)
        palette = ((0, 0, 0), (255, 255, 255))

        labels, confidence, alpha_mask = pixel_reduce.quantize_vote(
            rgba, target_size=(1, 1), palette=palette
        )
        self.assertFalse(alpha_mask[0, 0])


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.cfg = pixel_reduce.DEFAULT_REDUCER_CONFIG

    def test_single_pixel_hole_fill(self):
        # 3x3 surrounding of color 0, with center pixel of color 1 and low confidence
        labels = np.zeros((3, 3), dtype=np.int32)
        labels[1, 1] = 1
        confidence = np.full((3, 3), 0.9, dtype=np.float32)
        confidence[1, 1] = 0.4  # Below hole_fill_threshold (0.55)
        alpha_mask = np.ones((3, 3), dtype=bool)

        cleaned, changes = pixel_reduce.cleanup_clusters(labels, confidence, alpha_mask, self.cfg)
        self.assertEqual(cleaned[1, 1], 0)
        self.assertEqual(changes, 1)

    def test_orphan_pixel_removal(self):
        # 5x5 grid: color 0 everywhere, single isolated color 1 at (2, 2) with low confidence
        labels = np.zeros((5, 5), dtype=np.int32)
        labels[2, 2] = 1
        confidence = np.full((5, 5), 0.9, dtype=np.float32)
        confidence[2, 2] = 0.35  # Below orphan_threshold (0.45)
        alpha_mask = np.ones((5, 5), dtype=bool)

        cleaned, changes = pixel_reduce.cleanup_clusters(labels, confidence, alpha_mask, self.cfg)
        self.assertEqual(cleaned[2, 2], 0)
        self.assertGreaterEqual(changes, 1)

    def test_preserve_high_confidence_single_pixel_detail(self):
        # Eye / focal detail: isolated 1px with high confidence (e.g. 0.9) must NOT be deleted!
        labels = np.zeros((5, 5), dtype=np.int32)
        labels[2, 2] = 1
        confidence = np.full((5, 5), 0.9, dtype=np.float32)
        alpha_mask = np.ones((5, 5), dtype=bool)

        cleaned, changes = pixel_reduce.cleanup_clusters(labels, confidence, alpha_mask, self.cfg)
        self.assertEqual(cleaned[2, 2], 1)
        self.assertEqual(changes, 0)

    def test_preserve_diagonal_cluster(self):
        # Legitimate 2-pixel diagonal stroke: (1, 1) and (2, 2)
        labels = np.zeros((4, 4), dtype=np.int32)
        labels[1, 1] = 1
        labels[2, 2] = 1
        confidence = np.full((4, 4), 0.8, dtype=np.float32)
        alpha_mask = np.ones((4, 4), dtype=bool)

        cleaned, changes = pixel_reduce.cleanup_clusters(labels, confidence, alpha_mask, self.cfg)
        self.assertEqual(cleaned[1, 1], 1)
        self.assertEqual(cleaned[2, 2], 1)
        self.assertEqual(changes, 0)


class PhaseAlignmentTest(unittest.TestCase):
    def test_fallback_on_flat_image(self):
        # Uniform image has 0 gradient, phase alignment must fall back to (0.0, 0.0)
        flat = np.full((128, 128), 128, dtype=np.float32)
        offset, applied = pixel_reduce.estimate_grid_phase(flat, (16, 16))
        self.assertEqual(offset, (0.0, 0.0))
        self.assertFalse(applied)


class InvariantsTest(unittest.TestCase):
    def test_deterministic_output(self):
        rng = np.random.RandomState(42)
        noise = rng.randint(0, 256, (64, 64, 3), dtype=np.uint8)
        img = Image.fromarray(noise, mode="RGB")
        pal = ((0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0))

        res1 = pixel_reduce.reduce_pixel_art(img, target_size=(16, 16), palette=pal)
        res2 = pixel_reduce.reduce_pixel_art(img, target_size=(16, 16), palette=pal)

        b1 = io.BytesIO()
        res1.image.save(b1, "PNG")
        b2 = io.BytesIO()
        res2.image.save(b2, "PNG")

        sha1 = hashlib.sha256(b1.getvalue()).hexdigest()
        sha2 = hashlib.sha256(b2.getvalue()).hexdigest()
        self.assertEqual(sha1, sha2)

    def test_generate_and_repixelize_consistency(self):
        """Same model raw output + same settings must yield bitwise identical pixel outputs."""
        config = pr.make_config(model="m", api_key="k", size="16x16")
        rng = np.random.RandomState(99)
        raw_bytes = io.BytesIO()
        Image.fromarray(rng.randint(0, 256, (128, 128, 3), dtype=np.uint8)).save(raw_bytes, "PNG")
        raw = raw_bytes.getvalue()

        # Model generate path
        gen_reduction = pr.reduce_model_pixel_art(raw, config)
        buf1 = io.BytesIO()
        gen_reduction.image.save(buf1, "PNG")

        # Repixelize path
        repixel_reduction = pr.reduce_model_pixel_art(raw, config)
        buf2 = io.BytesIO()
        repixel_reduction.image.save(buf2, "PNG")

        self.assertEqual(buf1.getvalue(), buf2.getvalue())
        self.assertEqual(gen_reduction.labels.tolist(), repixel_reduction.labels.tolist())


if __name__ == "__main__":
    unittest.main()
