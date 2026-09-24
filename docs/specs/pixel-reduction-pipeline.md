# Spec: 像素化与降采样管线 (Pixel Reduction Pipeline)

## 1. 目标与范围

本文档定义 `pixel-redraw` 核心像素化与降采样算法的行为规范、业务规则与核心约束。
该管线运行在本地纯计算环境（CPython 及浏览器 WASM Pyodide），负责将大模型生成的图像或用户直接输入的参考图转换为高质量、网格对齐、严格受限调色板的像素画。

对应实现文件：
- [`pixel_redraw.py`](file:///Users/akiya/Project/pixel/pixel_redraw.py) —— 生成管线编排、协议构造、响应解析、不变量断言
- [`pixel_reduce.py`](file:///Users/akiya/Project/pixel/pixel_reduce.py) —— 网格计算、相位对齐、QVote 区域投票量化、拓扑清理
- [`pixel_color.py`](file:///Users/akiya/Project/pixel/pixel_color.py) —— Oklab 感知色彩空间转换、最近色映射、K-Means 自动调色板
- [`pixel_palettes.py`](file:///Users/akiya/Project/pixel/pixel_palettes.py) —— 预设调色板与元数据

---

## 2. 核心工作流

### 2.1 双轮生成流程 (Dual-Pass Generation)

默认启用双轮生成：
1. **Pass 1 (Draft Generation)**: 输入原图与初始提示词，调用 Gemini 获得初稿高分辨率渲染图。
2. **Intermediate Reduction**: 将初稿送入像素化 Reducer 得到逻辑像素网格草稿，并以最近邻放大（Nearest-Neighbor）格式保留。
3. **Pass 2 (Refinement)**: 将放大后的草稿图作为第一张图片、原图作为第二张参考图，输入精修提示词，由 Gemini 重新梳理轮廓与色块结构。
4. **Final Reduction**: 对精修输出再次执行统一像素化 Reducer，生成最终逻辑像素图像。

**容错与回退机制**：
- 若 Pass 2 失败（如模型超时、返回空内容、安全拦截等），系统自动回退使用 Pass 1 草稿作为最终结果，不会报错中断。
- 报告中通过 `refinement_applied: bool` 显式标识是否应用了精修。
- 用户可选择单轮模式（Single Pass）以跳过 Pass 2，节省 API 调用成本。

### 2.2 仅本地渲染 / 重像素化 (Repixelize)

- **Local Only 模式**：完全不发起上游模型调用，直接将用户输入图送入 Reducer，实现零 Token、零网络延迟的像素化。
- **Repixelize 模式**：切换分辨率或调色板时，复用内存中缓存的上游模型原始输出，直接重新运行 Reducer，无需重新调用模型。
- **一致性保证**：Generate、Refine 和 Repixelize **严格共享相同的 Reducer 逻辑与参数配置**，相同输入必产出逐位一致的二进制结果。

---

## 3. 算法与处理阶段规范

### 3.1 目标网格与分辨率计算 (Target Grid)
- **基准参考画布模型 (256px Reference Canvas)**：
  - Density（密度 $D \in \{8, 16, 32, 64, 128\}$）定义为 **256px 逻辑参考画布**上的网格单元数量（即每 256px 包含 $D$ 个逻辑像素）。
  - Density 控制的是**像素颗粒度（Granularity）**，而非固定输出绝对分辨率。任意分辨率的输入图像按比例映射，确保相同 Density 在不同分辨率下呈现一致的像素艺术块度感。
- **计算公式**：
  $$W_{grid} = \max\left(1, \operatorname{round}\left(\frac{W_{src} \times D}{256}\right)\right), \quad H_{grid} = \max\left(1, \operatorname{round}\left(\frac{H_{src} \times D}{256}\right)\right)$$
  宽高独立按 256 基准缩放，严格保持原图宽高比，无任何非等比拉伸或边缘裁剪。
- **密度档位与典型分辨率对照表**：

| Density ($D$) | 视觉颗粒度 (块大小@256px) | 256×256 输入 | 512×512 输入 | 1024×1024 输入 | 1920×1080 (16:9) 输入 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **8** (Ultra Low) | 32 px / 块 | 8×8 | 16×16 | 32×32 | 60×34 |
| **16** (Low) | 16 px / 块 | 16×16 | 32×32 | 64×64 | 120×68 |
| **32** (Medium, 默认) | 8 px / 块 | 32×32 | 64×64 | 128×128 | 240×135 |
| **64** (High) | 4 px / 块 | 64×64 | 128×128 | 256×256 | 480×270 |
| **128** (Ultra High) | 2 px / 块 | 128×128 | 256×256 | 512×512 | 960×540 |

### 3.2 网格相位对齐 (Grid Phase Alignment)
- AI 生成图往往在逻辑网格边缘带有轻微抗锯齿或错位。
- 在 $[-0.5, +0.5]$ 单个网格单元范围内以微小步长进行 2D 相位平移搜索。
- 通过评估网格边界线上的梯度对比度峰值，寻找最大化清晰边缘的相位偏移量 $(\Delta x, \Delta y)$。
- 若图像对比度不足（纯色或平滑图像），自动回退到默认居中对齐（偏移量 0）。

### 3.3 QVote 区域多数表决量化 (Quantize-then-Vote)
- **避免传统 RGB 均值降采样的混色污染**：先将采样单元内的每个高分辨率像素映射到调色板最近色（在 Oklab 空间计算），再统计单元内各颜色票数。
- 出现票数最多的颜色作为该逻辑像素的代表色（Mode Color）。
- 计算胜出颜色的得票占比作为**投票置信度**（Confidence），供后续拓扑清理使用。
- Alpha 通道：若单元内透明像素占比超过 50%，则该像素标记为透明。

### 3.4 感知色彩空间与调色板 (Oklab & Palette)
- **颜色空间**：所有色差比较与 K-Means 聚类均在 Oklab 感知色彩空间进行（`sRGB` $\leftrightarrow$ `Linear RGB` $\leftrightarrow$ `Oklab`）。
- **固定调色板**：用户选择内置预设（15 款硬件/艺术调色板）或自定义 HEX 调色板时，最近色映射在平局时严格选取索引较低的颜色（保证确定性）。
- **自动调色板 (Auto Palette)**：在 Oklab 空间执行轻量、确定性（固定随机种子）的 K-Means 聚类，提取 $K$ 种最具代表性的颜色。

### 3.5 像素拓扑与孤立像素清理 (Topology Cleanup)
- **单像素孔洞填补 (Hole Fill)**：当一个低置信度像素被 4-邻域或 8-邻域同一种主导颜色完全包围时，将其填补为主导颜色。
- **孤立杂点消除 (Orphan Pixel Removal)**：当单个像素与周围邻域颜色孤立且置信度较低时，将其替换为邻域占绝对多数的颜色（$\ge 75\%$ 邻域一致性）。
- **高置信度特征保护**：若单像素具有很高的采样置信度（如高对比度眼睛、细微高光），清理算法严格予以保留，避免抹除关键细节。
- 清理迭代次数受 `cleanup_passes`（默认 2 次）严格限制，防止过度平滑。

---

## 4. 核心不变量与质量断言

1. **调色板闭包不变量 (Palette Subset Invariant)**：
   - 凡指定了调色板（固定预设或自定义）的输出，结果中的所有非透明像素颜色**必须且仅能**是该调色板的子集。
   - `pixel_redraw.py` 内置严格断言检查，如有越界颜色会直接阻断并抛出异常。
2. **纯计算无副作用**：
   - 算法模块不依赖任何外部 I/O、全局系统环境或非确定性随机源。
3. **逐位一致性 (Bitwise Determinism)**：
   - 在相同平台或跨 CPython 3.9+ / Pyodide WASM 环境下，固定输入在相同参数下生成的逻辑像素图哈希必须完全一致。
