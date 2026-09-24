#!/usr/bin/env python3
"""Pixel reduction pipeline for pixel-redraw.

Implements target grid calculation, grid phase alignment, quantize-then-vote
(QVote), and confidence-aware cluster cleanup.

This module is pure compute. It reads no environment variables and accesses no
filesystem or network. Runs identically on CPython and Pyodide (WASM).
"""

from __future__ import annotations

from dataclasses import dataclass
import io
from typing import Any, Sequence

import numpy as np
from PIL import Image

import pixel_color


# --------------------------------------------------------------------------
# Developer Configuration (Centralized, No Magic Numbers)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReducerConfig:
    """Centralized configuration for pixel reduction.

    Developers can adjust these default thresholds here or pass a custom
    instance during tests or ablation studies.
    """

    # Top-level feature toggles
    align_grid: bool = True
    cleanup: bool = True

    # Grid Phase Alignment
    phase_search_range: float = 0.5          # Search in [-0.5, +0.5] cell
    alignment_confidence_min: float = 0.15    # Minimum relative peak edge contrast
    alignment_min_contrast: float = 8.0       # Minimum absolute edge contrast threshold

    # Cluster Topology Cleanup
    hole_fill_threshold: float = 0.55         # Max vote confidence to fill single-pixel hole
    orphan_threshold: float = 0.45            # Max vote confidence to remove orphan pixel
    orphan_dominant_ratio: float = 0.75       # Neighbor dominance required (>= 6/8)
    cleanup_passes: int = 2                   # Max topology cleanup iterations

    # Auto Palette (Oklab K-Means)
    kmeans_max_samples: int = 20000
    kmeans_iterations: int = 8
    kmeans_seed: int = 42


DEFAULT_REDUCER_CONFIG = ReducerConfig()


@dataclass
class ReductionResult:
    """Artifacts and diagnostic metrics produced by reduce_pixel_art."""

    image: Image.Image
    labels: np.ndarray
    confidence: np.ndarray
    palette: tuple[tuple[int, int, int], ...]
    grid_offset: tuple[float, float]
    cleanup_changes: int
    grid_alignment_applied: bool
    mean_vote_confidence: float
    low_confidence_cells: int


# --------------------------------------------------------------------------
# Section 5: Target Grid Calculation
# --------------------------------------------------------------------------


def target_grid(source_size: tuple[int, int], density: int, reference: int = 256) -> tuple[int, int]:
    """Calculate target logical canvas dimensions based on a reference canvas.

    A 256x256 source at density 64 becomes 64x64. A 1024x1024 source at the
    same density becomes 256x256. Width and height are scaled independently by
    the same reference, so non-square images keep their aspect ratio.
    """
    width, height = source_size
    if width < 1 or height < 1 or density < 1 or reference < 1:
        raise ValueError("Dimensions, density and reference must be positive")
    return (
        max(1, int(round(width * density / reference))),
        max(1, int(round(height * density / reference))),
    )


# --------------------------------------------------------------------------
# Section 6: Grid Phase Alignment
# --------------------------------------------------------------------------


def estimate_grid_phase(
    gray: np.ndarray,
    target_size: tuple[int, int],
    config: ReducerConfig = DEFAULT_REDUCER_CONFIG,
) -> tuple[tuple[float, float], bool]:
    """Estimate sub-cell phase offset (X, Y) relative to proposed logical cells.

    Uses image gradient projection near proposed cell boundaries.
    Returns:
        (offset_x_rel, offset_y_rel), applied_boolean
        Offsets are in [-0.5, 0.5] fraction of cell width/height.
        If confidence is below threshold, offset reverts to (0.0, 0.0).
    """
    height, width = gray.shape[:2]
    target_w, target_h = target_size

    cell_w = width / target_w
    cell_h = height / target_h

    # If cells are already ~1px, phase alignment is meaningless
    if cell_w <= 1.5 or cell_h <= 1.5:
        return (0.0, 0.0), False

    # Compute edge gradients
    # Horizontal edge gradient (for vertical cell boundaries)
    gx = np.abs(gray[:, 1:] - gray[:, :-1])
    col_edge = np.mean(gx, axis=0)  # Length: width - 1

    # Vertical edge gradient (for horizontal cell boundaries)
    gy = np.abs(gray[1:, :] - gray[:-1, :])
    row_edge = np.mean(gy, axis=1)  # Length: height - 1

    def search_axis(edge_profile: np.ndarray, cell_dim: float, num_cells: int) -> float:
        total_len = len(edge_profile)
        # Search discrete offsets in [-0.5 * cell, +0.5 * cell]
        max_shift = 0.5 * cell_dim
        # Sample ~11 candidate offsets
        candidates = np.linspace(-max_shift, max_shift, 11)
        scores = []

        for offset in candidates:
            score = 0.0
            valid_boundaries = 0
            for k in range(1, num_cells):
                boundary = int(round(k * cell_dim + offset))
                if 0 <= boundary < total_len:
                    score += float(edge_profile[boundary])
                    valid_boundaries += 1
            scores.append(score / max(1, valid_boundaries))

        scores = np.array(scores, dtype=np.float32)
        mean_score = float(np.mean(scores))
        peak_idx = int(np.argmax(scores))
        peak_score = float(scores[peak_idx])

        # Confidence: relative height of peak over background mean
        confidence = (peak_score - mean_score) / (mean_score + 1e-5)

        if confidence >= config.alignment_confidence_min and peak_score >= config.alignment_min_contrast:
            return float(candidates[peak_idx] / cell_dim)
        return 0.0

    off_x_rel = search_axis(col_edge, cell_w, target_w)
    off_y_rel = search_axis(row_edge, cell_h, target_h)

    applied = (off_x_rel != 0.0 or off_y_rel != 0.0)
    return (round(off_x_rel, 4), round(off_y_rel, 4)), applied


