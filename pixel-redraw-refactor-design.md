# Pixel-Redraw 像素化管线重构技术设计

**文档状态**：Draft  
**目标版本**：Pixel-Redraw Next  
**适用仓库**：`isolamenter/pixel-redraw`  
**范围**：模型输出后的像素化、调色板映射、网格对齐与像素簇清理  
**不涉及**：前端视觉重构、模型供应商切换、服务端架构调整

---

## 1. 背景

当前 `pixel-redraw` 的核心流程为：

```text
输入图
  ↓
Gemini 第一轮生成
  ↓
本地 resize + palette quantization
  ↓
Gemini 第二轮 refinement
  ↓
本地 resize + palette quantization
  ↓
输出
```

现有实现已经具备：

- 双轮模型生成；
- 固定 / 自动调色板；
- 无 dithering 的颜色量化；
- logical pixel 输出与 nearest-neighbor 预览；
- 本地 repixelize；
- palette subset 校验。

但实际生成结果仍存在：

- 轮廓锯齿节奏不稳定；
- 孤立像素和碎小色块较多；
- AI 抗锯齿与局部纹理被固化为脏边；
- 同一轮廓可能出现 1px 宽度抖动；
- 高分辨率输入会产生过高 logical resolution，降低“像素画抽象”程度；
- Generate 与 Repixelize 的采样路径目前不完全一致。

根因不是单纯的颜色数量，而是当前管线主要约束了 **颜色**，没有对 **像素网格与像素簇拓扑**形成硬约束。

---

## 2. 设计目标

### 2.1 必须实现

1. 模型输出必须稳定映射到明确 logical grid。
2. 每个 logical pixel 的颜色由区域信息决定，而不是单点采样或 RGB 均值。
3. 固定调色板输出必须严格属于指定 palette。
4. 自动调色板应使用感知颜色空间而不是裸 sRGB 欧氏距离。
5. 自动清除低置信度孤立像素、单像素孔洞和明显 jaggy。
6. 保留合法的单像素细节，例如眼睛、高光、饰品。
7. Generate、Refinement、Repixelize 必须复用同一个 reducer。
8. 算法保持确定性，可被单元测试和 golden image 测试覆盖。
9. 保持浏览器端 Pyodide + Pillow + NumPy 架构，不新增 OpenCV、scikit-image、sklearn 等重依赖。

### 2.2 非目标

本轮不实现：

- 完整 SLIC / Pixelated Image Abstraction；
- Deterministic Annealing palette optimization；
- Content-Adaptive EM downsampling；
- 自动 Floyd–Steinberg dithering；
- 神经网络后处理；
- 自动 sprite 分割；
- 复杂 vectorization。

---

## 3. 当前实现问题

### 3.1 Generate 与 Repixelize 采样语义不一致

核心代码允许：

```python
preserve_clusters = not pixelize_only
```

但浏览器正常 Generate 会显式传入：

```text
preserve_clusters = false
```

导致：

```text
Generate     → BOX
Repixelize   → NEAREST
```

同一张模型 raw 输出因此可能得到不同边缘。

### 3.2 BOX 会先污染边缘，再进行颜色量化

当前核心逻辑本质上是：

```text
高分辨率模型输出
  ↓
BOX resize
  ↓
Median Cut / nearest palette
```

BOX 会在轮廓边缘混合前景、背景和抗锯齿颜色，生成源图中不存在的中间 RGB 值。

随后调色板量化只能改变颜色，无法恢复原有几何结构。

### 3.3 NEAREST 也不足以完成 AI 像素画修复

NEAREST 避免颜色平均，但只是单点采样：

```text
一个 logical cell
  ↓
只看一个 source pixel
```

因此容易把：

- AI 抗锯齿残留；
- 细碎阴影；
- 高频纹理；
- 偶然噪点

直接抽成真实 logical pixel。

### 3.4 logical resolution 与原图分辨率绑定

当前：

```python
logical_width = source_width * density / 256
```

例如：

```text
1024 × 1024 + density 32 → 128 × 128
2048 × 2048 + density 32 → 256 × 256
```

同一个 density 会因为输入分辨率不同产生不同像素艺术尺度。

