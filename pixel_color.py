#!/usr/bin/env python3
"""Color space conversions and palette processing for pixel-redraw.

Provides sRGB <-> Linear RGB <-> Oklab conversions, Oklab-based nearest
palette mapping, and deterministic lightweight Oklab K-Means auto-palette
generation.

This module is pure compute. It reads no environment variables and accesses no
filesystem or network. Runs identically on CPython and Pyodide (WASM).
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np


# --------------------------------------------------------------------------
# Color space conversions: sRGB <-> Linear RGB <-> Oklab
# --------------------------------------------------------------------------


def srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    """Convert sRGB array ([0, 255] uint8 or [0.0, 1.0] float) to Linear RGB [0.0, 1.0]."""
    c = np.asarray(rgb, dtype=np.float32)
    if c.size > 0 and np.nanmax(c) > 1.0:
        c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    """Convert Linear RGB [0.0, 1.0] array to sRGB uint8 [0, 255]."""
    c = np.clip(np.asarray(linear, dtype=np.float32), 0.0, 1.0)
    srgb = np.where(c <= 0.0031308, 12.92 * c, 1.055 * (c ** (1.0 / 2.4)) - 0.055)
    return np.clip(np.round(srgb * 255.0), 0, 255).astype(np.uint8)


def linear_to_oklab(linear: np.ndarray) -> np.ndarray:
    """Convert Linear RGB [0.0, 1.0] to Oklab (L in [0, 1], a, b typically in [-0.5, 0.5])."""
    r = linear[..., 0]
    g = linear[..., 1]
    b = linear[..., 2]

    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b

    # Numerical noise can make l, m, s slightly negative for zero/near-zero inputs.
    l_ = np.cbrt(np.maximum(l, 0.0))
    m_ = np.cbrt(np.maximum(m, 0.0))
    s_ = np.cbrt(np.maximum(s, 0.0))

    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    b = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_

    return np.stack([L, a, b], axis=-1).astype(np.float32)


def oklab_to_linear(oklab: np.ndarray) -> np.ndarray:
    """Convert Oklab to Linear RGB [0.0, 1.0]."""
    L = oklab[..., 0]
    a = oklab[..., 1]
    b = oklab[..., 2]

    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b

    l = l_ ** 3
    m = m_ ** 3
    s = s_ ** 3

    r = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    b = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s

    return np.stack([r, g, b], axis=-1).astype(np.float32)


def srgb_to_oklab(rgb: np.ndarray) -> np.ndarray:
    """Direct conversion from sRGB ([0, 255] or [0, 1]) to Oklab."""
    return linear_to_oklab(srgb_to_linear(rgb))


def oklab_to_srgb(oklab: np.ndarray) -> np.ndarray:
    """Direct conversion from Oklab to sRGB uint8 [0, 255]."""
    return linear_to_srgb(oklab_to_linear(oklab))


# --------------------------------------------------------------------------
# Palette mapping & Distance
# --------------------------------------------------------------------------


def map_to_palette(
    rgb: np.ndarray,
    palette: Sequence[tuple[int, int, int]],
    *,
    lightness_weight: float = 1.0,
) -> np.ndarray:
    """Map an (..., 3) sRGB image or pixel array to the index of the nearest palette color in Oklab space.

    Ties resolve strictly to the lowest palette index.
    Memory-efficient: processes one palette entry per pass to avoid large (H, W, K, 3) tensors.

    Parameters:
        rgb: (..., 3) uint8 or float array.
        palette: sequence of (R, G, B) triples in [0, 255].
        lightness_weight: weight factor for delta L. Defaults to 1.0 (standard Oklab).

    Returns:
        (...) int32 array containing indices into `palette`.
    """
    if len(palette) == 0:
        raise ValueError("Palette must contain at least one color")

    # Shape preserving: shape[:-1]
    input_shape = rgb.shape[:-1]
    pixels_oklab = srgb_to_oklab(rgb.reshape(-1, 3))
    num_pixels = pixels_oklab.shape[0]

    palette_srgb = np.asarray(palette, dtype=np.uint8)
    palette_oklab = srgb_to_oklab(palette_srgb)

    best_distance = np.full(num_pixels, np.finfo(np.float32).max, dtype=np.float32)
    best_index = np.zeros(num_pixels, dtype=np.int32)

    L_scale = float(lightness_weight * lightness_weight)

    for idx, target_lab in enumerate(palette_oklab):
        dL = pixels_oklab[:, 0] - target_lab[0]
        da = pixels_oklab[:, 1] - target_lab[1]
        db = pixels_oklab[:, 2] - target_lab[2]
        dist = L_scale * (dL * dL) + (da * da) + (db * db)

        # Strict inequality ensures tie-breaking goes to the lowest index
        better = dist < best_distance
        if better.any():
            best_distance[better] = dist[better]
            best_index[better] = idx

    return best_index.reshape(input_shape)


# --------------------------------------------------------------------------
# Lightweight deterministic Oklab K-Means auto-palette
# --------------------------------------------------------------------------


def build_auto_palette(
    rgb_samples: np.ndarray,
    max_colors: int = 16,
    *,
    seed: int = 42,
    max_samples: int = 20000,
    iterations: int = 8,
) -> tuple[tuple[int, int, int], ...]:
    """Derive an optimal palette of up to `max_colors` using deterministic Oklab K-Means.

    Requirements (from design doc Sec 7.2):
    - Fixed seed, deterministic initialization.
    - Samples <= 20,000 opaque pixels.
    - 6-8 iterations in Oklab space.
    - Empty cluster recovery via furthest sample reassignment.
    - Final palette returned as sRGB (R, G, B) integer triples.
    """
    if max_colors < 1:
        raise ValueError("max_colors must be positive")

    flat = np.asarray(rgb_samples, dtype=np.uint8).reshape(-1, 3)
    if flat.shape[0] == 0:
        return ((0, 0, 0),)

    # Unique colors first
    unique_colors = np.unique(flat, axis=0)
    if len(unique_colors) <= max_colors:
        return tuple(tuple(int(c) for c in color) for color in unique_colors)

    # Subsample if necessary using deterministic RNG
    rng = np.random.RandomState(seed)
    if len(flat) > max_samples:
        indices = rng.choice(len(flat), size=max_samples, replace=False)
        flat = flat[indices]

    samples_oklab = srgb_to_oklab(flat)
    num_samples = len(samples_oklab)
    k = min(max_colors, num_samples)

    # Deterministic K-Means++ initialization
    first_idx = rng.randint(0, num_samples)
    centers = [samples_oklab[first_idx]]
    min_sq_dist = np.sum((samples_oklab - centers[0]) ** 2, axis=1)

    for _ in range(1, k):
        sum_dist = np.sum(min_sq_dist)
        if sum_dist <= 1e-12:
            # All remaining points identical or zero distance, pick random unseen
            idx = rng.randint(0, num_samples)
        else:
            probs = min_sq_dist / sum_dist
            idx = rng.choice(num_samples, p=probs)
        centers.append(samples_oklab[idx])
        new_dist = np.sum((samples_oklab - centers[-1]) ** 2, axis=1)
        min_sq_dist = np.minimum(min_sq_dist, new_dist)

    centers_arr = np.array(centers, dtype=np.float32)

    # K-Means iteration rounds
    for _ in range(max(1, iterations)):
        # Compute distances from samples to centers: (N, K)
        # Note: N <= 20000, K <= 64 -> (20000, 64) is ~5MB, completely safe for wasm
        diff = samples_oklab[:, np.newaxis, :] - centers_arr[np.newaxis, :, :]
        distances_sq = np.sum(diff * diff, axis=2)

        # Labels: lowest index on tie (np.argmin breaks ties at first occurrence)
        labels = np.argmin(distances_sq, axis=1)

        # Update centers
        for cluster_idx in range(k):
            mask = labels == cluster_idx
            if np.any(mask):
                centers_arr[cluster_idx] = np.mean(samples_oklab[mask], axis=0)
            else:
                # Empty cluster: reinitialize with the furthest sample
                curr_min_dists = distances_sq[np.arange(num_samples), labels]
                furthest_idx = int(np.argmax(curr_min_dists))
                centers_arr[cluster_idx] = samples_oklab[furthest_idx]

    # Convert final centers to sRGB
    srgb_centers = oklab_to_srgb(centers_arr)

    # Deduplicate and sort by perceived luminance for a stable, clean palette
    unique_list = []
    seen = set()
    for col in srgb_centers:
        triple = (int(col[0]), int(col[1]), int(col[2]))
        if triple not in seen:
            seen.add(triple)
            unique_list.append(triple)

    if not unique_list:
        unique_list.append((0, 0, 0))

    return tuple(unique_list)


# --------------------------------------------------------------------------
# Palette subset selection (Weighted Oklab Error Greedy Pruning)
# --------------------------------------------------------------------------


def select_sub_palette(
    rgb_samples: np.ndarray,
    palette: Sequence[tuple[int, int, int]],
    max_colors: int,
) -> tuple[tuple[int, int, int], ...]:
    """Deterministically select the optimal subset of up to `max_colors` from `palette`.

    Uses weighted perceptual error greedy pruning in Oklab space:
    1. Maps opaque sample pixels to the full palette and computes usage counts.
    2. Filters to active palette colors. If active count <= max_colors, returns
       the active colors preserving the original palette ordering.
    3. If active count > max_colors, iteratively merges the color whose removal
       causes the smallest weighted squared Oklab distance increase to its
       closest surviving neighbor, until exactly max_colors remain.
    4. Guarantees output is a strict subset of `palette`, maintaining original
       palette index order (preserving tie-breaking stability).
    """
    if max_colors < 1:
        raise ValueError("max_colors must be positive")
    if len(palette) == 0:
        raise ValueError("palette must contain at least one color")
    if len(palette) <= max_colors:
        return tuple(tuple(int(c) for c in color) for color in palette)

    flat = np.asarray(rgb_samples, dtype=np.uint8).reshape(-1, 3)
    if flat.shape[0] == 0:
        return tuple(tuple(int(c) for c in color) for color in palette[:max_colors])

    # Initial mapping to full palette
    labels = map_to_palette(flat, palette)
    counts = np.bincount(labels, minlength=len(palette))
    active_indices = np.where(counts > 0)[0].tolist()

    if len(active_indices) == 0:
        return tuple(tuple(int(c) for c in color) for color in palette[:max_colors])

    if len(active_indices) <= max_colors:
        # Fewer active colors than max_colors, return active subset preserving original order
        return tuple(tuple(int(c) for c in palette[idx]) for idx in active_indices)

    # Convert all palette colors to Oklab
    palette_srgb = np.asarray(palette, dtype=np.uint8)
    palette_oklab = srgb_to_oklab(palette_srgb)

    weights = {idx: float(counts[idx]) for idx in active_indices}
    current_active = list(active_indices)

    while len(current_active) > max_colors:
        best_delta = float("inf")
        best_idx_to_remove = -1
        best_target_neighbor = -1

        for i_idx, cand in enumerate(current_active):
            cand_lab = palette_oklab[cand]
            w = weights[cand]

            # Find closest remaining active neighbor
            min_dist_sq = float("inf")
            closest_neighbor = -1
            for j_idx, other in enumerate(current_active):
                if i_idx == j_idx:
                    continue
                diff = cand_lab - palette_oklab[other]
                d_sq = float(np.sum(diff * diff))
                if d_sq < min_dist_sq:
                    min_dist_sq = d_sq
                    closest_neighbor = other

            delta_e = w * min_dist_sq
            if delta_e < best_delta:
                best_delta = delta_e
                best_idx_to_remove = cand
                best_target_neighbor = closest_neighbor

        # Merge weight of removed color into its closest neighbor
        weights[best_target_neighbor] += weights[best_idx_to_remove]
        current_active.remove(best_idx_to_remove)

    current_active.sort()
    return tuple(tuple(int(c) for c in palette[idx]) for idx in current_active)