# --------------------------------------------------------------------------
# Section 8 & 9: Quantize -> Vote & Tie-break
# --------------------------------------------------------------------------


def quantize_vote(
    source_rgba: np.ndarray,
    target_size: tuple[int, int],
    palette: Sequence[tuple[int, int, int]],
    grid_offset: tuple[float, float] = (0.0, 0.0),
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Execute majority vote on palette labels within each logical cell.

    Returns:
        labels: (target_h, target_w) int32 palette indices
        confidence: (target_h, target_w) float32 vote confidence [0.0, 1.0]
        alpha_mask: (target_h, target_w) bool (True = opaque, False = transparent)
    """
    src_h, src_w = source_rgba.shape[:2]
    target_w, target_h = target_size
    num_colors = len(palette)

    rgb = source_rgba[..., :3]
    alpha = source_rgba[..., 3]

    # Step 1: High-res palette labeling in Oklab space
    palette_label_map = pixel_color.map_to_palette(rgb, palette)

    labels = np.zeros((target_h, target_w), dtype=np.int32)
    confidence = np.zeros((target_h, target_w), dtype=np.float32)
    alpha_mask = np.zeros((target_h, target_w), dtype=bool)

    cell_w = src_w / target_w
    cell_h = src_h / target_h

    off_x_px = grid_offset[0] * cell_w
    off_y_px = grid_offset[1] * cell_h

    for cy in range(target_h):
        y0 = max(0, min(src_h - 1, int(round(cy * cell_h + off_y_px))))
        y1 = max(y0 + 1, min(src_h, int(round((cy + 1) * cell_h + off_y_px))))

        for cx in range(target_w):
            x0 = max(0, min(src_w - 1, int(round(cx * cell_w + off_x_px))))
            x1 = max(x0 + 1, min(src_w, int(round((cx + 1) * cell_w + off_x_px))))

            cell_alpha = alpha[y0:y1, x0:x1]
            opaque_sub = cell_alpha >= 128
            opaque_count = int(np.count_nonzero(opaque_sub))
            total_sub = cell_alpha.size

            # Section 12: Alpha handling
            if opaque_count < total_sub * 0.5:
                # Transparent cell
                alpha_mask[cy, cx] = False
                labels[cy, cx] = 0
                confidence[cy, cx] = 1.0
                continue

            alpha_mask[cy, cx] = True
            cell_palette_labels = palette_label_map[y0:y1, x0:x1][opaque_sub]

            if cell_palette_labels.size == 0:
                labels[cy, cx] = 0
                confidence[cy, cx] = 0.0
                continue

            counts = np.bincount(cell_palette_labels, minlength=num_colors)
            max_count = int(np.max(counts))
            winner_candidates = np.where(counts == max_count)[0]

            if len(winner_candidates) == 1:
                winner = int(winner_candidates[0])
            else:
                # Section 9: Tie-break order
                # 1. cell center pixel's label
                center_x = max(0, min(src_w - 1, int(round((cx + 0.5) * cell_w + off_x_px))))
                center_y = max(0, min(src_h - 1, int(round((cy + 0.5) * cell_h + off_y_px))))
                center_label = int(palette_label_map[center_y, center_x])

                if center_label in winner_candidates:
                    winner = center_label
                else:
                    # 2. lowest palette index
                    winner = int(np.min(winner_candidates))

            labels[cy, cx] = winner
            confidence[cy, cx] = float(max_count / cell_palette_labels.size)

    return labels, confidence, alpha_mask


# --------------------------------------------------------------------------
# Section 11: Confidence-Aware Cluster Cleanup
# --------------------------------------------------------------------------


def cleanup_clusters(
    labels: np.ndarray,
    confidence: np.ndarray,
    alpha_mask: np.ndarray,
    config: ReducerConfig = DEFAULT_REDUCER_CONFIG,
) -> tuple[np.ndarray, int]:
    """Clean up low-confidence single-pixel holes and orphan pixels on the palette index grid.

    Returns:
        (cleaned_labels, total_changes_count)
    """
    h, w = labels.shape
    current = labels.copy()
    total_changes = 0

    for _ in range(max(1, config.cleanup_passes)):
        pass_changes = 0
        working = current.copy()

        for y in range(h):
            for x in range(w):
                if not alpha_mask[y, x]:
                    continue

                curr_label = current[y, x]
                curr_conf = confidence[y, x]

                # Gather 8-neighbors
                neighbors_8 = []
                neighbors_4 = []
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and alpha_mask[ny, nx]:
                        neighbors_4.append(current[ny, nx])
                        neighbors_8.append(current[ny, nx])

                for dy, dx in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and alpha_mask[ny, nx]:
                        neighbors_8.append(current[ny, nx])

                if not neighbors_8:
                    continue

                # Rule A: Single-pixel Hole Fill (Sec 11.1)
                # If 8-neighbors have >= 6 of a single label A, and conf < hole_fill_threshold
                counts_8 = np.bincount(neighbors_8)
                top_8_label = int(np.argmax(counts_8))
                top_8_count = int(counts_8[top_8_label])

                if top_8_label != curr_label and top_8_count >= 6 and curr_conf < config.hole_fill_threshold:
                    working[y, x] = top_8_label
                    pass_changes += 1
                    continue

                # Rule B & C: Orphan Pixel Removal (Sec 11.2 & 11.3)
                # No same-color 4-neighbor, <= 1 same-color diagonal, low confidence, dominant surround
                same_4 = sum(1 for n in neighbors_4 if n == curr_label)
                if same_4 == 0 and curr_conf < config.orphan_threshold:
                    # Dominant neighborhood check
                    if top_8_count >= int(round(config.orphan_dominant_ratio * len(neighbors_8))):
                        working[y, x] = top_8_label
                        pass_changes += 1
                        continue

        current = working
        total_changes += pass_changes
        if pass_changes == 0:
            break

    return current, total_changes


# --------------------------------------------------------------------------
# Section 14: Unified Reduction API
# --------------------------------------------------------------------------


def reduce_pixel_art(
    source: Any,
    *,
    target_size: tuple[int, int] | None = None,
    density: int | None = None,
    palette: Sequence[tuple[int, int, int]] | None = None,
    max_colors: int | None = 16,
    config: ReducerConfig = DEFAULT_REDUCER_CONFIG,
) -> ReductionResult:
    """Unified reducer for model pixel art and repixelize passes.

    Follows the full pipeline:
    1. Grid resolution (longest-edge density).
    2. Optional grid phase alignment.
    3. Palette determination (explicit or Oklab K-Means).
    4. QVote (quantize high-res first, majority vote per cell).
    5. Topology cluster cleanup.
    """
    # Normalize input to RGBA numpy array
    if isinstance(source, bytes):
        pil_img = Image.open(io.BytesIO(source)).convert("RGBA")
    elif isinstance(source, Image.Image):
        pil_img = source.convert("RGBA")
    elif isinstance(source, np.ndarray):
        pil_img = Image.fromarray(source).convert("RGBA")
    else:
        raise TypeError(f"Unsupported source type: {type(source)}")

    src_w, src_h = pil_img.size
    rgba_arr = np.asarray(pil_img, dtype=np.uint8)

    # 1. Resolve target grid
    if target_size is not None:
        resolved_size = target_size
    elif density is not None:
        resolved_size = target_grid((src_w, src_h), density)
    else:
        raise ValueError("Either target_size or density must be provided")

    # 2. Palette construction
    opaque_mask = rgba_arr[..., 3] >= 128
    opaque_pixels = rgba_arr[..., :3][opaque_mask]
    if palette:
        resolved_palette = tuple(tuple(int(c) for c in col) for col in palette)
        if isinstance(max_colors, int) and 1 <= max_colors < len(resolved_palette):
            resolved_palette = pixel_color.select_sub_palette(
                opaque_pixels, resolved_palette, max_colors
            )
    else:
        resolved_palette = pixel_color.build_auto_palette(
            opaque_pixels,
            max_colors=max_colors if isinstance(max_colors, int) and max_colors > 0 else 16,
            seed=config.kmeans_seed,
            max_samples=config.kmeans_max_samples,
            iterations=config.kmeans_iterations,
        )

    # 3. Optional Grid Phase Alignment
    if config.align_grid:
        gray = np.dot(rgba_arr[..., :3].astype(np.float32), [0.299, 0.587, 0.114])
        grid_offset, alignment_applied = estimate_grid_phase(gray, resolved_size, config)
    else:
        grid_offset, alignment_applied = (0.0, 0.0), False

    # 4. Quantize -> Vote
    labels, confidence, alpha_mask = quantize_vote(
        rgba_arr, resolved_size, resolved_palette, grid_offset=grid_offset
    )

    # 5. Cluster Cleanup
    cleanup_changes = 0
    if config.cleanup:
        labels, cleanup_changes = cleanup_clusters(labels, confidence, alpha_mask, config)

    # 6. Reconstruct output PIL Image
    palette_lookup = np.asarray(resolved_palette, dtype=np.uint8)
    out_rgb = palette_lookup[labels]
    out_alpha = np.where(alpha_mask, 255, 0).astype(np.uint8)

    out_rgba = np.dstack([out_rgb, out_alpha])
    result_image = Image.fromarray(out_rgba, mode="RGBA")

    # Metrics
    mean_conf = float(np.mean(confidence)) if confidence.size > 0 else 1.0
    low_conf_cells = int(np.count_nonzero(confidence < 0.5))

    return ReductionResult(
        image=result_image,
        labels=labels,
        confidence=confidence,
        palette=resolved_palette,
        grid_offset=grid_offset,
        cleanup_changes=cleanup_changes,
        grid_alignment_applied=alignment_applied,
        mean_vote_confidence=round(mean_conf, 4),
        low_confidence_cells=low_conf_cells,
    )


def reduce_photo(
    source: Any,
    *,
    target_size: tuple[int, int] | None = None,
    density: int | None = None,
    palette: Sequence[tuple[int, int, int]] | None = None,
    max_colors: int | None = 16,
    config: ReducerConfig = DEFAULT_REDUCER_CONFIG,
) -> ReductionResult:
    """Downsampling & reduction for natural user photos (Local Pixelize-Only).

    Uses area downsampling (BOX) followed by Oklab palette mapping, without
    enforcing AI cluster voting or topological jaggy cleanup.
    """
    if isinstance(source, bytes):
        pil_img = Image.open(io.BytesIO(source)).convert("RGBA")
    elif isinstance(source, Image.Image):
        pil_img = source.convert("RGBA")
    else:
        raise TypeError(f"Unsupported source type: {type(source)}")

    src_w, src_h = pil_img.size
    if target_size is not None:
        resolved_size = target_size
    elif density is not None:
        resolved_size = target_grid((src_w, src_h), density)
    else:
        raise ValueError("Either target_size or density must be provided")

    # Downsample via BOX (area averaging)
    rgba_down = pil_img.resize(resolved_size, Image.Resampling.BOX)
    down_arr = np.asarray(rgba_down, dtype=np.uint8)
    rgb = down_arr[..., :3]
    alpha = down_arr[..., 3]
    alpha_mask = alpha >= 128

    opaque_pixels = rgb[alpha_mask]
    if palette:
        resolved_palette = tuple(tuple(int(c) for c in col) for col in palette)
        if isinstance(max_colors, int) and 1 <= max_colors < len(resolved_palette):
            resolved_palette = pixel_color.select_sub_palette(
                opaque_pixels, resolved_palette, max_colors
            )
    else:
        resolved_palette = pixel_color.build_auto_palette(
            opaque_pixels,
            max_colors=max_colors if isinstance(max_colors, int) and max_colors > 0 else 16,
            seed=config.kmeans_seed,
            max_samples=config.kmeans_max_samples,
            iterations=config.kmeans_iterations,
        )

    labels = pixel_color.map_to_palette(rgb, resolved_palette)
    palette_lookup = np.asarray(resolved_palette, dtype=np.uint8)
    out_rgb = palette_lookup[labels]
    out_alpha = np.where(alpha_mask, 255, 0).astype(np.uint8)

    result_image = Image.fromarray(np.dstack([out_rgb, out_alpha]), mode="RGBA")
    dummy_conf = np.ones(resolved_size[::-1], dtype=np.float32)

    return ReductionResult(
        image=result_image,
        labels=labels,
        confidence=dummy_conf,
        palette=resolved_palette,
        grid_offset=(0.0, 0.0),
        cleanup_changes=0,
        grid_alignment_applied=False,
        mean_vote_confidence=1.0,
        low_confidence_cells=0,
    )