> [!NOTE] 架构演进说明 (2026-09-24)
> 初稿曾尝试将 Density 改造为“最长边绝对尺寸”，但该实验性方案会导致颗粒度失控及超大分辨率风险。最终系统正式确立并保留了 **256px 参考画布比例映射模型**，确立 Density 控制视觉颗粒度而非绝对分辨率。详见规范文档 [docs/specs/pixel-reduction-pipeline.md](file:///Users/akiya/Project/pixel/docs/specs/pixel-reduction-pipeline.md) 与架构决策 [docs/decisions/0005-reference-canvas-density-mapping.md](file:///Users/akiya/Project/pixel/docs/decisions/0005-reference-canvas-density-mapping.md)。

---

## 4. 重构后的总体架构

目标流程：

```text
Gemini raw image
      ↓
Resolve Target Grid
      ↓
Optional Grid Phase Alignment
      ↓
Palette Construction / Mapping
      ↓
Quantize → Vote
      ↓
Confidence-aware Cluster Cleanup
      ↓
Palette Validation
      ↓
Logical Pixel PNG
```

双轮生成：

```text
Source
  ↓
Gemini Pass 1
  ↓
reduce_pixel_art()
  ↓
nearest upscale
  ↓
Gemini Pass 2
  ↓
reduce_pixel_art()
  ↓
Final Output
```

Repixelize：

```text
Cached Gemini Raw
  ↓
reduce_pixel_art()
  ↓
Final Output
```

所有路径必须调用同一个 reducer。

---

## 5. Logical Grid 定义

> [!NOTE] 架构演进说明 (2026-09-24)
> **状态更替**：本节初稿提出的“最长边固定像素数 (Longest-edge)”定义在实践中已被正式废弃，由 **256px 参考画布比例映射 (Reference Canvas Mapping)** 取代。
> 实际生效规范与计算公式请参见规范文档 [docs/specs/pixel-reduction-pipeline.md](file:///Users/akiya/Project/pixel/docs/specs/pixel-reduction-pipeline.md#31-目标网格与分辨率计算-target-grid) 及决策记录 [docs/decisions/0005-reference-canvas-density-mapping.md](file:///Users/akiya/Project/pixel/docs/decisions/0005-reference-canvas-density-mapping.md)。以下 5.1 ~ 5.3 节内容仅作为初期设计草案历史记录保留。

### 5.1 新语义 (历史草案)

UI 中的：

```text
8 / 16 / 32 / 64 / 128
```

定义为：

> 最长边的 logical pixel 数量。

例如：

```text
1024 × 1024 + 32 → 32 × 32
2048 × 2048 + 32 → 32 × 32

1920 × 1080 + 64 → 64 × 36
3840 × 2160 + 64 → 64 × 36
```

### 5.2 建议实现

```python
def target_grid(source_size, density):
    width, height = source_size

    if width >= height:
        return (
            density,
            max(1, round(height / width * density)),
        )

    return (
        max(1, round(width / height * density)),
        density,
    )
```

这样 logical grid 只由：

```text
aspect ratio + density
```

决定，与输入图像物理分辨率无关。

---

## 6. Grid Phase Alignment

### 6.1 目的

AI 生成的“像素块”往往近似对齐，但实际 block boundary 可能相对目标网格偏移半个像素。

直接固定 cell boundary 会产生：

```text
真实边缘穿过 cell 中间
→ 一个 cell 同时包含多个区域
→ vote confidence 下降
→ 边缘抖动
```

### 6.2 不采用 FFT 自动网格检测

本项目已经知道目标 logical grid，因此无需重新猜测 grid size。

只需要估计：

```text
grid phase offset
```

### 6.3 算法

根据：

```text
cell_width  = source_width / target_width
cell_height = source_height / target_height
```

在：

```text
[-0.5 cell, +0.5 cell]
```

范围搜索 X / Y offset。

评分函数使用图像梯度：

```text
score(offset) =
sum(edge_strength near proposed cell boundaries)
```

只接受满足置信度阈值的 offset。

否则：

```text
offset = 0
```

### 6.4 约束

第一版：

- 不改变 pitch；
- 只搜索 phase；
- X / Y 独立；
- NumPy 实现；
- 低置信度时自动关闭。

后续可选：

```text
pitch ±5%
```

微调，但不属于 MVP。

---

## 7. Palette Pipeline

---

## 7.1 固定调色板

当前 RGB 欧氏距离：

```text
(r1-r2)^2 + (g1-g2)^2 + (b1-b2)^2
```

改为：

```text
sRGB
 ↓
linear RGB
 ↓
Oklab
 ↓
nearest palette entry
```

距离：

```text
ΔL² + Δa² + Δb²
```

可选亮度加权：

```text
(2 × ΔL)² + Δa² + Δb²
```

默认推荐普通 Oklab。

---

## 7.2 自动调色板

第一版使用轻量 Oklab K-Means：

```text
Raw opaque pixels
   ↓
随机固定 seed 采样 ≤ 20,000 pixels
   ↓
sRGB → Oklab
   ↓
K-Means
   ↓
6–8 iterations
   ↓
palette
```

要求：

- 固定 seed；
- 确定性初始化；
- 最大 64 colors；
- NumPy vectorization；
- 空 cluster 使用最远样本重新初始化；
- palette 最终保存为原始 sRGB。

不引入 sklearn。

---

## 8. Quantize → Vote

这是本次重构的核心。

### 8.1 旧方式

BOX：

```text
cell RGB pixels
   ↓
average RGB
   ↓
palette mapping
```

NEAREST：

```text
cell
 ↓
sample center pixel
```

### 8.2 新方式

先对高分辨率图进行 palette labeling：

```text
Source pixels
   ↓
nearest palette mapping
   ↓
palette_index_map
```

每个 logical cell：

```python
labels = palette_index_map[y0:y1, x0:x1]
counts = bincount(labels)

winner = argmax(counts)
confidence = counts[winner] / counts.sum()
```

最终：

```text
logical_pixel = palette[winner]
```

即：

> Quantize first, vote second.

这样不会产生新的混合色，并显著降低 AI 边缘噪声。

---

## 9. Vote Tie-break

当：

```text
top_count == second_count
```

按以下顺序决策：

1. cell center pixel 的 palette label；
2. 与 4-neighbor logical cells 当前候选更一致的 label；
3. palette index 较小者。

保证结果确定性。

---

## 10. Confidence Map

`quantize_vote()` 同时输出：

```text
logical_labels
confidence_map
```

其中：

```python
confidence[y, x] =
winning_votes / total_votes
```

用途：

- cleanup 判断；
- debug；
- 报告算法健康度；
- 后续自适应 refinement。

建议报告：

```json
{
  "mean_vote_confidence": 0.88,
  "low_confidence_cells": 17
}
```

---

## 11. Cluster Cleanup

所有 cleanup 在：

```text
palette index grid
```

上进行。

不要对最终 RGB 图执行普通 blur / sharpen。

最多执行 2 passes。

---

## 11.1 规则 A：Single-pixel Hole Fill

模式：

```text
A A A
A B A
A A A
```

若：

```text
8-neighbor 中 ≥ 6 个为 A
confidence(B) < threshold
```

则：

```text
B → A
```

建议：

```text
threshold = 0.55
```

---

## 11.2 规则 B：Orphan Pixel Removal

若当前 pixel：

- 没有任何同色 4-neighbor；
- 只有 0–1 个同色 diagonal neighbor；
- confidence < 0.45；
- 周围 dominant label ≥ 6 / 8；

则替换为 neighborhood dominant label。

目的：

```text
remove isolated speckles / AI jaggies
```

---

## 11.3 规则 C：1px Connected Component

对 palette label 做 connected component。

仅检查：

```text
component_size == 1
```

如果：

```text
confidence < 0.45
```

且周围存在明显 dominant color，则替换。

不自动删除：

```text
component_size >= 2
```

以避免破坏合法 pixel cluster。

---

## 11.4 不使用全图 Morphological Open/Close

原因：

- 会破坏合法 1px detail；
- 会改变轮廓厚度；
- 会误删眼睛、高光等真实细节；
- 对多色像素图的 RGB channel morphology 语义不稳定。

Morphology 仅作为未来可选高级模式，不进入默认流程。

---

## 12. Alpha

Alpha 与 RGB 独立处理。

高分辨率 alpha：

```text
alpha >= 128 → opaque
alpha < 128  → transparent
```

每个 logical cell：

```text
opaque_ratio >= 0.5 → opaque
otherwise          → transparent
```

透明 pixel：

```text
RGBA = (0, 0, 0, 0)
```

不参与 RGB vote。

---

## 13. Dithering

默认：

```text
NONE
```

不允许默认启用：

- Floyd–Steinberg；
- random noise；
- Atkinson。

原因：

- 会引入大量单像素；
- 破坏 cluster readability；
- 容易重新制造当前要解决的“脏”。

未来如增加 dithering：

```text
Bayer ordered dithering only
```

并限制在：

- 大面积平滑区域；
- 非 silhouette；
- 非 facial / focal region。

不属于本轮重构。

---

## 14. API 设计

新增：

```text
pixel_reduce.py
pixel_color.py
```

---

## 14.1 pixel_color.py

职责：

```python
srgb_to_linear()
linear_rgb_to_oklab()
srgb_to_oklab()

build_auto_palette()
map_to_palette()
```

---

## 14.2 pixel_reduce.py

职责：

```python
target_grid()
estimate_grid_phase()
quantize_vote()
cleanup_clusters()
reduce_pixel_art()
```

---

## 14.3 统一入口

```python
@dataclass
class ReductionResult:
    image: Image.Image
    labels: np.ndarray
    confidence: np.ndarray
    palette: tuple[tuple[int, int, int], ...]
    grid_offset: tuple[float, float]
    cleanup_changes: int


def reduce_pixel_art(
    source,
    *,
    target_size,
    palette=None,
    max_colors=16,
    align_grid=True,
    cleanup=True,
) -> ReductionResult:
    ...
```

---

## 15. pixel_redraw.py 调整

删除：

```text
preserve_clusters
```

作为跨层参数。

`pixel_redraw.py` 不再决定：

```text
BOX / NEAREST
```

模型 raw、第一轮 draft 和 repixelize 全部调用：

```python
reduce_pixel_art()
```

只有：

```text
普通用户输入的 local pixelize-only
```

可以保留单独的 photo reducer。

建议明确拆分：

```python
reduce_photo()
reduce_model_pixel_art()
```

避免再用一个 bool 控制两个语义完全不同的任务。

---

## 16. 双轮生成调整

第一轮：

```text
Gemini raw
  ↓
reduce_pixel_art
  ↓
logical clean draft
  ↓
nearest upscale to review size
```

第二轮 prompt 输入这张 clean draft。

第二轮结果：

```text
Gemini refined raw
  ↓
reduce_pixel_art
  ↓
final
```

第二轮模型负责：

- silhouette redesign；
- stair-step rhythm；
- cluster redesign；
- semantic detail。

本地 reducer 负责：

- hard grid；
- palette；
- vote；
- topology cleanup。

职责分离。

---

## 17. 报告字段

新增：

```json
{
  "reducer": "qvote-v1",
  "target_size": [64, 36],
  "grid_offset": [0.12, -0.08],
  "grid_alignment_applied": true,
  "mean_vote_confidence": 0.91,
  "low_confidence_cells": 11,
  "cleanup_changes": 7,
  "palette_metric": "oklab"
}
```

便于后续对比算法版本。

---

## 18. 测试

---

## 18.1 Unit Tests

必须覆盖：

### Logical Grid

```text
1024×1024 + 32 → 32×32
2048×2048 + 32 → 32×32
1920×1080 + 64 → 64×36
```

### Palette

- fixed palette subset；
- Oklab mapping deterministic；
- auto palette deterministic；
- alpha 不污染 palette。

### QVote

构造 cell：

```text
A A A A
A A B B
A A B B
A A A B
```

必须输出：

```text
A
```

### Cleanup

分别测试：

- isolated pixel；
- one-pixel hole；
- diagonal legitimate cluster；
- 高 confidence single-pixel detail。

---

## 18.2 Pipeline Invariants

必须满足：

### Deterministic

同输入同配置：

```text
SHA256(output_1) == SHA256(output_2)
```

### Palette Subset

```text
output_colors ⊆ requested_palette
```

### Generate / Repixelize Consistency

同 raw + 同参数：

```text
Generate reducer output
==
Repixelize reducer output
```

逐位一致。

### Idempotence

近似要求：

```text
R(nearest_upscale(R(x))) == R(x)
```

logical output 必须基本稳定。

---

## 19. Golden Corpus

建立：

```text
tests/golden/
```

建议 12–20 张固定输入，覆盖：

- 人脸；
- 全身角色；
- 头发；
- 深色轮廓；
- 浅色轮廓；
- 高对比背景；
- 复杂背景；
- 透明背景；
- 低色数 palette；
- 32 / 64 logical density。

每张保存：

```text
raw
legacy
nearest
qvote
qvote_cleanup
```

主要人工比较：

- silhouette；
- jaggy；
- isolated pixels；
- cluster readability；
- focal detail retention。

---

## 20. 自动质量指标

不使用 PSNR / SSIM 作为主要指标。

记录：

```text
isolated_pixel_count
single_pixel_component_count
low_confidence_cell_count
cleanup_changes
palette_color_count
mean_vote_confidence
```

趋势目标：

```text
isolated_pixel_count ↓
single_pixel_component_count ↓
mean_vote_confidence ↑
```

但不能以“孤立像素越少越好”为绝对目标。

---

## 21. 实施顺序

### Phase 1：修复现有语义

- 修复 Generate / Repixelize sampler 不一致；
- 移除跨层 `preserve_clusters`；
- 修改 density → logical longest edge；
- 补 regression tests。

### Phase 2：QVote

- Oklab conversion；
- fixed palette mapping；
- auto palette；
- palette label map；
- cell majority vote；
- confidence map。

完成后即可替换现有模型输出 pixelizer。

### Phase 3：Cleanup

实现：

- hole fill；
- orphan removal；
- size-1 component cleanup；
- confidence gating。

### Phase 4：Grid Alignment

- gradient projection；
- phase search；
- confidence threshold；
- report/debug visualization。

### Phase 5：Golden Evaluation

用固定 corpus 比较：

```text
legacy
nearest
qvote
qvote + cleanup
qvote + cleanup + alignment
```

确认 alignment 确实提升后再默认开启。

---

## 22. 明确暂缓的算法

以下方案有理论或实践价值，但当前不进入主线：

| 算法 | 暂缓原因 |
|---|---|
| SLIC Superpixels | 复杂度高，Gemini 已承担高层区域抽象 |
| Pixelated Image Abstraction 全算法 | 联合优化成本过高 |
| Content-Adaptive EM Downsampling | 实现与运行成本高，收益需单独验证 |
| Wu Quantization | 可作为后续 auto palette 对照组 |
| CIEDE2000 | 精度高但成本高，Oklab 足够作为默认 |
| 全图 Morphology | 容易损伤合法 pixel cluster |
| Floyd–Steinberg | 会增加单像素噪声 |
| Sharpen / Unsharp Mask | 无法解决 logical topology 问题 |

---

## 23. 最终设计原则

重构后不再把 Pixel-Redraw 看作：

> 高分辨率图片缩小 + 减色。

而是：

> 将模型输出解释成一个有限调色板上的 logical cell label map，并对该 label map 的网格、置信度和拓扑进行约束。

核心算法：

```text
Oklab Palette
      +
Quantize → Vote
      +
Confidence-aware Cluster Cleanup
      +
Optional Grid Phase Alignment
```

模型负责“画什么”。

Reducer 负责“哪些 logical pixels 最终存在”。

---

## 24. 参考依据

主要参考方向：

1. Timothy Gerstner et al., **Pixelated Image Abstraction**, 2012。  
   核心结论：像素艺术抽象不能等价为普通 resize；空间区域映射与 palette 应联合考虑。

2. Achanta et al., **SLIC Superpixels**。  
   核心结论：在感知颜色 + 空间位置中聚类，可获得规则且贴合边界的区域。

3. Johannes Kopf, Dani Lischinski, **Depixelizing Pixel Art**。  
   核心结论：像素艺术的核心问题包含局部连接关系、轮廓连续性与 diagonal ambiguity。

4. **PerfectPixel**。  
   AI 像素画修复实践：grid estimation、edge refinement、majority sampling。

5. **Unfake.js / unfake-core**。  
   AI 像素画修复实践：grid snap、dominant / qvote、palette quantization、jaggy cleanup。

6. **Oklab**。  
   适合图像处理与颜色操作的感知颜色空间，可替代 sRGB 欧氏 nearest-color。

7. Pixel-art practitioner literature。  
   核心原则：pixel cluster 是基本结构；孤立像素和不规则 jaggies 是主要噪声来源，但合法单像素细节不能被无条件删除。
